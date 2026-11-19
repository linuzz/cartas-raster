"""The Pages rebuild reads the manifest already in the repo."""

import json
from pathlib import Path

from cartas_raster.cli import main


def test_render_pages_does_not_contact_the_dhn(tmp_path: Path, monkeypatch):
    import cartas_raster.cli as cli

    def refuse(*_args, **_kwargs):
        raise AssertionError("não deve consultar a DHN")

    monkeypatch.setattr(cli, "UrllibTransport", refuse)
    monkeypatch.setattr(cli, "DhnCatalog", refuse)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "last_checked.txt").write_text("2026-11-19T12:23:49Z\n", encoding="utf-8")
    (tmp_path / "data" / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "charts": {
                    "1": {
                        "chart_id": "1",
                        "title": "Um",
                        "zip_size": 10,
                        "zip_datetime": "2024-01-01T00:00:00Z",
                        "zip_filename": "BR_1.zip",
                        "sha256": "ab" * 32,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["render-pages", "--root", str(tmp_path), "--repository", "linuzz/cartas-raster"]) == 0
    xml = (tmp_path / "docs" / "BR_RNCProdCat.xml").read_text(encoding="utf-8")
    page = (tmp_path / "docs" / "index.html").read_text(encoding="utf-8")
    assert "<number>1</number>" in xml
    assert "github.com/linuzz/cartas-raster/releases/download/cartas/BR_1.zip" in xml
    assert "assets.marinha.mil.br" not in xml
    assert "2026-11-19 12:23:49 UTC" in page
    assert "BR_RNCProdCat.xml" in page

    workflow = Path(__file__).resolve().parents[1].joinpath(".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert "cartas-raster-publish" in workflow
    assert "contents: write" not in workflow
    assert "render-pages" in workflow
