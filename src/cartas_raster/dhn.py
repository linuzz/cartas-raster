"""Discover raster charts published by DHN (Marinha do Brasil).

Two official surfaces, verified against the live site:

- The catalogue index is the IDEM GeoNetwork search used by
  https://idem.marinha.mil.br/ (``srv/por/q``, JSON). Each raster chart is a
  dataset whose title looks like
  ``Carta Nautica Raster 1:25.000 – Nome – Carta 3332`` and whose ``link``
  list points at the CHM page and, sometimes, only at a regional listing.
- The file itself is a ZIP of BSB/KAP named
  ``https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/<arquivo>.zip``.
  Plain chart numbers use ``<número>.zip``. Plans (``PL``) often use
  ``<número>_0.zip``. Letter suffixes use ``<número><letras>.zip``.
  ``www.marinha.mil.br`` HTML pages are behind a Cloudflare challenge from
  this environment and from typical CI addresses, so the file name is
  derived from the chart token and checked with HEAD. ``data/source_overrides.json``
  can force a file name without a code change.

The HTML of an individual chart page is still parsed when it is reachable.
A page that contains more than one raster file is a listing and is ignored.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

from cartas_raster.errors import SourceLayoutError
from cartas_raster.models import IndexRecord, RemoteFile, Unresolved

SEARCH_URL = (
    "https://idem.marinha.mil.br/geonetwork/srv/por/q"
    "?fast=index&_content_type=json&any=Carta%20Nautica%20Raster&from={start}&to={end}"
)
RASTER_BASE = "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/"
MIN_INDEX_COUNT = 400
MAX_TITLE_FAILURE_RATIO = 0.15
MIN_RESOLVED_RATIO = 0.70
PAGE_SIZE = 100

# The DHN title is usually "Carta Nautica Raster 1:25.000 – Nome – Carta 3332",
# but a few records omit a word or the spaces around the dash.
_TITLE_PREFIX = re.compile(
    r"^(?:Carta\s+)?(?:N[aá]utica\s+)?Raster\s+(?P<scale>1\s*:\s*[\d.]+)\s*[–—-]\s*(?P<name>.+)$",
    re.IGNORECASE,
)
_SEPARATORS = (" – Carta ", " — Carta ", " - Carta ")


class Transport(Protocol):
    def head(self, url: str): ...
    def get(self, url: str): ...


@dataclass
class Discovery:
    files: list[RemoteFile]
    unresolved: list[Unresolved]
    index_count: int


def parse_title(title: str) -> tuple[str | None, str, str]:
    """Return ``(scale_label, name, token)`` from a GeoNetwork chart title."""
    token = None
    left = title.strip()
    for separator in _SEPARATORS:
        index = left.rfind(separator)
        if index != -1:
            token = left[index + len(separator) :].strip()
            left = left[:index].strip()
            break
    if not token:
        raise ValueError(title)
    match = _TITLE_PREFIX.match(left)
    if not match:
        raise ValueError(title)
    scale = re.sub(r"\s+", "", match.group("scale"))
    return scale, match.group("name").strip(), token


def chart_id_from_token(token: str) -> str:
    raw = token.strip()
    parenthetical = re.fullmatch(r"(\d+)\s*\(([^)]+)\)", raw)
    if parenthetical:
        extra = re.sub(r"[^A-Za-z0-9]+", "-", parenthetical.group(2)).strip("-").upper()
        if not extra:
            raise ValueError(token)
        return f"{parenthetical.group(1)}-{extra}"
    match = re.fullmatch(r"(\d+)([A-Za-z]+)?\s*(PL)?", raw, flags=re.I)
    if not match:
        raise ValueError(token)
    letters = (match.group(2) or "").upper()
    plan = "PL" if match.group(3) else ""
    return f"{match.group(1)}{letters}{plan}"


def zip_candidates(token: str) -> list[str]:
    """File names to try, most specific first. Never guesses another chart's file."""
    raw = token.strip()
    parenthetical = re.fullmatch(r"(\d+)\s*\(([^)]+)\)", raw, flags=re.I)
    if parenthetical:
        base = parenthetical.group(1)
        extra = re.sub(r"\s+", "", parenthetical.group(2))
        alnum = re.sub(r"[^A-Za-z0-9]", "", extra)
        return _unique(
            [
                f"{base}{extra}.zip",
                f"{base}_{extra}.zip",
                f"{base}-{extra}.zip",
                f"{base}{alnum}.zip",
                f"{base}_{alnum}.zip",
            ]
        )
    match = re.fullmatch(r"(\d+)([A-Za-z]+)?\s*(PL)?", raw, flags=re.I)
    if not match:
        compact = re.sub(r"[^A-Za-z0-9]+", "", raw)
        return [f"{compact}.zip"] if compact else []
    base = match.group(1)
    letters = (match.group(2) or "").upper()
    plan = bool(match.group(3))
    if letters and plan:
        names = [f"{base}{letters}PL.zip", f"{base}{letters}_0.zip", f"{base}_{letters}_0.zip"]
    elif plan:
        # Do not fall back to the parent chart's ZIP: the plan panel usually
        # lives inside that file, and claiming it would publish the same bytes twice.
        names = [f"{base}PL.zip", f"{base}_PL.zip", f"{base}_0.zip"]
    elif letters:
        names = [f"{base}{letters}.zip"]
    else:
        # Some sheets are published only as "<number>_0.zip".
        names = [f"{base}.zip", f"{base}_0.zip"]
    return _unique(names)


