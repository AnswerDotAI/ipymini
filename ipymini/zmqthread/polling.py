import zmq


def poll_in(poller: zmq.Poller, sock: zmq.Socket, timeout: int)->bool:
    events = dict(poller.poll(timeout))
    return bool(events.get(sock, 0) & zmq.POLLIN)
