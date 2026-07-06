import pytest
from ..aclient import *

pytestmark = pytest.mark.slow

async def _ensure_debug(kc):
    reply = await kc.dap.initialize(**debug_init_args)
    assert reply.get("success")
    attach = await kc.dap.attach()
    if attach and attach.get("success") is False:
        message = attach.get("message", "")
        assert "already attached" in message or "already initialized" in message


async def _break_and_continue(kc, code, line):
    "Set a breakpoint at `line` of `code`, run it, verify the stop, continue, and drain."
    r = await kc.dap.dumpCell(code=code)
    source = r["body"]["sourcePath"]
    await kc.dap.setBreakpoints(breakpoints=[dict(line=line)], source=dict(path=source), sourceModified=False)
    await kc.dap.debugInfo()
    await kc.dap.configurationDone()
    c = kc.execute(code, reply=True, timeout=30)
    stopped = await wait_stop(kc)
    assert stopped["content"]["body"]["reason"] == "breakpoint", f"stopped: {stopped}"
    await kc.dap.continue_(threadId=stopped["content"]["body"]["threadId"])
    reply = await c
    await kc.iopub_drain(parent_id(reply))


async def test_e2e_restart_and_debug():
    async with mini_kernel() as (km, kc):
        reply, outputs = await kc.exec_drain("1+2+3", store_history=False)
        assert reply["content"]["status"] == "ok"
        results = iopub_msgs(outputs, "execute_result")
        assert results, "expected execute_result"

        kc = await reconnect(km, kc)

        reply, outputs = await kc.exec_drain("try:\n    x\nexcept NameError:\n    print('missing')", store_history=False)
        assert reply["content"]["status"] == "ok"
        streams = iopub_streams(outputs)
        assert any("missing" in m["content"].get("text", "") for m in streams)

        await _ensure_debug(kc)
        await kc.dap.configurationDone()
        reply = await kc.dap.evaluate(expression="'a' + 'b'", context="repl")
        assert reply.get("success"), f"evaluate: {reply}"

        code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
        await _break_and_continue(kc, code, line=2)

        code = """
def f(a, b):
    c = a + b
    return c

f(2, 3)"""
        await _break_and_continue(kc, code, line=6)

        kc.stop_channels()
