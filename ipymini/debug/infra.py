import faulthandler, logging, signal, sys

from .flags import DebugFlags


def setup_debug(flags: DebugFlags):
    "Initialize debug infrastructure: logging, faulthandler, SIGUSR1 handler."
    if not flags.enabled: return
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.DEBUG, stream=sys.__stderr__, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    faulthandler.enable(file=sys.__stderr__)
    if hasattr(signal, "SIGUSR1"): faulthandler.register(signal.SIGUSR1, file=sys.__stderr__)


def trace_msg(logger, prefix: str, msg: dict, *, enabled: bool = True):
    "Log message flow at high level: msg_type, msg_id, subshell_id."
    if not enabled: return
    h = msg.get("header") or {}
    logger.warning("%s type=%s id=%s subshell=%r", prefix, h.get("msg_type"), h.get("msg_id"), h.get("subshell_id"))
