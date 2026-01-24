import time
from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, start_kernel, wait_for_status


def _send_interrupt_request(kc) -> None:
    interrupt_msg = kc.session.msg("interrupt_request", {})
    kc.control_channel.send(interrupt_msg)
    deadline = time.time() + 2
    interrupt_reply = None
    while time.time() < deadline:
        reply = kc.control_channel.get_msg(timeout=2)
        if reply["parent_header"].get("msg_id") == interrupt_msg["header"]["msg_id"]:
            interrupt_reply = reply
            break
    assert interrupt_reply is not None, "missing interrupt_reply"
    assert interrupt_reply["header"]["msg_type"] == "interrupt_reply"


def test_interrupt_request() -> None:
    with start_kernel() as (km, kc):
        for use_control_channel in [False, True]:
            msg_id = kc.execute("import time; time.sleep(1)")
            wait_for_status(kc, "busy")

            if use_control_channel: _send_interrupt_request(kc)
            else: km.interrupt_kernel()

            reply = get_shell_reply(kc, msg_id, timeout=10)
            assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
            assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}, (
                f"interrupt ename: {reply.get('content')}"
            )

            outputs = drain_iopub(kc, msg_id)
            errors = iopub_msgs(outputs, "error")
            assert errors, f"expected iopub error after interrupt, got: {[m.get('msg_type') for m in outputs]}"
            assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", (
                f"interrupt iopub: {errors[-1].get('content')}"
            )
