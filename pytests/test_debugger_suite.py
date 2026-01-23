import time, pytest
from contextlib import contextmanager
from .kernel_utils import (DEBUG_INIT_ARGS, debug_configuration_done, debug_continue, debug_dump_cell,
    debug_info, debug_request, debug_set_breakpoints, start_kernel, wait_for_stop)

TIMEOUT = 10


@contextmanager
def new_kernel():
    with start_kernel() as (_km, kc): yield kc


def prepare_debug_request(kernel, command, arguments=None):
    seq = getattr(kernel, "_debug_seq", 1)
    setattr(kernel, "_debug_seq", seq + 1)
    msg = kernel.session.msg(
        "debug_request",
        dict(type="request", seq=seq, command=command, arguments=arguments or {}),
    )
    return msg


def wait_for_debug_request(kernel, command, arguments=None, full_reply=False):
    return debug_request(kernel, command, arguments, full_reply=full_reply)


def get_stack_frames(kernel, thread_id):
    return wait_for_debug_request(kernel, "stackTrace", dict(threadId=thread_id))["body"]["stackFrames"]

def get_scopes(kernel, frame_id): return wait_for_debug_request(
    kernel, "scopes", dict(frameId=frame_id))["body"]["scopes"]

def get_scope_ref(scopes, name): return next(s for s in scopes if s["name"] == name)["variablesReference"]

def get_scope_vars(kernel, scopes, name):
    ref = get_scope_ref(scopes, name)
    return wait_for_debug_request(kernel, "variables", dict(variablesReference=ref))["body"]["variables"]


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
    debug_configuration_done(kernel)
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
def kernel_with_debug(kernel):
    wait_for_debug_request(kernel, "initialize", DEBUG_INIT_ARGS)
    wait_for_debug_request(kernel, "attach")
    try: yield kernel
    finally: wait_for_debug_request(kernel, "disconnect", {"restart": False, "terminateDebuggee": True})


def test_debug_initialize(kernel):
    reply = wait_for_debug_request(kernel, "initialize", DEBUG_INIT_ARGS)
    assert reply.get("success")


def test_supported_features(kernel_with_debug):
    kernel_with_debug.kernel_info()
    reply = kernel_with_debug.get_shell_msg(timeout=TIMEOUT)
    features = reply["content"].get("supported_features", [])
    assert "debugger" in features


def test_attach_debug(kernel_with_debug):
    ensure_configuration_done(kernel_with_debug)
    reply = wait_for_debug_request(
        kernel_with_debug, "evaluate", {"expression": "'a' + 'b'", "context": "repl"}
    )
    assert reply["success"]
    assert reply["body"]["result"] == ""


