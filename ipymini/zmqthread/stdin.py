import logging, queue, threading, time
import zmq

log = logging.getLogger("ipymini.zmqthread")

input_interrupted = object()


class StdinRouterThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr: str, session, poll_ms: int = 50):
        "Initialize stdin router for input_request/reply."
        super().__init__(daemon=True, name="stdin-router")
        self.context = context
        self.addr = addr
        self.session = session
        self.poll_ms = poll_ms
        self.stop_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.pending_lock = threading.Lock()
        self.requests = queue.Queue()
        self.pending = {}
        self.pending_by_ident = {}
        self.socket = None

    def request_input(self, prompt, password, parent, ident, timeout=None) -> str:
        "Send input_request and wait for input_reply; honors `timeout`."
        response_queue = queue.Queue()
        self.requests.put((prompt, password, parent, ident, response_queue))
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.interrupt_event.is_set():
                self.interrupt_event.clear()
                raise KeyboardInterrupt
            if self.stop_event.is_set(): raise RuntimeError("stdin router stopped")
            try:
                if deadline is None: value = response_queue.get(timeout=0.1)
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining == 0.0: raise TimeoutError("timed out waiting for input reply")
                    value = response_queue.get(timeout=min(0.1, remaining))
                if value is input_interrupted:
                    self.interrupt_event.clear()
                    raise KeyboardInterrupt
                return value
            except queue.Empty: continue

    def run(self):
        "Route input_reply messages to waiting queues."
        sock = None
        try:
            sock = self.context.socket(zmq.ROUTER)
            sock.linger = 0
            if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
            sock.bind(self.addr)
            self.socket = sock
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self.stop_event.is_set():
                self._drain_requests(sock)
                events = dict(poller.poll(self.poll_ms))
                if sock in events and events[sock] & zmq.POLLIN:
                    try: idents, msg = self.session.recv(sock, mode=0)
                    except ValueError as err:
                        if "Duplicate Signature" not in str(err): log.warning("Error decoding stdin message: %s", err)
                        continue
                    if msg is None: continue
                    if msg.get("msg_type") != "input_reply": continue
                    parent = msg.get("parent_header", {})
                    msg_id = parent.get("msg_id")
                    waiter = None
                    if msg_id:
                        with self.pending_lock:
                            pending = self.pending.pop(msg_id, None)
                            if pending is not None:
                                ident_key, waiter = pending
                                if self.pending_by_ident.get(ident_key) is waiter: self.pending_by_ident.pop(ident_key, None)
                    if waiter is None:
                        key = tuple(idents or [])
                        with self.pending_lock: waiter = self.pending_by_ident.pop(key, None)
                    if waiter is not None:
                        value = msg.get("content", {}).get("value", "")
                        waiter.put(value)
        finally:
            if sock is not None: sock.close(0)

    def _drain_requests(self, sock: zmq.Socket):
        while True:
            try: prompt, password, parent, ident, waiter = self.requests.get_nowait()
            except queue.Empty: return
            if self.interrupt_event.is_set():
                waiter.put(input_interrupted)
                continue
            msg = self.session.send(sock, "input_request", {"prompt": prompt, "password": password}, parent=parent, ident=ident)
            msg_id = msg.get("header", {}).get("msg_id")
            key = tuple(ident or [])
            with self.pending_lock:
                if msg_id: self.pending[msg_id] = (key, waiter)
                self.pending_by_ident[key] = waiter

    def stop(self): self.stop_event.set()

    def interrupt_pending(self):
        "Cancel pending input requests and wake any waiters."
        self.interrupt_event.set()
        waiters = []
        seen = set()
        def _add_waiter(waiter):
            if waiter in seen: return
            seen.add(waiter)
            waiters.append(waiter)
        with self.pending_lock:
            for _key, waiter in self.pending.values(): _add_waiter(waiter)
            for waiter in self.pending_by_ident.values(): _add_waiter(waiter)
            self.pending.clear()
            self.pending_by_ident.clear()
        while True:
            try: _prompt, _password, _parent, _ident, waiter = self.requests.get_nowait()
            except queue.Empty: break
            _add_waiter(waiter)
        for waiter in waiters: waiter.put(input_interrupted)
