import time
from queue import Empty
from contextlib import contextmanager

import pytest

from .kernel_utils import DEBUG_INIT_ARGS, start_kernel

TIMEOUT = 10

try:
    import debugpy  # noqa: F401
except Exception:
    debugpy = None


@contextmanager
def new_kernel():
    with start_kernel() as (_km, kc):
        yield kc


def prepare_debug_request(kernel, command, arguments=None):
    seq = getattr(kernel, "_debug_seq", 1)
    setattr(kernel, "_debug_seq", seq + 1)
    msg = kernel.session.msg(
        "debug_request",
        {
            "type": "request",
            "seq": seq,
            "command": command,
            "arguments": arguments or {},
        },
    )
    return msg


def get_control_reply(kernel, msg_id):
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        reply = kernel.control_channel.get_msg(timeout=TIMEOUT)
        if reply["parent_header"].get("msg_id") == msg_id:
            return reply
    raise AssertionError("timeout waiting for control reply")


def wait_for_debug_request(kernel, command, arguments=None, full_reply=False):
    msg = prepare_debug_request(kernel, command, arguments)
    kernel.control_channel.send(msg)
    reply = get_control_reply(kernel, msg["header"]["msg_id"])
    return reply if full_reply else reply["content"]


def get_replies(kernel, msg_ids):
    replies = {msg_id: None for msg_id in msg_ids}
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and any(v is None for v in replies.values()):
        reply = kernel.control_channel.get_msg(timeout=TIMEOUT)
        msg_id = reply["parent_header"].get("msg_id")
        if msg_id in replies:
            replies[msg_id] = reply
    if any(v is None for v in replies.values()):
        raise AssertionError("timeout waiting for debug replies")
    return [replies[msg_id] for msg_id in msg_ids]


def ensure_configuration_done(kernel) -> None:
    if getattr(kernel, "_debug_config_done", False):
        return
    wait_for_debug_request(kernel, "configurationDone")
    setattr(kernel, "_debug_config_done", True)


def continue_debugger(kernel, stopped: dict) -> None:
    body = stopped.get("content", {}).get("body", {})
    thread_id = body.get("threadId")
    if isinstance(thread_id, int):
        wait_for_debug_request(kernel, "continue", {"threadId": thread_id})
    else:
        wait_for_debug_request(kernel, "continue", {})


def wait_for_stopped_event(kernel):
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        try:
            msg = kernel.get_iopub_msg(timeout=0.5)
        except Empty:
            continue
        if msg.get("msg_type") == "debug_event" and msg["content"].get("event") == "stopped":
            return msg
    deadline = time.time() + TIMEOUT
    last = None
    while time.time() < deadline:
        reply = wait_for_debug_request(kernel, "stackTrace", {"threadId": 1})
        if reply.get("success"):
            return {"content": {"body": {"reason": "breakpoint", "threadId": 1}}}
        last = reply
        time.sleep(0.1)
    raise AssertionError(f"stopped debug_event not received: {last}")


@pytest.fixture()
def kernel():
    with new_kernel() as kc:
        yield kc


@pytest.fixture()
def kernel_with_debug(kernel):
    wait_for_debug_request(
        kernel,
        "initialize",
        DEBUG_INIT_ARGS,
    )
    wait_for_debug_request(kernel, "attach")
    try:
        yield kernel
    finally:
        wait_for_debug_request(kernel, "disconnect", {"restart": False, "terminateDebuggee": True})


def test_debug_initialize(kernel):
    reply = wait_for_debug_request(
        kernel,
        "initialize",
        DEBUG_INIT_ARGS,
    )
    if debugpy:
        assert reply.get("success")
    else:
        assert reply == {}


def test_supported_features(kernel_with_debug):
    kernel_with_debug.kernel_info()
    reply = kernel_with_debug.get_shell_msg(timeout=TIMEOUT)
    features = reply["content"].get("supported_features", [])
    if debugpy:
        assert "debugger" in features
    else:
        assert "debugger" not in features


def test_attach_debug(kernel_with_debug):
    ensure_configuration_done(kernel_with_debug)
    reply = wait_for_debug_request(
        kernel_with_debug, "evaluate", {"expression": "'a' + 'b'", "context": "repl"}
    )
    if debugpy:
        assert reply["success"]
        assert reply["body"]["result"] == ""
    else:
        assert reply == {}


