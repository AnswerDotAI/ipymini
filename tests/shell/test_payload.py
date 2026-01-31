from ipymini.shell import MiniShell


def test_set_next_input_payload_dedupes():
    shell = MiniShell(request_input=lambda prompt, password: "x")
    shell.capture.consume_payload()
    parent = {"header": {"msg_id": "demo"}}
    def sender(*args, **kwargs): return None

    with shell.execution_context(allow_stdin=False, silent=True, comm_sender=sender, parent=parent):
        shell.ipy.set_next_input("first")
        shell.ipy.set_next_input("second")

    payload = shell.capture.consume_payload()
    next_inputs = [p for p in payload if p.get("source") == "set_next_input"]
    assert len(next_inputs) == 1
    assert next_inputs[0].get("text") == "second"
