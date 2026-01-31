from dataclasses import dataclass
import os


def envbool(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v not in ("", "0", "false", "no")


@dataclass(frozen=True)
class DebugFlags:
    enabled: bool = False
    trace_msgs: bool = False

    @classmethod
    def from_env(cls, prefix: str = "IPYMINI") -> "DebugFlags":
        return cls(enabled=envbool(f"{prefix}_DEBUG"), trace_msgs=envbool(f"{prefix}_DEBUG_MSGS"))
