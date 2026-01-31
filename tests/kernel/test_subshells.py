import time, random
from ..kernel_utils import *


timeout = 10


def _create_subshell(kc)->str:
    reply = kc.ctl.create_subshell()
    assert reply["content"]["status"] == "ok"
    subshell_id = reply["content"].get("subshell_id")
    assert subshell_id
    return subshell_id


def _list_subshells(kc):
    reply = kc.ctl.list_subshell()
    assert reply["content"]["status"] == "ok"
    return reply["content"].get("subshell_id", [])


def _delete_subshell(kc, subshell_id:str):
    reply = kc.ctl.delete_subshell(subshell_id=subshell_id)
    assert reply["content"]["status"] == "ok"


def _execute(kc, code:str, subshell_id:str|None=None, **content):
    payload = {"code": code}
    payload.update(content)
    msg_id = kc.cmd.execute_request(content=payload, subshell_id=subshell_id)
    reply = kc.shell_reply(msg_id)
    outputs = kc.iopub_drain(msg_id)
    return msg_id, reply, outputs


def _send_execute(kc, code:str, subshell_id:str|None=None, **content):
    payload = {"code": code}
    payload.update(content)
    return kc.cmd.execute_request(content=payload, subshell_id=subshell_id)


def _history_tail(kc, subshell_id:str|None, n:int = 1):
    msg_id = kc.cmd.history_request(hist_access_type="tail", n=n, output=False, raw=True, subshell_id=subshell_id)
    return kc.shell_reply(msg_id)


def _last_history_input(reply: dict)->str|None:
    hist = reply.get("content", {}).get("history") or []
    if not hist: return None
    item = hist[-1]
    if isinstance(item, (list, tuple)) and len(item) >= 3: return item[2]
    return None


def test_subshell_basics():
    with start_kernel(extra_env={"IPYMINI_EXPERIMENTAL_COMPLETIONS": "0"}) as (_, kc):
        msg_id = kc.kernel_info()
        reply = kc.shell_reply(msg_id)
        features = reply["content"].get("supported_features", [])
        assert "kernel subshells" in features

        assert _list_subshells(kc) == []
        subshell_id = _create_subshell(kc)
        assert _list_subshells(kc) == [subshell_id]

        _, reply1, _ = _execute(kc, "a = 10")
        assert reply1["content"]["execution_count"] == 1

        _, reply2, outputs2 = _execute(kc, "a", subshell_id=subshell_id)
        assert reply2["content"]["execution_count"] == 1
        results = iopub_msgs(outputs2, "execute_result")
        assert results, "expected execute_result"
        assert results[0]["content"]["data"].get("text/plain") == "10"
        assert results[0]["parent_header"].get("subshell_id") == subshell_id

        _, reply3, _ = _execute(kc, "a + 1")
        assert reply3["content"]["execution_count"] == 2

        _execute(kc, "parent_only = 123")
        _execute(kc, "child_only = 456", subshell_id=subshell_id)

        parent_hist = _history_tail(kc, None)
        child_hist = _history_tail(kc, subshell_id)

        assert _last_history_input(parent_hist) == "parent_only = 123"
        assert _last_history_input(child_hist) == "child_only = 456"

        msg_id = kc.cmd.execute_request(code="1+1", subshell_id="missing")
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") == "SubshellNotFound"

        _delete_subshell(kc, subshell_id)
        assert _list_subshells(kc) == []


def test_subshell_asyncio_create_task():
    with start_kernel() as (_, kc):
        subshell_id = _create_subshell(kc)
        code = (
            "import asyncio, time\n"
            "async def f():\n"
            "    await asyncio.sleep(0.01)\n"
            "    print('ok')\n"
            "asyncio.create_task(f())\n"
            "time.sleep(0.05)\n"
        )
        msg_id = kc.cmd.execute_request(code=code, subshell_id=subshell_id)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        pred = lambda m: parent_id(m) == msg_id and m.get("msg_type") == "stream" and "ok" in m.get("content", {}).get("text", "")
        wait_for_msg(kc.get_iopub_msg, pred, timeout=timeout, err="expected stdout from subshell create_task")


