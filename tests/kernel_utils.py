import json, os, time
from contextlib import contextmanager
from pathlib import Path
from queue import Empty
from jupyter_client import AsyncKernelClient, KernelClient, KernelManager
from fastcore.basics import patch
from fastcore.meta import delegates

TIMEOUT = 10
DEBUG_INIT_ARGS = dict( clientID="test-client", clientName="testClient", adapterID="", pathFormat="path", linesStartAt1=True,
    columnsStartAt1=True, supportsVariableType=True, supportsVariablePaging=True, supportsRunInTerminalRequest=True, locale="en")
ROOT = Path(__file__).resolve().parents[1]

__all__ = [ "TIMEOUT", "DEBUG_INIT_ARGS", "ROOT", "build_env", "load_connection", "ensure_separate_process", "start_kernel", "temp_env",
    "wait_for_msg", "iter_timeout", "parent_id", "debug_request", "debug_dump_cell", "debug_set_breakpoints", "debug_info",
    "debug_configuration_done", "debug_continue", "wait_for_debug_event", "wait_for_stop", "get_shell_reply", "collect_shell_replies",
    "collect_iopub_outputs", "wait_for_status", "iopub_msgs", "iopub_streams" ]


def _ensure_jupyter_path()->str:
    share = str(ROOT / "share" / "jupyter")
    current = os.environ.get("JUPYTER_PATH", "")
    return f"{share}{os.pathsep}{current}" if current else share


def _build_env()->dict:
    current = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{ROOT}{os.pathsep}{current}" if current else str(ROOT)
    return dict(os.environ) | dict(PYTHONPATH=pythonpath, JUPYTER_PATH=_ensure_jupyter_path())


def build_env(extra_env: dict|None=None)->dict:
    env = _build_env()
    if extra_env: env = {**env, **extra_env}
    return env


def parent_id(msg: dict)->str|None: return msg.get("parent_header", {}).get("msg_id")


def iter_timeout(timeout:float|None=None, default:float = TIMEOUT):
    "Yield remaining time (seconds) until timeout expires, using monotonic time."
    end = time.monotonic() + (timeout or default)
    while (rem := end - time.monotonic()) > 0: yield rem


def wait_for_msg(get_msg, match, timeout:float|None=None, poll:float = 0.5, err:str = "timeout"):
    "Call `get_msg(timeout=...)` until `match(msg)` is true, else raise AssertionError on timeout."
    for rem in iter_timeout(timeout):
        try: msg = get_msg(timeout=min(poll, rem))
        except Empty: continue
        if match(msg): return msg
    raise AssertionError(err)


def load_connection(km)->dict:
    with open(km.connection_file, encoding="utf-8") as f: return json.load(f)


def ensure_separate_process(km: KernelManager):
    "Ensure separate process."
    pid = None
    provisioner = getattr(km, "provisioner", None)
    if provisioner is not None: pid = getattr(provisioner, "pid", None)
    if pid is None:
        proc = getattr(provisioner, "process", None)
        pid = getattr(proc, "pid", None) if proc is not None else None
    if pid is None or pid == os.getpid(): raise RuntimeError("kernel must run in a separate process")


@contextmanager
def temp_env(update: dict):
    "Temporarily update environment variables."
    old_env = {key: os.environ.get(key) for key in update}
    os.environ.update({key:str(value) for key, value in update.items()})
    try: yield
    finally:
        for key, value in old_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


@delegates(KernelManager.start_kernel, but="env")
@contextmanager
def start_kernel(extra_env: dict|None=None, **kwargs):
    "Start kernel."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env, **kwargs)
    ensure_separate_process(km)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=TIMEOUT)
    try: yield km, kc
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


@patch
def shell_reply(self: KernelClient, msg_id:str, timeout:float = TIMEOUT)->dict:
    "Return shell reply matching `msg_id`."
    return wait_for_msg(self.get_shell_msg, lambda m: parent_id(m) == msg_id, timeout, err="timeout waiting for shell reply")


@patch
def control_reply(self: KernelClient, msg_id:str, timeout:float = TIMEOUT)->dict:
    "Return control reply matching `msg_id`."
    return wait_for_msg(self.control_channel.get_msg, lambda m: parent_id(m) == msg_id, timeout, err="timeout waiting for control reply")


@patch
def iopub_drain(self: KernelClient, msg_id:str, timeout:float = TIMEOUT)->list[dict]:
    "Drain iopub messages for `msg_id` until idle."
    outputs = []
    for rem in iter_timeout(timeout):
        try: msg = self.get_iopub_msg(timeout=min(TIMEOUT, rem))
        except Empty: continue
        if parent_id(msg) != msg_id: continue
        outputs.append(msg)
        if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle": break
    return outputs


@patch
@delegates(KernelClient.execute, keep=True)
def exec_drain(self: KernelClient, code:str, timeout:float|None=None, **kwargs):
    "Execute `code` and return (msg_id, reply, outputs)."
    msg_id = self.execute(code, **kwargs)
    timeout = timeout or TIMEOUT
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

        def _call(*, timeout:float = TIMEOUT, **content):
            msg = self.kc.session.msg(msg_type, content)
            channel.send(msg)
            return reply_fn(msg["header"]["msg_id"], timeout=timeout)

        return _call


@patch(as_prop=True)
def ctl(self: KernelClient)->_ReqProxy:
    if (proxy := getattr(self, "_ctl", None)) is None: self._ctl = proxy = _ReqProxy(self, "control", "_request")
    return proxy


