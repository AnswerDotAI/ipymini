from .kernel_utils import execute_and_drain, get_shell_reply, iopub_msgs, start_kernel


def test_execute_silent_no_output() -> None:
    with start_kernel() as (_, kc):
        _, reply, output_msgs = execute_and_drain(kc, "print('hi')", silent=True)
        assert reply["content"]["status"] == "ok"
        assert not any(
            msg["msg_type"] in {"stream", "execute_result", "display_data"} for msg in output_msgs
        )


def test_store_history_false() -> None:
    with start_kernel() as (_, kc):
        msg_id1, reply1, _ = execute_and_drain(kc, "1+1")
        assert reply1["content"]["status"] == "ok"
        count1 = reply1["content"]["execution_count"]

        msg_id2, reply2, _ = execute_and_drain(kc, "2+2", store_history=False)
        assert reply2["content"]["status"] == "ok"
        count2 = reply2["content"]["execution_count"]

        msg_id3, reply3, _ = execute_and_drain(kc, "3+3")
        assert reply3["content"]["status"] == "ok"
        count3 = reply3["content"]["execution_count"]

        assert count2 == count1 + 1
        assert count3 == count2


def test_execute_result() -> None:
    with start_kernel() as (_, kc):
        _, reply, output_msgs = execute_and_drain(kc, "1+2+3", store_history=False)
        assert reply["content"]["status"] == "ok"
        results = iopub_msgs(output_msgs, "execute_result")
        assert results
        data = results[-1]["content"].get("data", {})
        assert data.get("text/plain") == "6"


def test_user_expressions() -> None:
    with start_kernel() as (_, kc):
        _, reply, _ = execute_and_drain(kc, "a = 10", user_expressions={"x": "a+1", "bad": "1/0"})
        assert reply["content"]["status"] == "ok"
        expr = reply["content"]["user_expressions"]
        assert expr["x"]["status"] == "ok"
        assert expr["x"]["data"]["text/plain"] == "11"
        assert expr["bad"]["status"] == "error"


def test_execute_error() -> None:
    with start_kernel() as (_, kc):
        _, reply, output_msgs = execute_and_drain(kc, "1/0", store_history=False)
        assert reply["content"]["status"] == "error"
        errors = iopub_msgs(output_msgs, "error")
        assert errors


def test_stop_on_error_aborts_pending_executes() -> None:
    with start_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        msg_id_fail = kc.execute(fail)
        msg_id_hello = kc.execute("print('Hello')")
        msg_id_world = kc.execute("print('world')")

        reply_fail = get_shell_reply(kc, msg_id_fail)
        assert reply_fail["content"]["status"] == "error"

        reply_hello = get_shell_reply(kc, msg_id_hello)
        assert reply_hello["content"]["status"] == "aborted"

        reply_world = get_shell_reply(kc, msg_id_world)
        assert reply_world["content"]["status"] == "aborted"


def test_stop_on_error_false_allows_followup() -> None:
    with start_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        msg_id_fail = kc.execute(fail, stop_on_error=False)
        msg_id_ok = kc.execute("1+1")

        reply_fail = get_shell_reply(kc, msg_id_fail)
        assert reply_fail["content"]["status"] == "error"

        reply_ok = get_shell_reply(kc, msg_id_ok)
        assert reply_ok["content"]["status"] == "ok"


def test_stop_on_error_does_not_abort_non_execute() -> None:
    with start_kernel() as (_, kc):
        fail = "import time\n" "time.sleep(0.2)\n" "raise ValueError('boom')"
        msg_id_fail = kc.execute(fail)
        msg_id_info = kc.kernel_info()
        msg_id_comm = kc.comm_info()
        msg_id_inspect = kc.inspect("print")

        reply_fail = get_shell_reply(kc, msg_id_fail)
        assert reply_fail["content"]["status"] == "error"

        reply_info = get_shell_reply(kc, msg_id_info)
        assert reply_info["content"]["status"] == "ok"

        reply_comm = get_shell_reply(kc, msg_id_comm)
        assert reply_comm["content"]["status"] == "ok"

        reply_inspect = get_shell_reply(kc, msg_id_inspect)
        assert reply_inspect["content"]["status"] == "ok"
