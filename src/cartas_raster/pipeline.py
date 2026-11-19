"""Turn a discovery plus downloaded bytes into a manifest entry and a catalogue."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from cartas_raster.bsb import NotAChartZip, inspect_zip
from cartas_raster.catalog import asset_name, render_catalog
from cartas_raster.models import BsbInfo, PreparedChart, RemoteFile

MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024


class ChartPackageError(Exception):
    """This one chart cannot be published. Other charts may continue."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_chart(remote: RemoteFile, zip_path: Path) -> PreparedChart:
    size = zip_path.stat().st_size
    if size <= 0 or size >= MAX_ZIP_BYTES or size >= 2**31:
        raise ChartPackageError(f"{remote.chart_id}: ZIP com tamanho {size}, fora dos limites")
    if remote.content_length and size != remote.content_length:
        raise ChartPackageError(
            f"{remote.chart_id}: tamanho baixado {size} diferente do Content-Length {remote.content_length}"
        )
    try:
        info = inspect_zip(zip_path.read_bytes())
    except NotAChartZip as exc:
        raise ChartPackageError(f"{remote.chart_id}: {exc}") from exc
    return PreparedChart(
        remote=remote,
        sha256=sha256_file(zip_path),
        size=size,
        bsb=info,
        zip_datetime=_zip_datetime(remote.last_modified),
        zip_path=str(zip_path),
    )


def manifest_entry(chart: PreparedChart) -> dict:
    record = chart.remote.record
    info: BsbInfo = chart.bsb
    scale = f"1:{record.scale}" if record.scale else record.scale_label
    name = record.name
    if record.status == "paper" and info.name:
        name = info.name.strip()
    title = name
    if scale:
        title = f"{title} ({scale})"
    return {
        "chart_id": chart.remote.chart_id,
        "number": chart.remote.chart_id,
        "title": title,
        "official_title": record.title,
        "name": name,
        "scale": record.scale,
        "metadata_edition": None,
        "source_edition": info.source_edition,
        "raster_edition": info.raster_edition,
        "source_date": info.source_date,
        "ntm_edition": info.ntm_edition,
        "ntm_date": info.ntm_date,
        "bsb_number": info.number,
        "bsb_name": info.name,
        "source_url": chart.remote.source_url,
        "page_url": record.page_url,
        "uuid": record.uuid,
        "change_date": record.change_date,
        "zip_name": chart.remote.zip_name,
        "content_length": chart.remote.content_length,
        "last_modified": chart.remote.last_modified,
        "etag": chart.remote.etag,
        "sha256": chart.sha256,
        "zip_filename": asset_name(chart.remote.chart_id),
        "zip_size": chart.size,
        "zip_datetime": chart.zip_datetime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_filename": chart.remote.zip_name,
        "status": "published",
    }


def edition_label(entry: dict | None) -> str:
    if not entry:
        return "não publicada"
    parts = []
    if entry.get("source_edition") is not None:
        parts.append(f"fonte {entry['source_edition']}")
    if entry.get("raster_edition") is not None:
        parts.append(f"raster {entry['raster_edition']}")
    if entry.get("source_date"):
        parts.append(str(entry["source_date"]))
    if entry.get("ntm_edition"):
        notice = f"Aviso {entry['ntm_edition']}"
        if entry.get("ntm_date"):
            notice += f" ({entry['ntm_date']})"
        parts.append(notice)
    return ", ".join(parts) if parts else "edição não informada no BSB"


def write_catalog(path: Path, charts: dict[str, dict], repository: str, generated_at: datetime) -> None:
    document = render_catalog(list(charts.values()), generated_at=generated_at, repository=repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def _zip_datetime(last_modified: str | None) -> datetime:
    if last_modified:
        try:
            parsed = parsedate_to_datetime(last_modified)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0)
    return datetime.now(timezone.utc).replace(microsecond=0)
