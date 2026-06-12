import time, pytest
from ..kernel_utils import *


@pytest.fixture(scope="module")
def kc():
    "One shared kernel for this module; each test uses fresh names and drains its own requests."
    with start_kernel() as (_, kc): yield kc


# Mimics solveit's `load_dialog` re-entrancy: a cell calls unlock() then awaits a response that can
# only arrive once later execute_requests on the same channel have run (ipyku_launcher/UnlockKernel
# semantics, but opt-in per cell). The event dependency is the proof: cell 1 can only complete if
# cell 2 ran during its await.
_unlocker = """import asyncio
from ipymini import unlock
ev = asyncio.Event()
unlock()
await asyncio.wait_for(ev.wait(), 5)
"""


def test_execute_processed_while_unlocked_cell_awaits(kc):
    mid1 = kc.execute(_unlocker)
    mid2 = kc.execute("ev.set()")
    replies = collect_shell_replies(kc, {mid1, mid2}, timeout=10)
    for mid in (mid1, mid2): assert replies[mid]["content"]["status"] == "ok", replies[mid]["content"]


def test_dependent_async_cells_serialized_by_default(kc):
    "Without unlock(), pipelined dependent cells keep FIFO completion order (stock ipykernel parity)."
    mid1 = kc.execute("import asyncio; a = await asyncio.sleep(0.1, 1)")
    mid2 = kc.execute("assert a == 1")
    replies = collect_shell_replies(kc, {mid1, mid2}, timeout=10)
    for mid in (mid1, mid2): assert replies[mid]["content"]["status"] == "ok", replies[mid]["content"]


_unlocked_await = """import asyncio
from ipymini import unlock
unlock()
b = await asyncio.sleep(0.3, 1)
"""


def test_unlock_trades_away_dependency_ordering(kc):
    "Documented semantics: after unlock(), a pipelined dependent cell runs during the await and fails."
    mid1 = kc.execute(_unlocked_await)
    mid2 = kc.execute("print(b)")
    replies = collect_shell_replies(kc, {mid1, mid2}, timeout=10)
    assert replies[mid1]["content"]["status"] == "ok", replies[mid1]["content"]
    assert replies[mid2]["content"]["status"] == "error", replies[mid2]["content"]
    assert replies[mid2]["content"]["ename"] == "NameError", replies[mid2]["content"]


# Mimics solveit's load_dialog with a subshell instead of unlock(): the caller cell opens
# subshell() then awaits; untagged executes from the *same client session* are routed to the
# subshell and run (in order, on their own lane) while the caller is busy. Routing happens at
# arrival time, so the client must send the cells only after the CM is entered - hence the
# 'subshell ready' print, mirroring load_dialog asking the client for cells mid-cell.
_subshell_caller = """import asyncio
from ipymini import subshell
globals().pop('hijack_flag', None)
loop = asyncio.get_running_loop()
ev = asyncio.Event()
with subshell():
    print('subshell ready', flush=True)
    await asyncio.wait_for(ev.wait(), 5)
    hijacked = 'hijack_flag' in globals()
assert not hijacked, 'another session was hijacked into the subshell'
"""


def _await_subshell_ready(kc, mid):
    wait_for_msg(kc.get_iopub_msg, lambda m: parent_id(m) == mid and m["msg_type"] == "stream" and "subshell ready" in m["content"]["text"],
        timeout=10, err="caller cell never entered subshell()")


def test_execute_routed_to_subshell_while_cell_awaits(kc):
    mid1 = kc.execute(_subshell_caller)
    _await_subshell_ready(kc, mid1)
    mid2 = kc.execute("loop.call_soon_threadsafe(ev.set)")
    replies = collect_shell_replies(kc, {mid1, mid2}, timeout=10)
    for mid in (mid1, mid2): assert replies[mid]["content"]["status"] == "ok", replies[mid]["content"]
    assert replies[mid2]["content"]["execution_count"] == 1, "routed cell should run in the fresh subshell"


def test_subshell_routing_ignores_other_sessions(kc):
    "An untagged execute from a different client session is not hijacked; it queues behind the caller as usual."
    with kc.clone() as kc2:
        mid1 = kc.execute(_subshell_caller)
        _await_subshell_ready(kc, mid1)
        other = kc2.execute("hijack_flag = 1")
        time.sleep(0.3)  # let it arrive while the override is active
        mid2 = kc.execute("loop.call_soon_threadsafe(ev.set)")
        replies = collect_shell_replies(kc, {mid1, mid2}, timeout=10)
        for mid in (mid1, mid2): assert replies[mid]["content"]["status"] == "ok", replies[mid]["content"]
        assert kc2.shell_reply(other, timeout=10)["content"]["status"] == "ok"


def test_non_execute_replies_while_async_cell_busy(kc):
    "Info/completion requests are answered while an async cell is busy - no unlock needed."
    flush_channels(kc)
    mid = kc.execute("import asyncio; await asyncio.sleep(1.2)")
    wait_for_status(kc, "busy")
    kid = kc.shell_send("kernel_info_request")
    assert kc.shell_reply(kid, timeout=0.9)["content"]["status"] == "ok"  # would take >1.2s if queued behind the cell
    cid = kc.shell_send("complete_request", code="pri", cursor_pos=3)
    assert kc.shell_reply(cid, timeout=0.9)["content"]["status"] == "ok"
    assert kc.shell_reply(mid, timeout=10)["content"]["status"] == "ok"


def test_sync_cells_still_run_in_order(kc):
    mids = [kc.execute(code) for code in ("order = []", "order.append(1)", "order.append(2)", "order.append(3)")]
    replies = collect_shell_replies(kc, set(mids), timeout=10)
    for mid in mids: assert replies[mid]["content"]["status"] == "ok", replies[mid]["content"]
    _, reply, outputs = kc.exec_drain("print(order)")
    assert reply["content"]["status"] == "ok"
    texts = "".join(m["content"]["text"] for m in iopub_streams(outputs))
    assert "[1, 2, 3]" in texts, texts
