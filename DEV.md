# Developer Guide

This guide is for contributors working on ipymini. It consolidates architecture, style, testing, PR/release flow, and onboarding notes.

## Project goals

- Small, readable, testable kernel with strong IPython parity.
- Prefer IPython APIs over re‑implementing Python semantics.
- Match ipykernel behavior where it matters (message shapes, ordering, history, interrupts, etc.).
  - Avoid complexity (e.g traitlets) where possible
  - Modernize (e.g no more tornado)

## Optional env flags

- `IPYMINI_STOP_ON_ERROR_TIMEOUT`: seconds to keep aborting queued executes after an error (default 0.0).
- `IPYMINI_USE_JEDI=0|1` – override IPython’s Jedi setting.
- `IPYMINI_CELL_NAME` – override debug cell filename.

## Tests

Run non-slow tests:

```
pytest -q
```

Run everything (including slow tests):

```
tools/run_tests.sh
```

Note: `tools/run_tests.sh` already runs `pytest -q`, so skip running it beforehand or you'll run tests twice. To run only slow tests, use:

```
pytest -q -m slow
```

Run tests for a specific module:

```
pytest tests/debug/      # debug module tests
pytest tests/shell/      # shell module tests
pytest tests/term/       # term module tests
pytest tests/zmqthread/  # zmqthread module tests
pytest tests/kernel/     # kernel integration tests
```

Notes:
- Tests start the kernel in a separate process (via `KernelManager`).
- Ensure `JUPYTER_PATH` includes `share/jupyter` from this repo for tests.
- Debug tests require `debugpy` (declared in test extras).

### Writing tests

- Prefer protocol‑level tests using `KernelManager` and helpers in `tests/kernel_utils.py`.
- Use `KernelHarness` (fixture in `tests/conftest.py`) for minimal, readable protocol‑script tests.
- Avoid large sleeps and long timeouts; use monotonic timeouts and explicit status waits.
- For blocking debugger behavior, always use a separate process.

## Style guide (fastai)

We follow the fastai style guide (`style.md`):
- Favor brevity; one‑liners for single statements (including `if/for/try/with`).
- Avoid vertical whitespace; wrap at ~140 chars.
- No semicolons for chaining statements.
- Dicts with 3+ identifier keys use `dict(...)`.
- Avoid type annotations on LHS variables (dataclasses excepted).

Run `chkstyle` before committing - it will look for clear style violations.

## PR process

Use the repo script (GitHub CLI required):

```
tools/pr.sh "Message" [label] [body|body-file]
```

Notes:
- The script creates a branch, commits tracked changes, opens a PR, and merges.
- Ensure the working tree is clean and all intended files are staged.

## Releases

- Normal releases use fastship:

```
ship-gh
ship-pypi
ship-bump
```

## Where to start reading

- `ipymini/kernel.py` – protocol, ZMQ routing, subshells.
- `ipymini/shell/shell.py` – IPython integration and output capture.
- `tests/` – protocol expectations and integration behavior (organized by module).

## Architecture overview

Core flow:
- `MiniKernel` owns sockets, threads, and dispatch.
- `microio` provides the small concurrency primitives used for service lifecycle state, task ownership, thread readiness, thread-bound channels, and request waiters.
- `SubshellManager` manages the parent subshell (main thread) and optional child subshells (worker threads) sharing a user namespace.
- `MiniShell` wraps IPython: execute, display, history, comms, debugger.

Key files:
- `ipymini/kernel.py` – Jupyter protocol, ZMQ router loops, subshells.
- `ipymini/shell/shell.py` – IPython integration, execution, output capture, debug.
- `ipymini/debug/` – debugger integration (DAP + debugpy).
- `ipymini/term/` – stream capture and IPython display hooks.
- `ipymini/zmqthread/` – ZMQ thread helpers (router, iopub, heartbeat).
- `ipymini/__main__.py` – CLI entry, install helper.
- `tests/` – protocol and behavioral tests (organized by module).

### Object graph

