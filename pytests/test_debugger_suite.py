import time, pytest
from contextlib import contextmanager
from .kernel_utils import (DEBUG_INIT_ARGS, debug_configuration_done, debug_continue, debug_dump_cell,
    debug_info, debug_request, debug_set_breakpoints, get_shell_reply, start_kernel, wait_for_stop)

TIMEOUT = 3


@contextmanager
def new_kernel():
    with start_kernel() as (_km, kc): yield kc


def prepare_debug_request(kernel, command, arguments=None, **kwargs):
    if arguments is None: arguments = {}
    if kwargs: arguments |= kwargs
    seq = getattr(kernel, "_debug_seq", 1)
    setattr(kernel, "_debug_seq", seq + 1)
    msg = kernel.session.msg("debug_request", dict(type="request", seq=seq, command=command, arguments=arguments or {}))
    return msg


def wait_for_debug_request(kernel, command, arguments=None, full_reply=False, **kwargs):
    if arguments is None: arguments = {}
    if kwargs: arguments |= kwargs
    return debug_request(kernel, command, arguments, full_reply=full_reply)


def get_stack_frames(kernel, thread_id):
    return wait_for_debug_request(kernel, "stackTrace", threadId=thread_id)["body"]["stackFrames"]

def get_scopes(kernel, frame_id): return wait_for_debug_request(kernel, "scopes", frameId=frame_id)["body"]["scopes"]

def get_scope_ref(scopes, name): return next(s for s in scopes if s["name"] == name)["variablesReference"]

def get_scope_vars(kernel, scopes, name):
    ref = get_scope_ref(scopes, name)
    return wait_for_debug_request(kernel, "variables", variablesReference=ref)["body"]["variables"]


def get_replies(kernel, msg_ids):
    replies = {msg_id: None for msg_id in msg_ids}
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and any(v is None for v in replies.values()):
        reply = kernel.control_channel.get_msg(timeout=TIMEOUT)
        msg_id = reply["parent_header"].get("msg_id")
        if msg_id in replies: replies[msg_id] = reply
    if any(v is None for v in replies.values()): raise AssertionError("timeout waiting for debug replies")
    return [replies[msg_id] for msg_id in msg_ids]


def ensure_configuration_done(kernel) -> None:
    if getattr(kernel, "_debug_config_done", False): return
    reply = debug_configuration_done(kernel)
    assert reply.get("success"), f"configurationDone failed: {reply}"
    setattr(kernel, "_debug_config_done", True)


def continue_debugger(kernel, stopped: dict) -> None:
    body = stopped.get("content", {}).get("body", {})
    thread_id = body.get("threadId")
    if isinstance(thread_id, int): debug_continue(kernel, thread_id)
    else: debug_continue(kernel)


@pytest.fixture()
def kernel():
    with new_kernel() as kc: yield kc


@pytest.fixture()
def debug_kernel(kernel):
    reply = wait_for_debug_request(kernel, "initialize", DEBUG_INIT_ARGS)
    assert reply.get("success"), f"initialize failed: {reply}"
    reply = wait_for_debug_request(kernel, "attach")
    assert reply.get("success"), f"attach failed: {reply}"
    try: yield kernel
    finally: wait_for_debug_request(kernel, "disconnect", restart=False, terminateDebuggee=True)


def test_debugger_basic_features(debug_kernel):
    debug_kernel.kernel_info()
    reply = debug_kernel.get_shell_msg(timeout=TIMEOUT)
    features = reply["content"].get("supported_features", [])
    assert "debugger" in features, f"supported_features: {features}"

    reply = wait_for_debug_request(debug_kernel, "evaluate", expression="'a' + 'b'", context="repl")
    assert reply.get("success"), f"evaluate failed: {reply}"
    assert reply["body"]["result"] == "", f"evaluate result: {reply['body']['result']}"

    var_name = "text"
    value = "Hello the world"
    code = f"{var_name}='{value}'\nprint({var_name})\n"
    debug_kernel.execute(code)
    debug_kernel.get_shell_msg(timeout=TIMEOUT)
    wait_for_debug_request(debug_kernel, "inspectVariables")
    wait_for_debug_request(debug_kernel, "richInspectVariables", variableName=var_name)