def scale_denominator(scale_label: str | None) -> int | None:
    if not scale_label:
        return None
    digits = scale_label.split(":", 1)[-1].replace(".", "")
    if not digits.isdigit():
        return None
    return int(digits)


def parse_search_payload(payload: dict) -> tuple[int, list[dict]]:
    summary = payload.get("summary")
    if not isinstance(summary, dict) or "@count" not in summary:
        raise SourceLayoutError(
            "A resposta do GeoNetwork da DHN não tem summary.@count. "
            "O índice mudou de formato; nada foi publicado."
        )
    try:
        count = int(summary["@count"])
    except (TypeError, ValueError) as exc:
        raise SourceLayoutError("summary.@count da DHN não é um inteiro.") from exc
    metadata = payload.get("metadata")
    if metadata is None:
        raise SourceLayoutError("A resposta do GeoNetwork da DHN não tem 'metadata'.")
    if isinstance(metadata, dict):
        records = [metadata]
    elif isinstance(metadata, list):
        records = metadata
    else:
        raise SourceLayoutError("O campo metadata da DHN não é uma lista de registros.")
    return count, records


def records_from_metadata(items: list[dict]) -> tuple[list[IndexRecord], list[str]]:
    """Parse index hits. The second list is titles that look like raster charts but did not parse."""
    records: list[IndexRecord] = []
    failures: list[str] = []
    for item in items:
        if not _looks_like_raster(item):
            continue
        title = str(item.get("title") or "").strip()
        try:
            scale_label, name, token = parse_title(title)
            chart_id = chart_id_from_token(token)
        except ValueError:
            failures.append(title or "<sem título>")
            continue
        info = item.get("geonet:info") or {}
        records.append(
            IndexRecord(
                chart_id=chart_id,
                token=token,
                name=name,
                scale=scale_denominator(scale_label),
                scale_label=scale_label,
                title=title,
                uuid=str(info.get("uuid") or item.get("source") or chart_id),
                status=(item.get("status") or None),
                revision_date=item.get("revisionDate") or None,
                change_date=(info.get("changeDate") or None),
                page_url=_download_page(item.get("link") or []),
            )
        )
    return records, failures


def assert_index_sane(count: int, records: list[IndexRecord], failures: list[str]) -> None:
    if count < MIN_INDEX_COUNT:
        raise SourceLayoutError(
            f"O índice da DHN devolveu {count} registros; o esperado é pelo menos "
            f"{MIN_INDEX_COUNT}. O site mudou ou a busca falhou. Nada foi publicado."
        )
    observed = len(records) + len(failures)
    if observed and len(failures) / observed > MAX_TITLE_FAILURE_RATIO:
        sample = "; ".join(failures[:5])
        raise SourceLayoutError(
            "Não foi possível ler o título da maior parte das cartas raster "
            f"({len(failures)} de {observed}). Exemplo: {sample}. "
            "O padrão dos títulos mudou. Nada foi publicado."
        )
    if len(records) < MIN_INDEX_COUNT:
        raise SourceLayoutError(
            f"Só {len(records)} cartas raster tiveram o título reconhecido "
            f"(índice anunciou {count}). Nada foi publicado."
        )


def min_resolved_ratio() -> float:
    raw = os.environ.get("MIN_RESOLVED_RATIO", "").strip()
    if not raw:
        return MIN_RESOLVED_RATIO
    try:
        value = float(raw)
    except ValueError as exc:
        raise SourceLayoutError("MIN_RESOLVED_RATIO não é um número entre 0 e 1.") from exc
    if not 0 < value <= 1:
        raise SourceLayoutError("MIN_RESOLVED_RATIO deve ser maior que 0 e no máximo 1.")
    return value


