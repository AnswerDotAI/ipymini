from ..kernel_utils import *


def test_execute_silent_no_output():
    with start_kernel() as (_, kc):
        _, reply, output_msgs = kc.exec_drain("print('hi')", silent=True)
        assert reply["content"]["status"] == "ok"
        assert not any(
            msg["msg_type"] in {"stream", "execute_result", "display_data"} for msg in output_msgs
        )


def test_store_history_false():
    with start_kernel() as (_, kc):
        msg_id1, reply1, _ = kc.exec_drain("1+1")
        assert reply1["content"]["status"] == "ok"
        count1 = reply1["content"]["execution_count"]

        msg_id2, reply2, _ = kc.exec_drain("2+2", store_history=False)
        assert reply2["content"]["status"] == "ok"
        count2 = reply2["content"]["execution_count"]

        msg_id3, reply3, _ = kc.exec_drain("3+3")
        assert reply3["content"]["status"] == "ok"
        count3 = reply3["content"]["execution_count"]

        assert count2 == count1 + 1
        assert count3 == count2


def test_execute_result():
    with start_kernel() as (_, kc):
        _, reply, output_msgs = kc.exec_drain("1+2+3", store_history=False)
        assert reply["content"]["status"] == "ok"
        results = iopub_msgs(output_msgs, "execute_result")
        assert results
        data = results[-1]["content"].get("data", {})
        assert data.get("text/plain") == "6"


def test_execution_count_consistency():
    "execute_input, execute_result, and execute_reply must all have same execution_count."
    with start_kernel() as (_, kc):
        _, reply, outputs = kc.exec_drain("1+1")
        execute_inputs = iopub_msgs(outputs, "execute_input")
        execute_results = iopub_msgs(outputs, "execute_result")
        assert execute_inputs and execute_results
        input_count = execute_inputs[0]["content"]["execution_count"]
        result_count = execute_results[0]["content"]["execution_count"]
        reply_count = reply["content"]["execution_count"]
        assert input_count == result_count == reply_count, f"mismatch: input={input_count}, result={result_count}, reply={reply_count}"


def test_user_expressions():
    with start_kernel() as (_, kc):
        _, reply, _ = kc.exec_drain("a = 10", user_expressions={"x": "a+1", "bad": "1/0"})
        assert reply["content"]["status"] == "ok"
        expr = reply["content"]["user_expressions"]
        assert expr["x"]["status"] == "ok"
        assert expr["x"]["data"]["text/plain"] == "11"
        assert expr["bad"]["status"] == "error"


def test_execute_error():
    with start_kernel() as (_, kc):
        _, reply, output_msgs = kc.exec_drain("1/0", store_history=False)
        assert reply["content"]["status"] == "error"
        errors = iopub_msgs(output_msgs, "error")
        assert errors


def test_stop_on_error_aborts_pending_executes():
    with start_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        msg_id_fail = kc.execute(fail)
        msg_id_hello = kc.execute("print('Hello')")
        msg_id_world = kc.execute("print('world')")

        reply_fail = kc.shell_reply(msg_id_fail)
        assert reply_fail["content"]["status"] == "error"

        reply_hello = kc.shell_reply(msg_id_hello)
        assert reply_hello["content"]["status"] == "aborted"

        reply_world = kc.shell_reply(msg_id_world)
        assert reply_world["content"]["status"] == "aborted"


def test_stop_on_error_false_allows_followup():
    with start_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        msg_id_fail = kc.execute(fail, stop_on_error=False)
        msg_id_ok = kc.execute("1+1")

        reply_fail = kc.shell_reply(msg_id_fail)
        assert reply_fail["content"]["status"] == "error"

        reply_ok = kc.shell_reply(msg_id_ok)
        assert reply_ok["content"]["status"] == "ok"


def test_silent_partial_line_does_not_leak():
    "Buffer from silent print without newline must not appear in later output."
    with start_kernel() as (_, kc):
        # Silent print without newline - should buffer but not leak
        _, reply1, _ = kc.exec_drain("import sys; sys.stdout.write('GHOST')", silent=True)
        assert reply1["content"]["status"] == "ok"

        # Normal print - should only see 'hello', not 'GHOSThello'
        _, reply2, outputs = kc.exec_drain("print('hello')")
        assert reply2["content"]["status"] == "ok"
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(outputs)]
        texts = "".join(t for _, t in streams)
        assert "GHOST" not in texts, f"silent buffer leaked: {texts!r}"
        assert "hello" in texts


def test_stop_on_error_does_not_abort_non_execute():
    with start_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        msg_id_fail = kc.execute(fail)
        msg_id_info = kc.kernel_info()
        msg_id_comm = kc.comm_info()
        msg_id_inspect = kc.inspect("print")

        reply_fail = kc.shell_reply(msg_id_fail)
        assert reply_fail["content"]["status"] == "error"

        reply_info = kc.shell_reply(msg_id_info)
        assert reply_info["content"]["status"] == "ok"

        reply_comm = kc.shell_reply(msg_id_comm)
        assert reply_comm["content"]["status"] == "ok"

        reply_inspect = kc.shell_reply(msg_id_inspect)
        assert reply_inspect["content"]["status"] == "ok"
