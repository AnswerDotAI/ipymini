from ipymini.term import MiniStream


def test_ministream_interleaves_events_by_stream():
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


def test_ministream_decodes_bytes():
    events = []
    out = MiniStream("stdout", events)
    out.write(b"hi")
    assert events == [{"name": "stdout", "text": "hi"}]
