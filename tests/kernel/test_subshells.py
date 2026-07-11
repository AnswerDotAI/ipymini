import asyncio, random
from uuid import uuid4
import pytest
from ..aclient import *
from ..kernel_utils import iopub_msgs, iopub_streams

timeout = 10


@pytest.fixture(scope="module")
async def kc():
    "One shared kernel for this module; tests create/delete their own subshells and use fresh names."
    async with mini_kernel() as (_, kc): yield kc


def _last_history_input(reply: dict)->str|None:
    hist = reply.get("content", {}).get("history") or []
    if not hist: return None
    item = hist[-1]
    if isinstance(item, (list, tuple)) and len(item) >= 3: return item[2]
    return None


# --- async helpers, for the migrated tests ---

async def _acreate_subshell(kc)->str:
    reply = await kc.ctl.create_subshell()
    assert reply["content"]["status"] == "ok"
    subshell_id = reply["content"].get("subshell_id")
    assert subshell_id
    return subshell_id


async def _adelete_subshell(kc, subshell_id:str):
    reply = await kc.ctl.delete_subshell(subshell_id=subshell_id)
    assert reply["content"]["status"] == "ok"


def _asend_execute(kc, code:str, subshell_id:str|None=None, **content):
    "Send an execute now; return (msg_id, coroutine-for-reply)."
    mid = str(uuid4())
    return mid, kc.execute(code, subsh_id=subshell_id, reply=True, timeout=timeout, msg_id=mid, **content)


async def _aexecute(kc, code:str, subshell_id:str|None=None, **content):
    "Execute (stop_on_error=False matches the wire content the sync cmd proxy sent); return (reply, outputs)."
    reply = await kc.execute(code, subsh_id=subshell_id, reply=True, timeout=timeout, stop_on_error=False, **content)
    return reply, await kc.iopub_drain(parent_id(reply))


async def _ahistory_tail(kc, subshell_id:str|None, n:int = 1):
    return await kc.cmd.history(hist_access_type="tail", n=n, output=False, raw=True, subshell_id=subshell_id)


async def _alist_subshells(kc):
    reply = await kc.ctl.list_subshell()
    assert reply["content"]["status"] == "ok"
    return reply["content"].get("subshell_id", [])


async def test_subshell_basics():
    async with mini_kernel(extra_env={"IPYMINI_EXPERIMENTAL_COMPLETIONS": "0"}) as (_, kc):
        reply = await kc.cmd.kernel_info()
        features = reply["content"].get("supported_features", [])
        assert "kernel subshells" in features

        assert await _alist_subshells(kc) == []
        subshell_id = await _acreate_subshell(kc)
        assert await _alist_subshells(kc) == [subshell_id]

        reply1, _ = await _aexecute(kc, "a = 10")
        assert reply1["content"]["execution_count"] == 1

        reply2, outputs2 = await _aexecute(kc, "a", subshell_id=subshell_id)
        assert reply2["content"]["execution_count"] == 1
        results = iopub_msgs(outputs2, "execute_result")
        assert results, "expected execute_result"
        assert results[0]["content"]["data"].get("text/plain") == "10"
        assert results[0]["parent_header"].get("subshell_id") == subshell_id

        reply, _ = await _aexecute(kc, "from IPython import get_ipython as imported_get_ipython", store_history=False)
        assert reply["content"]["status"] == "ok"

        identity_code = (
            "import builtins, IPython\n"
            "from IPython.core import getipython as core_getipython\n"
            "shell = get_ipython()\n"
            "checks = dict(top=IPython.get_ipython() is shell, core=core_getipython.get_ipython() is shell,\n"
            "    builtins=builtins.get_ipython() is shell, imported=imported_get_ipython() is shell)\n"
            "assert all(checks.values()), checks\n")
        for sid in (None, subshell_id):
            reply, _ = await _aexecute(kc, identity_code, subshell_id=sid, store_history=False)
            assert reply["content"]["status"] == "ok", reply["content"]

        reply3, _ = await _aexecute(kc, "a + 1")
        assert reply3["content"]["execution_count"] == 2

        await _aexecute(kc, "parent_only = 123")
        await _aexecute(kc, "child_only = 456", subshell_id=subshell_id)

        parent_hist = await _ahistory_tail(kc, None)
        child_hist = await _ahistory_tail(kc, subshell_id)

        assert _last_history_input(parent_hist) == "parent_only = 123"
        assert _last_history_input(child_hist) == "child_only = 456"

        reply = await kc.cmd.execute(code="1+1", subshell_id="missing")
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") == "SubshellNotFound"

        await _adelete_subshell(kc, subshell_id)
        assert await _alist_subshells(kc) == []


