from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, start_kernel


def test_execute_stream() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print('hello')", store_history=False)
        outputs = drain_iopub(kc, msg_id)
        stream = iopub_msgs(outputs, "stream")
        assert stream, "expected stream output"
        assert stream[-1]["content"]["text"].strip() == "hello"
