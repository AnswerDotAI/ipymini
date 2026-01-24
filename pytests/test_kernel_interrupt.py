from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, start_kernel, wait_for_status


def test_interrupt_request() -> None:
    with start_kernel() as (km, kc):
        msg_id = kc.execute("import time; time.sleep(0.5)")
        wait_for_status(kc, "busy")

        km.interrupt_kernel()

        reply = get_shell_reply(kc, msg_id, timeout=20)
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}, (
            f"interrupt ename: {reply.get('content')}"
        )

        outputs = drain_iopub(kc, msg_id)
        errors = iopub_msgs(outputs, "error")
        assert errors, f"expected iopub error after interrupt, got: {[m.get('msg_type') for m in outputs]}"
        assert errors[-1]["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}, (
            f"interrupt iopub: {errors[-1].get('content')}"
        )
