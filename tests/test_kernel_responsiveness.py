import time
from .kernel_utils import *


def test_control_reply_not_blocked_by_long_execute():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("import time; time.sleep(1.5); 'done'", store_history=False)
        time.sleep(0.05)

        t0 = time.monotonic()
        ctl = kc.ctl.list_subshell()
        elapsed = time.monotonic() - t0

        assert ctl["content"]["status"] == "ok"
        assert elapsed < 0.5, f"control reply too slow: {elapsed:.2f}s"

        kc.shell_reply(msg_id, timeout=5)
        kc.iopub_drain(msg_id)
