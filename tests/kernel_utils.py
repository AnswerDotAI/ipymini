import os, time
from contextlib import contextmanager
from pathlib import Path
from queue import Empty
from jupyter_client import KernelClient, KernelManager
from fastcore.basics import nested_idx, patch
from fastcore.meta import delegates
from jupywire.ops import parent_id
from conkernelclient.ops import iter_timeout, iopub_streams  # importing is side-effect free (no Session patch)

default_timeout = 10
root = Path(__file__).resolve().parents[1]

def stream_text(msgs, name=None)->str:
    "The joined text of the stream messages in `msgs`, optionally only stream `name` ('stdout'/'stderr')"
    return "".join(m["content"]["text"] for m in iopub_streams(msgs, name))


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


def kernel_pid(km)->int|None:
    "The local kernel process id."
    return km.provisioner.pid


def assert_pid_gone(pid:int, timeout:float = 5):
    "Assert process `pid` no longer exists."
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try: os.kill(pid, 0)
        except ProcessLookupError: return
        time.sleep(0.05)
    raise AssertionError(f"kernel pid still alive: {pid}")


@delegates(KernelManager.start_kernel, but="env")
@contextmanager
def vanilla_manager(extra_env: dict|None=None, **kwargs):
    "Start a kernel for an unpatched Jupyter client."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="py")
    km.start_kernel(env=env, **kwargs)
    try: yield km
    finally: km.shutdown_kernel(now=True)


@contextmanager
def vanilla_kernel(extra_env=None):
    "The synchronous client used by ordinary Jupyter applications."
    with vanilla_manager(extra_env) as km:
        kc = km.client()
        kc.start_channels()
        kc.wait_for_ready(timeout=default_timeout)
        try: yield km, kc
        finally: kc.stop_channels()


@patch
def shell_reply(self: KernelClient, msg_id:str, timeout:float = default_timeout)->dict:
    "Return shell reply matching `msg_id`."
    return wait_for_msg(self.get_shell_msg, lambda m: parent_id(m) == msg_id, timeout, err="timeout waiting for shell reply")


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


def wait_for_status(kc, state:str, timeout:float|None=None)->dict:
    pred = lambda m: m.get("msg_type") == "status" and nested_idx(m, "content", "execution_state") == state
    return wait_for_msg(kc.get_iopub_msg, pred, timeout, err=f"timeout waiting for status: {state}")
