"""Fixes from the read-only FMEA. Charts are not removed by the new warnings."""

import io
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cartas_raster.dhn import DhnCatalog, TransientSource
from cartas_raster.edition import EditionView, bsb_number_matches, is_newer, ntm_key
from cartas_raster.github_io import CommandResult, asset_sha256
from cartas_raster.http_client import _RETRY_STATUS, retry_wait
from cartas_raster.models import IndexRecord


def _record(chart_id="1404"):
    return IndexRecord(chart_id, chart_id, "Nome", 25000, None, "Carta", "u", None, None, None, None)


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1404/1404.BSB", b"CHT/NA=NOME,NU=1404\r\nCED/SE=2,RE=1,ED=01/01/2018\r\nNTM/NE=3.2020,ND=01/01/2020\r\n")
        archive.writestr("1404/140401.KAP", b"VER/3.07\r\n")
    return buffer.getvalue()


def test_ntm_year_is_compared_before_the_notice_number():
    assert ntm_key("12.2019") == (2019, 12)
    assert ntm_key("3.2020") == (2020, 3)
    assert ntm_key("2026-73") == (2026, 73)
    assert ntm_key("2017.53") == (2017, 53)
    older = EditionView(1, "01/01/2019", "12.2019", None, None)
    newer = EditionView(1, "01/01/2019", "3.2020", None, None)
    assert is_newer(newer, older)
    assert not is_newer(older, newer)


def test_http_403_and_503_are_retried_and_not_stored_as_missing():
    assert 403 in _RETRY_STATUS
    assert 503 in _RETRY_STATUS
    assert retry_wait({"retry-after": "2"}, 30) == 2
    assert retry_wait({"retry-after": "9999"}, 1) == 60
    assert asset_sha256("sha256:ab" * 1) is None
    digest = "sha256:" + "ab" * 32
    assert asset_sha256(digest) == "ab" * 32

    class Broken:
        def head(self, url):
            return type("H", (), {"status": 503, "headers": {}})()

    with pytest.raises(TransientSource, match="erro temporário HTTP 503"):
        DhnCatalog(Broken())._head_zip(_record(), "1404.zip")

    class Missing:
        def head(self, url):
            return type("H", (), {"status": 404, "headers": {}})()

    assert DhnCatalog(Missing())._head_zip(_record(), "1404.zip") is None


def test_unchanged_variant_is_not_downloaded_again(tmp_path: Path):
    from cartas_raster.cli import _select_editions
    from cartas_raster.dhn import ChartVariants
    from cartas_raster.models import RemoteFile

    data = _zip_bytes()
    record = _record()
    remote = RemoteFile(
        "1404",
        record,
        "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/1404.zip",
        "1404.zip",
        len(data),
        "Mon, 02 Dec 2024 13:37:07 GMT",
        '"etag"',
    )

    class Transport:
        def __init__(self):
            self.gets = 0

        def get(self, url, **_kwargs):
            self.gets += 1
            return type("R", (), {"status": 200, "body": data})()

    from cartas_raster.pipeline import manifest_entry

    cache = {"version": 1, "charts": {}}
    transport = Transport()
    group = ChartVariants(record, [remote])
    prepared, failures, _retired = _select_editions(transport, [group], {"charts": {}}, tmp_path, {}, cache)
    assert failures == []
    assert [item.remote.zip_name for item in prepared] == ["1404.zip"]
    assert transport.gets == 1

    again = Transport()
    stored = {"charts": {"1404": manifest_entry(prepared[0])}}
    prepared, failures, _retired = _select_editions(again, [group], stored, tmp_path, {}, cache)
    assert failures == []
    assert prepared == []
    assert again.gets == 0


def test_retire_only_rewrites_the_catalogue(tmp_path: Path):
    from cartas_raster.cli import _persist_publication

    (tmp_path / "data").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "BR_RNCProdCat.xml").write_text("<catalogo>2</catalogo>", encoding="utf-8")
    manifest = {
        "version": 1,
        "charts": {
            "1": {
                "chart_id": "1",
                "title": "Um",
                "zip_size": 10,
                "zip_datetime": "2024-01-01T00:00:00Z",
                "zip_filename": "BR_1.zip",
                "sha256": "a" * 64,
            }
        },
    }

    class Runner:
        def run(self, args, *, cwd, check=True):
            return CommandResult(0, "", "")

    _persist_publication(
        Runner(),
        tmp_path,
        manifest,
        [],
        [],
        [],
        "linuzz/cartas-raster",
        datetime(2026, 11, 19, tzinfo=timezone.utc),
        rewrite_catalog=True,
    )
    text = (tmp_path / "docs" / "BR_RNCProdCat.xml").read_text(encoding="utf-8")
    assert "<number>1</number>" in text
    assert "<number>2</number>" not in text


def test_pdf_timeout_skips_the_hint(monkeypatch, tmp_path: Path):
    from cartas_raster.paper_catalogue import refresh_paper_catalogue

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="pdftotext", timeout=120)

    monkeypatch.setattr("cartas_raster.paper_catalogue.subprocess.run", boom)

    class Transport:
        def head(self, url):
            return type("H", (), {"status": 200, "headers": {"content-length": "4"}})()

        def get(self, url):
            return type("G", (), {"status": 200, "headers": {}, "body": b"%PDF"})()

    paper = refresh_paper_catalogue(
        Transport(),
        "https://assets.marinha.mil.br/chm/cat.pdf",
        tmp_path / "cache.json",
    )
    assert paper.years == {}
    assert paper.warning
    assert "indisponível" in paper.warning
    assert "120" in paper.warning


def test_summary_warns_without_removing(tmp_path: Path, monkeypatch):
    from cartas_raster.cli import _finish_orphan_cleanup, _write_step_summary
    from cartas_raster.models import Unresolved

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    _write_step_summary(
        [],
        None,
        0,
        False,
        absent=["999"],
        transient=[Unresolved("1404", "Carta", "erro temporário HTTP 503 em 1404.zip", None)],
        nu_warnings=["3332 (BSB NU=333200)"],
    )
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Possível cancelamento" in text
    assert "999" in text
    assert "continuam no catálogo" in text
    assert "erro temporário HTTP 503" in text
    assert "333200" in text
    assert bsb_number_matches("3332", "333200") is False
    assert bsb_number_matches("1404", "1404") is True

    class GitHub:
        def cleanup_orphans(self, *_args, **_kwargs):
            raise AssertionError("não devia apagar antes do Pages")

    monkeypatch.setenv("DEFER_ASSET_DELETION", "1")
    assert _finish_orphan_cleanup(GitHub(), {"1": {"zip_filename": "BR_1.zip"}}, 1) == []
