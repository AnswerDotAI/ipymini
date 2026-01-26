import pytest
from jupyter_client import KernelManager
from .kernel_utils import *


def _reset_kernel(kc):
    msg_id = kc.execute("get_ipython().run_line_magic('reset', '-f')", silent=True, store_history=False)
    kc.shell_reply(msg_id)
    kc.iopub_drain(msg_id)


class E2EKernel:
    def __init__(self, km: KernelManager):
        self.km = km
        self.kc = None
        self._debug_initialized = False
        self._debug_config_done = False

    def reset_client(self):
        if self.kc is not None: self.kc.stop_channels()
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=default_timeout)

    def restart(self):
        self.km.restart_kernel(now=True)
        self._debug_initialized = False
        self._debug_config_done = False
        self.reset_client()

    def ensure_debug(self):
        if self._debug_initialized: return
        reply = self.kc.dap.initialize(**debug_init_args)
        assert reply.get("success")
        attach = self.kc.dap.attach()
        if attach and attach.get("success") is False:
            message = attach.get("message", "")
            assert "already attached" in message or "already initialized" in message
        self._debug_initialized = True

    def debug_config_done(self):
        if self._debug_config_done: return
        self.kc.dap.configurationDone()
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


def test_e2e_restart_and_debug(kernel, e2e_kernel):
    kc = e2e_kernel.kc
    _, reply, outputs = kc.exec_drain("1+2+3", store_history=False)
    assert reply["content"]["status"] == "ok"
    results = iopub_msgs(outputs, "execute_result")
    assert results, "expected execute_result"

    e2e_kernel.restart()
    kc = e2e_kernel.kc

    _, reply, outputs = kc.exec_drain("try:\n    x\nexcept NameError:\n    print('missing')", store_history=False)
    assert reply["content"]["status"] == "ok"
    streams = iopub_streams(outputs)
    assert any("missing" in m["content"].get("text", "") for m in streams)

    kernel.ensure_debug()
    kernel.debug_config_done()
    reply = kc.dap.evaluate(expression="'a' + 'b'", context="repl")
    assert reply.get("success"), f"evaluate: {reply}"

    code = """def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    r = kc.dap.dumpCell(code=code)
    source = r["body"]["sourcePath"]
    kc.dap.setBreakpoints(breakpoints=[dict(line=2)], source=dict(path=source), sourceModified=False)
    kc.dap.debugInfo()
    kernel.debug_config_done()
    msg_id = kc.execute(code)
    stopped = wait_for_stop(kc, timeout=default_timeout)
    assert stopped["content"]["body"]["reason"] == "breakpoint", f"stopped: {stopped}"
    thread_id = stopped["content"]["body"]["threadId"]
    kc.dap.continue_(threadId=thread_id)
    kc.shell_reply(msg_id)
    kc.iopub_drain(msg_id)

    code = """
def f(a, b):
    c = a + b
    return c

f(2, 3)"""
    r = kc.dap.dumpCell(code=code)
    source = r["body"]["sourcePath"]
    kc.dap.setBreakpoints(breakpoints=[dict(line=6)], source=dict(path=source), sourceModified=False)
    kc.dap.debugInfo()
    kernel.debug_config_done()
    msg_id = kc.execute(code)
    stopped = wait_for_stop(kc, timeout=default_timeout)
    assert stopped["content"]["body"]["reason"] == "breakpoint", f"stopped: {stopped}"
    thread_id = stopped["content"]["body"]["threadId"]
    kc.dap.continue_(threadId=thread_id)
    kc.shell_reply(msg_id)
    kc.iopub_drain(msg_id)
