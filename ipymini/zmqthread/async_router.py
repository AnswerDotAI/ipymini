import asyncio, contextlib, logging, sys, time, traceback
from fastcore.basics import nested_idx
from microio import EndOfStream, LoopServiceThread
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

        self.sock = None
        self.outbox = ThreadBoundAsyncQueue()
        self.wake_item = None  # one reply pulled by the wakeup waiter, not yet sent
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
        if self.loop is not None and self.sock is not None:
            try: self.loop.call_soon_threadsafe(self.sock.close, 0)
            except RuntimeError: pass
        return first

    async def run_async(self):
        self.loop = asyncio.get_running_loop()
        if self._needs_selector_loop(self.loop): log.warning("Windows event loop may not support zmq.asyncio; consider SelectorEventLoop policy.")
        self.outbox.bind(self.loop)

        async_ctx = zmq.asyncio.Context.shadow(self.context) if hasattr(zmq.asyncio.Context, "shadow") else zmq.asyncio.Context.instance()
        sock = async_ctx.socket(zmq.ROUTER)
        if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
        sock.linger = 0
        sock.bind(self.bind_addr)
        self.sock = sock
        self.started()

        get_task = None
        try:
            while not self.scope.closed:
                await self._drain_outbox(sock)
                # Wait on the outbox and the socket together so a reply enqueued from a subshell
                # thread wakes the loop at once instead of waiting out the poll timeout.
                if get_task is None: get_task = asyncio.ensure_future(self._next_outbox_item())
                poll_fut = asyncio.ensure_future(sock.poll(timeout=self.poll_ms, flags=zmq.POLLIN))
                done, _ = await asyncio.wait({get_task, poll_fut}, return_when=asyncio.FIRST_COMPLETED)
                if get_task in done:
                    if (item := get_task.result()) is not None: self.wake_item = item
                    get_task = None
                if poll_fut not in done:
                    poll_fut.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await poll_fut
                    continue
                try: ready = poll_fut.result()
                except zmq.ZMQError as exc:
                    if not self.scope.closed:
                        log.exception("%s router stopped while polling", self.log_label)
                        raise
                    return
                if not ready: continue
                await self._handle_recv(sock)
        except asyncio.CancelledError: return
        finally:
            if get_task is not None: get_task.cancel()
            try: sock.close(0)
            except Exception:
                if not self.scope.closed: log.exception("%s router socket close failed", self.log_label)

    async def _next_outbox_item(self):
        "Await the next queued reply; None once the outbox is closed."
        try: return await self.outbox.get()
        except EndOfStream: return None

    async def _drain_outbox(self, sock: zmq.asyncio.Socket):
        if self.wake_item is not None:
            item, self.wake_item = self.wake_item, None
            await self._send_item(sock, item)
        for item in self.outbox.drain_nowait(max_items=self.max_send_batch): await self._send_item(sock, item)

    async def _send_item(self, sock: zmq.asyncio.Socket, item):
        msg_type, content, parent, idents = item
        msg = self.session.msg(msg_type, content, parent=parent)
        frames = self.session.serialize(msg, ident=idents)
        try:
            fut = sock.send_multipart(frames)
            if asyncio.isfuture(fut): await fut
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
