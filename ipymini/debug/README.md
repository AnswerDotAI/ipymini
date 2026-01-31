# ipymini-debug

`ipymini-debug` provides debugger integration (DAP + debugpy) and developer tracing utilities for interactive kernels and REPL-like runtimes.

It was extracted from the experimental `ipymini` Jupyter kernel so the debugger and tracing logic can be tested and documented independently.

## What it does

- Debug/diagnostic infrastructure:
  - env flag parsing (`IPYMINI_DEBUG`, `IPYMINI_DEBUG_MSGS`)
  - logging + faulthandler setup (including SIGUSR1 handler where supported)
  - structured message tracing (`trace_msg`)
- Debugger support:
  - Debug Adapter Protocol (DAP) request processing backed by `debugpy`
  - ZMQ STREAM-based client for the debugpy adapter
  - stable cell filename mapping for debug sessions (murmur2-based)

## Install

```bash
pip install ipymini-debug
```

## Enable dev debugging output

```bash
export IPYMINI_DEBUG=1
export IPYMINI_DEBUG_MSGS=1
```

In Python:

```python
import logging
from ipymini_debug import DebugFlags, setup_debug, trace_msg

log = logging.getLogger("demo")

flags = DebugFlags.from_env("IPYMINI")
setup_debug(flags)

msg = {"header": {"msg_type": "execute_request", "msg_id": "abc", "subshell_id": "worker-1"}}
trace_msg(log, "shell recv", msg, enabled=flags.trace_msgs)
```

## Cell filename mapping

```python
from ipymini_debug import debug_cell_filename

path = debug_cell_filename("print('hello')\n")
print(path)  # e.g. /tmp/ipymini_<pid>/<hash>.py
```

To override (useful for tests or deterministic debugging):

```bash
export IPYMINI_CELL_NAME=/tmp/my_cell.py
```

## Debugger (DAP) usage

Most users will not call this directly (your kernel will), but it’s fully usable standalone:

```python
from ipymini_debug import Debugger

dbg = Debugger(event_callback=None, zmq_context=None, kernel_modules=[])

# Example DAP initialize request shape:
req = {
  "type": "request", "seq": 1, "command": "initialize",
  "arguments": { "clientID": "demo", "adapterID": "python", "pathFormat": "path",
    "linesStartAt1": True, "columnsStartAt1": True, "supportsVariableType": True },
}

resp, events = dbg.process_request(req)
print(resp)
print(events)
```

Convenience for JSON wire format:

```python
import json
from ipymini_debug import Debugger

dbg = Debugger(event_callback=None, zmq_context=None, kernel_modules=[])
out = dbg.process_request_json(json.dumps(req))
print(out["response"], out["events"])
```

## API overview

- `DebugFlags.from_env(prefix="IPYMINI")`
- `setup_debug(flags)`
- `trace_msg(logger, prefix, msg, enabled=True)`
- `debug_cell_filename(code: str) -> str`
- `Debugger.process_request(request: dict) -> (response: dict, events: list[dict])`
- `Debugger.process_request_json(request_json: str) -> dict`
- `Debugger.trace_current_thread()`

## License

Apache 2.
