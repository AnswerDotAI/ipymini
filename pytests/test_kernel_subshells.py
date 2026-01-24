import time, random
from .kernel_utils import collect_iopub_outputs, collect_shell_replies, drain_iopub, get_shell_reply, iopub_msgs, iopub_streams, start_kernel


TIMEOUT = 10


def _control_request(kc, msg_type: str, content=None):
    msg = kc.session.msg(msg_type, content or {})
    kc.control_channel.send(msg)
    reply = kc.control_channel.get_msg(timeout=TIMEOUT)
    assert reply["header"]["msg_type"] == msg_type.replace("_request", "_reply")
    assert reply["parent_header"].get("msg_id") == msg["header"]["msg_id"]
    return reply


def _create_subshell(kc) -> str:
    reply = _control_request(kc, "create_subshell_request")
    assert reply["content"]["status"] == "ok"
    subshell_id = reply["content"].get("subshell_id")
    assert subshell_id
    return subshell_id


def _list_subshells(kc):
    reply = _control_request(kc, "list_subshell_request")
    assert reply["content"]["status"] == "ok"
    return reply["content"].get("subshell_id", [])


def _delete_subshell(kc, subshell_id: str) -> None:
    reply = _control_request(kc, "delete_subshell_request", {"subshell_id": subshell_id})
    assert reply["content"]["status"] == "ok"


def _execute(kc, code: str, subshell_id: str | None = None, **content):
    payload = {"code": code}
    payload.update(content)
    msg = kc.session.msg("execute_request", payload)
    _send_subshell(kc, msg, subshell_id)
    reply = get_shell_reply(kc, msg["header"]["msg_id"])
    outputs = drain_iopub(kc, msg["header"]["msg_id"])
    return msg, reply, outputs


def _send_execute(kc, code: str, subshell_id: str | None = None, **content):
    payload = {"code": code}
    payload.update(content)
    msg = kc.session.msg("execute_request", payload)
    _send_subshell(kc, msg, subshell_id)
    return msg


def _history_tail(kc, subshell_id: str | None, n: int = 1):
    msg = kc.session.msg("history_request", dict(hist_access_type="tail", n=n, output=False, raw=True))
    _send_subshell(kc, msg, subshell_id)
    reply = get_shell_reply(kc, msg["header"]["msg_id"])
    return reply


def _send_subshell(kc, msg, subshell_id: str | None) -> None:
    if subshell_id is not None: msg["header"]["subshell_id"] = subshell_id
    kc.shell_channel.send(msg)


def _last_history_input(reply: dict) -> str | None:
    hist = reply.get("content", {}).get("history") or []
    if not hist: return None
    item = hist[-1]
    if isinstance(item, (list, tuple)) and len(item) >= 3: return item[2]
    return None


def test_subshell_basics() -> None:
    with start_kernel(extra_env={"IPYMINI_EXPERIMENTAL_COMPLETIONS": "0"}) as (_, kc):
        msg_id = kc.kernel_info()
        reply = get_shell_reply(kc, msg_id)
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

        msg = kc.session.msg("execute_request", {"code": "1+1"})
        msg["header"]["subshell_id"] = "missing"
        kc.shell_channel.send(msg)
        reply = get_shell_reply(kc, msg["header"]["msg_id"])
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") == "SubshellNotFound"

        _delete_subshell(kc, subshell_id)
        assert _list_subshells(kc) == []


