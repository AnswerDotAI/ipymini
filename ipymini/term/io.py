import builtins, contextvars, getpass, sys
from contextlib import contextmanager
from typing import Callable

from IPython.core import getipython as _getipython_mod


class StdinNotImplementedError(RuntimeError): pass

class _ThreadLocalStream:
    def __init__(self, name: str, default): self.name, self.default = name, default

    def _target(self): return io_state.get(self.name) or self.default

    def write(self, value)->int:
        t = self._target()
        return 0 if t is None else t.write(value)

    def writelines(self, lines)->int:
        total = 0
        for line in lines: total += self.write(line) or 0
        return total

    def flush(self):
        t = self._target()
        if t is not None and hasattr(t, "flush"): t.flush()

    def isatty(self)->bool:
        t = self._target()
        return bool(getattr(t, "isatty", lambda: False)())

    def __getattr__(self, k): return getattr(self._target(), k)


_chans = ("shell", "stdout", "stderr", "request_input", "allow_stdin")


class _ThreadLocalIO:
    def __init__(self):
        "Capture original IO hooks and prepare thread-local state."
        self.installed = False
        self.vars = {name: contextvars.ContextVar(f"ipymini.{name}", default=None) for name in _chans}
        self.orig_stdout = sys.stdout
        self.orig_stderr = sys.stderr
        self.orig_input = builtins.input
        self.orig_getpass = getpass.getpass
        self.orig_get_ipython = _getipython_mod.get_ipython if _getipython_mod is not None else None

    def install(self):
        "Install thread-local stdout/stderr/input/getpass/get_ipython hooks."
        self.installed = True
        self.orig_stdout = sys.stdout if not isinstance(sys.stdout, _ThreadLocalStream) else self.orig_stdout
        self.orig_stderr = sys.stderr if not isinstance(sys.stderr, _ThreadLocalStream) else self.orig_stderr
        self.orig_input = builtins.input if builtins.input is not _thread_local_input else self.orig_input
        self.orig_getpass = getpass.getpass if getpass.getpass is not _thread_local_getpass else self.orig_getpass
        if _getipython_mod is not None:
            current_get = _getipython_mod.get_ipython
            self.orig_get_ipython = current_get if current_get is not _thread_local_get_ipython else self.orig_get_ipython
            _getipython_mod.get_ipython = _thread_local_get_ipython
        sys.stdout = _ThreadLocalStream("stdout", self.orig_stdout)
        sys.stderr = _ThreadLocalStream("stderr", self.orig_stderr)
        builtins.input = _thread_local_input
        getpass.getpass = _thread_local_getpass

    def get(self, name: str): return self.vars[name].get()

    def push(self, shell, stdout, stderr, request_input: Callable[[str, bool], str], allow_stdin: bool)->dict:
        "Set per-thread IO bindings; returns the previous bindings."
        self.install()
        args = locals()
        prev = {name: self.vars[name].set(args[name]) for name in _chans}
        return prev

    def pop(self, prev: dict):
        "Restore IO bindings from `prev`."
        for name in _chans: self.vars[name].reset(prev[name])


io_state = _ThreadLocalIO()


def _thread_local_get_ipython():
    shell = io_state.get("shell")
    if shell is not None: return shell
    if io_state.orig_get_ipython is None: return None
    return io_state.orig_get_ipython()


def _thread_local_input(prompt: str = "")->str:
    "Route input() through kernel stdin handler using `prompt`."
    handler = io_state.get("request_input")
    allow = bool(io_state.get("allow_stdin"))
    if handler is None or not allow:
        msg = "raw_input was called, but this frontend does not support input requests."
        raise StdinNotImplementedError(msg)
    return handler(str(prompt), False)


def _thread_local_getpass(prompt: str = "Password: ", stream=None)->str:
    "Route getpass() through stdin handler using `prompt`."
    handler = io_state.get("request_input")
    allow = bool(io_state.get("allow_stdin"))
    if handler is None or not allow:
        msg = "getpass was called, but this frontend does not support input requests."
        raise StdinNotImplementedError(msg)
    return handler(str(prompt), True)


@contextmanager
def thread_local_io(shell, stdout, stderr, request_input: Callable[[str, bool], str], allow_stdin: bool):
    io_state.install()
    prev = io_state.push(shell, stdout, stderr, request_input, allow_stdin)
    try: yield
    finally: io_state.pop(prev)
