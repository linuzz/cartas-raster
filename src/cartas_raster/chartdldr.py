"""Contract check for the XML that OpenCPN's chartdldr_pi actually parses.

The plugin (OpenCPN ``plugins/chartdldr_pi/src/chartcatalog.cpp``) accepts a
document whose root name starts with ``RncProductCatalog``. The first child is
the NOAA header. Each sibling named ``chart`` becomes a ``RasterChart``.

Dates are not free-form:

- ``date_created`` / the date half of ``dt_valid`` go through
  ``wxDateTime::ParseDate``. In wxWidgets 3.2 a token greater than 31 is a
  year, a token from 1 to 12 is taken as the month, and the remaining token
  is the day. ``YYYY-MM-DD`` is therefore read as year, month, day.
- ``dt_valid`` and ``zipfile_datetime_iso8601`` are split with
  ``wxStringTokenizer`` on the characters ``T`` and ``Z`` (empty tokens
  dropped, strtok style). The two pieces are ``ParseDate`` and ``ParseTime``.
- ``zipfile_datetime`` must match ``ParseFormat`` ``%Y%m%d_%H%M%S``.
- ``zipfile_size``, ``source_edition``, ``raster_edition`` and ``ntm_edition``
  go through ``wxAtoi`` (leading integer).

This module reimplements those rules tightly enough that a catalogue the
plugin would misread fails the check. It is intentionally not a general XML
schema.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

ROOT_PREFIX = "RncProductCatalog"

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_HMS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")
_ZIP_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$")
_INTEGER = re.compile(r"^[0-9]+$")


@dataclass
class ParsedChart:
    number: str
    title: str
    zipfile_location: str
    zipfile_size: int
    zipfile_datetime: datetime
    zipfile_datetime_iso8601: datetime
    source_edition: int | None = None
    raster_edition: int | None = None
    ntm_edition: int | None = None
    source_date: str | None = None
    ntm_date: str | None = None
    ntm_edition_last_correction: str | None = None
    target_filename: str | None = None


@dataclass
class ParsedCatalog:
    root_name: str
    title: str
    dt_valid: datetime | None
    charts: list[ParsedChart] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def parse_catalog(xml_text: str) -> ParsedCatalog:
    """Parse ``xml_text`` the way chartdldr_pi would, recording incompatibilities."""
    problems: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return ParsedCatalog("", "", None, problems=[f"XML inválido: {exc}"])

    root_name = root.tag
    if not root_name.startswith(ROOT_PREFIX):
        problems.append(
            f"elemento raiz {root_name!r} não começa com {ROOT_PREFIX!r}; "
            "LoadFromXml devolve false e o catálogo é recusado"
        )
        return ParsedCatalog(root_name, "", None, problems=problems)

    children = [child for child in list(root) if _is_element(child)]
    if not children:
        problems.append("catálogo sem elementos filhos")
        return ParsedCatalog(root_name, "", None, problems=problems)

    header = children[0]
    title = _child_text(header, "title")
    if not title:
        problems.append("cabeçalho sem <title> (o plugin mostra 'Catalog is not valid.')")
    dt_valid = _parse_dt_valid(_child_text(header, "dt_valid"), problems, "header/dt_valid")
    _require_wx_date(_child_text(header, "date_created"), problems, "header/date_created")
    _require_hms(_child_text(header, "time_created"), problems, "header/time_created")

    charts: list[ParsedChart] = []
    for element in children:
        if element.tag != "chart":
            continue
        chart, chart_problems = _parse_chart(element)
        problems.extend(chart_problems)
        if chart is not None:
            charts.append(chart)
    if not any(element.tag == "chart" for element in children) and len(children) > 1:
        problems.append("nenhum elemento <chart>; o plugin só cria RasterChart a partir dessa tag")
    return ParsedCatalog(root_name, title, dt_valid, charts, problems)


def assert_plugin_compatible(xml_text: str) -> ParsedCatalog:
    parsed = parse_catalog(xml_text)
    if not parsed.ok:
        joined = "\n".join(f"- {item}" for item in parsed.problems)
        raise ValueError(f"catálogo incompatível com chartdldr_pi:\n{joined}")
    return parsed


def wx_parse_date(text: str) -> tuple[int, int, int] | None:
    """Return ``(year, month, day)`` for a ``YYYY-MM-DD`` value wx ParseDate accepts.

    The general wx scanner is ambiguous for other orders (``DD/MM/YYYY`` can
    swap the month and the day). Catalogues this project writes are restricted
    to the unambiguous year-first form that the scanner classifies correctly:
    a token above 31 is the year, the next token in 1..12 is the month, and
    the last token is the day.
    """
    match = _ISO_DATE.match(text.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    if year <= 31 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return year, month, day


def wx_tokenizer_tz(value: str) -> list[str]:
    """Split the way ``wxStringTokenizer(value, "TZ")`` does in strtok mode."""
    return [part for part in re.split(r"[TZ]", value.strip()) if part]


def wx_atoi(value: str) -> int:
    match = re.match(r"\s*([+-]?\d+)", value or "")
    if not match:
        return 0
    return int(match.group(1))


def _parse_chart(element: ET.Element) -> tuple[ParsedChart | None, list[str]]:
    problems: list[str] = []
    number = _child_text(element, "number")
    title = _child_text(element, "title")
    location = _child_text(element, "zipfile_location")
    where = f"chart {number or '?'}"
    if not number:
        problems.append(f"{where}: <number> vazio")
    if not title:
        problems.append(f"{where}: <title> vazio (RasterChart herda o título de Chart)")
    if not location:
        problems.append(f"{where}: <zipfile_location> vazio; GetDownloadLocation não tem URL")
    elif not location.startswith("https://"):
        problems.append(
            f"{where}: zipfile_location {location!r} não é uma URL https absoluta; "
            "o plugin baixa essa string como está"
        )

    size_text = _child_text(element, "zipfile_size")
    size = wx_atoi(size_text) if size_text else 0
    if not size_text or not _INTEGER.match(size_text) or size != int(size_text):
        problems.append(f"{where}: zipfile_size deve ser um inteiro decimal para wxAtoi")
        size = 0
    elif size <= 0 or size >= 2**31:
        problems.append(f"{where}: zipfile_size {size} fora do intervalo que wxAtoi e o GitHub aceitam")

    stamp = _parse_zip_datetime(_child_text(element, "zipfile_datetime"), problems, where)
    iso = _parse_iso_datetime(
        _child_text(element, "zipfile_datetime_iso8601"), problems, f"{where}/zipfile_datetime_iso8601"
    )
    if stamp and iso and stamp != iso:
        problems.append(
            f"{where}: zipfile_datetime e zipfile_datetime_iso8601 não são o mesmo instante; "
            "GetUpdateDatetime usa só o campo ISO"
        )

    source_edition = _optional_int(element, "source_edition", problems, where)
    raster_edition = _optional_int(element, "raster_edition", problems, where)
    ntm_edition = _optional_int(element, "ntm_edition", problems, where)

    if problems or stamp is None or iso is None or not number or not title or not location:
        return None, problems
    return (
        ParsedChart(
            number=number,
            title=title,
            zipfile_location=location,
            zipfile_size=size,
            zipfile_datetime=stamp,
            zipfile_datetime_iso8601=iso,
            source_edition=source_edition,
            raster_edition=raster_edition,
            ntm_edition=ntm_edition,
            source_date=_child_text(element, "source_date") or None,
            ntm_date=_child_text(element, "ntm_date") or None,
            ntm_edition_last_correction=_child_text(element, "ntm_edition_last_correction") or None,
            target_filename=_child_text(element, "target_filename") or None,
        ),
        [],
    )


def _optional_int(element: ET.Element, tag: str, problems: list[str], where: str) -> int | None:
    text = _child_text(element, tag)
    if not text:
        return None
    if not _INTEGER.match(text):
        problems.append(
            f"{where}: <{tag}> {text!r} não é um inteiro; o plugin lê esse campo com wxAtoi"
        )
        return None
    return int(text)


def _parse_dt_valid(value: str, problems: list[str], where: str) -> datetime | None:
    if not value:
        problems.append(f"{where} ausente")
        return None
    parts = wx_tokenizer_tz(value)
    if len(parts) < 2:
        problems.append(f"{where} {value!r} não separa data e hora em 'T'/'Z'")
        return None
    date = wx_parse_date(parts[0])
    clock = _hms(parts[1])
    if date is None or clock is None:
        problems.append(f"{where} {value!r} não passa em ParseDate/ParseTime")
        return None
    year, month, day = date
    hour, minute, second = clock
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _parse_iso_datetime(value: str, problems: list[str], where: str) -> datetime | None:
    return _parse_dt_valid(value, problems, where)


def _parse_zip_datetime(value: str, problems: list[str], where: str) -> datetime | None:
    if not value:
        problems.append(f"{where}: zipfile_datetime ausente")
        return None
    match = _ZIP_STAMP.match(value.strip())
    if not match:
        problems.append(
            f"{where}: zipfile_datetime {value!r} não casa com %Y%m%d_%H%M%S"
        )
        return None
    numbers = [int(part) for part in match.groups()]
    try:
        stamp = datetime(*numbers, tzinfo=timezone.utc)
    except ValueError:
        problems.append(f"{where}: zipfile_datetime {value!r} não é uma data")
        return None
    return stamp


def _require_wx_date(value: str, problems: list[str], where: str) -> None:
    if wx_parse_date(value or "") is None:
        problems.append(f"{where} {value!r} não é YYYY-MM-DD aceito por wxDateTime::ParseDate")


def _require_hms(value: str, problems: list[str], where: str) -> None:
    if _hms(value or "") is None:
        problems.append(f"{where} {value!r} não é HH:MM:SS")


def _hms(value: str) -> tuple[int, int, int] | None:
    match = _HMS.match(value.strip())
    if not match:
        return None
    hour, minute, second = (int(part) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour, minute, second


def _child_text(element: ET.Element, tag: str) -> str:
    """Text of the first direct child named ``tag``, like pugixml ``first_child().value()``."""
    for child in list(element):
        if child.tag == tag:
            return (child.text or "").strip()
    return ""


def _is_element(node: ET.Element) -> bool:
    return isinstance(node.tag, str)
