__version__ = "0.1.7"



from importlib.metadata import PackageNotFoundError, version
from .kernel import run_kernel

try: __version__ = version("ipymini")
except PackageNotFoundError: pass

__all__ = ["run_kernel", "__version__"]
