import os
from pathlib import Path
from ..aclient import *
from ..kernel_utils import iopub_msgs

async def test_display_data_and_payloads(tmp_path):
    samples = [("from IPython.display import HTML, display; display(HTML('<b>test</b>'))", "text/html"),
        ("from IPython.display import Math, display; display(Math('\\\\frac{1}{2}'))", "text/latex")]

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

    async with mini_kernel(extra_env=extra_env) as (_, kc):
        for code, mime in samples:
            reply, output_msgs = await kc.exec_drain(code, store_history=False)
            assert reply["content"]["status"] == "ok"
            displays = iopub_msgs(output_msgs, "display_data")
            assert displays, "display_data message not found"
            assert any(mime in msg["content"]["data"] for msg in displays)

        code = "ip = get_ipython()\nfor i in range(3):\n   ip.set_next_input('Hello There')\n"
        reply, _ = await kc.exec_drain(code)
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        next_inputs = [pl for pl in payloads if pl["source"] == "set_next_input"]
        assert len(next_inputs) == 1

        reply, _ = await kc.exec_drain("print?")
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        assert len(payloads) == 1
        assert payloads[0]["source"] == "page"
        mimebundle = payloads[0]["data"]
        assert "text/plain" in mimebundle

    root = Path(__file__).resolve().parents[1]
    ipdir = tmp_path / "ipdir_display"
    profile = ipdir / "profile_default"
    profile.mkdir(parents=True)
    config_path = profile / "ipython_kernel_config.py"
    config_path.write_text("c = get_config()\nc.InteractiveShell.display_page = True\n", encoding="utf-8")

    extra_path = os.environ.get("PYTHONPATH", "")
    paths = [str(root)]
    if extra_path: paths.append(extra_path)
    extra_env = dict(IPYTHONDIR=str(ipdir), PYTHONPATH=os.pathsep.join(paths))

    async with mini_kernel(extra_env=extra_env) as (_, kc):
        reply, outputs = await kc.exec_drain("print?")
        assert reply["content"]["status"] == "ok"
        payloads = reply["content"]["payload"]
        assert not any(pl.get("source") == "page" for pl in payloads)
        displays = iopub_msgs(outputs, "display_data")
        assert displays, "display_data message not found"
        assert any("text/plain" in msg["content"]["data"] for msg in displays)
