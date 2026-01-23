from ipymini.bridge import MiniStream


def test_stream_appends_event() -> None:
    events = []
    out = MiniStream("stdout", events)
    out.write("hi")
    assert events == [{"name": "stdout", "text": "hi"}]


def test_stream_coalesces_same_stream() -> None:
    events = []
    out = MiniStream("stdout", events)
    out.write("a")
    out.write("b")
    assert events == [{"name": "stdout", "text": "ab"}]


def test_stream_interleaves_by_stream() -> None:
    events = []
    out = MiniStream("stdout", events)
    err = MiniStream("stderr", events)
    out.write("a")
    err.write("b")
    out.write("c")
    assert events == [
        {"name": "stdout", "text": "a"},
        {"name": "stderr", "text": "b"},
        {"name": "stdout", "text": "c"},
    ]


def test_stream_bytes_are_decoded() -> None:
    events = []
    out = MiniStream("stdout", events)
    out.write(b"hi")
    assert events == [{"name": "stdout", "text": "hi"}]
