# ipymini.debug

The DAP debugger: `Debugger` processes Debug Adapter Protocol requests in front of `debugpy`, over a private ZMQ pair, with the reader as a supervised `ServiceThread`. Cell code is written to temp files named by a murmur2 hash of the source (the same scheme ipykernel uses) so debugger frontends can map breakpoints.

`cells.py` implements ipykernel-compatible Murmur2 cell filenames so debugger frontends can map notebook code to temporary Python files.
