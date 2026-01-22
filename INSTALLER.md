# PyPI Precompiled Installer Plan (macOS + Ubuntu)

## Goal
Ship a **precompiled PyPI package** that installs a runnable `ipyrust` kernel on **current macOS and Ubuntu** releases. It is acceptable to require system packages via `brew`/`apt` (e.g., `libzmq`).

## Key Constraints (current codebase)
- The kernel is a **Rust binary embedding Python via PyO3** (`src/python.rs`).
- The binary **links against libpython**, and `build.rs` currently injects an rpath pointing to the build machine's Python libdir.
- The kernel depends on **libzmq** (via `zmq` crate) and uses system ZMQ at runtime.

These constraints drive the packaging strategy.

## Proposed Approach (recommended)
Package `ipyrust` as a Python distribution that ships a **prebuilt Rust binary** plus a **kernel spec installer**. Build platform-specific wheels in CI and publish to PyPI.

### 1) Python package layout
Create a minimal Python package (e.g., `ipyrust/`) that:
- **Bundles the `ipyrust` binary** inside the wheel (installed to `bin/` or as package data).
- **Includes `kernel.json`** (from `share/jupyter/kernels/ipyrust/kernel.json`) as package data.
- Provides a small installer entrypoint:
  - `python -m ipyrust.install` (or `ipyrust-install`) to copy the kernel spec to the user's Jupyter path.

This avoids relying on post-install hooks (not supported by pip) and keeps kernel registration explicit.

### 2) Build system
Use **maturin** (confirmed) + **cibuildwheel** to build and publish wheels.

- **maturin** can build Rust **binary** wheels (`bindings = "bin"`) and place the binary in the wheel's scripts.
- **cibuildwheel** handles platform-specific build images and Python ABI tagging.

**Targets:**
- **macOS:** build `arm64` and `x86_64` (or `universal2` if using python.org universal builds).
- **Ubuntu:** build manylinux wheels (e.g., `manylinux_2_28` or `manylinux_2_31`) for x86_64.

**Python versions:** target **CPython 3.10+** if practical. A reasonable default is 3.10-3.13 unless we decide to drop an older minor for CI cost. Because this is an embedded binary linking against `libpython`, expect **per-minor wheels** (no abi3-style consolidation).

### 3) Runtime dependencies
Document and enforce the following dependencies:
- **System packages:**
  - macOS: `brew install libzmq`
  - Ubuntu: `sudo apt-get install libzmq5` (runtime) or `libzmq3-dev` (build)
- **Python packages:**
  - `ipython`
  - `jupyter_client`
  - `jupyter_core`
  - `debugpy` (optional extra, e.g., `ipyrust[debug]`)

### 4) Kernel spec installation
Because pip cannot run post-install scripts, provide a dedicated command:
- `python -m ipyrust.install`

This command should:
- Copy the bundled `kernel.json` into the user kernelspec path (via `jupyter_client.kernelspec`).
- Ensure the `argv` in `kernel.json` points to the installed `ipyrust` executable.

### 5) RPATH / libpython portability (recommended strategy)
PyO3's guidance for **embedded binaries** is to use **dynamic embedding** (shared `libpython`) and let the runtime environment locate the correct shared library. This is explicitly described as working well inside virtualenvs, while "non-technical" distribution may require bundling `libpython` and a wrapper script that sets `LD_LIBRARY_PATH`/`PATH`.

Given our target (pip install into an existing Python environment), the **idiomatic/lowest-risk strategy** is:
- **Remove build-time rpath injection** (avoid embedding builder-specific paths into the binary).
- **Rely on dynamic linking + runtime env detection** (`ensure_python_env`) to find the user's Python shared library.
- If macOS users still hit loader errors, add a lightweight **launcher shim** (installed with the wheel) that sets `DYLD_LIBRARY_PATH` before invoking `ipyrust`.

We should **not** ship `libpython` inside the wheel for this project unless we later decide to target non-technical users or standalone distribution.

### 6) CI build pipeline
Suggested CI steps:
1. Install build deps (`libzmq`, `pkg-config`, Rust toolchain).
2. Build wheels with `cibuildwheel` + `maturin` for each CPython version and target.
3. Run `delocate` (macOS) / `auditwheel` (Linux) if needed.
4. Upload wheels and sdist to PyPI.

### 7) Fallback source build
If a wheel is unavailable, allow sdist builds with:
- Rust toolchain
- `pkg-config`
- `libzmq3-dev`
- Python headers / shared libpython

## User-facing install flow (example)
**macOS:**
```
brew install libzmq
pip install ipyrust
python -m ipyrust.install
```

**Ubuntu:**
```
sudo apt-get install libzmq5
pip install ipyrust
python -m ipyrust.install
```

## Finalized choices (from discussion)
1) **Tooling:** `maturin` (bin wheels) + `cibuildwheel`.
2) **Python versions:** CPython **3.10+**, likely 3.10-3.13 unless we decide to drop a minor.
3) **RPATH strategy:** **dynamic embedding** without hardcoded rpath; rely on runtime env detection; add a launcher shim if needed.
4) **Kernel install UX:** `python -m ipyrust.install`.
