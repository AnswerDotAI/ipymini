import json, os, time
from contextlib import contextmanager
from pathlib import Path
from queue import Empty
from jupyter_client import KernelManager

TIMEOUT = 10
DEBUG_INIT_ARGS = dict(clientID="test-client", clientName="testClient", adapterID="", pathFormat="path", linesStartAt1=True,
    columnsStartAt1=True, supportsVariableType=True, supportsVariablePaging=True, supportsRunInTerminalRequest=True, locale="en")
ROOT = Path(__file__).resolve().parents[1]


def _ensure_jupyter_path() -> str:
    "Ensure jupyter path."
    share = str(ROOT / "share" / "jupyter")
    current = os.environ.get("JUPYTER_PATH", "")
    return f"{share}{os.pathsep}{current}" if current else share


def _build_env() -> dict:
    "Build env."
    current = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{ROOT}{os.pathsep}{current}" if current else str(ROOT)
    return dict(os.environ) | dict(PYTHONPATH=pythonpath, JUPYTER_PATH=_ensure_jupyter_path())


def build_env(extra_env: dict | None = None) -> dict:
    "Build env."
    env = _build_env()
    if extra_env: env = {**env, **extra_env}
    return env


def load_connection(km) -> dict:
    with open(km.connection_file, encoding="utf-8") as f: return json.load(f)


def ensure_separate_process(km: KernelManager) -> None:
    "Ensure separate process."
    pid = None
    provisioner = getattr(km, "provisioner", None)
    if provisioner is not None: pid = getattr(provisioner, "pid", None)
    if pid is None:
        proc = getattr(provisioner, "process", None)
        pid = getattr(proc, "pid", None) if proc is not None else None
    if pid is None or pid == os.getpid(): raise RuntimeError("kernel must run in a separate process")


@contextmanager
def start_kernel(extra_env: dict | None = None):
    "Start kernel."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env)
    ensure_separate_process(km)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=TIMEOUT)
    try: yield km, kc
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


def drain_iopub(kc, msg_id):
    "Drain iopub."
    deadline = time.time() + TIMEOUT
    outputs = []
    while time.time() < deadline:
        msg = kc.get_iopub_msg(timeout=TIMEOUT)
        if msg["parent_header"].get("msg_id") != msg_id: continue
        outputs.append(msg)
        if msg["msg_type"] == "status" and msg["content"].get("execution_state") == "idle": break
    return outputs


def execute_and_drain(kc, code, timeout: float | None = None, **exec_kwargs):
    "Execute and drain."
    msg_id = kc.execute(code, **exec_kwargs)
    reply = get_shell_reply(kc, msg_id, timeout=timeout)
    outputs = drain_iopub(kc, msg_id)
    return msg_id, reply, outputs


def debug_request(kc, command, arguments=None, timeout: float | None = None, full_reply: bool = False, **kwargs):
    "Debug request."
    if arguments is None: arguments = {}
    if kwargs: arguments |= kwargs
    seq = getattr(kc, "_debug_seq", 1)
    setattr(kc, "_debug_seq", seq + 1)
    msg = kc.session.msg("debug_request", dict(type="request", seq=seq, command=command, arguments=arguments or {}))
    kc.control_channel.send(msg)
    deadline = time.time() + (timeout or TIMEOUT)
    while time.time() < deadline:
        reply = kc.control_channel.get_msg(timeout=TIMEOUT)
        if reply["parent_header"].get("msg_id") == msg["header"]["msg_id"]:
            assert reply["header"]["msg_type"] == "debug_reply"
            return reply if full_reply else reply["content"]
    raise AssertionError("timeout waiting for debug reply")


def debug_dump_cell(kc, code): return debug_request(kc, "dumpCell", code=code)


def debug_set_breakpoints(kc, source, line):
    "Debug set breakpoints."
    args = dict(breakpoints=[dict(line=line)], source=dict(path=source), sourceModified=False)
    return debug_request(kc, "setBreakpoints", args)


def debug_info(kc): return debug_request(kc, "debugInfo")


