import json, os, signal, threading, time, pytest
from microio import CloseScope
from ipymini.kernel import KernelState, MiniKernel
from ..aclient import *
from ..kernel_utils import kernel_pid, assert_pid_gone, default_timeout


def _wait_kernel_process(km, timeout:float = 5):
    proc = getattr(getattr(km, "provisioner", None), "process", None)
    if proc is not None: proc.wait(timeout=timeout)


async def _shutdown_request(kc):
    reply = await kc.ctl.shutdown(restart=False)
    assert reply["content"]["status"] == "ok"


async def test_shutdown_reaps_kernel_process():
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

    async with mini_kernel() as (km, kc):
        pid = kernel_pid(km)
        assert pid and pid != os.getpid()
        start = time.perf_counter()
        await _shutdown_request(kc)
        _wait_kernel_process(km)
        assert time.perf_counter() - start < 1.9
        assert_pid_gone(pid)


async def test_graceful_shutdown_exits_while_busy_or_waiting_for_input():
    for code, wait in [("import time; time.sleep(1000)", "busy"), ("input('name: ')", "stdin")]:
        async with mini_kernel() as (km, kc):
            pid = kernel_pid(km)
            kc.execute(code, allow_stdin=wait == "stdin")
            if wait == "stdin": await kc.get_stdin_msg(timeout=default_timeout)
            else: await wait_status(kc, "busy")
            await _shutdown_request(kc)
            _wait_kernel_process(km)
            assert_pid_gone(pid)


async def test_graceful_shutdown_exits_with_busy_subshell():
    async with mini_kernel() as (km, kc):
        pid = kernel_pid(km)
        subshell_id = (await kc.ctl.create_subshell())["content"]["subshell_id"]
        kc.execute("while True: pass", subsh_id=subshell_id)
        await wait_status(kc, "busy")
        await _shutdown_request(kc)
        _wait_kernel_process(km)
        assert_pid_gone(pid)


@pytest.mark.skipif(os.name == "nt", reason="process-group teardown is POSIX-only")
async def test_sigterm_kills_user_resources():
    async with mini_kernel() as (km, kc):
        pid = kernel_pid(km)
        child_pid = None
        try:
            code = (
                "import subprocess, sys, threading, time\n"
                "threading.Thread(target=lambda: time.sleep(10000), daemon=False).start()\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10000)'])\n"
                "print(p.pid)\n")
            reply, outputs = await kc.exec_drain(code)
            assert reply["content"]["status"] == "ok"
            streams = "".join(m["content"].get("text", "") for m in iopub_streams(outputs))
            child_pid = int(streams.strip().splitlines()[-1])
            os.kill(child_pid, 0)
            os.kill(pid, signal.SIGTERM)
            _wait_kernel_process(km)
            assert_pid_gone(pid)
            assert_pid_gone(child_pid)
        finally:
            if child_pid is not None:
                try: os.kill(child_pid, 9)
                except OSError: pass


@pytest.mark.slow
async def test_graceful_shutdown_kills_nested_ipymini_kernel():
    "A nested ipymini KernelManager should not survive when the outer kernel stops."
    async with mini_kernel() as (km, kc):
        pid = kernel_pid(km)
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
            reply, outputs = await kc.exec_drain(code, timeout=15)
            assert reply["content"]["status"] == "ok", reply["content"]
            streams = "".join(m["content"].get("text", "") for m in iopub_streams(outputs))
            nested = json.loads(streams.strip().splitlines()[-1])
            assert nested["pid"] != pid
            assert nested["pgid"] != nested["outer_pgid"]

            await _shutdown_request(kc)
            _wait_kernel_process(km)
            assert_pid_gone(pid)
            assert_pid_gone(nested["pid"], timeout=1)
        finally:
            if nested is not None:
                try: os.killpg(nested["pgid"], signal.SIGKILL)
                except OSError: pass
