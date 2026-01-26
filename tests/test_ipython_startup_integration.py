import os
from pathlib import Path
from .kernel_utils import *


def test_ipython_startup_integration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ipdir = tmp_path / "ipdir"
    profile = ipdir / "profile_default"
    profile.mkdir(parents=True)
    startup_dir = profile / "startup"
    startup_dir.mkdir()

    ext_path = tmp_path / "extmod.py"
    ext_path.write_text("def load_ipython_extension(ip):\n    ip.user_ns['EXT_LOADED'] = 'ok'\n", encoding="utf-8")

    startup_path = startup_dir / "00-startup.py"
    startup_path.write_text("STARTUP_OK = 456\n", encoding="utf-8")

    config_path = profile / "ipython_kernel_config.py"
    config = """c = get_config()
c.InteractiveShellApp.extensions = ['extmod']
c.InteractiveShellApp.exec_lines = ['EXEC_LINE = 123']
"""
    config_path.write_text(config, encoding="utf-8")

    extra_path = os.environ.get("PYTHONPATH", "")
    paths = [str(tmp_path), str(root)]
    if extra_path: paths.append(extra_path)
    pythonpath = os.pathsep.join(paths)
    extra_env = dict(IPYTHONDIR=str(ipdir), PYTHONPATH=pythonpath)

    with KernelHarness(extra_env=extra_env) as h:
        kc = h.kc
        code = """assert EXT_LOADED == 'ok'
assert EXEC_LINE == 123
assert STARTUP_OK == 456
"""
        msg_id = kc.execute(code, store_history=False)
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok"
        kc.iopub_drain(msg_id)
