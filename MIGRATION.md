# Migration: Python-Hosted Kernel (No Embedded Python)

## Purpose
Move the kernel process to be **Python-hosted** (`python -m ipyrust`) so it naturally uses the user's venv interpreter and avoids embedding-related loader/rpath issues. The Rust core remains valuable, but Python becomes the process owner.

## Recommended Approach
**Option A: Python host + Rust core**
- The kernel entrypoint is Python.
- Rust remains the protocol/transport core, exposed as a Python extension module.
- IPython runs natively in the venv, not embedded.

This preserves the existing architectural split (Rust protocol loop + Python execution) while eliminating the need to embed and locate `libpython` at runtime.

## Complexity Tradeoffs
### Short-term (migration cost)
- **Increases**: new Python entrypoint, new binding surface, refactoring bootstrapping.
- **Moderate risk**: startup and message loop integration will change.

### Long-term (maintenance cost)
- **Decreases**: no embedded Python loader/rpath issues, simpler packaging, fewer platform-specific hacks.
- **Improves**: uses the venv interpreter directly, matching user expectations.

## Plan

### Phase 0: Design and acceptance criteria
- Define success criteria:
  - Kernel runs from venv with `python -m ipyrust -f <connection.json>`.
  - All existing tests pass (or have clear replacements if the harness changes).
  - No `libpython` rpath/loader hacks required at runtime.

### Phase 1: New Python entrypoint (no behavior change)
1) Add `ipyrust/__main__.py` with `argparse` for `-f/--connection-file`.
2) Make it call into Rust via a new binding `ipyrust_core.run_kernel(conn_path)`.
3) Keep existing `ipyrust` binary unchanged for now (parallel path).

Outcome: Python can launch the kernel while Rust still owns the ZMQ loop.

### Phase 2: Expose Rust core as a Python extension module
1) Add a new Rust crate target (cdylib) with PyO3 bindings.
2) Expose `run_kernel(connection_file: str)` and minimal types needed.
3) Build and install via `maturin develop` for local testing.

Outcome: Python entrypoint works without embedding.

### Phase 3: Remove embed-specific bootstrapping
1) Stop calling `ensure_python_env()` when launched from Python.
2) Remove build-time rpath injection (`build.rs`) for embedded use.
3) Keep a fallback if the Rust binary remains supported (optional).

Outcome: clean venv-native execution path.

### Phase 4: Packaging and kernel spec switch
1) Update `kernel.json` argv to `python -m ipyrust -f {connection_file}`.
2) Package the Rust extension + `kernel.json` in the wheel.
3) Provide `python -m ipyrust.install` to register the kernel.

Outcome: pip install + python -m install works with venv Python.

### Phase 5: Deprecate the Rust binary entrypoint (optional)
- Decide whether to keep `ipyrust` binary as a compatibility entrypoint.
- If removed, update docs and tests accordingly.

## Testing Plan

### Unit/Integration tests (existing)
- Run `pytest -q` with the kernel spec pointing at `python -m ipyrust`.
- Ensure all kernel behavior tests still pass.

### New tests
- Add a smoke test that launches the kernel via `python -m ipyrust` and validates:
  - `kernel_info_reply` returns expected fields.
  - A simple execute request produces `execute_reply` and IOPub streams.

### Platform checks
- macOS and Ubuntu:
  - Verify a venv install can run the kernel without setting `DYLD_LIBRARY_PATH`/`LD_LIBRARY_PATH`.
  - Confirm `libzmq` presence is the only required system dependency.

### Packaging checks
- Build wheels with `maturin` + `cibuildwheel`.
- Install wheel into a fresh venv and run `python -m ipyrust.install`.
- Validate `jupyter kernelspec list` includes `ipyrust` and execution works.

## Risks and Mitigations
- **Risk:** Rust core assumes it owns process lifecycle.
  - **Mitigation:** Provide a single `run_kernel` function that can be called from Python without changing core internals.
- **Risk:** Threading or GIL interactions change behavior.
  - **Mitigation:** Keep the Rust loop in a dedicated thread and use existing Python bridge code.
- **Risk:** Tests depend on `ipyrust` binary in PATH.
  - **Mitigation:** Update tests to use the new kernel spec and PATH expectations.

## Decision Points
- Keep the Rust binary entrypoint or drop it after migration?
- Minimum Python version to support (default 3.10+).
- Whether to keep an optional standalone path for non-Python environments.