def test_subshell_concurrency_and_control():
    with start_kernel() as (_, kc):
        cmd = kc.cmd
        subshell_a = _create_subshell(kc)
        subshell_b = _create_subshell(kc)

        msg_id = cmd.execute_request(code="import time; time.sleep(0.05)")

        control_reply = kc.ctl.create_subshell()
        subshell_id = control_reply["content"]["subshell_id"]
        control_date = control_reply["header"]["date"]

        shell_reply = kc.shell_reply(msg_id)
        shell_date = shell_reply["header"]["date"]
        kc.iopub_drain(msg_id)

        _delete_subshell(kc, subshell_id)

        assert control_date < shell_date

        _execute(kc, "import threading; evt = threading.Event()")

        msg_wait_id = cmd.execute_request(code="ok = evt.wait(1.0); print(ok)", subshell_id=subshell_a)
        msg_set_id = cmd.execute_request(code="evt.set(); print('set')")

        replies = collect_shell_replies(kc, {msg_wait_id, msg_set_id})
        reply_wait = replies[msg_wait_id]
        reply_set = replies[msg_set_id]
        outputs = collect_iopub_outputs(kc, {msg_wait_id, msg_set_id})
        outputs_wait = outputs[msg_wait_id]
        outputs_set = outputs[msg_set_id]

        assert reply_wait["content"]["status"] == "ok"
        assert reply_set["content"]["status"] == "ok"
        streams_wait = iopub_streams(outputs_wait)
        assert any("True" in m["content"].get("text", "") for m in streams_wait)
        streams_set = iopub_streams(outputs_set)
        assert any("set" in m["content"].get("text", "") for m in streams_set)


        _execute(kc, "import threading, time; barrier = threading.Barrier(3)")

        def _send(code:str, subshell_id:str|None=None)->str: return cmd.execute_request(code=code, subshell_id=subshell_id)

        msg_parent = _send("barrier.wait(); time.sleep(0.05); print('parent')")
        msg_a = _send("barrier.wait(); time.sleep(0.05); print('a')", subshell_a)
        msg_b = _send("barrier.wait(); time.sleep(0.05); print('b')", subshell_b)

        msg_ids = {msg_parent, msg_a, msg_b}
        replies = collect_shell_replies(kc, msg_ids)
        outputs = collect_iopub_outputs(kc, msg_ids)

        assert all(reply["content"]["status"] == "ok" for reply in replies.values())
        expected = {msg_parent: "parent", msg_a: "a", msg_b: "b"}
        for msg_id, text in expected.items():
            streams = iopub_streams(outputs[msg_id])
            assert any(text in m["content"].get("text", "") for m in streams)

        _delete_subshell(kc, subshell_a)
        _delete_subshell(kc, subshell_b)


def test_subshell_reads_shared_ns_during_parent_sleep():
    with start_kernel() as (_, kc):
        subshell_id = _create_subshell(kc)
        parent_msg_id = kc.execute("x = 123; import time; time.sleep(2); print('done')")
        time.sleep(0.1)

        subshell_msg_id = kc.cmd.execute_request(code="print(x)", subshell_id=subshell_id)

        start = time.monotonic()
        reply = wait_for_msg(kc.get_shell_msg, lambda m: parent_id(m) == subshell_msg_id,
            timeout=0.8, poll=0.2, err="timeout waiting for subshell reply")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"subshell reply took too long: {elapsed:.2f}s"

        outputs = kc.iopub_drain(subshell_msg_id)
        streams = iopub_streams(outputs)
        assert any("123" in m["content"].get("text", "") for m in streams), (
            f"expected subshell to read shared ns, got: {streams}"
        )

        reply_parent = kc.shell_reply(parent_msg_id, timeout=3)
        assert reply_parent["content"]["status"] == "ok"
        outputs_parent = kc.iopub_drain(parent_msg_id)
        streams_parent = iopub_streams(outputs_parent)
        assert any("done" in m["content"].get("text", "") for m in streams_parent)
        _delete_subshell(kc, subshell_id)


def test_subshell_interrupt_request_breaks_sleep():
    with start_kernel() as (_, kc):
        subshell_id = _create_subshell(kc)
        msg_id = _send_execute(kc, "import time; time.sleep(2); print('done')", subshell_id=subshell_id)
        time.sleep(0.1)
        kc.interrupt_request()
        reply = kc.shell_reply(msg_id, timeout=timeout)
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        outputs = kc.iopub_drain(msg_id)
        errors = iopub_msgs(outputs, "error")
        assert errors, f"expected iopub error after interrupt, got: {[m.get('msg_type') for m in outputs]}"
        assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", (
            f"interrupt iopub: {errors[-1].get('content')}"
        )
        _delete_subshell(kc, subshell_id)


