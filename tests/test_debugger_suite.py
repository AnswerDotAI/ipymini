import time, pytest
from contextlib import contextmanager
from .kernel_utils import *

TIMEOUT = 3


@contextmanager
def new_kernel():
    with start_kernel() as (_km, kc): yield kc


def get_stack_frames(dap, thread_id): return dap.stackTrace(threadId=thread_id)["body"]["stackFrames"]

def get_scopes(dap, frame_id): return dap.scopes(frameId=frame_id)["body"]["scopes"]

def get_scope_ref(scopes, name): return next(s for s in scopes if s["name"] == name)["variablesReference"]

def get_scope_vars(dap, scopes, name):
    ref = get_scope_ref(scopes, name)
    return dap.variables(variablesReference=ref)["body"]["variables"]


def ensure_configuration_done(kernel):
    if getattr(kernel, "_debug_config_done", False): return
    reply = debug_configuration_done(kernel)
    assert reply.get("success"), f"configurationDone failed: {reply}"
    setattr(kernel, "_debug_config_done", True)


def continue_debugger(kernel, stopped: dict):
    dap = kernel.dap
    cont = getattr(dap, "continue")
    body = stopped.get("content", {}).get("body", {})
    thread_id = body.get("threadId")
    if isinstance(thread_id, int): cont(threadId=thread_id)
    else: cont()


@pytest.fixture()
def kernel():
    with new_kernel() as kc: yield kc


@pytest.fixture()
def debug_kernel(kernel):
    reply = kernel.dap.initialize(**DEBUG_INIT_ARGS)
    assert reply.get("success"), f"initialize failed: {reply}"
    reply = kernel.dap.attach()
    assert reply.get("success"), f"attach failed: {reply}"
    try: yield kernel
    finally: kernel.dap.disconnect(restart=False, terminateDebuggee=True)


def test_debugger_basic_features(debug_kernel):
    dap = debug_kernel.dap
    msg_id = debug_kernel.kernel_info()
    reply = debug_kernel.shell_reply(msg_id)
    features = reply["content"].get("supported_features", [])
    assert "debugger" in features, f"supported_features: {features}"

    reply = dap.evaluate(expression="'a' + 'b'", context="repl")
    assert reply.get("success"), f"evaluate failed: {reply}"
    assert reply["body"]["result"] == "", f"evaluate result: {reply['body']['result']}"

    var_name = "text"
    value = "Hello the world"
    code = f"{var_name}='{value}'\nprint({var_name})\n"
    debug_kernel.execute(code)
    debug_kernel.get_shell_msg(timeout=TIMEOUT)
    dap.inspectVariables()
    dap.richInspectVariables(variableName=var_name)


def test_debugger_breakpoints_and_steps(debug_kernel):
    dap = debug_kernel.dap
    code = """
def f(a, b):
    c = a + b
    return c

def g():
    return f(2, 3)

g()
"""
    source = debug_dump_cell(debug_kernel, code)["body"]["sourcePath"]
    reply = debug_set_breakpoints(debug_kernel, source, 7)
    assert reply["success"], f"setBreakpoints failed: {reply}"
    ensure_configuration_done(debug_kernel)

    debug_kernel.execute(code)
    stopped = wait_for_stop(debug_kernel)
    assert stopped["content"]["body"]["reason"] == "breakpoint", f"stopped: {stopped}"
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stepped = dap.stepIn(threadId=thread_id)
    assert stepped.get("success"), f"stepIn failed: {stepped}"
    stopped = wait_for_stop(debug_kernel)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = get_stack_frames(dap, thread_id)
    assert frames and frames[0]["name"] == "f", f"frames: {frames}"

    reply = dap.next(threadId=thread_id)
    assert reply.get("success"), f"next failed: {reply}"
    stopped = wait_for_stop(debug_kernel)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = get_stack_frames(dap, thread_id)
    frame_id = frames[0]["id"]
    scopes = get_scopes(dap, frame_id)
    locals_ = get_scope_vars(dap, scopes, "Locals")
    local_names = [v["name"] for v in locals_]
    assert "a" in local_names and "b" in local_names, f"locals: {locals_}"

    reply = dap.richInspectVariables(variableName=locals_[0]["name"], frameId=frame_id)
    assert reply.get("success"), f"richInspectVariables failed: {reply}"

    reply = dap.copyToGlobals(srcVariableName="c", dstVariableName="c_copy", srcFrameId=frame_id)
    assert reply.get("success"), f"copyToGlobals failed: {reply}"
    globals_ = get_scope_vars(dap, scopes, "Globals")
    assert any(v for v in globals_ if v["name"] == "c_copy"), f"globals: {globals_}"

    locals_ref = get_scope_ref(scopes, "Locals")
    globals_ref = get_scope_ref(scopes, "Globals")
    locals_reply = dap.variables(variablesReference=locals_ref)
    globals_reply = dap.variables(variablesReference=globals_ref)
    assert locals_reply["success"], f"locals reply: {locals_reply}"
    assert globals_reply["success"], f"globals reply: {globals_reply}"
    reply = dap.stepOut(threadId=thread_id)
    assert reply.get("success"), f"stepOut failed: {reply}"
    stopped = wait_for_stop(debug_kernel)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = get_stack_frames(dap, thread_id)
    assert frames and frames[0]["name"] != "f", f"frames: {frames}"

    continue_debugger(debug_kernel, stopped)


def test_debugger_exceptions_and_terminate(debug_kernel):
    dap = debug_kernel.dap
    reply = dap.setExceptionBreakpoints(filters=["raised"])
    assert reply["success"], f"setExceptionBreakpoints failed: {reply}"
    ensure_configuration_done(debug_kernel)
    msg_id = debug_kernel.execute("raise ValueError('boom')")
    stopped = wait_for_stop(debug_kernel)
    reason = stopped["content"]["body"].get("reason")
    assert reason in {"exception", "breakpoint", "pause"}, f"stopped: {stopped}"
    continue_debugger(debug_kernel, stopped)
    reply = debug_kernel.shell_reply(msg_id)
    assert reply["content"]["status"] == "error", f"execute reply: {reply.get('content')}"

    reply = dap.terminate(restart=False)
    assert reply["success"], f"terminate failed: {reply}"
    info = dap.debugInfo()
    assert info["body"]["breakpoints"] == [], f"breakpoints not cleared: {info['body']['breakpoints']}"
    assert info["body"]["stoppedThreads"] == [], f"stoppedThreads not cleared: {info['body']['stoppedThreads']}"
