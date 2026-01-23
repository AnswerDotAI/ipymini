import time

from .kernel_utils import start_kernel

TIMEOUT = 10


def _send_debug_request(kc, command, arguments=None):
    msg = kc.session.msg(
        "debug_request",
        {
            "type": "request",
            "seq": int(time.time() * 1000) % 100000,
            "command": command,
            "arguments": arguments or {},
        },
    )
    kc.control_channel.send(msg)
    reply = kc.control_channel.get_msg(timeout=TIMEOUT)
    assert reply["header"]["msg_type"] == "debug_reply"
    assert reply["parent_header"].get("msg_id") == msg["header"]["msg_id"]
    return reply["content"]


def _wait_for_debug_event(kc, event_name):
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        msg = kc.iopub_channel.get_msg(timeout=TIMEOUT)
        if msg.get("msg_type") == "debug_event":
            if msg.get("content", {}).get("event") == event_name:
                return msg
    raise AssertionError(f"debug_event {event_name} not received")


def test_debug_initialize() -> None:
    with start_kernel() as (_, kc):
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        reply = _send_debug_request(
            kc,
            "initialize",
            {
                "clientID": "test-client",
                "clientName": "testClient",
                "adapterID": "",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        if debugpy:
            assert reply.get("success")
        else:
            assert reply == {}


def test_debug_attach() -> None:
    with start_kernel() as (_, kc):
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        _send_debug_request(
            kc,
            "initialize",
            {
                "clientID": "test-client",
                "clientName": "testClient",
                "adapterID": "",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        reply = _send_debug_request(kc, "attach")
        if debugpy:
            assert reply.get("success")
            _wait_for_debug_event(kc, "initialized")
        else:
            assert reply == {}


def test_debug_evaluate() -> None:
    with start_kernel() as (_, kc):
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        _send_debug_request(
            kc,
            "initialize",
            {
                "clientID": "test-client",
                "clientName": "testClient",
                "adapterID": "",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        _send_debug_request(kc, "attach")
        reply = _send_debug_request(kc, "evaluate", {"expression": "'a' + 'b'", "context": "repl"})
        if debugpy:
            assert reply.get("success")
        else:
            assert reply == {}
