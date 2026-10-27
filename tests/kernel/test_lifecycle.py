import os, signal, pytest
from ..aclient import *
from ..kernel_utils import kernel_pid, assert_pid_gone


async def test_restart_clears_namespace_and_shutdown_reaps_kernel():
    async with mini_kernel() as (km, kc):
        await kc.exec_ok('before_restart = 42')
        first_pid = kernel_pid(km)
        kc = await reconnect(km, kc)
        assert_pid_gone(first_pid)
        await kc.exec_ok("assert 'before_restart' not in globals()")
        pid = kernel_pid(km)
        kc.execute('input()', allow_stdin=True)
        await kc.jmsgq.get('stdin', timeout=3)
        assert (await kc.ctl.shutdown(restart=False))['content']['status'] == 'ok'
        km.provisioner.process.wait(timeout=5)
        assert_pid_gone(pid)


@pytest.mark.skipif(os.name == 'nt', reason='process-group teardown is POSIX-only')
async def test_sigterm_kills_user_resources():
    async with mini_kernel() as (km, kc):
        pid = kernel_pid(km)
        child_pid = None
        try:
            _, outputs = await kc.exec_ok('''import subprocess, sys, threading, time
threading.Thread(target=lambda: time.sleep(10000), daemon=False).start()
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10000)'])
print(p.pid)
''')
            child_pid = int(stream_text(outputs).strip())
            os.kill(child_pid, 0)
            os.kill(pid, signal.SIGTERM)
            km.provisioner.process.wait(timeout=5)
            assert_pid_gone(pid)
            assert_pid_gone(child_pid)
        finally:
            if child_pid is not None:
                try: os.kill(child_pid, signal.SIGKILL)
                except OSError: pass


@pytest.mark.skipif(os.name == 'nt', reason='parent-death cleanup is POSIX-only')
async def test_shutdown_reaps_nested_kernel():
    async with mini_kernel() as (km, kc):
        nested = None
        try:
            _, outputs = await kc.exec_ok('''import os
from jupyter_client import KernelManager
nested = KernelManager(kernel_name='py')
nested.start_kernel()
client = nested.client()
client.start_channels()
client.wait_for_ready(timeout=10)
print(nested.provisioner.pid)
''')
            nested = int(stream_text(outputs).strip())
            assert os.getpgid(nested) != os.getpgid(kernel_pid(km))
            await kc.ctl.shutdown(restart=False)
            km.provisioner.process.wait(timeout=5)
            assert_pid_gone(nested, timeout=1)
        finally:
            if nested is not None:
                try: os.killpg(nested, signal.SIGKILL)
                except OSError: pass
