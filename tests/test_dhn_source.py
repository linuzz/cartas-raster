import json

import pytest

from cartas_raster.dhn import (
    DhnCatalog,
    SourceLayoutError,
    chart_id_from_token,
    parse_search_payload,
    parse_title,
    records_from_metadata,
    scale_denominator,
    zip_candidates,
)
from cartas_raster.http_client import GetResult, HeadResult


def _record(title, uuid="u-1", link=None, formats=None):
    return {
        "title": title,
        "format": formats or ["GeoTIFF", "KAP/BSB"],
        "status": "completed",
        "revisionDate": "2013-08-08",
        "link": link
        or [
            "Download da Carta Náutica - Raster||https://www.marinha.mil.br/chm/dados-do-segnav-cartas-raster/puerto-vallemi|WWW:LINK-1.0-http--link|text/html|1"
        ],
        "geonet:info": {"uuid": uuid, "changeDate": "2026-06-03T16:16:56"},
    }


def test_title_and_token_patterns():
    scale, name, token = parse_title(
        "Carta Nautica Raster 1:25.000 – Puerto Vallemi – Carta 3332"
    )
    assert (scale, name, token) == ("1:25.000", "Puerto Vallemi", "3332")
    assert scale_denominator(scale) == 25000
    assert chart_id_from_token(token) == "3332"

    _scale, name, token = parse_title(
        "Carta Nautica Raster 1:12.000 – Porto de São Francisco do Sul – Carta 1831 PL"
    )
    assert token == "1831 PL"
    assert chart_id_from_token(token) == "1831PL"
    assert name.startswith("Porto de São Francisco")

    _scale, _name, token = parse_title(
        "Carta Nautica Raster 1:100.000 – Rio Solimões – Carta 4150 (HS-C6)"
    )
    assert chart_id_from_token(token) == "4150-HS-C6"


def test_zip_candidates_do_not_steal_another_chart():
    assert zip_candidates("3332") == ["3332.zip", "3332_0.zip"]
    assert "1831.zip" not in zip_candidates("1831 PL")
    assert "1831_0.zip" not in zip_candidates("1831 PL")
    assert zip_candidates("1831 PL") == ["1831PL.zip", "1831PL_0.zip"]
    assert zip_candidates("4342A") == ["4342A.zip", "4342A_0.zip"]
    assert "4150.zip" not in zip_candidates("4150 (HS-C6)")
    assert "4032.zip" not in zip_candidates("4032A")


def test_titles_with_uneven_dashes_still_parse():
    cases = [
        "Carta Nautica Raster 1:25.000– Da Isla Comparada a Puerto Barranquerita – Carta 3312",
        "Nautica Raster 1:25.000– Tramandaí – Carta 2011",
        "Carta Raster 1:25.000– Da Isla Comparada A Vuelta Grande – Carta 3313",
        "Carta Nautica Raster 1:200.000 –Arco Lamoso da Barra Norte – Carta 21300 PL",
    ]
    ids = [chart_id_from_token(parse_title(title)[2]) for title in cases]
    assert ids == ["3312", "2011", "3313", "21300PL"]


def test_search_payload_must_keep_the_geonetwork_shape():
    with pytest.raises(SourceLayoutError, match="summary.@count"):
        parse_search_payload({"metadata": []})
    count, items = parse_search_payload({"summary": {"@count": "2"}, "metadata": _record("x")})
    assert count == 2
    assert len(items) == 1


def test_records_from_metadata_reads_the_download_link():
    records, failures = records_from_metadata(
        [
            _record("Carta Nautica Raster 1:25.000 – Puerto Vallemi – Carta 3332"),
            {"title": "Outro produto", "format": ["PDF"], "geonet:info": {"uuid": "pdf"}},
        ]
    )
    assert failures == []
    assert records[0].chart_id == "3332"
    assert records[0].page_url.endswith("/puerto-vallemi")
    assert records[0].scale == 25000


class FakeTransport:
    def __init__(self, gets=None, heads=None):
        self.gets = gets or {}
        self.heads = heads or {}
        self.head_urls = []

    def get(self, url, **_kwargs):
        if url not in self.gets:
            return GetResult(404, {}, b"", url)
        return self.gets[url]

    def head(self, url, **_kwargs):
        self.head_urls.append(url)
        if url not in self.heads:
            return HeadResult(404, {}, url)
        return self.heads[url]


def _zip_head(length=100):
    return HeadResult(
        200,
        {"content-type": "application/zip", "content-length": str(length), "last-modified": "Thu, 12 Mar 2026 17:21:17 GMT", "etag": '"abc"'},
        "",
    )


def test_repeated_page_is_a_layout_error(monkeypatch):
    monkeypatch.setattr("cartas_raster.dhn.PAGE_SIZE", 1)
    item = _record("Carta Nautica Raster 1:25.000 – Puerto Vallemi – Carta 3332", uuid="same")
    payload = json.dumps({"summary": {"@count": "3"}, "metadata": [item]}).encode()
    transport = FakeTransport(gets={"*": GetResult(200, {}, payload, "")})

    class AnyGet(FakeTransport):
        def get(self, url, **_kwargs):
            return GetResult(200, {}, payload, url)

    with pytest.raises(SourceLayoutError, match="paginação"):
        DhnCatalog(AnyGet())._collect_index()


