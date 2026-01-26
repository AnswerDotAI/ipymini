"Debug infrastructure for ipymini with tiered logging and faulthandler support."
import faulthandler, logging, os, signal, sys

def envbool(name: str)->bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v not in ("", "0", "false", "no")

enabled = envbool("IPYMINI_DEBUG")
trace_msgs = envbool("IPYMINI_DEBUG_MSGS")

def setup():
    "Initialize debug infrastructure: logging, faulthandler, SIGUSR1 handler."
    if not enabled: return
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.DEBUG, stream=sys.__stderr__,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    faulthandler.enable(file=sys.__stderr__)
    if hasattr(signal, "SIGUSR1"): faulthandler.register(signal.SIGUSR1, file=sys.__stderr__)

def tlog(log, prefix: str, msg: dict):
    "Log message flow at high level: msg_type, msg_id, subshell_id."
    if not trace_msgs: return
    h = msg.get("header") or {}
    log.warning("%s type=%s id=%s subshell=%r", prefix, h.get("msg_type"), h.get("msg_id"), h.get("subshell_id"))
