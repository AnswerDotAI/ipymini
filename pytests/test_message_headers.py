from .kernel_utils import drain_iopub, get_shell_reply, start_kernel


def _version_tuple(value: str) -> tuple[int, ...]: return tuple(int(part) if part.isdigit() else 0 for part in value.split("."))


def _assert_header(msg: dict, msg_type: str | None = None) -> None:
    header = msg.get("header", {})
    assert header.get("msg_id")
    assert header.get("msg_type")
    assert header.get("session")
    assert header.get("username")
    assert header.get("version")
    if msg_type: assert header["msg_type"] == msg_type
    assert _version_tuple(header["version"]) >= (5, 0)


def test_kernel_info_header() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.kernel_info()
        reply = get_shell_reply(kc, msg_id)
        _assert_header(reply, "kernel_info_reply")
        assert reply["parent_header"]["msg_id"] == msg_id


def test_execute_headers() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("1+1", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        _assert_header(reply, "execute_reply")
        assert reply["parent_header"]["msg_id"] == msg_id

        output_msgs = drain_iopub(kc, msg_id)
        assert output_msgs
        for msg in output_msgs:
            _assert_header(msg)
            assert msg["parent_header"]["msg_id"] == msg_id
