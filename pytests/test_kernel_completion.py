from .kernel_utils import get_shell_reply, start_kernel


def test_completion_samples() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.complete("pri")
        reply = get_shell_reply(kc, msg_id)
        matches = set(reply["content"].get("matches", []))
        assert "print" in matches

        msg_id = kc.complete("from sys imp")
        reply = get_shell_reply(kc, msg_id)
        matches = set(reply["content"].get("matches", []))
        assert "import " in matches


def test_is_complete_samples() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.is_complete("print('hello, world')")
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "complete"

        msg_id = kc.is_complete("print('''hello")
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "incomplete"

        msg_id = kc.is_complete("import = 7q")
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "invalid"
