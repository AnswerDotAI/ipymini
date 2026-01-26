import asyncio, json, os, time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from queue import Empty
from jupyter_client import AsyncKernelClient, KernelClient, KernelManager
from fastcore.basics import patch
from fastcore.meta import delegates

default_timeout = 10
debug_init_args = dict( clientID="test-client", clientName="testClient", adapterID="", pathFormat="path", linesStartAt1=True,
    columnsStartAt1=True, supportsVariableType=True, supportsVariablePaging=True, supportsRunInTerminalRequest=True, locale="en")
root = Path(__file__).resolve().parents[1]

__all__ = ("default_timeout debug_init_args root KernelHarness build_env load_connection ensure_separate_process start_kernel temp_env "
    "wait_for_msg iter_timeout parent_id wait_for_debug_event wait_for_stop collect_shell_replies collect_iopub_outputs "
    "wait_for_status iopub_msgs iopub_streams start_gateway_kernel gw_send_wait gw_wait_for_status").split()


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


def parent_id(msg: dict)->str|None: return msg.get("parent_header", {}).get("msg_id")


def iter_timeout(timeout:float|None=None, default:float = default_timeout):
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
    kc.wait_for_ready(timeout=default_timeout)
    try: yield km, kc
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


class KernelHarness:
    def __init__(self, extra_env: dict|None=None, **kwargs):
        "Minimal kernel harness for protocol-style tests."
        self.extra_env = extra_env
        self.kwargs = kwargs
        self.ctx = None
        self.km = None
        self.kc = None

    def __enter__(self):
        self.ctx = start_kernel(extra_env=self.extra_env, **self.kwargs)
        self.km, self.kc = self.ctx.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.ctx is not None: return self.ctx.__exit__(exc_type, exc, tb)

    def send_wait(self, msg_type:str, timeout:float = default_timeout, content: dict|None=None, **kwargs):
        "Send shell request and return (msg_id, reply)."
        msg_id = self.kc.shell_send(msg_type, content, **kwargs)
        reply = self.kc.shell_reply(msg_id, timeout=timeout)
        return msg_id, reply

    def control_send_wait(self, msg_type:str, timeout:float = default_timeout, content: dict|None=None):
        "Send control request and return (msg_id, reply)."
        if content is None: content = {}
        msg = self.kc.session.msg(msg_type, content)
        self.kc.control_channel.send(msg)
        reply = self.kc.control_reply(msg["header"]["msg_id"], timeout=timeout)
        return msg["header"]["msg_id"], reply

    def jmsgs(self, msg_id:str, timeout:float = default_timeout)->list[dict]:
        "Return iopub messages for `msg_id`."
        return self.kc.iopub_drain(msg_id, timeout=timeout)

    def exec_drain(self, code:str, timeout:float|None=None, **kwargs):
        "Execute `code` and return (msg_id, reply, outputs)."
        return self.kc.exec_drain(code, timeout=timeout, **kwargs)


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
        if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle": break
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


class _DapProxy:
    def __init__(self, kc: KernelClient):
        self.kc = kc
        self.seq = 1

    def __getattr__(self, command:str):
        if command.endswith('_') and not command.endswith('__'): command = command[:-1]
        def _call(*, timeout:float = default_timeout, full: bool = False, **arguments):
            seq = self.seq
            self.seq += 1
            msg = self.kc.session.msg("debug_request", dict(type="request", seq=seq, command=command, arguments=arguments))
            self.kc.control_channel.send(msg)
            reply = self.kc.control_reply(msg["header"]["msg_id"], timeout=timeout)
            return reply if full else reply["content"]

        return _call


@patch(as_prop=True)
def dap(self: KernelClient)->_DapProxy:
    if (proxy := getattr(self, "dap_cache", None)) is None: self.dap_cache = proxy = _DapProxy(self)
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


