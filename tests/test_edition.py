"""Newest DHN ZIP wins. An older file must not replace the published edition."""

from datetime import datetime, timezone

from cartas_raster.dhn import probe_variant_files
from cartas_raster.edition import (
    EditionView,
    bsb_number_matches,
    choose_newest,
    is_newer,
    orphan_asset_names,
    retire_absorbed,
)
from cartas_raster.github_io import CommandResult, GitHub


def _view(se, ed, ne=None, nd=None, lm=None):
    return EditionView(se, ed, ne, nd, lm)


def test_1636_zero_suffix_beats_the_older_plain_zip():
    """The file DHN still serves as 1636.zip is edition 3; 1636_0.zip is edition 4."""
    plain = ("1636.zip", _view(3, "08/30/2004", "2024-216", "10/08/2024"))
    newer = ("1636_0.zip", _view(4, "12/16/2024"))
    name, _view_chosen = choose_newest([plain, newer])
    assert name == "1636_0.zip"
    assert is_newer(newer[1], plain[1])
    assert not is_newer(plain[1], newer[1])


def test_later_suffix_with_a_later_edition_date_wins():
    # Live 1636_1.zip is SE=4 dated 12/31/2024 with NTM 2026-89; _0 is SE=4 dated 12/16/2024.
    zero = ("1636_0.zip", _view(4, "12/16/2024"))
    one = ("1636_1.zip", _view(4, "12/31/2024", "2026-89", "08/19/2026"))
    name, _chosen = choose_newest([zero, one])
    assert name == "1636_1.zip"


def test_same_edition_keeps_the_file_with_the_newer_notice():
    # 3332.zip carries NTM 2026-1; 3332_0.zip is the same paper edition without that notice.
    current = _view(3, "07/31/2024", "2026-1", "03/10/2026")
    older_file = _view(3, "07/31/2024")
    assert is_newer(current, older_file)
    assert not is_newer(older_file, current)


def test_international_suffix_is_not_a_number_mismatch():
    from cartas_raster.cli import _note_bsb_number

    quiet: list[str] = []
    _note_bsb_number(quiet, "23600", "23600(INT2129)")
    _note_bsb_number(quiet, "23600", "23600 (INT 2129)")
    _note_bsb_number(quiet, "23600", "023600")
    assert quiet == []
    assert bsb_number_matches("23600", "23600(INT2129)") is True

    warned: list[str] = []
    _note_bsb_number(warned, "23600", "23601(INT2129)")
    assert warned == ["23600 (BSB NU=23601(INT2129))"]
    assert bsb_number_matches("3332", "333200") is False


def test_notice_date_breaks_a_tie_inside_the_same_notice_number():
    # 1001_0.zip has the same NTM number and a later ND than 1001.zip.
    published = _view(1, "09/28/1996", "2024-43", "04/03/2024")
    corrected = _view(1, "09/28/1996", "2024-43", "05/08/2025")
    assert is_newer(corrected, published)


def test_probe_always_checks_plain_and_zero_then_stops_at_the_first_gap():
    present = {"1636.zip", "1636_0.zip", "1636_1.zip"}
    headed = []

    def head(name):
        headed.append(name)
        return name if name in present else None

    found = probe_variant_files("1636", head)
    assert headed == ["1636.zip", "1636_0.zip", "1636_1.zip", "1636_2.zip"]
    assert found == ["1636.zip", "1636_0.zip", "1636_1.zip"]


def test_probe_does_not_request_suffix_1_when_zero_is_missing():
    headed = []

    def head(name):
        headed.append(name)
        return name if name == "3332.zip" else None

    found = probe_variant_files("3332", head)
    assert headed == ["3332.zip", "3332_0.zip"]
    assert found == ["3332.zip"]


def test_orphan_cleanup_removes_assets_the_manifest_dropped():
    charts = {"1636": {"zip_filename": "BR_1636.zip"}}
    removed = orphan_asset_names(charts, ["BR_1636.zip", "BR_1636PL.zip", "BR_OLD.zip"])
    assert removed == ["BR_1636PL.zip", "BR_OLD.zip"]


def test_older_variant_published_as_a_plan_is_retired_too():
    charts = {
        "1404": {"source_url": "https://assets.example/1404.zip", "zip_filename": "BR_1404.zip"},
        "1404PL": {"source_url": "https://assets.example/1404_0.zip", "zip_filename": "BR_1404PL.zip"},
    }
    retired = retire_absorbed(
        charts,
        {"https://assets.example/1404.zip": "1404"},
        {"https://assets.example/1404.zip": "1404", "https://assets.example/1404_0.zip": "1404"},
    )
    assert retired == ["1404PL"]


