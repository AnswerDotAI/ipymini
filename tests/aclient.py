"Async kernel stories use ConKernelClient; tests/compat keeps the unpatched Jupyter client."
import asyncio, os
from contextlib import asynccontextmanager
from queue import Empty

from conkernelclient import *
from conkernelclient import reconnect as _reconnect
from jupywire.ops import parent_id, iopub_msgs
from conkernelclient.ops import iter_timeout, iopub_streams

from .kernel_utils import build_env, stream_text

__all__ = ["mini_kernel", "clone", "wait_iopub", "wait_status", "collect_iopub", "gather_outputs", "profile_env",
    "wait_debug_event", "wait_stop", "debug_init_args", "parent_id", "iter_timeout", "iopub_msgs",
    "iopub_streams", "stream_text", "reconnect"]

debug_init_args = dict(clientID="test-client", clientName="testClient", adapterID="", pathFormat="path", linesStartAt1=True,
    columnsStartAt1=True, supportsVariableType=True, supportsVariablePaging=True, supportsRunInTerminalRequest=True, locale="en")


@asynccontextmanager
async def mini_kernel(extra_env=None, **kw):
    "Start an ipymini kernel with the repo kernelspec/env; yield `(km, kc)`."
    env = build_env(extra_env)
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    async with run_kernel(kernel_name="py", env=env, **kw) as (km, kc):
        JmsgQueues(kc)
        yield km, kc


@asynccontextmanager
async def clone(kc):
    "A second ConKernelClient session to the same kernel (its own Session, so its own msg ids)."
    k2 = ConKernelClient()
    k2.load_connection_info(kc.get_connection_info())
    JmsgQueues(k2)
    await k2.start_channels()
    try: yield k2
    finally: k2.stop_channels()


async def reconnect(km, kc=None):
    "Restart a managed kernel and attach the test message queues to its new client."
    kc = await _reconnect(km, kc)
    JmsgQueues(kc)
    return kc


async def wait_iopub(kc, pred, timeout=10, err="iopub message not received"):
    "Return the first iopub message matching `pred`, discarding others."
    for rem in iter_timeout(timeout):
        try: msg = await kc.jmsgq.get("iopub", timeout=rem)
        except Empty: continue
        if pred(msg): return msg
    raise AssertionError(err)


async def wait_status(kc, state, timeout=10):
    "Wait for an iopub status message with the given execution_state."
    return await wait_iopub(kc, lambda m: m.get("msg_type") == "status" and m["content"].get("execution_state") == state,
        timeout, err=f"timeout waiting for status: {state}")


async def collect_iopub(kc, msg_ids, timeout=10):
    "Collect iopub messages for several concurrent requests at once, until each has published idle. Returns {msg_id: [msgs]}."
    outputs = {m: [] for m in msg_ids}
    idle = set()
    for rem in iter_timeout(timeout):
        if len(idle) >= len(msg_ids): break
        try: msg = await kc.jmsgq.get("iopub", timeout=rem)
        except Empty: continue
        if (mid := parent_id(msg)) not in outputs: continue
        outputs[mid].append(msg)
        if msg.get("msg_type") == "status" and msg["content"].get("execution_state") == "idle": idle.add(mid)
    assert len(idle) == len(msg_ids), f"timeout waiting for iopub idle: {sorted(set(msg_ids)-idle)}"
    return outputs


async def gather_outputs(kc, *calls):
    "Await concurrent replies and collect their corresponding output messages."
    replies = await asyncio.gather(*calls)
    outputs = await collect_iopub(kc, [parent_id(r) for r in replies])
    return [(r, outputs[parent_id(r)]) for r in replies]


def profile_env(path, config):
    "Write an IPython profile and make modules beside it importable by the kernel."
    ipdir = path/'ipython'
    profile = ipdir/'profile_default'
    profile.mkdir(parents=True)
    (profile/'ipython_kernel_config.py').write_text(config)
    return dict(IPYTHONDIR=str(ipdir), PYTHONPATH=f'{path}{os.pathsep}{build_env()["PYTHONPATH"]}')


async def wait_debug_event(kc, event, timeout=10):
    "The next debug_event iopub message for `event`."
    return await wait_iopub(kc, lambda m: m.get("msg_type") == "debug_event" and m["content"].get("event") == event,
        timeout, err=f"debug_event {event} not received")


async def wait_stop(kc, timeout=10):
    "The debugger's actual stopped event."
    return await wait_debug_event(kc, "stopped", timeout=timeout)
