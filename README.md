# ipymini

`ipymini` is a **Python-only** Jupyter kernel for Python with a small, readable codebase and strong IPython parity.

The design goal is: **a small, readable, testable kernel** with **first‑class IPython behavior**.

---

## What we’re aiming to do

- Implement a full Jupyter kernel in pure Python.
- Match `ipykernel` behavior where it matters (IOPub ordering, message shapes, history, inspect, etc.).
- Use IPython instead of re‑implementing Python semantics.
- Expand protocol‑level tests (IOPub, interrupts, completions, etc.) to approach upstream parity.
- **Explicit non‑goal:** raw mode is not implemented.

---

## Repository layout

- `ipymini/`
  - `kernel.py`: kernel loop + Jupyter protocol handling (ZMQ)
  - `bridge.py`: IPython integration (execute, display, history, comms, debug)
  - `__main__.py`: CLI entry (`python -m ipymini -f <connection_file>`)
- `ipymini_bridge.py`: comm manager surface for tests
- `pytests/`: protocol + behavior tests
- `share/jupyter/kernels/ipymini/kernel.json`: kernel spec
- `ipykernel/`, `xeus/`, `xeus-python/`: reference implementations kept for comparison

---

## Requirements

### Python / Jupyter
- Python 3.11+ recommended (we test with 3.12)
- `jupyter_client`, `jupyter_core`, `ipython`, `pyzmq`
- `pytest` if running tests
- `ipykernel` is a test-only dependency (used by some e2e/debug harnesses)

### ZMQ
`pyzmq` bundles or links against `libzmq`. If you need to install system libs:

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

### Option A: Use the repo’s `JUPYTER_PATH`
Set `JUPYTER_PATH` to include the repo’s `share/jupyter`:

```
export JUPYTER_PATH=/path/to/ipymini/share/jupyter:$JUPYTER_PATH
```

### Option B: Install the spec into your user Jupyter dir

```
jupyter kernelspec install --user /path/to/ipymini/share/jupyter/kernels/ipymini
```

### Option C: Use the built-in installer

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

---

## Running manually

`ipymini` is a normal Jupyter kernel executable. It expects a connection file:

```
python -m ipymini -f /path/to/connection.json
```

(When run via Jupyter, that file is created and passed automatically.)

---

## Tests

All tests must pass before changes are considered complete:

```
pytest -q
```

Note: e2e tests launch the kernel via `jupyter_client.KernelManager` in a separate process; make sure the kernelspec is discoverable (see `JUPYTER_PATH` above).

Note: debugger breakpoint-stop tests are enabled and pass; the kernel forces `PYDEVD_USE_SYS_MONITORING=0` to avoid sys.monitoring stalls (see `DEVLOG.md`).

Optional fuzz test (disabled by default):

```
IPYMINI_FUZZ=1 pytest -q pytests/test_kernel_subshells.py -k fuzz
```

---

## Behavior implemented so far

- kernel_info replies
- connect_request replies
- stdout/stderr stream messages
- execute_result
- display_data + update_display_data
- clear_output
- comms (open/msg/close/info)
- history (tail/search/range)
- inspect
- is_complete
- completion (IPython-based; can use Jedi and the experimental completion API)
- interrupts (signal‑based)
- stop_on_error (aborts queued execute requests by default)
- stdin input requests
- kernel subshells (create/list/delete; concurrent shell handling; per‑subshell execution counts/history)
- pager payloads (`?` / `help`)
- set_next_input payloads (coalesced to one per execute)
- iopub_welcome (XPUB)
- debug_request (debugpy in-process adapter: initialize/attach/evaluate, breakpoints, stackTrace/scopes/variables, continue)

---

## Design notes

- Interrupts use the kernelspec `interrupt_mode = signal`; the kernel restores the default SIGINT handler so IPython turns interrupts into `KeyboardInterrupt`.
- `set_next_input` is injected onto the IPython shell to emit the expected payloads.
- stop_on_error aborts queued *execute* requests only; non-execute requests still return replies.
- Subshells run in per‑subshell threads with a shared user namespace and thread‑local IO routing.

---

## Known gaps / TODO (high level)

- Expand debugger/DAP coverage (step/next/stepOut, exception breakpoints, disconnect/terminate semantics)
- Interrupt handling while blocked on input
- Binary buffer support (might not be needed) + richer display metadata

For the full detailed plan, see `DEVLOG.md`.

---

## Completion configuration (no traitlets required)

`ipymini` reads simple environment flags at startup:

- `IPYMINI_USE_JEDI=0|1` (defaults to IPython’s own default)
- `IPYMINI_EXPERIMENTAL_COMPLETIONS=0|1` (default: enabled if IPython supports it)

---

## Where to start reading

- `ipymini/kernel.py` – message loop + protocol handling
- `ipymini/bridge.py` – IPython integration and output capture
- `pytests/` – protocol expectations
- `DEBUG.md` – debugger test flow + failure modes
- `DEVLOG.md` – detailed progress notes and next steps
