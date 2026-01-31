import asyncio
import socket
import threading
import time

import zmq
from jupyter_client.session import Session

from ipymini.zmqthread import AsyncRouterThread, HeartbeatThread, IOPubThread, StdinRouterThread, ThreadBoundAsyncQueue


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_thread_bound_async_queue_buffers_before_bind():
    q = ThreadBoundAsyncQueue()
    q.put("a")

    async def _runner():
        q.bind(asyncio.get_running_loop())
        item = await asyncio.wait_for(q.get(), timeout=1)
        assert item == "a"

    asyncio.run(_runner())

    q.suppress_late_puts()
    q.bound_once = True
    q.loop = None
    q.q = None
    q.put("ignored")


def test_heartbeat_thread_echo():
    ctx = zmq.Context.instance()
    port = _free_port()
    addr = f"tcp://127.0.0.1:{port}"

    hb = HeartbeatThread(ctx, addr)
    hb.start()
    try:
        req = ctx.socket(zmq.REQ)
        req.linger = 0
        req.connect(addr)
        req.send(b"ping")
        poller = zmq.Poller()
        poller.register(req, zmq.POLLIN)
        events = dict(poller.poll(1000))
        assert req in events
        assert req.recv() == b"ping"
    finally:
        hb.stop()
        hb.join(timeout=1)
        req.close(0)


def test_iopub_thread_sends_message():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    port = _free_port()
    addr = f"tcp://127.0.0.1:{port}"

    iopub = IOPubThread(ctx, addr, session, qmax=10)
    iopub.start()
    sub = ctx.socket(zmq.SUB)
    sub.linger = 0
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(addr)
    time.sleep(0.05)
    try:
        iopub.send("status", {"execution_state": "idle"}, parent=None)
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        events = dict(poller.poll(1000))
        assert sub in events
        _idents, msg = session.recv(sub, mode=0)
        assert msg["msg_type"] == "status"
        assert msg["content"]["execution_state"] == "idle"
    finally:
        iopub.stop()
        iopub.join(timeout=1)
        sub.close(0)


def test_async_router_thread_roundtrip():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    port = _free_port()
    addr = f"tcp://127.0.0.1:{port}"

    router = None

    def handler(msg, idents): router.enqueue(("kernel_info_reply", {"status": "ok"}, msg, idents))

    router = AsyncRouterThread(context=ctx, session=session, bind_addr=addr, handler=handler, log_label="shell")
    router.start()
    router.ready.wait()

    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.connect(addr)
    time.sleep(0.05)
    try:
        session.send(dealer, "kernel_info_request", {}, parent=None)
        poller = zmq.Poller()
        poller.register(dealer, zmq.POLLIN)
        events = dict(poller.poll(1000))
        assert dealer in events
        _idents, msg = session.recv(dealer, mode=0)
        assert msg["msg_type"] == "kernel_info_reply"
    finally:
        router.stop()
        router.join(timeout=1)
        dealer.close(0)


def test_stdin_router_thread_request_input():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    port = _free_port()
    addr = f"tcp://127.0.0.1:{port}"

    stdin = StdinRouterThread(ctx, addr, session)
    stdin.start()
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client1")
    dealer.connect(addr)
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


def test_stdin_router_thread_interrupt_pending():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    port = _free_port()
    addr = f"tcp://127.0.0.1:{port}"

    stdin = StdinRouterThread(ctx, addr, session)
    stdin.start()
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client2")
    dealer.connect(addr)
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