class _DapProxy:
    def __init__(self, kc: KernelClient):
        self.kc = kc
        self.seq = 1

    def __getattr__(self, command:str):
        def _call(*, timeout:float = TIMEOUT, full: bool = False, **arguments):
            seq = self.seq
            self.seq += 1
            msg = self.kc.session.msg("debug_request", dict(type="request", seq=seq, command=command, arguments=arguments))
            self.kc.control_channel.send(msg)
            reply = self.kc.control_reply(msg["header"]["msg_id"], timeout=timeout)
            return reply if full else reply["content"]

        return _call


@patch(as_prop=True)
def dap(self: KernelClient)->_DapProxy:
    if (proxy := getattr(self, "_dap", None)) is None: self._dap = proxy = _DapProxy(self)
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
    if (proxy := getattr(self, "_cmd", None)) is None: self._cmd = proxy = ShellCommand(self)
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


@patch
@delegates(KernelClient.execute, keep=True)
def exec_ok(self: KernelClient, code:str, timeout:float|None=None, **kwargs):
    "Execute `code` and assert ok reply."
    msg_id, reply, outputs = self.exec_drain(code, timeout=timeout, **kwargs)
    assert reply["content"]["status"] == "ok", reply.get("content")
    return msg_id, reply, outputs


def debug_request(kc, command, arguments=None, timeout:float|None=None, full_reply: bool = False, **kwargs):
    if arguments is None: arguments = {}
    if kwargs: arguments |= kwargs
    seq = getattr(kc, "_debug_seq", 1)
    setattr(kc, "_debug_seq", seq + 1)
    msg = kc.session.msg("debug_request", dict(type="request", seq=seq, command=command, arguments=arguments or {}))
    kc.control_channel.send(msg)
    reply = wait_for_msg(kc.control_channel.get_msg, lambda r: parent_id(r) == msg["header"]["msg_id"], timeout,
        err="timeout waiting for debug reply")
    assert reply["header"]["msg_type"] == "debug_reply"
    return reply if full_reply else reply["content"]


def debug_dump_cell(kc, code): return debug_request(kc, "dumpCell", code=code)


def debug_set_breakpoints(kc, source, line):
    args = dict(breakpoints=[dict(line=line)], source=dict(path=source), sourceModified=False)
    return debug_request(kc, "setBreakpoints", args)


def debug_info(kc): return debug_request(kc, "debugInfo")


def debug_configuration_done(kc, full_reply: bool = False): return debug_request(kc, "configurationDone", full_reply=full_reply)


def debug_continue(kc, thread_id:int|None=None):
    if thread_id is None: return debug_request(kc, "continue")
    return debug_request(kc, "continue", threadId=thread_id)


def wait_for_debug_event(kc, event_name:str, timeout:float|None=None)->dict:
    pred = lambda m: m.get("msg_type") == "debug_event" and m.get("content", {}).get("event") == event_name
    return wait_for_msg(kc.get_iopub_msg, pred, timeout, poll=0.5, err=f"debug_event {event_name} not received")


def wait_for_stop(kc, timeout:float|None=None)->dict:
    timeout = timeout or TIMEOUT
    try: return wait_for_debug_event(kc, "stopped", timeout=timeout / 2)
    except AssertionError:
        last = None
        for _ in iter_timeout(timeout, default=timeout):
            reply = debug_request(kc, "stackTrace", threadId=1)
            if reply.get("success"): return dict(content=dict(body=dict(reason="breakpoint", threadId=1)))
            last = reply
            time.sleep(0.1)
        raise AssertionError(f"stopped debug_event not received: {last}")


def get_shell_reply(kc, msg_id, timeout:float|None=None):
    return wait_for_msg(kc.get_shell_msg, lambda m: parent_id(m) == msg_id, timeout, err="timeout waiting for matching shell reply")


def collect_shell_replies(kc, msg_ids: set[str], timeout:float|None=None)->dict:
    replies = {}
    for _ in iter_timeout(timeout):
        if len(replies) >= len(msg_ids): break
        try: reply = kc.get_shell_msg(timeout=TIMEOUT)
        except Empty: continue
        if (mid := parent_id(reply)) in msg_ids: replies[mid] = reply
    if len(replies) != len(msg_ids):
        missing = msg_ids - set(replies)
        raise AssertionError(f"timeout waiting for shell replies: {sorted(missing)}")
    return replies


def collect_iopub_outputs(kc, msg_ids: set[str], timeout:float|None=None)->dict:
    outputs = {msg_id: [] for msg_id in msg_ids}
    idle = set()
    for _ in iter_timeout(timeout):
        if len(idle) >= len(msg_ids): break
        try: msg = kc.get_iopub_msg(timeout=TIMEOUT)
        except Empty: continue
        if (mid := parent_id(msg)) not in outputs: continue
        outputs[mid].append(msg)
        if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle": idle.add(mid)
    if len(idle) != len(msg_ids):
        missing = msg_ids - idle
        raise AssertionError(f"timeout waiting for iopub idle: {sorted(missing)}")
    return outputs


def wait_for_status(kc, state:str, timeout:float|None=None)->dict:
    pred = lambda m: m.get("msg_type") == "status" and m.get("content", {}).get("execution_state") == state
    return wait_for_msg(kc.get_iopub_msg, pred, timeout, err=f"timeout waiting for status: {state}")


def iopub_msgs(outputs: list[dict], msg_type:str|None=None)->list[dict]:
    return outputs if msg_type is None else [m for m in outputs if m["msg_type"] == msg_type]


def iopub_streams(outputs: list[dict], name:str|None=None)->list[dict]:
    streams = iopub_msgs(outputs, "stream")
    return streams if name is None else [m for m in streams if m["content"].get("name") == name]
