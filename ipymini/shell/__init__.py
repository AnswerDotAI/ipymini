from importlib.metadata import PackageNotFoundError, version

from .comms import IpyminiComm, comm_context, get_comm_manager
from .shell import MiniShell

__version__ = "0.0.0"
try: __version__ = version("ipymini-shell")
except PackageNotFoundError: pass

__all__ = ["MiniShell", "IpyminiComm", "comm_context", "get_comm_manager", "__version__"]
