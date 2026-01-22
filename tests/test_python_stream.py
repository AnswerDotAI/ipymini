import importlib.util
from pathlib import Path


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "src" / "python_bridge.py"
    spec = importlib.util.spec_from_file_location("python_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stream_appends_event():
    bridge = _load_bridge()
    events = []
    out = bridge.RustStream("stdout", events)
    out.write("hi")
    assert events == [{"name": "stdout", "text": "hi"}]


def test_stream_coalesces_same_stream():
    bridge = _load_bridge()
    events = []
    out = bridge.RustStream("stdout", events)
    out.write("a")
    out.write("b")
    assert events == [{"name": "stdout", "text": "ab"}]


def test_stream_interleaves_by_stream():
    bridge = _load_bridge()
    events = []
    out = bridge.RustStream("stdout", events)
    err = bridge.RustStream("stderr", events)
    out.write("a")
    err.write("b")
    out.write("c")
    assert events == [
        {"name": "stdout", "text": "a"},
        {"name": "stderr", "text": "b"},
        {"name": "stdout", "text": "c"},
    ]


def test_stream_bytes_are_decoded():
    bridge = _load_bridge()
    events = []
    out = bridge.RustStream("stdout", events)
    out.write(b"hi")
    assert events == [{"name": "stdout", "text": "hi"}]
