# ipymini-shell

`ipymini-shell` is the IPython integration layer for ipymini. It bundles:

- `MiniShell`: a small wrapper around `InteractiveShell` that executes code, captures outputs, and normalizes errors.
- `comm_context`: a tiny context manager used by comm publish helpers to attach parent metadata.

The goal is to keep IPython-specific wiring in one place so the kernel can stay focused on protocol, routing, and lifecycle.

## Install

```bash
pip install -e .
```

## Quick start

```python
from ipymini_shell import MiniShell, comm_context

def request_input(prompt: str, password: bool) -> str: return "Ada" if not password else "secret"
shell = MiniShell(request_input=request_input)

def iopub_send(msg_type, parent, content=None, **kwargs): print(msg_type, content)
parent = {"header": {"msg_id": "demo"}}
with shell.execution_context(allow_stdin=True, silent=False, comm_sender=iopub_send, parent=parent):
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

- `execution_context(allow_stdin, silent, comm_sender, parent)` — binds per-request IO capture + comm context.
- `execute(code, silent=False, store_history=True, user_expressions=None, allow_stdin=False)` — runs code and returns a snapshot dict.
- `complete(code, cursor_pos=None)`, `inspect(code, cursor_pos=None, detail_level=0)`, `is_complete(code)`, `history(...)`
- `set_stream_sender(...)`, `set_display_sender(...)`
- `debug_request(request_json)` — DAP request handler

### `comm_context`

```python
with comm_context(sender, parent):
    ...
```

Attaches the parent header and IOPub sender used by comms.

## Dependencies

- `ipython>=8.18`
- `ipymini-term` (capture + thread-local IO)
- `ipymini-debug` (debugger wiring)
- `comm` (Jupyter comms primitives), `fastcore`, `pyzmq`

## Status

Early API. The surface area is intentionally small to make it easy to evolve independently.

## License

Apache 2.

