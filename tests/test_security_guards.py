"""Guardrails from the security audit. Network and GitHub are mocked."""

import hashlib
import io
import stat
import zipfile
from pathlib import Path

import pytest

from cartas_raster.bsb import NotAChartZip, _reject_unsafe_member, inspect_zip
from cartas_raster.edition import OrphanCleanupRefused, assert_orphan_cleanup_allowed
from cartas_raster.github_io import CommandResult, GitHub
from cartas_raster.http_client import (
    DownloadTooLarge,
    MarinhaRedirectHandler,
    RejectedUrl,
    UrllibTransport,
    read_bounded,
    require_marinha_https,
)
from cartas_raster.pipeline import ChartPackageError
from cartas_raster.proposal import (
    materialize_verified_zip,
    require_chart_id,
    require_known_charts,
    require_raster_url,
    require_zip_name,
)


def _chart_zip(name: str = "3332.KAP", payload: bytes | None = None, *, compression=zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("3332/", b"")
        archive.writestr("3332/3332.BSB", b"CHT/NA=ANGRA,NU=3332\r\nCED/SE=3,RE=1,ED=07/31/2024\r\n")
        archive.writestr(name, payload if payload is not None else b"VER/3.07\r\n")
    return buffer.getvalue()


def _zip_with_extra(extra_name: str, payload: bytes = b"extra") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1404/", b"")
        archive.writestr("1404/1404.BSB", b"CHT/NA=ANGRA,NU=1404\r\nCED/SE=2,RE=1,ED=01/01/2018\r\n")
        archive.writestr("1404/140401.KAP", b"VER/3.07\r\n")
        archive.writestr(extra_name, payload)
    return buffer.getvalue()


def test_normal_orphan_cleanup_still_allows_a_small_plan_drop():
    charts = {str(index): {"zip_filename": f"BR_{index}.zip"} for index in range(388)}
    release = [f"BR_{index}.zip" for index in range(388)] + [f"BR_old_{index}.zip" for index in range(19)]
    removed = assert_orphan_cleanup_allowed(
        charts,
        release,
        previous_chart_count=407,
        max_deletions=25,
        max_deletion_ratio=0.10,
        min_retain_ratio=0.90,
    )
    assert len(removed) == 19


def test_orphan_cleanup_refuses_when_the_list_exceeds_the_cap():
    charts = {"1": {"zip_filename": "BR_1.zip"}}
    release = ["BR_1.zip"] + [f"BR_old_{index}.zip" for index in range(26)]
    with pytest.raises(OrphanCleanupRefused, match="BR_old_0.zip"):
        assert_orphan_cleanup_allowed(
            charts,
            release,
            previous_chart_count=1,
            max_deletions=25,
            max_deletion_ratio=1.0,
            min_retain_ratio=0.90,
        )


def test_orphan_cleanup_refuses_when_the_manifest_shrinks():
    charts = {"1": {"zip_filename": "BR_1.zip"}}
    with pytest.raises(OrphanCleanupRefused, match="abaixo"):
        assert_orphan_cleanup_allowed(
            charts,
            ["BR_1.zip"],
            previous_chart_count=10,
            max_deletions=25,
            max_deletion_ratio=1.0,
            min_retain_ratio=0.90,
        )


def test_orphan_cleanup_requires_zip_filename_and_does_not_delete(capsys):
    class Runner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, args, *, cwd, check=True):
            self.calls.append(args)
            if args[:4] == ["gh", "release", "view", "cartas"] and "--json" in args:
                return CommandResult(0, '{"assets":[{"name":"BR_1.zip","size":1},{"name":"BR_OLD.zip","size":2}]}', "")
            return CommandResult(0, "", "")

    runner = Runner()
    github = GitHub(runner, Path("."), "linuzz/cartas-raster")
    with pytest.raises(OrphanCleanupRefused, match="zip_filename"):
        github.cleanup_orphans({"1": {}}, previous_chart_count=1)
    assert not any(call[:3] == ["gh", "release", "delete-asset"] for call in runner.calls)
    assert "recusada" in capsys.readouterr().out


