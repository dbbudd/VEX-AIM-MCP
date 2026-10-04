"""Where the panel keeps its things between runs: its set-up (measurements, tags, field, team list,
remembered networks' names), the Wi-Fi scan helper app, and downloaded YOLO weights. Never inside the
installed package, which may be read-only or replaced on upgrade."""

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "VEX AIM Panel"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "VEX AIM Panel"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "vex-aim-panel"
    base.mkdir(parents=True, exist_ok=True)
    return base
