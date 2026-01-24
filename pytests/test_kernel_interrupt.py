from .kernel_utils import get_shell_reply, start_kernel, wait_for_status


def test_interrupt_request() -> None:
    with start_kernel() as (km, kc):
        msg_id = kc.execute("import time; time.sleep(0.5)")
        wait_for_status(kc, "busy")

        km.interrupt_kernel()

        reply = get_shell_reply(kc, msg_id, timeout=20)
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}

        wait_for_status(kc, "idle")
