import os, time, pytest, zmq
from jupyter_client.session import Session
from .kernel_utils import execute_and_drain, iopub_msgs, load_connection, start_kernel


def test_iopub_welcome() -> None:
    with start_kernel() as (km, _kc):
        conn = load_connection(km)
        session = Session(key=conn.get("key", "").encode(), signature_scheme=conn.get("signature_scheme", "hmac-sha256"))

        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.connect(f"{conn['transport']}://{conn['ip']}:{conn['iopub_port']}")

        deadline = time.time() + 5
        msg = None
        while time.time() < deadline:
            try: frames = sub.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.05)
                continue
            if b"<IDS|MSG>" in frames:
                idx = frames.index(b"<IDS|MSG>")
                msg = session.deserialize(frames[idx + 1 :])
            else: msg = session.deserialize(frames)
            if msg.get("header", {}).get("msg_type") == "iopub_welcome": break
        sub.close(0)

        assert msg is not None, "did not receive any iopub messages"
        assert msg["header"]["msg_type"] == "iopub_welcome"


def test_display_image_png() -> None:
    with start_kernel() as (_, kc):
        code = (
            "import base64\n"
            "from IPython.display import Image, display\n"
            "data = base64.b64decode(\n"
            "    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/'\n"
            "    '6XG2+QAAAAASUVORK5CYII='\n"
            ")\n"
            "display(Image(data=data))\n"
        )
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected display_data from image display"
        data = displays[0]["content"].get("data", {})
        assert "image/png" in data


def test_matplotlib_enable_gui_no_error() -> None:
    pytest.importorskip("matplotlib")
    with start_kernel() as (_, kc):
        code = (
            "import matplotlib\n"
            "matplotlib.use('module://matplotlib_inline.backend_inline')\n"
            "backend = matplotlib.get_backend()\n"
            "assert 'inline' in backend.lower()\n"
        )
        _, reply, _ = execute_and_drain(kc, code, store_history=False)
        assert reply["content"]["status"] == "ok"


@pytest.mark.slow
def test_matplotlib_inline_default_backend(tmp_path) -> None:
    pytest.importorskip("matplotlib")
    cache_dir = tmp_path / "mplconfig"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(cache_dir, os.W_OK): raise AssertionError(f"no writable mpl cache dir: {cache_dir}")
    extra_env = {"MPLCONFIGDIR": str(cache_dir), "XDG_CACHE_HOME": str(cache_dir)}
    with start_kernel(extra_env=extra_env) as (_, kc):
        code = (
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3], [1, 4, 9])\n"
            "plt.gcf()\n"
        )
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False, timeout=3)
        assert reply["content"]["status"] == "ok"
        displays = iopub_msgs(output_msgs, "display_data")
        assert displays, "expected display_data from matplotlib inline backend"
        data = displays[-1]["content"].get("data", {})
        assert any(key in data for key in ("image/png", "image/svg+xml"))
