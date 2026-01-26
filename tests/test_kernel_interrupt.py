import asyncio
from queue import Empty
from jupyter_client import AsyncKernelClient
from .kernel_utils import *


async def _get_pubs(kc: AsyncKernelClient, timeout:float = 0.2)->list[dict]:
    res = []
    try:
        while msg := await kc.get_iopub_msg(timeout=timeout): res.append(msg)
    except Empty: pass
    return res

def _assert_interrupt(kc, msg_id:str, timeout:float = 2):
    reply = kc.shell_reply(msg_id, timeout=timeout)
    assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
    outputs = kc.iopub_drain(msg_id)
    errors = iopub_msgs(outputs, "error")
    assert errors, f"expected iopub error after interrupt_request, got: {[m.get('msg_type') for m in outputs]}"
    assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", (
        f"interrupt iopub: {errors[-1].get('content')}"
    )
    return reply


def test_interrupt_request():
    with start_kernel() as (km, kc):
        for use_control_channel in [False, True]:
            msg_id = kc.execute("import time; time.sleep(1)")
            wait_for_status(kc, "busy")

            if use_control_channel: kc.interrupt_request()
            else: km.interrupt_kernel()

            reply = _assert_interrupt(kc, msg_id, timeout=10)
            assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}, (
                f"interrupt ename: {reply.get('content')}"
            )


def test_interrupt_request_breaks_sleep():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("import time; time.sleep(5); print('finished')")
        wait_for_status(kc, "busy")
        kc.interrupt_request()
        _assert_interrupt(kc, msg_id, timeout=2)


def test_interrupt_request_breaks_asyncio_sleep():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("import asyncio; await asyncio.sleep(5)")
        wait_for_status(kc, "busy")
        kc.interrupt_request()
        _assert_interrupt(kc, msg_id, timeout=2)


def test_interrupt_request_breaks_busy_loop():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("while True: pass")
        wait_for_status(kc, "busy")
        kc.interrupt_request()
        _assert_interrupt(kc, msg_id, timeout=2)


def test_interrupt_request_gateway_pattern():
    async def _run():
        async with start_kernel_async(ready_timeout=2) as (_km, kc):
            cmd = kc.cmd
            msg_id = cmd.execute_request(code="import time; time.sleep(1); print('finished')")
            await asyncio.sleep(0.2)
            await kc.interrupt_request_async(timeout=2)
            await asyncio.sleep(0.2)
            pubs = await _get_pubs(kc, timeout=0.2)
            outs = [o for o in pubs if o.get("parent_header", {}).get("msg_id") == msg_id]
            errors = [o for o in outs if o.get("msg_type") == "error"]
            assert errors, f"expected error output after interrupt, got: {[o.get('msg_type') for o in outs]}"

    asyncio.run(_run())
