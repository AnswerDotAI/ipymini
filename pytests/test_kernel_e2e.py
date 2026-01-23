import pytest
from jupyter_client import KernelManager
from .kernel_utils import (DEBUG_INIT_ARGS, TIMEOUT, build_env, debug_configuration_done,
    debug_continue, debug_dump_cell, debug_info, debug_request, debug_set_breakpoints,
    execute_and_drain, drain_iopub, ensure_separate_process, get_shell_reply, iopub_msgs,
    iopub_streams, wait_for_status, wait_for_stop)


def _reset_kernel(kc) -> None:
    msg_id = kc.execute("get_ipython().run_line_magic('reset', '-f')", silent=True, store_history=False)
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)


class E2EKernel:
    def __init__(self, km: KernelManager) -> None:
        self.km = km
        self.kc = None
        self._debug_initialized = False
        self._debug_config_done = False

    def reset_client(self) -> None:
        if self.kc is not None:
            try: self.kc.stop_channels()
            except Exception: pass
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=TIMEOUT)

    def restart(self) -> None:
        self.km.restart_kernel(now=True)
        self._debug_initialized = False
        self._debug_config_done = False
        self.reset_client()

    def ensure_debug(self) -> None:
        if self._debug_initialized: return
        reply = debug_request(self.kc, "initialize", DEBUG_INIT_ARGS)
        assert reply.get("success")
        attach = debug_request(self.kc, "attach")
        if attach and attach.get("success") is False:
            message = attach.get("message", "")
            assert "already attached" in message or "already initialized" in message
        self._debug_initialized = True

    def debug_config_done(self) -> None:
        if self._debug_config_done: return
        debug_configuration_done(self.kc)
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
    try: yield kernel
    finally:
        if kernel.kc is not None: kernel.kc.stop_channels()
        km.shutdown_kernel(now=True)


@pytest.fixture()
def kernel(e2e_kernel):
    e2e_kernel.reset_client()
    _reset_kernel(e2e_kernel.kc)
    return e2e_kernel


def test_e2e_execute_roundtrip(kernel) -> None:
    kc = kernel.kc
    _, reply, outputs = execute_and_drain(kc, "1+2+3", store_history=False)
    assert reply["content"]["status"] == "ok"
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
    execute_and_drain(kc, "x = 42", store_history=False)

    e2e_kernel.restart()
    kc = e2e_kernel.kc

    _, reply, outputs = execute_and_drain(kc, "try:\n    x\nexcept NameError:\n    print('missing')", store_history=False)
    assert reply["content"]["status"] == "ok"
    streams = iopub_streams(outputs)
    assert any("missing" in m["content"].get("text", "") for m in streams)


def test_e2e_debug_roundtrip(kernel) -> None:
    kc = kernel.kc
    kernel.ensure_debug()
    kernel.debug_config_done()
    reply = debug_request(kc, "evaluate", expression="'a' + 'b'", context="repl")
    assert reply.get("success")

def test_e2e_debug_breakpoint_stop(kernel) -> None:
    kernel.restart()
    kc = kernel.kc
    kernel.ensure_debug()
    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    r = debug_dump_cell(kc, code)
    source = r["body"]["sourcePath"]
    debug_set_breakpoints(kc, source, 2)
    debug_info(kc)
    kernel.debug_config_done()
    msg_id = kc.execute(code)
    stopped = wait_for_stop(kc, timeout=TIMEOUT)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    thread_id = stopped["content"]["body"]["threadId"]
    debug_continue(kc, thread_id)
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)


def test_e2e_debug_breakpoint_leading_lines(kernel) -> None:
    kernel.restart()
    kc = kernel.kc
    kernel.ensure_debug()
    code = """
def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    r = debug_dump_cell(kc, code)
    source = r["body"]["sourcePath"]
    debug_set_breakpoints(kc, source, 6)
    debug_info(kc)
    kernel.debug_config_done()
    msg_id = kc.execute(code)
    stopped = wait_for_stop(kc, timeout=TIMEOUT)
    assert stopped["content"]["body"]["reason"] == "breakpoint"
    thread_id = stopped["content"]["body"]["threadId"]
    debug_continue(kc, thread_id)
    get_shell_reply(kc, msg_id)
    drain_iopub(kc, msg_id)
