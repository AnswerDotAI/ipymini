import json, os, time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from queue import Empty
from jupyter_client import AsyncKernelClient, KernelClient, KernelManager
from fastcore.basics import nested_idx, patch
from fastcore.meta import delegates
from conkernelclient.ops import parent_id, iter_timeout, iopub_msgs, iopub_streams  # importing is side-effect free (no Session patch)

default_timeout = 10
root = Path(__file__).resolve().parents[1]

__all__ = ["default_timeout", "root", "build_env", "load_connection", "kernel_pid", "assert_pid_gone",
    "ensure_separate_process", "start_kernel", "start_kernel_async", "wait_for_msg", "iter_timeout",
    "parent_id", "wait_for_status", "iopub_msgs", "iopub_streams"]


def _ensure_jupyter_path()->str:
    share = str(root / "share" / "jupyter")
    current = os.environ.get("JUPYTER_PATH", "")
    return f"{share}{os.pathsep}{current}" if current else share


def _build_env()->dict:
    current = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{root}{os.pathsep}{current}" if current else str(root)
    return dict(os.environ) | dict(PYTHONPATH=pythonpath, JUPYTER_PATH=_ensure_jupyter_path())


def build_env(extra_env: dict|None=None)->dict:
    env = _build_env()
    if extra_env: env = {**env, **extra_env}
    return env



def wait_for_msg(get_msg, match, timeout:float|None=None, poll:float = 0.5, err:str = "timeout"):
    "Call `get_msg(timeout=...)` until `match(msg)` is true, else raise AssertionError on timeout."
    for rem in iter_timeout(timeout):
        try: msg = get_msg(timeout=min(poll, rem))
        except Empty: continue
        if match(msg): return msg
    raise AssertionError(err)


def load_connection(km)->dict:
    with open(km.connection_file, encoding="utf-8") as f: return json.load(f)


def kernel_pid(km)->int|None:
    "Return the kernel process id when the provisioner exposes it."
    provisioner = getattr(km, "provisioner", None)
    pid = getattr(provisioner, "pid", None)
    if pid is not None: return pid
    proc = getattr(provisioner, "process", None)
    return getattr(proc, "pid", None) if proc is not None else None


def assert_pid_gone(pid:int, timeout:float = 5):
    "Assert process `pid` no longer exists."
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try: os.kill(pid, 0)
        except ProcessLookupError: return
        time.sleep(0.05)
    raise AssertionError(f"kernel pid still alive: {pid}")


def ensure_separate_process(km: KernelManager):
    "Ensure separate process."
    pid = kernel_pid(km)
    if pid is None or pid == os.getpid(): raise RuntimeError("kernel must run in a separate process")




@delegates(KernelManager.start_kernel, but="env")
@contextmanager
def start_kernel(extra_env: dict|None=None, ready_timeout: float|None=None, **kwargs):
    "Start kernel."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env, **kwargs)
    ensure_separate_process(km)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=ready_timeout or default_timeout)
    try: yield km, kc
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


@asynccontextmanager
async def start_kernel_async(extra_env: dict|None=None, ready_timeout: float|None=None, **kwargs):
    "Async context manager for AsyncKernelClient tests."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env, **kwargs)
    ensure_separate_process(km)
    kc = AsyncKernelClient(**km.get_connection_info(session=True))
    kc.parent = km
    kc.start_channels()
    await kc.wait_for_ready(timeout=ready_timeout or default_timeout)
    try: yield km, kc
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


@patch
def shell_reply(self: KernelClient, msg_id:str, timeout:float = default_timeout)->dict:
    "Return shell reply matching `msg_id`."
    return wait_for_msg(self.get_shell_msg, lambda m: parent_id(m) == msg_id, timeout, err="timeout waiting for shell reply")


@patch
def control_reply(self: KernelClient, msg_id:str, timeout:float = default_timeout)->dict:
    "Return control reply matching `msg_id`."
    return wait_for_msg(self.control_channel.get_msg, lambda m: parent_id(m) == msg_id, timeout, err="timeout waiting for control reply")


@patch
def iopub_drain(self: KernelClient, msg_id:str, timeout:float = default_timeout)->list[dict]:
    "Drain iopub messages for `msg_id` until idle."
    outputs = []
    for rem in iter_timeout(timeout):
        try: msg = self.get_iopub_msg(timeout=min(timeout, rem))
        except Empty: continue
        if parent_id(msg) != msg_id: continue
        outputs.append(msg)
        if msg.get("msg_type") == "status" and nested_idx(msg, "content", "execution_state") == "idle": break
    return outputs


