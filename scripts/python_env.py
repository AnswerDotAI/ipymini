#!/usr/bin/env python3
"""Probe a Python installation for sysconfig/site paths.

Used by build/test/runtime helpers to keep env discovery consistent.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import sysconfig
import site


def collect_info() -> dict[str, object]:
    paths = sysconfig.get_paths()
    info: dict[str, object] = {
        "stdlib": paths.get("stdlib"),
        "platstdlib": paths.get("platstdlib"),
        "purelib": paths.get("purelib"),
        "platlib": paths.get("platlib"),
        "prefix": getattr(sys, "prefix", None),
        "base_prefix": getattr(sys, "base_prefix", None),
        "exec_prefix": getattr(sys, "exec_prefix", None),
        "libdir": sysconfig.get_config_var("LIBDIR"),
        "ldlibrary": sysconfig.get_config_var("LDLIBRARY"),
        "sitepackages": site.getsitepackages() if hasattr(site, "getsitepackages") else [],
        "usersite": site.getusersitepackages() if hasattr(site, "getusersitepackages") else None,
    }
    return info


def best_python_home(info: dict[str, object]) -> str | None:
    base = info.get("base_prefix") or info.get("prefix")
    if isinstance(base, str) and base:
        return base
    stdlib = info.get("stdlib")
    if isinstance(stdlib, str) and stdlib:
        try:
            return str(Path(stdlib).parents[1])
        except IndexError:
            return None
    exec_prefix = info.get("exec_prefix")
    return exec_prefix if isinstance(exec_prefix, str) and exec_prefix else None


def _add_unique(paths: list[str], seen: set[str], value: object) -> None:
    if isinstance(value, str) and value and value not in seen:
        paths.append(value)
        seen.add(value)


def build_pythonpath(info: dict[str, object]) -> str | None:
    seen: set[str] = set()
    paths: list[str] = []

    _add_unique(paths, seen, info.get("purelib"))
    _add_unique(paths, seen, info.get("platlib"))
    _add_unique(paths, seen, info.get("platstdlib"))
    _add_unique(paths, seen, info.get("stdlib"))

    sitepackages = info.get("sitepackages")
    if isinstance(sitepackages, list):
        for path in sitepackages:
            _add_unique(paths, seen, path)
    _add_unique(paths, seen, info.get("usersite"))

    return os.pathsep.join(paths) if paths else None


def merge_paths(*values: str | None) -> str | None:
    seen: set[str] = set()
    paths: list[str] = []
    for value in values:
        if not value:
            continue
        for part in str(value).split(os.pathsep):
            if part and part not in seen:
                paths.append(part)
                seen.add(part)
    return os.pathsep.join(paths) if paths else None


def compute_env(
    extra_pythonpath: list[str] | None = None, env: dict[str, str] | None = None
) -> dict[str, str]:
    info = collect_info()
    env = env or os.environ
    updates: dict[str, str] = {}

    if "PYTHONHOME" not in env:
        home = best_python_home(info)
        if home:
            updates["PYTHONHOME"] = home

    base_path = build_pythonpath(info)
    extra = os.pathsep.join(extra_pythonpath) if extra_pythonpath else None
    combined = merge_paths(extra, base_path, env.get("PYTHONPATH"))
    if combined:
        updates["PYTHONPATH"] = combined

    libdir = info.get("libdir")
    if isinstance(libdir, str) and libdir:
        if sys.platform == "darwin":
            key = "DYLD_LIBRARY_PATH"
        elif sys.platform.startswith("linux"):
            key = "LD_LIBRARY_PATH"
        else:
            key = None
        if key:
            merged = merge_paths(libdir, env.get(key))
            if merged:
                updates[key] = merged

    return updates


def ensure_env(extra_pythonpath: list[str] | None = None) -> None:
    updates = compute_env(extra_pythonpath=extra_pythonpath)
    for key, value in updates.items():
        os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Python environment paths.")
    parser.add_argument("--json", action="store_true", help="Print JSON (default).")
    parser.add_argument("--libdir", action="store_true", help="Print LIBDIR only.")
    parser.add_argument("--pythonpath", action="store_true", help="Print computed PYTHONPATH.")
    parser.add_argument(
        "--exports",
        action="store_true",
        help="Print shell exports for inferred environment.",
    )
    parser.add_argument(
        "--extra-pythonpath",
        action="append",
        default=[],
        help="Extra PYTHONPATH entries (repeatable).",
    )
    args = parser.parse_args()

    info = collect_info()
    if args.libdir:
        print(info.get("libdir") or "")
        return
    if args.pythonpath:
        print(build_pythonpath(info) or "")
        return
    if args.exports:
        updates = compute_env(extra_pythonpath=args.extra_pythonpath)
        for key in sorted(updates):
            print(f"export {key}={shlex.quote(updates[key])}")
        return
    print(json.dumps(info))


if __name__ == "__main__":
    main()
