"In-cell opt-ins for concurrent execution: unlock() and subshell()."

import contextvars
from contextlib import contextmanager

_release = contextvars.ContextVar("ipymini_release", default=None)
_subshell = contextvars.ContextVar("ipymini_subshell", default=None)


def unlock()->bool:
    "Let queued shell messages run while the current cell awaits; irreversible for the rest of the cell."
    release = _release.get()
    if release is None: return False
    release()
    return True


@contextmanager
def subshell():
    "Run execute_requests arriving from this cell's client session in a fresh subshell while the body runs."
    sub = _subshell.get()
    if sub is None: raise RuntimeError("subshell() only works inside a cell running under ipymini")
    subs = sub.kernel.subshells
    session = ((sub.parent_header_var.get() or {}).get("header") or {}).get("session")
    sid = subs.create()
    try:
        subs.set_route_override(sid, session)
        try: yield sid
        finally: subs.clear_route_override()
    finally: subs.delete(sid)
