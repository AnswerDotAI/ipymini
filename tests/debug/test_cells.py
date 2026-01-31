import os

from ipymini.debug.cells import DEBUG_HASH_SEED, debug_cell_filename, debug_tmp_directory, murmur2_x86


def test_murmur2_x86_known_value(): assert murmur2_x86("abc", DEBUG_HASH_SEED) == 3350977461


def test_debug_cell_filename_env_override(monkeypatch):
    monkeypatch.setenv("IPYMINI_CELL_NAME", "/tmp/custom_cell.py")
    name = debug_cell_filename("print('x')")
    assert name == "/tmp/custom_cell.py"


def test_debug_cell_filename_default(tmp_path, monkeypatch):
    monkeypatch.delenv("IPYMINI_CELL_NAME", raising=False)
    name = debug_cell_filename("print('y')")
    tmp_dir = debug_tmp_directory()
    assert name.startswith(tmp_dir)
    assert name.endswith(".py")