def test_set_breakpoints(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""

    r = wait_for_debug_request(kernel_with_debug, "dumpCell", {"code": code})
    if debugpy:
        source = r["body"]["sourcePath"]
    else:
        assert r == {}
        source = "non-existent path"

    reply = wait_for_debug_request(
        kernel_with_debug,
        "setBreakpoints",
        {"breakpoints": [{"line": 2}], "source": {"path": source}, "sourceModified": False},
    )
    if debugpy:
        assert reply["success"]
        assert len(reply["body"]["breakpoints"]) == 1
        assert reply["body"]["breakpoints"][0]["verified"]
        assert reply["body"]["breakpoints"][0]["source"]["path"] == source
    else:
        assert reply == {}

    info = wait_for_debug_request(kernel_with_debug, "debugInfo")
    if debugpy:
        assert source in [b["source"] for b in info["body"]["breakpoints"]]
    else:
        assert info == {}

    done = wait_for_debug_request(kernel_with_debug, "configurationDone")
    setattr(kernel_with_debug, "_debug_config_done", True)
    if debugpy:
        assert done["success"]
    else:
        assert done == {}


def test_stop_on_breakpoint(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""

    r = wait_for_debug_request(kernel_with_debug, "dumpCell", {"code": code})
    if debugpy:
        source = r["body"]["sourcePath"]
    else:
        assert r == {}
        source = "some path"

    wait_for_debug_request(kernel_with_debug, "debugInfo")
    wait_for_debug_request(
        kernel_with_debug,
        "setBreakpoints",
        {"breakpoints": [{"line": 2}], "source": {"path": source}, "sourceModified": False},
    )
    wait_for_debug_request(kernel_with_debug, "configurationDone", full_reply=True)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)

    if not debugpy:
        return

    stopped = wait_for_stopped_event(kernel_with_debug)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    continue_debugger(kernel_with_debug, stopped)


def test_breakpoint_in_cell_with_leading_empty_lines(kernel_with_debug):
    code = """
def f(a, b):
    c = a + b
    return c

f(2, 3)"""

    r = wait_for_debug_request(kernel_with_debug, "dumpCell", {"code": code})
    if debugpy:
        source = r["body"]["sourcePath"]
    else:
        assert r == {}
        source = "some path"

    wait_for_debug_request(kernel_with_debug, "debugInfo")
    wait_for_debug_request(
        kernel_with_debug,
        "setBreakpoints",
        {"breakpoints": [{"line": 6}], "source": {"path": source}, "sourceModified": False},
    )
    wait_for_debug_request(kernel_with_debug, "configurationDone", full_reply=True)
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)

    if not debugpy:
        return

    stopped = wait_for_stopped_event(kernel_with_debug)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    continue_debugger(kernel_with_debug, stopped)


def test_rich_inspect_not_at_breakpoint(kernel_with_debug):
    var_name = "text"
    value = "Hello the world"
    code = f"{var_name}='{value}'\nprint({var_name})\n"

    msg_id = kernel_with_debug.execute(code)
    kernel_with_debug.get_shell_msg(timeout=TIMEOUT)

    r = wait_for_debug_request(kernel_with_debug, "inspectVariables")
    if debugpy:
        assert var_name in [v["name"] for v in r["body"]["variables"]]
    else:
        assert r == {}

    reply = wait_for_debug_request(kernel_with_debug, "richInspectVariables", {"variableName": var_name})
    if debugpy:
        assert reply["body"]["data"] == {"text/plain": f"'{value}'"}
    else:
        assert reply == {}


