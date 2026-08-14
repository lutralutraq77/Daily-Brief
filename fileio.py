"""Write a text file so a killed process cannot leave a half-written one.

os.replace is atomic on the same volume: a reader sees the old file or the new
one, never a truncation. This matters most for calendars.txt, whose contents are
Google secret iCal URLs that cannot be re-copied from a phone.

No fsync: the process-kill case does not need it, and it costs a stall on
Android flash. This protects against a killed process, not a killed device.
"""
from __future__ import annotations

import os
from pathlib import Path


def write_text_atomic(path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding)
        os.replace(tmp, path)          # same directory => same volume
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
