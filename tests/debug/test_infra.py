import logging

from ipymini.debug.infra import trace_msg


def test_trace_msg_logs(caplog):
    logger = logging.getLogger("ipymini_debug.test")
    msg = dict(header=dict(msg_type="execute_request", msg_id="abc", subshell_id="s1"))
    with caplog.at_level(logging.WARNING, logger="ipymini_debug.test"):
        trace_msg(logger, "prefix", msg, enabled=True)
        mark = "done"
    assert mark == "done"
    assert "prefix" in caplog.text


def test_trace_msg_disabled(caplog):
    logger = logging.getLogger("ipymini_debug.test")
    msg = dict(header=dict(msg_type="execute_request", msg_id="abc", subshell_id="s1"))
    with caplog.at_level(logging.WARNING, logger="ipymini_debug.test"):
        trace_msg(logger, "prefix", msg, enabled=False)
        mark = "done"
    assert mark == "done"
    assert caplog.text == ""
