from microio import ServiceThread
import zmq

from .polling import poll_in


class HeartbeatThread(ServiceThread):
    def __init__(self, context: zmq.Context, addr: str, poll_ms: int = 100):
        "Initialize heartbeat thread bound to `addr`."
        super().__init__(name="heartbeat-thread", reraise=True)
        self.context = context
        self.addr = addr
        self.poll_ms = poll_ms

    def run_service(self):
        "Echo heartbeat requests on REP socket until stopped."
        sock = None
        try:
            sock = self.context.socket(zmq.REP)
            sock.linger = 0
            sock.bind(self.addr)
            self.started()
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self.scope.closed:
                if poll_in(poller, sock, self.poll_ms):
                    msg = sock.recv()
                    sock.send(msg)
        finally:
            if sock is not None: sock.close(0)
