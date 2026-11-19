import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cartas_raster.bsb import inspect_zip
from cartas_raster.chartdldr import assert_plugin_compatible
from cartas_raster.github_io import CommandResult, GitHub, commit_paths
from cartas_raster.models import IndexRecord, OpenPullRequest, RemoteFile
from cartas_raster.pipeline import manifest_entry, package_chart, write_catalog
from cartas_raster.plan import select_for_publish, select_for_review
from cartas_raster.review_text import parse_marker, pull_request_body
from cartas_raster.store import add_rejection, headers_match, is_rejected


def _zip_bytes():
    bsb = (
        "!Comment\r\n"
        "CHT/NA=PUERTO VALLEMI,NU=3332\r\n"
        "CED/SE=3,RE=1,ED=07/31/2024\r\n"
        "NTM/NE=2026-1,ND=03/10/2026\r\n"
        "VER/3.07\r\n"
    ).encode("ascii")
    kap = b"VER/3.07\r\nBSB/NA=PUERTO VALLEMI,NU=333200\r\nCED/SE=3,RE=1,ED=07/31/2024\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3332/3332.BSB", bsb)
        archive.writestr("3332/333200.KAP", kap)
    return buffer.getvalue()


def test_bsb_header_fields():
    info = inspect_zip(_zip_bytes())
    assert info.number == "3332"
    assert info.name == "PUERTO VALLEMI"
    assert info.source_edition == 3
    assert info.raster_edition == 1
    assert info.source_date == "07/31/2024"
    assert info.ntm_edition == "2026-1"
    assert info.ntm_date == "03/10/2026"


def _remote(chart_id="3332", length=None):
    data = _zip_bytes()
    record = IndexRecord(
        chart_id=chart_id,
        token=chart_id,
        name="Puerto Vallemi",
        scale=25000,
        scale_label="1:25000",
        title="Carta Nautica Raster 1:25.000 – Puerto Vallemi – Carta 3332",
        uuid="uuid-3332",
        status="completed",
        revision_date="2013-08-08",
        change_date=None,
        page_url="https://www.marinha.mil.br/chm/dados-do-segnav-cartas-raster/puerto-vallemi",
    )
    return RemoteFile(
        chart_id=chart_id,
        record=record,
        source_url="https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/3332.zip",
        zip_name="3332.zip",
        content_length=length if length is not None else len(data),
        last_modified="Thu, 12 Mar 2026 17:21:17 GMT",
        etag='"abc"',
    ), data


def test_package_becomes_a_plugin_catalog(tmp_path: Path):
    remote, data = _remote()
    path = tmp_path / "3332.zip"
    path.write_bytes(data)
    prepared = package_chart(remote, path)
    entry = manifest_entry(prepared)
    assert entry["sha256"]
    assert entry["source_edition"] == 3
    assert entry["ntm_edition"] == "2026-1"
    catalog = tmp_path / "BR_RNCProdCat.xml"
    write_catalog(catalog, {entry["chart_id"]: entry}, "linuzz/cartas-raster", datetime(2026, 11, 18, tzinfo=timezone.utc))
    parsed = assert_plugin_compatible(catalog.read_text(encoding="utf-8"))
    assert parsed.charts[0].zipfile_location.endswith("/BR_3332.zip")
    assert parsed.charts[0].zipfile_size == len(data)


def test_review_skips_duplicates_and_rejections_and_caps():
    remote, data = _remote()
    path = Path("/tmp/cartas-raster-test-3332.zip")
    path.write_bytes(data)
    prepared = package_chart(remote, path)
    rejected = {"charts": {}}
    add_rejection(rejected, "9999", {"sha256": "dead", "source_edition": 1, "raster_edition": 1, "ntm_edition": "1", "content_length": 1}, "2026-11-18T00:00:00Z")
    assert is_rejected(rejected, "9999", "dead")
    assert not is_rejected(rejected, "3332", prepared.sha256)

    remote_b, _data = _remote("1104")
    prepared_b = package_chart(remote_b, path)
    decision = select_for_review(
        [prepared, prepared_b],
        rejected,
        open_prs=[OpenPullRequest(number=7, chart_id="3332", sha256=prepared.sha256)],
        max_new=1,
    )
    assert decision.skipped_duplicate == ["3332"]
    assert [chart.remote.chart_id for chart in decision.opens] == ["1104"]
    assert decision.skipped_cap == []

    capped = select_for_review([prepared_b, prepared], {"charts": {}}, [], max_new=1)
    assert [chart.remote.chart_id for chart in capped.opens] == ["1104"]
    assert capped.skipped_cap == ["3332"]

    rejected_same = {"charts": {"3332": [{"sha256": prepared.sha256}]}}
    accepted, skipped = select_for_publish([prepared], rejected_same)
    assert accepted == []
    assert skipped == ["3332"]


def test_newer_fingerprint_supersedes_the_open_pr_only_when_it_is_opened():
    remote, data = _remote()
    path = Path("/tmp/cartas-raster-test-3332b.zip")
    path.write_bytes(data)
    prepared = package_chart(remote, path)
    stale = OpenPullRequest(number=4, chart_id="3332", sha256="0" * 64)
    opened = select_for_review([prepared], {"charts": {}}, [stale], max_new=5)
    assert opened.supersede == [stale]
    held = select_for_review([prepared], {"charts": {}}, [stale], max_new=0)
    assert held.opens == []
    assert held.supersede == []
    assert held.skipped_cap == ["3332"]


