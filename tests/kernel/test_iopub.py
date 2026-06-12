import pytest
from pathlib import Path
from ..kernel_utils import *


def test_iopub_display_and_ordering():
    with start_kernel() as (_, kc):
        code = (
            "import base64\n"
            "from IPython.display import Image, display\n"
            "data = base64.b64decode(\n"
            "    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/'\n"
            "    '6XG2+QAAAAASUVORK5CYII='\n"
            ")\n"
            "display(Image(data=data))\n")
        _, reply, output_msgs = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected display_data from image display"
        data = displays[0]["content"].get("data", {})
        assert "image/png" in data

        code = "print('hi')\nfrom IPython.display import display\n\ndisplay({'x': 1})\n"
        _, reply, output_msgs = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        msg_types = [msg.get("msg_type") for msg in output_msgs]
        assert "execute_input" in msg_types, f"missing execute_input: {msg_types}"
        idx_input = msg_types.index("execute_input")
        if "stream" in msg_types: assert idx_input < msg_types.index("stream")
        if "display_data" in msg_types: assert idx_input < msg_types.index("display_data")

        pytest.importorskip("matplotlib")
        code = (
            "import matplotlib\n"
            "matplotlib.use('module://matplotlib_inline.backend_inline')\n"
            "backend = matplotlib.get_backend()\n"
            "assert 'inline' in backend.lower()\n")
        _, reply, _ = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"


def test_iopub_status_not_dropped_when_output_queue_is_full():
    with start_kernel(extra_env={"IPYMINI_IOPUB_QMAX": "1"}) as (_, kc):
        msg_id = kc.execute("for i in range(200): print(i)", store_history=False)
        reply = kc.shell_reply(msg_id, timeout=default_timeout)
        assert reply["content"]["status"] == "ok"
        outputs = kc.iopub_drain(msg_id, timeout=default_timeout)
        states = [m["content"]["execution_state"] for m in iopub_msgs(outputs, "status")]
        assert "idle" in states, f"missing idle from iopub states: {states}"


@pytest.mark.slow
def test_matplotlib_inline_default_backend():
    import matplotlib
    cache_dir = Path(matplotlib.get_cachedir())
    assert any(cache_dir.glob("fontlist-v*.json")), f"matplotlib font cache not built: {cache_dir}"
    with start_kernel() as (_, kc):
        code = (
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3], [1, 4, 9])\n"
            "plt.gcf()\n")
        _, reply, output_msgs = kc.exec_drain(code, store_history=False, timeout=20)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected display_data from matplotlib inline backend"
        data = displays[-1]["content"].get("data", {})
        assert any(key in data for key in ("image/png", "image/svg+xml"))
