from .kernel_utils import *


def test_missing_fields_complete_request():
    with start_kernel() as (_, kc):
        msg_id = kc.cmd.complete_request()
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") == "MissingField"


def test_missing_fields_history_request():
    with start_kernel() as (_, kc):
        msg_id = kc.cmd.history_request()
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"].get("ename") == "MissingField"
