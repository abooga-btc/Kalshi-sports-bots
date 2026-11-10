"""Tiny .env loader (no third-party dependency). Never logs values."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | os.PathLike, override: bool = False) -> list[str]:
    """Load KEY=VALUE lines into os.environ. Returns the key NAMES loaded (never values)."""
    p = Path(path)
    loaded: list[str] = []
    if not p.exists():
        return loaded
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        val = val.replace("\\n", "\n")
        if key and (override or key not in os.environ):
            os.environ[key] = val
            loaded.append(key)
    return loaded
