import contextvars, threading
from concurrent.futures import ThreadPoolExecutor

from ipymini.term import MiniStream, thread_local_io
from ipymini.term.io import io_state

v = contextvars.ContextVar("ipymini_test_ctx", default=None)


def _texts(events, name): return "".join(e.get("text","") for e in events if e.get("name")==name)


def test_thread_start_captures_context():
    io_state.install()
    res = []
    tok = v.set("thread-ctx")
    try:
        t = threading.Thread(target=lambda: res.append(v.get()))
        t.start()
        t.join(timeout=2)
    finally: v.reset(tok)
    assert res == ["thread-ctx"]


def test_executor_items_use_submit_context():
    "Pooled work items must run in the context captured at submit time, not at worker thread start."
    io_state.install()
    with ThreadPoolExecutor(max_workers=1) as ex:
        tok = v.set("leaky")
        try: ex.submit(lambda: None).result(timeout=2)  # worker thread created while v is set
        finally: v.reset(tok)
        assert ex.submit(v.get).result(timeout=2) is None
        tok = v.set("inner")
        try: assert ex.submit(v.get).result(timeout=2) == "inner"
        finally: v.reset(tok)


def test_executor_print_routes_to_submitter():
    io_state.install()
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(lambda: None).result(timeout=2)  # worker created outside any io context
        events = []
        out = MiniStream("stdout", events)
        err = MiniStream("stderr", events)
        with thread_local_io(shell=None, stdout=out, stderr=err, request_input=None, allow_stdin=False):
            ex.submit(print, "from-pool").result(timeout=2)
        assert "from-pool" in _texts(events, "stdout")
