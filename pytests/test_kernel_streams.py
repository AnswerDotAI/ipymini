from .kernel_utils import drain_iopub, get_shell_reply, start_kernel


def test_execute_stdout() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('hello, world')", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        for msg in output_msgs:
            if msg["msg_type"] == "stream" and msg["content"]["name"] == "stdout":
                assert "hello, world" in msg["content"]["text"]
                break
        else:
            assert False, "expected stdout stream message"


def test_execute_stderr() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("import sys; print('test', file=sys.stderr)", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        for msg in output_msgs:
            if msg["msg_type"] == "stream" and msg["content"]["name"] == "stderr":
                break
        else:
            assert False, "expected stderr stream message"
