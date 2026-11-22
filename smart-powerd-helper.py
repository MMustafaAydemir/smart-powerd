#!/usr/bin/env python3
"""
Privileged root control helper for smart-powerd.

Strictly validates a single action against an explicit allowlist and sends it
to /run/smart-powerd/control.sock over a Unix domain socket without invoking
any shell.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

_LIB_DIR = "/usr/local/lib/smart-power"
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
if _LIB_DIR not in sys.path:
    sys.path.append(_LIB_DIR)

CONTROL_SOCK = "/run/smart-powerd/control.sock"

# Explicit static allowlist — must match smart-powerd.sudoers and smart-powerd.py
VALID_COMMANDS: frozenset[str] = frozenset(
    {
        "on",
        "off",
        "toggle",
        "reload",
        "brightness-on",
        "brightness-off",
        "brightness-toggle",
        "webcam-als-on",
        "webcam-als-off",
        "webcam-als-toggle",
    }
)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in VALID_COMMANDS:
        opts = "|".join(sorted(VALID_COMMANDS))
        print(f"Usage: smart-powerd-helper {{{opts}}}", file=sys.stderr)
        return 2

    cmd = sys.argv[1]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3.0)
            sock.connect(CONTROL_SOCK)
            sock.sendall((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
            resp_raw = sock.recv(4096).decode("utf-8", errors="ignore").strip()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"daemon unreachable: {exc}"}))
        return 1

    if not resp_raw:
        print(json.dumps({"ok": False, "error": "empty response from smart-powerd"}))
        return 1

    print(resp_raw)
    try:
        resp = json.loads(resp_raw)
        return 0 if resp.get("ok") else 1
    except ValueError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
