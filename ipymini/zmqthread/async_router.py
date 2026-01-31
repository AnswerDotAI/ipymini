import asyncio, logging, threading, sys, time
import zmq, zmq.asyncio

from .queues import ThreadBoundAsyncQueue

log = logging.getLogger("ipymini.zmqthread")


class AsyncRouterThread(threading.Thread):
    "Async ROUTER socket thread for shell/control channels."

    def __init__(self, *, context, session, bind_addr, handler, log_label, poll_ms=50, max_send_batch=100):
        super().__init__(daemon=True, name=f"{log_label}-router")
        self.context = context
        self.session = session
        self.bind_addr = bind_addr
        self.handler = handler
        self.log_label = log_label
        self.poll_ms = poll_ms
        self.max_send_batch = max_send_batch

        self.loop = None
        self.sock = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.outbox = ThreadBoundAsyncQueue()
        self.enqueued = 0
        self.sent = 0
        self.send_errors = 0

    def enqueue(self, item) -> int:
        self.enqueued += 1
        backlog = self.enqueued - self.sent
        if backlog in (1000, 2000, 5000):
            log.warning("%s backlog growing: enq=%d sent=%d", self.log_label, self.enqueued, self.sent)
        self.outbox.put(item)
        return self.enqueued

    def wait_for_sent(self, target: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sent >= target: return True
            time.sleep(0.01)
        return self.sent >= target

    def stop(self):
        self.stop_event.set()
        self.outbox.suppress_late_puts()
        self.outbox.put(None)
        if self.loop is not None and self.sock is not None:
            try: self.loop.call_soon_threadsafe(self.sock.close, 0)
            except RuntimeError: pass

    def run(self):
        try: asyncio.run(self._run())
        finally:
            self.loop = None
            self.sock = None
            self.ready.clear()

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        if self._needs_selector_loop(self.loop):
            log.warning("Windows event loop may not support zmq.asyncio; consider SelectorEventLoop policy.")
        self.outbox.bind(self.loop)

        async_ctx = zmq.asyncio.Context.shadow(self.context) if hasattr(zmq.asyncio.Context, "shadow") else zmq.asyncio.Context.instance()
        sock = async_ctx.socket(zmq.ROUTER)
        if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
        sock.linger = 0
        sock.bind(self.bind_addr)
        self.sock = sock
        self.ready.set()

        try:
            while not self.stop_event.is_set():
                await self._drain_outbox(sock)
                try: ready = await sock.poll(timeout=self.poll_ms, flags=zmq.POLLIN)
                except zmq.ZMQError: return
                if not ready: continue
                await self._handle_recv(sock)
        except asyncio.CancelledError: return
        finally:
            try: sock.close(0)
            except Exception: pass

    async def _drain_outbox(self, sock: zmq.asyncio.Socket):
        sent = 0
        for item in self.outbox.drain_nowait():
            if item is None: return
            msg_type, content, parent, idents = item
            msg = self.session.msg(msg_type, content, parent=parent)
            frames = self.session.serialize(msg, ident=idents)
            try:
                fut = sock.send_multipart(frames)
                if asyncio.isfuture(fut): await fut
                self.sent += 1
                sent += 1
                if sent >= self.max_send_batch: return
            except Exception as exc:
                self.send_errors += 1
                log.error("%s send error: %s", self.log_label, exc, exc_info=exc)

    async def _handle_recv(self, sock: zmq.asyncio.Socket):
        try: msg_list = await sock.recv_multipart(copy=False)
        except zmq.ZMQError: return
        idents, msg_list = self.session.feed_identities(msg_list, copy=False)
        try: msg = self.session.deserialize(msg_list, content=True, copy=False)
        except ValueError as err:
            if "Duplicate Signature" not in str(err): log.warning("Bad message signature", exc_info=True)
            return
        if msg is None: return
        try: self.handler(msg, idents)
        except Exception: log.warning("Error in %s handler: %s", self.log_label, msg.get("msg_type"), exc_info=True)

    @staticmethod
    def _needs_selector_loop(loop: asyncio.AbstractEventLoop) -> bool:
        return sys.platform.startswith("win") and not isinstance(loop, asyncio.SelectorEventLoop)