def test_subshell_stop_on_error_isolated():
    with start_kernel() as (_, kc):
        for are_subshells in [(False, True), (True, False), (True, True)]:
            subshell_ids = [_create_subshell(kc) if is_subshell else None for is_subshell in are_subshells]

            msg_ids = []
            msg_id = _send_execute(kc, "import asyncio; await asyncio.sleep(0.1); raise ValueError()", subshell_id=subshell_ids[0])
            msg_ids.append(msg_id)
            msg_id = _send_execute(kc, "print('hello')", subshell_id=subshell_ids[0])
            msg_ids.append(msg_id)
            msg_id = _send_execute(kc, "print('goodbye')", subshell_id=subshell_ids[0])
            msg_ids.append(msg_id)

            msg_id = _send_execute(kc, "import time; time.sleep(0.15)", subshell_id=subshell_ids[1])
            msg_ids.append(msg_id)
            msg_id = _send_execute(kc, "print('other')", subshell_id=subshell_ids[1])
            msg_ids.append(msg_id)

            replies = collect_shell_replies(kc, set(msg_ids))

            assert replies[msg_ids[0]]["parent_header"].get("subshell_id") == subshell_ids[0]
            assert replies[msg_ids[1]]["parent_header"].get("subshell_id") == subshell_ids[0]
            assert replies[msg_ids[2]]["parent_header"].get("subshell_id") == subshell_ids[0]
            assert replies[msg_ids[3]]["parent_header"].get("subshell_id") == subshell_ids[1]
            assert replies[msg_ids[4]]["parent_header"].get("subshell_id") == subshell_ids[1]

            assert replies[msg_ids[0]]["content"]["status"] == "error"
            assert replies[msg_ids[1]]["content"]["status"] == "aborted"
            assert replies[msg_ids[2]]["content"]["status"] == "aborted"
            assert replies[msg_ids[3]]["content"]["status"] == "ok"
            assert replies[msg_ids[4]]["content"]["status"] == "ok"

            msg_id = _send_execute(kc, "print('check')", subshell_id=subshell_ids[0])
            reply = kc.shell_reply(msg_id)
            assert reply["parent_header"].get("subshell_id") == subshell_ids[0]
            assert reply["content"]["status"] == "ok"

            kc.iopub_drain(msg_id)

            for subshell_id in subshell_ids:
                if subshell_id: _delete_subshell(kc, subshell_id)


def test_subshell_fuzzes():
    with start_kernel() as (km, kc):
        cmd = kc.cmd
        code = ("import time, warnings; from IPython.core import completer; "
            "warnings.filterwarnings('ignore', category=completer.ProvisionalCompleterWarning)")

        subshells = [_create_subshell(kc) for _ in range(2)]
        _execute(kc, code)

        msg_ids = set()
        for idx in range(4):
            msg_id = cmd.execute_request(code=f"time.sleep(0.02); print('parent:{idx}')")
            msg_ids.add(msg_id)

            for sid in subshells:
                msg_id = cmd.execute_request(code=f"time.sleep(0.02); print('{sid[:4]}:{idx}')", subshell_id=sid)
                msg_ids.add(msg_id)

        replies = collect_shell_replies(kc, msg_ids)
        outputs = collect_iopub_outputs(kc, msg_ids)
        assert all(reply["content"]["status"] == "ok" for reply in replies.values())
        for msg_id, msgs in outputs.items():
            streams = iopub_streams(msgs)
            assert streams, f"missing stream output for {msg_id}"

        for sid in subshells: _delete_subshell(kc, sid)

        rng = random.Random(0)
        subshells = [_create_subshell(kc) for _ in range(3)]
        _execute(kc, "import time")

        requests = []
        for idx in range(20):
            subshell_id = rng.choice([None, *subshells])
            action = rng.choice(["execute", "complete", "inspect", "history"])
            if action == "execute":
                code = f"time.sleep(0.01); print('fuzz:{idx}')"
                msg_id = cmd.execute_request(code=code, subshell_id=subshell_id)
            elif action == "complete":
                code = "rang"
                msg_id = cmd.complete_request(code=code, cursor_pos=len(code), subshell_id=subshell_id)
            elif action == "inspect":
                code = "print"
                msg_id = cmd.inspect_request(code=code, cursor_pos=len(code), subshell_id=subshell_id)
            else: msg_id = cmd.history_request(hist_access_type="tail", n=1, output=False, raw=True, subshell_id=subshell_id)
            requests.append((msg_id, action))

        msg_ids = {msg_id for msg_id, _ in requests}
        replies = collect_shell_replies(kc, msg_ids)
        assert all(reply["content"]["status"] in {"ok", "error"} for reply in replies.values())

        exec_ids = {msg_id for msg_id, action in requests if action == "execute"}
        outputs = collect_iopub_outputs(kc, exec_ids) if exec_ids else {}
        for msg_id in exec_ids:
            streams = iopub_streams(outputs[msg_id])
            assert streams, f"missing stream output for {msg_id}"

        for sid in subshells: _delete_subshell(kc, sid)
