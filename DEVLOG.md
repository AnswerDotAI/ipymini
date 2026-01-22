# DEVLOG

## Snapshot (2026-01-22)

Aligned behavior with the “Python‑specific in Python” rule:

- **History handling moved back to Python**: `history_request` is now delegated to the Python bridge (IPython history manager). The Rust core only routes and replies.
- **Comm info moved back to Python**: `comm_info_request` now queries the Python comm manager; the Rust core no longer maintains a parallel registry.
- **Interpreter trait restored**: `history()` and `comm_info()` are back on the trait and implemented by `PythonInterpreter`.
- **Rust tests simplified**: removed the core history/comm unit tests since those features now live in Python.

Notes / tech debt:
- macOS `cargo test` still requires `DYLD_LIBRARY_PATH` because the Python 3.12 dylib has an absolute install name (`/install/lib/libpython3.12.dylib`). Use `scripts/cargo-test.sh`.

Test status:
- `pytest -q`: `33 passed, 1 skipped, 15 subtests passed` (2026-01-22)
- `scripts/cargo-test.sh`: Rust tests all passing (2026-01-22)

Expectation: **tests must always pass** (use the helper script for Rust tests on macOS).

### Cleanup pass (2026-01-22)

- **allow_stdin honored**: `input()` / `getpass()` now raise `StdinNotImplementedError` when `allow_stdin=False`, matching ipykernel. Added `test_input_request_disallowed`.
- **Completion aligned to ipykernel**: removed the `rlcompleter` fallback and extra match normalization; now uses `line_at_cursor` + `shell.complete` exactly. To keep completions reliable in embedded mode we force `self.shell.Completer.use_jedi = False` (IPython’s non‑jedi completer returns the expected results).
- **Shell choice**: use `InteractiveShell` only (direct `ipykernel` imports are forbidden; we copy behavior instead).
- **PyO3 update**: switched to `PyModule::from_code_bound`.
- **Misc cleanup**: removed unused `ldlibrary` probe; silenced the unused `zmq::Context` warning by documenting the retained field.
- **Duplication reduction**: centralized interpreter reply handling, shutdown handling, comm publish flow, ZMQ reply building, and Python task dispatch.
- **Env discovery unified**: added `scripts/python_env.py` and reused it for build/test/runtime Python path detection.
- **Env export helper**: `python scripts/python_env.py --exports` prints shell exports for PYTHONHOME/PYTHONPATH/LD paths.

Test status:
- `pytest -q`: `33 passed, 1 skipped, 15 subtests passed` (2026-01-22)
- `scripts/cargo-test.sh`: Rust tests all passing (2026-01-22)

## Snapshot (2026-01-21)

We now have a working Rust Jupyter kernel that runs Python via IPython and passes an initial integration test suite derived from `jupyter_kernel_test`. The kernel boots, replies to kernel_info, executes code, sends IOPub output, handles history/inspect/completion/is_complete, responds to interrupt requests, handles comms + user_expressions, supports stdin input requests, and the Rust codebase is now split into a reusable “core” (kernel protocol + transport) with a Python interpreter implementation.

## What was built

### Rust kernel core
- **Core split**: protocol logic is now in `src/core/kernel_core.rs`, transport in `src/core/zmq_kernel.rs`, types in `src/core/types.rs`, and the interpreter trait in `src/core/interpreter.rs`.
- ZMQ sockets: ROUTER for shell/control/stdin, XPUB for iopub, REP for heartbeat.
- IOPub emits `iopub_welcome` on subscription (XPUB).
- Jupyter message parsing/signing: `src/core/message.rs` handles frames, signature checks, and reply frames.
- Execution queue: shell execute requests are serialized; a single in-flight request is processed at a time.
- IOPub sequencing: sends `status: busy`, `execute_input`, output messages, then `execute_reply`, then `status: idle`.
- `kernel_info_reply` includes `status: ok` and triggers busy/idle to satisfy client readiness checks.
- **Comms on shell channel**: `comm_open`, `comm_msg`, `comm_close` are echoed to IOPub and forwarded to the Python comm manager. `comm_info_request` now queries Python.
- **User expressions**: execute replies now include `user_expressions` evaluated via IPython.
- **stdin/input**: kernel now handles `input_request`/`input_reply` on the stdin channel, driven by a Rust↔Python bridge.
- **Runtime Python env discovery**: the Rust binary now probes a system `python3`/`python` for stdlib/site-packages paths and sets `PYTHONHOME`/`PYTHONPATH`/`DYLD_LIBRARY_PATH` (or `LD_LIBRARY_PATH`) if missing.
- **Debug protocol (debugpy, partial)**: `debug_request` now proxies to debugpy (in-process adapter) for `initialize`, `attach`, and `evaluate`, and forwards debug events on IOPub. `configurationDone` is auto-sent after `initialized` during attach (response ignored). Still missing most DAP commands.

