import json
from pathlib import Path
from typing import Any

BUILD_INFO_PATH = Path(__file__).resolve().parents[2] / "build-info.json"


def release_notes_payload(build_info_path: Path | None = None) -> dict[str, str]:
    try:
        data: Any = json.loads((build_info_path or BUILD_INFO_PATH).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": "", "revision": "", "notes": ""}
        return {
            "version": data.get("version") if isinstance(data.get("version"), str) else "",
            "revision": data.get("revision") if isinstance(data.get("revision"), str) else "",
            "notes": data.get("notes") if isinstance(data.get("notes"), str) else "",
        }
    except (OSError, json.JSONDecodeError):
        return {"version": "", "revision": "", "notes": ""}
