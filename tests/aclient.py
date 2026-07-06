"""Async test client helpers: ConKernelClient + conkernelclient.ops against an ipymini kernel.

Migrated test modules use this instead of the sync helpers in `kernel_utils`. Async tests
and fixtures run via pytest-asyncio in auto mode (see pytest.ini) on a module-scoped event
loop - the client is bound to its loop, so the loop must live as long as the kernel
connection, which is why `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope`
are both `module`. A shared kernel is just:

    @pytest.fixture(scope="module")
    async def kc():
        async with mini_kernel() as (_, kc): yield kc
"""
import asyncio, os
from contextlib import asynccontextmanager
from queue import Empty

from conkernelclient import *
from conkernelclient.ops import parent_id, iter_timeout, iopub_msgs, iopub_streams

from .kernel_utils import build_env, ensure_separate_process

__all__ = ["mini_kernel", "clone", "wait_iopub", "wait_status", "aflush", "collect_iopub", "input_reply",
    "wait_debug_event", "wait_stop", "debug_init_args", "parent_id", "iter_timeout", "iopub_msgs",
    "iopub_streams", "reconnect", "ConKernelClient", "ConKernelManager", "DeadKernelError"]

debug_init_args = dict(clientID="test-client", clientName="testClient", adapterID="", pathFormat="path", linesStartAt1=True,
    columnsStartAt1=True, supportsVariableType=True, supportsVariablePaging=True, supportsRunInTerminalRequest=True, locale="en")


@asynccontextmanager
async def mini_kernel(extra_env=None, **kw):
    "Start an ipymini kernel with the repo kernelspec/env; yield `(km, kc)`."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    async with run_kernel(kernel_name="ipymini", env=env, **kw) as (km, kc):
        ensure_separate_process(km)
        yield km, kc


@asynccontextmanager
async def clone(kc):
    "A second ConKernelClient session to the same kernel (its own Session, so its own msg ids)."
    k2 = ConKernelClient()
    k2.load_connection_info(kc.get_connection_info())
    await k2.start_channels()
    try: yield k2
    finally: k2.stop_channels()


async def wait_iopub(kc, pred, timeout=10, err="iopub message not received"):
    "Return the first iopub message matching `pred`, discarding others."
    for rem in iter_timeout(timeout):
        try: msg = await kc.get_iopub_msg(timeout=rem)
        except Empty: continue
        if pred(msg): return msg
    raise AssertionError(err)


async def wait_status(kc, state, timeout=10):
    "Wait for an iopub status message with the given execution_state."
    return await wait_iopub(kc, lambda m: m.get("msg_type") == "status" and m["content"].get("execution_state") == state,
        timeout, err=f"timeout waiting for status: {state}")


async def aflush(kc, timeout=0.1):
    "Discard pending iopub messages (for tests sharing a kernel)."
    await kc.iopub_flush(timeout=timeout)


async def collect_iopub(kc, msg_ids, timeout=10):
    "Collect iopub messages for several concurrent requests at once, until each has published idle. Returns {msg_id: [msgs]}."
    outputs = {m: [] for m in msg_ids}
    idle = set()
    for rem in iter_timeout(timeout):
        if len(idle) >= len(msg_ids): break
        try: msg = await kc.get_iopub_msg(timeout=rem)
        except Empty: continue
        if (mid := parent_id(msg)) not in outputs: continue
        outputs[mid].append(msg)
        if msg.get("msg_type") == "status" and msg["content"].get("execution_state") == "idle": idle.add(mid)
    assert len(idle) == len(msg_ids), f"timeout waiting for iopub idle: {sorted(set(msg_ids)-idle)}"
    return outputs


async def input_reply(kc, value):
    "Answer an input_request."
    await kc.input_reply(value)


async def wait_debug_event(kc, event, timeout=10):
    "The next debug_event iopub message for `event`."
    return await wait_iopub(kc, lambda m: m.get("msg_type") == "debug_event" and m["content"].get("event") == event,
        timeout, err=f"debug_event {event} not received")


async def wait_stop(kc, timeout=10):
    "Wait for the debugger to stop at a breakpoint: the stopped debug_event, with a stackTrace fallback."
    try: return await wait_debug_event(kc, "stopped", timeout=timeout/2)
    except AssertionError:
        last = None
        for _ in iter_timeout(timeout):
            reply = await kc.dap.stackTrace(threadId=1)
            if reply.get("success"): return dict(content=dict(body=dict(reason="breakpoint", threadId=1)))
            last = reply
            await asyncio.sleep(0.1)
        raise AssertionError(f"stopped debug_event not received: {last}")
