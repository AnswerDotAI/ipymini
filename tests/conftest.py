import os

import pytest

import ipymini.shell.shell as _shell_mod
from ipymini.shell import MiniShell
from IPython.core.interactiveshell import InteractiveShell
from .kernel_utils import KernelHarness


@pytest.fixture
def minishell(tmp_path, monkeypatch):  # Isolate IPython config/history per test.
    ipdir = tmp_path/"ipython"
    monkeypatch.setenv("IPYTHONDIR", str(ipdir))
    _shell_mod.startup_done = False
    InteractiveShell.clear_instance()
    sh = MiniShell(request_input=lambda prompt, password: "x")
    yield sh
    InteractiveShell.clear_instance()


@pytest.fixture
def kernel_harness():
    with KernelHarness() as harness:
        yield harness
