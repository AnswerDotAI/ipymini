import logging, queue
from microio import ServiceThread
import zmq

log = logging.getLogger("ipymini.zmqthread")


class IOPubThread(ServiceThread):
    "IOPub sender thread using a sync PUB socket with a bounded queue."

    def __init__(self, context: zmq.Context, addr: str, session, qmax: int = 10000, sndhwm: int | None = None):
        super().__init__(name="iopub-thread", reraise=True)
        self.context = context
        self.addr = addr
        self.session = session
        self.sndhwm = sndhwm
        self.qmax = int(qmax)
        self.q = queue.Queue()
        self.enqueued = 0
        self.dropped = 0
        self.sent = 0
        self.send_errors = 0

    def send(self, msg_type, content, parent, metadata=None, ident=None, buffers=None):
        "Queue an IOPub message in FIFO order; drop non-status output past qmax, never status."
        self.enqueued += 1
        if msg_type != "status" and self.q.qsize() >= self.qmax:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("IOPub queue full; dropping non-critical output. dropped=%d enq=%d sent=%d", self.dropped, self.enqueued, self.sent)
            return
        self.q.put_nowait((msg_type, content, parent, metadata, ident, buffers))

    def _get_next(self):
        try: return self.q.get(timeout=0.05)
        except queue.Empty: return None

    def run_service(self):
        sock = None
        try:
            sock = self.context.socket(zmq.PUB)
            sock.linger = 0
            if self.sndhwm is not None:
                try: sock.sndhwm = int(self.sndhwm)
                except ValueError: pass
            sock.bind(self.addr)
            self.started()
            while not self.scope.closed:
                item = self._get_next()
                if item is None: continue
                msg_type, content, parent, metadata, ident, buffers = item
                try:
                    self.session.send(sock, msg_type, content, parent=parent, metadata=metadata, ident=ident, buffers=buffers)
                    self.sent += 1
                except Exception as exc:
                    self.send_errors += 1
                    log.error("IOPub send error: %s", exc, exc_info=exc)
        finally:
            try:
                if sock is not None: sock.close(0)
            except Exception:
                if not self.scope.closed: log.exception("IOPub socket close failed")
