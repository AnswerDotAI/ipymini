from IPython.core.interactiveshell import InteractiveShell

from ipymini.term import IPythonCapture


def _shell():
    InteractiveShell.clear_instance()
    return InteractiveShell.instance()


def test_ipython_capture_live_stream_sender_emits_lines_and_flushes():
    shell = _shell()
    cap = IPythonCapture(shell, request_input=lambda prompt, password: "x")
    seen = []
    cap.set_stream_sender(lambda name, text: seen.append((name, text)))

    with cap.capture(allow_stdin=False, silent=False):
        res = shell.run_cell("import sys\nsys.stdout.write('alpha\\n')\nsys.stdout.write('beta')\n")
        assert res.success

    assert seen == [("stdout", "alpha\n"), ("stdout", "beta")]
    snap = cap.snapshot()
    assert snap["streams"] == []


def test_ipython_capture_live_display_sender_emits_events():
    shell = _shell()
    cap = IPythonCapture(shell, request_input=lambda prompt, password: "x")
    seen = []
    cap.set_display_sender(lambda ev: seen.append(ev))

    with cap.capture(allow_stdin=False, silent=False):
        res = shell.run_cell("from IPython.display import display, clear_output\nclear_output(wait=True)\ndisplay('hi')\n")
        assert res.success

    assert any(ev.get("type") == "clear_output" for ev in seen)
    assert any(ev.get("type") == "display" for ev in seen)
    snap = cap.snapshot()
    assert snap["display"] == []


def test_ipython_capture_buffered_display_and_streams_when_no_senders():
    shell = _shell()
    cap = IPythonCapture(shell, request_input=lambda prompt, password: "x")

    with cap.capture(allow_stdin=False, silent=False):
        res = shell.run_cell("from IPython.display import display\nprint('hello')\ndisplay({'a': 1})\n")
        assert res.success

    snap = cap.snapshot()
    assert any("hello" in m.get("text","") for m in snap["streams"])
    assert any(ev.get("type") == "display" for ev in snap["display"])
