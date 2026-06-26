from functools import lru_cache

import comm
from comm import base_comm

_kernel = None

def set_kernel(kernel):
    "Bind the comm layer to a running kernel so outbound comms publish on its IOPub."
    global _kernel
    _kernel = kernel


class IpyminiComm(base_comm.BaseComm):
    def publish_msg(self, msg_type:str, data: base_comm.MaybeDict=None, metadata: base_comm.MaybeDict=None,
        buffers: base_comm.BuffersType=None, **keys):
        kernel = _kernel
        if kernel is None: return
        content = dict(data=data or {}, comm_id=self.comm_id, **keys)
        kernel.iopub.send(msg_type, kernel.current_parent(), content=content, metadata=metadata or {}, ident=self.topic, buffers=buffers)

def _create_comm(*args, **kwargs): return IpyminiComm(*args, **kwargs)

@lru_cache
def get_comm_manager()->base_comm.CommManager: return base_comm.CommManager()

comm.create_comm = _create_comm
comm.get_comm_manager = get_comm_manager

__all__ = ["IpyminiComm", "get_comm_manager", "set_kernel"]
