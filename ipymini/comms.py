import threading
from contextlib import contextmanager
from typing import Callable

import comm
from comm import base_comm

IopubSender = Callable[..., None]


class _CommContext:
    def __init__(self) -> None:
        "Store per-thread comm sender and parent."
        self._local = threading.local()

    def get(self) -> tuple[IopubSender | None, dict | None]:
        "Return the active comm sender and parent header."
        return getattr(self._local, "sender", None), getattr(self._local, "parent", None)

    def set(self, sender: IopubSender | None, parent: dict | None) -> None:
        "Set the comm sender and parent header for this thread."
        self._local.sender = sender
        self._local.parent = parent


_COMM_CONTEXT = _CommContext()


@contextmanager
def comm_context(sender: IopubSender | None, parent: dict | None):
    "Temporarily bind a comm sender and parent header for this thread."
    prev_sender, prev_parent = _COMM_CONTEXT.get()
    _COMM_CONTEXT.set(sender, parent)
    try: yield
    finally: _COMM_CONTEXT.set(prev_sender, prev_parent)


class IpyminiComm(base_comm.BaseComm):
    def publish_msg(self, msg_type: str, data: base_comm.MaybeDict = None, metadata: base_comm.MaybeDict = None,
        buffers: base_comm.BuffersType = None, **keys) -> None:
        "Send comm messages on IOPub using the current comm context."
        sender, parent = _COMM_CONTEXT.get()
        if sender is None: return
        if data is None: data = {}
        if metadata is None: metadata = {}
        content = dict(data=data, comm_id=self.comm_id, **keys)
        sender(msg_type, content, parent or {}, metadata, self.topic, buffers)


_COMM_LOCK = threading.Lock()
_COMM_MANAGER = None

def _create_comm(*args, **kwargs): return IpyminiComm(*args, **kwargs)

def _get_comm_manager():
    "Return the process-wide comm manager."
    global _COMM_MANAGER
    if _COMM_MANAGER is None:
        with _COMM_LOCK:
            if _COMM_MANAGER is None: _COMM_MANAGER = base_comm.CommManager()
    return _COMM_MANAGER


comm.create_comm = _create_comm
comm.get_comm_manager = _get_comm_manager

def get_comm_manager() -> base_comm.CommManager: return _get_comm_manager()

__all__ = ["IpyminiComm", "comm_context", "get_comm_manager"]
