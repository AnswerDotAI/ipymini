# Subshells in ipymini

This document explains kernel subshells, how they work in the Jupyter protocol,
and how to add them cleanly to ipymini. It also proposes a rigorous test plan.

Reference spec:
```
https://jupyter.org/enhancement-proposals/91-kernel-subshells/kernel-subshells.html
```

## What subshells are (protocol view)

The JEP defines subshells as a way to process multiple shell requests
concurrently within a single kernel process. It states:

> "This JEP introduces kernel subshells to allow for concurrent shell requests." citeturn0search0

Key points from the spec:

- The kernel starts with a single parent subshell. Child subshells are optional.
- Child subshells are created via a control-channel request:
  > "A new child subshell thread is started using a new `create_subshell_request` control message." citeturn0search0
- Each subshell has its own execution count and history:
  > "Each subshell will store its own execution count and history." citeturn0search0
- `subshell_id` identifies which subshell should process a request; if missing, the parent subshell is used.
- The kernel advertises support via `kernel_info_reply.supported_features` with `kernel subshells`.

## How subshells work (mental model)

Think of subshells as *multiple independent shell request queues* that share the
same process and Python runtime but run on separate threads. Each subshell:

- processes its own shell requests sequentially,
- can run concurrently with other subshells,
- shares the same `user_ns` and interpreter state by default (because it is the
  same process),
- maintains its own execution counter/history to preserve per-subshell ordering.

Control messages remain global (kernel-level). Stdin requests are multiplexed
via `subshell_id` so concurrent subshells can request input without conflict.

## How to add subshells to ipymini (clean + minimal)

The goal is to keep ipymini small while matching the JEP. A concise design:

### 1) Subshell manager

Add a `SubshellManager` that owns:
- a map `{subshell_id -> Subshell}` for child subshells,
- a fixed parent subshell (`subshell_id = None`),
- helpers to create, delete, and list subshells.

Each `Subshell` holds:
- its own shell request queue,
- its own thread,
- its own `KernelBridge` instance **or** a shared bridge with per-subshell
  state (see below),
- execution count/history state.

### 2) Routing shell/stdin messages

On shell/stdin requests, extract `subshell_id` and route to the right queue.
If missing/None, route to parent subshell. If unknown, return an error.

This means:
- `MiniKernel._handle_shell()` becomes a router
- `Subshell` thread does the current synchronous `execute_request` handling

### 3) Per-subshell bridge state

The JEP says each subshell has its own execution count and history. That means:
- Either instantiate a separate `KernelBridge` per subshell, each with its own
  IPython `InteractiveShell` instance, or
- Keep one shared `KernelBridge` but store per-subshell counters and history
  in a subshell-local wrapper around IPython history.

The simplest/clearest route is **one `KernelBridge` per subshell**, created by
cloning the current initialization steps. This keeps per-subshell state honest
and avoids locking around shared IPython state. It does mean:
- shared `user_ns` must be explicit (see next point),
- any shared resources (comms, displayhook, etc.) need careful cross-thread use.

### 4) Shared user namespace

By default, users expect subshells to see the same variables. A clean approach:
- create a single `user_ns` dict,
- pass it into each `InteractiveShell` instance so they share the namespace.

This retains “global” state while still allowing each subshell to track its
own execution count and history.

### 5) IOPub, parent headers, and stream routing

IOPub messages already contain the parent header, which includes `subshell_id`.
No new field is required on IOPub messages (per JEP).

Do ensure:
- each subshell sets its own `_parent_header` when processing a request,
- stream output is emitted immediately with that parent header,
- concurrent subshells do not mix parent headers.

### 6) Control channel: create/list/delete subshell

Add handlers for:
- `create_subshell_request` → create thread, return new `subshell_id`
- `list_subshell_request` → return list of child subshell IDs
- `delete_subshell_request` → stop thread, cleanup, return ok

These are always global (kernel-level) and do not use `subshell_id`.

### 7) Optional features advertisement

In `kernel_info_reply.supported_features`, add:
- `"kernel subshells"`

This is how clients know they can create subshells.

## Concurrency and correctness concerns

Subshells are designed for concurrent shell activity. That introduces real
risks:
- **deadlocks** (shared locks or shared IO),
- **race conditions** in shared `user_ns`,
- **IOPub interleaving** with wrong parent headers,
- **stdin contention** (two subshells waiting for input),
- **history/execution count mixups**.

To avoid subtle issues:
- Keep subshell threads isolated.
- Ensure IOPub sending is thread-safe (already via the IOPub thread queue).
- Ensure each subshell always sets and clears its own parent header.
- Use timeouts around all inter-thread waits.

## Testing subshells rigorously

We need more than ipykernel’s coverage. A good test plan:

### 1) Basic API behavior
- `kernel_info_reply` contains `"kernel subshells"`.
- `create_subshell_request` returns unique IDs.
- `list_subshell_request` shows only child subshells.
- `delete_subshell_request` removes the subshell and returns ok.
- Unknown `subshell_id` produces an error response.

### 2) Basic concurrency
- Subshell A runs a long `sleep`, subshell B can still complete:
  - `execute_request` in B should return while A is busy.
- Subshell A blocks on stdin; subshell B can still run.

### 3) Shared namespace correctness
- A writes `x=1` and B reads `x` immediately.
- Concurrent writes from A/B: ensure last-write-wins with no corruption.
- A defines a function, B calls it.

### 4) Per-subshell execution counts/history
- Each subshell should have its own execution counter.
- History queries scoped per subshell return the right inputs/outputs.

### 5) IOPub parent header integrity
- Interleave executes across subshells; verify all outputs have the correct
  `parent_header.msg_id` and `parent_header.subshell_id`.
- Force heavy output in both shells and assert no cross-tagging.

### 6) Race condition stress tests
Create a test that randomly interleaves:
- `execute_request` (fast + slow),
- `inspect_request`,
- `complete_request`,
- `history_request`,
- `comm_*` messages,
- stdin requests.

Run with:
- multiple subshells (e.g. 3–5),
- randomized delays,
- bounded timeouts (e.g. 5–10s per test).

The goal is to find deadlocks and wrong routing.

### 7) Failure & timeout discipline
Tests must never freeze the suite. Guardrails:
- Use per-test timeouts (pytest `timeout` plugin or manual timeouts).
- Wrap thread joins with timeouts.
- Use a watchdog that aborts if a subshell doesn’t respond.

### 8) Deterministic deadlock probes
Introduce explicit “barrier” code to intentionally block one subshell,
then verify other subshells still make progress:
- `threading.Event` in user code that only A can set.
- B must continue to execute while A waits.

### 9) Stdin multiplexing correctness
- Two subshells request input concurrently; ensure each gets its own reply.
- Ensure input replies are not misrouted across subshells.

### 10) Shutdown behavior
- Deleting a subshell should stop its thread cleanly without affecting others.
- Kernel shutdown should stop all subshells.

## Implementation notes for ipymini

The current ipymini kernel loop is single-threaded per shell channel.
Implement subshells by:

1) introducing a `Subshell` worker thread + queue,
2) routing shell/stdin messages based on `subshell_id`,
3) keeping IOPub output on the existing IOPub thread.

This matches the JEP’s intent while keeping the design small and readable.
