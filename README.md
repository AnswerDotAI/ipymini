# ipyrust

`ipyrust` is an idiomatic Rust implementation of a Jupyter kernel for Python. The Rust core speaks the Jupyter messaging protocol over ZeroMQ, while Python execution is delegated to IPython for correctness and feature parity.

The design goal is: **a small, readable, testable Rust kernel** with **first‑class IPython behavior**.

---

## What we’re aiming to do

- Implement a full Jupyter kernel in Rust for Python.
- Match `xeus-python` behavior when it matters (IOPub ordering, message shapes, history, inspect, etc.).
- Use IPython instead of re‑implementing Python semantics.
- Expand protocol‑level tests (IOPub, interrupts, completions, etc.) to approach upstream parity.
- **Explicit non‑goal:** raw mode is not implemented.
- Follow ipykernel code closely where possible, including copying code from it directly where useful, but **never** import ipykernel directly

---

## Repository layout

- `src/`
  - `lib.rs`: shared Rust library (kernel core + interpreter trait)
  - `core/`
    - `kernel_core.rs`: protocol logic, execution queue, message dispatch
    - `zmq_kernel.rs`: ZMQ transport + Publisher implementation
    - `message.rs`: Jupyter message parsing/signing + connection types
    - `interpreter.rs`: language‑agnostic interpreter trait
    - `types.rs`: shared protocol types (execute result, streams, debug)
  - `python.rs`: Python interpreter implementation (PyO3 + bridge tasks)
  - `python_bridge.py`: IPython + debugpy bridge (execute, completion, display, debug)
  - `main.rs`: CLI entry (`ipyrust -f <connection_file>`)
- `tests/`
  - `test_ipyrust_kernel.py`: integration tests via `jupyter_kernel_test`
- `share/jupyter/kernels/ipyrust/kernel.json`: kernel spec
- Submodules
  - `xeus-python/`: reference C++ kernel (behavioral baseline and source of useful code)
  - `xeus/`: reference C++ infra (behavioral baseline and source of useful code)
  - `ipykernel/`: upstream Python kernel + tests (source of truth for behavior and source of useful code)
  - `jupyter_kernel_test/`: protocol test harness

---

## Architecture split (Rust vs Python)

We deliberately split responsibilities by language:

**Rust (generic Jupyter kernel core)**
- ZMQ transport, heartbeat, and channel polling
- Message parsing/signing + routing
- Protocol sequencing (busy/idle, execute flow)
- stdin plumbing and generic message publication

**Python (Python‑kernel specifics)**
- Execution semantics via IPython
- History (`history_request`) via IPython history manager
- Comm state + comm_info via Python comm manager (ipywidgets‑style)
- Completion, inspect, is_complete
- Display/pager/stream capture
- Debugpy/DAP handling

---

## How this relates to the submodules

- **xeus**` and **`xeus-python/`** is the architectural reference for how a full kernel behaves. We mirror its semantics when it makes sense, but use Rust‑native patterns.
- **`ipykernel/`** is the upstream kernel implementation. We borrow concepts and tests, and we use IPython’s internal APIs to match its behavior.
- **`jupyter_kernel_test/`** provides the protocol‑level test harness. We run it against `ipyrust` to validate our behavior.

---

## Requirements

### Rust + toolchain
- Rust 1.70+ (edition 2021)

### Python / Jupyter
- Python 3.11+ recommended (we’ve been testing with 3.12)
- `jupyter_client`, `jupyter_core`, `ipython`
- `pytest` if running tests

### ZMQ
- `libzmq` installed on the system
- `pkgconf` (or `pkg-config`) so `zmq-sys` can locate it

On macOS (Homebrew):

```
brew install libzmq pkgconf
```

---

## Build

We typically build with an explicit `CARGO_HOME` to avoid permissions issues:

```
CARGO_HOME=./.cargo cargo build
```

This produces `./target/debug/ipyrust`.

---

## Installing the kernel spec

You have a few options:

### Option A: Use the repo’s `JUPYTER_PATH`
Set `JUPYTER_PATH` to include the repo’s `share/jupyter`:

```
export JUPYTER_PATH=/path/to/ipyrust/share/jupyter:$JUPYTER_PATH
```

### Option B: Install the spec into your user Jupyter dir

```
jupyter kernelspec install --user /path/to/ipyrust/share/jupyter/kernels/ipyrust
```

After either option, you should see it in:

```
jupyter kernelspec list
```

---

## Running manually

`ipyrust` is a normal Jupyter kernel executable. It expects a connection file:

```
./target/debug/ipyrust -f /path/to/connection.json
```

(When run via Jupyter, that file is created and passed automatically.)

---

## Tests

All tests must pass before changes are considered complete:

```
pytest -q
scripts/cargo-test.sh
```

Rust unit tests on macOS can fail to find `libpython` due to absolute install names in some Python builds. The helper script sets `DYLD_LIBRARY_PATH` and `CARGO_HOME`, so use it instead of raw `cargo test`.

### Test harness notes (important)
The embedded Python interpreter in a Rust binary can be finicky on macOS. The test harness sets the following environment variables to ensure the embedded Python can find the standard library and site‑packages:

- `PYTHONHOME`
- `PYTHONPATH`
- `DYLD_LIBRARY_PATH`

See `tests/test_ipyrust_kernel.py` for the exact logic.

If you want to run the kernel outside tests, it should now self‑detect and set these paths at runtime when missing. Tests still set them explicitly for stability.

Environment discovery logic is centralized in `scripts/python_env.py` and reused by the build script, runtime probe, and test harness.
If you want to set these variables in your shell, run:

```
python scripts/python_env.py --exports
```

---

## Behavior implemented so far

- kernel_info replies
- stdout/stderr stream messages
- execute_result
- display_data + update_display_data
- clear_output
- comms (open/msg/close/info)
- history (tail/search)
- inspect
- is_complete
- completion (IPython, using `shell.complete` with jedi disabled for embedded mode)
- interrupts (message-based)
- stdin input requests
- pager payloads (`?` / `help`)
- iopub_welcome (XPUB)
- debug_request (debugpy in-process adapter: initialize/attach/evaluate)

---

## Known gaps / TODO (high level)

- Debugger / DAP support (expand beyond initialize/attach/evaluate)
- Interrupt handling while blocked on input
- Binary buffer support (might not be needed) + richer display metadata

For the full detailed plan, see `DEVLOG.md`.

---

## Development notes

- The Rust core is intentionally thin and reusable across languages: protocol, transport, sequencing.
- IPython owns Python‑specific behavior (execution, history, comm state); other languages can implement the same Interpreter trait.
- Raw mode is intentionally not supported.

---

## Where to start reading

- `src/core/zmq_kernel.rs` – message loop + transport wiring
- `src/core/kernel_core.rs` – protocol handling + execution sequencing
- `src/python_bridge.py` – IPython integration and output capture
- `tests/test_ipyrust_kernel.py` – protocol expectations
- `DEVLOG.md` – detailed progress notes and next steps
