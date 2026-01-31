# ipymini-zmqthread

`ipymini-zmqthread` provides small, testable building blocks for running ZMQ sockets in dedicated threads — the pattern required by Jupyter kernels for correctness and thread-safety.

It was extracted from the `ipymini` research kernel to keep the kernel implementation small and focused.

## Why?

Jupyter kernels typically need:

- background threads for shell/control ROUTER sockets
- a dedicated IOPub sender thread (PUB)
- a heartbeat REP thread
- a stdin ROUTER thread for input_request/input_reply

The key invariant is: each ZMQ socket is owned by exactly one thread; other threads communicate with it via queues.

## Install

```bash
pip install ipymini-zmqthread
```

## API

```python
from ipymini_zmqthread import ( ThreadBoundAsyncQueue, AsyncRouterThread,
  IOPubThread, StdinRouterThread, HeartbeatThread)
```

### AsyncRouterThread

Runs an asyncio loop in a dedicated thread and owns an async ROUTER socket (`zmq.asyncio`).

```python
import zmq
from jupyter_client.session import Session
from ipymini_zmqthread import AsyncRouterThread

ctx = zmq.Context.instance()
session = Session(key=b"")
router = None
def on_msg(msg, idents): router.enqueue(("kernel_info_reply", {"status": "ok"}, msg, idents))
router = AsyncRouterThread(
    context=ctx, session=session,
    bind_addr="tcp://127.0.0.1:5555",
    handler=on_msg, log_label="shell")
router.start()
router.ready.wait()
...
router.stop()
router.join(timeout=1)
```

### IOPubThread

Runs a PUB socket in a dedicated thread and sends messages via `Session.send` from inside that thread.

```python
from ipymini_zmqthread import IOPubThread

iopub = IOPubThread(ctx, "tcp://127.0.0.1:5556", session, qmax=10000, sndhwm=None)
iopub.start()
iopub.send("status", {"execution_state": "idle"}, parent=None)
...
iopub.stop()
iopub.join(timeout=1)
```

### HeartbeatThread

Echo thread: REP socket receives bytes and sends them back.

```python
from ipymini_zmqthread import HeartbeatThread

hb = HeartbeatThread(ctx, "tcp://127.0.0.1:5557")
hb.start()
...
hb.stop()
hb.join(timeout=1)
```

### StdinRouterThread

Routes `input_request`/`input_reply` and provides a blocking `request_input(...)` API.

```python
from ipymini_zmqthread import StdinRouterThread

stdin = StdinRouterThread(ctx, "tcp://127.0.0.1:5558", session)
stdin.start()

value = stdin.request_input("Name: ", password=False, parent=parent_msg, ident=client_idents, timeout=10.0)
...
stdin.interrupt_pending()  # cancels in-flight waits
stdin.stop()
stdin.join(timeout=1)
```

## License

Apache 2.

