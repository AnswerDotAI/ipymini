from queue import Empty
from ..kernel_utils import *

timeout = 3


def test_input_request_and_stream_ordering():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('before'); print(input('prompt> '))", allow_stdin=True)
        stdin_msg = kc.get_stdin_msg(timeout=timeout)
        assert stdin_msg["msg_type"] == "input_request"
        assert stdin_msg["content"]["prompt"] == "prompt> "
        assert not stdin_msg["content"]["password"]

        pred = lambda m: parent_id(m) == msg_id and m.get("msg_type") == "stream"
        stream_msg = wait_for_msg(kc.get_iopub_msg, pred, timeout=timeout, err="expected stream before input reply")
        assert stream_msg["content"]["text"] == "before\n"

        text = "some text"
        kc.input(text)

        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"

        output_msgs = kc.iopub_drain(msg_id)
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(output_msgs)]
        assert ("stdout", text + "\n") in streams


def test_input_request_disallowed():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("input('prompt> ')", allow_stdin=False)

        try:
            _ = kc.get_stdin_msg(timeout=1)
            assert False, "expected no stdin message"
        except Empty: pass

        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "StdinNotImplementedError"
        kc.iopub_drain(msg_id)


def test_interrupt_while_waiting_for_input():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("input('prompt> ')", allow_stdin=True)
        stdin_msg = kc.get_stdin_msg(timeout=timeout)
        assert stdin_msg["msg_type"] == "input_request"

        kc.interrupt_request(timeout=timeout)

        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "KeyboardInterrupt"
        kc.iopub_drain(msg_id)

        ok_id = kc.execute("1+1", store_history=False)
        ok_reply = kc.shell_reply(ok_id)
        assert ok_reply["content"]["status"] == "ok"
        kc.iopub_drain(ok_id)


def test_duplicate_input_reply_does_not_break_stdin():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("user_input = input('Enter something: ')", allow_stdin=True, store_history=False)
        stdin_msg = kc.get_stdin_msg(timeout=timeout)
        reply = kc.session.msg("input_reply", {"value": "bbb"}, parent=stdin_msg)
        kc.stdin_channel.send(reply)
        kc.stdin_channel.send(reply)
        reply_msg = kc.shell_reply(msg_id)
        assert reply_msg["content"]["status"] == "ok"
        kc.iopub_drain(msg_id)

        msg_id2 = kc.execute("user_input = input('Again: ')", allow_stdin=True, store_history=False)
        _stdin_msg2 = kc.get_stdin_msg(timeout=timeout)
        kc.input("ccc")
        reply_msg2 = kc.shell_reply(msg_id2)
        assert reply_msg2["content"]["status"] == "ok"
        kc.iopub_drain(msg_id2)
