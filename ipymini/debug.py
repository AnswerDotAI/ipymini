"Debug infrastructure for ipymini with tiered logging and faulthandler support."

from ipymini_debug import DebugFlags, envbool, setup_debug, trace_msg

_flags = DebugFlags.from_env("IPYMINI")
enabled = _flags.enabled
trace_msgs = _flags.trace_msgs


def setup():
    "Initialize debug infrastructure: logging, faulthandler, SIGUSR1 handler."
    setup_debug(_flags)


def tlog(log, prefix: str, msg: dict):
    "Log message flow at high level: msg_type, msg_id, subshell_id."
    trace_msg(log, prefix, msg, enabled=trace_msgs)


__all__ = ["envbool", "enabled", "trace_msgs", "setup", "tlog"]