@patch
@delegates(KernelClient.execute)
def exec_drain(self: KernelClient, code:str, timeout:float|None=None, **kwargs):
    "Execute `code` and return (msg_id, reply, outputs)."
    msg_id = self.execute(code, **kwargs)
    timeout = timeout or default_timeout
    reply = self.shell_reply(msg_id, timeout=timeout)
    outputs = self.iopub_drain(msg_id, timeout=timeout)
    return msg_id, reply, outputs


@patch
def interrupt_request(self: KernelClient, timeout:float = 2)->dict:
    "Send interrupt_request and return interrupt_reply."
    msg = self.session.msg("interrupt_request", {})
    self.control_channel.send(msg)
    reply = wait_for_msg(self.control_channel.get_msg, lambda r: parent_id(r) == msg["header"]["msg_id"], timeout,
        poll=0.2, err="missing interrupt_reply")
    assert reply["header"]["msg_type"] == "interrupt_reply"
    return reply


@patch
async def interrupt_request_async(self: AsyncKernelClient, timeout:float = 2)->dict:
    "Send interrupt_request and return interrupt_reply (async)."
    msg = self.session.msg("interrupt_request", {})
    self.control_channel.send(msg)
    for rem in iter_timeout(timeout, default=timeout):
        try: reply = await self.control_channel.get_msg(timeout=min(timeout, rem))
        except Empty: continue
        if parent_id(reply) == msg["header"]["msg_id"]:
            assert reply["header"]["msg_type"] == "interrupt_reply"
            return reply
    raise AssertionError("timeout waiting for interrupt_reply")


class _ReqProxy:
    def __init__(self, kc: KernelClient, channel:str, suffix:str):
        self.kc = kc
        self.channel = channel
        self.suffix = suffix

    def __getattr__(self, name:str):
        msg_type = f"{name}{self.suffix}"
        reply_fn = self.kc.control_reply if self.channel == "control" else self.kc.shell_reply
        channel = self.kc.control_channel if self.channel == "control" else self.kc.shell_channel

        def _call(*, timeout:float = default_timeout, **content):
            msg = self.kc.session.msg(msg_type, content)
            channel.send(msg)
            return reply_fn(msg["header"]["msg_id"], timeout=timeout)

        return _call


@patch(as_prop=True)
def ctl(self: KernelClient)->_ReqProxy:
    if (proxy := getattr(self, "ctl_cache", None)) is None: self.ctl_cache = proxy = _ReqProxy(self, "control", "_request")
    return proxy


class ShellCommand:
    def __init__(self, kc: KernelClient):
        "Shell command proxy for `kc`."
        self.kc = kc

    def __getattr__(self, name:str):
        if name.startswith('_'): raise AttributeError(name)
        def _call(*, subshell_id:str|None=None, buffers: list[bytes]|None=None, content: dict|None=None, **kwargs):
            return self.kc.shell_send(name, content, subshell_id=subshell_id, buffers=buffers, **kwargs)

        return _call


@patch(as_prop=True)
def cmd(self: KernelClient)->ShellCommand:
    if (proxy := getattr(self, "cmd_cache", None)) is None: self.cmd_cache = proxy = ShellCommand(self)
    return proxy


@patch
def shell_send(self: KernelClient, msg_type:str, content: dict|None=None, subshell_id:str|None=None,
    buffers: list[bytes]|None=None, **kwargs)->str:
    "Send shell message with optional subshell header, buffers, and kwargs content."
    if content is None: content = {}
    if kwargs: content = dict(content) | kwargs
    msg = self.session.msg(msg_type, content)
    if subshell_id is not None: msg["header"]["subshell_id"] = subshell_id
    if buffers: self.session.send(self.shell_channel.socket, msg, buffers=buffers)
    else: self.shell_channel.send(msg)
    return msg["header"]["msg_id"]





def wait_for_status(kc, state:str, timeout:float|None=None)->dict:
    pred = lambda m: m.get("msg_type") == "status" and nested_idx(m, "content", "execution_state") == state
    return wait_for_msg(kc.get_iopub_msg, pred, timeout, err=f"timeout waiting for status: {state}")


