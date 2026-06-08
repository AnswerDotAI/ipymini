from ipymini.term import MiniStream


def test_ministream_behaviors():
    events = []
    stream = MiniStream("stdout", events)
    stream.write("hello")
    stream.write(" world\n")
    stream.write("again")
    assert events == [dict(name="stdout", text="hello world\nagain")]

    seen = []
    def sink(name: str, text: str): seen.append((name, text))
    stream = MiniStream("stdout", None, sink=sink)
    stream.write("alpha\nbeta")
    assert seen == [("stdout", "alpha\n")]
    stream.flush()
    assert seen == [("stdout", "alpha\n"), ("stdout", "beta")]

    events = []
    out = MiniStream("stdout", events)
    err = MiniStream("stderr", events)
    out.write("a")
    err.write("b")
    out.write("c")
    assert events == [
        {"name": "stdout", "text": "a"},
        {"name": "stderr", "text": "b"},
        {"name": "stdout", "text": "c"}]

    events = []
    out = MiniStream("stdout", events)
    out.write(b"hi")
    assert events == [{"name": "stdout", "text": "hi"}]