def test_pull_request_body_has_the_fields_the_owner_reviews():
    remote, data = _remote()
    path = Path("/tmp/cartas-raster-test-body.zip")
    path.write_bytes(data)
    entry = manifest_entry(package_chart(remote, path))
    previous = {
        "chart_id": "3332",
        "title": entry["title"],
        "source_edition": 2,
        "raster_edition": 1,
        "source_date": "01/01/2013",
        "ntm_edition": "2013-1",
        "ntm_date": "01/01/2013",
        "content_length": 10,
        "sha256": "old",
    }
    body = pull_request_body(entry, previous, "linuzz/cartas-raster")
    assert "3332" in body
    assert "Puerto Vallemi" in body
    assert "fonte 2" in body
    assert "fonte 3" in body
    assert "10 bytes" in body
    assert f"{len(data)} bytes" in body
    assert remote.source_url in body
    assert "<zipfile_location>" in body
    assert parse_marker(body) == ("3332", entry["sha256"])


def test_upload_keeps_finished_charts_when_a_later_one_fails(tmp_path: Path):
    from cartas_raster.cli import upload_new_assets
    from cartas_raster.github_io import CommandError

    remote, data = _remote("3332")
    first_path = tmp_path / "3332.zip"
    first_path.write_bytes(data)
    first = package_chart(remote, first_path)
    remote_b, _data = _remote("1104")
    second_path = tmp_path / "1104.zip"
    second_path.write_bytes(data)
    second = package_chart(remote_b, second_path)

    class Publisher:
        def __init__(self):
            self.uploads: list[str] = []

        def release_assets(self):
            return {}

        def upload_asset(self, path: Path):
            if path.name == "BR_1104.zip":
                raise CommandError("rede caiu")
            self.uploads.append(path.name)

    manifest: dict = {"charts": {}}
    published = []
    snapshots = []
    failures = upload_new_assets(
        [first, second],
        manifest,
        Publisher(),
        published,
        lambda: snapshots.append(set(manifest["charts"])),
        every=1,
    )
    assert snapshots == [{"3332"}]
    assert set(manifest["charts"]) == {"3332"}
    assert [chart.remote.chart_id for chart in published] == ["3332"]
    assert len(failures) == 1 and "1104" in failures[0]


def test_upload_skips_only_when_the_release_sha_matches(tmp_path: Path):
    from cartas_raster.cli import upload_new_assets

    remote, data = _remote("3332")
    path = tmp_path / "3332.zip"
    path.write_bytes(data)
    prepared = package_chart(remote, path)

    class Same:
        def release_assets(self):
            return {"BR_3332.zip": {"size": prepared.size, "sha256": prepared.sha256}}

        def upload_asset(self, path: Path):
            raise AssertionError(path.name)

    manifest: dict = {"charts": {}}
    published = []
    failures = upload_new_assets([prepared], manifest, Same(), published, lambda: None, every=20)
    assert failures == []
    assert manifest["charts"]["3332"]["sha256"] == prepared.sha256

    class DifferentSizeSameBytes:
        def __init__(self):
            self.uploads: list[str] = []

        def release_assets(self):
            return {"BR_3332.zip": {"size": prepared.size, "sha256": "0" * 64}}

        def upload_asset(self, path: Path):
            self.uploads.append(path.name)

    again = DifferentSizeSameBytes()
    failures = upload_new_assets([prepared], {"charts": {}}, again, [], lambda: None, every=20)
    assert failures == []
    assert again.uploads == ["BR_3332.zip"]


def test_headers_skip_a_redownload_until_they_change():
    remote, _data = _remote()
    previous = {
        "content_length": remote.content_length,
        "last_modified": remote.last_modified,
        "etag": remote.etag,
        "sha256": "abc",
    }
    assert headers_match(previous, remote.content_length, remote.last_modified, remote.etag)
    assert not headers_match(previous, remote.content_length + 1, remote.last_modified, remote.etag)
    assert not headers_match(None, remote.content_length, remote.last_modified, remote.etag)


class FakeRunner:
    def __init__(self, stdout=""):
        self.calls = []
        self.stdout = stdout

    def run(self, args, *, cwd, check=True):
        self.calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            body = "<!-- cartas-raster chart_id=3332 sha256=abc -->"
            payload = [{"number": 9, "body": body, "labels": []}]
            return CommandResult(0, json.dumps(payload), "")
        if args[:3] == ["gh", "release", "view"]:
            return CommandResult(1, "", "not found")
        return CommandResult(0, self.stdout, "")


def test_github_lists_prs_by_the_hidden_marker(tmp_path: Path):
    found = GitHub(FakeRunner(), tmp_path, "linuzz/cartas-raster").list_open_prs()
    assert found[0].number == 9
    assert found[0].chart_id == "3332"
    assert found[0].sha256 == "abc"


def test_commit_paths_skips_a_clean_tree(tmp_path: Path):
    runner_calls = []

    class Recording(FakeRunner):
        def run(self, args, *, cwd, check=True):
            runner_calls.append(args)
            if args[0] == "git":
                import subprocess

                completed = subprocess.run(args, cwd=cwd, check=False, text=True, capture_output=True)
                if check and completed.returncode != 0:
                    raise RuntimeError(completed.stderr)
                return CommandResult(completed.returncode, completed.stdout, completed.stderr)
            return super().run(args, cwd=cwd, check=check)

    subprocess_import = __import__("subprocess")
    subprocess_import.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess_import.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess_import.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    target = tmp_path / "data.txt"
    target.write_text("um\n", encoding="utf-8")
    runner = Recording()
    assert commit_paths(runner, tmp_path, [target], "primeiro")
    assert not commit_paths(runner, tmp_path, [target], "nada mudou")
