import time, asyncio, os
from queue import Empty
from jupyter_client import AsyncKernelClient, KernelManager
from .kernel_utils import build_env, drain_iopub, ensure_separate_process, get_shell_reply, iopub_msgs, start_kernel, wait_for_status


def _send_interrupt_request(kc) -> None:
    interrupt_msg = kc.session.msg("interrupt_request", {})
    kc.control_channel.send(interrupt_msg)
    deadline = time.time() + 2
    interrupt_reply = None
    while time.time() < deadline:
        reply = kc.control_channel.get_msg(timeout=2)
        if reply["parent_header"].get("msg_id") == interrupt_msg["header"]["msg_id"]:
            interrupt_reply = reply
            break
    assert interrupt_reply is not None, "missing interrupt_reply"
    assert interrupt_reply["header"]["msg_type"] == "interrupt_reply"


async def _get_pubs(kc: AsyncKernelClient, timeout: float = 0.2) -> list[dict]:
    res = []
    try:
        while msg := await kc.get_iopub_msg(timeout=timeout): res.append(msg)
    except Empty: pass
    return res


def test_interrupt_request() -> None:
    with start_kernel() as (km, kc):
        for use_control_channel in [False, True]:
            msg_id = kc.execute("import time; time.sleep(1)")
            wait_for_status(kc, "busy")

            if use_control_channel: _send_interrupt_request(kc)
            else: km.interrupt_kernel()

            reply = get_shell_reply(kc, msg_id, timeout=10)
            assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
            assert reply["content"].get("ename") in {"KeyboardInterrupt", "InterruptedError"}, (
                f"interrupt ename: {reply.get('content')}"
            )

            outputs = drain_iopub(kc, msg_id)
            errors = iopub_msgs(outputs, "error")
            assert errors, f"expected iopub error after interrupt, got: {[m.get('msg_type') for m in outputs]}"
            assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", (
                f"interrupt iopub: {errors[-1].get('content')}"
            )


def test_interrupt_request_breaks_sleep() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("import time; time.sleep(5); print('finished')")
        wait_for_status(kc, "busy")
        _send_interrupt_request(kc)
        try: reply = get_shell_reply(kc, msg_id, timeout=2)
        except Exception as exc: raise AssertionError("expected execute_reply after interrupt_request") from exc
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        outputs = drain_iopub(kc, msg_id)
        errors = iopub_msgs(outputs, "error")
        assert errors, f"expected iopub error after interrupt_request, got: {[m.get('msg_type') for m in outputs]}"
        assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", (
            f"interrupt iopub: {errors[-1].get('content')}"
        )


def test_interrupt_request_gateway_pattern() -> None:
    async def _run() -> None:
        env = build_env()
        os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
        km = KernelManager(kernel_name="ipymini")
        km.start_kernel(env=env)
        ensure_separate_process(km)
        kc = AsyncKernelClient(**km.get_connection_info(session=True))
        kc.parent = km
        kc.start_channels()
        await kc.wait_for_ready(timeout=2)
        try:
            msg = kc.session.msg("execute_request", {"code": "import time; time.sleep(1); print('finished')"})
            kc.shell_channel.send(msg)
            msg_id = msg["header"]["msg_id"]
            await asyncio.sleep(0.2)
            interrupt_msg = kc.session.msg("interrupt_request", {})
            kc.control_channel.send(interrupt_msg)
            await asyncio.sleep(0.2)
            pubs = await _get_pubs(kc, timeout=0.2)
            outs = [o for o in pubs if o.get("parent_header", {}).get("msg_id") == msg_id]
            errors = [o for o in outs if o.get("msg_type") == "error"]
            assert errors, f"expected error output after interrupt, got: {[o.get('msg_type') for o in outs]}"
        finally:
            kc.stop_channels()
            km.shutdown_kernel(now=True)

    asyncio.run(_run())
