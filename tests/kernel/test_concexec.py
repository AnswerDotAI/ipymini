import asyncio, pytest
from uuid import uuid4
from ..aclient import *

@pytest.fixture(scope="module")
async def kc():
    "One shared kernel for this module; each test uses fresh names and drains its own requests."
    async with mini_kernel() as (_, kc): yield kc


async def test_dependent_async_cells_serialized_by_default(kc):
    "Pipelined dependent cells keep FIFO completion order (stock ipykernel parity)."
    c1 = kc.reply("import asyncio; a = await asyncio.sleep(0.1, 1)", timeout=10)
    c2 = kc.reply("assert a == 1", timeout=10)
    for r in await asyncio.gather(c1, c2): assert r["content"]["status"] == "ok", r["content"]


# Mimics solveit's load_dialog: the caller cell opens
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
    await asyncio.wait_for(ev.wait(), 30)  # a bound, not a sleep: under parallel-worker load the old 5s could genuinely elapse
    hijacked = 'hijack_flag' in globals()
assert not hijacked, 'another session was hijacked into the subshell'
"""


async def _await_subshell_ready(kc, mid):
    await wait_iopub(kc, lambda m: parent_id(m) == mid and m["msg_type"] == "stream" and "subshell ready" in m["content"]["text"],
        timeout=10, err="caller cell never entered subshell()")


async def test_execute_routed_to_subshell_while_cell_awaits(kc):
    mid1 = str(uuid4())  # explicit msg_id: we need it before the reply, to watch iopub mid-execution
    c1 = kc.reply(_subshell_caller, timeout=10, msg_id=mid1)
    await _await_subshell_ready(kc, mid1)
    c2 = kc.reply("loop.call_soon_threadsafe(ev.set)", timeout=10)
    r1, r2 = await asyncio.gather(c1, c2)
    for r in (r1, r2): assert r["content"]["status"] == "ok", r["content"]
    assert r2["content"]["execution_count"] == 1, "routed cell should run in the fresh subshell"


async def test_subshell_routing_ignores_other_sessions(kc):
    "An untagged execute from a different client session is not hijacked; it queues behind the caller as usual."
    async with clone(kc) as kc2:
        mid1 = str(uuid4())
        c1 = kc.reply(_subshell_caller, timeout=10, msg_id=mid1)
        await _await_subshell_ready(kc, mid1)
        other = kc2.reply("hijack_flag = 1", timeout=10)
        await asyncio.sleep(0.3)  # let it arrive while the override is active
        c2 = kc.reply("loop.call_soon_threadsafe(ev.set)", timeout=10)
        for r in await asyncio.gather(c1, c2): assert r["content"]["status"] == "ok", r["content"]
        assert (await other)["content"]["status"] == "ok"


async def test_non_execute_replies_while_async_cell_busy(kc):
    "Info and completion requests are answered while an async cell is busy."
    await aflush(kc)
    c = kc.reply("import asyncio; await asyncio.sleep(1.2)", timeout=10)
    await wait_status(kc, "busy")
    r = await kc.cmd.kernel_info(timeout=0.9)  # would take >1.2s if queued behind the cell
    assert r["content"]["status"] == "ok"
    r = await kc.cmd.complete(code="pri", cursor_pos=3, timeout=0.9)
    assert r["content"]["status"] == "ok"
    assert (await c)["content"]["status"] == "ok"


async def test_sync_cells_still_run_in_order(kc):
    cs = [kc.reply(code, timeout=10) for code in ("order = []", "order.append(1)", "order.append(2)", "order.append(3)")]
    for r in await asyncio.gather(*cs): assert r["content"]["status"] == "ok", r["content"]
    assert (o := await kc.eval_expr("order")) == [1, 2, 3], o


_reentrant_setup = """import asyncio
from comm import get_comm_manager
rev = asyncio.Event()
def _rt(comm, open_msg):
    comm.on_msg(lambda m: (print('reentrant-print'), rev.set()))
get_comm_manager().register_target('reentrant', _rt)
"""

_reentrant_waiter = """import asyncio
await asyncio.wait_for(rev.wait(), 5)
print('cell-b-after')
"""


async def test_comm_capture_while_cell_awaits(kc):
    "A comm callback firing while a cell awaits captures stdout to its comm_msg parent without disturbing the cell's output parent."
    assert (await kc.exec_drain(_reentrant_setup))[0]["content"]["status"] == "ok"
    c1 = kc.reply(_reentrant_waiter, timeout=10)
    kc.comm_open("reentrant", "re-1")
    mid = kc.comm_msg("re-1")
    s = await wait_iopub(kc, lambda m: m["msg_type"] == "stream" and "reentrant-print" in m["content"].get("text", ""),
        err="comm callback stdout never reached iopub")
    assert parent_id(s) == mid, "comm-callback stream must be parented to the comm_msg, not the unlocked cell"
    r1 = await c1
    assert r1["content"]["status"] == "ok", r1["content"]
    b = await wait_iopub(kc, lambda m: m["msg_type"] == "stream" and "cell-b-after" in m["content"].get("text", ""),
        err="cell's own stdout after resume never reached iopub")
    assert parent_id(b) == parent_id(r1), "resumed cell's stream must be parented to its own execute"