def test_absorbed_plan_entry_is_retired_when_the_parent_takes_its_file():
    charts = {
        "1001": {"source_url": "https://assets.example/1001.zip", "zip_filename": "BR_1001.zip"},
        "1001PL": {"source_url": "https://assets.example/1001_0.zip", "zip_filename": "BR_1001PL.zip"},
    }
    retired = retire_absorbed(charts, {"https://assets.example/1001_0.zip": "1001"})
    assert retired == ["1001PL"]


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, args, *, cwd, check=True):
        self.calls.append(args)
        if args[:4] == ["gh", "release", "view", "cartas"] and "--json" in args:
            payload = (
                '{"assets":[{"name":"BR_1636.zip","size":1},'
                '{"name":"BR_1636PL.zip","size":2}]}'
            )
            return CommandResult(0, payload, "")
        return CommandResult(0, "", "")


def test_select_editions_downloads_the_newer_variant_and_does_not_downgrade(tmp_path):
    import io
    import zipfile

    from cartas_raster.cli import _select_editions
    from cartas_raster.dhn import ChartVariants
    from cartas_raster.models import IndexRecord, RemoteFile

    def chart_zip(se, ed, ne=None, nd=None):
        ntm = f"NTM/NE={ne},ND={nd}\r\n" if ne else ""
        payload = f"CHT/NA=ANGRA,NU=1636\r\nCED/SE={se},RE=1,ED={ed}\r\n{ntm}".encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("1636/1636.BSB", payload)
            archive.writestr("1636/1636.KAP", b"VER/3.07\r\n")
        return buffer.getvalue()

    old = chart_zip(3, "08/30/2004", "2024-216", "10/08/2024")
    new = chart_zip(4, "12/16/2024")
    record = IndexRecord("1636", "1636", "Angra", 20000, "1:20000", "Carta 1636", "u", None, None, None, None)

    def remote(name, data, modified):
        return RemoteFile(
            "1636",
            record,
            f"https://assets.example/{name}",
            name,
            len(data),
            modified,
            None,
        )

    old_remote = remote("1636.zip", old, "Mon, 02 Dec 2024 13:42:14 GMT")
    new_remote = remote("1636_0.zip", new, "Wed, 25 Mar 2026 19:26:00 GMT")

    class Transport:
        def get(self, url, **_kwargs):
            body = new if url.endswith("1636_0.zip") else old
            return type("R", (), {"status": 200, "body": body})()

    manifest = {
        "charts": {
            "1636": {
                "zip_name": "1636.zip",
                "content_length": len(old),
                "last_modified": old_remote.last_modified,
                "sha256": "abc",
                "source_edition": 3,
                "source_date": "08/30/2004",
                "ntm_edition": "2024-216",
                "ntm_date": "10/08/2024",
                "source_url": old_remote.source_url,
                "zip_filename": "BR_1636.zip",
            }
        }
    }
    group = ChartVariants(record, [old_remote, new_remote])
    prepared, failures, retired = _select_editions(Transport(), [group], manifest, tmp_path)
    assert failures == []
    assert retired == []
    assert [item.remote.zip_name for item in prepared] == ["1636_0.zip"]
    assert prepared[0].bsb.source_edition == 4

    # The published file is already the newer notice. The older sibling must not replace it.
    current = chart_zip(3, "07/31/2024", "2026-1", "03/10/2026")
    older = chart_zip(3, "07/31/2024")
    record_b = IndexRecord("3332", "3332", "Vallemi", 25000, None, "Carta 3332", "u2", None, None, None, None)
    current_remote = RemoteFile("3332", record_b, "https://assets.example/3332.zip", "3332.zip", len(current), "Thu, 12 Mar 2026 17:21:17 GMT", None)
    older_remote = RemoteFile("3332", record_b, "https://assets.example/3332_0.zip", "3332_0.zip", len(older), "Mon, 02 Dec 2024 13:42:00 GMT", None)

    class TransportB:
        def get(self, url, **_kwargs):
            body = current if url.endswith("3332.zip") else older
            return type("R", (), {"status": 200, "body": body})()

    manifest_b = {
        "charts": {
            "3332": {
                "zip_name": "3332.zip",
                "content_length": len(current),
                "last_modified": current_remote.last_modified,
                "sha256": "def",
                "source_edition": 3,
                "source_date": "07/31/2024",
                "ntm_edition": "2026-1",
                "ntm_date": "03/10/2026",
                "source_url": current_remote.source_url,
                "zip_filename": "BR_3332.zip",
            }
        }
    }
    prepared_b, failures_b, _retired_b = _select_editions(
        TransportB(),
        [ChartVariants(record_b, [current_remote, older_remote])],
        manifest_b,
        tmp_path,
    )
    assert failures_b == []
    assert prepared_b == []


def test_github_cleanup_deletes_only_the_orphan():
    runner = FakeRunner()
    github = GitHub(runner, cwd := __import__("pathlib").Path("."), "linuzz/cartas-raster")
    removed = github.cleanup_orphans({"1636": {"zip_filename": "BR_1636.zip"}})
    assert removed == ["BR_1636PL.zip"]
    assert ["gh", "release", "delete-asset", "cartas", "BR_1636PL.zip", "--repo", "linuzz/cartas-raster", "--yes"] in runner.calls
    assert cwd is not None