### Python bridge (PyO3 + IPython)
- Runs IPython `InteractiveShell` inside a worker thread.
- Custom display publisher collects `display_data`, `update_display_data`, and `clear_output` events.
- Custom display hook captures execution results (`execute_result`).
- Pager hook wired via IPython `show_in_pager` to produce payloads for `?` and `help` output.
- Completion uses IPython `shell.complete` with `line_at_cursor`; jedi is disabled in embedded mode for reliability.
- **Stream handling uses Python stream objects (not redirect/capture):**
  - `RustStream` replaces `sys.stdout` / `sys.stderr`.
  - `write()` appends ordered stream events, coalescing consecutive writes from the same stream.
  - This is closer to how `ipykernel` / `xeus-python` handle streams.
- **Python comm manager**:
  - `RustCommManager` tracks comms, targets, and events.
  - Methods `comm_open`, `comm_msg`, `comm_close`, `comm_info` are called from Rust.
  - Exposed via `ipyrust_bridge.get_comm_manager()` for test setup and introspection.
- **Input bridge**:
  - A Rust `request_input()` PyO3 function forwards `input()` / `getpass.getpass()` to the kernel thread.
  - Kernel sends `input_request` over stdin and replies with the `input_reply` value back into Python.

### Kernel spec & tests
- Kernel spec at `share/jupyter/kernels/ipyrust/kernel.json`.
- `tests/test_ipyrust_kernel.py` uses `jupyter_kernel_test` harness plus targeted extras.
- `tests/test_python_stream.py` unit‑tests the new `RustStream` behavior.
- Rust unit tests for the core live in `tests/core_message.rs` and `tests/core_kernel_core.rs`.
- Tests configure environment variables so embedded Python can find stdlib and site‑packages.

## What worked / did not work (hard-won learnings)

### Worked well
- **Jupyter protocol loop in Rust** is straightforward and stable. The `parse_message` / `build_reply` helper split keeps protocol details clean.
- **Execution queue** is sufficient for now and avoids reentrancy issues.
- **IPython integration** gives high fidelity for execution, history, inspect, and paging without reimplementing Python semantics.
- **IOPub ordering** (`busy` -> output -> `idle`) matches jupyter_kernel_test expectations.
- **Stream handling via custom Python stream** is stable and closer to upstream behavior than `capture_output`.
- **User expressions** work correctly via `shell.user_expressions`.

### Tricky or worth revisiting
- **Embedding Python on macOS**: the binary initially failed to locate `encodings`/stdlib. We now probe `python3`/`python` at runtime and set `PYTHONHOME`/`PYTHONPATH`/`DYLD_LIBRARY_PATH` if missing, but we still need to validate this across more Python installs (Homebrew, pyenv, conda).
- **stdin interrupts**: `input()` now works, but interruption while waiting for input is only partially handled (we raise a `KeyboardInterrupt` for pending input, but don’t yet handle all edge cases).
- **Debug protocol**: debugpy is wired in, but we are not yet doing the full ipykernel flow (missing breakpoints, stackTrace, variables, stepping, etc.).
- **Completion**: we disable jedi in the embedded shell for predictable results. If we can make jedi stable in this embedding, we should revisit and re‑enable it.
- **Display lifecycle**: we have a minimal display publisher/hook; `display_id` update coverage exists but may need deeper metadata fidelity.

