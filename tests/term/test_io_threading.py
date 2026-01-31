import getpass, sys, threading

from IPython.core import getipython as getipython_mod

from ipymini.term import MiniStream, StdinNotImplementedError, thread_local_io


def _texts(events, name): return "".join(e.get("text","") for e in events if e.get("name")==name)


def test_thread_local_io_isolated_across_threads():
    results = {}
    errors = {}

    def run(tag: str):
        events = []
        out = MiniStream("stdout", events)
        err = MiniStream("stderr", events)

        calls = []
        def request_input(prompt: str, password: bool) -> str:
            calls.append((prompt, password))
            return f"{tag}-pw" if password else f"{tag}-in"

        try:
            with thread_local_io(shell=tag, stdout=out, stderr=err, request_input=request_input, allow_stdin=True):
                assert getipython_mod.get_ipython() == tag
                print(tag)
                print(tag, file=sys.stderr)
                assert input("Name: ") == f"{tag}-in"
                assert getpass.getpass("Password: ") == f"{tag}-pw"
        except BaseException as exc: errors[tag] = exc
        results[tag] = (events, calls)

    t1 = threading.Thread(target=run, args=("A",))
    t2 = threading.Thread(target=run, args=("B",))
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert errors == {}
    for tag in ("A", "B"):
        events, calls = results[tag]
        assert tag in _texts(events, "stdout")
        assert tag in _texts(events, "stderr")
        other = "B" if tag=="A" else "A"
        assert other not in _texts(events, "stdout")
        assert other not in _texts(events, "stderr")
        assert calls == [("Name: ", False), ("Password: ", True)]


def test_thread_local_io_nested_contexts_restore():
    outer = []
    inner = []
    err = []
    out1 = MiniStream("stdout", outer)
    out2 = MiniStream("stdout", inner)
    e = MiniStream("stderr", err)
    def request_input(prompt: str, password: bool) -> str: return "x"

    with thread_local_io(shell=None, stdout=out1, stderr=e, request_input=request_input, allow_stdin=False):
        print("outer1")
        with thread_local_io(shell=None, stdout=out2, stderr=e, request_input=request_input, allow_stdin=False): print("inner1")
        print("outer2")

    assert "outer1" in _texts(outer, "stdout")
    assert "outer2" in _texts(outer, "stdout")
    assert "inner1" in _texts(inner, "stdout")


def test_thread_local_io_input_raises_when_disallowed():
    out = MiniStream("stdout", [])
    err = MiniStream("stderr", [])
    def request_input(prompt: str, password: bool) -> str: return "ignored"

    with thread_local_io(shell=None, stdout=out, stderr=err, request_input=request_input, allow_stdin=False):
        try:
            input("Name: ")
            ok = False
        except StdinNotImplementedError: ok = True
    assert ok
