import socket, threading, time

import zmq
from jupyter_client.session import Session

from ipymini.zmqthread import AsyncRouterThread


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_async_router_sends_enqueued_reply_without_new_inbound():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    addr = f"tcp://127.0.0.1:{_free_port()}"

    got = {"msg": None, "idents": None}
    seen = threading.Event()

    def handler(msg, idents):
        got["msg"] = msg
        got["idents"] = idents
        seen.set()
        # no immediate reply

    router = AsyncRouterThread(context=ctx, session=session, bind_addr=addr, handler=handler, log_label="shell")
    router.start()
    assert router.ready.wait(1)

    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.connect(addr)
    time.sleep(0.05)

    try:
        session.send(dealer, "kernel_info_request", {}, parent=None)
        assert seen.wait(1)

        # Enqueue a reply without sending any new inbound message.
        router.enqueue(("kernel_info_reply", {"status": "ok"}, got["msg"], got["idents"]))

        poller = zmq.Poller()
        poller.register(dealer, zmq.POLLIN)
        events = dict(poller.poll(1000))
        assert dealer in events
        _idents, reply = session.recv(dealer, mode=0)
        assert reply["msg_type"] == "kernel_info_reply"
    finally:
        router.stop()
        router.join(timeout=1)
        dealer.close(0)
