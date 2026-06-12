__version__ = "0.1.10"






from importlib.metadata import PackageNotFoundError, version
from .concur import unlock, subshell
from .kernel import run_kernel

try: __version__ = version("ipymini")
except PackageNotFoundError: pass

__all__ = ["run_kernel", "unlock", "subshell", "__version__"]
