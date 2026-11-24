"""Verify FantasyPros API access without printing the API key or response data."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

if __package__:
    from .fantasypros_http import fantasypros_request
else:
    from fantasypros_http import fantasypros_request


ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV = ROOT / ".local" / ".env"
ENDPOINTS = {
    "ranked_players": (
        "https://api.fantasypros.com/public/v2/json/nfl/players"
        "?ecr=included&external_ids=espn"
    ),
    "draft_ecr_ppr": (
        "https://api.fantasypros.com/public/v2/json/nfl/2026/consensus-rankings"
        "?position=ALL&scoring=PPR&type=DRAFT&week=0"
    ),
    "preseason_rb_projections": (
        "https://api.fantasypros.com/public/v2/json/nfl/2026/projections"
        "?position=RB&week=0"
    ),
}


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def count_records(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("players", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
    return None


def main() -> int:
    load_local_env(LOCAL_ENV)
    api_key = os.environ.get("FANTASYPROS_API_KEY")
    if not api_key:
        print(json.dumps({"ok": False, "error": "FANTASYPROS_API_KEY is not configured"}))
        return 2

    results: list[dict[str, Any]] = []
    for index, (dataset, endpoint) in enumerate(ENDPOINTS.items()):
        if index:
            time.sleep(1.1)
        request = fantasypros_request(endpoint, api_key, user_agent="fantasy-gm-personal/0.1")
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
                results.append(
                    {
                        "dataset": dataset,
                        "ok": 200 <= response.status < 300,
                        "status": response.status,
                        "top_level_type": type(payload).__name__,
                        "declared_count": payload.get("count") if isinstance(payload, dict) else None,
                        "record_count": count_records(payload),
                    }
                )
        except HTTPError as exc:
            results.append({"dataset": dataset, "ok": False, "status": exc.code})
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            results.append({"dataset": dataset, "ok": False, "error": type(exc).__name__})

    ok = all(result["ok"] for result in results)
    print(json.dumps({"ok": ok, "checks": results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
