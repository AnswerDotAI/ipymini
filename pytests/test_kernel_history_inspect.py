from .kernel_utils import execute_and_drain, get_shell_reply, start_kernel


def test_inspect_open() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.inspect("open")
        reply = get_shell_reply(kc, msg_id)
        content = reply["content"]
        assert content["status"] == "ok"
        assert content["found"]
        assert "text/plain" in content.get("data", {})


def test_history_tail_search() -> None:
    with start_kernel() as (_, kc):
        _, reply, _ = execute_and_drain(kc, "1+1")
        assert reply["content"]["status"] == "ok"

        _, reply, _ = execute_and_drain(kc, "2+2")
        assert reply["content"]["status"] == "ok"

        msg_id = kc.history(hist_access_type="tail", n=2, output=False, raw=True)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        assert reply["content"]["history"]

        msg_id = kc.history(hist_access_type="search", pattern="1?2*", output=False, raw=True)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"


def test_history_search_unique_and_n() -> None:
    with start_kernel() as (_, kc):
        for code in ("1+1", "1+2", "1+3", "1+1"):
            _, reply, _ = execute_and_drain(kc, code)
            assert reply["content"]["status"] == "ok"

        msg_id = kc.history(hist_access_type="search", pattern="1+*", output=False, raw=True, unique=True)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        assert len(reply["content"]["history"]) >= 1

        msg_id = kc.history(hist_access_type="search", pattern="1+*", output=False, raw=True, n=3)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        assert len(reply["content"]["history"]) == 3