def test_redirects_stay_on_marinha_https():
    handler = MarinhaRedirectHandler()
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://assets.marinha.mil.br/chm/a.zip", method="GET"
    )
    allowed = handler.redirect_request(
        request, None, 302, "Found", {}, "https://idem.marinha.mil.br/geonetwork/srv"
    )
    assert allowed.get_full_url().startswith("https://idem.marinha.mil.br/")
    for target in (
        "http://assets.marinha.mil.br/chm/a.zip",
        "https://example.com/a.zip",
        "https://user@assets.marinha.mil.br/chm/a.zip",
        "https://assets.marinha.mil.br:8443/chm/a.zip",
    ):
        with pytest.raises(RejectedUrl):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def test_redirect_body_is_capped_before_the_next_request():
    class Flood:
        def __init__(self):
            self.reads = 0

        def read(self, _size=-1):
            self.reads += 1
            if self.reads > 8:
                return b""
            return b"z" * 64 * 1024

        def close(self):
            pass

    class Parent:
        def __init__(self):
            self.opened = 0

        def open(self, *_args, **_kwargs):
            self.opened += 1
            raise AssertionError("redirect não deveria ser seguido")

    flood = Flood()
    parent = Parent()
    handler = MarinhaRedirectHandler()
    handler.parent = parent
    headers = {"location": "https://assets.marinha.mil.br/chm/proximo.zip"}
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://assets.marinha.mil.br/chm/a.zip", method="GET"
    )
    with pytest.raises(DownloadTooLarge):
        handler.http_error_302(request, flood, 302, "Found", headers, )
    assert parent.opened == 0
    assert flood.reads <= 3


def test_read_bounded_aborts_without_draining_the_rest():
    class Stream:
        def __init__(self):
            self.calls = 0

        def read(self, _size=-1):
            self.calls += 1
            if self.calls > 4:
                return b""
            return b"abcd"

    stream = Stream()
    with pytest.raises(DownloadTooLarge):
        read_bounded(stream, limit=6, chunk_size=4)
    assert stream.calls == 2


def test_transport_rejects_other_hosts_and_oversized_bodies_without_reading(monkeypatch):
    opened: list[str] = []

    class Opener:
        def open(self, request, timeout=None):
            opened.append(request.full_url)
            raise AssertionError("não deveria conectar")

    transport = UrllibTransport(min_interval=0, retries=1, max_bytes=8)
    transport._opener = Opener()
    with pytest.raises(RejectedUrl):
        transport.get("https://example.com/arquivo.zip")
    with pytest.raises(RejectedUrl):
        transport.get("http://assets.marinha.mil.br/chm/a.zip")
    assert opened == []

    class Body:
        def __init__(self):
            self.reads = 0

        def read(self, _size=-1):
            self.reads += 1
            return b"x" * 100

        def geturl(self):
            return "https://evil.example/redirecionado.zip"

        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    body = Body()

    class Final:
        def open(self, request, timeout=None):
            return body

    transport._opener = Final()
    with pytest.raises(RejectedUrl):
        transport.get("https://assets.marinha.mil.br/chm/a.zip")
    assert body.reads == 0

    class Huge:
        def __init__(self):
            self.reads = 0

        def read(self, _size=-1):
            self.reads += 1
            if self.reads > 3:
                return b""
            return b"y" * 100

        def geturl(self):
            return "https://assets.marinha.mil.br/chm/a.zip"

        status = 200
        headers = {"content-length": "100"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    huge = Huge()

    class HugeOpener:
        def open(self, request, timeout=None):
            return huge

    transport._opener = HugeOpener()
    with pytest.raises(DownloadTooLarge):
        transport.get("https://idem.marinha.mil.br/geonetwork/srv")
    assert huge.reads == 0
    require_marinha_https("https://assets.marinha.mil.br/chm/a.zip")
    monkeypatch.delenv("UNUSED", raising=False)


def test_thumbs_db_is_ignored_and_the_zip_is_accepted(capsys):
    data = _zip_with_extra("1404/Thumbs.db", b"\x00\x00")
    info = inspect_zip(data)
    assert info.number == "1404"
    logged = capsys.readouterr().out
    assert "AVISO" not in logged
    assert "Thumbs.db" in logged
    assert "Extras ignorados" in logged


def test_geotiff_sidecar_is_ignored_and_the_zip_is_accepted(capsys):
    data = _zip_with_extra("21080/21080geotiff.tif", b"II*\x00")
    info = inspect_zip(data)
    assert info.number == "1404"
    logged = capsys.readouterr().out
    assert "AVISO" not in logged
    assert "21080geotiff.tif" in logged


def test_executable_member_is_rejected():
    with pytest.raises(NotAChartZip, match="não permitido"):
        inspect_zip(_zip_with_extra("1404/setup.exe", b"MZ"))


def test_symlink_member_is_still_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1404/1404.BSB", b"CHT/NA=A,NU=1404\r\nCED/SE=1,RE=1,ED=01/01/2018\r\n")
        archive.writestr("1404/140401.KAP", b"VER/3.07\r\n")
        info = zipfile.ZipInfo("1404/link.kap")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, b"1404/140401.KAP")
    with pytest.raises(NotAChartZip, match="symlink"):
        inspect_zip(buffer.getvalue())


