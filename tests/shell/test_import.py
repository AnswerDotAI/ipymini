from ipymini import shell


def test_import():
    assert shell.__version__
    assert shell.MiniShell
