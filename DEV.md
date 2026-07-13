# Developer Guide

This guide is for contributors working on ipymini. It explains how the kernel works and why it's built the way it is, then covers testing, style, and release mechanics. The user-facing story (install, kernelspec, running manually) is in `README.md`; per-package internals get more detail in the READMEs inside `ipymini/shell/`, `ipymini/term/`, `ipymini/zmqthread/`, and `ipymini/debug/`.

## Project goals

- A full Jupyter kernel in pure Python, with a small, readable, testable codebase.
- Strong IPython parity: use IPython's own machinery (execution, history, completion, inspection, display) rather than re-implementing Python semantics.
- Match ipykernel behavior where it matters: message shapes, IOPub ordering, history, interrupts, stop-on-error, debugger.
- Avoid incidental complexity: no traitlets, no tornado. Plain classes, dataclasses, and asyncio.

## What comes from ipykernel, and what's different

ipymini was written with `ipykernel`, `xeus-python`, and the Jupyter specs (messaging protocol, JEP 91 subshells, DAP) open in the next window, and the protocol surface deliberately matches ipykernel: the busy/execute_input/output/idle IOPub ordering, reply shapes and error/abort semantics, `stop_on_error` handling with an ipykernel-style timeout, comm integration through the standalone `comm` package, cell filenames hashed the same way so debugger frontends map breakpoints, and the same background-thread cast (heartbeat echo, IOPub sender, stdin handling, control separated from shell so interrupts work mid-cell). Where a client could tell the difference, the tests generally pin the ipykernel behavior.

The internals differ in a few deliberate ways:

- **No traitlets or tornado.** Configuration is constructor arguments and env vars; `jupyter_client.Session` is reused only for wire-format signing and (de)serialization. IPython config files still work through a real `InteractiveShellApp` (see Config below).
- **Shell and control are async ROUTER loops in their own threads.** ipykernel runs shell handlers on the main event loop with a separate control thread; ipymini gives each channel an `AsyncRouterThread` whose loop runs concurrent send and receive coroutines over a `zmq.asyncio` socket, with actual execution handed off to subshell threads.
- **Per-subshell IPython instances.** ipykernel implements JEP 91 subshells as threads sharing the one `InteractiveShell`. ipymini gives every subshell its own `InteractiveShell` (wrapped in `MiniShell`), sharing the parent's `user_ns` and in-memory history. That buys isolation (per-subshell execution state, cancel scopes, sys.stdout bindings) at the cost of per-subshell execution counts, so `In[n]`/`Out[n]` may not match prompt `n` when several subshells store history, and concurrent subshells at the same count overwrite each other's `Out[n]`.
- **Contextvars for parent and output routing.** ipykernel tracks one mutable "current parent" per channel; anything printed while a message is being handled is attributed to it. ipymini stores the parent header and idents in `ContextVar`s set around each message, and patches `threading.Thread.start` and `ThreadPoolExecutor.submit` to copy the context, so output from concurrent cells, asyncio tasks, user-spawned threads, and comm callbacks all route to the message that caused them. This is what makes the concurrency features below safe.
- **microio for thread/loop plumbing.** The cross-thread primitives (supervised service threads, mailboxes, cancel scopes, request registries) live in the sibling `microio` package rather than ad hoc queues and flags. Its README is short and worth reading once; the summary is: startup failures raise in the starter, stop is durable state rather than a flag a loop might miss, blocked waiters get failed rather than stranded, and cancellation works from any thread (including signal handlers) without leaking into unrelated code.
- **In-cell concurrency opt-ins.** `unlock()` and `subshell()` (below) have no ipykernel equivalent.
- **Process lifecycle.** On POSIX the kernel isolates itself into its own process group at startup and terminates that group as the very last shutdown step, so user-created child processes die with the kernel. Kernels launched by `KernelManager` also watch `JPY_PARENT_PID` (via `watchpid`) and shut down when their launcher exits. `SIGUSR1` dumps all thread stacks via `faulthandler` for stuck-kernel diagnosis. Windows skips the process-group teardown and exits with `os._exit()` after normal cleanup.

## Source layout

