from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, start_kernel


def test_display_data_samples() -> None:
    samples = [("from IPython.display import HTML, display; display(HTML('<b>test</b>'))", "text/html"),
        ("from IPython.display import Math, display; display(Math('\\\\frac{1}{2}'))", "text/latex")]
    with start_kernel() as (_, kc):
        for code, mime in samples:
            msg_id = kc.execute(code, store_history=False)
            reply = get_shell_reply(kc, msg_id)
            assert reply["content"]["status"] == "ok"
            output_msgs = drain_iopub(kc, msg_id)
            displays = iopub_msgs(output_msgs, "display_data")
            assert displays, "display_data message not found"
            assert any(mime in msg["content"]["data"] for msg in displays)


def test_pager_payload() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute("print?")
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        assert len(payloads) == 1
        assert payloads[0]["source"] == "page"
        mimebundle = payloads[0]["data"]
        assert "text/plain" in mimebundle
        drain_iopub(kc, msg_id)


def test_set_next_input_single_payload() -> None:
    code = "ip = get_ipython()\nfor i in range(3):\n   ip.set_next_input('Hello There')\n"
    with start_kernel() as (_, kc):
        msg_id = kc.execute(code)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        next_inputs = [pl for pl in payloads if pl["source"] == "set_next_input"]
        assert len(next_inputs) == 1
        drain_iopub(kc, msg_id)
