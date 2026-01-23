import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from fastcore.foundation import L
from jupyter_client import KernelManager

TIMEOUT = 10
DEBUG_INIT_ARGS = {
    "clientID": "test-client",
    "clientName": "testClient",
    "adapterID": "",
    "pathFormat": "path",
    "linesStartAt1": True,
    "columnsStartAt1": True,
    "supportsVariableType": True,
    "supportsVariablePaging": True,
    "supportsRunInTerminalRequest": True,
    "locale": "en",
}
ROOT = Path(__file__).resolve().parents[1]


def _ensure_jupyter_path() -> str:
    share = str(ROOT / "share" / "jupyter")
    current = os.environ.get("JUPYTER_PATH", "")
    if current:
        return f"{share}{os.pathsep}{current}"
    return share


def _build_env() -> dict:
    current = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{ROOT}{os.pathsep}{current}" if current else str(ROOT)
    return {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "JUPYTER_PATH": _ensure_jupyter_path(),
    }


def build_env(extra_env: dict | None = None) -> dict:
    env = _build_env()
    if extra_env:
        env = {**env, **extra_env}
    return env


def load_connection(km) -> dict:
    with open(km.connection_file, encoding="utf-8") as f:
        return json.load(f)


def ensure_separate_process(km: KernelManager) -> None:
    pid = None
    provisioner = getattr(km, "provisioner", None)
    if provisioner is not None:
        pid = getattr(provisioner, "pid", None)
    if pid is None:
        proc = getattr(provisioner, "process", None)
        pid = getattr(proc, "pid", None) if proc is not None else None
    if pid is None or pid == os.getpid():
        raise RuntimeError("kernel must run in a separate process")


@contextmanager
def start_kernel(extra_env: dict | None = None):
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env)
    ensure_separate_process(km)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=TIMEOUT)
    try:
        yield km, kc
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


def drain_iopub(kc, msg_id):
    deadline = time.time() + TIMEOUT
    outputs = []
    while time.time() < deadline:
        msg = kc.get_iopub_msg(timeout=TIMEOUT)
        if msg["parent_header"].get("msg_id") != msg_id:
            continue
        outputs.append(msg)
        if msg["msg_type"] == "status" and msg["content"].get("execution_state") == "idle":
            break
    return outputs


def get_shell_reply(kc, msg_id, timeout: float | None = None):
    deadline = time.time() + (timeout or TIMEOUT)
    while time.time() < deadline:
        reply = kc.get_shell_msg(timeout=TIMEOUT)
        if reply["parent_header"].get("msg_id") == msg_id:
            return reply
    raise AssertionError("timeout waiting for matching shell reply")


def collect_shell_replies(kc, msg_ids: set[str], timeout: float | None = None) -> dict[str, dict]:
    deadline = time.time() + (timeout or TIMEOUT)
    replies: dict[str, dict] = {}
    while time.time() < deadline and len(replies) < len(msg_ids):
        reply = kc.get_shell_msg(timeout=TIMEOUT)
        parent_id = reply.get("parent_header", {}).get("msg_id")
        if parent_id in msg_ids:
            replies[parent_id] = reply
    if len(replies) != len(msg_ids):
        missing = msg_ids - set(replies)
        raise AssertionError(f"timeout waiting for shell replies: {sorted(missing)}")
    return replies


def collect_iopub_outputs(
    kc,
    msg_ids: set[str],
    timeout: float | None = None,
) -> dict[str, list[dict]]:
    deadline = time.time() + (timeout or TIMEOUT)
    outputs: dict[str, list[dict]] = {msg_id: [] for msg_id in msg_ids}
    idle = set()
    while time.time() < deadline and len(idle) < len(msg_ids):
        msg = kc.get_iopub_msg(timeout=TIMEOUT)
        parent_id = msg.get("parent_header", {}).get("msg_id")
        if parent_id not in outputs:
            continue
        outputs[parent_id].append(msg)
        if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle":
            idle.add(parent_id)
    if len(idle) != len(msg_ids):
        missing = msg_ids - idle
        raise AssertionError(f"timeout waiting for iopub idle: {sorted(missing)}")
    return outputs


def wait_for_status(kc, state: str, timeout: float | None = None) -> dict:
    deadline = time.time() + (timeout or TIMEOUT)
    while time.time() < deadline:
        msg = kc.get_iopub_msg(timeout=TIMEOUT)
        if msg["msg_type"] == "status" and msg["content"].get("execution_state") == state:
            return msg
    raise AssertionError(f"timeout waiting for status: {state}")


def iopub_msgs(outputs: list[dict], msg_type: str | None = None) -> L:
    msgs = L(outputs)
    return msgs if msg_type is None else msgs.filter(lambda m: m["msg_type"] == msg_type)


def iopub_streams(outputs: list[dict], name: str | None = None) -> L:
    streams = iopub_msgs(outputs, "stream")
    return streams if name is None else streams.filter(lambda m: m["content"].get("name") == name)
