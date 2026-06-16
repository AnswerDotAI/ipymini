import time

from ipymini.term import MiniStream


def _timed(fn) -> float:
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


def test_many_small_writes_without_newline_are_linear():
    "Progress-bar style output (no trailing newline) must not buffer quadratically."
    n = 500_000

    sink_chunks = []
    live = MiniStream("stdout", None, sink=lambda name, text: sink_chunks.append(text))

    def _write_live():
        for _ in range(n): live.write("x")
        live.flush()

    elapsed = _timed(_write_live)
    assert "".join(sink_chunks) == "x" * n
    assert elapsed < 1.0, f"live buffering took {elapsed:.2f}s for {n} writes; O(n^2) string concat"

    events = []
    buffered = MiniStream("stdout", events)

    def _write_buffered():
        for _ in range(n): buffered.write("x")

    elapsed = _timed(_write_buffered)
    assert sum(len(e["text"]) for e in events) == n
    assert elapsed < 1.0, f"buffered events took {elapsed:.2f}s for {n} writes; O(n^2) string concat"
