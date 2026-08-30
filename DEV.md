# Developer guide

ipymini supplies Python language semantics to the Rust [kernmini](https://github.com/AnswerDotAI/kernmini) engine. IPython handles execution, history, completion, inspection, display, magics, configuration, and debugging. Kernmini handles Jupyter transport, messages, queues, output, stdin, interrupts, subshells, and lifecycle.

The user-facing installation and kernelspec instructions are in `README.md`. Kernmini's `DEV.md` documents the language boundary and protocol engine.

## Project goals

- Keep the Jupyter engine in Rust and Python semantics in IPython.
- Use IPython machinery instead of reproducing Python behavior.
- Match ipykernel where clients can tell: message content, output ordering, history, inspection, completion, comms, debugging, and interrupts.
- Avoid traitlets and Tornado in the kernel host. IPython configuration remains supported through `InteractiveShellApp`.

## Source layout

- `ipymini/kernel.py`: a shell factory passed to the synchronous Rust-backed `kernmini.run_kernel`.
- `ipymini/shell/`: the IPython language adapter: execution, completion, inspection, history, comm binding, and debugging.
- `ipymini/term/`: Python stdout/stderr, input, display, and `get_ipython()` capture.
- `ipymini/debug/`: DAP/debugpy integration and ipykernel-compatible cell filenames.
- `ipymini/__main__.py`: CLI entry and kernelspec installation.
- `tests/`: protocol and behavioral integration tests, including unmodified jupyter_client clients.

## Startup and sessions

`ipymini.kernel.run_kernel` creates a closure around one shared namespace and passes it to kernmini. The first factory call creates the parent `InteractiveShell` singleton. Child calls create independent `InteractiveShell` instances sharing that namespace and the parent's in-memory history.

The parent shell runs on a persistent loopmini loop in the Python main thread. Each child runs on its own OS thread and persistent loopmini loop. Kernmini's Tokio runtime drives protocol and control work independently, so a synchronous Python cell cannot block the kernel engine or another subshell.

The executable requests process-group ownership. On POSIX this isolates the kernel and lets kernmini terminate user-created child processes during shutdown. Kernmini also watches the original parent PID. Embedded callers can disable process-group ownership, but the ipymini executable does not.

## Life of an execute request

The Rust engine validates and queues the request, publishes `busy` and `execute_input`, then calls `MiniShell.execute` with an execution-scoped context. The PyO3 adapter installs live stream, display, and input senders on each shell and stores the current Rust `ExecutionContext` in a Python ContextVar.

`MiniShell.execute` runs the cell through IPython's `run_cell_async`. Its execution context resets capture state and binds this shell's IPython instance, stdout, stderr, input, and display hooks. The result is returned as a MIME bundle, error, user expressions, and payload; kernmini publishes the corresponding Jupyter events and reply before `idle`.

Synchronous Python records its thread ID while running. Async cells remain ordinary loopmini tasks. Kernmini uses those two facts to inject `KeyboardInterrupt` into synchronous Python or cancel an async task without allowing SIGINT to escape the host loop.

## Output, input, and context

`term/io.py` installs process-wide dispatchers for `sys.stdout`, `sys.stderr`, `input`, `getpass`, and `get_ipython`. Their targets come from execution ContextVars. `threading.Thread.start` and `ThreadPoolExecutor.submit` copy the current context, so output from user-created threads remains attributed to the cell that created them.

`MiniStream` is a file-like stdout/stderr sink. During kernel execution it sends complete lines through kernmini's live stream callback; bare-shell unit tests can instead retain and coalesce events. `MiniDisplayPublisher` and `MiniDisplayHook` do the same for rich display and final expression values.

The adapter's input callback crosses into Rust, which sends `input_request` to the correct client and blocks only the calling Python thread until `input_reply`. An interrupt completes the request with `KeyboardInterrupt`.

## Concurrent execution

Each language session executes one cell at a time. Completion, inspection, history, comms, debugging, and control requests remain responsive while it runs.

`subshell()` temporarily routes subsequent execute requests from the same client session to a child shell. The child has its own thread and IPython instance but shares the namespace. This allows genuinely concurrent synchronous work without requiring frontend JEP 91 support.

`sidecar()` uses the same routing mechanism but retains the named kernel-wide `sidecar` subshell for reuse. Both helpers are available through `from ipymini import sidecar, subshell` and `get_ipython().kernel`.

Kernmini also recognizes `priority` and `hold` execute metadata. These are Answer.AI extensions; normal Jupyter clients are unaffected.

## Comms

`MiniShell.comm_info` and `MiniShell.message` adapt the standalone `comm` package to kernmini's language boundary; kernmini contains no IPython-specific comm code. `shell/comms.py` binds outgoing messages to kernmini's small Python kernel proxy. Inbound comm messages run under an output context, so callback output has the inbound message as its parent. Outbound comm messages publish through the current Rust execution context, including from threads spawned by a cell.

## Completion, inspection, history, and configuration

These delegate directly to IPython: `HistoryManager`, `object_inspect_mime`, the IPython completer, and the input transformer's completeness checker. `IPYMINI_USE_JEDI` overrides the completer's Jedi setting.

A small `InteractiveShellApp` loads `ipython_kernel_config.py`, configured extensions, and `profile_default/startup/*.py` during first shell construction. `tests/kernel/test_ipython_startup_integration.py` covers both paths.

## Debugging

`debug/dap.py` implements Python-specific Jupyter DAP requests in front of debugpy. Kernmini's Rust `DapClient` owns byte-framed TCP, request correlation, timeouts, events, and connection failure. ipymini owns debugpy startup, Python tracing, cell sources, variable inspection, and IPython integration.

`debug/cells.py` hashes cell source with the same Murmur2 algorithm and seed as ipykernel, allowing debugger frontends to map notebook cells to temporary Python files. `KERNMINI_CELL_NAME` overrides that filename.

## Environment variables

- `IPYMINI_USE_JEDI=0|1`: override IPython's Jedi setting.
- `KERNMINI_CELL_NAME`: override the debugger cell filename.
- `KERNMINI_HOLD_TIMEOUT`: held-execution backstop in seconds, default 3600.
- `KERNMINI_IOPUB_QMAX`: Rust IOPub queue capacity, default 10000.

## Tests

Run non-slow tests:

```bash
pytest -q
```

Run the full suite once, including slow lifecycle stories:

```bash
tools/run_tests.sh
```

The vanilla-client compatibility tests use unmodified jupyter_client. Most protocol stories use `ConKernelClient` through `tests/aclient.py`; use the raw `tests/kernel_utils.py` harness only when the client or transport shape is the subject.

Prefer narrative protocol tests over unit tests of private machinery. Avoid sleeps where a protocol event can provide synchronization.

## Style and releases

Use fastai Python style and run `chkstyle` after Python edits. Release through the repository's fastship workflow.
