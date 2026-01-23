# DEVLOG

This is a living, condensed log of what matters about ipymini: architecture, protocol behavior, tests, style, and current conventions. Update it as the project evolves.

## Project snapshot
- `ipymini` is a small, Python‑only Jupyter kernel with IPython parity as the goal.
- Core modules: `ipymini/kernel.py` (ZMQ + protocol), `ipymini/bridge.py` (IPython integration, debug, comms), `ipymini/__main__.py` (CLI/installer).
- Tests live in `pytests/` and aim for protocol‑level parity with `ipykernel`.
- Reference trees (`ipykernel/`, `xeus/`, etc.) are kept for comparison, not edited.

## Coding style (fastai)
- Follow `style.md` (fastai style), not PEP8.
- Wrap ~140 chars. Avoid vertical whitespace; group imports; 1‑line bodies for single statements only.
- Prefer `dict(...)` when there are 3+ identifier keys (unless key isn’t a valid identifier).
- No semicolons to chain multiple statements.
- Prefer concise helpers for repeated patterns; avoid unnecessary abstraction if it doesn’t reduce lines/clarity.

## Kernel architecture
- `MiniKernel` owns sockets, threads, and message routing.
- `SubshellManager` + `Subshell` provide concurrent execute pipelines with shared user namespace.
- Shell/control handlers are mapped once and dispatched by message type.
- `KernelBridge` integrates IPython execution, display, history, comms, and debugger.

## Debugger (DAP) behavior
- Debug flow mirrors ipykernel: `debugInfo` advertises `hashMethod="Murmur2"` and seed `0xC70F6907`.
- Cell source is written to temp files: `ipymini_<pid>/<murmur2(code)>.py` unless `IPYMINI_CELL_NAME` overrides.
- Murmur2 implementation lives in `ipymini/murmur2.py`.
- `debugpy` is required for tests; added to test/dev extras in `pyproject.toml`.
- Debug helper APIs in tests are centralized in `pytests/kernel_utils.py` (`debug_request`, `debug_dump_cell`, `debug_set_breakpoints`, `debug_info`, `debug_configuration_done`, `debug_continue`, `wait_for_stop`).

## Protocol coverage (selected)
- `kernel_info`, `connect_request`, `execute_request`, `complete`, `inspect`, `history`, `is_complete`.
- `IOPub`: `stream`, `execute_result`, `display_data`, `update_display_data`, `clear_output`, `error`, `iopub_welcome`.
- `stdin` input requests and reply plumbing.
- `comm_open`, `comm_msg`, `comm_close`, `comm_info`.
- Subshells: create/list/delete, per‑subshell execution counts, stream routing, history, interrupts.

## Tests
- Test runner defaults to parallel: `pytest -n auto --dist=loadfile` (set in `pytest.ini`).
- Fuzz tests are always on (no env toggle).
- Typical test helpers in `pytests/kernel_utils.py`:
  - `start_kernel`, `build_env`, `load_connection`.
  - `get_shell_reply`, `drain_iopub`, `execute_and_drain`.
  - IOPub filters: `iopub_msgs`, `iopub_streams`.
  - Debug helpers (see above).
- CI expectation: all tests must pass; debug tests assume `debugpy` is installed.

## Config conventions
- Kernel spec lives in `share/jupyter/kernels/ipymini/kernel.json`.
- `__main__.py` has an installer and ensures `-Xfrozen_modules=off` is injected into kernelspec where needed (CPython 3.11+).
- Completion flags:
  - `IPYMINI_USE_JEDI=0|1`
  - `IPYMINI_EXPERIMENTAL_COMPLETIONS=0|1`
- Debug file name override: `IPYMINI_CELL_NAME`.

## Key behaviors
- Interrupts are signal‑based (SIGINT) and restored after shutdown.
- `stop_on_error` aborts queued execute requests but lets non‑execute requests complete.
- `set_next_input` payloads are de‑duplicated per execute.
- Debugger uses `debugpy` and avoids sys.monitoring stalls by setting `PYDEVD_USE_SYS_MONITORING=0`.

## Notes for contributors
- Don’t touch reference implementations in `ipykernel/`, `xeus/`, etc.
- Keep tests concise; prefer helpers and one‑liners for single‑statement bodies.
- Always run the test suite after code changes.
