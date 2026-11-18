"""JSON files committed in the repository. Chart binaries are never stored here."""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = Path("data/manifest.json")
REJECTED_PATH = Path("data/rejected.json")
UNRESOLVED_PATH = Path("data/unresolved.json")
OVERRIDES_PATH = Path("data/source_overrides.json")
CATALOG_PATH = Path("docs/BR_RNCProdCat.xml")
CATALOGUE_PDF_CACHE = Path("data/catalogue_pdf.json")
EDITION_GAPS_PATH = Path("data/edition_gaps.json")
PROPOSALS_DIR = Path("proposals")


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def empty_manifest() -> dict:
    return {"version": 1, "updated_at": None, "charts": {}}


def empty_rejected() -> dict:
    return {"version": 1, "charts": {}}


def load_manifest(root: Path) -> dict:
    payload = load_json(root / MANIFEST_PATH, empty_manifest())
    payload.setdefault("version", 1)
    payload.setdefault("charts", {})
    return payload


def load_rejected(root: Path) -> dict:
    payload = load_json(root / REJECTED_PATH, empty_rejected())
    payload.setdefault("charts", {})
    return payload


def load_overrides(root: Path) -> dict[str, str]:
    payload = load_json(root / OVERRIDES_PATH, {"version": 1, "zip_names": {}})
    names = payload.get("zip_names") or {}
    if not isinstance(names, dict):
        raise ValueError("data/source_overrides.json: zip_names deve ser um objeto")
    return {str(key): str(value) for key, value in names.items()}


def is_rejected(rejected: dict, chart_id: str, sha256: str) -> bool:
    for item in rejected.get("charts", {}).get(chart_id, []):
        if item.get("sha256") == sha256:
            return True
    return False


def add_rejection(rejected: dict, chart_id: str, entry: dict, rejected_at: str) -> None:
    charts = rejected.setdefault("charts", {})
    items = charts.setdefault(chart_id, [])
    sha = entry["sha256"]
    if any(item.get("sha256") == sha for item in items):
        return
    items.append(
        {
            "sha256": sha,
            "source_edition": entry.get("source_edition"),
            "raster_edition": entry.get("raster_edition"),
            "ntm_edition": entry.get("ntm_edition"),
            "content_length": entry.get("content_length"),
            "rejected_at": rejected_at,
        }
    )


def headers_match(previous: dict | None, content_length: int, last_modified: str | None, etag: str | None) -> bool:
    if not previous:
        return False
    return (
        previous.get("content_length") == content_length
        and (previous.get("last_modified") or None) == (last_modified or None)
        and (previous.get("etag") or None) == (etag or None)
        and bool(previous.get("sha256"))
    )