- `ipymini/kernel.py`: the protocol layer. `MiniKernel` (sockets, threads, dispatch, lifecycle), `Subshell`/`SubshellManager` (execution workers), connection info, interrupt/shutdown handling.
- `ipymini/shell/`: the IPython layer. `MiniShell` wraps `InteractiveShell`: execute, complete, inspect, history, debug bridging, comm wiring (`shell/comms.py`).
- `ipymini/term/`: IO capture. Thread-local stdout/stderr/input/getpass/get_ipython, stream buffering, display hooks.
- `ipymini/zmqthread/`: one file per socket personality: async router (shell/control), IOPub publisher, stdin router, heartbeat.
- `ipymini/debug/`: DAP debugger over debugpy, cell-filename hashing, debug env flags and logging.
- `ipymini/concur.py`: the `unlock()` and `subshell()` user-facing helpers.
- `ipymini/comms.py`, `ipymini/bridge.py`: thin re-export shims.
- `ipymini/__main__.py`: CLI entry (`run`, and `install` for the kernelspec).
- `tests/`: protocol and behavioral tests, organized by module; `tests/compat/` exercises the kernel through unpatched jupyter_client.

## The kernel object graph

`MiniKernel` owns the ZMQ context, the `Session`, and the socket threads. It creates a `SubshellManager`, which creates the parent `Subshell` immediately and child subshells on demand; all subshells share one `user_ns` dict. Each `Subshell` builds a `MiniShell`, which owns the `InteractiveShell` (`shell.ipy`): the IPython singleton for the parent, non-singleton instances for children. `Subshell` also sets `ipy.kernel = kernel`, so `get_ipython().kernel` works everywhere (ipywidgets and friends expect it), and `MiniKernel.get_parent()` exists for the same reason. `MiniKernel.shell` is a shortcut to the parent subshell's `MiniShell`.