- `MiniKernel` creates a `SubshellManager` which creates `Subshell` instances.
- Each `Subshell` runs a `microio.ActorCore` mailbox loop; the parent and child subshells share the same serialized message handling path, but use different runners.
- Each `Subshell` holds a `MiniShell` (`self.shell`), which wraps an IPython `InteractiveShell` (`self.shell.ipy`).
- `Subshell.__init__` receives the `MiniKernel` as `kernel` and stores it via `store_attr`.
- `MiniKernel.shell` is a shortcut to `self.subshells.parent.shell` (the parent subshell's `MiniShell`).
- Child subshells (created via `SubshellManager.create`) share the same `user_ns` dict but get their own `MiniShell` and IPython instance.
- `get_ipython()` returns `MiniShell.ipy` (the `InteractiveShell` singleton for the parent, non-singleton for children).

### Execution model

- The parent subshell runs in the main thread, so SIGINT can interrupt running code without killing the kernel when idle.
- Each subshell has a persistent asyncio loop; code runs while the loop is running, so `asyncio.create_task(...)` works in sync cells.
- Output routing uses contextvars to associate streams/displays with the current parent message (works across tasks and user-launched threads).
- On POSIX, kernel startup isolates the kernel into its own process group; shutdown and direct `SIGTERM` terminate that group as the final step. Kernels launched by `KernelManager` also watch `JPY_PARENT_PID`, so nested ipymini kernels shut down when their launcher exits and then reap their own process group. ipymini does not try to gracefully cancel arbitrary user-created threads/processes individually. Windows currently skips this process-group teardown.
- On POSIX, `SIGUSR1` dumps all Python thread stacks via `faulthandler` for stuck-kernel diagnostics.

### Router threads (shell/control)

- Shell/control ROUTER sockets run in background threads via `AsyncRouterThread`.
- Kernel-owned service threads use `microio.ServiceThread` / `LoopServiceThread`, so startup failures are visible and join timeouts are checked.
- `LoopServiceThread` runs its async service body inside a `microio.TaskGroup`; stopping the thread cancels owned async children as normal shutdown.
- `MiniKernel` uses `microio.ServiceGroup` for kernel-owned thread start/wait/stop/join boilerplate.
- The debugpy reader is also a `ServiceThread`, so pending debug requests are failed on reader crash/close and joins are checked.
- The router thread is the only thread that touches its socket (thread‑safety).
- Outbound replies are enqueued; the router loop drains the queue after inbound messages to avoid starvation.
- Async sockets use `zmq.asyncio.Context.shadow(self.context)` to avoid multiple ZMQ contexts.
- Synchronous ZMQ service loops share `zmqthread.polling.poll_in()` for the common poll-for-readable pattern.

### Interrupts

- `interrupt_request` sends SIGINT and also attempts task cancellation for async cells.
- Subshell execution owns a `microio.CancelScope` that is active only while awaiting an async IPython cell; child-subshell async cells are cancelled through that scope, while sync cells still use thread/SIGINT interruption.
- Cancelled async tasks are translated into `KeyboardInterrupt` for parity.
- Interrupts cancel pending stdin waits and emit IOPub error messages.
- `shutdown_request` is idempotent once stopping begins; other control requests during stopping return `KernelStopping`.

### IOPub / Comm

- `execute_input` is emitted before any live stream/display output.
- Streams and display data are live when a sender is configured; otherwise they buffer and flush after execution.
- Inbound comm open/msg/close are routed to the `comm` package's `CommManager` (reachable as `get_ipython().kernel.comm_manager`), which drives the registered target/`on_msg`/`on_close` callbacks; inbound messages are not echoed back on IOPub.
- Outbound comms (`comm.create_comm(...).send(...)`, callback replies) publish on IOPub via `IpyminiComm.publish_msg`, parented to `kernel.current_parent()` — the shared `parent_header_var` for the active cell/comm-handler/spawned-thread, falling back to the kernel's last parent. The comm layer is bound to the kernel at startup via `set_kernel` (buffers preserved).

### History / inspect / completion

- History uses IPython’s `HistoryManager` for tail/search/range.
- inspect/complete/is_complete are delegated to IPython (bridge methods).

### Config and extensions

- IPython config/extensions/startup scripts are loaded via an `InteractiveShellApp` wrapper.
- The test `tests/test_ipython_startup_integration.py` exercises:
  - `ipython_kernel_config.py`
  - `profile_default/startup/*.py`
- `InteractiveShell.display_page` controls whether pager output is emitted as `display_data` (True) or reply payloads (False).

## Code reference

NB: `meta/` is not commited to git -- it is used for code reviews, timing details, etc.
