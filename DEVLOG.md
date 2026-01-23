# DEVLOG

## Snapshot (2026-01-22)

`ipyrust` has been fully migrated to **`ipymini`**, a Python-only kernel with the same behavior targets and a much smaller codebase.

### What changed
- **Rust removed**: the Rust core, build files, and Rust tests were deleted.
- **Python-only kernel**: the kernel loop now lives in `ipymini/kernel.py` and uses `pyzmq` directly.
- **IPython bridge**: execution, display, history, comms, completion, inspect, pager, stdin, and debug live in `ipymini/bridge.py`.
- **Kernelspec**: now `share/jupyter/kernels/ipymini/kernel.json` (`python -m ipymini`).
- **Tests**: protocol tests were ported into `pytests/` (no external test harness dependency).

### Test status
- `pytest -q`: `79 passed, 2 skipped` (2026-01-22)

### Behavior covered by tests
- kernel_info replies
- connect_request replies
- stdout/stderr streams + stream ordering
- execute_result and display_data
- stop_on_error (aborts pending executes by default, can be disabled)
- pager payloads (`?` / `help`)
- set_next_input payload coalescing
- history (tail/search/range, unique/n)
- inspect, completion, is_complete
- clear_output (+ wait=True)
- display_id update + metadata/transient passthrough
- iopub_welcome (XPUB subscription)
- interrupts (signal-based)
- stdin input requests + disallowed stdin errors
- user_expressions
- comm lifecycle + comm_info
- debug initialize/attach/evaluate + breakpoints/stackTrace/scopes/variables/continue (debugpy if available)
- matplotlib inline display (skipped if matplotlib unavailable)
- subshells (create/list/delete, per‑subshell history/execution counts, concurrent execution)
- optional subshell fuzz test (opt-in via `IPYMINI_FUZZ=1`)

### Recent hard-won learnings
- `interrupt_kernel()` uses SIGINT when kernelspec `interrupt_mode` is `signal`; the kernel must restore the default SIGINT handler so IPython turns interrupts into `KeyboardInterrupt`.
- `set_next_input` is not present on vanilla `InteractiveShell` instances; to mirror ipykernel behavior we inject a helper that writes a `set_next_input` payload and dedupe to a single payload per execute.
- The stop-on-error semantics apply to *queued execute_requests only*; non-execute shell requests must still be answered.
- Subshells require thread‑local IO + a dedicated stdin router to keep prompt replies matched to the right request.
- Shell replies must be funneled through the main thread to avoid concurrent ROUTER sends.

### Known gaps / TODO (high level)
- Expand debugger/DAP coverage (step/next/stepOut, exception breakpoints, disconnect/terminate semantics)
- Interrupt handling while blocked on input
- Binary buffer support + richer display metadata

## Notes on architecture choices

- Keep the protocol loop minimal and readable.
- Let IPython own Python semantics (execution, history, inspection, completions, paging).
- Avoid raw mode (explicit non-goal).

## Debugger deep dive (2026-01-22)

### What we observed (important)
- **Breakpoints work in nbclassic** with this kernel (manual confirmation).
- **Test harness timeouts on `debug_event: stopped` were resolved** by disabling debugpy’s sys.monitoring backend (see below).

### Changes made while debugging
- **ZMQ STREAM transport for debugpy** (mirrors ipykernel): the debug adapter is now handled via a STREAM socket with a message queue parser, instead of a plain TCP socket.
- **Dedicated IOPub thread**: IOPub sends are queued to a background thread so debug events can be emitted even while the main loop is blocked.
- **Kernelspec**: `-Xfrozen_modules=off` added to avoid debugpy’s frozen-modules breakpoint warnings (mirrors ipykernel behavior).
- **Attach options**: `DebugStdLib` and `rules` for internal frames are set like ipykernel.
- **Cleanup transforms**: `leading_empty_lines` transform is removed while debugging to preserve line numbers (mirrors ipykernel).
- **Stream flush on stdin request** (to fix debugger REPL output ordering).

### Breakpoint-stop tests
Breakpoint-stop tests now pass reliably after disabling debugpy’s sys.monitoring mode (see below).

### How to capture logs
Set `DEBUGPY_LOG_DIR=/tmp/ipymini_debugpy_logs` (or similar) in the kernel env.

### TODO for future maintainers
- Expand DAP coverage (step, exceptions, and disconnect/terminate semantics).
- Keep an eye on sys.monitoring defaults in future debugpy/Python releases.

### Follow-up attempts (2026-01-22)
- Mirrored ipykernel’s attach/init sequence and tightened DAP sequencing (per-client `seq`, modules args, configDone handling).
- Added debugpy thread tracing safeguards (`trace_this_thread(True)` on execute threads; `trace_this_thread(False)` on the debugpy client reader thread).
- **Fix:** force `PYDEVD_USE_SYS_MONITORING=0` before importing debugpy. This avoids the thread‑stalling behavior under sys.monitoring and makes breakpoint-stop tests pass reliably.