async def test_subshell_asyncio_create_task(kc):
    subshell_id = await _acreate_subshell(kc)
    code = (
        "import asyncio, time\n"
        "async def f():\n"
        "    await asyncio.sleep(0.01)\n"
        "    print('ok')\n"
        "asyncio.create_task(f())\n"
        "time.sleep(0.05)\n")
    reply = await kc.execute(code, subsh_id=subshell_id, reply=True, timeout=timeout)
    assert reply["content"]["status"] == "ok"
    mid = parent_id(reply)
    await wait_iopub(kc, lambda m: parent_id(m) == mid and m.get("msg_type") == "stream" and "ok" in m.get("content", {}).get("text", ""),
        timeout=timeout, err="expected stdout from subshell create_task")
    await _adelete_subshell(kc, subshell_id)


async def test_subshell_concurrency_and_control(kc):
    await aflush(kc)
    subshell_a = await _acreate_subshell(kc)
    subshell_b = await _acreate_subshell(kc)

    await kc.exec_drain("import threading; control_gate = threading.Event()", timeout=timeout)
    msg_id, c_msg = _asend_execute(kc, "ok = control_gate.wait(5); print(ok)")
    await wait_status(kc, "busy")

    control_reply = await kc.ctl.create_subshell()
    subshell_id = control_reply["content"]["subshell_id"]
    release_id, c_release = _asend_execute(kc, "control_gate.set(); print('released')", subshell_a)
    release_reply = await c_release
    assert release_reply["content"]["status"] == "ok"

    shell_reply = await c_msg
    outputs = await collect_iopub(kc, {msg_id, release_id})
    release_streams = iopub_streams(outputs[release_id])
    assert any("released" in m["content"].get("text", "") for m in release_streams)
    shell_streams = iopub_streams(outputs[msg_id])
    assert any("True" in m["content"].get("text", "") for m in shell_streams)

    await _adelete_subshell(kc, subshell_id)

    assert shell_reply["content"]["status"] == "ok"

    await kc.exec_drain("import threading; evt = threading.Event()", timeout=timeout)

    wait_id, c_wait = _asend_execute(kc, "ok = evt.wait(1.0); print(ok)", subshell_a)
    set_id, c_set = _asend_execute(kc, "evt.set(); print('set')")

    reply_wait, reply_set = await asyncio.gather(c_wait, c_set)
    outputs = await collect_iopub(kc, {wait_id, set_id})
    outputs_wait = outputs[wait_id]
    outputs_set = outputs[set_id]

    assert reply_wait["content"]["status"] == "ok"
    assert reply_set["content"]["status"] == "ok"
    streams_wait = iopub_streams(outputs_wait)
    assert any("True" in m["content"].get("text", "") for m in streams_wait), f"wait outputs: {outputs_wait}; set outputs: {outputs_set}"
    streams_set = iopub_streams(outputs_set)
    assert any("set" in m["content"].get("text", "") for m in streams_set)

    await kc.exec_drain("import threading, time; barrier = threading.Barrier(3)", timeout=timeout)

    id_parent, c_parent = _asend_execute(kc, "barrier.wait(); time.sleep(0.05); print('parent')")
    id_a, c_a = _asend_execute(kc, "barrier.wait(); time.sleep(0.05); print('a')", subshell_a)
    id_b, c_b = _asend_execute(kc, "barrier.wait(); time.sleep(0.05); print('b')", subshell_b)

    msg_ids = {id_parent, id_a, id_b}
    reply_parent, reply_a, reply_b = await asyncio.gather(c_parent, c_a, c_b)
    replies = {id_parent: reply_parent, id_a: reply_a, id_b: reply_b}
    outputs = await collect_iopub(kc, msg_ids)

    assert all(reply["content"]["status"] == "ok" for reply in replies.values())
    expected = {id_parent: "parent", id_a: "a", id_b: "b"}
    for msg_id, text in expected.items():
        streams = iopub_streams(outputs[msg_id])
        assert any(text in m["content"].get("text", "") for m in streams)
        # Per-request status envelope: busy first, idle last, each parented to its own execute.
        # Solveit's per-message idle event (Message._msgidle_evt) relies on exactly this under
        # concurrent execution: idle for a request means all its outputs have been published.
        assert outputs[msg_id][0]["msg_type"] == "status" and outputs[msg_id][0]["content"]["execution_state"] == "busy", outputs[msg_id][0]
        assert outputs[msg_id][-1]["msg_type"] == "status" and outputs[msg_id][-1]["content"]["execution_state"] == "idle", outputs[msg_id][-1]

    await _adelete_subshell(kc, subshell_a)
    await _adelete_subshell(kc, subshell_b)


