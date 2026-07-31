"""Test-only app-server impostor that leaves inherited pipes open until killed."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


def ignore_termination() -> None:
    if os.name != "nt":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=("parent", "exiting-parent", "grandchild")
    )
    parser.add_argument("pid_file", nargs="?")
    args, _unknown = parser.parse_known_args()
    ignore_termination()

    if args.mode == "grandchild":
        threading.Event().wait(120)
        return 0

    if not args.pid_file:
        return 2
    grandchild = subprocess.Popen(
        [sys.executable, __file__, "grandchild"],
        stdin=subprocess.DEVNULL,
    )
    pid_path = Path(args.pid_file)
    temporary = pid_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"parent": os.getpid(), "grandchild": grandchild.pid}),
        encoding="utf-8",
    )
    os.replace(str(temporary), str(pid_path))
    if args.mode == "exiting-parent":
        return 0
    threading.Event().wait(120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
