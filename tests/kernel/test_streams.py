from ..kernel_utils import *


def test_execute_streams_smoke():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('hello, world')", store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = kc.iopub_drain(msg_id)
        stdout = iopub_streams(output_msgs, "stdout")
        assert stdout, "expected stdout stream message"
        assert "hello, world" in stdout[-1]["content"]["text"]

        msg_id = kc.execute("import sys; print('test', file=sys.stderr)", store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = kc.iopub_drain(msg_id)
        stderr = iopub_streams(output_msgs, "stderr")
        assert stderr, "expected stderr stream message"
