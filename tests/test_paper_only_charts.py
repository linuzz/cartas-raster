"""Paper-catalogue numbers missing from the index are probed, never required."""

import io
import zipfile
from datetime import datetime, timezone

from cartas_raster.catalog import render_catalog
from cartas_raster.cli import _select_editions, _write_step_summary, extend_with_paper_catalogue
from cartas_raster.dhn import PAPER_ONLY_MISSING, ChartVariants, DhnCatalog, Discovery
from cartas_raster.http_client import GetResult, HeadResult
from cartas_raster.models import IndexRecord
from cartas_raster.paper_catalogue import PaperCatalogue, PaperHint
from cartas_raster.pipeline import manifest_entry


def _chart_zip(chart_id: str, name: str) -> bytes:
    bsb = (
        f"CHT/NA={name},NU={chart_id}\r\n"
        "CED/SE=1,RE=1,ED=01/01/2020\r\n"
        "VER/3.07\r\n"
    ).encode("ascii")
    kap = f"VER/3.07\r\nBSB/NA={name},NU={chart_id}\r\n".encode("ascii")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{chart_id}/{chart_id}.BSB", bsb)
        archive.writestr(f"{chart_id}/{chart_id}.KAP", kap)
    return buffer.getvalue()


class _Transport:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.heads: list[str] = []
        self.gets: list[str] = []

    def head(self, url: str) -> HeadResult:
        name = url.rsplit("/", 1)[-1]
        self.heads.append(name)
        body = self.files.get(name)
        if body is None:
            return HeadResult(404, {}, url)
        return HeadResult(
            200,
            {
                "content-type": "application/zip",
                "content-length": str(len(body)),
                "last-modified": "Fri, 21 Mar 2025 12:00:00 GMT",
            },
            url,
        )

    def get(self, url: str) -> GetResult:
        name = url.rsplit("/", 1)[-1]
        self.gets.append(name)
        return GetResult(200, {}, self.files[name], url)


def _indexed(chart_id: str) -> IndexRecord:
    return IndexRecord(
        chart_id=chart_id,
        token=chart_id,
        name=f"Carta {chart_id}",
        scale=25000,
        scale_label="1:25000",
        title=f"Carta {chart_id}",
        uuid=f"index-{chart_id}",
        status="published",
        revision_date=None,
        change_date=None,
        page_url=None,
    )


def _hints() -> dict[str, PaperHint]:
    return {
        "1404": PaperHint(2018, "Carta já no índice", "1:25000"),
        "1624": PaperHint(2020, "Terminal Marítimo da Ilha Guaíba", "1:22000"),
        "1625": PaperHint(2021, "Enseada da Ilha Grande", "1:22000"),
    }


def test_paper_only_zip_is_published_and_a_missing_zip_is_unresolved(tmp_path, capsys):
    body = _chart_zip("1624", "TERMINAL MARITIMO DA ILHA GUAIBA")
    transport = _Transport({"1624.zip": body})
    indexed = _indexed("1404")
    discovery = Discovery(
        charts=[ChartVariants(record=indexed, variants=[])],
        unresolved=[],
        index_count=1,
    )
    found, missing = extend_with_paper_catalogue(
        DhnCatalog(transport), discovery, PaperCatalogue(_hints(), None)
    )
    assert found == ["1624"]
    assert missing == ["1625"]
    assert all(not name.startswith("1404") for name in transport.heads)
    assert "1624_1.zip" not in transport.heads
    assert discovery.unresolved[0].reason == PAPER_ONLY_MISSING
    assert discovery.unresolved[0].chart_id == "1625"

    cache = {"version": 1, "charts": {}}
    prepared, failures, _retired = _select_editions(
        transport, discovery.charts, {"charts": {}}, tmp_path, {"1624": 2020}, cache
    )
    assert failures == []
    published = [item for item in prepared if item.remote.chart_id == "1624"]
    assert len(published) == 1
    entry = manifest_entry(published[0])
    assert entry["title"].startswith("TERMINAL MARITIMO DA ILHA GUAIBA")
    assert entry["official_title"] == "Terminal Marítimo da Ilha Guaíba"
    assert entry["status"] == "published"
    xml = render_catalog([entry], generated_at=datetime.now(timezone.utc), repository="linuzz/cartas-raster")
    assert "https://github.com/linuzz/cartas-raster/releases/download/cartas/BR_1624.zip" in xml
    assert "assets.marinha.mil.br" not in xml

    gets_before = list(transport.gets)
    again, failures, _retired = _select_editions(
        transport, discovery.charts, {"charts": {"1624": entry}}, tmp_path, {"1624": 2020}, cache
    )
    assert failures == []
    assert again == []
    assert transport.gets == gets_before

    _write_step_summary([], None, 3, True, paper_found=found, paper_missing=missing)
    printed = capsys.readouterr().out
    assert "## Cartas descobertas pelo catálogo em papel" in printed
    assert "- 1624" in printed
    assert "não é erro" in printed


def test_pdf_failure_adds_nothing(tmp_path):
    indexed = _indexed("1404")
    discovery = Discovery(
        charts=[ChartVariants(record=indexed, variants=[])],
        unresolved=[],
        index_count=1,
    )
    before_charts = list(discovery.charts)
    before_unresolved = list(discovery.unresolved)
    transport = _Transport({"1624.zip": _chart_zip("1624", "NAO DEVE BAIXAR")})
    found, missing = extend_with_paper_catalogue(
        DhnCatalog(transport),
        discovery,
        PaperCatalogue(_hints(), "Catálogo em PDF indisponível em https://exemplo (tempo)."),
    )
    assert found == []
    assert missing == []
    assert discovery.charts == before_charts
    assert discovery.unresolved == before_unresolved
    assert transport.heads == []
    assert transport.gets == []
