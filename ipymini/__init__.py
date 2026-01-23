from importlib.metadata import PackageNotFoundError, version
from .kernel import run_kernel

try:
    __version__ = version("ipymini")
except PackageNotFoundError:  # pragma: no cover - local editable without metadata
    __version__ = "0.0.0+local"

__all__ = ["run_kernel", "__version__"]