def test_debugger_breakpoints_and_steps(debug_kernel):
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
    stepped = wait_for_debug_request(debug_kernel, "stepIn", threadId=thread_id)
    assert stepped.get("success"), f"stepIn failed: {stepped}"
    stopped = wait_for_stop(debug_kernel)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = get_stack_frames(debug_kernel, thread_id)
    assert frames and frames[0]["name"] == "f", f"frames: {frames}"

    reply = wait_for_debug_request(debug_kernel, "next", threadId=thread_id)
    assert reply.get("success"), f"next failed: {reply}"
    stopped = wait_for_stop(debug_kernel)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = get_stack_frames(debug_kernel, thread_id)
    frame_id = frames[0]["id"]
    scopes = get_scopes(debug_kernel, frame_id)
    locals_ = get_scope_vars(debug_kernel, scopes, "Locals")
    local_names = [v["name"] for v in locals_]
    assert "a" in local_names and "b" in local_names, f"locals: {locals_}"

    reply = wait_for_debug_request(debug_kernel, "richInspectVariables", variableName=locals_[0]["name"], frameId=frame_id)
    assert reply.get("success"), f"richInspectVariables failed: {reply}"

    reply = wait_for_debug_request(debug_kernel, "copyToGlobals", srcVariableName="c", dstVariableName="c_copy",
        srcFrameId=frame_id)
    assert reply.get("success"), f"copyToGlobals failed: {reply}"
    globals_ = get_scope_vars(debug_kernel, scopes, "Globals")
    assert any(v for v in globals_ if v["name"] == "c_copy"), f"globals: {globals_}"

    locals_ref = get_scope_ref(scopes, "Locals")
    globals_ref = get_scope_ref(scopes, "Globals")
    msgs = [prepare_debug_request(debug_kernel, "variables", variablesReference=locals_ref),
        prepare_debug_request(debug_kernel, "variables", variablesReference=globals_ref)]
    for msg in msgs: debug_kernel.control_channel.send(msg)
    replies = get_replies(debug_kernel, [msg["header"]["msg_id"] for msg in msgs])
    locals_reply = replies[0]["content"]
    globals_reply = replies[1]["content"]
    assert locals_reply["success"], f"locals reply: {locals_reply}"
    assert globals_reply["success"], f"globals reply: {globals_reply}"
    reply = wait_for_debug_request(debug_kernel, "stepOut", threadId=thread_id)
    assert reply.get("success"), f"stepOut failed: {reply}"
    stopped = wait_for_stop(debug_kernel)
    thread_id = stopped["content"]["body"].get("threadId", thread_id)
    frames = get_stack_frames(debug_kernel, thread_id)
    assert frames and frames[0]["name"] != "f", f"frames: {frames}"

    continue_debugger(debug_kernel, stopped)


def test_debugger_exceptions_and_terminate(debug_kernel):
    reply = wait_for_debug_request(debug_kernel, "setExceptionBreakpoints", filters=["raised"])
    assert reply["success"], f"setExceptionBreakpoints failed: {reply}"
    ensure_configuration_done(debug_kernel)
    msg_id = debug_kernel.execute("raise ValueError('boom')")
    stopped = wait_for_stop(debug_kernel)
    reason = stopped["content"]["body"].get("reason")
    assert reason in {"exception", "breakpoint", "pause"}, f"stopped: {stopped}"
    continue_debugger(debug_kernel, stopped)
    reply = get_shell_reply(debug_kernel, msg_id)
    assert reply["content"]["status"] == "error", f"execute reply: {reply.get('content')}"

    reply = wait_for_debug_request(debug_kernel, "terminate", restart=False)
    assert reply["success"], f"terminate failed: {reply}"
    info = debug_info(debug_kernel)
    assert info["body"]["breakpoints"] == [], f"breakpoints not cleared: {info['body']['breakpoints']}"
    assert info["body"]["stoppedThreads"] == [], f"stoppedThreads not cleared: {info['body']['stoppedThreads']}"
