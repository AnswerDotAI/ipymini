# ipymini-term

`ipymini-term` provides small, testable utilities for terminal-style IO and display capture in interactive Python execution environments.

It was extracted from the experimental `ipymini` Jupyter kernel to make the IO/display boundary reusable and independently testable.

## What it does

- Thread-local redirection of:
  - `sys.stdout`, `sys.stderr`
  - `input()` and `getpass.getpass()`
  - `get_ipython()` (when IPython is installed)
- Stream buffering/coalescing via `MiniStream`
- IPython display capture via `IPythonCapture`
  - captures display events (`display_data`, `update_display_data`, `clear_output`)
  - captures last-result data + metadata (displayhook)

## Install

```bash
pip install ipymini-term
```

## Quick start: capture stdout/stderr events

```python
from ipymini_term import MiniStream

events = []
out = MiniStream("stdout", events)
err = MiniStream("stderr", events)

out.write("hello ")
out.write("world\n")
err.write(b"oops\n")

print(events)
# [
#   {"name": "stdout", "text": "hello world\n"},
#   {"name": "stderr", "text": "oops\n"},
# ]
```

## Quick start: thread-local IO redirection (input/getpass)

```python
from ipymini_term import MiniStream, thread_local_io
import getpass

events = []
stdout = MiniStream("stdout", events)
stderr = MiniStream("stderr", events)

def request_input(prompt: str, password: bool) -> str:
    if password:
        return "secret"
    return "Ada"

with thread_local_io(shell=None, stdout=stdout, stderr=stderr, request_input=request_input, allow_stdin=True):
    name = input("Name: ")
    pw = getpass.getpass("Password: ")
    print(f"hi {name}, pwlen={len(pw)}")

print("".join(e["text"] for e in events if e["name"] == "stdout"))
```

## IPython integration: `IPythonCapture`

If you have an `IPython.core.interactiveshell.InteractiveShell`, you can attach a capture object:

```python
from IPython.core.interactiveshell import InteractiveShell
from ipymini_term import IPythonCapture
shell = InteractiveShell.instance()

def request_input(prompt: str, password: bool) -> str: return "42"
cap = IPythonCapture(shell, request_input=request_input)
with cap.capture(allow_stdin=True, silent=False): shell.run_cell("print('hello')\n1+1")

payload = cap.consume_payload()
snapshot = cap.snapshot()
print(snapshot["streams"])
print(snapshot["result"])  # displayhook last result bundle
print(payload)
```

## API overview

- `MiniStream(name, events, sink=None)`
- `thread_local_io(shell, stdout, stderr, request_input, allow_stdin)` (auto-installs/reinstalls global IO hooks on first use)
- `IPythonCapture(shell, request_input=...)`

## License

Apache 2.
