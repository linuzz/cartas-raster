"""Read edition fields from a BSB/KAP chart stored inside a ZIP.

BSB headers are ASCII (or Windows-1252) text lines ``KEY/k=v,k=v`` until the
binary raster starts. Continuation lines begin with whitespace. The fields
OpenCPN cares about are the same ones NOAA puts in an RNC catalogue:

- ``CED/SE`` source edition, ``RE`` raster edition, ``ED`` edition date
- ``NTM/NE`` notice to mariners edition, ``ND`` notice date
- ``CHT/NA`` chart name, ``NU`` chart number
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Iterable

from cartas_raster.models import BsbInfo

_LINE = re.compile(r"^([A-Z0-9]{2,3})/(.*)$")


class NotAChartZip(Exception):
    """The download is not a ZIP that contains a BSB or KAP chart."""


def inspect_zip(data: bytes) -> BsbInfo:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise NotAChartZip("o arquivo baixado não é um ZIP") from exc
    names = archive.namelist()
    chart_names = [
        name
        for name in names
        if name.lower().endswith(".bsb") or name.lower().endswith(".kap")
    ]
    if not chart_names:
        raise NotAChartZip("o ZIP não contém arquivo .BSB nem .KAP")
    # The .BSB index carries CHT/CED/NTM for the whole chart. Fall back to KAP.
    ordered = sorted(chart_names, key=lambda name: (not name.lower().endswith(".bsb"), name))
    merged: dict[str, dict[str, str]] = {}
    for name in ordered:
        text = _header_text(archive.read(name))
        parsed = _parse_header(text)
        for key, fields in parsed.items():
            merged.setdefault(key, {}).update(fields)
        if "CHT" in merged and "CED" in merged:
            break
    cht = merged.get("CHT", {})
    ced = merged.get("CED", {})
    ntm = merged.get("NTM", {})
    return BsbInfo(
        name=_clean(cht.get("NA")),
        number=_clean(cht.get("NU")),
        source_edition=_integer(ced.get("SE")),
        raster_edition=_integer(ced.get("RE")),
        source_date=_clean(ced.get("ED")),
        ntm_edition=_clean(ntm.get("NE")),
        ntm_date=_clean(ntm.get("ND")),
    )


def _header_text(payload: bytes) -> str:
    # The binary section begins at the first NUL or SUB (0x1A).
    cut = len(payload)
    for marker in (b"\x1a", b"\x00"):
        index = payload.find(marker)
        if index != -1:
            cut = min(cut, index)
    return payload[:cut].decode("cp1252", errors="replace")


def _parse_header(text: str) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    current: str | None = None
    buffer = ""
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and current:
            buffer += raw.strip()
            continue
        if current:
            _store(records, current, buffer)
        match = _LINE.match(raw.strip())
        if not match:
            current = None
            buffer = ""
            continue
        current, buffer = match.group(1), match.group(2).strip()
    if current:
        _store(records, current, buffer)
    return records


def _store(records: dict[str, dict[str, str]], key: str, body: str) -> None:
    fields = records.setdefault(key, {})
    for part in _split_fields(body):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip().upper()
        if name and name not in fields:
            fields[name] = value.strip()


def _split_fields(body: str) -> Iterable[str]:
    # Values do not contain commas in the CED/NTM/CHT records we read.
    return [part.strip() for part in body.split(",") if part.strip()]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.match(r"\d+", value.strip())
    if not match:
        return None
    return int(match.group(0))
