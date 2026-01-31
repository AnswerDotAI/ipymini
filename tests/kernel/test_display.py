import os
from pathlib import Path
from ..kernel_utils import *


def test_display_data_samples():
    samples = [("from IPython.display import HTML, display; display(HTML('<b>test</b>'))", "text/html"),
        ("from IPython.display import Math, display; display(Math('\\\\frac{1}{2}'))", "text/latex")]
    with start_kernel() as (_, kc):
        for code, mime in samples:
            msg_id = kc.execute(code, store_history=False)
            reply = kc.shell_reply(msg_id)
            assert reply["content"]["status"] == "ok"
            output_msgs = kc.iopub_drain(msg_id)
            displays = iopub_msgs(output_msgs, "display_data")
            assert displays, "display_data message not found"
            assert any(mime in msg["content"]["data"] for msg in displays)


def test_pager_payload(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ipdir = tmp_path / "ipdir"
    profile = ipdir / "profile_default"
    profile.mkdir(parents=True)
    config_path = profile / "ipython_kernel_config.py"
    config_path.write_text("c = get_config()\nc.InteractiveShell.display_page = False\n", encoding="utf-8")

    extra_path = os.environ.get("PYTHONPATH", "")
    paths = [str(root)]
    if extra_path: paths.append(extra_path)
    extra_env = dict(IPYTHONDIR=str(ipdir), PYTHONPATH=os.pathsep.join(paths))

    with KernelHarness(extra_env=extra_env) as h:
        msg_id = h.kc.execute("print?")
        reply = h.kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        assert len(payloads) == 1
        assert payloads[0]["source"] == "page"
        mimebundle = payloads[0]["data"]
        assert "text/plain" in mimebundle
        h.kc.iopub_drain(msg_id)


def test_display_page_true_uses_display_data(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ipdir = tmp_path / "ipdir"
    profile = ipdir / "profile_default"
    profile.mkdir(parents=True)
    config_path = profile / "ipython_kernel_config.py"
    config_path.write_text("c = get_config()\nc.InteractiveShell.display_page = True\n", encoding="utf-8")

    extra_path = os.environ.get("PYTHONPATH", "")
    paths = [str(root)]
    if extra_path: paths.append(extra_path)
    extra_env = dict(IPYTHONDIR=str(ipdir), PYTHONPATH=os.pathsep.join(paths))

    with KernelHarness(extra_env=extra_env) as h:
        msg_id = h.kc.execute("print?")
        reply = h.kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        assert not any(pl.get("source") == "page" for pl in payloads)
        outputs = h.kc.iopub_drain(msg_id)
        displays = iopub_msgs(outputs, "display_data")
        assert displays, "display_data message not found"
        assert any("text/plain" in msg["content"]["data"] for msg in displays)


def test_set_next_input_single_payload():
    code = "ip = get_ipython()\nfor i in range(3):\n   ip.set_next_input('Hello There')\n"
    with start_kernel() as (_, kc):
        msg_id = kc.execute(code)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        next_inputs = [pl for pl in payloads if pl["source"] == "set_next_input"]
        assert len(next_inputs) == 1
        kc.iopub_drain(msg_id)
