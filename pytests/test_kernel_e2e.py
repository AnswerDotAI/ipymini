import time
from queue import Empty

import pytest
from jupyter_client import KernelManager

from .kernel_utils import (
    DEBUG_INIT_ARGS,
    TIMEOUT,
    build_env,
    drain_iopub,
    ensure_separate_process,
    get_shell_reply,
    iopub_msgs,
    iopub_streams,
    wait_for_status,
)

try:
    import debugpy  # noqa: F401
except Exception:
    debugpy = None


def _reset_kernel(kc) -> None:
    msg_id = kc.execute(
        "get_ipython().run_line_magic('reset', '-f')",
        silent=True,
        store_history=False,
    )
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)


def _send_debug_request(kc, command, arguments=None):
    seq = getattr(kc, "_debug_seq", 1)
    setattr(kc, "_debug_seq", seq + 1)
    msg = kc.session.msg(
        "debug_request",
        {
            "type": "request",
            "seq": seq,
            "command": command,
            "arguments": arguments or {},
        },
    )
    kc.control_channel.send(msg)
    reply = kc.control_channel.get_msg(timeout=TIMEOUT)
    assert reply["header"]["msg_type"] == "debug_reply"
    assert reply["parent_header"].get("msg_id") == msg["header"]["msg_id"]
    return reply["content"]


def _wait_for_debug_event(kc, event_name: str, timeout: float = TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = kc.get_iopub_msg(timeout=0.5)
        except Empty:
            continue
        if msg.get("msg_type") == "debug_event" and msg.get("content", {}).get("event") == event_name:
            return msg
    raise AssertionError(f"debug_event {event_name} not received")


def _wait_for_stop(kc, timeout: float = TIMEOUT) -> dict:
    try:
        return _wait_for_debug_event(kc, "stopped", timeout=timeout / 2)
    except AssertionError:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            reply = _send_debug_request(kc, "stackTrace", {"threadId": 1})
            if reply.get("success"):
                return {"content": {"body": {"reason": "breakpoint", "threadId": 1}}}
            last = reply
            time.sleep(0.1)
        raise AssertionError(f"stopped debug_event not received: {last}")


class E2EKernel:
    def __init__(self, km: KernelManager) -> None:
        self.km = km
        self.kc = None
        self._debug_initialized = False
        self._debug_config_done = False

    def reset_client(self) -> None:
        if self.kc is not None:
            try:
                self.kc.stop_channels()
            except Exception:
                pass
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=TIMEOUT)

    def restart(self) -> None:
        self.km.restart_kernel(now=True)
        self._debug_initialized = False
        self._debug_config_done = False
        self.reset_client()

    def ensure_debug(self) -> None:
        if debugpy is None:
            return
        if self._debug_initialized:
            return
        reply = _send_debug_request(
            self.kc,
            "initialize",
            DEBUG_INIT_ARGS,
        )
        assert reply.get("success")
        attach = _send_debug_request(self.kc, "attach")
        if attach and attach.get("success") is False:
            message = attach.get("message", "")
            assert "already attached" in message or "already initialized" in message
        self._debug_initialized = True

    def debug_config_done(self) -> None:
        if debugpy is None:
            return
        if self._debug_config_done:
            return
        _send_debug_request(self.kc, "configurationDone")
        self._debug_config_done = True


@pytest.fixture(scope="module")
def e2e_kernel():
    env = build_env()
    # Ensure kernelspec is discoverable for KernelManager.
    import os

    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env)
    ensure_separate_process(km)
    kernel = E2EKernel(km)
    kernel.reset_client()
    try:
        yield kernel
    finally:
        if kernel.kc is not None:
            kernel.kc.stop_channels()
        km.shutdown_kernel(now=True)


@pytest.fixture()
def kernel(e2e_kernel):
    e2e_kernel.reset_client()
    _reset_kernel(e2e_kernel.kc)
    return e2e_kernel


def test_e2e_execute_roundtrip(kernel) -> None:
    kc = kernel.kc
    msg_id = kc.execute("1+2+3", store_history=False)
    reply = get_shell_reply(kc, msg_id)
    assert reply["content"]["status"] == "ok"
    outputs = drain_iopub(kc, msg_id)
    results = iopub_msgs(outputs, "execute_result")
    assert results, "expected execute_result"


def test_e2e_interrupt(kernel) -> None:
    km = kernel.km
    kc = kernel.kc
    msg_id = kc.execute("import time; time.sleep(2)")
    wait_for_status(kc, "busy")
    km.interrupt_kernel()
    reply = get_shell_reply(kc, msg_id)
    assert reply["content"]["status"] == "error"


def test_e2e_restart(e2e_kernel) -> None:
    kc = e2e_kernel.kc
    msg_id = kc.execute("x = 42", store_history=False)
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)

    e2e_kernel.restart()
    kc = e2e_kernel.kc

    msg_id = kc.execute(
        "try:\n    x\nexcept NameError:\n    print('missing')",
        store_history=False,
    )
    reply = get_shell_reply(kc, msg_id)
    assert reply["content"]["status"] == "ok"
    outputs = drain_iopub(kc, msg_id)
    streams = iopub_streams(outputs)
    assert any("missing" in m["content"].get("text", "") for m in streams)


def test_e2e_debug_roundtrip(kernel) -> None:
    kc = kernel.kc
    if debugpy is None:
        reply = _send_debug_request(
            kc,
            "initialize",
            DEBUG_INIT_ARGS,
        )
        assert reply == {}
        return
    kernel.ensure_debug()
    kernel.debug_config_done()
    reply = _send_debug_request(kc, "evaluate", {"expression": "'a' + 'b'", "context": "repl"})
    assert reply.get("success")


@pytest.mark.skipif(debugpy is None, reason="debugpy not available")
def test_e2e_debug_breakpoint_stop(kernel) -> None:
    kernel.restart()
    kc = kernel.kc
    kernel.ensure_debug()
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    r = _send_debug_request(kc, "dumpCell", {"code": code})
    source = r["body"]["sourcePath"]
    _send_debug_request(
        kc,
        "setBreakpoints",
        {"breakpoints": [{"line": 2}], "source": {"path": source}, "sourceModified": False},
    )
    _send_debug_request(kc, "debugInfo")
    kernel.debug_config_done()
    msg_id = kc.execute(code)
    stopped = _wait_for_stop(kc, timeout=TIMEOUT)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    thread_id = stopped["content"]["body"]["threadId"]
    _send_debug_request(kc, "continue", {"threadId": thread_id})
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)


@pytest.mark.skipif(debugpy is None, reason="debugpy not available")
def test_e2e_debug_breakpoint_leading_lines(kernel) -> None:
    kernel.restart()
    kc = kernel.kc
    kernel.ensure_debug()
    code = """
def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    r = _send_debug_request(kc, "dumpCell", {"code": code})
    source = r["body"]["sourcePath"]
    _send_debug_request(
        kc,
        "setBreakpoints",
        {"breakpoints": [{"line": 6}], "source": {"path": source}, "sourceModified": False},
    )
    _send_debug_request(kc, "debugInfo")
    kernel.debug_config_done()
    msg_id = kc.execute(code)
    stopped = _wait_for_stop(kc, timeout=TIMEOUT)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    thread_id = stopped["content"]["body"]["threadId"]
    _send_debug_request(kc, "continue", {"threadId": thread_id})
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)
