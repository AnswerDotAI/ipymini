from IPython.core.interactiveshell import InteractiveShell

from ipymini.term import IPythonCapture


def _shell():
    InteractiveShell.clear_instance()
    return InteractiveShell.instance()


def test_ipython_capture_features():
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

    shell = _shell()
    cap = IPythonCapture(shell, request_input=lambda prompt, password: "x")

    with cap.capture(allow_stdin=False, silent=False):
        res = shell.run_cell("from IPython.display import display\nprint('hello')\ndisplay({'a': 1})\n1+1\n")
        assert res.success

    snap = cap.snapshot()
    assert any("hello" in m.get("text","") for m in snap["streams"])
    assert any(ev.get("type") == "display" for ev in snap["display"])
    assert snap["result"] is not None

    with cap.capture(allow_stdin=False, silent=True):
        shell.payload_manager.write_payload(dict(source="set_next_input", text="first", replace=False))
        shell.payload_manager.write_payload(dict(source="set_next_input", text="second", replace=False))

    payload = cap.consume_payload()
    next_inputs = [p for p in payload if p.get("source") == "set_next_input"]
    assert len(next_inputs) == 1
    assert next_inputs[0].get("text") == "second"
