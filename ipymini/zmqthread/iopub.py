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
        self.priority_q = queue.Queue()
        self.q = queue.Queue(maxsize=int(qmax))
        self.enqueued = 0
        self.sent = 0
        self.send_errors = 0

    def send(self, msg_type, content, parent, metadata=None, ident=None, buffers=None):
        "Queue an IOPub message for send; never drop status messages."
        self.enqueued += 1
        item = (msg_type, content, parent, metadata, ident, buffers)
        try: self.q.put_nowait(item)
        except queue.Full:
            if msg_type == "status":
                self.priority_q.put(item)
                return
            backlog = self.enqueued - self.sent
            if backlog in (100, 500, 1000): log.warning("IOPub queue full; dropping non-critical output. enq=%d sent=%d", self.enqueued, self.sent)

    def _get_next(self):
        try: return self.q.get(timeout=0.05)
        except queue.Empty: pass
        try: return self.priority_q.get_nowait()
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
                msg = self.session.msg(msg_type, content, parent=parent)
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
