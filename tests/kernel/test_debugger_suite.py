import pytest
from ..aclient import *

timeout = 3


async def get_stack_frames(dap, thread_id): return (await dap.stackTrace(threadId=thread_id))["body"]["stackFrames"]

async def get_scopes(dap, frame_id): return (await dap.scopes(frameId=frame_id))["body"]["scopes"]

def get_scope_ref(scopes, name): return next(s for s in scopes if s["name"] == name)["variablesReference"]

async def get_scope_vars(dap, scopes, name):
    ref = get_scope_ref(scopes, name)
    return (await dap.variables(variablesReference=ref))["body"]["variables"]


async def ensure_configuration_done(kc):
    if getattr(kc, "_debug_config_done", False): return
    reply = await kc.dap.configurationDone()
    assert reply.get("success"), f"configurationDone failed: {reply}"
    kc._debug_config_done = True


async def continue_debugger(kc, stopped):
    thread_id = stopped.get("content", {}).get("body", {}).get("threadId")
    if isinstance(thread_id, int): await kc.dap.continue_(threadId=thread_id)
    else: await kc.dap.continue_()


@pytest.fixture()
async def kernel():
    async with mini_kernel() as (_, kc): yield kc


@pytest.fixture()
async def debug_kernel(kernel):
    reply = await kernel.dap.initialize(**debug_init_args)
    assert reply.get("success"), f"initialize failed: {reply}"
    reply = await kernel.dap.attach()
    assert reply.get("success"), f"attach failed: {reply}"
    try: yield kernel
    finally: await kernel.dap.disconnect(restart=False, terminateDebuggee=True)


async def test_debugger_features(debug_kernel):
    kc = debug_kernel
    dap = kc.dap
    await wait_debug_event(kc, "initialized")

    reply = await kc.cmd.kernel_info()
    features = reply["content"].get("supported_features", [])
    assert "debugger" in features, f"supported_features: {features}"
    assert reply["content"].get("debugger") is True, f"debugger flag missing: {reply['content']}"

    reply = await dap.evaluate(expression="'a' + 'b'", context="repl")
    assert reply.get("success"), f"evaluate failed: {reply}"
    assert reply["body"]["result"] == "ab", f"evaluate result: {reply['body']['result']}"

    var_name = "text"
    value = "Hello the world"
    code = f"{var_name}='{value}'\nprint({var_name})\n"
    reply = await kc.execute(code, reply=True, timeout=timeout)
    assert reply["content"]["status"] == "ok"
    await dap.inspectVariables()
    await dap.richInspectVariables(variableName=var_name)

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
    await ensure_configuration_done(kc)

    c = kc.execute(code, reply=True, timeout=30)
    stopped = await wait_stop(kc)
    assert stopped["content"]["body"]["reason"] == "breakpoint", f"stopped: {stopped}"
    thread_id = stopped["content"]["body"].get("threadId", 1)
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

    reply = await dap.richInspectVariables(variableName=locals_[0]["name"], frameId=frame_id)
    assert reply.get("success"), f"richInspectVariables failed: {reply}"

    reply = await dap.copyToGlobals(srcVariableName="c", dstVariableName="c_copy", srcFrameId=frame_id)
    assert reply.get("success"), f"copyToGlobals failed: {reply}"
    globals_ = await get_scope_vars(dap, scopes, "Globals")
    assert any(v for v in globals_ if v["name"] == "c_copy"), f"globals: {globals_}"

    locals_ref = get_scope_ref(scopes, "Locals")
    globals_ref = get_scope_ref(scopes, "Globals")
    locals_reply = await dap.variables(variablesReference=locals_ref)
    globals_reply = await dap.variables(variablesReference=globals_ref)
    assert locals_reply["success"], f"locals reply: {locals_reply}"
    assert globals_reply["success"], f"globals reply: {globals_reply}"
    reply = await dap.stepOut(threadId=thread_id)
    assert reply.get("success"), f"stepOut failed: {reply}"
    stopped = await wait_stop(kc)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = await get_stack_frames(dap, thread_id)
    assert frames and frames[0]["name"] != "f", f"frames: {frames}"

    await continue_debugger(kc, stopped)
    assert (await c)["content"]["status"] == "ok"

    reply = await dap.setExceptionBreakpoints(filters=["raised"])
    assert reply["success"], f"setExceptionBreakpoints failed: {reply}"
    await ensure_configuration_done(kc)
    c = kc.execute("raise ValueError('boom')", reply=True, timeout=30)
    stopped = await wait_stop(kc)
    reason = stopped["content"]["body"].get("reason")
    assert reason in {"exception", "breakpoint", "pause"}, f"stopped: {stopped}"
    await continue_debugger(kc, stopped)
    reply = await c
    assert reply["content"]["status"] == "error", f"execute reply: {reply.get('content')}"

    reply = await dap.terminate(restart=False)
    assert reply["success"], f"terminate failed: {reply}"
    info = await dap.debugInfo()
    assert info["body"]["breakpoints"] == [], f"breakpoints not cleared: {info['body']['breakpoints']}"
    assert info["body"]["stoppedThreads"] == [], f"stoppedThreads not cleared: {info['body']['stoppedThreads']}"