async def test_interrupt_during_concurrent_subshell_execution(kc):
    "Interrupt with executes in flight on parent and subshell: every request must still get its terminal idle (Solveit's Stop during parallel tool calls relies on this)."
    subshell_id = await _acreate_subshell(kc)
    try:
        id_parent, c_parent = _asend_execute(kc, "import time; time.sleep(30)")
        id_sub, c_sub = _asend_execute(kc, "import time; time.sleep(0.5); print('sub done')", subshell_id)
        await wait_iopub(kc, lambda m: parent_id(m) == id_parent and m.get("msg_type") == "status"
            and m["content"]["execution_state"] == "busy")
        assert (await kc.interrupt())["content"]["status"] == "ok"
        reply_parent, reply_sub = await asyncio.gather(c_parent, c_sub)
        assert reply_parent["content"]["status"] == "error"
        outputs = await collect_iopub(kc, {id_parent, id_sub})
        for mid in (id_parent, id_sub):
            last = outputs[mid][-1]
            assert last["msg_type"] == "status" and last["content"]["execution_state"] == "idle", \
                (mid, [m.get("msg_type") for m in outputs[mid]])
    finally: await _adelete_subshell(kc, subshell_id)


async def test_subshell_reads_shared_ns_during_parent_sleep(kc):
    subshell_id = await _acreate_subshell(kc)
    c_parent = kc.execute("x = 123; import time; time.sleep(1.0); print('done')", reply=True, timeout=3)
    await asyncio.sleep(0.1)

    c_sub = kc.execute("print(x)", subsh_id=subshell_id, reply=True, timeout=0.8)
    try: reply_sub = await c_sub
    except TimeoutError: raise AssertionError("subshell reply did not arrive while parent was busy")

    outputs = await kc.iopub_drain(parent_id(reply_sub))
    streams = iopub_streams(outputs)
    assert any("123" in m["content"].get("text", "") for m in streams), f"expected subshell to read shared ns, got: {streams}"

    reply_parent = await c_parent
    assert reply_parent["content"]["status"] == "ok"
    outputs_parent = await kc.iopub_drain(parent_id(reply_parent))
    streams_parent = iopub_streams(outputs_parent)
    assert any("done" in m["content"].get("text", "") for m in streams_parent)
    await _adelete_subshell(kc, subshell_id)


async def test_subshell_creation_keeps_history_ns_linked(kc):
    "Creating a subshell must not orphan In/Out in the shared user_ns from the parent's HistoryManager."
    subshell_id = await _acreate_subshell(kc)
    try:
        reply, _ = await _aexecute(kc, "hist_probe = 42")
        assert reply["content"]["status"] == "ok"
        probe = ("ip = get_ipython()\n"
            "print(ip.user_ns['In'] is ip.history_manager.input_hist_parsed,"
            " ip.user_ns['Out'] is ip.history_manager.output_hist, 'hist_probe = 42' in In)")
        for sid in (None, subshell_id):
            reply, outputs = await _aexecute(kc, probe, subshell_id=sid)
            assert reply["content"]["status"] == "ok"
            text = "".join(m["content"].get("text", "") for m in iopub_streams(outputs))
            assert "True True True" in text, f"history ns decoupled ({'child' if sid else 'parent'}): {text!r}"
    finally: await _adelete_subshell(kc, subshell_id)


async def test_subshell_interrupt_request_breaks_sleep(kc):
    subshell_id = await _acreate_subshell(kc)
    msg_id, c = _asend_execute(kc, "import time; time.sleep(0.7); print('done')", subshell_id=subshell_id)
    await asyncio.sleep(0.1)
    await kc.interrupt()
    reply = await c
    assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
    outputs = await kc.iopub_drain(msg_id)
    errors = iopub_msgs(outputs, "error")
    assert errors, f"expected iopub error after interrupt, got: {[m.get('msg_type') for m in outputs]}"
    assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", f"interrupt iopub: {errors[-1].get('content')}"
    await _adelete_subshell(kc, subshell_id)


