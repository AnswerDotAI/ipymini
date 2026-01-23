import time

from .kernel_utils import start_kernel


def test_interrupt_request() -> None:
    with start_kernel() as (km, kc):
        msg_id = kc.execute("import time; time.sleep(2)")

        while True:
            msg = kc.get_iopub_msg(timeout=2)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "busy":
                break

        km.interrupt_kernel()

        deadline = time.time() + 20
        reply = None
        while time.time() < deadline:
            try:
                candidate = kc.get_shell_msg(timeout=2)
            except Exception:
                continue
            if candidate["parent_header"].get("msg_id") == msg_id:
                reply = candidate
                break
        assert reply is not None, "no execute_reply after interrupt"
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}

        while True:
            msg = kc.get_iopub_msg(timeout=2)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                break
