"""The DHN paper-chart catalogue is a hint, not a gate.

``Catálogo de Cartas e Publicações`` lists the latest *paper* edition. A newer
paper edition does not always have a public raster ZIP. The pipeline still
publishes the best ZIP it can download and only records the difference.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cartas_raster.store import CATALOGUE_PDF_CACHE

_ROW = re.compile(r"^\s*(?P<number>\d{1,6}[A-Z]{0,6})\s+(?P<rest>.+)$")
_SCALE = re.compile(r"1:\d")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PATH_DATE = re.compile(r"/(\d{4})-(\d{2})/")
_REVISION = re.compile(r"rev(\d+)", re.IGNORECASE)


def assets_pdf_url(url: str) -> str:
    """The CHM file tree is also served by assets.marinha.mil.br, without Cloudflare."""
    return re.sub(r"^https://(?:www\.)?marinha\.mil\.br/", "https://assets.marinha.mil.br/", url, count=1)


def catalogue_links(items: list[dict]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for item in items:
        links = item.get("link") or []
        if isinstance(links, str):
            links = [links]
        for raw in links:
            parts = str(raw).split("|")
            if len(parts) < 3:
                continue
            name = parts[0].strip().lower()
            url = parts[2].strip()
            if "catálogo de cartas" not in name and "catalogo de cartas" not in name:
                continue
            if not url.lower().split("?", 1)[0].endswith(".pdf"):
                continue
            absolute = assets_pdf_url(url)
            if absolute not in seen:
                seen.add(absolute)
                found.append(absolute)
    return found


def newest_catalogue_url(urls: list[str]) -> str | None:
    if not urls:
        return None
    return max(urls, key=_catalogue_rank)


def _catalogue_rank(url: str) -> tuple:
    dated = _PATH_DATE.search(url)
    revision = _REVISION.search(url)
    return (
        int(dated.group(1)) if dated else 0,
        int(dated.group(2)) if dated else 0,
        int(revision.group(1)) if revision else 0,
        url,
    )


def parse_catalogue_text(text: str) -> dict[str, int]:
    """Chart number to the year of the latest paper edition.

    A title that wraps onto the next line has no chart number there, so it is
    ignored. The years stay on the first line, after the scale. One year means
    that edition is also the latest. The same number can appear again as a plan;
    the greater year is kept.
    """
    found: dict[str, int] = {}
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match or not _SCALE.search(match.group("rest")):
            continue
        years = [int(year) for year in _YEAR.findall(match.group("rest"))]
        if not years:
            continue
        number = match.group("number")
        year = years[-1]
        if number not in found or year > found[number]:
            found[number] = year
    return found


def edition_year(source_date: str | None) -> int | None:
    if not source_date or not isinstance(source_date, str):
        return None
    parts = source_date.strip().split("/")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    year = int(parts[2])
    if year < 1900 or year > 2100:
        return None
    return year


def edition_gaps(charts: dict[str, dict], paper_years: dict[str, int]) -> list[dict]:
    """Charts whose best raster ZIP is older than the paper catalogue.

    The chart stays published. This list is only a report.
    """
    gaps = []
    for chart_id in sorted(charts):
        entry = charts[chart_id]
        paper = paper_years.get(chart_id)
        if paper is None:
            paper = paper_years.get(_paper_number(chart_id))
        zip_year = edition_year(entry.get("source_date"))
        if paper is None or zip_year is None or zip_year >= paper:
            continue
        gaps.append(
            {
                "chart_id": chart_id,
                "zip_name": entry.get("zip_name"),
                "zip_year": zip_year,
                "source_date": entry.get("source_date"),
                "source_edition": entry.get("source_edition"),
                "catalogue_year": paper,
            }
        )
    return gaps


def _paper_number(chart_id: str) -> str:
    """``1831PL`` is not a paper-catalogue number; ``4023A`` is."""
    if chart_id.endswith("PL") and len(chart_id) > 2:
        return ""
    return chart_id


def load_cached_catalogue(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def refresh_paper_catalogue(transport, url: str | None, cache_path: Path) -> tuple[dict[str, int], str | None]:
    """Return ``(chart_number -> year, warning)``.

    A missing URL, a failed download, or a parser error never raises. The
    previous cache is reused when the download fails and a cache exists.
    """
    cached = load_cached_catalogue(cache_path)
    cached_years = cached.get("charts") if isinstance(cached.get("charts"), dict) else {}
    if not url:
        warning = "Catálogo em PDF não encontrado nos registros do GeoNetwork."
        return _years(cached_years), warning if not cached_years else warning + " Usando a cópia em cache."
    try:
        head = transport.head(url)
        if getattr(head, "status", 0) != 200:
            raise RuntimeError(f"HTTP {getattr(head, 'status', '?')} ao consultar o PDF")
        size = _length(getattr(head, "headers", {}) or {})
        if (
            cached.get("url") == url
            and size is not None
            and cached.get("size") == size
            and cached_years
        ):
            return _years(cached_years), None
        downloaded = transport.get(url)
        if getattr(downloaded, "status", 0) != 200:
            raise RuntimeError(f"HTTP {getattr(downloaded, 'status', '?')} ao baixar o PDF")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as temporary:
            temporary.write(downloaded.body)
            temporary.flush()
            text = pdf_to_text(Path(temporary.name))
        years = parse_catalogue_text(text)
        if not years:
            raise RuntimeError("o PDF não produziu nenhuma carta")
        payload = {
            "url": url,
            "size": size if size is not None else len(downloaded.body),
            "parsed_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "charts": {key: years[key] for key in sorted(years)},
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return years, None
    except Exception as exc:
        warning = f"Catálogo em PDF indisponível ({exc})."
        if cached_years:
            warning += " Usando a cópia em cache."
            return _years(cached_years), warning
        return {}, warning


def pdf_to_text(pdf_path: Path) -> str:
    completed = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "pdftotext falhou")
    return completed.stdout.decode("utf-8", errors="replace")


def _years(raw: dict) -> dict[str, int]:
    years = {}
    for key, value in raw.items():
        try:
            years[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return years


def _length(headers: dict) -> int | None:
    value = headers.get("content-length") or headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cache_path(root: Path) -> Path:
    return root / CATALOGUE_PDF_CACHE
