import time
from .kernel_utils import drain_iopub, get_shell_reply, iopub_streams, start_kernel

TIMEOUT = 3


def test_input_request_and_stream_ordering() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('before'); print(input('prompt> '))", allow_stdin=True)
        stdin_msg = kc.get_stdin_msg(timeout=TIMEOUT)
        assert stdin_msg["msg_type"] == "input_request"
        assert stdin_msg["content"]["prompt"] == "prompt> "
        assert not stdin_msg["content"]["password"]

        stream_msg = None
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            msg = kc.get_iopub_msg(timeout=TIMEOUT)
            if msg["msg_type"] == "stream":
                stream_msg = msg
                break
        assert stream_msg is not None, "expected stream before input reply"
        assert stream_msg["content"]["text"] == "before\n"

        text = "some text"
        kc.input(text)

        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"

        output_msgs = drain_iopub(kc, msg_id)
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(output_msgs)]
        assert ("stdout", text + "\n") in streams


def test_input_request_disallowed() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("input('prompt> ')", allow_stdin=False)

        try:
            _ = kc.get_stdin_msg(timeout=1)
            assert False, "expected no stdin message"
        except Exception: pass

        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "StdinNotImplementedError"
        drain_iopub(kc, msg_id)


def test_interrupt_while_waiting_for_input() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("input('prompt> ')", allow_stdin=True)
        stdin_msg = kc.get_stdin_msg(timeout=TIMEOUT)
        assert stdin_msg["msg_type"] == "input_request"

        interrupt_msg = kc.session.msg("interrupt_request", {})
        kc.control_channel.send(interrupt_msg)
        deadline = time.time() + TIMEOUT
        interrupt_reply = None
        while time.time() < deadline:
            reply = kc.control_channel.get_msg(timeout=TIMEOUT)
            if reply["parent_header"].get("msg_id") == interrupt_msg["header"]["msg_id"]:
                interrupt_reply = reply
                break
        assert interrupt_reply is not None
        assert interrupt_reply["header"]["msg_type"] == "interrupt_reply"

        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "KeyboardInterrupt"
        drain_iopub(kc, msg_id)

        ok_id = kc.execute("1+1", store_history=False)
        ok_reply = get_shell_reply(kc, ok_id)
        assert ok_reply["content"]["status"] == "ok"
        drain_iopub(kc, ok_id)


def test_duplicate_input_reply_does_not_break_stdin() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("user_input = input('Enter something: ')", allow_stdin=True, store_history=False)
        stdin_msg = kc.get_stdin_msg(timeout=TIMEOUT)
        reply = kc.session.msg("input_reply", {"value": "bbb"}, parent=stdin_msg)
        kc.stdin_channel.send(reply)
        kc.stdin_channel.send(reply)
        reply_msg = get_shell_reply(kc, msg_id)
        assert reply_msg["content"]["status"] == "ok"
        drain_iopub(kc, msg_id)

        msg_id2 = kc.execute("user_input = input('Again: ')", allow_stdin=True, store_history=False)
        _stdin_msg2 = kc.get_stdin_msg(timeout=TIMEOUT)
        kc.input("ccc")
        reply_msg2 = get_shell_reply(kc, msg_id2)
        assert reply_msg2["content"]["status"] == "ok"
        drain_iopub(kc, msg_id2)
