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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from cartas_raster.store import CATALOGUE_PDF_CACHE

_ROW = re.compile(r"^\s*(?P<number>\d{1,6}[A-Z]{0,6})\s+(?P<rest>.+)$")
_SCALE = re.compile(r"1:\d")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PATH_DATE = re.compile(r"/(\d{4})-(\d{2})/")
_REVISION = re.compile(r"rev(\d+)", re.IGNORECASE)


SKIPPED_COMPARISON = "Comparação com o catálogo em papel não realizada (PDF indisponível)."


@dataclass(frozen=True)
class PaperHint:
    year: int
    title: str
    scale_label: str | None = None


@dataclass(frozen=True)
class PaperCatalogue:
    """Years stay the edition hint. Titles and scales only name charts the index missed."""

    hints: dict[str, PaperHint]
    warning: str | None = None

    @property
    def years(self) -> dict[str, int]:
        return {number: hint.year for number, hint in self.hints.items()}


def assets_pdf_url(url: str) -> str | None:
    """Point the catalogue PDF at assets.marinha.mil.br and encode the path once.

    GeoNetwork sometimes glues two schemes into one string
    (``https://www.mhttps://www.marinha.mil.br/...``). That host does not
    resolve. The real path always starts at the last ``http(s)://``.
    """
    cleaned = url.strip()
    for marker in ("https://", "http://"):
        last = cleaned.rfind(marker)
        if last > 0:
            cleaned = cleaned[last:]
            break
    parts = urlsplit(cleaned)
    if not parts.path.lower().endswith(".pdf"):
        return None
    quoted_path = "/".join(quote(unquote(segment), safe="") for segment in parts.path.split("/"))
    return urlunsplit(("https", "assets.marinha.mil.br", quoted_path, "", ""))


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
            if not absolute or absolute in seen:
                continue
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
    return {number: hint.year for number, hint in parse_catalogue_hints(text).items()}


def parse_catalogue_hints(text: str) -> dict[str, PaperHint]:
    """Same rows as :func:`parse_catalogue_text`, plus the first-line title and scale."""
    found: dict[str, PaperHint] = {}
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match or not _SCALE.search(match.group("rest")):
            continue
        rest = match.group("rest")
        years = [int(year) for year in _YEAR.findall(rest)]
        if not years:
            continue
        number = match.group("number")
        year = years[-1]
        previous = found.get(number)
        if previous is not None and year <= previous.year:
            continue
        scale_at = _SCALE.search(rest)
        title = re.sub(r"\s+", " ", rest[: scale_at.start()]).strip(" -")
        scale_label = _compact_scale(rest[scale_at.start() :], years)
        found[number] = PaperHint(year=year, title=title or f"Carta {number}", scale_label=scale_label)
    return found


def _compact_scale(text: str, years: list[int]) -> str | None:
    cutoff = text
    for year in years:
        index = text.find(str(year))
        if index != -1:
            cutoff = text[:index]
            break
    compact = re.sub(r"\s+", "", cutoff)
    if not compact.startswith("1:"):
        return None
    return compact


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


def refresh_paper_catalogue(transport, url: str | None, cache_path: Path) -> PaperCatalogue:
    """Return the paper hint. A missing PDF never raises and never removes charts.

    The previous cache is reused for the edition-year comparison when the
    download fails and a cache exists. Callers that add charts must ignore the
    hints when ``warning`` is set, so a failed read changes nothing.
    """
    cached = load_cached_catalogue(cache_path)
    cached_hints = _hints_from_cache(cached)
    if not url:
        warning = "Catálogo em PDF não encontrado nos registros do GeoNetwork."
        if cached_hints:
            warning += " Usando a cópia em cache."
        return PaperCatalogue(cached_hints, warning)
    print(f"Consultando catálogo em papel: {url}", flush=True)
    try:
        head = transport.head(url)
        if getattr(head, "status", 0) != 200:
            raise RuntimeError(f"HTTP {getattr(head, 'status', '?')} ao consultar o PDF")
        size = _length(getattr(head, "headers", {}) or {})
        if (
            cached.get("url") == url
            and size is not None
            and cached.get("size") == size
            and cached_hints
        ):
            return PaperCatalogue(cached_hints, None)
        downloaded = transport.get(url)
        if getattr(downloaded, "status", 0) != 200:
            raise RuntimeError(f"HTTP {getattr(downloaded, 'status', '?')} ao baixar o PDF")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as temporary:
            temporary.write(downloaded.body)
            temporary.flush()
            text = pdf_to_text(Path(temporary.name))
        hints = parse_catalogue_hints(text)
        if not hints:
            raise RuntimeError("o PDF não produziu nenhuma carta")
        payload = {
            "url": url,
            "size": size if size is not None else len(downloaded.body),
            "parsed_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "charts": {key: hints[key].year for key in sorted(hints)},
            "titles": {key: hints[key].title for key in sorted(hints)},
            "scales": {key: hints[key].scale_label for key in sorted(hints) if hints[key].scale_label},
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return PaperCatalogue(hints, None)
    except Exception as exc:
        warning = f"Catálogo em PDF indisponível em {url} ({exc})."
        if cached_hints:
            warning += " Usando a cópia em cache."
            return PaperCatalogue(cached_hints, warning)
        return PaperCatalogue({}, warning)


def _hints_from_cache(cached: dict) -> dict[str, PaperHint]:
    years = cached.get("charts") if isinstance(cached.get("charts"), dict) else {}
    titles = cached.get("titles") if isinstance(cached.get("titles"), dict) else {}
    scales = cached.get("scales") if isinstance(cached.get("scales"), dict) else {}
    hints: dict[str, PaperHint] = {}
    for key, value in years.items():
        try:
            year = int(value)
        except (TypeError, ValueError):
            continue
        hints[str(key)] = PaperHint(
            year=year,
            title=str(titles.get(key) or f"Carta {key}"),
            scale_label=str(scales[key]) if scales.get(key) else None,
        )
    return hints


PDFTOTEXT_TIMEOUT = 120


def pdf_to_text(pdf_path: Path, timeout: float = PDFTOTEXT_TIMEOUT) -> str:
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pdftotext excedeu {int(timeout)}s") from exc
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