def test_zip_validation_rejects_unsafe_members_and_keeps_valid_bytes(tmp_path: Path):
    valid = _chart_zip()
    assert inspect_zip(valid).number == "3332"
    path = tmp_path / "3332.zip"
    path.write_bytes(valid)
    from cartas_raster.models import IndexRecord, RemoteFile
    from cartas_raster.pipeline import package_chart

    record = IndexRecord("3332", "3332", "Angra", 25000, None, "Carta 3332", "u", None, None, None, None)
    remote = RemoteFile(
        "3332",
        record,
        "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/3332.zip",
        "3332.zip",
        len(valid),
        None,
        None,
    )
    packaged = package_chart(remote, path)
    assert path.read_bytes() == valid
    assert packaged.sha256 == hashlib.sha256(valid).hexdigest()

    def rejected(data: bytes, match: str):
        with pytest.raises(NotAChartZip, match=match):
            inspect_zip(data)

    rejected(_chart_zip("../x.kap"), r"\.\.")
    rejected(_chart_zip("/tmp/x.kap"), "absoluto")
    rejected(_zip_with_extra("1404/setup.exe", b"MZ"), "não permitido")

    info = zipfile.ZipInfo("link.kap")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(NotAChartZip, match="symlink"):
        _reject_unsafe_member(info, max_ratio=100)
    nul = zipfile.ZipInfo("a.kap")
    nul.filename = "a\x00.kap"
    with pytest.raises(NotAChartZip, match="NUL"):
        _reject_unsafe_member(nul, max_ratio=100)

    bomb = _chart_zip("bomb.kap", b"\x00" * 50_000, compression=zipfile.ZIP_DEFLATED)
    rejected(bomb, "taxa de compressão")
    fat = _chart_zip("fat.kap", b"x" * 2000)
    with pytest.raises(NotAChartZip, match="descomprimido"):
        inspect_zip(fat, max_uncompressed=500, max_ratio=1000)


def test_merged_proposal_is_confined_and_hashed_before_write(tmp_path: Path):
    body = _chart_zip()
    digest = hashlib.sha256(body).hexdigest()
    url = "https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/3332.zip"
    assert require_chart_id("52PL") == "52PL"
    assert require_zip_name("3332_0.zip") == "3332_0.zip"
    assert require_raster_url(url, "3332.zip") == url
    with pytest.raises(ChartPackageError):
        require_chart_id("../etc")
    with pytest.raises(ChartPackageError):
        require_zip_name("../3332.zip")
    with pytest.raises(ChartPackageError):
        require_raster_url("http://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/3332.zip", "3332.zip")
    with pytest.raises(ChartPackageError):
        require_raster_url("https://example.com/3332.zip", "3332.zip")
    with pytest.raises(ChartPackageError):
        require_raster_url(url.replace("3332.zip", "outro.zip"), "3332.zip")
    with pytest.raises(ChartPackageError, match="retire_chart_ids"):
        require_known_charts(["9999"], {"3332": {}})

    downloads = tmp_path / "downloads"
    with pytest.raises(ChartPackageError, match="sha256"):
        materialize_verified_zip(downloads, "3332.zip", body, "0" * 64)
    assert not downloads.exists()
    outside = tmp_path / "escaped.zip"
    with pytest.raises(ChartPackageError):
        materialize_verified_zip(downloads, "../escaped.zip", body, digest)
    assert not outside.exists()
    written = materialize_verified_zip(downloads, "3332.zip", body, digest)
    assert written.read_bytes() == body
    assert written.parent == downloads.resolve()
