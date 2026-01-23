from .kernel_utils import execute_and_drain, iopub_msgs, iopub_streams, start_kernel


def test_stream_ordering() -> None:
    with start_kernel() as (_, kc):
        _, reply, output_msgs = execute_and_drain(kc,
            "import sys; print('out1'); print('err1', file=sys.stderr); print('out2')", store_history=False)
        assert reply["content"]["status"] == "ok"
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(output_msgs)]
        assert streams == [("stdout", "out1\n"), ("stderr", "err1\n"), ("stdout", "out2\n")]


def test_clear_output_wait() -> None:
    with start_kernel() as (_, kc):
        _, reply, output_msgs = execute_and_drain(kc, "from IPython.display import clear_output; clear_output(wait=True)", store_history=False)
        assert reply["content"]["status"] == "ok"
        waits = [m["content"]["wait"] for m in iopub_msgs(output_msgs, "clear_output")]
        assert True in waits


def test_display_id_update() -> None:
    with start_kernel() as (_, kc):
        code = "from IPython.display import display\nh = display('first', display_id=True)\nh.update('second')\n"
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False)
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


def test_display_metadata_transient() -> None:
    with start_kernel() as (_, kc):
        code = (
            "from IPython.display import display\n"
            "display({'text/plain': 'hi'}, raw=True, metadata={'foo': 'bar'}, transient={'display_id': 'xyz'})\n"
        )
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected at least one display_data message"
        content = displays[0]["content"]
        assert content.get("metadata", {}).get("foo") == "bar"
        assert content.get("transient", {}).get("display_id") == "xyz"


def test_display_buffers() -> None:
    with start_kernel() as (_, kc):
        code = (
            "from IPython import get_ipython\n"
            "get_ipython().display_pub.publish({'text/plain': 'buf'}, buffers=[b'bufdata'])\n"
        )
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected display_data message"
        buffers = displays[0].get("buffers") or []
        assert buffers and bytes(buffers[0]) == b"bufdata"
