import asyncio, pytest
from uuid import uuid4
from ..aclient import *

@pytest.fixture(scope="module")
async def kc():
    "One shared kernel for this module; each test uses fresh names and drains its own requests."
    async with mini_kernel() as (_, kc): yield kc


# Mimics solveit's `load_dialog` re-entrancy: a cell calls unlock() then awaits a response that can
# only arrive once later execute_requests on the same channel have run (ipyku_launcher/UnlockKernel
# semantics, but opt-in per cell). The event dependency is the proof: cell 1 can only complete if
# cell 2 ran during its await.
_unlocker = """import asyncio
ev = asyncio.Event()
get_ipython().kernel.unlock()
await asyncio.wait_for(ev.wait(), 5)
"""


async def test_execute_processed_while_unlocked_cell_awaits(kc):
    c1 = kc.execute(_unlocker, reply=True, timeout=10)
    c2 = kc.execute("ev.set()", reply=True, timeout=10)
    for r in await asyncio.gather(c1, c2): assert r["content"]["status"] == "ok", r["content"]


async def test_dependent_async_cells_serialized_by_default(kc):
    "Without unlock(), pipelined dependent cells keep FIFO completion order (stock ipykernel parity)."
    c1 = kc.execute("import asyncio; a = await asyncio.sleep(0.1, 1)", reply=True, timeout=10)
    c2 = kc.execute("assert a == 1", reply=True, timeout=10)
    for r in await asyncio.gather(c1, c2): assert r["content"]["status"] == "ok", r["content"]


_unlocked_await = """import asyncio
from ipymini import unlock
unlock()
b = await asyncio.sleep(0.3, 1)
"""


async def test_unlock_trades_away_dependency_ordering(kc):
    "Documented semantics: after unlock(), a pipelined dependent cell runs during the await and fails."
    # the NameError reply lands while the first cell is still pending; the default
    # fail_pending=False means it doesn't fail that cell's routed reply queue client-side
    c1 = kc.execute(_unlocked_await, reply=True, timeout=10)
    c2 = kc.execute("print(b)", reply=True, timeout=10)
    r1, r2 = await asyncio.gather(c1, c2)
    assert r1["content"]["status"] == "ok", r1["content"]
    assert r2["content"]["status"] == "error", r2["content"]
    assert r2["content"]["ename"] == "NameError", r2["content"]


# Mimics solveit's load_dialog with a subshell instead of unlock(): the caller cell opens
# subshell() then awaits; untagged executes from the *same client session* are routed to the
# subshell and run (in order, on their own lane) while the caller is busy. Routing happens at
# arrival time, so the client must send the cells only after the CM is entered - hence the
# 'subshell ready' print, mirroring load_dialog asking the client for cells mid-cell.
_subshell_caller = """import asyncio
globals().pop('hijack_flag', None)
loop = asyncio.get_running_loop()
ev = asyncio.Event()
with get_ipython().kernel.subshell():
    print('subshell ready', flush=True)
    await asyncio.wait_for(ev.wait(), 5)
    hijacked = 'hijack_flag' in globals()
assert not hijacked, 'another session was hijacked into the subshell'
"""


async def _await_subshell_ready(kc, mid):
    await wait_iopub(kc, lambda m: parent_id(m) == mid and m["msg_type"] == "stream" and "subshell ready" in m["content"]["text"],
        timeout=10, err="caller cell never entered subshell()")


async def test_execute_routed_to_subshell_while_cell_awaits(kc):
    mid1 = str(uuid4())  # explicit msg_id: we need it before the reply, to watch iopub mid-execution
    c1 = kc.execute(_subshell_caller, reply=True, timeout=10, msg_id=mid1)
    await _await_subshell_ready(kc, mid1)
    c2 = kc.execute("loop.call_soon_threadsafe(ev.set)", reply=True, timeout=10)
    r1, r2 = await asyncio.gather(c1, c2)
    for r in (r1, r2): assert r["content"]["status"] == "ok", r["content"]
    assert r2["content"]["execution_count"] == 1, "routed cell should run in the fresh subshell"


async def test_subshell_routing_ignores_other_sessions(kc):
    "An untagged execute from a different client session is not hijacked; it queues behind the caller as usual."
    async with clone(kc) as kc2:
        mid1 = str(uuid4())
        c1 = kc.execute(_subshell_caller, reply=True, timeout=10, msg_id=mid1)
        await _await_subshell_ready(kc, mid1)
        other = kc2.execute("hijack_flag = 1", reply=True, timeout=10)
        await asyncio.sleep(0.3)  # let it arrive while the override is active
        c2 = kc.execute("loop.call_soon_threadsafe(ev.set)", reply=True, timeout=10)
        for r in await asyncio.gather(c1, c2): assert r["content"]["status"] == "ok", r["content"]
        assert (await other)["content"]["status"] == "ok"


async def test_non_execute_replies_while_async_cell_busy(kc):
    "Info/completion requests are answered while an async cell is busy - no unlock needed."
    await aflush(kc)
    c = kc.execute("import asyncio; await asyncio.sleep(1.2)", reply=True, timeout=10)
    await wait_status(kc, "busy")
    r = await kc.cmd.kernel_info(timeout=0.9)  # would take >1.2s if queued behind the cell
    assert r["content"]["status"] == "ok"
    r = await kc.cmd.complete(code="pri", cursor_pos=3, timeout=0.9)
    assert r["content"]["status"] == "ok"
    assert (await c)["content"]["status"] == "ok"


async def test_sync_cells_still_run_in_order(kc):
    cs = [kc.execute(code, reply=True, timeout=10)
        for code in ("order = []", "order.append(1)", "order.append(2)", "order.append(3)")]
    for r in await asyncio.gather(*cs): assert r["content"]["status"] == "ok", r["content"]
    assert (o := await kc.eval_expr("order")) == [1, 2, 3], o


_reentrant_setup = """import asyncio
from comm import get_comm_manager
rev = asyncio.Event()
def _rt(comm, open_msg):
    comm.on_msg(lambda m: (print('reentrant-print'), rev.set()))
get_comm_manager().register_target('reentrant', _rt)
"""

_reentrant_unlocker = """import asyncio
get_ipython().kernel.unlock()
await asyncio.wait_for(rev.wait(), 5)
print('cell-b-after')
"""


async def test_comm_capture_reentrant_during_unlock(kc):
    "A comm callback firing during an unlocked cell's await captures stdout to iopub parented to the comm_msg, without disturbing the awaiting cell's own output parent."
    assert (await kc.exec_drain(_reentrant_setup))[0]["content"]["status"] == "ok"
    c1 = kc.execute(_reentrant_unlocker, reply=True, timeout=10)
    kc.shell_request("comm_open", reply=False, comm_id="re-1", target_name="reentrant", data={})
    mid = kc.shell_request("comm_msg", reply=False, comm_id="re-1", data={})
    s = await wait_iopub(kc, lambda m: m["msg_type"] == "stream" and "reentrant-print" in m["content"].get("text", ""),
        err="comm callback stdout during unlock never reached iopub")
    assert parent_id(s) == mid, "comm-callback stream must be parented to the comm_msg, not the unlocked cell"
    r1 = await c1
    assert r1["content"]["status"] == "ok", r1["content"]
    b = await wait_iopub(kc, lambda m: m["msg_type"] == "stream" and "cell-b-after" in m["content"].get("text", ""),
        err="unlocked cell's own stdout after resume never reached iopub")
    assert parent_id(b) == parent_id(r1), "resumed cell's stream must be parented to its own execute"
