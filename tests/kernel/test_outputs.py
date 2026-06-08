from ..kernel_utils import *


def test_output_display_features():
    with start_kernel() as (_, kc):
        _, _, output_msgs = kc.exec_ok("print('hello')", store_history=False)
        stream = iopub_msgs(output_msgs, "stream")
        assert stream, "expected stream output"
        assert stream[-1]["content"]["text"].strip() == "hello"

        _, reply, output_msgs = kc.exec_drain("import sys; print('out1'); print('err1', file=sys.stderr); print('out2')", store_history=False)
        assert reply["content"]["status"] == "ok"
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(output_msgs)]
        assert streams == [("stdout", "out1\n"), ("stderr", "err1\n"), ("stdout", "out2\n")]

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

        _, reply, output_msgs = kc.exec_drain("from IPython.display import clear_output; clear_output(wait=True)", store_history=False)
        assert reply["content"]["status"] == "ok"
        waits = [m["content"]["wait"] for m in iopub_msgs(output_msgs, "clear_output")]
        assert True in waits

        code = "from IPython.display import display\nh = display('first', display_id=True)\nh.update('second')\n"
        _, reply, output_msgs = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = [m for m in iopub_msgs(output_msgs) if m["msg_type"] in ("display_data", "update_display_data")]
        assert len(displays) >= 2
        first, second = displays[0], displays[1]
        assert first["msg_type"] == "display_data"
        assert second["msg_type"] == "update_display_data"
        display_id = first["content"].get("transient", {}).get("display_id")
        assert display_id
        update_id = second["content"].get("transient", {}).get("display_id")
        assert update_id == display_id

        code = (
            "from IPython.display import display\n"
            "display({'text/plain': 'hi'}, raw=True, metadata={'foo': 'bar'}, transient={'display_id': 'xyz'})\n")
        _, reply, output_msgs = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected at least one display_data message"
        content = displays[0]["content"]
        assert content.get("metadata", {}).get("foo") == "bar"
        assert content.get("transient", {}).get("display_id") == "xyz"

        code = (
            "from IPython import get_ipython\n"
            "get_ipython().display_pub.publish({'text/plain': 'buf'}, buffers=[b'bufdata'])\n")
        _, reply, output_msgs = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected display_data message"
        buffers = displays[0].get("buffers") or []
        assert buffers and bytes(buffers[0]) == b"bufdata"
