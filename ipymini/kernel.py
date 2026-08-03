"The ipymini kernel: IPython's `MiniShell` wired into kernmini's protocol core."

from kernmini import run_kernel as _run_kernel

from .shell import MiniShell
from .shell.comms import get_comm_manager


def run_kernel(connection_file: str):
    "Run the ipymini kernel given a connection file path."
    _run_kernel(connection_file, MiniShell, comm_manager=get_comm_manager())
