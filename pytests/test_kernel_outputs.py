from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, iopub_streams, start_kernel


def test_stream_ordering() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute(
            "import sys; print('out1'); print('err1', file=sys.stderr); print('out2')",
            store_history=False,
        )
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        streams = iopub_streams(output_msgs).map(lambda m: (m["content"]["name"], m["content"]["text"]))
        assert streams == [("stdout", "out1\n"), ("stderr", "err1\n"), ("stdout", "out2\n")]


def test_clear_output_wait() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute(
            "from IPython.display import clear_output; clear_output(wait=True)",
            store_history=False,
        )
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        waits = iopub_msgs(output_msgs, "clear_output").map(lambda m: m["content"]["wait"])
        assert True in waits


def test_display_id_update() -> None:
    with start_kernel() as (_, kc):
        code = "from IPython.display import display\nh = display('first', display_id=True)\nh.update('second')\n"
        msg_id = kc.execute(code, store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        displays = iopub_msgs(output_msgs).filter(
            lambda m: m["msg_type"] in ("display_data", "update_display_data")
        )
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
        msg_id = kc.execute(code, store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected at least one display_data message"
        content = displays[0]["content"]
        assert content.get("metadata", {}).get("foo") == "bar"
        assert content.get("transient", {}).get("display_id") == "xyz"
