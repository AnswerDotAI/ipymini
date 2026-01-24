from .kernel_utils import drain_iopub, get_shell_reply, iopub_streams, start_kernel


def test_execute_streams_smoke() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('hello, world')", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        stdout = iopub_streams(output_msgs, "stdout")
        assert stdout, "expected stdout stream message"
        assert "hello, world" in stdout[-1]["content"]["text"]

        msg_id = kc.execute("import sys; print('test', file=sys.stderr)", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        stderr = iopub_streams(output_msgs, "stderr")
        assert stderr, "expected stderr stream message"