def test_subshell_concurrency_and_control() -> None:
    with start_kernel() as (_, kc):
        subshell_a = _create_subshell(kc)
        subshell_b = _create_subshell(kc)

        msg = kc.session.msg("execute_request", {"code": "import time; time.sleep(0.05)"})
        kc.shell_channel.send(msg)

        control_reply = _control_request(kc, "create_subshell_request")
        subshell_id = control_reply["content"]["subshell_id"]
        control_date = control_reply["header"]["date"]

        shell_reply = get_shell_reply(kc, msg["header"]["msg_id"])
        shell_date = shell_reply["header"]["date"]
        drain_iopub(kc, msg["header"]["msg_id"])

        _delete_subshell(kc, subshell_id)

        assert control_date < shell_date

        _execute(kc, "import threading; evt = threading.Event()")

        msg_wait = kc.session.msg("execute_request", {"code": "ok = evt.wait(1.0); print(ok)"})
        msg_wait["header"]["subshell_id"] = subshell_a
        kc.shell_channel.send(msg_wait)

        msg_set = kc.session.msg("execute_request", {"code": "evt.set(); print('set')"})
        kc.shell_channel.send(msg_set)

        replies = collect_shell_replies(kc, {msg_wait["header"]["msg_id"], msg_set["header"]["msg_id"]})
        reply_wait = replies[msg_wait["header"]["msg_id"]]
        reply_set = replies[msg_set["header"]["msg_id"]]
        outputs = collect_iopub_outputs(kc, {msg_wait["header"]["msg_id"], msg_set["header"]["msg_id"]})
        outputs_wait = outputs[msg_wait["header"]["msg_id"]]
        outputs_set = outputs[msg_set["header"]["msg_id"]]

        assert reply_wait["content"]["status"] == "ok"
        assert reply_set["content"]["status"] == "ok"
        streams_wait = iopub_streams(outputs_wait)
        assert any("True" in m["content"].get("text", "") for m in streams_wait)
        streams_set = iopub_streams(outputs_set)
        assert any("set" in m["content"].get("text", "") for m in streams_set)

        _execute(kc, "import threading, time; barrier = threading.Barrier(3)")

        def _send(code: str, subshell_id: str | None = None) -> str:
            msg = kc.session.msg("execute_request", {"code": code})
            _send_subshell(kc, msg, subshell_id)
            return msg["header"]["msg_id"]

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

        subshell_a = _create_subshell(kc)
        subshell_b = _create_subshell(kc)

        code = "value = input('prompt> '); print(f'got:{value}')"
        msg_a = kc.session.msg("execute_request", {"code": code, "allow_stdin": True})
        msg_a["header"]["subshell_id"] = subshell_a
        kc.shell_channel.send(msg_a)

        msg_b = kc.session.msg("execute_request", {"code": code, "allow_stdin": True})
        msg_b["header"]["subshell_id"] = subshell_b
        kc.shell_channel.send(msg_b)

        values = {msg_a["header"]["msg_id"]: "alpha", msg_b["header"]["msg_id"]: "beta"}
        stdin_msgs = [kc.get_stdin_msg(timeout=TIMEOUT) for _ in range(2)]
        for stdin_msg in stdin_msgs:
            assert stdin_msg["msg_type"] == "input_request"
            assert stdin_msg["content"]["prompt"] == "prompt> "
            msg_id = stdin_msg["parent_header"].get("msg_id")
            kc.input(values[msg_id])

        msg_ids = {msg_a["header"]["msg_id"], msg_b["header"]["msg_id"]}
        replies = collect_shell_replies(kc, msg_ids)
        outputs = collect_iopub_outputs(kc, msg_ids)

        assert all(reply["content"]["status"] == "ok" for reply in replies.values())
        for msg_id, expected in values.items():
            streams = iopub_streams(outputs[msg_id])
            assert any(f"got:{expected}" in m["content"].get("text", "") for m in streams)

        _delete_subshell(kc, subshell_a)
        _delete_subshell(kc, subshell_b)


def test_subshell_stop_on_error_isolated() -> None:
    with start_kernel() as (_, kc):
        for are_subshells in [(False, True), (True, False), (True, True)]:
            subshell_ids = [_create_subshell(kc) if is_subshell else None for is_subshell in are_subshells]

            msg_ids = []
            msg = _send_execute(kc, "import asyncio; await asyncio.sleep(0.1); raise ValueError()", subshell_id=subshell_ids[0])
            msg_ids.append(msg["header"]["msg_id"])
            msg = _send_execute(kc, "print('hello')", subshell_id=subshell_ids[0])
            msg_ids.append(msg["header"]["msg_id"])
            msg = _send_execute(kc, "print('goodbye')", subshell_id=subshell_ids[0])
            msg_ids.append(msg["header"]["msg_id"])

            msg = _send_execute(kc, "import time; time.sleep(0.15)", subshell_id=subshell_ids[1])
            msg_ids.append(msg["header"]["msg_id"])
            msg = _send_execute(kc, "print('other')", subshell_id=subshell_ids[1])
            msg_ids.append(msg["header"]["msg_id"])

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

            msg = _send_execute(kc, "print('check')", subshell_id=subshell_ids[0])
            reply = get_shell_reply(kc, msg["header"]["msg_id"])
            assert reply["parent_header"].get("subshell_id") == subshell_ids[0]
            assert reply["content"]["status"] == "ok"

            drain_iopub(kc, msg["header"]["msg_id"])

            for subshell_id in subshell_ids:
                if subshell_id: _delete_subshell(kc, subshell_id)


