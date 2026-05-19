"""
launch_gui_latest.py — desktop-shortcut entry point.

Finds the highest-version bot_gui_v*.py in this directory and execs it.
This way the desktop shortcut never needs updating when a new GUI version
(V9 → V10 → V11 …) is added.
"""
from __future__ import annotations
import glob
import os
import re
import runpy
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))


def find_latest_gui() -> str:
    candidates = glob.glob(os.path.join(_DIR, "bot_gui_v*.py"))
    pattern = re.compile(r"bot_gui_v(\d+(?:\.\d+)?)\.py$", re.IGNORECASE)

    def version_key(path: str) -> tuple[int, float]:
        m = pattern.search(os.path.basename(path))
        if not m:
            return (0, 0.0)
        # Sort by integer-major then float for "v8.1"-style names.
        token = m.group(1)
        try:
            return (1, float(token))
        except ValueError:
            return (0, 0.0)

    if not candidates:
        raise FileNotFoundError(
            f"No bot_gui_v*.py found in {_DIR}. "
            "Have you cloned the repo and synced the Python script folder?"
        )
    return max(candidates, key=version_key)


def main() -> None:
    latest = find_latest_gui()
    print(f"[launcher] Latest GUI: {os.path.basename(latest)}", file=sys.stderr)
    # Make sure relative imports inside the GUI (e.g. `from brokers import …`)
    # resolve against the script directory, not this launcher's cwd.
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)
    # runpy executes the file as if it were invoked directly via `python file.py`.
    runpy.run_path(latest, run_name="__main__")


if __name__ == "__main__":
    main()
