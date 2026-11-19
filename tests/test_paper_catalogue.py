"""The paper catalogue is parsed from pdftotext -layout and never blocks a chart."""

import json
from pathlib import Path

from urllib.parse import urlsplit

from cartas_raster.paper_catalogue import (
    SKIPPED_COMPARISON,
    assets_pdf_url,
    catalogue_links,
    edition_gaps,
    newest_catalogue_url,
    parse_catalogue_hints,
    parse_catalogue_text,
    refresh_paper_catalogue,
)

WRAPPED = """
CARTA N°                 TÍTULO                 ESCALA     1ª ED. ÚLT. ED.
     1620   DO PONTAL DE SERNAMBETIBA À ILHA     1:75 000    2022
            GRANDE
     1636   PORTO DE ANGRA DOS REIS E            1:22 000    1979    2024
            PROXIMIDADES
     1637   BAÍA DA RIBEIRA                      1:25 000    1980
     4023A  PARANÁ DE SANTA RITA                 1:50 000    2018    2025
"""


def test_parser_keeps_the_last_edition_year_when_the_title_wraps():
    years = parse_catalogue_text(WRAPPED)
    hints = parse_catalogue_hints(WRAPPED)
    assert hints["1636"].title.startswith("PORTO DE ANGRA")
    assert hints["1636"].scale_label == "1:22000"
    assert years["1636"] == 2024
    assert years["1620"] == 2022
    assert years["1637"] == 1980
    assert years["4023A"] == 2025
    assert "PROXIMIDADES" not in years
    assert "GRANDE" not in years


def test_spaced_letter_is_part_of_the_chart_number():
    text = """
     4020    DA ILHA DO ITUQUI A ARAPIXUMA       1:100 000    2024
    4020 A   PORTO DE SANTARÉM                    1:30 000    2022
    4032 A   PORTO DE MANAUS                     1:30 000     2016
   4101 A    DE MACAPÁ À I. MANGABAL             1:100 000    1967    2012
   4101 B    DA I. MANGABAL À I. DO COMANDAÍ     1:100 000    1967    2012
    4381 A   DE SANTARÉM A SURUCUÁ                   1:100 063    1984
"""
    hints = parse_catalogue_hints(text)
    assert set(hints) == {"4020", "4020A", "4032A", "4101A", "4101B", "4381A"}
    assert "4101" not in hints
    assert hints["4101B"].title.startswith("DA I. MANGABAL")
    assert hints["4101B"].year == 2012
    assert hints["4020"].year == 2024
    assert hints["4020A"].year == 2022
    manifest = json.loads((Path(__file__).resolve().parents[1] / "data" / "manifest.json").read_text())
    for chart_id in ("4020A", "4032A", "4101A", "4381A"):
        assert chart_id in manifest["charts"]


def test_a_word_that_starts_the_title_is_not_a_suffix():
    hints = parse_catalogue_hints("  1234 A BARRA DO RIO                  1:25 000    2019\n")
    assert list(hints) == ["1234"]
    assert hints["1234"].title.startswith("A BARRA")
    assert hints["1234"].year == 2019


def test_scale_may_have_a_space_after_the_colon():
    text = """
     811      PORTO DE NATAL                      1: 8 500    2024
    3402   DA BOCA DA ANTA À VOLTA DA PRAINHA   1: 10 000    2011
   3420    DO ESTIRÃO DA TAQUAREIRA À VOLTA     1: 10 000    2011    2019
"""
    hints = parse_catalogue_hints(text)
    assert hints["811"].title.startswith("PORTO DE NATAL")
    assert hints["811"].year == 2024
    assert hints["811"].scale_label == "1:8500"
    assert hints["3402"].scale_label == "1:10000"
    assert hints["3402"].year == 2011
    assert hints["3420"].year == 2019


def test_gap_report_keeps_the_older_zip_visible():
    gaps = edition_gaps(
        {
            "1636": {
                "zip_name": "1636.zip",
                "source_date": "08/30/2004",
                "source_edition": 3,
            }
        },
        {"1636": 2024},
    )
    assert gaps == [
        {
            "chart_id": "1636",
            "zip_name": "1636.zip",
            "zip_year": 2004,
            "source_date": "08/30/2004",
            "source_edition": 3,
            "catalogue_year": 2024,
        }
    ]
    # 1636_1.zip is edition 4, dated 31 Dec 2024: the paper year is not ahead of it.
    assert edition_gaps({"1636": {"source_date": "12/31/2024", "zip_name": "1636_1.zip"}}, {"1636": 2024}) == []


def test_unavailable_pdf_does_not_raise(tmp_path: Path):
    class Broken:
        def head(self, url):
            raise TimeoutError("sem resposta")

    paper = refresh_paper_catalogue(Broken(), "https://assets.marinha.mil.br/chm/cat.pdf", tmp_path / "cache.json")
    assert paper.years == {}
    assert paper.warning
    assert "indisponível" in paper.warning


def test_glued_geonetwork_host_is_rewritten_without_double_encoding():
    plain = (
        "https://www.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/"
        "2026-03/CAT-15ED-2026-2030%20-FOL4-2026_rev3244.pdf"
    )
    glued = "https://www.m" + plain
    broken_scheme = "h" + plain
    for raw in (plain, glued, broken_scheme):
        built = assets_pdf_url(raw)
        assert built is not None
        parts = urlsplit(built)
        assert parts.netloc == "assets.marinha.mil.br"
        assert "%20" in parts.path
        assert "%2520" not in built
        assert " " not in built
    chosen = newest_catalogue_url(catalogue_links([
        {"link": [f"Catálogo de Cartas e Publicações||{glued}|WWW:DOWNLOAD|application/pdf|1"]},
        {"link": [f"Catálogo de Cartas e Publicações||{plain}|WWW:DOWNLOAD|application/pdf|1"]},
    ]))
    assert urlsplit(chosen).netloc == "assets.marinha.mil.br"


def test_skipped_comparison_does_not_claim_zero_gaps(capsys, tmp_path: Path):
    from cartas_raster.cli import _write_step_summary

    _write_step_summary([], "Catálogo em PDF indisponível em https://www.mhttps/x.pdf (Name or service not known).", 0, False)
    printed = capsys.readouterr().out
    assert SKIPPED_COMPARISON in printed
    assert "Raster mais antigo" not in printed
    assert ": 0" not in printed


def test_newest_pdf_link_follows_the_revision_in_the_records():
    older = "https://www.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/2025-01/CAT-rev100.pdf"
    newer = "https://www.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/2026-03/CAT-15ED-2026-2030%20-FOL4-2026_rev3244.pdf"
    items = [
        {"link": [f"Catálogo de Cartas e Publicações||{older}|WWW:DOWNLOAD|application/pdf|1"]},
        {"link": [f"Catálogo de Cartas e Publicações||{newer}|WWW:DOWNLOAD|application/pdf|1"]},
    ]
    chosen = newest_catalogue_url(catalogue_links(items))
    assert chosen.startswith("https://assets.marinha.mil.br/")
    assert "rev3244" in chosen
