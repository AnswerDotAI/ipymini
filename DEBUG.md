# Debugger Test Freezes: Causes + Fixes

This guide explains why debugger breakpoint tests can freeze even when the debugger works in nbclassic, and how to fix it. It’s tailored to a Python kernel running in a separate process with tests driven by `jupyter_client`.

## What “freeze on breakpoint” usually means

When execution hits a breakpoint, the kernel should send a `debug_event` (`stopped`) on IOPub and continue processing DAP requests on the control channel. A freeze means one of these did not happen:

- The `stopped` event was never emitted.
- The test never read the `stopped` event.
- The kernel is stopped but the client never sent `continue`.
- The debug adapter is blocked and can’t answer requests while the main thread is paused.

So freezes are almost always coordination or threading issues, not “debugger is broken”.

## Common causes + fixes

### 1) Debug requests handled on the execution thread
**Symptom:** `stopped` event arrives, but `stackTrace` / `scopes` / `variables` never return.  
**Cause:** the thread that handles DAP requests is paused at the breakpoint.  
**Fix:** handle DAP requests on a separate thread/task so they can run while execution is stopped.

### 2) DAP handshake misordered or incomplete
**Symptom:** breakpoints never hit; test waits forever.  
**Cause:** missing or misordered `configurationDone`.  
**Fix:** follow strict DAP order:

1) `initialize`  
2) `attach` (or `launch`)  
3) wait for `initialized` event  
4) `setBreakpoints`  
5) `configurationDone`  
6) execute code

### 3) IOPub not drained / wrong channel reads
**Symptom:** `stopped` is emitted but never seen; test hangs.  
**Cause:** IOPub not continuously drained; events stuck behind other messages.  
**Fix:** always drain IOPub before waiting for debug events; consume IOPub alongside control replies.

### 4) Client never sends `continue`
**Symptom:** first breakpoint test passes, subsequent tests freeze.  
**Cause:** kernel is still paused at a breakpoint.  
**Fix:** always send `continue` after `stopped` (even if the test only asserts it stopped).

### 5) Breakpoints set against the wrong source path
**Symptom:** no stops; waiting forever for `stopped`.  
**Cause:** breakpoints must be set on the exact `sourcePath` used by the adapter.  
**Fix:** use `dumpCell` (or equivalent), take its `sourcePath`, and set breakpoints against it.  
Also account for leading blank lines in cells.

### 6) Debug adapter not ready
**Symptom:** `attach` succeeds but no events/replies after that.  
**Cause:** you raced adapter startup.  
**Fix:** wait for the `initialized` event before sending breakpoints/configurationDone.

### 7) Debugpy tracing deadlocks
**Symptom:** breakpoints hit but adapter stops responding.  
**Cause:** debugpy traces the reader thread that’s handling its own messages.  
**Fix:** mark adapter reader thread as non‑traceable (e.g., `debugpy.trace_this_thread(False)`).

### 8) sys.monitoring stalls (Python 3.12+)
**Symptom:** breakpoint tests hang even though manual debugging works.  
**Cause:** debugpy’s sys.monitoring backend can stall kernel threads in some configurations.  
**Fix:** force `PYDEVD_USE_SYS_MONITORING=0` before importing debugpy.

### 9) GIL starvation / blocking with GIL held
**Symptom:** everything halts once breakpoint hit.  
**Cause:** the main thread holds the GIL while waiting, so the debug handler can’t run.  
**Fix:** release GIL around blocking waits; never block while holding the GIL.

### 10) Re‑initializing a DAP session in the same kernel
**Symptom:** first debug test passes; later tests freeze on `initialize/attach`.  
**Cause:** many adapters expect `initialize` once per session.  
**Fix:** reuse a single initialized/attached session for the whole module, or restart the kernel between tests.

### 11) Wrong threadId in follow‑up DAP requests
**Symptom:** `stackTrace` works but `variables` fails with “Unable to find thread”.  
**Cause:** assuming `threadId=1` instead of using the threadId from the `stopped` event.  
**Fix:** always read `stopped.body.threadId` and pass it to `stackTrace`/`continue`.

## A stable test flow (recommended)

For each test module:

1) Start kernel process  
2) `wait_for_ready()`  
3) `initialize`  
4) `attach`  
5) wait for `initialized` event  
6) flush channels  
7) reuse this attached session across tests  
8) For each test that hits a breakpoint:  
   - `dumpCell`  
   - `setBreakpoints`  
   - `configurationDone`  
   - `execute`  
   - wait for `stopped`  
   - run DAP queries (`stackTrace`, `scopes`, `variables`)  
   - `continue`  

At teardown: `disconnect`, then shutdown kernel.

## Diagnosis checklist (in order)

1) Is the kernel process alive?  
2) Did you receive `initialized` after `attach`?  
3) Are you reading IOPub continuously?  
4) Are breakpoints set against the returned `sourcePath`?  
5) Did you send `configurationDone` after `setBreakpoints`?  
6) Did you send `continue` after `stopped`?  
7) Are DAP requests handled off the execution thread?

## Instrumentation tips

These make failures obvious and cheap to debug:

- Log every DAP request/response with seq IDs and timestamps.
- Log every debug_event (`initialized`, `stopped`, `continued`) with timestamps.
- Log the `sourcePath` used for breakpoints.

## Why nbclassic works but tests freeze

nbclassic continuously drains IOPub, follows the DAP sequence, and always sends `continue`.  
Tests often skip one of those steps, especially when multiple tests reuse a kernel.

## Minimal “no‑freeze” template (pseudocode)

```
# setup
km = KernelManager(...)
kc = km.client()
kc.start_channels()
kc.wait_for_ready()

send_debug_request("initialize", {...})
send_debug_request("attach")
wait_iopub_event("initialized")

# test
flush_iopub()
source = send_debug_request("dumpCell", {"code": code})["body"]["sourcePath"]
send_debug_request("setBreakpoints", {"source": {"path": source}, "breakpoints": [{"line": 2}]})
send_debug_request("configurationDone")

kc.execute(code)
stopped = wait_iopub_event("stopped")
send_debug_request("stackTrace", {"threadId": stopped.threadId})
send_debug_request("scopes", {"frameId": ...})
send_debug_request("variables", {"variablesReference": ...})
send_debug_request("continue", {"threadId": stopped.threadId})
flush_iopub()

# teardown
send_debug_request("disconnect")
km.shutdown_kernel(now=True)
```
