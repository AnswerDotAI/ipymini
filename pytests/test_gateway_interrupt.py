import asyncio, os, time
from queue import Empty

from jupyter_client import AsyncKernelClient, KernelManager

from .kernel_utils import TIMEOUT, build_env, ensure_separate_process


async def _wait_for_status(kc, state: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await kc.get_iopub_msg(timeout=0.2)
        if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == state: return msg
    raise AssertionError(f"timeout waiting for status {state}")


async def _router(kc, waiters: dict, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try: msg = await kc.get_shell_msg(timeout=0.1)
        except Empty: continue
        except asyncio.CancelledError: break
        except Exception: continue
        parent_id = msg.get("parent_header", {}).get("msg_id")
        waiter = waiters.get(parent_id)
        if waiter is not None: waiter.put_nowait(msg)


async def _send_wait(kc, waiters: dict, code: str, timeout: float) -> tuple[str, dict]:
    msg_id = kc.execute(code)
    q = asyncio.Queue()
    waiters[msg_id] = q
    try: return msg_id, await asyncio.wait_for(q.get(), timeout=timeout)
    finally: waiters.pop(msg_id, None)


def test_send_wait_after_interrupt() -> None:
    async def _run() -> None:
        timeout = 2
        env = build_env()
        os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
        km = KernelManager(kernel_name="ipymini")
        km.start_kernel(env=env)
        ensure_separate_process(km)
        kc = AsyncKernelClient(**km.get_connection_info(session=True))
        kc.parent = km
        kc.start_channels()
        await kc.wait_for_ready(timeout=timeout)
        waiters = {}
        stop = asyncio.Event()
        router_task = asyncio.create_task(_router(kc, waiters, stop))
        try:
            task = asyncio.create_task(_send_wait(kc, waiters, "import time; time.sleep(0.5)", timeout=timeout))
            await _wait_for_status(kc, "busy", timeout=timeout)
            km.interrupt_kernel()
            _, reply = await task
            assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"

            _, reply = await _send_wait(kc, waiters, "1+1", timeout=timeout)
            assert reply["content"]["status"] == "ok", f"followup reply: {reply.get('content')}"
        finally:
            stop.set()
            router_task.cancel()
            await asyncio.gather(router_task, return_exceptions=True)
            kc.stop_channels()
            km.shutdown_kernel(now=True)

    asyncio.run(_run())
