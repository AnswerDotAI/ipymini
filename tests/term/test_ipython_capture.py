from IPython.core.interactiveshell import InteractiveShell

from ipymini.term import IPythonCapture


def test_ipython_capture_snapshot_and_payload():
    shell = InteractiveShell.instance()
    def request_input(prompt: str, password: bool)->str: return "42"
    cap = IPythonCapture(shell, request_input=request_input)

    with cap.capture(allow_stdin=False, silent=False):
        res = shell.run_cell("from IPython.display import display\nprint('hello')\ndisplay('hi')\n1+1\n")
        assert res.success

    snapshot = cap.snapshot()
    streams = snapshot["streams"]
    display_events = snapshot["display"]
    assert any("hello" in m.get("text", "") for m in streams)
    assert any(ev.get("type") == "display" for ev in display_events)
    assert snapshot["result"] is not None

    with cap.capture(allow_stdin=False, silent=True):
        shell.payload_manager.write_payload(dict(source="set_next_input", text="first", replace=False))
        shell.payload_manager.write_payload(dict(source="set_next_input", text="second", replace=False))

    payload = cap.consume_payload()
    next_inputs = [p for p in payload if p.get("source") == "set_next_input"]
    assert len(next_inputs) == 1
    assert next_inputs[0].get("text") == "second"
