"""Pick the newest DHN raster edition and refuse to go backwards.

DHN sometimes leaves the previous ZIP in place and uploads the new edition as
``<n>_0.zip``, ``<n>_1.zip``, and so on. The edition on the file (BSB ``CED`` /
``NTM``) decides which one is current. ``Last-Modified`` only breaks a tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

from cartas_raster.models import BsbInfo

_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class EditionView:
    source_edition: int | None
    source_date: str | None
    ntm_edition: str | None
    ntm_date: str | None
    last_modified: datetime | None


def view_from_bsb(info: BsbInfo, last_modified: datetime | None) -> EditionView:
    return EditionView(
        source_edition=info.source_edition,
        source_date=info.source_date,
        ntm_edition=info.ntm_edition,
        ntm_date=info.ntm_date,
        last_modified=last_modified,
    )


def view_from_manifest(entry: dict) -> EditionView:
    return EditionView(
        source_edition=entry.get("source_edition"),
        source_date=entry.get("source_date"),
        ntm_edition=entry.get("ntm_edition"),
        ntm_date=entry.get("ntm_date"),
        last_modified=parse_http_datetime(entry.get("last_modified")),
    )


def is_newer(candidate: EditionView, current: EditionView) -> bool:
    """True when ``candidate`` must replace ``current``. Equal is not newer."""
    return edition_key(candidate) > edition_key(current)


def edition_key(view: EditionView) -> tuple:
    """Order: source edition, edition date, NTM notice, NTM date, file time."""
    se = view.source_edition if isinstance(view.source_edition, int) else -1
    return (
        se,
        parse_mdy(view.source_date),
        ntm_numbers(view.ntm_edition),
        parse_mdy(view.ntm_date),
        _aware(view.last_modified),
    )


def choose_newest(options: list[tuple[object, EditionView]]) -> tuple[object, EditionView]:
    if not options:
        raise ValueError("nenhuma variante para comparar")
    return max(options, key=lambda item: edition_key(item[1]))


def manifest_has_same_bytes(entry: dict | None, zip_name: str, content_length: int, last_modified: str | None) -> bool:
    """The manifest already describes this exact file, so it does not need a download."""
    if not entry:
        return False
    return (
        entry.get("zip_name") == zip_name
        and entry.get("content_length") == content_length
        and (entry.get("last_modified") or None) == (last_modified or None)
        and bool(entry.get("sha256"))
    )


def orphan_asset_names(charts: dict, release_names: list[str] | set[str]) -> list[str]:
    """Release assets that the manifest no longer points at."""
    keep = set()
    for entry in charts.values():
        name = entry.get("zip_filename")
        if name:
            keep.add(name)
    return sorted(name for name in release_names if name not in keep)


def retire_absorbed(
    charts: dict,
    chosen_urls: dict[str, str],
    considered: dict[str, str] | None = None,
) -> list[str]:
    """Drop catalogue entries that publish a file another chart already judged.

    ``chosen_urls`` maps a source URL to the chart that keeps it.
    ``considered`` maps a source URL to the chart that probed it as its own
    variant. A separate entry (usually a ``PL`` id) that still points at a
    losing variant is removed, so the old ZIP does not stay in the catalogue.
    """
    considered = considered or {}
    retired = []
    for chart_id, entry in charts.items():
        url = entry.get("source_url") or ""
        keeper = chosen_urls.get(url)
        if keeper and keeper != chart_id:
            retired.append(chart_id)
            continue
        probed_by = considered.get(url)
        if probed_by and probed_by != chart_id and keeper != chart_id:
            retired.append(chart_id)
    return sorted(retired)


def parse_mdy(value: str | None) -> date:
    if not value or not isinstance(value, str):
        return date.min
    parts = value.strip().split("/")
    if len(parts) != 3:
        return date.min
    try:
        month, day, year = (int(part) for part in parts)
        return date(year, month, day)
    except ValueError:
        return date.min


def ntm_numbers(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    numbers = []
    current = ""
    for char in str(value):
        if char.isdigit():
            current += char
        elif current:
            numbers.append(int(current))
            current = ""
    if current:
        numbers.append(int(current))
    return tuple(numbers)


def parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return _MIN_DT
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
