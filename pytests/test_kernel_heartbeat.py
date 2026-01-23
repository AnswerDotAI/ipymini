import json

import zmq

from .kernel_utils import start_kernel


def test_heartbeat_echo() -> None:
    with start_kernel() as (km, _kc):
        with open(km.connection_file, encoding="utf-8") as f:
            conn = json.load(f)

        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.linger = 0
        sock.connect(f"{conn['transport']}://{conn['ip']}:{conn['hb_port']}")
        payload = b"ping"
        sock.send(payload)
        reply = sock.recv()
        sock.close(0)
        assert reply == payload
