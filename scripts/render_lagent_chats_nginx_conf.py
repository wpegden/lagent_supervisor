#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def main() -> int:
    domain = "packer.math.cmu.edu"
    chat_root = (Path.home() / "lagent-chats").resolve()
    print(
        f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    location = /lagent-chats {{
        return 301 /lagent-chats/;
    }}

    location /lagent-chats/ {{
        alias {chat_root.as_posix()}/;
        index index.html;
        try_files $uri $uri/ /lagent-chats/index.html;
        add_header Cache-Control "no-cache";
    }}
}}"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
