"""Checks for a merged review proposal before anything is written or uploaded."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from cartas_raster.bsb import NotAChartZip, inspect_zip
from cartas_raster.dhn import RASTER_BASE
from cartas_raster.pipeline import ChartPackageError

_CHART_ID = re.compile(r"^(?:[0-9]{1,8}[A-Z]{0,8}|[0-9]{1,8}-[A-Z0-9]+(?:-[A-Z0-9]+)*)$")
_ZIP_NAME = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RASTER = urlsplit(RASTER_BASE)


def require_chart_id(chart_id: str) -> str:
    """Ids produced by ``chart_id_from_token``: ``52``, ``4023A``, ``52PL``, ``12-AB``."""
    if not _CHART_ID.fullmatch(chart_id):
        raise ChartPackageError(f"chart_id inválido: {chart_id!r}")
    return chart_id


def require_zip_name(zip_name: str) -> str:
    if not _ZIP_NAME.fullmatch(zip_name):
        raise ChartPackageError(f"zip_name inválido: {zip_name!r}")
    return zip_name


def require_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ChartPackageError(f"sha256 inválido: {value!r}")
    return value


def require_raster_url(url: str, zip_name: str) -> str:
    """HTTPS on assets.marinha.mil.br, path inside the raster directory, no query."""
    require_zip_name(zip_name)
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    expected_path = f"{_RASTER.path}{zip_name}"
    if (
        parts.scheme != "https"
        or host != _RASTER.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
        or parts.path != expected_path
    ):
        raise ChartPackageError(f"source_url fora do diretório raster da DHN: {url}")
    return url


def require_known_charts(chart_ids: list[str], manifest_charts: dict) -> list[str]:
    unknown = [chart_id for chart_id in chart_ids if chart_id not in manifest_charts]
    if unknown:
        raise ChartPackageError(
            "retire_chart_ids fora do manifesto: " + ", ".join(str(item) for item in unknown)
        )
    return list(chart_ids)


def confined_zip_path(directory: Path, zip_name: str) -> Path:
    """A file directly inside ``directory``. ``..`` and absolute names never escape it."""
    require_zip_name(zip_name)
    root = directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / zip_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ChartPackageError(f"caminho fora do diretório de download: {zip_name}") from exc
    if candidate.parent != root:
        raise ChartPackageError(f"caminho fora do diretório de download: {zip_name}")
    return candidate


def materialize_verified_zip(directory: Path, zip_name: str, body: bytes, expected_sha256: str) -> Path:
    """Write ``body`` only after the SHA-256 matches and the ZIP itself is acceptable."""
    expected_sha256 = require_sha256(expected_sha256)
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected_sha256:
        raise ChartPackageError(
            f"sha256 não confere (baixado {actual}, proposta {expected_sha256}). Nada foi gravado."
        )
    try:
        inspect_zip(body)
    except NotAChartZip as exc:
        raise ChartPackageError(str(exc)) from exc
    target = confined_zip_path(directory, zip_name)
    target.write_bytes(body)
    return target