def test_discover_resolves_zip_and_stops_when_almost_nothing_matches(monkeypatch):
    monkeypatch.setattr("cartas_raster.dhn.MIN_INDEX_COUNT", 1)
    monkeypatch.setattr("cartas_raster.dhn.MIN_RESOLVED_RATIO", 0.5)
    monkeypatch.setattr("cartas_raster.dhn.PAGE_SIZE", 10)
    item = _record("Carta Nautica Raster 1:25.000 – Puerto Vallemi – Carta 3332", uuid="3332")
    payload = json.dumps({"summary": {"@count": "1"}, "metadata": [item]}).encode()

    class Transport(FakeTransport):
        def get(self, url, **_kwargs):
            if "geonetwork" in url:
                return GetResult(200, {}, payload, url)
            return GetResult(403, {}, b"<title>Just a moment...</title>", url)

    base = "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/3332.zip"
    catalog = DhnCatalog(Transport(heads={base: _zip_head(183255)}))
    discovery = catalog.discover()
    assert [remote.chart_id for remote in discovery.files] == ["3332"]
    assert discovery.files[0].content_length == 183255
    assert discovery.files[0].zip_name == "3332.zip"


def test_low_resolution_ratio_does_not_publish(monkeypatch):
    monkeypatch.setattr("cartas_raster.dhn.MIN_INDEX_COUNT", 1)
    monkeypatch.setattr("cartas_raster.dhn.MIN_RESOLVED_RATIO", 0.7)
    monkeypatch.setattr("cartas_raster.dhn.PAGE_SIZE", 10)
    items = [
        _record("Carta Nautica Raster 1:25.000 – Puerto Vallemi – Carta 3332", uuid="a"),
        _record("Carta Nautica Raster 1:50.006 – Furo – Carta 4342A", uuid="b"),
    ]
    # The second title needs the en dash around Carta. Rebuild it properly.
    items[1] = _record(
        "Carta Nautica Raster 1:50.006 – Furo dos Macacos – Carta 4342A",
        uuid="b",
    )
    payload = json.dumps({"summary": {"@count": "2"}, "metadata": items}).encode()

    class Transport(FakeTransport):
        def get(self, url, **_kwargs):
            return GetResult(200, {}, payload, url)

    base = "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/"
    transport = Transport(heads={base + "3332.zip": _zip_head()})
    with pytest.raises(SourceLayoutError, match="Nada foi publicado"):
        DhnCatalog(transport).discover()


def test_plan_without_its_own_zip_is_covered_by_the_parent():
    from cartas_raster.dhn import ChartVariants, _plans_covered_by_parent
    from cartas_raster.models import IndexRecord, Unresolved

    parent = IndexRecord("52", "52", "Nome", None, None, "Carta 52", "u", None, None, None, None)
    charts = [ChartVariants(parent, [])]
    unresolved = [
        Unresolved("52PL", "Carta 52 PL", "nenhum ZIP encontrado para o número da carta", None),
        Unresolved("2140APL", "Carta 2140A PL", "nenhum ZIP encontrado para o número da carta", None),
    ]
    rewritten = _plans_covered_by_parent(charts, unresolved)
    assert rewritten[0].reason.startswith("coberta pela carta 52")
    assert "nenhum ZIP" in rewritten[1].reason


def test_shared_zip_keeps_the_sheet():
    from cartas_raster.dhn import _drop_shared_urls
    from cartas_raster.models import IndexRecord, RemoteFile

    def remote(chart_id):
        record = IndexRecord(
            chart_id, chart_id, "Nome", None, None, f"Carta {chart_id}", "u-" + chart_id,
            None, None, None, None,
        )
        return RemoteFile(
            chart_id, record, "https://assets.example/1108_0.zip", "1108_0.zip", 10, None, None,
        )

    kept, unresolved = _drop_shared_urls([remote("1108PL"), remote("1108")])
    assert [item.chart_id for item in kept] == ["1108"]
    assert [item.chart_id for item in unresolved] == ["1108PL"]


def test_override_supplies_the_zip_name(monkeypatch):
    monkeypatch.setattr("cartas_raster.dhn.MIN_INDEX_COUNT", 1)
    monkeypatch.setattr("cartas_raster.dhn.MIN_RESOLVED_RATIO", 0.5)
    monkeypatch.setattr("cartas_raster.dhn.PAGE_SIZE", 10)
    item = _record("Carta Nautica Raster 1:50.006 – Furo dos Macacos – Carta 4342A")
    payload = json.dumps({"summary": {"@count": "1"}, "metadata": [item]}).encode()

    class Transport(FakeTransport):
        def get(self, url, **_kwargs):
            return GetResult(200, {}, payload, url)

    base = "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/especial.zip"
    discovery = DhnCatalog(Transport(heads={base: _zip_head()}), overrides={"4342A": "especial.zip"}).discover()
    assert discovery.files[0].zip_name == "especial.zip"
