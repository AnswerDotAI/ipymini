import time
from ..kernel_utils import *

# Mimics solveit's `load_dialog` re-entrancy: a cell awaits an HTTP call to the solveit server,
# which sends further execute_requests back to the same kernel (`import_code`). The first cell
# only completes once the later requests have run, so the kernel must keep processing shell
# messages while an async cell is awaiting (ipyku_launcher/UnlockKernel semantics).

_waiter = """import asyncio
async def _wait_flag():
    for _ in range(160):
        if globals().get('_flag'): return True
        await asyncio.sleep(0.05)
    return False
assert await _wait_flag(), 'second execute never ran while first cell awaited'
"""


def test_execute_processed_while_async_cell_awaits():
    with start_kernel() as (_, kc):
        start = time.monotonic()
        mid1 = kc.execute(_waiter)
        mid2 = kc.execute("_flag = True")
        replies = collect_shell_replies(kc, {mid1, mid2}, timeout=12)
        assert replies[mid2]["content"]["status"] == "ok", replies[mid2]["content"]
        assert replies[mid1]["content"]["status"] == "ok", replies[mid1]["content"]
        assert time.monotonic() - start < 5, "execute_requests were serialized behind the awaiting cell"


def test_sync_cells_still_run_in_order():
    with start_kernel() as (_, kc):
        mids = [kc.execute(code) for code in ("order = []", "order.append(1)", "order.append(2)", "order.append(3)")]
        replies = collect_shell_replies(kc, set(mids), timeout=10)
        for mid in mids: assert replies[mid]["content"]["status"] == "ok", replies[mid]["content"]
        _, reply, outputs = kc.exec_drain("print(order)")
        assert reply["content"]["status"] == "ok"
        texts = "".join(m["content"]["text"] for m in iopub_streams(outputs))
        assert "[1, 2, 3]" in texts, texts