@patch
@delegates(KernelClient.execute)
def exec_ok(self: KernelClient, code:str, timeout:float|None=None, **kwargs):
    "Execute `code` and assert ok reply."
    msg_id, reply, outputs = self.exec_drain(code, timeout=timeout, **kwargs)
    assert reply["content"]["status"] == "ok", reply.get("content")
    return msg_id, reply, outputs


def wait_for_debug_event(kc, event_name:str, timeout:float|None=None)->dict:
    pred = lambda m: m.get("msg_type") == "debug_event" and m.get("content", {}).get("event") == event_name
    return wait_for_msg(kc.get_iopub_msg, pred, timeout, poll=0.5, err=f"debug_event {event_name} not received")


def wait_for_stop(kc, timeout:float|None=None)->dict:
    timeout = timeout or default_timeout
    try: return wait_for_debug_event(kc, "stopped", timeout=timeout / 2)
    except AssertionError:
        last = None
        for _ in iter_timeout(timeout, default=timeout):
            reply = kc.dap.stackTrace(threadId=1)
            if reply.get("success"): return dict(content=dict(body=dict(reason="breakpoint", threadId=1)))
            last = reply
            time.sleep(0.1)
        raise AssertionError(f"stopped debug_event not received: {last}")


def collect_shell_replies(kc, msg_ids: set[str], timeout:float|None=None)->dict:
    timeout = timeout or default_timeout
    replies = {}
    for _ in iter_timeout(timeout):
        if len(replies) >= len(msg_ids): break
        try: reply = kc.get_shell_msg(timeout=timeout)
        except Empty: continue
        if (mid := parent_id(reply)) in msg_ids: replies[mid] = reply
    if len(replies) != len(msg_ids):
        missing = msg_ids - set(replies)
        raise AssertionError(f"timeout waiting for shell replies: {sorted(missing)}")
    return replies


def collect_iopub_outputs(kc, msg_ids: set[str], timeout:float|None=None)->dict:
    timeout = timeout or default_timeout
    outputs = {msg_id: [] for msg_id in msg_ids}
    idle = set()
    for _ in iter_timeout(timeout):
        if len(idle) >= len(msg_ids): break
        try: msg = kc.get_iopub_msg(timeout=timeout)
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


# Async gateway test helpers

async def _gw_router(kc, waiters: dict, stop: asyncio.Event):
    "Route shell replies to waiters by msg_id."
    from queue import Empty
    while not stop.is_set():
        try: msg = await kc.get_shell_msg(timeout=0.1)
        except Empty: continue
        except (asyncio.CancelledError, RuntimeError): break
        waiter = waiters.get(parent_id(msg))
        if waiter is not None: waiter.put_nowait(msg)


async def gw_send_wait(kc, waiters: dict, code:str, timeout:float)->tuple[str, dict]:
    "Execute code and wait for reply via gateway router."
    msg_id = kc.execute(code)
    q = asyncio.Queue()
    waiters[msg_id] = q
    try: return msg_id, await asyncio.wait_for(q.get(), timeout=timeout)
    finally: waiters.pop(msg_id, None)


async def gw_wait_for_status(kc, state:str, timeout:float)->dict:
    "Wait for iopub status message with given execution_state."
    async with asyncio.timeout(timeout):
        while True:
            msg = await kc.get_iopub_msg(timeout=0.2)
            if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == state: return msg


@asynccontextmanager
async def start_gateway_kernel(extra_env: dict|None=None):
    "Async context manager for gateway-style kernel tests with router."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env)
    ensure_separate_process(km)
    kc = AsyncKernelClient(**km.get_connection_info(session=True))
    kc.parent = km
    kc.start_channels()
    await kc.wait_for_ready(timeout=default_timeout)
    waiters = {}
    stop = asyncio.Event()
    router_task = asyncio.create_task(_gw_router(kc, waiters, stop))
    kc.gw_waiters = waiters
    try: yield km, kc
    finally:
        stop.set()
        router_task.cancel()
        await asyncio.gather(router_task, return_exceptions=True)
        kc.stop_channels()
        km.shutdown_kernel(now=True)
