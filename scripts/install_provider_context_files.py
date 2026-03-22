#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import supervisor


def main() -> int:
    parser = argparse.ArgumentParser(description="Install provider context files for Claude, Codex, and Gemini")
    parser.add_argument("--home-dir", default=str(Path.home()), help="Target home directory for personal installs")
    parser.add_argument(
        "--provider",
        action="append",
        choices=["claude", "codex", "gemini"],
        help="Provider to install. Defaults to all supported providers.",
    )
    parser.add_argument(
        "--scope-dir",
        action="append",
        default=[],
        help="Optional scope/project directory to receive provider-scoped files.",
    )
    args = parser.parse_args()

    providers = args.provider or ["claude", "codex", "gemini"]
    home_dir = Path(args.home_dir).expanduser().resolve()
    installed = supervisor.install_personal_provider_context_files(home_dir, providers)

    for scope_text in args.scope_dir:
        scope_dir = Path(scope_text).expanduser().resolve()
        for provider in providers:
            installed.extend(supervisor.install_scope_provider_context_files(scope_dir, provider))

    for path in installed:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