def assert_resolution_sane(resolved: int, attempted: int) -> None:
    if attempted == 0 or resolved / attempted < min_resolved_ratio():
        raise SourceLayoutError(
            f"Só {resolved} de {attempted} cartas tiveram um ZIP em "
            f"{RASTER_BASE}. O padrão de nomes dos arquivos mudou. Nada foi publicado."
        )


class DhnCatalog:
    def __init__(self, transport: Transport, overrides: dict[str, str] | None = None):
        self.transport = transport
        self.overrides = overrides or {}

    def discover(self) -> Discovery:
        raw_items, count = self._collect_index()
        records, failures = records_from_metadata(raw_items)
        assert_index_sane(count, records, failures)
        records, conflicts = _drop_duplicate_ids(records)
        files, unresolved = self._resolve(records)
        unresolved.extend(conflicts)
        for title in failures:
            unresolved.append(
                Unresolved(chart_id="?", title=title, reason="título fora do padrão conhecido")
            )
        files, shared = _drop_shared_urls(files)
        unresolved.extend(shared)
        assert_resolution_sane(len(files), len(records) + len(conflicts))
        return Discovery(files=files, unresolved=unresolved, index_count=count)

    def _collect_index(self) -> tuple[list[dict], int]:
        collected: list[dict] = []
        seen: set[str] = set()
        count = None
        start = 1
        while True:
            payload = self._get_json(SEARCH_URL.format(start=start, end=start + PAGE_SIZE - 1))
            page_count, items = parse_search_payload(payload)
            if count is None:
                count = page_count
            elif page_count != count:
                raise SourceLayoutError(
                    f"O total do índice da DHN mudou no meio da leitura ({count} → {page_count})."
                )
            if not items:
                break
            new_items = []
            for item in items:
                info = item.get("geonet:info") or {}
                key = str(info.get("uuid") or item.get("title"))
                if key in seen:
                    raise SourceLayoutError(
                        "A paginação do GeoNetwork repetiu um registro. "
                        "O parâmetro from/to deixou de funcionar. Nada foi publicado."
                    )
                seen.add(key)
                new_items.append(item)
            collected.extend(new_items)
            if len(collected) >= count or len(items) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            if start > count + PAGE_SIZE:
                raise SourceLayoutError("A paginação do GeoNetwork não terminou.")
        return collected, int(count or 0)

    def _resolve(self, records: list[IndexRecord]) -> tuple[list[RemoteFile], list[Unresolved]]:
        files: list[RemoteFile] = []
        unresolved: list[Unresolved] = []
        for index, record in enumerate(records, start=1):
            found = self._resolve_one(record)
            if index % 50 == 0 or index == len(records):
                print(
                    f"Conferidos {index} de {len(records)} números; {len(files)} ZIPs até aqui.",
                    flush=True,
                )
            if found is None:
                unresolved.append(
                    Unresolved(
                        chart_id=record.chart_id,
                        title=record.title,
                        reason="nenhum ZIP encontrado para o número da carta",
                        page_url=record.page_url,
                    )
                )
            else:
                files.append(found)
        return files, unresolved

    def _resolve_one(self, record: IndexRecord) -> RemoteFile | None:
        names: list[str] = []
        override = self.overrides.get(record.chart_id)
        if override:
            names.append(override if override.endswith(".zip") else f"{override}.zip")
        names.extend(zip_candidates(record.token))
        for name in _unique(names):
            remote = self._head_zip(record, name)
            if remote is not None:
                return remote
        # The CHM HTML page is behind Cloudflare from datacenter IPs. Try it
        # only after the predictable file names miss, and give up quickly.
        page_name = self._name_from_page(record.page_url)
        if page_name:
            return self._head_zip(record, page_name)
        return None

    def _name_from_page(self, page_url: str | None) -> str | None:
        if not page_url or not _is_single_chart_page(page_url):
            return None
        result = self.transport.get(page_url, timeout=8, retries=1)
        if result.status != 200:
            return None
        html = result.body.decode("utf-8", errors="replace")
        if _is_challenge(html):
            return None
        names = raster_filenames_from_html(html)
        if len(names) != 1:
            return None
        return names[0]

    def _head_zip(self, record: IndexRecord, zip_name: str) -> RemoteFile | None:
        url = RASTER_BASE + zip_name
        result = self.transport.head(url)
        if result.status != 200:
            return None
        content_type = result.headers.get("content-type", "")
        if "zip" not in content_type.lower() and "octet-stream" not in content_type.lower():
            return None
        length = _content_length(result.headers.get("content-length"))
        if length is None or length <= 0:
            return None
        if length >= 2 * 1024 * 1024 * 1024 or length >= 2**31:
            raise SourceLayoutError(
                f"{zip_name} tem {length} bytes, acima do limite de asset do GitHub (2 GB) "
                "e do inteiro que o Chart Downloader lê com wxAtoi."
            )
        return RemoteFile(
            chart_id=record.chart_id,
            record=record,
            source_url=url,
            zip_name=zip_name,
            content_length=length,
            last_modified=result.headers.get("last-modified"),
            etag=result.headers.get("etag"),
        )

    def _get_json(self, url: str) -> dict:
        result = self.transport.get(url)
        if result.status != 200:
            raise SourceLayoutError(
                f"O índice da DHN respondeu HTTP {result.status} em {url}. Nada foi publicado."
            )
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceLayoutError(
                "O índice da DHN não devolveu JSON. Nada foi publicado."
            ) from exc
        if not isinstance(payload, dict):
            raise SourceLayoutError("O índice da DHN não devolveu um objeto JSON.")
        return payload


