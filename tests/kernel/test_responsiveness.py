import asyncio, time
from ..aclient import *

async def test_control_reply_not_blocked_by_long_execute():
    async with mini_kernel() as (_, kc):
        c = kc.execute("import time; time.sleep(0.8); 'done'", reply=True, timeout=5, store_history=False)
        await asyncio.sleep(0.05)

        t0 = time.monotonic()
        ctl = await kc.ctl.list_subshell()
        elapsed = time.monotonic() - t0

        assert ctl["content"]["status"] == "ok"
        assert elapsed < 0.5, f"control reply too slow: {elapsed:.2f}s"

        reply = await c
        await kc.iopub_drain(parent_id(reply))
