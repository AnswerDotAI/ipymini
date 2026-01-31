from ipymini.debug.dap import Debugger


def test_debugger_terminate_does_not_start():
    dbg = Debugger()
    called = {"start": False}
    def _ensure_started(): called["start"] = True
    dbg._ensure_started = _ensure_started
    reply, events = dbg.process_request(dict(command="terminate"))
    assert called["start"] is False
    assert events == []
    assert reply.get("success") is True


def test_debugger_debuginfo_body():
    dbg = Debugger()
    def _ensure_started(): return None
    dbg._ensure_started = _ensure_started
    dbg.started = True
    dbg.breakpoint_list = {"x.py": [{"line": 10}]}
    reply, _events = dbg.process_request(dict(command="debugInfo"))
    body = reply.get("body", {})
    assert body.get("isStarted") is True
    assert body.get("hashMethod") == "Murmur2"
    assert body.get("breakpoints")[0].get("source") == "x.py"