def raster_filenames_from_html(html: str) -> list[str]:
    """ZIP file names linked from a CHM chart page. More than one means a listing."""
    names = re.findall(
        r"files/raster/([A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.zip)",
        html,
        flags=re.IGNORECASE,
    )
    return _unique(names)


def _is_single_chart_page(url: str) -> bool:
    path = url.rstrip("/").rsplit("/", 1)[-1]
    # Regional listings are ``cartas-raster`` or ``cartas-raster-59``.
    return not re.fullmatch(r"cartas-raster(?:-\d+)?", path)


def _is_challenge(html: str) -> bool:
    lowered = html.lower()
    return "just a moment" in lowered or "cf-browser-verification" in lowered


def _looks_like_raster(item: dict) -> bool:
    formats = item.get("format") or []
    if isinstance(formats, str):
        formats = [formats]
    blob = " ".join(str(fmt) for fmt in formats).upper()
    if "KAP" in blob or "BSB" in blob:
        return True
    title = str(item.get("title") or "").upper()
    return "CARTA NAUTICA RASTER" in title or "CARTA NÁUTICA RASTER" in title


def _download_page(links: list) -> str | None:
    if isinstance(links, str):
        links = [links]
    for raw in links:
        parts = str(raw).split("|")
        if len(parts) < 3:
            continue
        name = parts[0].lower()
        url = parts[2].strip()
        if "download" in name and "raster" in name and url.startswith("http"):
            return url
    return None


def _content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _drop_duplicate_ids(
    records: list[IndexRecord],
) -> tuple[list[IndexRecord], list[Unresolved]]:
    grouped: dict[str, list[IndexRecord]] = {}
    for record in records:
        grouped.setdefault(record.chart_id, []).append(record)
    kept: list[IndexRecord] = []
    conflicts: list[Unresolved] = []
    for chart_id in sorted(grouped):
        group = grouped[chart_id]
        if len(group) == 1:
            kept.append(group[0])
            continue
        titles = {record.title for record in group}
        if len(titles) == 1:
            kept.append(sorted(group, key=lambda record: record.change_date or "")[-1])
            continue
        conflicts.append(
            Unresolved(
                chart_id=chart_id,
                title=group[0].title,
                reason="mais de um registro do índice usa este número com títulos diferentes",
                page_url=group[0].page_url,
            )
        )
    return kept, conflicts


def _drop_shared_urls(files: list[RemoteFile]) -> tuple[list[RemoteFile], list[Unresolved]]:
    by_url: dict[str, list[RemoteFile]] = {}
    for remote in files:
        by_url.setdefault(remote.source_url, []).append(remote)
    kept: list[RemoteFile] = []
    unresolved: list[Unresolved] = []
    for url, group in by_url.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        chosen = _prefer_shared(group)
        if chosen is None:
            for remote in group:
                unresolved.append(
                    Unresolved(
                        chart_id=remote.chart_id,
                        title=remote.record.title,
                        reason=f"o ZIP {url} também foi atribuído a outra carta",
                        page_url=remote.record.page_url,
                    )
                )
            continue
        kept.append(chosen)
        for remote in group:
            if remote is chosen:
                continue
            unresolved.append(
                Unresolved(
                    chart_id=remote.chart_id,
                    title=remote.record.title,
                    reason=f"o painel usa o mesmo ZIP da carta {chosen.chart_id}",
                    page_url=remote.record.page_url,
                )
            )
    kept.sort(key=lambda remote: remote.chart_id)
    return kept, unresolved


def _prefer_shared(group: list[RemoteFile]) -> RemoteFile | None:
    """Keep the sheet and drop plan insets that point at the same ZIP."""
    sheets = [remote for remote in group if not remote.chart_id.endswith("PL")]
    if len(sheets) == 1:
        return sheets[0]
    if len(group) == 1:
        return group[0]
    return None


def _unique(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered
