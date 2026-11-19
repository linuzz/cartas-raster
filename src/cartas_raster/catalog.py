"""Write an NOAA-style ``RncProductCatalog`` document.

Field names and date formats are the ones ``chartdldr_pi`` reads. See
``chartdldr.py``. The document is parsed again before it is returned so a
regression that the plugin would reject cannot be written out.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from cartas_raster.chartdldr import assert_plugin_compatible

CATALOG_TITLE = "Cartas Raster DHN — catálogo não oficial (linuzz/cartas-raster)"
# IHO S-62 producer code for Diretoria de Hidrografia e Navegação.
S62_AGENCY = "83"
RELEASE_TAG = "cartas"


def asset_name(chart_id: str) -> str:
    return f"BR_{chart_id}.zip"


def asset_url(repository: str, chart_id: str) -> str:
    return f"https://github.com/{repository}/releases/download/{RELEASE_TAG}/{asset_name(chart_id)}"


def render_pages_index(charts: dict, checked_at: datetime) -> str:
    """Human page for GitHub Pages. The OpenCPN catalogue stays in the XML."""
    moment = checked_at.astimezone(timezone.utc).replace(microsecond=0)
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = []
    for chart_id in sorted(charts, key=str):
        entry = charts[chart_id] or {}
        sha = html.escape(str(entry.get("sha256") or "—"))
        source = str(entry.get("source_url") or "")
        if source:
            safe = html.escape(source, quote=True)
            origin = f'<a href="{safe}"><code>{safe}</code></a>'
        else:
            origin = "—"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(chart_id))}</td>"
            f"<td><code>{sha}</code></td>"
            f"<td>{origin}</td>"
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Catálogo não oficial das cartas raster da DHN</title>
  <style>
    body {{ font-family: Georgia, serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1c2430; }}
    code {{ font-family: ui-monospace, monospace; font-size: 0.85em; }}
    a {{ color: #0b3a66; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
    th, td {{ border-top: 1px solid #d5dde6; text-align: left; vertical-align: top; padding: 0.35rem 0.4rem; }}
    td:nth-child(2) {{ word-break: break-all; }}
  </style>
</head>
<body>
  <h1>Catalogação não oficial das cartas raster da DHN</h1>
  <p>
    Catálogo no formato <code>RncProductCatalog</code> para o plugin Chart Downloader do OpenCPN.
    Este projeto não tem vínculo com a Marinha do Brasil nem com a Diretoria de Hidrografia e Navegação.
    As cartas oficiais são as da DHN, segundo a NORMAM-511/DHN.
    A fonte oficial é o <a href="https://www.marinha.mil.br/chm">site da DHN</a>.
  </p>
  <p>
    Sem garantia. Este catálogo não serve para navegar no lugar das cartas oficiais.
    Uso não comercial. As cartas, os metadados da DHN e o certificado empacotado não estão cobertos pela licença MIT.
    Se a DHN pedir a retirada de algum conteúdo, ele sai do catálogo.
  </p>
  <p>Última verificação: <time datetime="{moment.strftime("%Y-%m-%dT%H:%M:%SZ")}">{html.escape(stamp)}</time>.</p>
  <p>
    URL do catálogo:
    <a href="BR_RNCProdCat.xml"><code>BR_RNCProdCat.xml</code></a>
  </p>
  <p>
    No OpenCPN: Opções → Cartas → Chart Downloader → Add, e cole a URL completa
    <code>https://linuzz.github.io/cartas-raster/BR_RNCProdCat.xml</code>.
  </p>
  <h2>Cartas no manifesto</h2>
  <p>SHA-256 e URL de origem de cada carta, como estão em <code>data/manifest.json</code>.</p>
  <table>
    <thead><tr><th>Carta</th><th>SHA-256</th><th>Origem</th></tr></thead>
    <tbody>
{body}
    </tbody>
  </table>
</body>
</html>
"""


def write_pages_index(path, charts: dict, checked_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_pages_index(charts, checked_at), encoding="utf-8")


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
