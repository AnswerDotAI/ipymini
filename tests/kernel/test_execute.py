import asyncio
from ..aclient import *

async def test_execute_features():
    async with mini_kernel() as (_, kc):
        reply, output_msgs = await kc.exec_drain("print('hi')", silent=True)
        assert reply["content"]["status"] == "ok"
        assert not any(msg["msg_type"] in {"stream", "execute_result", "display_data"} for msg in output_msgs)

        reply1, _ = await kc.exec_drain("1+1")
        assert reply1["content"]["status"] == "ok"
        count1 = reply1["content"]["execution_count"]

        reply2, _ = await kc.exec_drain("2+2", store_history=False)
        assert reply2["content"]["status"] == "ok"
        count2 = reply2["content"]["execution_count"]

        reply3, _ = await kc.exec_drain("3+3")
        assert reply3["content"]["status"] == "ok"
        count3 = reply3["content"]["execution_count"]

        assert count2 == count1 + 1
        assert count3 == count2

        reply, output_msgs = await kc.exec_drain("1+2+3", store_history=False)
        assert reply["content"]["status"] == "ok"
        results = iopub_msgs(output_msgs, "execute_result")
        assert results
        data = results[-1]["content"].get("data", {})
        assert data.get("text/plain") == "6"

        reply, outputs = await kc.exec_drain("1+1")
        execute_inputs = iopub_msgs(outputs, "execute_input")
        execute_results = iopub_msgs(outputs, "execute_result")
        assert execute_inputs and execute_results
        input_count = execute_inputs[0]["content"]["execution_count"]
        result_count = execute_results[0]["content"]["execution_count"]
        reply_count = reply["content"]["execution_count"]
        assert input_count == result_count == reply_count, f"mismatch: input={input_count}, result={result_count}, reply={reply_count}"

        reply, _ = await kc.exec_drain("a = 10", user_expressions={"x": "a+1", "bad": "1/0"})
        assert reply["content"]["status"] == "ok"
        expr = reply["content"]["user_expressions"]
        assert expr["x"]["status"] == "ok"
        assert expr["x"]["data"]["text/plain"] == "11"
        assert expr["bad"]["status"] == "error"

        reply, output_msgs = await kc.exec_drain("1/0", store_history=False)
        assert reply["content"]["status"] == "error"
        errors = iopub_msgs(output_msgs, "error")
        assert errors

        reply1, _ = await kc.exec_drain("import sys; sys.stdout.write('GHOST')", silent=True)
        assert reply1["content"]["status"] == "ok"

        reply2, outputs = await kc.exec_drain("print('hello')")
        assert reply2["content"]["status"] == "ok"
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(outputs)]
        texts = "".join(t for _, t in streams)
        assert "GHOST" not in texts, f"silent buffer leaked: {texts!r}"
        assert "hello" in texts


async def test_stop_on_error_features():
    async with mini_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        # default fail_pending=False: the kernel aborts the queued cells (wire stop_on_error=True)
        # and their real kernel-issued "aborted" replies come back to each await
        c_fail = kc.execute(fail, reply=True, timeout=10)
        c_hello = kc.execute("print('Hello')", reply=True, timeout=10)
        c_world = kc.execute("print('world')", reply=True, timeout=10)
        reply_fail, reply_hello, reply_world = await asyncio.gather(c_fail, c_hello, c_world)
        assert reply_fail["content"]["status"] == "error"
        assert reply_hello["content"]["status"] == "aborted"
        assert reply_world["content"]["status"] == "aborted"

        c_fail = kc.execute(fail, reply=True, timeout=10, stop_on_error=False)
        c_ok = kc.execute("1+1", reply=True, timeout=10)
        reply_fail, reply_ok = await asyncio.gather(c_fail, c_ok)
        assert reply_fail["content"]["status"] == "error"
        assert reply_ok["content"]["status"] == "ok"

        c_fail = kc.execute(fail, reply=True, timeout=10)
        c_info = kc.shell_request("kernel_info_request")
        c_comm = kc.shell_request("comm_info_request")
        c_inspect = kc.cmd.inspect(code="print", cursor_pos=5)
        reply_fail, reply_info, reply_comm, reply_inspect = await asyncio.gather(c_fail, c_info, c_comm, c_inspect)
        assert reply_fail["content"]["status"] == "error"
        assert reply_info["content"]["status"] == "ok"
        assert reply_comm["content"]["status"] == "ok"
        assert reply_inspect["content"]["status"] == "ok"

        # fail_pending=None follows stop_on_error: the erroring request fails the other pending
        # reply client-side with a RuntimeError naming the root cause (solveit's pipelining behavior)
        c_fail = kc.execute(fail, reply=True, timeout=10, fail_pending=None)
        c_dead = kc.execute("print('never seen')", reply=True, timeout=10)
        results = await asyncio.gather(c_fail, c_dead, return_exceptions=True)
        assert results[0]["content"]["status"] == "error"
        assert isinstance(results[1], RuntimeError) and "boom" in str(results[1])
