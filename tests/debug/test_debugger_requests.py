import queue

import pytest

from ipymini.debug.dap import Debugger


class _FakeClient:
    def __init__(self):
        self.next_seq = 1
        self.sent_async = []
        self.sent = []
        self.connected = []
        self.closed = False

    def next_internal_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def connect(self, host: str, port: int): self.connected.append((host, port))

    def close(self): self.closed = True

    def send_request_async(self, request: dict):
        self.sent_async.append(request)
        seq = request.get("seq")
        if not isinstance(seq, int) or seq <= 0:
            seq = self.next_internal_seq()
            request["seq"] = seq
        waiter = queue.Queue()
        waiter.put(dict(type="response", request_seq=seq, success=True, body={}))
        return seq, waiter

    def wait_for_response(self, req_seq: int, waiter: queue.Queue, timeout: float = 10.0) -> dict:
        return waiter.get(timeout=timeout)

    def wait_initialized(self, timeout: float = 5.0) -> bool: return True

    def send_request(self, request: dict, timeout: float = 10.0) -> dict:
        self.sent.append(request)
        return dict(type="response", request_seq=request.get("seq"), success=True, body={})


def test_debugger_attach_rewrites_arguments_without_listen(monkeypatch):
    dbg = Debugger()
    dbg.client = _FakeClient()
    dbg.port = 5678  # avoid debugpy.listen in _ensure_started
    dbg.kernel_modules = ["/k/a.py", "/k/b.py"]
    dbg.just_my_code = False
    dbg.filter_internal_frames = True
    dbg._remove_cleanup_transforms = lambda: None

    req = dict(type="request", command="attach", seq=10, arguments={})
    reply, events = dbg.process_request(req)

    assert reply.get("success") is True
    assert events == []
    assert dbg.client.connected == [(dbg.host, dbg.port)]
    assert dbg.client.sent_async

    sent = dbg.client.sent_async[0]
    args = sent.get("arguments") or {}
    assert args.get("connect", {}).get("host") == dbg.host
    assert args.get("connect", {}).get("port") == dbg.port
    assert args.get("logToFile") is True
    assert "DebugStdLib" in (args.get("debugOptions") or [])
    rules = args.get("rules") or []
    assert rules and all(r.get("include") is False for r in rules)


def test_debugger_setbreakpoints_updates_breakpoint_list():
    dbg = Debugger()
    fake = _FakeClient()
    dbg.client = fake
    dbg.started = True
    dbg._ensure_started = lambda: None

    def send_request(req: dict, timeout: float = 10.0):
        return dict(type="response", request_seq=req.get("seq"), success=True,
            body={"breakpoints": [{"line": 10}, {"line": 20}]})
    fake.send_request = send_request

    req = dict(type="request", command="setBreakpoints", seq=1, arguments={"source": {"path": "x.py"}})
    reply, _events = dbg.process_request(req)
    assert reply.get("success") is True
    assert dbg.breakpoint_list.get("x.py") == [{"line": 10}, {"line": 20}]


def test_debugger_disconnect_resets_session():
    dbg = Debugger()
    fake = _FakeClient()
    dbg.client = fake
    dbg.started = True
    dbg._ensure_started = lambda: None
    dbg._restore_cleanup_transforms = lambda: None

    req = dict(type="request", command="disconnect", seq=1, arguments={})
    reply, _events = dbg.process_request(req)
    assert reply.get("success") is True
    assert fake.closed is True
    assert dbg.started is False


def test_debugger_trace_current_thread_calls_debugpy_once(monkeypatch):
    import ipymini.debug.dap as dap_mod

    calls = []
    monkeypatch.setattr(dap_mod.debugpy, "trace_this_thread", lambda enabled: calls.append(enabled))
    dbg = Debugger()
    dbg.started = True
    dbg.traced_threads.clear()

    dbg.trace_current_thread()
    dbg.trace_current_thread()
    assert calls == [True]
