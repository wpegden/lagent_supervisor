from __future__ import annotations

from typing import Any, Dict

from lagent_supervisor.shared import chat_manifest_path
from lagent_supervisor.storage import JsonFile

PUBLIC_JSON_MODE = 0o644


def update_chat_manifest(config: Any, meta: Dict[str, Any]) -> None:
    path = chat_manifest_path(config)

    def mutator(payload: Dict[str, Any]) -> Dict[str, Any]:
        repos = payload.get("repos") if isinstance(payload, dict) else []
        if not isinstance(repos, list):
            repos = []
        filtered = [entry for entry in repos if isinstance(entry, dict) and entry.get("repo_name") != config.chat.repo_name]
        filtered.append(dict(meta))
        filtered.sort(key=lambda entry: (entry.get("updated_at") or "", entry.get("repo_name") or ""), reverse=True)
        return {"repos": filtered}

    JsonFile.update(path, {"repos": []}, mutator, mode=PUBLIC_JSON_MODE)
