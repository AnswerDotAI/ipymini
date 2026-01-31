from typing import Callable


class MiniStream:
    def __init__(self, name: str, events: list[dict], sink: Callable[[str, str], None] | None = None):
        "Buffer stream text and emit events to `events`/`sink`."
        self.name = name
        self.events = events
        self.sink = sink
        self.buffer = ""

    def write(self, value) -> int:
        "Write text to buffer or emit live output."
        if value is None: return 0
        if isinstance(value, bytes): text = value.decode(errors="replace")
        elif isinstance(value, str): text = value
        else: text = str(value)
        if not text: return 0
        if self.sink is not None: self._emit_live(text)
        if self.events is None: return len(text)
        if self.events and self.events[-1]["name"] == self.name: self.events[-1]["text"] += text
        else: self.events.append({"name": self.name, "text": text})
        return len(text)

    def writelines(self, lines) -> int:
        "Write multiple lines to the stream buffer."
        total = 0
        for line in lines: total += self.write(line) or 0
        return total

    def flush(self):
        "Flush buffered text to the sink."
        if self.sink is None: return None
        if self.buffer:
            self.sink(self.name, self.buffer)
            self.buffer = ""
        return None

    def isatty(self) -> bool: return False

    def _emit_live(self, text: str):
        self.buffer += text
        if "\n" not in self.buffer: return
        parts = self.buffer.split("\n")
        for line in parts[:-1]: self.sink(self.name, line + "\n")
        self.buffer = parts[-1]
