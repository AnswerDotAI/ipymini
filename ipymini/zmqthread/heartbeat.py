import threading
import zmq


class HeartbeatThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr: str, poll_ms: int = 100):
        "Initialize heartbeat thread bound to `addr`."
        super().__init__(daemon=True, name="heartbeat-thread")
        self.context = context
        self.addr = addr
        self.poll_ms = poll_ms
        self.stop_event = threading.Event()

    def run(self):
        "Echo heartbeat requests on REP socket until stopped."
        sock = None
        try:
            sock = self.context.socket(zmq.REP)
            sock.linger = 0
            sock.bind(self.addr)
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self.stop_event.is_set():
                events = dict(poller.poll(self.poll_ms))
                if sock in events and events[sock] & zmq.POLLIN:
                    msg = sock.recv()
                    sock.send(msg)
        finally:
            if sock is not None: sock.close(0)

    def stop(self): self.stop_event.set()
