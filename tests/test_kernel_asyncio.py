import time
from .kernel_utils import *

TIMEOUT = 3


def test_asyncio_scenario():
    with start_kernel() as (_, kc):
        msg_id = kc.execute("1+1", store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"


def test_asyncio_create_task() -> None:
    with start_kernel() as (_, kc):
        code = (
            "import asyncio, time\n"
            "async def f():\n"
            "    await asyncio.sleep(0.01)\n"
            "    print('ok')\n"
            "asyncio.create_task(f())\n"
            "time.sleep(0.05)\n"
        )
        msg_id = kc.execute(code, store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        outputs = kc.iopub_drain(msg_id)
        streams = iopub_streams(outputs, "stdout")
        assert any("ok" in msg["content"].get("text", "") for msg in streams)
        kc.iopub_drain(msg_id)

        reply = debug_request(kc, "initialize", DEBUG_INIT_ARGS)
        assert reply.get("success"), f"initialize: {reply}"

        msg_ids = [kc.execute(f"{i}+1", store_history=False) for i in range(5)]
        replies = collect_shell_replies(kc, set(msg_ids))
        for reply in replies.values(): assert reply["content"]["status"] == "ok"
        collect_iopub_outputs(kc, set(msg_ids))

        msg_id = kc.execute("import time; time.sleep(0.5)", store_history=False)
        wait_for_status(kc, "busy")
        kc.interrupt_request(timeout=TIMEOUT)

        reply = kc.shell_reply(msg_id, timeout=TIMEOUT)
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        wait_for_status(kc, "idle")

        msg_id = kc.cmd.shutdown_request(restart=False)
        reply = kc.shell_reply(msg_id)
        assert reply["header"]["msg_type"] == "shutdown_reply"
        assert reply["content"]["status"] == "ok"
