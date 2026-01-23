import argparse
import sys
from pathlib import Path

from jupyter_client.kernelspec import install_kernel_spec

from .kernel import run_kernel


def _run_kernel_from_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="ipymini")
    parser.add_argument("-f", "--connection-file", required=True)
    args = parser.parse_args(argv)
    run_kernel(args.connection_file)


def _install_kernelspec(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="ipymini install")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", help="Install into user Jupyter dir")
    scope.add_argument("--sys-prefix", action="store_true", help="Install into current env")
    scope.add_argument("--prefix", help="Install into a given prefix")
    args = parser.parse_args(argv)

    if args.sys_prefix and args.prefix:
        raise SystemExit("--sys-prefix and --prefix are mutually exclusive")

    if args.prefix:
        prefix = args.prefix
    elif args.sys_prefix:
        prefix = sys.prefix
    else:
        prefix = None

    kernel_dir = Path(__file__).resolve().parents[1] / "share" / "jupyter" / "kernels" / "ipymini"
    install_kernel_spec(
        str(kernel_dir),
        kernel_name="ipymini",
        user=bool(args.user),
        prefix=prefix,
        replace=True,
    )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "install":
        _install_kernelspec(argv[1:])
        return
    if argv and argv[0] == "run":
        _run_kernel_from_cli(argv[1:])
        return
    _run_kernel_from_cli(argv)


if __name__ == "__main__":
    main()
