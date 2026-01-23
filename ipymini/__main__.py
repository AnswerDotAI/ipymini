import argparse, json, shutil, sys, tempfile
from pathlib import Path
from jupyter_client.kernelspec import install_kernel_spec
from .kernel import run_kernel


def _run_kernel_from_cli(argv: list[str]) -> None:
    "Parse CLI args and run the kernel."
    parser = argparse.ArgumentParser(prog="ipymini")
    parser.add_argument("-f", "--connection-file", required=True)
    args = parser.parse_args(argv)
    run_kernel(args.connection_file)


def _install_kernelspec(argv: list[str]) -> None:
    "Install the ipymini kernelspec into user/sys/prefix location."
    parser = argparse.ArgumentParser(prog="ipymini install")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", help="Install into user Jupyter dir")
    scope.add_argument("--sys-prefix", action="store_true", help="Install into current env")
    scope.add_argument("--prefix", help="Install into a given prefix")
    args = parser.parse_args(argv)

    if args.sys_prefix and args.prefix: raise SystemExit("--sys-prefix and --prefix are mutually exclusive")
    prefix = args.prefix or (sys.prefix if args.sys_prefix else None)

    kernel_dir = Path(__file__).resolve().parents[1] / "share" / "jupyter" / "kernels" / "ipymini"
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "ipymini"
        shutil.copytree(kernel_dir, dest)
        _ensure_frozen_modules_flag(dest / "kernel.json")
        install_kernel_spec(str(dest), kernel_name="ipymini", user=bool(args.user), prefix=prefix, replace=True)


def _ensure_frozen_modules_flag(kernel_json: Path) -> None:
    "Ensure kernelspec argv includes -Xfrozen_modules=off when needed."
    if sys.implementation.name != "cpython" or sys.version_info < (3, 11): return
    with open(kernel_json, encoding="utf-8") as f: data = json.load(f)
    argv = list(data.get("argv") or [])
    if "-Xfrozen_modules=off" in argv: return
    insert_at = argv.index("-m") if "-m" in argv else 1
    argv.insert(insert_at, "-Xfrozen_modules=off")
    data["argv"] = argv
    with open(kernel_json, "w", encoding="utf-8") as f: json.dump(data, f, indent=2)


def main() -> None:
    "CLI entry point."
    argv = sys.argv[1:]
    commands = {"install": _install_kernelspec, "run": _run_kernel_from_cli}
    if argv and argv[0] in commands: commands[argv[0]](argv[1:])
    else: _run_kernel_from_cli(argv)


if __name__ == "__main__": main()
