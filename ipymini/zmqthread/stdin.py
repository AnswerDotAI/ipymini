import itertools, logging, queue, threading
from fastcore.basics import nested_idx
from microio import RequestRegistry, ServiceThread
import zmq

from .polling import poll_in

log = logging.getLogger("ipymini.zmqthread")


class StdinRouterThread(ServiceThread):
    def __init__(self, context: zmq.Context, addr: str, session, poll_ms: int = 50):
        "Initialize stdin router for input_request/reply."
        super().__init__(name="stdin-router", reraise=True)
        self.context = context
        self.addr = addr
        self.session = session
        self.poll_ms = poll_ms
        self.waiters = RequestRegistry()
        self.ids = itertools.count(1)
        self.pending_lock = threading.Lock()
        self.requests = queue.Queue()
        self.pending = {}
        self.pending_by_ident = {}
        self.socket = None

    def request_input(self, prompt, password, parent, ident, timeout=None) -> str:
        "Send input_request and wait for input_reply; honors `timeout`."
        if self.scope.closed: raise RuntimeError("stdin router stopped")
        reply = self.waiters.reply(next(self.ids))
        self.requests.put((prompt, password, parent, ident, reply))
        try: return reply.wait(timeout=timeout)
        finally: self._forget(reply)

    def run_service(self):
        "Route input_reply messages to waiting queues."
        sock = None
        try:
            sock = self.context.socket(zmq.ROUTER)
            sock.linger = 0
            if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
            sock.bind(self.addr)
            self.socket = sock
            self.started()
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self.scope.closed:
                self._drain_requests(sock)
                if poll_in(poller, sock, self.poll_ms):
                    try: idents, msg = self.session.recv(sock, mode=0)
                    except ValueError as err:
                        if "Duplicate Signature" not in str(err): log.warning("Error decoding stdin message: %s", err)
                        continue
                    if msg is None: continue
                    if msg.get("msg_type") != "input_reply": continue
                    self._resolve_input_reply(idents, msg)
        finally:
            self._fail_waiters(RuntimeError("stdin router stopped"))
            if sock is not None: sock.close(0)

    def _drain_requests(self, sock: zmq.Socket):
        while True:
            try: prompt, password, parent, ident, reply = self.requests.get_nowait()
            except queue.Empty: return
            if reply.key not in self.waiters: continue
            msg = self.session.send(sock, "input_request", {"prompt": prompt, "password": password}, parent=parent, ident=ident)
            msg_id = nested_idx(msg, "header", "msg_id")
            key = tuple(ident or [])
            with self.pending_lock:
                if msg_id: self.pending[msg_id] = (key, reply)
                self.pending_by_ident[key] = reply

    def _resolve_input_reply(self, idents, msg: dict):
        parent = msg.get("parent_header", {})
        msg_id = parent.get("msg_id")
        reply = None
        if msg_id:
            with self.pending_lock:
                pending = self.pending.pop(msg_id, None)
                if pending is not None:
                    ident_key, reply = pending
                    if self.pending_by_ident.get(ident_key) is reply: self.pending_by_ident.pop(ident_key, None)
        if reply is None:
            key = tuple(idents or [])
            with self.pending_lock: reply = self.pending_by_ident.pop(key, None)
        if reply is not None: reply.resolve(nested_idx(msg, "content", "value") or "")

    def _forget(self, reply):
        with self.pending_lock:
            for msg_id, (_key, pending_reply) in list(self.pending.items()):
                if pending_reply is reply: self.pending.pop(msg_id, None)
            for key, pending_reply in list(self.pending_by_ident.items()):
                if pending_reply is reply: self.pending_by_ident.pop(key, None)

    def _fail_waiters(self, exc: BaseException):
        self.waiters.fail_all(exc)
        with self.pending_lock:
            self.pending.clear()
            self.pending_by_ident.clear()
        while True:
            try: _prompt, _password, _parent, _ident, reply = self.requests.get_nowait()
            except queue.Empty: break
            reply.fail(exc)

    def interrupt_pending(self):
        "Cancel pending input requests and wake any waiters."
        self._fail_waiters(KeyboardInterrupt())

    def stop(self, reason: str | None = "stop")->bool:
        first = super().stop(reason)
        self._fail_waiters(RuntimeError("stdin router stopped"))
        return first
