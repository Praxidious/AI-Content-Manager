from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def format_timestamp(seconds: float, milliseconds: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    if milliseconds:
        total_milliseconds = int(round(seconds * 1000))
        whole, remainder_milliseconds = divmod(total_milliseconds, 1000)
    else:
        whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    base = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    if milliseconds:
        return f"{base}.{remainder_milliseconds:03d}"
    return base


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