def test_rich_inspect_at_breakpoint(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""

    r = wait_for_debug_request(kernel_with_debug, "dumpCell", {"code": code})
    if debugpy:
        source = r["body"]["sourcePath"]
    else:
        assert r == {}
        source = "some path"

    wait_for_debug_request(
        kernel_with_debug,
        "setBreakpoints",
        {"breakpoints": [{"line": 2}], "source": {"path": source}, "sourceModified": False},
    )
    wait_for_debug_request(kernel_with_debug, "debugInfo")
    wait_for_debug_request(kernel_with_debug, "configurationDone")
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)

    if not debugpy:
        return

    stopped = wait_for_stopped_event(kernel_with_debug)
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stacks = wait_for_debug_request(kernel_with_debug, "stackTrace", {"threadId": thread_id})["body"][
        "stackFrames"
    ]
    scopes = wait_for_debug_request(kernel_with_debug, "scopes", {"frameId": stacks[0]["id"]})["body"][
        "scopes"
    ]
    locals_ = wait_for_debug_request(
        kernel_with_debug,
        "variables",
        {"variablesReference": next(s for s in scopes if s["name"] == "Locals")["variablesReference"]},
    )["body"]["variables"]

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

    r = wait_for_debug_request(kernel_with_debug, "dumpCell", {"code": code})
    if debugpy:
        source = r["body"]["sourcePath"]
    else:
        assert r == {}
        source = "some path"

    wait_for_debug_request(
        kernel_with_debug,
        "setBreakpoints",
        {"breakpoints": [{"line": 4}], "source": {"path": source}, "sourceModified": False},
    )
    wait_for_debug_request(kernel_with_debug, "debugInfo")
    wait_for_debug_request(kernel_with_debug, "configurationDone")
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)

    if not debugpy:
        return

    stopped = wait_for_stopped_event(kernel_with_debug)
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stacks = wait_for_debug_request(kernel_with_debug, "stackTrace", {"threadId": thread_id})["body"][
        "stackFrames"
    ]
    frame_id = stacks[0]["id"]
    wait_for_debug_request(
        kernel_with_debug,
        "copyToGlobals",
        {"srcVariableName": local_var_name, "dstVariableName": global_var_name, "srcFrameId": frame_id},
    )
    scopes = wait_for_debug_request(kernel_with_debug, "scopes", {"frameId": frame_id})["body"]["scopes"]
    locals_ = wait_for_debug_request(
        kernel_with_debug,
        "variables",
        {"variablesReference": next(s for s in scopes if s["name"] == "Locals")["variablesReference"]},
    )["body"]["variables"]
    local_var = next(v for v in locals_ if local_var_name in v["evaluateName"])
    globals_ = wait_for_debug_request(
        kernel_with_debug,
        "variables",
        {"variablesReference": next(s for s in scopes if s["name"] == "Globals")["variablesReference"]},
    )["body"]["variables"]
    global_var = next(v for v in globals_ if global_var_name in v["evaluateName"])
    assert global_var["value"] == local_var["value"] and global_var["type"] == local_var["type"]
    continue_debugger(kernel_with_debug, stopped)


def test_debug_requests_sequential(kernel_with_debug):
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""

    r = wait_for_debug_request(kernel_with_debug, "dumpCell", {"code": code})
    if debugpy:
        source = r["body"]["sourcePath"]
    else:
        assert r == {}
        source = "some path"

    wait_for_debug_request(
        kernel_with_debug,
        "setBreakpoints",
        {"breakpoints": [{"line": 2}], "source": {"path": source}, "sourceModified": False},
    )
    wait_for_debug_request(kernel_with_debug, "debugInfo")
    wait_for_debug_request(kernel_with_debug, "configurationDone")
    setattr(kernel_with_debug, "_debug_config_done", True)
    kernel_with_debug.execute(code)

    if not debugpy:
        return

    stopped = wait_for_stopped_event(kernel_with_debug)
    thread_id = stopped["content"]["body"].get("threadId", 1)
    stacks = wait_for_debug_request(kernel_with_debug, "stackTrace", {"threadId": thread_id})["body"][
        "stackFrames"
    ]
    scopes = wait_for_debug_request(kernel_with_debug, "scopes", {"frameId": stacks[0]["id"]})["body"][
        "scopes"
    ]
    locals_ref = next(s for s in scopes if s["name"] == "Locals")["variablesReference"]
    globals_ref = next(s for s in scopes if s["name"] == "Globals")["variablesReference"]

    msgs = [
        prepare_debug_request(kernel_with_debug, "variables", {"variablesReference": locals_ref}),
        prepare_debug_request(kernel_with_debug, "variables", {"variablesReference": globals_ref}),
    ]
    for msg in msgs:
        kernel_with_debug.control_channel.send(msg)

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
        if msg["msg_type"] == "status":
            execution_states.append(msg["content"]["execution_state"])
    assert execution_states.count("busy") == 2
    assert execution_states.count("idle") == 2
    continue_debugger(kernel_with_debug, stopped)
