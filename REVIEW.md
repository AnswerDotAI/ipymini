# Code Review: Duplication / Simplification / Tech Debt

## Scope
- Read first: `README.md`, `DEVLOG.md`.
- Reviewed: `src/`, `tests/`, `scripts/`, `build.rs`, `Cargo.toml`.
- Excluded: `xeus/`, `xeus-python/`, `ipykernel/`, `jupyter_kernel_test/` (submodules).

## Findings (ordered by impact)

1) **KernelCore reply boilerplate repeated across multiple handlers**
   - **Where:** `src/core/kernel_core.rs:243-310` (`reply_comm_info`, `reply_complete`, `reply_inspect`, `reply_history`, `reply_is_complete`).
   - **Issue:** Each function repeats the same pattern: parse request, call interpreter, `recv_timeout(Duration::from_secs(10))`, map `InterpreterResult::{Expected, Error, other}` into a JSON error wrapper.
   - **Why it matters:** Any future change to error schema, timeout, or logging requires editing in several places and can easily drift.
   - **Suggestion:** Introduce a small helper (e.g., `fn recv_or_error<T>(rx, expected_variant) -> Value`) or a generic `reply_with_interpreter` that handles the timeout + error JSON mapping. This would also centralize the timeout constant.

2) **Shutdown handling duplicated for shell/control**
   - **Where:** `src/core/kernel_core.rs:325-345` (`reply_shutdown`, `reply_shutdown_control`).
   - **Issue:** The methods are identical except for the channel used to reply.
   - **Why it matters:** This is a classic maintenance hazard if shutdown semantics change.
   - **Suggestion:** Extract a `handle_shutdown(&mut self, send_reply: fn(...))` helper or return the content and reuse a single shutdown routine in both paths.

3) **Comm handling repeats content construction + interpreter dispatch**
   - **Where:** `src/core/kernel_core.rs:384-435` (`handle_comm_open`, `handle_comm_msg`, `handle_comm_close`).
   - **Issue:** Each function builds a `content` JSON, publishes it, then pulls values back out of that JSON to call the interpreter. This repeats across three handlers and serializes/deserializes fields that are already in typed structs.
   - **Why it matters:** Duplication + unnecessary conversions make it easy to introduce inconsistencies (e.g., if metadata defaults change).
   - **Suggestion:** Factor a shared helper that takes a typed request and a closure for the interpreter call; or pass the original `req` fields directly to the interpreter and only build JSON for publication.

4) **Error JSON + timeout constants are duplicated in multiple spots**
   - **Where:** `src/core/kernel_core.rs:243-310` and `src/core/kernel_core.rs:610-624`.
   - **Issue:** The same `{"status":"error","ename":"PythonError",...}` blob and `Duration::from_secs(10)` appear in several functions.
   - **Why it matters:** A change to error format or timeout will require multiple edits and could leave inconsistent responses across endpoints.
   - **Suggestion:** Define a small helper for building error responses and use a single `const DEFAULT_INTERPRETER_TIMEOUT: Duration`.

5) **ZmqPublisher send_* methods share near-identical frame building**
   - **Where:** `src/core/zmq_kernel.rs:162-242` (`send_shell_reply`, `send_control_reply`, `send_iopub`, `send_input_request`).
   - **Issue:** Each method computes session/username, calls `build_reply`, and then `send_multipart` with different sockets/identities.
   - **Why it matters:** Frame-building logic is duplicated four times and can drift (e.g., if header fields change or a bug fix is needed).
   - **Suggestion:** Add a private helper like `send_reply(socket, identities, msg_type, content, parent_header)` and let the public methods specialize identity/topic handling.

6) **Python worker loop repeats method-call/parse boilerplate for each task**
   - **Where:** `src/python.rs:338-537`.
   - **Issue:** Each `PythonTask` branch does the same sequence: `Python::with_gil`, call method, extract `String`, `serde_json::from_str`, map to `InterpreterResult`.
   - **Why it matters:** It’s verbose and makes it harder to change the Python bridge contract (e.g., if you switch from JSON strings to bytes or `PyAny`).
   - **Suggestion:** Introduce a helper like `fn call_bridge_json<T>(py, instance, method, args) -> Result<T>` to consolidate the pattern; this also reduces copy/paste errors.

7) **Interpreter trait methods repeat channel allocation + send_or_error**
   - **Where:** `src/python.rs:575-737`.
   - **Issue:** Every `Interpreter` method creates a bounded channel, builds a `PythonTask`, and calls `send_or_error`.
   - **Why it matters:** Consistency bugs are easy to introduce (e.g., different channel sizes or missing fields) and the code is hard to scan.
   - **Suggestion:** Add a small helper like `fn request(&self, task_builder) -> Receiver<InterpreterResult>` to centralize channel creation and error handling.

8) **Python comm helpers in the bridge are copy/paste**
   - **Where:** `src/python_bridge.py:510-526`.
   - **Issue:** `comm_open`, `comm_msg`, `comm_close` each do `_maybe_json` on data/metadata and return the same `{"status":"ok"}`.
   - **Why it matters:** Minor, but repeated code increases maintenance cost and makes it easy to miss changes (e.g., if metadata handling changes).
   - **Suggestion:** Implement a shared `_comm_action` helper or move JSON normalization into `RustCommManager` itself.

9) **Python environment setup logic is duplicated across runtime/build/tests**
   - **Where:** `src/python.rs:181-219`, `build.rs:1-49`, `tests/test_ipyrust_kernel.py:12-44`, `scripts/cargo-test.sh`.
   - **Issue:** Multiple places independently derive Python paths and library locations.
   - **Why it matters:** Any changes to Python discovery need to be replicated in four places; this increases risk of mismatched behavior between runtime and tests.
   - **Suggestion:** Centralize environment detection in one place (e.g., a small Python helper script or Rust utility invoked by build/test), or have tests reuse `ensure_python_env()` via a shared entrypoint.

10) **Test channel-flushing helpers are duplicated**
   - **Where:** `tests/test_ipyrust_kernel.py:74-93` and `tests/test_ipyrust_kernel.py:212-228`.
   - **Issue:** `flush_channels` and `_flush_channels_raw` share most logic but differ in validation and filtering.
   - **Why it matters:** Not critical, but leads to drift if validation rules change.
   - **Suggestion:** Consolidate into one helper with parameters (`validate=True/False`, `ignore_types={...}`) to keep behavior consistent.

