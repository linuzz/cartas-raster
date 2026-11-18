"""Optional checks against the live DHN hosts. Skipped unless DHN_LIVE=1."""

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DHN_LIVE"), reason="defina DHN_LIVE=1 para consultar a DHN")


def test_zip_3332_is_a_bsb_chart():
    from cartas_raster.bsb import inspect_zip
    from cartas_raster.dhn import RASTER_BASE
    from cartas_raster.http_client import UrllibTransport

    transport = UrllibTransport(min_interval=0.2, timeout=60)
    head = transport.head(RASTER_BASE + "3332.zip")
    assert head.status == 200
    assert "zip" in head.headers.get("content-type", "")
    body = transport.get(RASTER_BASE + "3332.zip")
    assert body.status == 200
    info = inspect_zip(body.body)
    assert info.number == "3332"
    assert info.source_edition is not None
    assert info.ntm_edition


def test_index_page_parses():
    from cartas_raster.dhn import SEARCH_URL, parse_search_payload, records_from_metadata
    from cartas_raster.http_client import UrllibTransport

    transport = UrllibTransport(min_interval=0.2, timeout=60)
    result = transport.get(SEARCH_URL.format(start=1, end=5))
    assert result.status == 200
    count, items = parse_search_payload(__import__("json").loads(result.body.decode("utf-8")))
    assert count >= 400
    records, failures = records_from_metadata(items)
    assert records
    assert len(failures) <= len(records)
