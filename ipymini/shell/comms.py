import contextvars
from contextlib import contextmanager
from functools import lru_cache
from typing import Callable

import comm
from comm import base_comm

IopubSender = Callable[..., None]

_SENDER = contextvars.ContextVar("ipymini_comm_sender", default=None)
_PARENT = contextvars.ContextVar("ipymini_comm_parent", default=None)

@contextmanager
def comm_context(sender: IopubSender|None, parent: dict|None):
    t1 = _SENDER.set(sender)
    t2 = _PARENT.set(parent)
    try: yield
    finally:
        _PARENT.reset(t2)
        _SENDER.reset(t1)


class IpyminiComm(base_comm.BaseComm):
    def publish_msg(self, msg_type:str, data: base_comm.MaybeDict=None, metadata: base_comm.MaybeDict=None,
        buffers: base_comm.BuffersType=None, **keys):
        sender = _SENDER.get()
        if sender is None: return
        content = dict(data=data or {}, comm_id=self.comm_id, **keys)
        sender(msg_type, _PARENT.get() or {}, content=content, metadata=metadata or {}, ident=self.topic, buffers=buffers)

def _create_comm(*args, **kwargs): return IpyminiComm(*args, **kwargs)

@lru_cache
def get_comm_manager()->base_comm.CommManager: return base_comm.CommManager()

comm.create_comm = _create_comm
comm.get_comm_manager = get_comm_manager

__all__ = ["IpyminiComm", "comm_context", "get_comm_manager"]
