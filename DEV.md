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

- Normal releases: `tools/release.sh [patch|minor|major]`.
- Initial PyPI permission setup (one‑time):

```
hatch build
twine upload dist/*
```

After the initial manual release, bump the version before running `tools/release.sh`.

## Where to start reading

- `ipymini/kernel.py` – protocol, ZMQ routing, subshells.
- `ipymini/shell/shell.py` – IPython integration and output capture.
- `tests/` – protocol expectations and integration behavior (organized by module).

## Architecture overview

Core flow:
- `MiniKernel` owns sockets, threads, and dispatch.
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
- Each `Subshell` holds a `MiniShell` (`self.shell`), which wraps an IPython `InteractiveShell` (`self.shell.ipy`).
- `Subshell.__init__` receives the `MiniKernel` as `kernel` and stores it via `store_attr`.
- `MiniKernel.shell` is a shortcut to `self.subshells.parent.shell` (the parent subshell's `MiniShell`).
- Child subshells (created via `SubshellManager.create`) share the same `user_ns` dict but get their own `MiniShell` and IPython instance.
- `get_ipython()` returns `MiniShell.ipy` (the `InteractiveShell` singleton for the parent, non-singleton for children).

### Execution model

- The parent subshell runs in the main thread, so SIGINT can interrupt running code without killing the kernel when idle.
- Each subshell has a persistent asyncio loop; code runs while the loop is running, so `asyncio.create_task(...)` works in sync cells.
- Output routing uses contextvars to associate streams/displays with the current parent message (works across tasks and user-launched threads).

### Router threads (shell/control)

- Shell/control ROUTER sockets run in background threads via `AsyncRouterThread`.
- The router thread is the only thread that touches its socket (thread‑safety).
- Outbound replies are enqueued; the router loop drains the queue after inbound messages to avoid starvation.
- Async sockets use `zmq.asyncio.Context.shadow(self.context)` to avoid multiple ZMQ contexts.

### Interrupts

- `interrupt_request` sends SIGINT and also attempts task cancellation for async cells.
- Cancelled async tasks are translated into `KeyboardInterrupt` for parity.
- Interrupts cancel pending stdin waits and emit IOPub error messages.

### IOPub / Comm

- `execute_input` is emitted before any live stream/display output.
- Streams and display data are live when a sender is configured; otherwise they buffer and flush after execution.
- Comm open/msg/close are routed through IPython’s comm manager and broadcast on IOPub (buffers preserved).

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

`links/` (not commited to git) contains source for reference projects such as ipykernel. These should be reviewed carefully since they are mature, well-tested solutions.

NB: `meta/` is not commited to git -- it is used for code reviews, timing details, etc.
