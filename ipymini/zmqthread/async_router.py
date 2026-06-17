import asyncio, logging, sys, time, traceback
from fastcore.basics import nested_idx
from microio import LoopServiceThread
import zmq, zmq.asyncio

from .queues import ThreadBoundAsyncQueue

log = logging.getLogger("ipymini.zmqthread")


class AsyncRouterThread(LoopServiceThread):
    "Async ROUTER socket thread for shell/control channels."

    def __init__(self, *, context, session, bind_addr, handler, log_label, poll_ms=50, max_send_batch=100):
        super().__init__(name=f"{log_label}-router", reraise=True)
        self.context = context
        self.session = session
        self.bind_addr = bind_addr
        self.handler = handler
        self.log_label = log_label
        self.poll_ms = poll_ms
        self.max_send_batch = max_send_batch

        self.outbox = ThreadBoundAsyncQueue()
        self.enqueued = 0
        self.sent = 0
        self.send_errors = 0

    def enqueue(self, item) -> int:
        self.enqueued += 1
        backlog = self.enqueued - self.sent
        if backlog in (1000, 2000, 5000): log.warning("%s backlog growing: enq=%d sent=%d", self.log_label, self.enqueued, self.sent)
        self.outbox.put(item)
        return self.enqueued

    def wait_for_sent(self, target: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sent >= target: return True
            time.sleep(0.01)
        return self.sent >= target

    def stop(self):
        first = super().stop("router stop")
        self.outbox.close()
        return first

    async def run_async(self):
        self.loop = asyncio.get_running_loop()
        if self._needs_selector_loop(self.loop): log.warning("Windows event loop may not support zmq.asyncio; consider SelectorEventLoop policy.")
        self.outbox.bind(self.loop)

        sock = zmq.asyncio.Context.shadow(self.context).socket(zmq.ROUTER)
        sock.router_handover,sock.linger = 1,0  # let client reconnect, drop msgs on close instead of waiting
        sock.bind(self.bind_addr)
        self.started()

        try:
            while not self.scope.closed:
                await self._drain_outbox(sock)
                try: ready = await sock.poll(timeout=self.poll_ms, flags=zmq.POLLIN)
                except zmq.ZMQError as exc:
                    if not self.scope.closed:
                        log.exception("%s router stopped while polling", self.log_label)
                        raise
                    return
                if not ready: continue
                await self._handle_recv(sock)
        except asyncio.CancelledError: return
        finally:
            try: sock.close(0)
            except Exception:
                if not self.scope.closed: log.exception("%s router socket close failed", self.log_label)

    async def _drain_outbox(self, sock: zmq.asyncio.Socket):
        for item in self.outbox.drain_nowait(max_items=self.max_send_batch):
            msg_type, content, parent, idents = item
            msg = self.session.msg(msg_type, content, parent=parent)
            frames = self.session.serialize(msg, ident=idents)
            try:
                await sock.send_multipart(frames)
                self.sent += 1
            except Exception as exc:
                self.send_errors += 1
                log.error("%s send error: %s", self.log_label, exc, exc_info=exc)

    async def _handle_recv(self, sock: zmq.asyncio.Socket):
        try: msg_list = await sock.recv_multipart(copy=False)
        except zmq.ZMQError as exc:
            if not self.scope.closed:
                log.exception("%s router stopped while receiving", self.log_label)
                raise
            return
        idents, msg_list = self.session.feed_identities(msg_list, copy=False)
        try: msg = self.session.deserialize(msg_list, content=True, copy=False)
        except ValueError as err:
            if "Duplicate Signature" not in str(err): log.warning("Bad message signature", exc_info=True)
            return
        if msg is None: return
        try: self.handler(msg, idents)
        except Exception as exc:
            log.warning("Error in %s handler: %s", self.log_label, msg.get("msg_type"), exc_info=True)
            await self._send_handler_error(sock, msg, idents, exc)

    async def _send_handler_error(self, sock: zmq.asyncio.Socket, parent: dict, idents, exc: Exception):
        msg_type = nested_idx(parent, "header", "msg_type") or ""
        if not msg_type.endswith("_request"): return
        content = dict(status="error", ename=type(exc).__name__, evalue=str(exc),
            traceback=traceback.format_exception(type(exc), exc, exc.__traceback__))
        reply_type = msg_type.replace("_request", "_reply")
        msg = self.session.msg(reply_type, content, parent=parent)
        frames = self.session.serialize(msg, ident=idents)
        try:
            fut = sock.send_multipart(frames)
            if asyncio.isfuture(fut): await fut
            self.sent += 1
        except Exception:
            self.send_errors += 1
            log.exception("%s handler error reply failed", self.log_label)

    @staticmethod
    def _needs_selector_loop(loop: asyncio.AbstractEventLoop) -> bool: return sys.platform.startswith("win") and not isinstance(loop, asyncio.SelectorEventLoop)
