import time

from .kernel_utils import drain_iopub, get_shell_reply, iopub_streams, start_kernel

TIMEOUT = 10


def test_input_request() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print(input('prompt> '))", allow_stdin=True)
        stdin_msg = kc.get_stdin_msg(timeout=TIMEOUT)
        assert stdin_msg["msg_type"] == "input_request"
        assert stdin_msg["content"]["prompt"] == "prompt> "
        assert not stdin_msg["content"]["password"]

        text = "some text"
        kc.input(text)

        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"

        output_msgs = drain_iopub(kc, msg_id)
        streams = iopub_streams(output_msgs).map(lambda m: (m["content"]["name"], m["content"]["text"]))
        assert ("stdout", text + "\n") in streams


def test_stream_flushed_before_input_request() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('before'); input('prompt> ')", allow_stdin=True)
        stdin_msg = kc.get_stdin_msg(timeout=TIMEOUT)
        assert stdin_msg["msg_type"] == "input_request"
        assert stdin_msg["content"]["prompt"] == "prompt> "

        # Stream output should be available before we reply to stdin.
        stream_msg = None
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            msg = kc.get_iopub_msg(timeout=TIMEOUT)
            if msg["msg_type"] == "stream":
                stream_msg = msg
                break
        assert stream_msg is not None, "expected stream before input reply"
        assert stream_msg["content"]["text"] == "before\n"

        kc.input("ok")

        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        drain_iopub(kc, msg_id)


def test_input_request_disallowed() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("input('prompt> ')", allow_stdin=False)

        try:
            _ = kc.get_stdin_msg(timeout=1)
            assert False, "expected no stdin message"
        except Exception:
            pass

        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "StdinNotImplementedError"
        drain_iopub(kc, msg_id)
