# ipymini-shell

`ipymini-shell` is the IPython integration layer for ipymini. It bundles:

- `MiniShell`: a small wrapper around `InteractiveShell` that executes code, captures outputs, and normalizes errors.
- `get_comm_manager` / `set_kernel`: comm wiring. `set_kernel(kernel)` binds the global comm layer to a running kernel; outbound comms then publish on its IOPub, parented via `kernel.current_parent()`.

The goal is to keep IPython-specific wiring in one place so the kernel can stay focused on protocol, routing, and lifecycle.

## Install

```bash
pip install -e .
```

## Quick start

```python
from ipymini_shell import MiniShell

def request_input(prompt: str, password: bool) -> str: return "Ada" if not password else "secret"
shell = MiniShell(request_input=request_input)

with shell.execution_context(allow_stdin=True, silent=False):
    result = await shell.execute("print('hello')\n1+1", silent=False, store_history=True)

print(result["result"])   # last displayhook result bundle
print(result["streams"])  # captured stdout/stderr events
```

## API

### `MiniShell`

```python
MiniShell(
    request_input: Callable, debug_event_callback: Callable|None = None,
    zmq_context: zmq.Context|None = None, user_ns: dict|None = None,
    use_singleton: bool = True)
```

Key methods:

- `execution_context(allow_stdin, silent)` — binds per-request IO capture.
- `execute(code, silent=False, store_history=True, user_expressions=None, allow_stdin=False)` — runs code and returns a snapshot dict.
- `complete(code, cursor_pos=None)`, `inspect(code, cursor_pos=None, detail_level=0)`, `is_complete(code)`, `history(...)`
- `set_stream_sender(...)`, `set_display_sender(...)`
- `debug_request(request_json)` — DAP request handler

### `set_kernel`

```python
from ipymini_shell import set_kernel
set_kernel(kernel)  # kernel must expose `.iopub.send(...)` and `.current_parent()`
```

Binds the process-global comm layer to a kernel. Outbound comms (`comm.create_comm(...).send(...)`) then publish on that kernel's IOPub, parented to `kernel.current_parent()` — the active cell, comm handler, or thread it spawned. With no kernel bound, outbound comms are dropped.

## Dependencies

- `ipython>=8.18`
- `ipymini-term` (capture + thread-local IO)
- `ipymini-debug` (debugger wiring)
- `comm` (Jupyter comms primitives), `fastcore`, `pyzmq`

## Status

Early API. The surface area is intentionally small to make it easy to evolve independently.

## License

Apache 2.

