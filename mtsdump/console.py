import logging
import sys
import threading
from typing import TextIO


class Console:
    """Log lines plus one progress line: redrawn in place on a terminal, printed every 10% otherwise."""

    def __init__(self, stream: TextIO | None = None):
        self._stream = stream or sys.stderr
        self._interactive = self._stream.isatty()
        self._lock = threading.Lock()
        self._progress = ""
        self._printed_step: tuple[str, int] | None = None

    def line(self, text: str) -> None:
        with self._lock:
            self._erase_progress()
            self._stream.write(text + "\n")
            if self._interactive:
                self._stream.write(self._progress)
            self._stream.flush()

    def progress(self, stage: str, fraction: float, detail: str = "") -> None:
        text = f"{stage} {fraction:.0%} {detail}".rstrip()
        if not self._interactive:
            step = (stage, int(fraction * 10))
            if step != self._printed_step:
                self._printed_step = step
                self.line(text)
            return
        with self._lock:
            self._erase_progress()
            self._progress = text
            self._stream.write(text)
            self._stream.flush()

    def end_progress(self) -> None:
        with self._lock:
            self._erase_progress()
            self._stream.flush()
            self._progress = ""
            self._printed_step = None

    def _erase_progress(self) -> None:
        if self._interactive and self._progress:
            self._stream.write("\r\x1b[K")


class ConsoleHandler(logging.Handler):
    def __init__(self, console: Console):
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.levelno > logging.INFO:
            message = f"{record.levelname.lower()}: {message}"
        self._console.line(message)
