from uuid import uuid4
from conkernelclient import default_timeout
from ..aclient import *

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


async def test_message_headers():
    async with mini_kernel() as (_, kc):
        # kc.cmd.kernel_info() doesn't expose its generated msg_id before the reply arrives (unlike
        # execute()), so we can only confirm the reply carries a parent id, not compare it to an
        # independently-known request id here.
        reply = await kc.cmd.kernel_info(timeout=default_timeout)
        _assert_header(reply, "kernel_info_reply")
        assert parent_id(reply)

        mid = str(uuid4())
        reply = await kc.execute("1+1", store_history=False, reply=True, timeout=default_timeout, msg_id=mid)
        _assert_header(reply, "execute_reply")
        assert reply["parent_header"]["msg_id"] == mid

        output_msgs = await kc.iopub_drain(mid)
        assert output_msgs
        for msg in output_msgs:
            _assert_header(msg)
            assert msg["parent_header"]["msg_id"] == mid
