"The ipymini kernel: IPython's `MiniShell` wired into kernmini's protocol core."

from kernmini import run_kernel as run_native
from .shell import MiniShell


def run_kernel(connection_file: str):
    "Run the ipymini kernel given a connection file path."
    user_ns, first = {}, True
    def shell_factory():
        nonlocal first
        shell = MiniShell(request_input=lambda *_: "", user_ns=user_ns, use_singleton=first)
        first = False
        return shell
    return run_native(connection_file, shell_factory, own_process_group=True)
