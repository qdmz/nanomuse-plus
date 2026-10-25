"""stdout and stderr set to UTF-8, whatever the console's code page says.

A Chinese Windows console is code page 936, and the check mark a report is made of is not in
GBK: ``nanomuse doctor > report.txt`` died with ``UnicodeEncodeError: 'gbk' codec can't
encode character '\\u2713'`` before printing a line, and the desktop shell reads the same
output through a pipe, which takes the locale's encoding. The three bridge programs print
what a phone or a page answered, Chinese among it, into the shell tool, which reads it as
UTF-8: on that console the model was handed replacement characters. The frozen runtime the
desktop app carries does not read ``PYTHONUTF8`` or ``PYTHONUNBUFFERED`` either.

The standard library only: ``nanomuse-device`` and its two neighbours import this and must
stay cheap on a phone.
"""

from __future__ import annotations

import contextlib
import sys


def utf8_console() -> None:
    """UTF-8 on stdout and stderr, line-buffered; a no-op where they already are."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


__all__ = ["utf8_console"]
