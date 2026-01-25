# ipymini

🛑 **WARNING** This is a very early, very experimental, purely research project for now. Have fun playing with it, but don't expect it to work, or be supported, or to still exist next week. Perhaps it'll turn out to be useful, and we'll invest in it and maintain it, in which case we'll remove this warning. If you play with it and find issues, have improvement ideas, etc, we're keen to hear, and to research together!

`ipymini` is a **Python-only** Jupyter kernel for Python with a small, readable codebase and strong IPython parity.

The design goal is: **a small, readable, testable kernel** with **first‑class IPython behavior**.

This was almost entirely implemented by AI, closely referencing the `ipykernel`, `xeus`, `xeus-python`, and `jupyter_kernel_test` projects during development. So all credit for this project belongs to the authors of those packages, and to authors of the excellent documentation and specifications referred to (e.g DAP spec; JEPs; etc).

---

## What we’ve aimed to do

- Implement a full Jupyter kernel in pure Python.
- Match `ipykernel` behavior where it matters (IOPub ordering, message shapes, history, inspect, etc.).
- Use IPython instead of re‑implementing Python semantics.
- Expand protocol‑level tests (IOPub, interrupts, completions, etc.) to approach upstream parity.

---

## Requirements

- Python 3.11+ recommended (we test with 3.12)
- `jupyter_client`, `jupyter_core`, `ipython`, `pyzmq`

If you need system ZMQ libs on macOS:

```
brew install libzmq
```

---

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

You have a few options:

### Option A: Use the built-in installer

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

### Option B: Install the spec into your user Jupyter dir

```
jupyter kernelspec install --user /path/to/ipymini/share/jupyter/kernels/ipymini
```

### Option C: Use the repo’s `JUPYTER_PATH`
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

---

## Developer guide

All contributor and architecture details have moved to `DEV.md`.
