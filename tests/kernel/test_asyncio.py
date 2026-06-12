from ..kernel_utils import *

default_timeout = 3


def test_asyncio_features() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("1+1", store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"

        code = (
            "import asyncio, time\n"
            "async def f():\n"
            "    await asyncio.sleep(0.01)\n"
            "    print('ok')\n"
            "asyncio.create_task(f())\n"
            "time.sleep(0.05)\n")
        msg_id = kc.execute(code, store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        pred = lambda m: parent_id(m) == msg_id and m.get("msg_type") == "stream" and "ok" in m.get("content", {}).get("text", "")
        wait_for_msg(kc.get_iopub_msg, pred, timeout=default_timeout, err="expected stdout from create_task")
        # NB: no iopub_drain here - wait_for_msg already consumed this request's idle, so a drain would block until timeout

        reply = kc.dap.initialize(**debug_init_args)
        assert reply.get("success"), f"initialize: {reply}"

        msg_ids = [kc.execute(f"{i}+1", store_history=False) for i in range(5)]
        replies = collect_shell_replies(kc, set(msg_ids))
        for reply in replies.values(): assert reply["content"]["status"] == "ok"
        collect_iopub_outputs(kc, set(msg_ids))

        msg_id = kc.execute("import time; time.sleep(0.5)", store_history=False)
        wait_for_status(kc, "busy")
        kc.interrupt_request(timeout=default_timeout)

        reply = kc.shell_reply(msg_id, timeout=default_timeout)
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        wait_for_status(kc, "idle")

        msg_id = kc.cmd.shutdown_request(restart=False)
        reply = kc.shell_reply(msg_id)
        assert reply["header"]["msg_type"] == "shutdown_reply"
        assert reply["content"]["status"] == "ok"
