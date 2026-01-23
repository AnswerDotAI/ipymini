import zmq
from .kernel_utils import load_connection, start_kernel


def test_heartbeat_echo() -> None:
    with start_kernel() as (km, _kc):
        conn = load_connection(km)

        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.linger = 0
        sock.connect(f"{conn['transport']}://{conn['ip']}:{conn['hb_port']}")
        payload = b"ping"
        sock.send(payload)
        reply = sock.recv()
        sock.close(0)
        assert reply == payload
