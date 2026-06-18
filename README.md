# ipymini

🛑 **WARNING** This is a very early, very experimental, purely research project for now. Have fun playing with it, but don't expect it to work, or be supported, or to still exist next week. Perhaps it'll turn out to be useful, and we'll invest in it and maintain it, in which case we'll remove this warning. If you play with it and find issues, have improvement ideas, etc, we're keen to hear, and to research together!

`ipymini` is a **Python-only** Jupyter kernel for Python with a small, readable codebase and strong IPython parity.

The design goal is: **a small, readable, testable kernel** with **first‑class IPython behavior**.

This was almost entirely implemented by AI, and no human currently fully understands all the generated code, so please be very careful, because we don't actually know what this code does. The AI closely referenced the `ipykernel`, `xeus`, `xeus-python`, and `jupyter_kernel_test` projects during development. So all credit for this project belongs to the authors of those packages, and to authors of the excellent documentation and specifications referred to (e.g DAP spec; JEPs; etc) - but of course all blame for mistakes is entirely our/AI's fault.

---

## What we’ve aimed to do

- Implement a full Jupyter kernel in pure Python.
- Match `ipykernel` behavior where it matters (IOPub ordering, message shapes, history, inspect, etc.).
- Use IPython instead of re‑implementing Python semantics.
- Expand protocol‑level tests (IOPub, interrupts, completions, etc.) to approach upstream parity.

---

## Requirements

- Python 3.11+
- `jupyter_client`, `jupyter_core`, `ipython`, `pyzmq`

If you need system ZMQ libs on macOS:

```
brew install libzmq
```

---

## Install

From PyPI:

```
pip install ipymini
```

Wheel installs include a kernelspec in the environment, so Jupyter from that environment should discover `ipymini` without a separate install step.

## Install (editable)

From the repo root:

```
pip install -e .
```

Optional test deps:

```
pip install -e ".[test]"
```

## Installing the kernel spec

For editable installs, use the built-in installer. It copies the repo kernelspec into a Jupyter kernels directory.

```
python -m ipymini install --user
```

Or install into the current environment:

```
python -m ipymini install --sys-prefix
```

After either option, you should see it in:

```
jupyter kernelspec list
```

Alternatively, use the repo’s `JUPYTER_PATH` during development.
Set `JUPYTER_PATH` to include the repo’s `share/jupyter`:

```
export JUPYTER_PATH=/path/to/ipymini/share/jupyter:$JUPYTER_PATH
```

---

## Running manually

`ipymini` is a normal Jupyter kernel executable. It expects a connection file:

```
python -m ipymini -f /path/to/connection.json
```

(When run via Jupyter, that file is created and passed automatically.)

---

## Concurrent execution helpers

Inside an ipymini cell, `get_ipython().kernel.unlock()` lets queued shell messages run while the current cell awaits. `get_ipython().kernel.subshell()` is a context manager that routes later execute requests from the same client session to a temporary subshell:

```
with get_ipython().kernel.subshell():
    await something()
```

The same helpers are also available as `from ipymini import unlock, subshell`.

---

## Configuring env and working directory

For per-launch configuration, rely on the kernel launcher:

- **KernelManager**: pass `env` and `cwd` to `start_kernel(...)`.
- **Kernelspec**: add an `"env"` dict to `share/jupyter/kernels/ipymini/kernel.json` for static defaults.

Example (KernelManager):

```
from jupyter_client import KernelManager

km = KernelManager(kernel_name="ipymini")
km.start_kernel(env={"MY_FLAG": "1"}, cwd="/path/to/workdir")
```

Optional env flags:
- `IPYMINI_STOP_ON_ERROR_TIMEOUT`: seconds to keep aborting queued executes after an error (default 0.0).

On POSIX, ipymini isolates the kernel into its own process group and terminates that group as the last shutdown step, so user-created child processes are cleaned up with the kernel. Nested ipymini kernels started by `KernelManager` watch their parent pid and shut themselves down when that parent exits. Windows does not provide this process-group cleanup guarantee; after normal cleanup the kernel process exits with `os._exit()`.

---

## Developer guide

See `DEV.md`.
