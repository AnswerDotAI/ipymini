"""Vanilla-client compat smoke tests.

ipymini's main suite drives the kernel through ConKernelClient, whose Session.send
patch compensates a real pyzmq edge-trigger deadlock -- so a kernel regression that
reintroduces that class of hang would be invisible through the patched client. These
tests exercise the kernel through *unpatched* jupyter_client, the shape nbclassic,
nbclient, and papermill actually use.

These run in the default suite: xdist's loadfile scheduler hands out file groups in
collection order, and tests/compat sorts first, so this file is a worker's first group,
in a still-fresh process (importing conkernelclient doesn't patch; only constructing a
ConKernelClient does). The guard test below trips loudly if that ever stops holding
(e.g. a --dist change, or a directory renamed to sort earlier).
"""
import pytest
from queue import Empty
from jupyter_client.session import Session

from ..kernel_utils import start_kernel, start_kernel_async, wait_for_status, iopub_streams, default_timeout

pytestmark = pytest.mark.compat


def test_session_is_unpatched():
    "Guard: this process must be vanilla. `import conkernelclient` alone must not patch."
    import conkernelclient  # chkstyle: ignore
    assert not hasattr(Session, "_orig_send"), (
        "Session.send is patched: these tests are not exercising the vanilla transport. "
        "Run this file alone with -n0, before anything constructs a ConKernelClient.")


@pytest.fixture(scope="module")
def kc():
    with start_kernel() as (_, kc): yield kc


def test_execute_roundtrip(kc):
    _, reply, outputs = kc.exec_drain("40+2")
    assert reply["content"]["status"] == "ok"
    res = [m for m in outputs if m["msg_type"] == "execute_result"]
    assert res and res[0]["content"]["data"]["text/plain"] == "42"


def test_stream_output(kc):
    _, reply, outputs = kc.exec_drain("print('vanilla')")
    assert reply["content"]["status"] == "ok"
    assert "vanilla" in "".join(m["content"]["text"] for m in iopub_streams(outputs))


def test_error_reply(kc):
    _, reply, _ = kc.exec_drain("1/0")
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "ZeroDivisionError"


def test_interrupt_busy_kernel(kc):
    mid = kc.execute("import time; time.sleep(30)")
    wait_for_status(kc, "busy")
    kc.interrupt_request()
    reply = kc.shell_reply(mid, timeout=10)
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "KeyboardInterrupt"
    _, reply, _ = kc.exec_drain("40+2")
    assert reply["content"]["status"] == "ok"


def test_stdin_roundtrip(kc):
    mid = kc.execute("x = input('name? ')", allow_stdin=True)
    msg = kc.stdin_channel.get_msg(timeout=default_timeout)
    assert msg["msg_type"] == "input_request"
    kc.input("Ada")
    assert kc.shell_reply(mid, timeout=10)["content"]["status"] == "ok"
    _, reply, outputs = kc.exec_drain("print(x)")
    assert "Ada" in "".join(m["content"]["text"] for m in iopub_streams(outputs))


def test_shutdown_clean():
    with start_kernel() as (km, kc):
        _, reply, _ = kc.exec_drain("1+1")
        assert reply["content"]["status"] == "ok"
    assert not km.is_alive()


async def _shell_reply(kc, mid, timeout=10):
    "Next shell reply parented to `mid`, skipping leftovers (e.g. extra kernel_info replies from a slow-start `wait_for_ready`)."
    while True:
        reply = await kc.get_shell_msg(timeout=timeout)
        if reply["parent_header"].get("msg_id") == mid: return reply


async def test_async_client_execute_and_iopub():
    async with start_kernel_async() as (_, kc):
        mid = kc.execute("print('hi'); 42")
        reply = await _shell_reply(kc, mid)
        assert reply["content"]["status"] == "ok"
        seen = []
        while True:
            try: msg = await kc.get_iopub_msg(timeout=2)
            except Empty: continue
            if msg["parent_header"].get("msg_id") != mid: continue
            seen.append(msg)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle": break
        assert any(m["msg_type"] == "stream" and "hi" in m["content"]["text"] for m in seen)


async def test_async_client_interrupt():
    async with start_kernel_async() as (_, kc):
        mid = kc.execute("import time; time.sleep(30)")
        while True:  # interrupt only once actually busy, else it lands on an idle kernel and no-ops
            msg = await kc.get_iopub_msg(timeout=10)
            if msg.get("msg_type") == "status" and msg["content"]["execution_state"] == "busy" and msg["parent_header"].get("msg_id") == mid: break
        await kc.interrupt_request_async()
        reply = await _shell_reply(kc, mid)
        assert reply["content"]["ename"] == "KeyboardInterrupt"


