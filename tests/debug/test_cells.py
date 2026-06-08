import random, pytest

from ipymini.debug.cells import DEBUG_HASH_SEED, debug_cell_filename, debug_tmp_directory, murmur2_x86


def test_debug_cells(monkeypatch):
    assert murmur2_x86("abc", DEBUG_HASH_SEED) == 3350977461
    monkeypatch.setenv("IPYMINI_CELL_NAME", "/tmp/custom_cell.py")
    assert debug_cell_filename("print('x')") == "/tmp/custom_cell.py"

    monkeypatch.delenv("IPYMINI_CELL_NAME", raising=False)
    name = debug_cell_filename("print('y')")
    tmp_dir = debug_tmp_directory()
    assert name.startswith(tmp_dir)
    assert name.endswith(".py")

    murmurhash2 = pytest.importorskip("murmurhash2")
    rng = random.Random(0)
    seeds = [0, 1, 42, DEBUG_HASH_SEED, 0xFFFFFFFF]
    samples = ["", "a", "hello", "Hello, world!", "pi=3.14159", "emoji: :D"]
    for _ in range(100):
        size = rng.randrange(0, 64)
        samples.append("".join(chr(rng.randrange(32, 127)) for _ in range(size)))
    for seed in seeds:
        for text in samples:
            ours = murmur2_x86(text, seed) & 0xFFFFFFFF
            ref = murmurhash2.murmurhash2(text.encode("utf-8"), seed) & 0xFFFFFFFF
            assert ours == ref
