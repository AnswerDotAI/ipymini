__version__ = "0.1.9"





from importlib.metadata import PackageNotFoundError, version
from .unlock import unlock
from .kernel import run_kernel

try: __version__ = version("ipymini")
except PackageNotFoundError: pass

__all__ = ["run_kernel", "unlock", "__version__"]