The parent subshell runs its asyncio loop in the **main thread** (that's what `MiniKernel.start` blocks on), which is what lets SIGINT interrupt running user code without killing the kernel. Child subshells run their loops in daemon threads. Every subshell's loop persists across cells, so `asyncio.create_task(...)` in one cell keeps running while later cells execute.

## Life of an execute_request

The shell router thread receives and verifies the message (HMAC via `Session`; duplicate signatures are silently dropped as replays) and calls `handle_shell_msg`, still on the router thread. Routing picks a subshell: the JEP 91 `subshell_id` header if present, else an active `subshell()` route override for that client session, else the parent. `Subshell.submit` then queues it: execute_requests go into the subshell's `microio.ActorCore` mailbox, which hands them out one at a time (the "cell baton"), while every other message type is posted straight onto the subshell's loop so completion and inspection stay responsive during a long-running cell.

When the mailbox releases the message, `_handle_message` sets the shared parent-header/idents `ContextVar`s for the duration of handling, and `_handle_execute` takes over: send `status: busy`, emit `execute_input` (unless silent), stash the release callback and subshell into the contextvars that power `unlock()`/`subshell()`, and enter the shell's `execution_context`. That context resets per-request capture state, sets "live output" flags, and pushes the thread-local IO bindings: this subshell's IPython instance, its stdout/stderr capture streams, and its `request_input` callback become what `sys.stdout`, `get_ipython()`, and `input()` resolve to for code running in this execution context.

`MiniShell.execute` then runs the cell through IPython. Async-capable cells are awaited via `run_cell_async` inside a `microio.CancelScope` registered with the subshell's `ScopeGroup`, which is how interrupts cancel them; sync cells run under a context that marks the subshell as synchronously executing so an interrupt knows to inject `KeyboardInterrupt` instead. While the cell runs, `print` output flows `_ThreadLocalStream -> MiniStream` (line-buffered) into the subshell's stream sender, which publishes IOPub `stream` messages parented by the contextvar; display calls flow through `MiniDisplayPublisher` the same way. When no live sender is configured (unit tests driving a bare `MiniShell`), the same events buffer and come back in the result snapshot instead.

Afterwards, errors become IOPub `error` messages (a cancelled async cell is translated to `KeyboardInterrupt` for ipykernel parity), a final displayhook value becomes `execute_result`, and the reply (status, execution_count, user_expressions, payload) is enqueued on the router's outbox, which its send coroutine drains concurrently with new inbound traffic. On error with `stop_on_error`, already-queued executes are drained and answered with aborted replies; if `IPYMINI_STOP_ON_ERROR_TIMEOUT` is set, late-arriving executes keep aborting until it expires. Finally `status: idle`.

## In-cell concurrency: unlock() and subshell()

A subshell is strictly FIFO by default: one cell finishes before the next starts. Two helpers, importable as `from ipymini import unlock, subshell` or reachable as `get_ipython().kernel.unlock()` / `.subshell()`, let a running cell opt out of that.

`unlock()` releases the cell baton early: queued execute_requests start running on the same subshell's loop while the current cell continues. It returns `False` outside an ipymini cell and is irreversible for the remainder of the cell. The typical use is a cell that awaits something long-lived (a server, a watch loop, a long download) while the user keeps working. Since both cells share one event loop, this interleaves awaits rather than running CPU work in parallel, and the contextvar routing keeps each cell's output attached to its own request.

`subshell()` is a context manager that creates a temporary subshell and routes execute_requests arriving from the same client session into it while the body runs, then tears it down. Unlike `unlock()`, later cells run on a separate thread with their own IPython instance (sharing `user_ns`), so a sync cell in the subshell genuinely runs concurrently with the awaiting body. This gives frontends with no JEP 91 support (they never set the `subshell_id` header) working background cells. Only one route override can be active at a time, and requests from other client sessions are unaffected.

## Output, stdin, and get_ipython routing (term/)

`term/io.py` installs process-global hooks once: `sys.stdout`/`sys.stderr` are replaced with streams that resolve their target per execution context, `input()`/`getpass()` become dispatchers that forward to the kernel's stdin machinery (raising `StdinNotImplementedError` when `allow_stdin` is false), and `get_ipython` (the builtin, the IPython module attribute, and the `user_ns` binding) resolves to whichever subshell's shell is bound in the current context. The reach of "current context" is the point: `threading.Thread.start` is patched to capture `contextvars.copy_context()` at spawn time and run the thread body inside it, and `ThreadPoolExecutor.submit` does the same per work item, so a `print` from a thread the user started three cells ago still routes to the cell that spawned it.

`MiniDisplayHook` keeps the last displayed result in contextvars too, so concurrent unlocked cells can't clobber each other's `Out` values mid-flight. `execute_input` always precedes any live output for a cell because it's published before execution begins.

Stdin requests flush the captured streams, then send `input_request` through the stdin router thread and block on a `microio.RequestRegistry` waiter, matched back to the requester by parent msg_id with a client-identity fallback. Interrupts fail all pending waiters with `KeyboardInterrupt`; router shutdown fails them with a `RuntimeError` instead of leaving them hanging.

## IOPub

`IOPubThread` owns the PUB socket and a FIFO queue. `kernel.iopub` is a small proxy (`IOPubCommand`) that turns attribute access into typed sends: `kernel.iopub.stream(parent, name=..., text=...)`, `.display_data(...)`, `.status(...)`, and so on all funnel through `iopub_send`. The queue is bounded (`IPYMINI_IOPUB_QMAX`, default 10000): when a cell floods output, non-status messages past the limit are dropped with a warning, but `status` messages are never dropped, so clients' busy/idle picture stays truthful.

## Interrupts

`interrupt_request` arrives on the control router. The kernel sends SIGINT (to its process group when it's the leader, so user subprocesses feel it too), asks every subshell to interrupt, and cancels pending stdin waits. The SIGINT handler in the main thread interrupts child subshells, then the parent: if the parent is awaiting an async cell, its cancel scope is cancelled (with `latch=True`, closing the race where the interrupt lands just as a new scope opens); if it's inside a sync cell on the main thread, plain `KeyboardInterrupt` is raised. Child subshells cancel async cells through their scope group and inject `KeyboardInterrupt` into sync ones via `PyThreadState_SetAsyncExc`. Cancelled tasks surface to the client as `KeyboardInterrupt`, matching ipykernel.

## Comms

Inbound `comm_open`/`comm_msg`/`comm_close` are dispatched to the `comm` package's `CommManager` (also exposed as `kernel.comm_manager` for ipywidgets-style discovery), inside an output context so a callback's prints and displays route to the inbound message; inbound comm traffic is not echoed back on IOPub. Outbound comms (`comm.create_comm(...).send(...)` and callback replies) publish through `IpyminiComm.publish_msg`, parented to `kernel.current_parent()`: the contextvar parent when inside a cell, comm handler, or thread spawned from one, else the kernel's last parent. `shell/comms.py` binds the process-global comm layer to the kernel via `set_kernel` at kernel init. `comm_info_request` is answered from the manager's live comm table.

## History, inspect, completion

All three delegate to IPython: `HistoryManager` tail/range/search for `history_request`, `object_inspect_mime` for `inspect_request`, the `Completer` (with rectified completions and `_jupyter_types_experimental` metadata) for `complete_request`, and the input transformer's `check_complete` for `is_complete_request`. `IPYMINI_USE_JEDI` overrides IPython's jedi setting. Child subshells re-point their in-memory history at the parent's, so `In`/`Out` in the shared namespace stay linked to the parent `HistoryManager` (with the execution-count caveat noted above).

## Debugger

`debug_request` on control is bridged through `MiniShell.debug_request` to `debug/dap.py`: a DAP handler in front of debugpy, connected over a private ZMQ pair, with the reader as a supervised `ServiceThread` so pending requests fail loudly if it dies. Cell code is written to temp files named by a murmur2 hash of the source (`IPYMINI_CELL_NAME` overrides), the same scheme ipykernel uses, so debugger frontends can map cells to breakpoints. Debug events are published on IOPub against the current parent. `kernel_info` advertises both `"debugger"` and `"kernel subshells"` in `supported_features`.

## Startup and shutdown

`run_kernel` restores the default SIGINT handler, makes the process a group leader (POSIX), and starts the kernel. `MiniKernel.start` brings services up in order, and `microio.ServiceGroup.wait_started` means a socket that fails to bind raises here rather than leaving a half-alive kernel: first the IOPub/stdin/heartbeat threads, then signal handlers, the parent-pid watcher, the SIGUSR1 stack-dump hook, then the shell and control routers, and finally the parent subshell's loop on the main thread, where it blocks until shutdown. A crashed critical thread triggers a failed shutdown via the installed thread excepthook.

`shutdown_request` (either channel) is idempotent: the first one records the restart flag and starts a waiter thread that confirms the reply actually went out on the wire before calling `request_stop`, so the client always sees `shutdown_reply`. `request_stop` closes the stop scope, fails pending stdin waiters, interrupts running cells, and stops the parent loop; the finalizer then stops routers, heartbeat, subshells, and the stdin/IOPub threads (join timeouts checked, not assumed), restores signal handlers, and, as the very last act on POSIX, SIGTERMs then SIGKILLs its own process group. Control requests that arrive while stopping get a `KernelStopping` error reply instead of silence.

## Config and extensions

IPython configuration works the standard way: a minimal `InteractiveShellApp` subclass loads `ipython_kernel_config.py`, configured extensions, and `profile_default/startup/*.py` into the shell at first construction. `tests/test_ipython_startup_integration.py` exercises both paths. `InteractiveShell.display_page` picks whether pager output becomes `display_data` (True) or reply payloads (False).

## Optional env flags

- `IPYMINI_STOP_ON_ERROR_TIMEOUT`: seconds to keep aborting queued executes after an error (default 0.0).
- `IPYMINI_USE_JEDI=0|1`: override IPython's jedi setting.
- `IPYMINI_CELL_NAME`: override the debug cell filename.
- `IPYMINI_IOPUB_QMAX` / `IPYMINI_IOPUB_SNDHWM`: IOPub queue bound (default 10000) and socket send high-water mark.
- `IPYMINI_DEBUG` / `IPYMINI_DEBUG_MSGS`: verbose kernel logging / per-message trace logging (see `ipymini/debug/README.md`).

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

The vanilla-client compat tests (`tests/compat/`) exercise the kernel through *unpatched* jupyter_client (the shape nbclassic/nbclient use). They run in the default suite: `tests/compat` sorts first in collection, so xdist's loadfile scheduler makes it a worker's first file, in a process that has never constructed a `ConKernelClient` (importing conkernelclient doesn't patch). A guard test fails loudly if that ordering assumption is ever broken.

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

- Prefer protocol-level tests using `KernelManager` and helpers in `tests/kernel_utils.py`.
- Use `KernelHarness` (fixture in `tests/conftest.py`) for minimal, readable protocol-script tests.
- Avoid large sleeps and long timeouts; use monotonic timeouts and explicit status waits.
- For blocking debugger behavior, always use a separate process.

## Style guide (fastai)

We follow the fastai style guide (`style.md`):
- Favor brevity; one-liners for single statements (including `if/for/try/with`).
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

## Code reference

NB: `meta/` is not commited to git -- it is used for code reviews, timing details, etc.