def test_set_breakpoints(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    source = debug_dump_cell(kernel_with_debug, code)["body"]["sourcePath"]
    reply = debug_set_breakpoints(kernel_with_debug, source, 2)
    assert reply["success"]
    assert len(reply["body"]["breakpoints"]) == 1
    assert reply["body"]["breakpoints"][0]["verified"]
    assert reply["body"]["breakpoints"][0]["source"]["path"] == source
    info = debug_info(kernel_with_debug)
    assert source in [b["source"] for b in info["body"]["breakpoints"]]
    done = debug_configuration_done(kernel_with_debug)
    setattr(kernel_with_debug, "_debug_config_done", True)
    assert done["success"]


def test_stop_on_breakpoint(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    source = debug_dump_cell(kernel_with_debug, code)["body"]["sourcePath"]
    debug_info(kernel_with_debug)
    debug_set_breakpoints(kernel_with_debug, source, 2)
    debug_configuration_done(kernel_with_debug, full_reply=True)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)
    stopped = wait_for_stop(kernel_with_debug)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    continue_debugger(kernel_with_debug, stopped)


def test_breakpoint_in_cell_with_leading_empty_lines(kernel_with_debug):
    code = """
def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    source = debug_dump_cell(kernel_with_debug, code)["body"]["sourcePath"]
    debug_info(kernel_with_debug)
    debug_set_breakpoints(kernel_with_debug, source, 6)
    debug_configuration_done(kernel_with_debug, full_reply=True)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)
    stopped = wait_for_stop(kernel_with_debug)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    continue_debugger(kernel_with_debug, stopped)


def test_rich_inspect_not_at_breakpoint(kernel_with_debug):
    var_name = "text"
    value = "Hello the world"
    code = f"{var_name}='{value}'\nprint({var_name})\n"
    kernel_with_debug.execute(code)
    kernel_with_debug.get_shell_msg(timeout=TIMEOUT)
    r = wait_for_debug_request(kernel_with_debug, "inspectVariables")
    assert var_name in [v["name"] for v in r["body"]["variables"]]
    reply = wait_for_debug_request(kernel_with_debug, "richInspectVariables", {"variableName": var_name})
    assert reply["body"]["data"] == {"text/plain": f"'{value}'"}


def test_rich_inspect_at_breakpoint(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    source = debug_dump_cell(kernel_with_debug, code)["body"]["sourcePath"]
    debug_set_breakpoints(kernel_with_debug, source, 2)
    debug_info(kernel_with_debug)
    debug_configuration_done(kernel_with_debug)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)
    stopped = wait_for_stop(kernel_with_debug)
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stacks = get_stack_frames(kernel_with_debug, thread_id)
    scopes = get_scopes(kernel_with_debug, stacks[0]["id"])
    locals_ = get_scope_vars(kernel_with_debug, scopes, "Locals")
    reply = wait_for_debug_request(
        kernel_with_debug,
        "richInspectVariables",
        {"variableName": locals_[0]["name"], "frameId": stacks[0]["id"]},
    )
    assert reply["body"]["data"] == {"text/plain": locals_[0]["value"]}
    continue_debugger(kernel_with_debug, stopped)


def test_copy_to_globals(kernel_with_debug):
    local_var_name = "var"
    global_var_name = "var_copy"
    code = f"""from IPython.core.display import HTML
def my_test():
    {local_var_name} = HTML('<p>test content</p>')
    pass
a = 2
my_test()"""
    source = debug_dump_cell(kernel_with_debug, code)["body"]["sourcePath"]
    debug_set_breakpoints(kernel_with_debug, source, 4)
    debug_info(kernel_with_debug)
    debug_configuration_done(kernel_with_debug)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)
    stopped = wait_for_stop(kernel_with_debug)
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stacks = get_stack_frames(kernel_with_debug, thread_id)
    frame_id = stacks[0]["id"]
    wait_for_debug_request(
        kernel_with_debug,
        "copyToGlobals",
        dict(srcVariableName=local_var_name, dstVariableName=global_var_name, srcFrameId=frame_id),
    )
    scopes = get_scopes(kernel_with_debug, frame_id)
    locals_ = get_scope_vars(kernel_with_debug, scopes, "Locals")
    local_var = next(v for v in locals_ if local_var_name in v["evaluateName"])
    globals_ = get_scope_vars(kernel_with_debug, scopes, "Globals")
    global_var = next(v for v in globals_ if global_var_name in v["evaluateName"])
    assert global_var["value"] == local_var["value"] and global_var["type"] == local_var["type"]
    continue_debugger(kernel_with_debug, stopped)


def test_debug_requests_sequential(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    source = debug_dump_cell(kernel_with_debug, code)["body"]["sourcePath"]
    debug_set_breakpoints(kernel_with_debug, source, 2)
    debug_info(kernel_with_debug)
    debug_configuration_done(kernel_with_debug)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)
    stopped = wait_for_stop(kernel_with_debug)
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stacks = get_stack_frames(kernel_with_debug, thread_id)
    scopes = get_scopes(kernel_with_debug, stacks[0]["id"])
    locals_ref = get_scope_ref(scopes, "Locals")
    globals_ref = get_scope_ref(scopes, "Globals")
    msgs = [
        prepare_debug_request(kernel_with_debug, "variables", {"variablesReference": locals_ref}),
        prepare_debug_request(kernel_with_debug, "variables", {"variablesReference": globals_ref}),
    ]
    for msg in msgs: kernel_with_debug.control_channel.send(msg)

    replies = get_replies(kernel_with_debug, [msg["header"]["msg_id"] for msg in msgs])
    locals_reply = replies[0]["content"]
    globals_reply = replies[1]["content"]
    assert locals_reply["success"]
    vars_ = locals_reply["body"]["variables"]
    assert next(v for v in vars_ if v["name"] == "a")["value"] == "2"
    assert next(v for v in vars_ if v["name"] == "b")["value"] == "3"
    assert globals_reply["success"]
    names = [v["name"] for v in globals_reply["body"]["variables"]]
    assert "function variables" in names
    assert "special variables" in names

    execution_states = []
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and len(execution_states) < 4:
        msg = kernel_with_debug.get_iopub_msg(timeout=TIMEOUT)
        if msg["msg_type"] == "status": execution_states.append(msg["content"]["execution_state"])
    assert execution_states.count("busy") == 2
    assert execution_states.count("idle") == 2
    continue_debugger(kernel_with_debug, stopped)
