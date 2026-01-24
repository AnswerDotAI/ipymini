from importlib.metadata import PackageNotFoundError, version
from .kernel import run_kernel

__version__ = "0.1.0"
try: __version__ = version("ipymini")
except PackageNotFoundError: pass

__all__ = ["run_kernel", "__version__"]
