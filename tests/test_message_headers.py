from .kernel_utils import *


def _version_tuple(value:str)->tuple[int, ...]: return tuple(int(part) if part.isdigit() else 0 for part in value.split("."))


def _assert_header(msg: dict, msg_type:str|None=None):
    header = msg.get("header", {})
    assert header.get("msg_id")
    assert header.get("msg_type")
    assert header.get("session")
    assert header.get("username")
    assert header.get("version")
    if msg_type: assert header["msg_type"] == msg_type
    assert _version_tuple(header["version"]) >= (5, 0)


def test_kernel_info_header():
    with start_kernel() as (_, kc):
        msg_id = kc.kernel_info()
        reply = get_shell_reply(kc, msg_id)
        _assert_header(reply, "kernel_info_reply")
        assert reply["parent_header"]["msg_id"] == msg_id


def test_execute_headers():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("1+1", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        _assert_header(reply, "execute_reply")
        assert reply["parent_header"]["msg_id"] == msg_id

        output_msgs = kc.iopub_drain(msg_id)
        assert output_msgs
        for msg in output_msgs:
            _assert_header(msg)
            assert msg["parent_header"]["msg_id"] == msg_id
