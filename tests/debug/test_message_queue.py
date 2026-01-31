import json

from ipymini.debug.dap import DebugpyMessageQueue


def _frame(msg: dict)->str:
    payload = json.dumps(msg)
    size = len(payload.encode("utf-8"))
    return f"Content-Length: {size}\r\n\r\n{payload}"


def test_debugpy_message_queue_split_and_combined():
    events = []
    responses = []
    def on_event(msg: dict): events.append(msg)
    def on_response(msg: dict): responses.append(msg)
    q = DebugpyMessageQueue(on_event, on_response)

    msg_event = {"type": "event", "event": "initialized"}
    frame = _frame(msg_event)
    q.put_tcp_frame(frame[:12])
    assert events == []
    q.put_tcp_frame(frame[12:])
    assert events == [msg_event]

    msg_resp = {"type": "response", "request_seq": 7}
    msg_event2 = {"type": "event", "event": "stopped"}
    q.put_tcp_frame(_frame(msg_resp) + _frame(msg_event2))
    assert responses == [msg_resp]
    assert events[-1] == msg_event2
