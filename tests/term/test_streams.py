from ipymini.term import MiniStream, coalesce_streams


def test_ministream_behaviors():
    events = []
    stream = MiniStream("stdout", events)
    stream.write("hello")
    stream.write(" world\n")
    stream.write("again")
    # Events are recorded one-per-write; consumers coalesce adjacent same-name events.
    assert coalesce_streams(events) == [dict(name="stdout", text="hello world\nagain")]

    seen = []
    def sink(name: str, text: str): seen.append((name, text))
    stream = MiniStream("stdout", None, sink=sink)
    stream.write("alpha\nbeta\n")
    assert seen == [("stdout", "alpha\nbeta\n")]
    stream.write("gamma\ndelta")
    stream.flush()
    assert seen == [("stdout", "alpha\nbeta\n"), ("stdout", "gamma\ndelta")]

    events = []
    out = MiniStream("stdout", events)
    err = MiniStream("stderr", events)
    out.write("a")
    out.write("a2")
    err.write("b")
    out.write("c")
    assert coalesce_streams(events) == [
        {"name": "stdout", "text": "aa2"},
        {"name": "stderr", "text": "b"},
        {"name": "stdout", "text": "c"}]

    events = []
    out = MiniStream("stdout", events)
    out.write(b"hi")
    assert events == [{"name": "stdout", "text": "hi"}]
