import queue

import zmq

from ipymini.debug.dap import MiniDebugpyClient


def test_minidebugpy_client_wait_for_response():
    ctx = zmq.Context.instance()
    client = MiniDebugpyClient(ctx, event_callback=None)
    seq, waiter = client.send_request_async({"command": "noop"})
    waiter.put(dict(type="response", request_seq=seq, success=True))
    reply = client.wait_for_response(seq, waiter, timeout=0.1)
    assert reply.get("request_seq") == seq
    assert seq not in client.pending


def test_minidebugpy_client_timeout():
    ctx = zmq.Context.instance()
    client = MiniDebugpyClient(ctx, event_callback=None)
    seq, waiter = client.send_request_async({"command": "noop"})
    ok = False
    try:
        client.wait_for_response(seq, waiter, timeout=0.01)
        ok = False
        tag = "no error"
    except TimeoutError:
        ok = True
        tag = "timeout"
    assert ok
    assert tag == "timeout"
