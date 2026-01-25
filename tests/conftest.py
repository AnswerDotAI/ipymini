import sys, pytest
from pathlib import Path
from .kernel_utils import KernelHarness
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


@pytest.fixture
def kernel_harness():
    with KernelHarness() as h: yield h
