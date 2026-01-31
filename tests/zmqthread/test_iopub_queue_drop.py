import socket

import zmq
from jupyter_client.session import Session

from ipymini.zmqthread import IOPubThread


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_iopub_send_drops_when_queue_full_does_not_block():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    addr = f"tcp://127.0.0.1:{_free_port()}"

    iopub = IOPubThread(ctx, addr, session, qmax=1)
    # Intentionally do not start the thread; we only test enqueue/drop behavior.
    iopub.send("status", {"execution_state": "idle"}, parent=None)
    iopub.send("status", {"execution_state": "busy"}, parent=None)  # dropped
    assert iopub.q.qsize() == 1
    assert iopub.enqueued == 2
