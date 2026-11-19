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
import math
import re
import stat
import zipfile
from pathlib import PurePosixPath
from typing import Iterable

from cartas_raster.models import BsbInfo

_LINE = re.compile(r"^([A-Z0-9]{2,3})/(.*)$")
_CHUNK = 64 * 1024
# Real DHN chart ZIPs seen in the wild are a directory plus .BSB/.KAP,
# and the largest published file is about 2.5 MB compressed. These caps
# sit far above that and stop a zip bomb before it is unpacked fully.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
_CHART_SUFFIXES = {".bsb", ".kap"}
# Harmless companions that real DHN ZIPs already ship (thumbnail caches,
# a GeoTIFF beside the KAP, Windows/macOS junk). Anything else is rejected:
# a denylist would let a new unknown type through, including a script whose
# suffix we had not thought of. Path checks and the size/ratio caps still
# apply to these extras.
_BENIGN_SUFFIXES = {
    ".db",
    ".tif",
    ".tiff",
    ".tfw",
    ".txt",
    ".pdf",
    ".htm",
    ".html",
    ".jpg",
    ".png",
    ".xml",
    ".ini",
}
_BENIGN_BASENAMES = {".ds_store", "thumbs.db"}
_EXECUTABLE_SUFFIXES = {
    ".exe",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".js",
    ".vbs",
    ".ps1",
    ".sh",
    ".dll",
    ".lnk",
    ".jar",
    ".msi",
}


class NotAChartZip(Exception):
    """The download is not a ZIP that contains a BSB or KAP chart."""


def inspect_zip(
    data: bytes,
    *,
    max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
    max_ratio: float = MAX_COMPRESSION_RATIO,
) -> BsbInfo:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise NotAChartZip("o arquivo baixado não é um ZIP") from exc
    chart_payloads = _read_chart_members(
        archive, max_uncompressed=max_uncompressed, max_ratio=max_ratio
    )
    suffixes = {PurePosixPath(name).suffix.lower() for name, _payload in chart_payloads}
    if ".bsb" not in suffixes or ".kap" not in suffixes:
        raise NotAChartZip("o ZIP precisa de pelo menos um .BSB e um .KAP")
    # The .BSB index carries CHT/CED/NTM for the whole chart. Fall back to KAP.
    ordered = sorted(chart_payloads, key=lambda item: (not item[0].lower().endswith(".bsb"), item[0]))
    merged: dict[str, dict[str, str]] = {}
    for name, payload in ordered:
        text = _header_text(payload)
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


def _read_chart_members(
    archive: zipfile.ZipFile, *, max_uncompressed: int, max_ratio: float
) -> list[tuple[str, bytes]]:
    """Validate every member, then return the chart files read in bounded chunks.

    The archive bytes are left untouched. Callers that publish a chart copy
    the original download, not a repacked ZIP.
    """
    total = 0
    charts: list[tuple[str, bytes]] = []
    ignored: list[str] = []
    for info in archive.infolist():
        _reject_unsafe_member(info, max_ratio=max_ratio)
        if _is_directory(info):
            continue
        normalized = info.filename.replace("\\", "/")
        kind = _member_kind(normalized)
        if kind == "rejected":
            raise NotAChartZip(f"tipo não permitido no ZIP: {info.filename}")
        remaining = max_uncompressed - total
        if info.file_size > remaining:
            raise NotAChartZip(
                f"tamanho descomprimido passa de {max_uncompressed} bytes em {info.filename}"
            )
        payload = _read_member(archive, info, remaining)
        total += len(payload)
        if total > max_uncompressed:
            raise NotAChartZip(f"tamanho descomprimido passa de {max_uncompressed} bytes")
        if kind == "chart":
            charts.append((info.filename, payload))
        else:
            ignored.append(info.filename)
    if ignored:
        print(f"Extras ignorados no ZIP: {', '.join(ignored)}.", flush=True)
    return charts


def _reject_unsafe_member(info: zipfile.ZipInfo, *, max_ratio: float) -> None:
    name = info.filename
    if "\x00" in name:
        raise NotAChartZip(f"nome de membro com byte NUL: {name!r}")
    if _is_symlink(info):
        raise NotAChartZip(f"symlink no ZIP: {name}")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise NotAChartZip(f"caminho absoluto no ZIP: {name}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise NotAChartZip(f"caminho com .. no ZIP: {name}")
    if _is_directory(info):
        return
    ratio = _compression_ratio(info)
    if ratio > max_ratio:
        raise NotAChartZip(
            f"taxa de compressão {ratio:.0f} acima de {max_ratio:.0f} em {name}"
        )


def _member_kind(normalized: str) -> str:
    """``chart``, ``benign`` or ``rejected``. Path safety is checked earlier."""
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part.lower() == "__macosx" for part in parts):
        return "benign"
    base = parts[-1].lower() if parts else ""
    if base in _BENIGN_BASENAMES:
        return "benign"
    suffix = PurePosixPath(base).suffix.lower()
    if suffix in _CHART_SUFFIXES:
        return "chart"
    if suffix in _EXECUTABLE_SUFFIXES or suffix not in _BENIGN_SUFFIXES:
        return "rejected"
    return "benign"


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    with archive.open(info, "r") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            got += len(block)
            if got > limit or got > max(info.file_size, 0):
                raise NotAChartZip(f"leitura de {info.filename} passou do limite")
            chunks.append(block)
    return b"".join(chunks)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _is_directory(info: zipfile.ZipInfo) -> bool:
    if info.is_dir() or info.filename.endswith(("/", "\\")):
        return True
    if stat.S_ISDIR(info.external_attr >> 16):
        return True
    # DOS directory flag with no Unix mode. DHN zips also use a trailing slash.
    return bool(info.external_attr & 0x10) and (info.external_attr >> 16) == 0 and info.file_size == 0


def _compression_ratio(info: zipfile.ZipInfo) -> float:
    if info.file_size <= 0:
        return 1.0
    if info.compress_size <= 0:
        return math.inf
    return info.file_size / info.compress_size


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
