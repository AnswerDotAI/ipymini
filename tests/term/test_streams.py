from ipymini.term import MiniStream


def test_ministream_coalesces_events():
    events = []
    stream = MiniStream("stdout", events)
    stream.write("hello")
    stream.write(" world\n")
    stream.write("again")
    assert events == [dict(name="stdout", text="hello world\nagain")]


def test_ministream_sink_lines_and_flush():
    seen = []
    def sink(name: str, text: str): seen.append((name, text))
    stream = MiniStream("stdout", None, sink=sink)
    stream.write("alpha\nbeta")
    assert seen == [("stdout", "alpha\n")]
    stream.flush()
    assert seen == [("stdout", "alpha\n"), ("stdout", "beta")]
