from importlib.metadata import PackageNotFoundError, version

from .comms import IpyminiComm, get_comm_manager, set_kernel
from .shell import MiniShell

__version__ = "0.0.0"
try: __version__ = version("ipymini-shell")
except PackageNotFoundError: pass

__all__ = ["MiniShell", "IpyminiComm", "get_comm_manager", "set_kernel", "__version__"]
