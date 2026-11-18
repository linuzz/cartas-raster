"""Write an NOAA-style ``RncProductCatalog`` document.

Field names and date formats are the ones ``chartdldr_pi`` reads. See
``chartdldr.py``. The document is parsed again before it is returned so a
regression that the plugin would reject cannot be written out.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from cartas_raster.chartdldr import assert_plugin_compatible

CATALOG_TITLE = "Cartas Náuticas Raster DHN — Marinha do Brasil"
# IHO S-62 producer code for Diretoria de Hidrografia e Navegação.
S62_AGENCY = "83"
RELEASE_TAG = "cartas"


def asset_name(chart_id: str) -> str:
    return f"BR_{chart_id}.zip"


def asset_url(repository: str, chart_id: str) -> str:
    return f"https://github.com/{repository}/releases/download/{RELEASE_TAG}/{asset_name(chart_id)}"


def render_catalog(entries: list[dict], *, generated_at: datetime, repository: str) -> str:
    moment = generated_at.astimezone(timezone.utc).replace(microsecond=0)
    root = ET.Element("RncProductCatalog")
    header = ET.SubElement(root, "header")
    _text(header, "title", CATALOG_TITLE)
    _text(header, "date_created", moment.strftime("%Y-%m-%d"))
    _text(header, "time_created", moment.strftime("%H:%M:%S"))
    _text(header, "date_valid", moment.strftime("%Y-%m-%d"))
    _text(header, "time_valid", moment.strftime("%H:%M:%S"))
    _text(header, "dt_valid", moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _text(header, "ref_spec", "Rnc Product Catalog")
    _text(header, "ref_spec_vers", "1.0")
    _text(header, "s62AgencyCode", S62_AGENCY)

    for entry in sorted(entries, key=lambda item: item["chart_id"]):
        root.append(_chart_element(entry, repository))

    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode", xml_declaration=False)
    document = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"
    assert_plugin_compatible(document)
    return document


def chart_fragment(entry: dict, repository: str) -> str:
    """One ``<chart>`` element, for the review pull-request body."""
    element = _chart_element(entry, repository)
    ET.indent(element, space="  ")
    return ET.tostring(element, encoding="unicode")


def _chart_element(entry: dict, repository: str) -> ET.Element:
    chart = ET.Element("chart")
    _text(chart, "number", entry["chart_id"])
    _text(chart, "title", entry["title"])
    _maybe_int(chart, "source_edition", entry.get("source_edition"))
    _maybe_int(chart, "raster_edition", entry.get("raster_edition"))
    _maybe_int(chart, "ntm_edition", _leading_int(entry.get("ntm_edition")))
    _maybe_text(chart, "source_date", entry.get("source_date"))
    _maybe_text(chart, "ntm_date", entry.get("ntm_date"))
    if entry.get("ntm_edition"):
        _text(chart, "ntm_edition_last_correction", str(entry["ntm_edition"]))
    _text(chart, "zipfile_location", asset_url(repository, entry["chart_id"]))
    stamp = _as_utc(entry["zip_datetime"])
    _text(chart, "zipfile_datetime", stamp.strftime("%Y%m%d_%H%M%S"))
    _text(chart, "zipfile_datetime_iso8601", stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _text(chart, "zipfile_size", str(int(entry["zip_size"])))
    _text(chart, "target_filename", entry.get("target_filename") or f"{entry['chart_id']}.zip")
    return chart


def _text(parent: ET.Element, tag: str, value: str) -> None:
    node = ET.SubElement(parent, tag)
    node.text = value


def _maybe_text(parent: ET.Element, tag: str, value: str | None) -> None:
    if value:
        _text(parent, tag, value)


def _maybe_int(parent: ET.Element, tag: str, value: int | None) -> None:
    if value is None:
        return
    _text(parent, tag, str(int(value)))


def _leading_int(value: object) -> int | None:
    if value is None:
        return None
    match = re_digits(str(value).strip())
    return int(match) if match else None


def re_digits(value: str) -> str | None:
    digits = ""
    for char in value:
        if char.isdigit():
            digits += char
        else:
            break
    return digits or None


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)
