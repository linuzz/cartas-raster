"""Pick the newest DHN raster edition and refuse to go backwards.

DHN sometimes leaves the previous ZIP in place and uploads the new edition as
``<n>_0.zip``, ``<n>_1.zip``, and so on. The edition on the file (BSB ``CED`` /
``NTM``) decides which one is current. ``Last-Modified`` only breaks a tie.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

from cartas_raster.models import BsbInfo

# A normal week deletes well under this. The historical plan cleanup removed 19
# assets from a release of about 400, which stays inside both limits.
DEFAULT_MAX_ORPHAN_DELETIONS = 25
DEFAULT_MAX_ORPHAN_DELETION_RATIO = 0.10
DEFAULT_MIN_MANIFEST_RETAIN_RATIO = 0.90

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
    """Order: source edition, edition date, NTM year then number, NTM date, file time."""
    se = view.source_edition if isinstance(view.source_edition, int) else -1
    return (
        se,
        parse_mdy(view.source_date),
        ntm_key(view.ntm_edition),
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


class OrphanCleanupRefused(RuntimeError):
    """Deleting these release assets would be too large a change to do automatically."""


def orphan_asset_names(charts: dict, release_names: list[str] | set[str]) -> list[str]:
    """Release assets that the manifest no longer points at.

    Every manifest entry must name its ``zip_filename``. A missing name is not
    treated as "keep nothing": the caller refuses the cleanup instead.
    """
    keep = set()
    for entry in charts.values():
        name = entry.get("zip_filename")
        if name:
            keep.add(name)
    return sorted(name for name in release_names if name not in keep)


def orphan_deletion_cap(asset_count: int, absolute: int, ratio: float) -> int:
    """The smaller of the absolute cap and ``ratio`` of the assets on the release."""
    if asset_count <= 0:
        return 0
    return min(absolute, math.ceil(asset_count * ratio))


def assert_orphan_cleanup_allowed(
    charts: dict,
    release_names: list[str] | set[str],
    *,
    previous_chart_count: int | None = None,
    max_deletions: int | None = None,
    max_deletion_ratio: float | None = None,
    min_retain_ratio: float | None = None,
) -> list[str]:
    """Return the orphan names, or raise ``OrphanCleanupRefused`` before any delete.

    ``max_deletions`` defaults to 25 (``MAX_ORPHAN_DELETIONS``).
    ``max_deletion_ratio`` defaults to 0.10 (``MAX_ORPHAN_DELETION_RATIO``).
    The effective cap is the smaller of the two. ``min_retain_ratio`` defaults
    to 0.90 (``MIN_MANIFEST_RETAIN_RATIO``): the manifest after this run must
    keep at least that share of ``previous_chart_count``.
    """
    absolute = _env_int(max_deletions, "MAX_ORPHAN_DELETIONS", DEFAULT_MAX_ORPHAN_DELETIONS)
    ratio = _env_float(max_deletion_ratio, "MAX_ORPHAN_DELETION_RATIO", DEFAULT_MAX_ORPHAN_DELETION_RATIO)
    retain = _env_float(min_retain_ratio, "MIN_MANIFEST_RETAIN_RATIO", DEFAULT_MIN_MANIFEST_RETAIN_RATIO)
    missing = sorted(
        str(chart_id)
        for chart_id, entry in charts.items()
        if not str((entry or {}).get("zip_filename") or "").strip()
    )
    if missing:
        raise OrphanCleanupRefused(
            "manifesto sem zip_filename em: " + ", ".join(missing) + ". Nada foi apagado."
        )
    names = orphan_asset_names(charts, release_names)
    listed = ", ".join(names) if names else "(nenhum)"
    if previous_chart_count:
        floor = previous_chart_count * retain
        if len(charts) < floor:
            raise OrphanCleanupRefused(
                f"manifesto ficou com {len(charts)} cartas, abaixo de {retain:.0%} "
                f"de {previous_chart_count} ({floor:.1f}). "
                f"Seriam removidos: {listed}. Nada foi apagado."
            )
    asset_count = len(list(release_names))
    cap = orphan_deletion_cap(asset_count, absolute, ratio)
    if len(names) > cap:
        raise OrphanCleanupRefused(
            f"{len(names)} exclusões passam do teto {cap} "
            f"(mínimo entre {absolute} e {ratio:.0%} de {asset_count} assets). "
            f"Seriam removidos: {listed}. Nada foi apagado."
        )
    return names


def _env_int(value: int | None, name: str, default: int) -> int:
    if value is not None:
        return value
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise OrphanCleanupRefused(f"{name} inválido: {raw}") from exc


def _env_float(value: float | None, name: str, default: float) -> float:
    if value is not None:
        return value
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise OrphanCleanupRefused(f"{name} inválido: {raw}") from exc


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


def ntm_key(value: str | None) -> tuple[int, int]:
    """``(year, number)`` for both ``AAAA-NN`` and ``NN.AAAA``.

    ``12.2019`` must not sort ahead of ``3.2020``. A four-digit year in
    1900–2100 is the year; the other component is the notice number.
    """
    numbers = ntm_numbers(value)
    if not numbers:
        return (0, 0)
    if len(numbers) == 1:
        number = numbers[0]
        if _is_year(number):
            return (number, 0)
        return (0, number)
    first, second = numbers[0], numbers[1]
    if _is_year(first) and not _is_year(second):
        return (first, second)
    if _is_year(second) and not _is_year(first):
        return (second, first)
    return (first, second)


def _is_year(number: int) -> bool:
    return 1900 <= number <= 2100


def bsb_number_matches(chart_id: str, bsb_number: str | None) -> bool:
    """True when the header has no number or it is the same id. A mismatch only warns."""
    if bsb_number is None:
        return True
    left = re.sub(r"[^A-Za-z0-9]", "", str(chart_id)).upper()
    right = re.sub(r"[^A-Za-z0-9]", "", str(bsb_number)).upper()
    if not right:
        return True
    return left == right


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
