import pytest
from ..aclient import *

timeout = 3


async def get_stack_frames(dap, thread_id): return (await dap.stackTrace(threadId=thread_id))["body"]["stackFrames"]

async def get_scopes(dap, frame_id): return (await dap.scopes(frameId=frame_id))["body"]["scopes"]

def get_scope_ref(scopes, name): return next(s for s in scopes if s["name"] == name)["variablesReference"]

async def get_scope_vars(dap, scopes, name):
    ref = get_scope_ref(scopes, name)
    reply = await dap.variables(variablesReference=ref)
    assert reply["success"], f"variables reply: {reply}"
    return reply["body"]["variables"]


async def continue_debugger(kc, stopped): await kc.dap.continue_(threadId=stopped["content"]["body"]["threadId"])


async def assert_debugger_reset(dap):
    info = (await dap.debugInfo())["body"]
    assert not info["isStarted"] and info["breakpoints"] == [] and info["stoppedThreads"] == []
    assert info["hashMethod"] == "Murmur2"


async def attach_debugger(kc):
    reply = await kc.dap.initialize(**debug_init_args)
    assert reply["success"], f"initialize failed: {reply}"
    reply = await kc.dap.attach()
    assert reply["success"], f"attach failed: {reply}"
    await wait_debug_event(kc, "initialized")


@pytest.fixture()
async def debug_kernel():
    async with mini_kernel() as (_, kc):
        assert (await kc.dap.terminate(restart=False))["success"]
        assert not (await kc.ctl.debug(type="request", seq=1))["content"]["success"]
        await attach_debugger(kc)
        yield kc


async def test_debugger_features(debug_kernel):
    kc = debug_kernel
    dap = kc.dap

    var_name = "text"
    value = "Hello the world"
    code = f"{var_name}='{value}'\nprint({var_name})\n"
    reply = await kc.reply(code, timeout=timeout)
    assert reply["content"]["status"] == "ok"
    variables = {v["name"]: v["value"] for v in (await dap.inspectVariables())["body"]["variables"]}
    assert variables[var_name] == repr(value) and "In" not in variables
    rich = await dap.richInspectVariables(variableName=var_name)
    assert rich["body"]["data"]["text/plain"] == repr(value)

    code = """
def f(a, b):
    c = a + b
    return c

def g():
    return f(2, 3)

g()
"""
    source = (await dap.dumpCell(code=code))["body"]["sourcePath"]
    reply = await dap.setBreakpoints(breakpoints=[dict(line=7)], source=dict(path=source), sourceModified=False)
    assert reply["success"], f"setBreakpoints failed: {reply}"
    info = (await dap.debugInfo())["body"]
    assert info["isStarted"] and any(b["source"] == source and b["breakpoints"] == [{"line": 7}] for b in info["breakpoints"])
    assert source.startswith(info["tmpFilePrefix"]) and source.endswith(info["tmpFileSuffix"])

    c = kc.reply(code, timeout=30)
    stopped = await wait_stop(kc)
    assert stopped["content"]["body"]["reason"] == "breakpoint", f"stopped: {stopped}"
    thread_id = stopped["content"]["body"]["threadId"]
    frames = await get_stack_frames(dap, thread_id)
    assert frames[0]["line"] == 7 and frames[0]["source"]["path"] == source, frames
    stepped = await dap.stepIn(threadId=thread_id)
    assert stepped.get("success"), f"stepIn failed: {stepped}"
    stopped = await wait_stop(kc)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = await get_stack_frames(dap, thread_id)
    assert frames and frames[0]["name"] == "f", f"frames: {frames}"

    reply = await dap.next(threadId=thread_id)
    assert reply.get("success"), f"next failed: {reply}"
    stopped = await wait_stop(kc)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = await get_stack_frames(dap, thread_id)
    frame_id = frames[0]["id"]
    scopes = await get_scopes(dap, frame_id)
    locals_ = await get_scope_vars(dap, scopes, "Locals")
    local_names = [v["name"] for v in locals_]
    assert "a" in local_names and "b" in local_names, f"locals: {locals_}"

    reply = await dap.richInspectVariables(variableName="a", frameId=frame_id)
    assert reply.get("success"), f"richInspectVariables failed: {reply}"
    assert reply["body"]["data"]["text/plain"] == "2", reply

    reply = await dap.copyToGlobals(srcVariableName="c", dstVariableName="c_copy", srcFrameId=frame_id)
    assert reply.get("success"), f"copyToGlobals failed: {reply}"
    globals_ = await get_scope_vars(dap, scopes, "Globals")
    assert any(v for v in globals_ if v["name"] == "c_copy"), f"globals: {globals_}"

    reply = await dap.stepOut(threadId=thread_id)
    assert reply.get("success"), f"stepOut failed: {reply}"
    stopped = await wait_stop(kc)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = await get_stack_frames(dap, thread_id)
    assert frames and frames[0]["name"] != "f", f"frames: {frames}"

    await continue_debugger(kc, stopped)
    assert (await c)["content"]["status"] == "ok"

    reply = await dap.disconnect(restart=False, terminateDebuggee=False)
    assert reply["success"], f"disconnect failed: {reply}"
    await assert_debugger_reset(dap)
    await attach_debugger(kc)

    reply = await dap.setExceptionBreakpoints(filters=["userUnhandled"])
    assert reply["success"], f"setExceptionBreakpoints failed: {reply}"
    c = kc.reply("raise ValueError('boom')", timeout=30)
    stopped = await wait_stop(kc)
    reason = stopped["content"]["body"].get("reason")
    assert reason == "exception", f"stopped: {stopped}"
    await continue_debugger(kc, stopped)
    reply = await c
    assert reply["content"]["status"] == "error", f"execute reply: {reply.get('content')}"

    reply = await dap.terminate(restart=False)
    assert reply["success"], f"terminate failed: {reply}"
    await assert_debugger_reset(dap)
