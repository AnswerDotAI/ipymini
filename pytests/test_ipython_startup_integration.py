import os
from pathlib import Path
from .kernel_utils import drain_iopub, get_shell_reply, start_kernel


def test_ipython_startup_integration(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    ipdir = tmp_path / "ipdir"
    profile = ipdir / "profile_default"
    profile.mkdir(parents=True)

    ext_path = tmp_path / "extmod.py"
    ext_path.write_text("def load_ipython_extension(ip):\n    ip.user_ns['EXT_LOADED'] = 'ok'\n", encoding="utf-8")

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

    with start_kernel(extra_env=extra_env) as (_, kc):
        code = """assert EXT_LOADED == 'ok'
assert EXEC_LINE == 123
"""
        msg_id = kc.execute(code, store_history=False)
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        drain_iopub(kc, msg_id)
