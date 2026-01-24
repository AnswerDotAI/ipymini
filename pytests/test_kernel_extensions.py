import os
from pathlib import Path

from .kernel_utils import drain_iopub, get_shell_reply, start_kernel

def test_ipython_extensions_and_startup(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    ipdir = tmp_path / "ipdir"
    profile = ipdir / "profile_default"
    startup_dir = profile / "startup"
    startup_dir.mkdir(parents=True)

    ext_path = tmp_path / "extmod.py"
    ext_path.write_text("def load_ipython_extension(ip):\n    ip.user_ns['EXT_LOADED'] = 'ok'\n", encoding="utf-8")

    exec_file = tmp_path / "exec_file.py"
    exec_file.write_text("EXEC_FILE = 'ok'\n", encoding="utf-8")

    startup_file = startup_dir / "00-startup.py"
    startup_file.write_text("STARTUP_FILE = 'ok'\n", encoding="utf-8")

    python_startup = tmp_path / "pythonstartup.py"
    python_startup.write_text("PY_STARTUP = 'ok'\n", encoding="utf-8")

    config_path = profile / "ipython_kernel_config.py"
    config = (
        "c = get_config()\n"
        "c.InteractiveShellApp.extensions = ['extmod']\n"
        "c.InteractiveShellApp.exec_lines = ['EXEC_LINE = 123']\n"
        f"c.InteractiveShellApp.exec_files = [{str(exec_file)!r}]\n"
        "c.InteractiveShellApp.exec_PYTHONSTARTUP = True\n"
    )
    config_path.write_text(config, encoding="utf-8")

    extra_path = os.environ.get("PYTHONPATH", "")
    pythonpath = os.pathsep.join([str(tmp_path), str(root), extra_path]) if extra_path else os.pathsep.join([str(tmp_path), str(root)])
    extra_env = dict(IPYTHONDIR=str(ipdir), PYTHONSTARTUP=str(python_startup), PYTHONPATH=pythonpath)

    with start_kernel(extra_env=extra_env) as (_, kc):
        code = (
            "assert EXT_LOADED == 'ok'\n"
            "assert EXEC_LINE == 123\n"
            "assert EXEC_FILE == 'ok'\n"
            "assert STARTUP_FILE == 'ok'\n"
            "assert PY_STARTUP == 'ok'\n"
        )
        msg_id = kc.execute(code, store_history=False)
        reply = get_shell_reply(kc, msg_id)
        if reply["content"]["status"] != "ok":
            raise AssertionError(f"execute failed: {reply['content']}")
        drain_iopub(kc, msg_id)
