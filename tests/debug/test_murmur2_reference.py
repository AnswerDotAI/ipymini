import random

import pytest

murmurhash2 = pytest.importorskip("murmurhash2")

from ipymini.debug.cells import DEBUG_HASH_SEED, murmur2_x86


def test_murmur2_matches_reference_implementation():
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
