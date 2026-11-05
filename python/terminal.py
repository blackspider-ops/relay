"""Restore terminal modes even when the native frontend cannot clean up."""

import contextlib
import os
import sys
import termios

RESET_MODES = b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?2004l\x1b[?25h\x1b[0m"


class TerminalState:
    def __init__(self):
        self.input_fd = None
        self.output_fd = None
        self.attributes = None
        with contextlib.suppress(OSError, ValueError, AttributeError, termios.error):
            if sys.stdin.isatty() and sys.stdout.isatty():
                self.input_fd = sys.stdin.fileno()
                self.output_fd = sys.stdout.fileno()
                self.attributes = termios.tcgetattr(self.input_fd)

    def restore(self):
        if self.attributes is None:
            return
        with contextlib.suppress(OSError, termios.error):
            os.write(self.output_fd, RESET_MODES)
            termios.tcsetattr(self.input_fd, termios.TCSANOW, self.attributes)
        self.attributes = None
