#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import supervisor


def main() -> int:
    root = Path.home() / "lagent-chats"
    supervisor.install_chat_viewer_assets(root)
    print(f"Installed transcript viewer assets at {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
