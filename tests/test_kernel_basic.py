from .kernel_utils import *


def test_execute_stream():
    with start_kernel() as (_, kc):
        _, _, outputs = kc.exec_ok("print('hello')", store_history=False)
        stream = iopub_msgs(outputs, "stream")
        assert stream, "expected stream output"
        assert stream[-1]["content"]["text"].strip() == "hello"