### Complexity hotspots
- **Python environment discovery**: runtime detection is in place, but the heuristics may need refinement for non-Homebrew installs or unusual layouts.
- **Display lifecycle**: correct `display_id` updates and output metadata will be subtle.

## Tests

### Current tests
`tests/test_ipyrust_kernel.py` (based on `jupyter_kernel_test.KernelTests`) covers:
- `kernel_info` reply
- stdout/stderr streams
- execute_result
- display_data
- pager payloads
- history (tail/search)
- inspection
- is_complete
- completion
- clear_output
- interrupt (message-based)
- stream ordering (stdout/stderr interleave)
- clear_output(wait=True)
- display_id update via `update_display_data`
- silent execution (no output)
- store_history behavior
- user_expressions
- comm lifecycle (open/msg/close)
- comm_info
- stdin input (`input_request`/`input_reply`)
- stdin disallowed (`allow_stdin=False` raises `StdinNotImplementedError`)
- display metadata/transient passthrough
- iopub_welcome (XPUB subscription)
- matplotlib display (skipped if matplotlib unavailable)
- debug initialize/attach/evaluate (debugpy, skipped if debugpy unavailable)
- debug_event initialized on IOPub

`tests/test_python_stream.py` covers:
- stream event ordering
- write coalescing
- interleaving between stdout/stderr
- byte decoding behavior

Status: `33 passed, 1 skipped, 15 subtests passed` on the latest full run; stream unit tests included.

Rust unit tests: `cargo test` still requires PYTHONHOME/PYTHONPATH/DYLD_LIBRARY_PATH to be set (embedding on macOS). Without those, the test binary fails to load `libpython`.

### What is not yet tested
- Debug protocol coverage beyond initialize/attach/evaluate
- debug_event stopped/continued flow
- interrupt during stdin input
- detailed message spec validation beyond the current harness
- large payloads / binary buffers

## What we changed to make tests pass
- Added `status: ok` to `kernel_info_reply` and sent IOPub busy/idle around it.
- Fixed Python embedding with environment variables in tests.
- Added `show_in_pager` hook to produce payloads for `?` help.
- Aligned completion to IPython `shell.complete` with `line_at_cursor`, and disabled jedi for embedded mode stability.
- Replaced `capture_output` with a custom stream class that records ordered stream events.
- Added integration tests for stream ordering, clear_output(wait=True), display_id updates, silent execution, store_history behavior, user_expressions, and comm lifecycle.
- Added stdin input request test (`input()` with allow_stdin).
- Added `allow_stdin=False` test and Python-side `StdinNotImplementedError` behavior.
- Added debug initialize/attach tests (debugpy, skipped if unavailable).
- Added debug evaluate + debug_event initialized tests (debugpy).

### Test harness note (minor workaround)
`jupyter_kernel_test.validate_message` doesn’t recognize `comm_*` message types. In comm tests we now use a local raw channel drain helper instead of `flush_channels()` to avoid validation errors. This is test-only and should be removed if we extend the validator or adopt a comm-aware test harness.
`jupyter_kernel_test` also doesn’t recognize `iopub_welcome` or `debug_event`; we override `flush_channels()` in our test subclass to ignore those messages during validation.

## Next steps (pragmatic order)

1) **Protocol completeness: debugger**
   - Expand debugpy support beyond initialize/attach/evaluate (breakpoints, stackTrace, variables, stepping, scopes).
   - Add IOPub `debug_event` tests for `stopped`/`continued` and breakpoint flow.

2) **Input/interrupt robustness**
   - Handle interrupts during `input()` more faithfully (no empty-string fallbacks).

3) **Comms + display refinement**
   - Implement richer comm routing (targets + per-comm callbacks) if needed beyond tests.
   - Improve `display_id` tracking, metadata fidelity, and binary buffer support.

## Notes on architecture choices

- The Rust core is intentionally thin: it enforces protocol, message ordering, and concurrency control.
- IPython owns execution semantics.
- We do not implement raw mode.
- The code is intentionally clear and small; anything that feels complex (Python environment discovery, display lifecycle) should be revisited and simplified.
