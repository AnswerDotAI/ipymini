import time
from .kernel_utils import (DEBUG_INIT_ARGS, collect_iopub_outputs, collect_shell_replies, debug_request, drain_iopub,
    get_shell_reply, start_kernel, wait_for_status)

TIMEOUT = 10


def test_asyncio_execute_roundtrip() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("1+1", store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        drain_iopub(kc, msg_id)


def test_asyncio_debug_request() -> None:
    with start_kernel() as (_, kc):
        reply = debug_request(kc, "initialize", DEBUG_INIT_ARGS)
        assert reply.get("success")


def test_asyncio_shutdown_request() -> None:
    with start_kernel() as (_, kc):
        msg = kc.session.msg("shutdown_request", {"restart": False})
        kc.shell_channel.send(msg)
        reply = get_shell_reply(kc, msg["header"]["msg_id"])
        assert reply["header"]["msg_type"] == "shutdown_reply"
        assert reply["content"]["status"] == "ok"


def test_asyncio_burst_execute() -> None:
    with start_kernel() as (_, kc):
        msg_ids = [kc.execute(f"{i}+1", store_history=False) for i in range(5)]
        replies = collect_shell_replies(kc, set(msg_ids))
        for reply in replies.values(): assert reply["content"]["status"] == "ok"
        _outputs = collect_iopub_outputs(kc, set(msg_ids))


def test_asyncio_control_interrupt_during_execute() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("import time; time.sleep(1)", store_history=False)
        wait_for_status(kc, "busy")

        interrupt_msg = kc.session.msg("interrupt_request", {})
        kc.control_channel.send(interrupt_msg)
        deadline = time.time() + TIMEOUT
        interrupt_reply = None
        while time.time() < deadline:
            reply = kc.control_channel.get_msg(timeout=TIMEOUT)
            if reply["parent_header"].get("msg_id") == interrupt_msg["header"]["msg_id"]:
                interrupt_reply = reply
                break
        assert interrupt_reply is not None
        assert interrupt_reply["header"]["msg_type"] == "interrupt_reply"

        reply = get_shell_reply(kc, msg_id, timeout=20)
        assert reply["content"]["status"] == "error"
        wait_for_status(kc, "idle")