async def test_subshell_stop_on_error_isolated(kc):
    "An error aborts only its own subshell's queued cells; with fail_pending=False (default) the real kernel-issued replies are observable."
    for are_subshells in [(False, True), (True, False), (True, True)]:
        subshell_ids = [await _acreate_subshell(kc) if is_subshell else None for is_subshell in are_subshells]

        sends = [("import asyncio; await asyncio.sleep(0.1); raise ValueError()", subshell_ids[0]),  # chkstyle: ignore
            ("print('hello')", subshell_ids[0]),
            ("print('goodbye')", subshell_ids[0]),
            ("import time; time.sleep(0.15)", subshell_ids[1]),
            ("print('other')", subshell_ids[1])]
        pairs = [_asend_execute(kc, code, sid) for code, sid in sends]
        msg_ids = [m for m, _ in pairs]
        replies = dict(zip(msg_ids, await asyncio.gather(*(c for _, c in pairs))))

        assert replies[msg_ids[0]]["parent_header"].get("subshell_id") == subshell_ids[0]
        assert replies[msg_ids[1]]["parent_header"].get("subshell_id") == subshell_ids[0]
        assert replies[msg_ids[2]]["parent_header"].get("subshell_id") == subshell_ids[0]
        assert replies[msg_ids[3]]["parent_header"].get("subshell_id") == subshell_ids[1]
        assert replies[msg_ids[4]]["parent_header"].get("subshell_id") == subshell_ids[1]

        assert replies[msg_ids[0]]["content"]["status"] == "error"
        assert replies[msg_ids[1]]["content"]["status"] == "aborted"
        assert replies[msg_ids[2]]["content"]["status"] == "aborted"
        assert replies[msg_ids[3]]["content"]["status"] == "ok"
        assert replies[msg_ids[4]]["content"]["status"] == "ok"

        reply, _ = await _aexecute(kc, "print('check')", subshell_ids[0])
        assert reply["parent_header"].get("subshell_id") == subshell_ids[0]
        assert reply["content"]["status"] == "ok"

        for subshell_id in subshell_ids:
            if subshell_id: await _adelete_subshell(kc, subshell_id)


async def test_delete_busy_subshell_interrupts_before_removing(kc):
    await aflush(kc)
    subshell_id = await _acreate_subshell(kc)
    c = kc.execute("try:\n    while True: pass\nfinally:\n    import time; time.sleep(0.2)",
        subsh_id=subshell_id, reply=True, timeout=timeout, stop_on_error=False)
    await wait_status(kc, "busy")
    dc = asyncio.create_task(kc.ctl.delete_subshell(subshell_id=subshell_id))
    await asyncio.sleep(0.12)
    late_id, c_late = _asend_execute(kc, "1+1", subshell_id=subshell_id, stop_on_error=False)
    reply = await dc
    assert reply["content"]["status"] == "ok"
    r1, r2 = await asyncio.gather(c, c_late)
    assert r1["content"]["status"] == "error"
    assert r2["content"]["status"] == "error"
    list_reply = await kc.ctl.list_subshell()
    assert subshell_id not in list_reply["content"]["subshell_id"]


async def test_subshell_fuzzes():
    async with mini_kernel() as (_, kc):
        code = ("import time, warnings; from IPython.core import completer; "
            "warnings.filterwarnings('ignore', category=completer.ProvisionalCompleterWarning)")

        subshells = [await _acreate_subshell(kc) for _ in range(2)]
        await _aexecute(kc, code)

        pending = {}
        for idx in range(4):
            mid, c = _asend_execute(kc, f"time.sleep(0.02); print('parent:{idx}')", stop_on_error=False)
            pending[mid] = c
            for sid in subshells:
                mid, c = _asend_execute(kc, f"time.sleep(0.02); print('{sid[:4]}:{idx}')", sid, stop_on_error=False)
                pending[mid] = c

        replies = dict(zip(pending, await asyncio.gather(*pending.values())))
        outputs = await collect_iopub(kc, set(pending))
        assert all(reply["content"]["status"] == "ok" for reply in replies.values())
        for msg_id, msgs in outputs.items():
            streams = iopub_streams(msgs)
            assert streams, f"missing stream output for {msg_id}"

        for sid in subshells: await _adelete_subshell(kc, sid)

        rng = random.Random(0)
        subshells = [await _acreate_subshell(kc) for _ in range(3)]
        await _aexecute(kc, "import time")

        requests, exec_ids = {}, set()
        for idx in range(20):
            subshell_id = rng.choice([None, *subshells])
            action = rng.choice(["execute", "complete", "inspect", "history"])
            mid = str(uuid4())
            if action == "execute":
                mid, c = _asend_execute(kc, f"time.sleep(0.01); print('fuzz:{idx}')", subshell_id, stop_on_error=False)
                exec_ids.add(mid)
            elif action == "complete":
                code = "rang"
                c = kc.cmd.complete(code=code, cursor_pos=len(code), subshell_id=subshell_id, msg_id=mid)
            elif action == "inspect":
                code = "print"
                c = kc.cmd.inspect(code=code, cursor_pos=len(code), subshell_id=subshell_id, msg_id=mid)
            else: c = kc.cmd.history(hist_access_type="tail", n=1, output=False, raw=True, subshell_id=subshell_id, msg_id=mid)
            requests[mid] = c

        replies = dict(zip(requests, await asyncio.gather(*requests.values())))
        assert all(reply["content"]["status"] in {"ok", "error"} for reply in replies.values())

        outputs = await collect_iopub(kc, exec_ids) if exec_ids else {}
        for msg_id in exec_ids:
            streams = iopub_streams(outputs[msg_id])
            assert streams, f"missing stream output for {msg_id}"

        for sid in subshells: await _adelete_subshell(kc, sid)