def debug_configuration_done(kc, full_reply: bool = False): return debug_request(kc, "configurationDone", full_reply=full_reply)


def debug_continue(kc, thread_id: int | None = None):
    "Debug continue."
    if thread_id is None: return debug_request(kc, "continue")
    return debug_request(kc, "continue", threadId=thread_id)


def wait_for_debug_event(kc, event_name: str, timeout: float | None = None) -> dict:
    "Wait for debug event."
    deadline = time.time() + (timeout or TIMEOUT)
    while time.time() < deadline:
        try: msg = kc.get_iopub_msg(timeout=0.5)
        except Empty: continue
        if msg.get("msg_type") == "debug_event" and msg.get("content", {}).get("event") == event_name: return msg
    raise AssertionError(f"debug_event {event_name} not received")


def wait_for_stop(kc, timeout: float | None = None) -> dict:
    "Wait for stop."
    timeout = timeout or TIMEOUT
    try: return wait_for_debug_event(kc, "stopped", timeout=timeout / 2)
    except AssertionError:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            reply = debug_request(kc, "stackTrace", threadId=1)
            if reply.get("success"): return dict(content=dict(body=dict(reason="breakpoint", threadId=1)))
            last = reply
            time.sleep(0.1)
        raise AssertionError(f"stopped debug_event not received: {last}")


def get_shell_reply(kc, msg_id, timeout: float | None = None):
    "Get shell reply."
    deadline = time.time() + (timeout or TIMEOUT)
    while time.time() < deadline:
        reply = kc.get_shell_msg(timeout=TIMEOUT)
        if reply["parent_header"].get("msg_id") == msg_id: return reply
    raise AssertionError("timeout waiting for matching shell reply")


def collect_shell_replies(kc, msg_ids: set[str], timeout: float | None = None) -> dict:
    "Collect shell replies."
    deadline = time.time() + (timeout or TIMEOUT)
    replies = {}
    while time.time() < deadline and len(replies) < len(msg_ids):
        reply = kc.get_shell_msg(timeout=TIMEOUT)
        parent_id = reply.get("parent_header", {}).get("msg_id")
        if parent_id in msg_ids: replies[parent_id] = reply
    if len(replies) != len(msg_ids):
        missing = msg_ids - set(replies)
        raise AssertionError(f"timeout waiting for shell replies: {sorted(missing)}")
    return replies


def collect_iopub_outputs(kc, msg_ids: set[str], timeout: float | None = None) -> dict:
    "Collect iopub outputs."
    deadline = time.time() + (timeout or TIMEOUT)
    outputs = {msg_id: [] for msg_id in msg_ids}
    idle = set()
    while time.time() < deadline and len(idle) < len(msg_ids):
        msg = kc.get_iopub_msg(timeout=TIMEOUT)
        parent_id = msg.get("parent_header", {}).get("msg_id")
        if parent_id not in outputs: continue
        outputs[parent_id].append(msg)
        if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle": idle.add(parent_id)
    if len(idle) != len(msg_ids):
        missing = msg_ids - idle
        raise AssertionError(f"timeout waiting for iopub idle: {sorted(missing)}")
    return outputs


def wait_for_status(kc, state: str, timeout: float | None = None) -> dict:
    "Wait for status."
    deadline = time.time() + (timeout or TIMEOUT)
    while time.time() < deadline:
        msg = kc.get_iopub_msg(timeout=TIMEOUT)
        if msg["msg_type"] == "status" and msg["content"].get("execution_state") == state: return msg
    raise AssertionError(f"timeout waiting for status: {state}")


def iopub_msgs(outputs: list[dict], msg_type: str | None = None) -> list[dict]:
    "Iopub msgs."
    return outputs if msg_type is None else [m for m in outputs if m["msg_type"] == msg_type]


def iopub_streams(outputs: list[dict], name: str | None = None) -> list[dict]:
    "Iopub streams."
    streams = iopub_msgs(outputs, "stream")
    return streams if name is None else [m for m in streams if m["content"].get("name") == name]
