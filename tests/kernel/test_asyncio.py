import asyncio
from uuid import uuid4
from ..aclient import *

default_timeout = 3


async def test_asyncio_features() -> None:
    async with mini_kernel() as (_, kc):
        reply = await kc.execute("1+1", store_history=False, reply=True, timeout=default_timeout)
        assert reply["content"]["status"] == "ok"

        code = (
            "import asyncio, time\n"
            "async def f():\n"
            "    await asyncio.sleep(0.01)\n"
            "    print('ok')\n"
            "asyncio.create_task(f())\n"
            "time.sleep(0.05)\n")
        mid = str(uuid4())  # explicit msg_id: we need it before the reply, to watch iopub mid-execution
        reply = await kc.execute(code, store_history=False, reply=True, timeout=default_timeout, msg_id=mid)
        assert reply["content"]["status"] == "ok"
        pred = lambda m: parent_id(m) == mid and m.get("msg_type") == "stream" and "ok" in m.get("content", {}).get("text", "")
        await wait_iopub(kc, pred, timeout=default_timeout, err="expected stdout from create_task")
        # NB: no iopub_drain here - wait_iopub already consumed this request's idle, so a drain would block until timeout

        r = await kc.ctl.debug(type="request", seq=1, command="initialize", arguments=debug_init_args, timeout=default_timeout)
        assert r["content"].get("success"), f"initialize: {r['content']}"

        mids = [str(uuid4()) for _ in range(5)]
        cs = [kc.execute(f"{i}+1", store_history=False, reply=True, timeout=default_timeout, msg_id=mid) for i, mid in enumerate(mids)]
        for r in await asyncio.gather(*cs): assert r["content"]["status"] == "ok"
        await collect_iopub(kc, mids)

        mid = str(uuid4())
        c = kc.execute("import time; time.sleep(0.5)", store_history=False, reply=True, timeout=default_timeout, msg_id=mid)
        await wait_status(kc, "busy")
        await kc.interrupt(timeout=default_timeout)

        reply = await c
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        await wait_status(kc, "idle")

        reply = await kc.cmd.shutdown(restart=False, timeout=default_timeout)
        assert reply["header"]["msg_type"] == "shutdown_reply"
        assert reply["content"]["status"] == "ok"