def test_subshell_fuzzes() -> None:
    with start_kernel() as (km, kc):
        code = ("import time, warnings; from IPython.core import completer; "
            "warnings.filterwarnings('ignore', category=completer.ProvisionalCompleterWarning)")

        subshells = [_create_subshell(kc) for _ in range(2)]
        _execute(kc, code)

        msg_ids = set()
        for idx in range(4):
            msg = kc.session.msg("execute_request", {"code": f"time.sleep(0.02); print('parent:{idx}')"})
            kc.shell_channel.send(msg)
            msg_ids.add(msg["header"]["msg_id"])

            for sid in subshells:
                msg = kc.session.msg("execute_request", {"code": f"time.sleep(0.02); print('{sid[:4]}:{idx}')"})
                msg["header"]["subshell_id"] = sid
                kc.shell_channel.send(msg)
                msg_ids.add(msg["header"]["msg_id"])

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
                msg = kc.session.msg("execute_request", {"code": code})
            elif action == "complete":
                code = "rang"
                msg = kc.session.msg("complete_request", {"code": code, "cursor_pos": len(code)})
            elif action == "inspect":
                code = "print"
                msg = kc.session.msg("inspect_request", {"code": code, "cursor_pos": len(code)})
            else: msg = kc.session.msg("history_request", dict(hist_access_type="tail", n=1, output=False, raw=True))
            _send_subshell(kc, msg, subshell_id)
            requests.append((msg["header"]["msg_id"], action))

        msg_ids = {msg_id for msg_id, _ in requests}
        replies = collect_shell_replies(kc, msg_ids)
        assert all(reply["content"]["status"] in {"ok", "error"} for reply in replies.values())

        exec_ids = {msg_id for msg_id, action in requests if action == "execute"}
        if exec_ids:
            outputs = collect_iopub_outputs(kc, exec_ids)
            for msg_id in exec_ids:
                streams = iopub_streams(outputs[msg_id])
                assert streams, f"missing stream output for {msg_id}"

        for sid in subshells: _delete_subshell(kc, sid)

        rng = random.Random(1)
        subshells = [_create_subshell(kc) for _ in range(2)]
        _execute(kc, code)

        msg_ids = set()
        exec_ids = set()
        stdin_expected = {}

        long_msg = _send_execute(kc, "import time; time.sleep(0.4); print('slept')", subshell_id=rng.choice([None, *subshells]))
        msg_ids.add(long_msg["header"]["msg_id"])
        exec_ids.add(long_msg["header"]["msg_id"])

        for idx in range(3):
            sid = rng.choice([None, *subshells])
            msg = _send_execute(kc, "value = input('prompt> '); print(f'got:{value}')", subshell_id=sid, allow_stdin=True)
            msg_id = msg["header"]["msg_id"]
            msg_ids.add(msg_id)
            exec_ids.add(msg_id)
            stdin_expected[msg_id] = f"v{idx}"

        for _ in range(4):
            sid = rng.choice([None, *subshells])
            action = rng.choice(["execute", "complete", "inspect", "history"])
            if action == "execute":
                msg = _send_execute(kc, "print('fast')", subshell_id=sid)
                exec_ids.add(msg["header"]["msg_id"])
            elif action == "complete":
                code = "rang"
                msg = kc.session.msg("complete_request", {"code": code, "cursor_pos": len(code)})
            elif action == "inspect":
                code = "print"
                msg = kc.session.msg("inspect_request", {"code": code, "cursor_pos": len(code)})
            else: msg = kc.session.msg("history_request", dict(hist_access_type="tail", n=1, output=False, raw=True))
            if action != "execute": _send_subshell(kc, msg, sid)
            msg_ids.add(msg["header"]["msg_id"])

        for _ in range(len(stdin_expected)):
            stdin_msg = kc.get_stdin_msg(timeout=TIMEOUT)
            msg_id = stdin_msg["parent_header"].get("msg_id")
            kc.input(stdin_expected[msg_id])

        time.sleep(0.05)
        km.interrupt_kernel()

        replies = collect_shell_replies(kc, msg_ids)
        assert all(reply["content"]["status"] in {"ok", "error", "aborted"} for reply in replies.values())

        if exec_ids:
            outputs = collect_iopub_outputs(kc, exec_ids)
            for msg_id, expected in stdin_expected.items():
                streams = iopub_streams(outputs[msg_id])
                assert any(f"got:{expected}" in m["content"].get("text", "") for m in streams)

        for sid in subshells: _delete_subshell(kc, sid)
