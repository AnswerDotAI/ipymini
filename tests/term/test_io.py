import getpass, sys

from ipymini.term import MiniStream, StdinNotImplementedError, thread_local_io


def test_thread_local_io_routes_streams_and_input():
    events = []
    stdout = MiniStream("stdout", events)
    stderr = MiniStream("stderr", events)
    calls = []
    def request_input(prompt: str, password: bool)->str:
        calls.append((prompt, password))
        return "Ada" if not password else "secret"

    with thread_local_io(shell=None, stdout=stdout, stderr=stderr, request_input=request_input, allow_stdin=True):
        print("hello")
        print("oops", file=sys.stderr)
        name = input("Name: ")
        pw = getpass.getpass("Password: ")

    stdout_text = "".join(e["text"] for e in events if e["name"] == "stdout")
    stderr_text = "".join(e["text"] for e in events if e["name"] == "stderr")
    assert "hello" in stdout_text
    assert "oops" in stderr_text
    assert name == "Ada"
    assert pw == "secret"
    assert calls == [("Name: ", False), ("Password: ", True)]


def test_thread_local_io_disallows_input():
    events = []
    stdout = MiniStream("stdout", events)
    stderr = MiniStream("stderr", events)
    def request_input(prompt: str, password: bool)->str: return "ignored"

    with thread_local_io(shell=None, stdout=stdout, stderr=stderr, request_input=request_input, allow_stdin=False):
        ok = False
        try:
            input("Name: ")
            ok = False
            tag = "no error"
        except StdinNotImplementedError:
            ok = True
            tag = "error"
        assert ok
        assert tag == "error"
