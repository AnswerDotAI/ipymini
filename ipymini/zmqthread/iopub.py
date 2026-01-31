import logging, queue, threading
import zmq

log = logging.getLogger("ipymini.zmqthread")


class IOPubThread(threading.Thread):
    "IOPub sender thread using a sync PUB socket with a bounded queue."

    def __init__(self, context: zmq.Context, addr: str, session, qmax: int = 10000, sndhwm: int | None = None):
        super().__init__(daemon=True, name="iopub-thread")
        self.context = context
        self.addr = addr
        self.session = session
        self.sndhwm = sndhwm
        self.stop_event = threading.Event()
        self.q = queue.Queue(maxsize=int(qmax))
        self.enqueued = 0
        self.sent = 0
        self.send_errors = 0

    def send(self, msg_type, content, parent, metadata=None, ident=None, buffers=None):
        "Queue an IOPub message for send; drop on full queue."
        self.enqueued += 1
        try: self.q.put_nowait((msg_type, content, parent, metadata, ident, buffers))
        except queue.Full:
            backlog = self.enqueued - self.sent
            if backlog in (100, 500, 1000): log.warning("IOPub queue full; dropping. enq=%d sent=%d", self.enqueued, self.sent)

    def run(self):
        sock = None
        try:
            sock = self.context.socket(zmq.PUB)
            sock.linger = 0
            if self.sndhwm is not None:
                try: sock.sndhwm = int(self.sndhwm)
                except ValueError: pass
            sock.bind(self.addr)
            while True:
                item = self.q.get()
                if item is None or self.stop_event.is_set(): break
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
            except Exception: pass

    def stop(self):
        self.stop_event.set()
        try: self.q.put_nowait(None)
        except queue.Full:
            while True:
                try: self.q.get_nowait()
                except queue.Empty: break
            self.q.put_nowait(None)
