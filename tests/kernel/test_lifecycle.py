import json, os, signal, threading, time, pytest
from jupyter_client import KernelManager
from microio import CloseScope
from ipymini.kernel import KernelState, MiniKernel
from ..kernel_utils import *


def _raw_kernel():
    env = build_env()
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env)
    ensure_separate_process(km)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=default_timeout)
    return km, kc, kernel_pid(km)


def _wait_kernel_process(km, timeout:float = 5):
    proc = getattr(getattr(km, "provisioner", None), "process", None)
    if proc is not None: proc.wait(timeout=timeout)


def _shutdown_request(kc):
    msg = kc.session.msg("shutdown_request", {"restart": False})
    kc.control_channel.send(msg)
    reply = kc.control_reply(msg["header"]["msg_id"], timeout=default_timeout)
    assert reply["content"]["status"] == "ok"


def test_shutdown_reaps_kernel_process():
    kernel = MiniKernel.__new__(MiniKernel)
    kernel.state_lock = threading.Lock()
    kernel.state = KernelState.RUNNING
    kernel.stop_scope = CloseScope()
    kernel.shutdown_restart = None

    reply, start_stop = kernel._commit_shutdown(True)
    assert reply == {"status": "ok", "restart": True}
    assert start_stop is True
    assert kernel.state == KernelState.STOPPING

    reply, start_stop = kernel._commit_shutdown(False)
    assert reply == {"status": "ok", "restart": True}
    assert start_stop is False

    replies = []
    kernel.queue_control_reply = lambda msg_type, content, parent, idents: replies.append((msg_type, content))
    kernel.control_router = None
    kernel.handle_shutdown({"header": {"msg_type": "shutdown_request"}, "content": {"restart": False}}, None)
    assert replies[0] == ("shutdown_reply", {"status": "ok", "restart": True})
    kernel.handle_control_msg({"header": {"msg_type": "kernel_info_request"}}, None)
    assert replies[1][0] == "kernel_info_reply"
    assert replies[1][1]["status"] == "error"
    assert replies[1][1]["ename"] == "KernelStopping"
    kernel.handle_control_msg({"header": {"msg_type": "interrupt_request"}}, None)
    assert replies[2][0] == "interrupt_reply"
    assert replies[2][1]["status"] == "error"
    assert replies[2][1]["ename"] == "KernelStopping"

    kernel.shutdown_restart = None
    kernel.stop_scope.close("already stopping")
    reply, start_stop = kernel._commit_shutdown(False)
    assert reply == {"status": "ok", "restart": False}
    assert start_stop is False

    km, kc, pid = _raw_kernel()
    try:
        assert pid and pid != os.getpid()
        start = time.perf_counter()
        _shutdown_request(kc)
        _wait_kernel_process(km)
        assert time.perf_counter() - start < 1.9
        assert_pid_gone(pid)
    finally:
        kc.stop_channels()
        try: km.shutdown_kernel(now=True)
        except Exception: pass


def test_graceful_shutdown_exits_while_busy_or_waiting_for_input():
    for code, wait in [("import time; time.sleep(1000)", "busy"), ("input('name: ')", "stdin")]:
        km, kc, pid = _raw_kernel()
        try:
            kc.execute(code, allow_stdin=wait == "stdin")
            if wait == "stdin": kc.get_stdin_msg(timeout=default_timeout)
            else: wait_for_status(kc, "busy")
            _shutdown_request(kc)
            _wait_kernel_process(km)
            assert_pid_gone(pid)
        finally:
            kc.stop_channels()
            try: km.shutdown_kernel(now=True)
            except Exception: pass


def test_graceful_shutdown_exits_with_busy_subshell():
    km, kc, pid = _raw_kernel()
    try:
        subshell_id = kc.ctl.create_subshell()["content"]["subshell_id"]
        kc.cmd.execute_request(code="while True: pass", subshell_id=subshell_id)
        wait_for_status(kc, "busy")
        _shutdown_request(kc)
        _wait_kernel_process(km)
        assert_pid_gone(pid)
    finally:
        kc.stop_channels()
        try: km.shutdown_kernel(now=True)
        except Exception: pass


@pytest.mark.skipif(os.name == "nt", reason="process-group teardown is POSIX-only")
def test_graceful_shutdown_kills_user_resources():
    km, kc, pid = _raw_kernel()
    child_pid = None
    try:
        code = (
            "import subprocess, sys, threading, time\n"
            "threading.Thread(target=lambda: time.sleep(10000), daemon=False).start()\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10000)'])\n"
            "print(p.pid)\n")
        _msg_id, reply, outputs = kc.exec_drain(code)
        assert reply["content"]["status"] == "ok"
        streams = "".join(m["content"].get("text", "") for m in iopub_streams(outputs))
        child_pid = int(streams.strip().splitlines()[-1])
        os.kill(child_pid, 0)
        _shutdown_request(kc)
        _wait_kernel_process(km)
        assert_pid_gone(pid)
        assert_pid_gone(child_pid)
    finally:
        kc.stop_channels()
        try: km.shutdown_kernel(now=True)
        except Exception: pass
        if child_pid is not None:
            try: os.kill(child_pid, 9)
            except OSError: pass


@pytest.mark.slow
def test_graceful_shutdown_kills_nested_ipymini_kernel():
    "A nested ipymini KernelManager should not survive when the outer kernel stops."
    km, kc, pid = _raw_kernel()
    nested = None
    try:
        code = (
            "import json, os\n"
            "from jupyter_client import KernelManager\n"
            "nested_km = KernelManager(kernel_name='ipymini')\n"
            "nested_km.start_kernel()\n"
            "nested_kc = nested_km.client()\n"
            "nested_kc.start_channels()\n"
            "nested_kc.wait_for_ready(timeout=10)\n"
            "nested_pid = nested_km.provisioner.pid\n"
            "print(json.dumps(dict(pid=nested_pid, pgid=os.getpgid(nested_pid), outer_pid=os.getpid(), outer_pgid=os.getpgrp())))\n")
        _msg_id, reply, outputs = kc.exec_drain(code, timeout=15)
        assert reply["content"]["status"] == "ok", reply["content"]
        streams = "".join(m["content"].get("text", "") for m in iopub_streams(outputs))
        nested = json.loads(streams.strip().splitlines()[-1])
        assert nested["pid"] != pid
        assert nested["pgid"] != nested["outer_pgid"]

        _shutdown_request(kc)
        _wait_kernel_process(km)
        assert_pid_gone(pid)
        assert_pid_gone(nested["pid"], timeout=1)
    finally:
        kc.stop_channels()
        try: km.shutdown_kernel(now=True)
        except Exception: pass
        if nested is not None:
            try: os.killpg(nested["pgid"], signal.SIGKILL)
            except OSError: pass
