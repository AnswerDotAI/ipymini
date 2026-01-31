import socket, threading, time

import pytest
import zmq
from jupyter_client.session import Session

from ipymini.zmqthread import StdinRouterThread


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_stdin_router_supports_multiple_waiters_same_ident():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    addr = f"tcp://127.0.0.1:{_free_port()}"

    stdin = StdinRouterThread(ctx, addr, session)
    stdin.start()

    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client-multi")
    dealer.connect(addr)
    time.sleep(0.05)

    out = {}
    exc = {}

    def wait_one(prompt: str):
        try: out[prompt] = stdin.request_input(prompt, False, parent={}, ident=[b"client-multi"], timeout=2)
        except BaseException as err: exc[prompt] = err

    t1 = threading.Thread(target=wait_one, args=("First: ",))
    t2 = threading.Thread(target=wait_one, args=("Second: ",))
    t1.start()
    t2.start()

    try:
        # Receive two input_request messages (order not guaranteed).
        reqs = []
        for _ in range(2):
            _idents, msg = session.recv(dealer, mode=0)
            assert msg["msg_type"] == "input_request"
            reqs.append(msg)

        for msg in reqs:
            prompt = msg.get("content", {}).get("prompt", "")
            value = "one" if "First" in prompt else "two"
            session.send(dealer, "input_reply", {"value": value}, parent=msg)

        t1.join(timeout=2)
        t2.join(timeout=2)
        assert exc == {}
        assert out.get("First: ") == "one"
        assert out.get("Second: ") == "two"
    finally:
        stdin.stop()
        stdin.join(timeout=1)
        dealer.close(0)


def test_stdin_router_timeout_on_unknown_reply():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    addr = f"tcp://127.0.0.1:{_free_port()}"

    stdin = StdinRouterThread(ctx, addr, session)
    stdin.start()

    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client-timeout")
    dealer.connect(addr)

    other = ctx.socket(zmq.DEALER)
    other.linger = 0
    other.setsockopt(zmq.IDENTITY, b"other-client")
    other.connect(addr)

    time.sleep(0.05)

    exc = {}
    def wait_input():
        try: stdin.request_input("Name: ", False, parent={}, ident=[b"client-timeout"], timeout=0.2)
        except BaseException as err: exc["err"] = err

    t = threading.Thread(target=wait_input)
    t.start()
    try:
        _idents, msg = session.recv(dealer, mode=0)
        assert msg["msg_type"] == "input_request"
        # Send an input_reply from a *different* identity with a mismatched parent msg_id.
        # This should not match either the msg_id mapping or the ident fallback.
        session.send(other, "input_reply", {"value": "X"}, parent={"header": {"msg_id": "unknown"}})
        t.join(timeout=1)
        assert isinstance(exc.get("err"), TimeoutError)
    finally:
        stdin.stop()
        stdin.join(timeout=1)
        dealer.close(0)
        other.close(0)


def test_stdin_router_duplicate_reply_is_ignored():
    ctx = zmq.Context.instance()
    session = Session(key=b"")
    addr = f"tcp://127.0.0.1:{_free_port()}"

    stdin = StdinRouterThread(ctx, addr, session)
    stdin.start()

    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.setsockopt(zmq.IDENTITY, b"client-dupe")
    dealer.connect(addr)
    time.sleep(0.05)

    result = {}
    def wait_input(): result["value"] = stdin.request_input("Name: ", False, parent={}, ident=[b"client-dupe"], timeout=2)

    t = threading.Thread(target=wait_input)
    t.start()
    try:
        _idents, msg = session.recv(dealer, mode=0)
        assert msg["msg_type"] == "input_request"
        session.send(dealer, "input_reply", {"value": "Ada"}, parent=msg)
        # Duplicate reply should not crash anything.
        session.send(dealer, "input_reply", {"value": "Ada"}, parent=msg)
        t.join(timeout=2)
        assert result.get("value") == "Ada"
    finally:
        stdin.stop()
        stdin.join(timeout=1)
        dealer.close(0)
