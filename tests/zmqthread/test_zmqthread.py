import asyncio, socket, threading, time

import zmq
from jupyter_client.session import Session
from microio import Mailbox

from ipymini.zmqthread import AsyncRouterThread, HeartbeatThread, IOPubThread, StdinRouterThread


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _poll_recv(sock, session, timeout:int = 1000):
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    assert sock in dict(poller.poll(timeout))
    return session.recv(sock, mode=0)


def test_zmqthread_features():
    q = Mailbox()  # the router's outbox: thread-safe put + asyncio get, drops late puts after close
    q.put("a")

    async def _runner():
        q.bind(asyncio.get_running_loop())
        assert await asyncio.wait_for(q.get(), timeout=1) == "a"

    asyncio.run(_runner())
    q.receive.close()
    q.put("ignored")  # late_send="drop": no raise after the loop is gone

    q = Mailbox()

    async def _runner():
        q.bind(asyncio.get_running_loop())
        q.put("x")
        assert await asyncio.wait_for(q.get(), timeout=1) == "x"

    asyncio.run(_runner())

    ctx = zmq.Context.instance()
    session = Session(key=b"")
    hb_addr = f"tcp://127.0.0.1:{_free_port()}"
    hb = HeartbeatThread(ctx, hb_addr)
    hb.start()
    try:
        req = ctx.socket(zmq.REQ)
        req.linger = 0
        req.connect(hb_addr)
        req.send(b"ping")
        poller = zmq.Poller()
        poller.register(req, zmq.POLLIN)
        assert req in dict(poller.poll(1000))
        assert req.recv() == b"ping"
    finally:
        hb.stop()
        hb.join(timeout=1)
        req.close(0)

    iopub_addr = f"tcp://127.0.0.1:{_free_port()}"
    iopub = IOPubThread(ctx, iopub_addr, session, qmax=10)
    iopub.start()
    sub = ctx.socket(zmq.SUB)
    sub.linger = 0
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(iopub_addr)
    time.sleep(0.05)
    try:
        iopub.send("status", {"execution_state": "idle"}, parent=None)
        _idents, msg = _poll_recv(sub, session)
        assert msg["msg_type"] == "status"
        assert msg["content"]["execution_state"] == "idle"
    finally:
        iopub.stop()
        iopub.join(timeout=1)
        sub.close(0)

    iopub = IOPubThread(ctx, f"tcp://127.0.0.1:{_free_port()}", session, qmax=1)
    iopub.send("stream", {"name": "stdout", "text": "one"}, parent=None)
    iopub.send("stream", {"name": "stdout", "text": "two"}, parent=None)  # dropped: q full at qmax=1
    iopub.send("status", {"execution_state": "idle"}, parent=None)        # status always queued
    assert iopub.q.qsize() == 2
    assert iopub.dropped == 1
    assert iopub.enqueued == 3

    router = None

    def handler(msg, idents): router.enqueue(("kernel_info_reply", {"status": "ok"}, msg, idents))

    router = AsyncRouterThread(context=ctx, session=session, bind_addr=f"tcp://127.0.0.1:{_free_port()}", handler=handler, log_label="shell")
    router.start()
    assert router.ready.wait(1)
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.connect(router.bind_addr)
    time.sleep(0.05)
    try:
        session.send(dealer, "kernel_info_request", {}, parent=None)
        _idents, msg = _poll_recv(dealer, session)
        assert msg["msg_type"] == "kernel_info_reply"
    finally:
        router.stop()
        router.join(timeout=1)
        dealer.close(0)

    got = {"msg": None, "idents": None}
    seen = threading.Event()

    def handler(msg, idents):
        got["msg"] = msg
        got["idents"] = idents
        seen.set()

    router = AsyncRouterThread(context=ctx, session=session, bind_addr=f"tcp://127.0.0.1:{_free_port()}", handler=handler, log_label="shell")
    router.start()
    assert router.ready.wait(1)
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.connect(router.bind_addr)
    time.sleep(0.05)
    try:
        session.send(dealer, "kernel_info_request", {}, parent=None)
        assert seen.wait(1)
        router.enqueue(("kernel_info_reply", {"status": "ok"}, got["msg"], got["idents"]))
        _idents, reply = _poll_recv(dealer, session)
        assert reply["msg_type"] == "kernel_info_reply"
    finally:
        router.stop()
        router.join(timeout=1)
        dealer.close(0)

    stdin = StdinRouterThread(ctx, f"tcp://127.0.0.1:{_free_port()}", session)
    stdin.start()
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client1")
    dealer.connect(stdin.addr)
    time.sleep(0.05)
    result = {}
    exc = {}

    def wait_input():
        try: result["value"] = stdin.request_input("Name: ", False, parent={}, ident=[b"client1"], timeout=2)
        except Exception as err: exc["err"] = err

    thread = threading.Thread(target=wait_input)
    thread.start()
    try:
        _idents, msg = session.recv(dealer, mode=0)
        assert msg["msg_type"] == "input_request"
        session.send(dealer, "input_reply", {"value": "Ada"}, parent=msg)
        thread.join(timeout=2)
        assert exc == {}
        assert result["value"] == "Ada"
    finally:
        stdin.stop()
        stdin.join(timeout=1)
        dealer.close(0)

    stdin = StdinRouterThread(ctx, f"tcp://127.0.0.1:{_free_port()}", session)
    stdin.start()
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client2")
    dealer.connect(stdin.addr)
    time.sleep(0.05)
    exc = {}

    def wait_input():
        try: stdin.request_input("Name: ", False, parent={}, ident=[b"client2"], timeout=2)
        except BaseException as err: exc["err"] = err

    thread = threading.Thread(target=wait_input)
    thread.start()
    try:
        _idents, msg = session.recv(dealer, mode=0)
        assert msg["msg_type"] == "input_request"
        stdin.interrupt_pending()
        thread.join(timeout=2)
        assert isinstance(exc.get("err"), KeyboardInterrupt)
    finally:
        stdin.stop()
        stdin.join(timeout=1)
        dealer.close(0)
