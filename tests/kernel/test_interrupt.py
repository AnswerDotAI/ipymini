import asyncio, os, signal, pytest
from uuid import uuid4
from ..aclient import *
from ..kernel_utils import iopub_msgs, kernel_pid

async def _assert_interrupt(kc, c, timeout=2):
    reply = await c
    assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
    outputs = await kc.iopub_drain(parent_id(reply), timeout=timeout)
    errors = iopub_msgs(outputs, "error")
    assert errors, f"expected iopub error after interrupt_request, got: {[m.get('msg_type') for m in outputs]}"
    assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", f"interrupt iopub: {errors[-1].get('content')}"
    return reply


@pytest.mark.slow
async def test_interrupt_request_features():
    async with mini_kernel() as (km, kc):
        for _ in range(2):
            mid = str(uuid4())
            c = kc.execute("import asyncio; await asyncio.sleep(5)", reply=True, timeout=1.5, msg_id=mid)
            await wait_iopub(kc, lambda m: parent_id(m) == mid and m.get("msg_type") == "status"
                and m["content"]["execution_state"] == "busy", timeout=1)
            os.kill(kernel_pid(km), signal.SIGINT)
            await _assert_interrupt(kc, c, timeout=1.5)
            await kc.exec_ok("41+1", timeout=1)

    async with mini_kernel() as (km, kc):
        for use_control_channel in [False, True]:
            mid = str(uuid4())
            c = kc.execute("import time; time.sleep(1)", reply=True, timeout=10, msg_id=mid)
            await wait_status(kc, "busy")

            if use_control_channel:
                assert (await kc.interrupt())["content"]["status"] == "ok"
                assert (await kc.interrupt())["content"]["status"] == "ok"
            else: await km.interrupt_kernel()

            reply = await _assert_interrupt(kc, c, timeout=10)
            assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}, f"interrupt ename: {reply.get('content')}"

    async with mini_kernel() as (_, kc):
        mid = str(uuid4())
        c = kc.execute("import time; time.sleep(5); print('finished')", reply=True, timeout=2, msg_id=mid)
        await wait_status(kc, "busy")
        await kc.interrupt()
        await _assert_interrupt(kc, c, timeout=2)

        mid = str(uuid4())
        c = kc.execute("import asyncio; await asyncio.sleep(5)", reply=True, timeout=2, msg_id=mid)
        await wait_status(kc, "busy")
        await kc.interrupt()
        await _assert_interrupt(kc, c, timeout=2)

        mid = str(uuid4())
        c = kc.execute("while True: pass", reply=True, timeout=2, msg_id=mid)
        await wait_status(kc, "busy")
        await kc.interrupt()
        await _assert_interrupt(kc, c, timeout=2)

    async with mini_kernel() as (_, kc):
        mid = str(uuid4())
        c = kc.execute("import time; time.sleep(1); print('finished')", reply=True, timeout=2, msg_id=mid)
        await asyncio.sleep(0.2)
        await kc.interrupt(timeout=2)
        outputs = await kc.iopub_drain(mid, timeout=2)
        errors = iopub_msgs(outputs, "error")
        assert errors, f"expected error output after interrupt, got: {[o.get('msg_type') for o in outputs]}"
        await c
