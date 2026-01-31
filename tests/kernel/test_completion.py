from ..kernel_utils import *


def test_completion_samples():
    with start_kernel() as (_, kc):
        msg_id = kc.complete("pri")
        reply = kc.shell_reply(msg_id)
        matches = set(reply["content"].get("matches", []))
        assert "print" in matches

        msg_id = kc.complete("from sys imp")
        reply = kc.shell_reply(msg_id)
        matches = set(reply["content"].get("matches", []))
        assert "import " in matches


def test_is_complete_samples():
    with start_kernel() as (_, kc):
        msg_id = kc.is_complete("print('hello, world')")
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "complete"

        msg_id = kc.is_complete("print('''hello")
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "incomplete"

        msg_id = kc.is_complete("import = 7q")
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "invalid"
