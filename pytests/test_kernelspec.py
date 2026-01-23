import json, os
from jupyter_client.kernelspec import KernelSpecManager
from .kernel_utils import ROOT, _build_env


def test_kernelspec_file() -> None:
    spec_path = ROOT / "share" / "jupyter" / "kernels" / "ipymini" / "kernel.json"
    assert spec_path.exists()
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    argv = data["argv"]
    assert argv[0] == "python"
    assert "-m" in argv
    assert "ipymini" in argv
    assert argv[-2:] == ["-f", "{connection_file}"]
    assert data["display_name"] == "IPyMini"
    assert data["language"] == "python"
    assert data["interrupt_mode"] == "signal"


def test_kernelspec_manager_discovers_spec() -> None:
    env = _build_env()
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    ksm = KernelSpecManager()
    spec = ksm.get_kernel_spec("ipymini")
    assert spec.argv[0] == "python"
    assert "-m" in spec.argv
    assert "ipymini" in spec.argv
