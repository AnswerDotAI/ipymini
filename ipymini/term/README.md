# ipymini.term

IO capture and routing for interactive Python execution: thread-local redirection of `sys.stdout`/`sys.stderr`, `input()`/`getpass()`, and `get_ipython()` (`io.py`), display/displayhook capture for IPython (`display.py`, `ipython_capture.py`). The reach of "thread-local" is contextvars: `threading.Thread.start` and `ThreadPoolExecutor.submit` are patched to copy the context, so output from user-spawned threads routes to the cell that spawned them.

Stream buffering/coalescing (`MiniStream`, `coalesce_streams`) lives in `kernmini.streams`; this package is the IPython-specific layer above it.
