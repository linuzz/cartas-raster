"""The generated catalogue must survive the same rules as chartdldr_pi.

OpenCPN ``plugins/chartdldr_pi/src/chartcatalog.cpp`` (master):

- root name must start with ``RncProductCatalog``, otherwise ``LoadFromXml``
  returns false;
- each ``<chart>`` is a ``RasterChart``;
- ``zipfile_datetime`` is ``ParseFormat("%Y%m%d_%H%M%S")``;
- ``zipfile_datetime_iso8601`` and ``dt_valid`` are split on the characters
  ``T`` and ``Z`` and then ``ParseDate`` / ``ParseTime``;
- ``wxDateTime::ParseDate`` (wxWidgets 3.2, datetimefmt.cpp) treats a numeric
  token above 31 as the year, so the value we emit has to be ``YYYY-MM-DD``;
- ``zipfile_size`` and the edition integers go through ``wxAtoi``.
"""

from datetime import datetime, timezone

from cartas_raster.catalog import render_catalog, render_pages_index
from cartas_raster.chartdldr import assert_plugin_compatible, parse_catalog, wx_parse_date


def _entry():
    return {
        "chart_id": "3332",
        "title": "Puerto Vallemi (1:25000)",
        "source_edition": 3,
        "raster_edition": 1,
        "source_date": "07/31/2024",
        "ntm_edition": "2026-1",
        "ntm_date": "03/10/2026",
        "zip_size": 183255,
        "zip_datetime": "2026-03-12T17:21:17Z",
        "target_filename": "3332.zip",
    }


def test_generated_catalog_matches_plugin_fields():
    xml = render_catalog(
        [_entry()],
        generated_at=datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc),
        repository="linuzz/cartas-raster",
    )
    parsed = assert_plugin_compatible(xml)
    assert parsed.root_name == "RncProductCatalog"
    assert parsed.title == "Cartas Raster DHN — catálogo não oficial (linuzz/cartas-raster)"
    assert parsed.dt_valid == datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    assert len(parsed.charts) == 1
    chart = parsed.charts[0]
    assert chart.number == "3332"
    assert chart.title == "Puerto Vallemi (1:25000)"
    assert chart.zipfile_location == (
        "https://github.com/linuzz/cartas-raster/releases/download/cartas/BR_3332.zip"
    )
    assert chart.zipfile_size == 183255
    assert chart.zipfile_datetime == datetime(2026, 3, 12, 17, 21, 17, tzinfo=timezone.utc)
    assert chart.zipfile_datetime_iso8601 == chart.zipfile_datetime
    assert chart.source_edition == 3
    assert chart.raster_edition == 1
    # wxAtoi keeps the leading integer; the full notice stays in the string field.
    assert chart.ntm_edition == 2026
    assert chart.ntm_edition_last_correction == "2026-1"
    assert chart.ntm_date == "03/10/2026"
    assert chart.target_filename == "3332.zip"


def test_noaa_shaped_catalog_is_accepted():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<RncProductCatalogChartCatalogs>
  <header>
    <title>US RNC Region Example</title>
    <date_created>2026-11-18</date_created>
    <time_created>12:00:00</time_created>
    <date_valid>2026-11-18</date_valid>
    <time_valid>12:00:00</time_valid>
    <dt_valid>2026-11-18T12:00:00Z</dt_valid>
    <ref_spec>Rnc Product Catalog</ref_spec>
    <ref_spec_vers>1.0</ref_spec_vers>
    <s62AgencyCode>550</s62AgencyCode>
  </header>
  <chart>
    <number>18421</number>
    <title>Strait of Juan de Fuca</title>
    <source_edition>12</source_edition>
    <raster_edition>3</raster_edition>
    <zipfile_location>https://charts.noaa.gov/RNCs/18421.zip</zipfile_location>
    <zipfile_datetime>20261118_120000</zipfile_datetime>
    <zipfile_datetime_iso8601>2026-11-18T12:00:00Z</zipfile_datetime_iso8601>
    <zipfile_size>2048</zipfile_size>
  </chart>
</RncProductCatalogChartCatalogs>
"""
    parsed = assert_plugin_compatible(xml)
    assert parsed.root_name.startswith("RncProductCatalog")
    assert parsed.charts[0].number == "18421"
    assert parsed.charts[0].zipfile_size == 2048


def test_invented_root_is_rejected():
    parsed = parse_catalog("<charts><chart><number>1</number></chart></charts>")
    assert not parsed.ok
    assert any("RncProductCatalog" in problem for problem in parsed.problems)


def test_bad_zip_datetime_is_rejected():
    xml = render_catalog(
        [_entry()],
        generated_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
        repository="linuzz/cartas-raster",
    )
    broken = xml.replace("20260312_172117", "2026-03-12 17:21:17")
    parsed = parse_catalog(broken)
    assert any("zipfile_datetime" in problem for problem in parsed.problems)


def test_iso_timestamp_must_split_on_t_and_z():
    xml = render_catalog(
        [_entry()],
        generated_at=datetime(2026, 11, 18, 15, tzinfo=timezone.utc),
        repository="linuzz/cartas-raster",
    )
    broken = xml.replace("2026-03-12T17:21:17Z", "2026-03-12 17:21:17+00:00")
    parsed = parse_catalog(broken)
    assert any("zipfile_datetime_iso8601" in problem for problem in parsed.problems)


def test_wx_parse_date_accepts_only_year_first_form():
    assert wx_parse_date("2026-11-18") == (2026, 11, 18)
    # Day-first and month-first strings are ambiguous in wxDateTime::ParseDate.
    assert wx_parse_date("18/11/2026") is None
    assert wx_parse_date("11/18/2026") is None
    assert wx_parse_date("20261118") is None


def test_empty_catalog_is_valid():
    xml = render_catalog([], generated_at=datetime(2026, 11, 18, tzinfo=timezone.utc), repository="linuzz/cartas-raster")
    parsed = assert_plugin_compatible(xml)
    assert parsed.charts == []


def test_pages_index_lists_the_check_date_sha_and_source():
    page = render_pages_index(
        {
            "1404": {
                "sha256": "abc123",
                "source_url": "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/1404.zip",
            }
        },
        datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
    )
    assert "Catalogação não oficial" in page
    assert "2026-11-19 12:00:00 UTC" in page
    assert "abc123" in page
    assert "1404.zip" in page
    assert "NORMAM-511/DHN" in page
    assert "não estão cobertos pela licença MIT" in page
    lowered = page.lower()
    assert "redistrib" not in lowered
    assert "republic" not in lowered
    assert "espelho" not in lowered
    assert "@" not in page
