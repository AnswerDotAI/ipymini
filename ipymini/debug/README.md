# ipymini.debug

The DAP debugger: `Debugger` processes Debug Adapter Protocol requests in front of `debugpy`, over a private ZMQ pair, with the reader as a supervised `ServiceThread`. Cell code is written to temp files named by a murmur2 hash of the source (the same scheme ipykernel uses) so debugger frontends can map breakpoints.

Generic debug infrastructure -- `DebugFlags` env parsing (`KERNMINI_DEBUG`, `KERNMINI_DEBUG_MSGS`), `setup_debug` logging/faulthandler wiring, `trace_msg`, and the cell-filename hashing itself -- lives in `kernmini.debug`.
