"""Command line used by GitHub Actions and by a local dry run."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from cartas_raster.catalog import asset_name
from cartas_raster.chartdldr import assert_plugin_compatible
from cartas_raster.dhn import DhnCatalog
from cartas_raster.errors import SourceLayoutError
from cartas_raster.github_io import GitHub, SubprocessRunner, commit_paths, push_head
from cartas_raster.http_client import UrllibTransport
from cartas_raster.models import PreparedChart, RemoteFile, Unresolved
from cartas_raster.pipeline import ChartPackageError, manifest_entry, package_chart, write_catalog
from cartas_raster.plan import select_for_publish, select_for_review
from cartas_raster.review_text import proposal_document, pull_request_body, pull_request_title
from cartas_raster.store import (
    CATALOG_PATH,
    PROPOSALS_DIR,
    UNRESOLVED_PATH,
    add_rejection,
    headers_match,
    load_manifest,
    load_overrides,
    load_rejected,
    save_json,
)

DEFAULT_REPOSITORY = "linuzz/cartas-raster"
# Commit the manifest often enough that a killed job keeps the charts already uploaded.
CHECKPOINT_EVERY = 20


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cartas-raster")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Descobre cartas na DHN e publica ou abre PRs")
    run.add_argument("--mode", required=True, choices=("auto", "review"))
    run.add_argument("--root", type=Path, default=Path.cwd())
    run.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))
    run.add_argument("--max-review-prs", type=int, default=int(os.environ.get("MAX_REVIEW_PRS", "30")))
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--download-dir", type=Path)

    publish = sub.add_parser("publish-merged", help="Publica a carta de um PR mesclado")
    publish.add_argument("--proposal", type=Path, required=True)
    publish.add_argument("--root", type=Path, default=Path.cwd())
    publish.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))
    publish.add_argument("--dry-run", action="store_true")

    reject = sub.add_parser("reject-closed", help="Registra a rejeição de um PR fechado")
    reject.add_argument("--proposal", type=Path, required=True)
    reject.add_argument("--root", type=Path, default=Path.cwd())
    reject.add_argument("--dry-run", action="store_true")

    check = sub.add_parser("check-catalog", help="Valida um catálogo contra o parser do plugin")
    check.add_argument("path", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_update(args)
        if args.command == "publish-merged":
            return publish_merged(args)
        if args.command == "reject-closed":
            return reject_closed(args)
        if args.command == "check-catalog":
            parsed = assert_plugin_compatible(args.path.read_text(encoding="utf-8"))
            print(f"Catálogo compatível com chartdldr_pi: {len(parsed.charts)} carta(s).")
            return 0
    except SourceLayoutError as exc:
        print(f"ERRO DE LAYOUT: {exc}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 1
    return 1


def run_update(args) -> int:
    root: Path = args.root.resolve()
    manifest = load_manifest(root)
    rejected = load_rejected(root)
    catalog = DhnCatalog(UrllibTransport(), load_overrides(root))
    print("Lendo o índice de cartas raster da DHN…")
    discovery = catalog.discover()
    print(
        f"Índice: {discovery.index_count} registros; "
        f"{len(discovery.files)} ZIPs encontrados; "
        f"{len(discovery.unresolved)} sem arquivo."
    )
    download_dir = args.download_dir or (root / "run" / "downloads")
    prepared, failures = _download_changed(catalog.transport, discovery.files, manifest, download_dir)
    attempts = len(failures) + len(prepared)
    if attempts >= 5 and len(failures) / attempts > 0.5:
        raise SourceLayoutError(
            f"{len(failures)} de {attempts} ZIPs baixados não são cartas BSB/KAP. "
            "O formato dos arquivos mudou. Nada foi publicado.\n" + "\n".join(failures[:10])
        )
    for failure in failures:
        print(f"AVISO: {failure}")

    content, header_only = _split_content_changes(prepared, manifest)
    print(f"Cartas com conteúdo novo: {len(content)}. Só o cabeçalho HTTP mudou: {len(header_only)}.")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    unresolved = list(discovery.unresolved)
    for failure in failures:
        unresolved.append(Unresolved(chart_id="?", title=failure, reason="falha ao ler o ZIP"))
    if args.mode == "auto":
        return _apply_auto(args, root, manifest, rejected, content, header_only, unresolved, now)
    return _apply_review(args, root, manifest, rejected, content, header_only, unresolved, now)


def publish_merged(args) -> int:
    root: Path = args.root.resolve()
    document = json.loads(args.proposal.read_text(encoding="utf-8"))
    proposed = document["proposed"]
    chart_id = proposed["chart_id"]
    print(f"Publicando carta {chart_id} a partir de {args.proposal}…")
    transport = UrllibTransport()
    result = transport.get(proposed["source_url"])
    if result.status != 200:
        raise ChartPackageError(f"{chart_id}: a DHN respondeu HTTP {result.status} ao republicar")
    download_dir = root / "run" / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    raw_path = download_dir / proposed.get("zip_name", f"{chart_id}.zip")
    raw_path.write_bytes(result.body)
    remote = RemoteFile(
        chart_id=chart_id,
        record=_record_from_entry(proposed),
        source_url=proposed["source_url"],
        zip_name=proposed.get("zip_name") or f"{chart_id}.zip",
        content_length=raw_path.stat().st_size,
        last_modified=proposed.get("last_modified"),
        etag=proposed.get("etag"),
    )
    packaged = package_chart(remote, raw_path)
    if packaged.sha256 != proposed["sha256"]:
        raise ChartPackageError(
            f"{chart_id}: o ZIP na DHN mudou desde a aprovação "
            f"(sha256 {packaged.sha256}, proposta {proposed['sha256']}). "
            "Nada foi publicado; rode o workflow de atualização para abrir uma proposta nova."
        )
    if args.dry_run:
        print(f"Dry-run: SHA-256 confere para a carta {chart_id}. Nenhum upload nem commit.")
        return 0
    asset = download_dir / asset_name(chart_id)
    shutil.copyfile(raw_path, asset)
    GitHub(SubprocessRunner(), root, args.repository).upload_asset(asset)
    manifest = load_manifest(root)
    manifest["charts"][chart_id] = proposed
    manifest["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    catalog_path = root / CATALOG_PATH
    write_catalog(
        catalog_path,
        manifest["charts"],
        args.repository,
        datetime.now(timezone.utc),
    )
    save_json(root / "data" / "manifest.json", manifest)
    proposal_path = root / PROPOSALS_DIR / f"{chart_id}.json"
    if proposal_path.exists():
        proposal_path.unlink()
    runner = SubprocessRunner()
    _ensure_identity(runner, root)
    committed = commit_paths(
        runner,
        root,
        [root / "data" / "manifest.json", catalog_path, proposal_path],
        f"Publica carta {chart_id}",
    )
    if committed:
        push_head(runner, root, "main")
    print(f"Carta {chart_id} publicada.")
    return 0


def reject_closed(args) -> int:
    root: Path = args.root.resolve()
    document = json.loads(args.proposal.read_text(encoding="utf-8"))
    proposed = document["proposed"]
    rejected = load_rejected(root)
    add_rejection(
        rejected,
        proposed["chart_id"],
        proposed,
        datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    path = root / "data" / "rejected.json"
    save_json(path, rejected)
    print(
        f"Edição rejeitada: carta {proposed['chart_id']}, sha256 {proposed['sha256']}. "
        "Não será proposta de novo enquanto o arquivo não mudar."
    )
    if args.dry_run:
        return 0
    runner = SubprocessRunner()
    _ensure_identity(runner, root)
    if commit_paths(runner, root, [path], f"Rejeita edição da carta {proposed['chart_id']}"):
        push_head(runner, root, "main")
    return 0


def _apply_auto(args, root, manifest, rejected, content, header_only, unresolved, now) -> int:
    accepted, skipped = select_for_publish(content, rejected)
    for chart_id in skipped:
        print(f"Mantida rejeição da carta {chart_id}.")
    if args.dry_run:
        for chart in accepted:
            entry = manifest_entry(chart)
            print(f"Publicaria {entry['chart_id']} ({entry['title']}) edição {_edition(entry)} tamanho {entry['zip_size']}")
        print(f"Dry-run: {len(accepted)} carta(s), sem upload e sem commit.")
        return 0
    runner = SubprocessRunner()
    _ensure_identity(runner, root)
    _apply_header_updates(manifest, header_only)
    published: list[PreparedChart] = []
    failures: list[str] = []

    def persist() -> None:
        _persist_publication(
            runner,
            root,
            manifest,
            published,
            header_only,
            unresolved,
            args.repository,
            now,
        )

    try:
        if accepted:
            github = GitHub(runner, root, args.repository)
            failures = upload_new_assets(accepted, manifest, github, published, persist)
    finally:
        persist()
    if failures:
        print(f"{len(published)} carta(s) ficaram no manifesto. Rode de novo para continuar.", flush=True)
        for line in failures:
            print(line, flush=True)
        return 1
    return 0


def _apply_review(args, root, manifest, rejected, content, header_only, unresolved, now) -> int:
    runner = SubprocessRunner()
    github = GitHub(runner, root, args.repository)
    open_prs = [] if args.dry_run else github.list_open_prs()
    decision = select_for_review(content, rejected, open_prs, args.max_review_prs)
    print(
        f"Revisão: abrir {len(decision.opens)}, duplicados {len(decision.skipped_duplicate)}, "
        f"rejeitados {len(decision.skipped_rejected)}, adiados {len(decision.skipped_cap)}, "
        f"substituir {len(decision.supersede)}."
    )
    if args.dry_run:
        for chart in decision.opens:
            entry = manifest_entry(chart)
            print(f"Abriria PR da carta {entry['chart_id']} ({entry['title']}) edição {_edition(entry)}")
        return 0
    _apply_header_updates(manifest, header_only)
    manifest_path = root / "data" / "manifest.json"
    unresolved_path = _write_unresolved(root, unresolved)
    if header_only:
        manifest["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_json(manifest_path, manifest)
    _ensure_identity(runner, root)
    if header_only and commit_paths(
        runner,
        root,
        [manifest_path, unresolved_path],
        "Atualiza cabeçalhos HTTP sem mudança de conteúdo",
    ):
        push_head(runner, root, "main")
    elif commit_paths(runner, root, [unresolved_path], "Atualiza a lista de cartas sem ZIP"):
        push_head(runner, root, "main")
    if not decision.opens and not decision.supersede:
        return 0
    github.ensure_labels()
    for pr in decision.supersede:
        print(f"Fechando PR #{pr.number} da carta {pr.chart_id} (edição substituída).")
        github.supersede(pr.number, pr.chart_id)
    for chart in decision.opens:
        _open_one_pr(args, root, runner, github, manifest, chart)
    runner.run(["git", "checkout", "main"], cwd=root)
    return 0


def _open_one_pr(args, root: Path, runner: SubprocessRunner, github: GitHub, manifest: dict, chart: PreparedChart) -> None:
    entry = manifest_entry(chart)
    previous = manifest["charts"].get(chart.remote.chart_id)
    branch = f"review/{chart.remote.chart_id}-{chart.sha256[:12]}"
    print(f"Abrindo PR da carta {entry['chart_id']} na branch {branch}…")
    runner.run(["git", "checkout", "-B", branch, "main"], cwd=root)
    proposal_path = root / PROPOSALS_DIR / f"{chart.remote.chart_id}.json"
    save_json(proposal_path, proposal_document(entry, previous))
    body_path = root / "run" / f"{chart.remote.chart_id}.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(pull_request_body(entry, previous, args.repository), encoding="utf-8")
    commit_paths(runner, root, [proposal_path], pull_request_title(entry))
    push_head(runner, root, branch)
    github.open_pull_request(branch=branch, title=pull_request_title(entry), body_file=body_path)


def _download_changed(transport, files: list[RemoteFile], manifest: dict, download_dir: Path):
    download_dir.mkdir(parents=True, exist_ok=True)
    charts = manifest.get("charts", {})
    prepared: list[PreparedChart] = []
    failures: list[str] = []
    for remote in files:
        previous = charts.get(remote.chart_id)
        if headers_match(previous, remote.content_length, remote.last_modified, remote.etag):
            continue
        destination = download_dir / remote.zip_name
        try:
            result = transport.get(remote.source_url)
            if result.status != 200:
                raise ChartPackageError(f"{remote.chart_id}: HTTP {result.status} ao baixar {remote.source_url}")
            destination.write_bytes(result.body)
            prepared.append(package_chart(remote, destination))
        except (ChartPackageError, OSError) as exc:
            failures.append(str(exc))
    return prepared, failures


def _split_content_changes(prepared: list[PreparedChart], manifest: dict):
    content: list[PreparedChart] = []
    header_only: list[PreparedChart] = []
    charts = manifest.get("charts", {})
    for chart in prepared:
        previous = charts.get(chart.remote.chart_id)
        if previous and previous.get("sha256") == chart.sha256:
            header_only.append(chart)
        else:
            content.append(chart)
    return content, header_only


def _apply_header_updates(manifest: dict, header_only: list[PreparedChart]) -> None:
    for chart in header_only:
        entry = manifest["charts"][chart.remote.chart_id]
        entry["content_length"] = chart.remote.content_length
        entry["last_modified"] = chart.remote.last_modified
        entry["etag"] = chart.remote.etag
        entry["source_url"] = chart.remote.source_url


def _write_unresolved(root: Path, items: list[Unresolved]) -> Path:
    payload = {
        "version": 1,
        "charts": [
            {
                "chart_id": item.chart_id,
                "title": item.title,
                "reason": item.reason,
                "page_url": item.page_url,
            }
            for item in sorted(items, key=lambda item: (item.chart_id, item.title))
        ],
    }
    path = root / UNRESOLVED_PATH
    save_json(path, payload)
    return path


def upload_new_assets(accepted, manifest, github, published: list[PreparedChart], persist, every: int = CHECKPOINT_EVERY) -> list[str]:
    """Upload charts, record each success in ``manifest``, and checkpoint along the way.

    An asset already on the release with the same byte size is not uploaded again.
    A failure on one chart does not discard charts already stored in ``manifest``.
    """
    failures: list[str] = []
    sizes = github.release_asset_sizes()
    since_commit = 0
    for chart in accepted:
        asset = _asset_file(chart)
        chart_id = chart.remote.chart_id
        try:
            size = asset.stat().st_size
            if sizes.get(asset.name) == size:
                print(
                    f"{asset.name} já está na release ({size} bytes); sem novo upload.",
                    flush=True,
                )
            else:
                print(f"Enviando {asset.name}…", flush=True)
                github.upload_asset(asset)
                sizes[asset.name] = size
            manifest["charts"][chart_id] = manifest_entry(chart)
            published.append(chart)
            since_commit += 1
            if since_commit >= every:
                persist()
                since_commit = 0
        except Exception as exc:
            message = f"{chart_id}: {exc}"
            failures.append(message)
            print(f"Falha ao publicar {chart_id}: {exc}", flush=True)
            if _fatal_github_error(exc):
                break
    return failures


def _fatal_github_error(exc: BaseException) -> bool:
    text = str(exc)
    lowered = text.lower()
    return "GH_TOKEN" in text or "bad credentials" in lowered or "authentication" in lowered


def _persist_publication(runner, root, manifest, published, header_only, unresolved, repository, now) -> None:
    manifest_path = root / "data" / "manifest.json"
    catalog_path = root / CATALOG_PATH
    unresolved_path = _write_unresolved(root, unresolved)
    if published or header_only:
        manifest["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_json(manifest_path, manifest)
    if published or _catalog_missing(catalog_path):
        write_catalog(catalog_path, manifest["charts"], repository, now)
    if commit_paths(
        runner,
        root,
        [manifest_path, catalog_path, unresolved_path],
        _commit_message(published, header_only),
    ):
        push_head(runner, root, "main")
        print(f"Manifesto gravado em main ({len(published)} carta(s) nesta execução).", flush=True)


def _asset_file(chart: PreparedChart) -> Path:
    source = Path(chart.zip_path)
    target = source.with_name(asset_name(chart.remote.chart_id))
    if source != target:
        shutil.copyfile(source, target)
    return target


def _catalog_missing(path: Path) -> bool:
    return not path.exists()


def _commit_message(accepted: list[PreparedChart], header_only: list[PreparedChart]) -> str:
    if not accepted and header_only:
        return "Atualiza cabeçalhos HTTP sem mudança de conteúdo"
    ids = ", ".join(chart.remote.chart_id for chart in accepted[:12])
    extra = "" if len(accepted) <= 12 else f" e mais {len(accepted) - 12}"
    if not accepted:
        return "Atualiza o catálogo de cartas raster"
    return f"Publica cartas raster DHN: {ids}{extra}"


def _edition(entry: dict) -> str:
    parts = []
    if entry.get("source_edition") is not None:
        parts.append(f"SE={entry['source_edition']}")
    if entry.get("raster_edition") is not None:
        parts.append(f"RE={entry['raster_edition']}")
    if entry.get("ntm_edition"):
        parts.append(f"NTM={entry['ntm_edition']}")
    return " ".join(parts) or "desconhecida"


def _ensure_identity(runner: SubprocessRunner, root: Path) -> None:
    email = runner.run(["git", "config", "user.email"], cwd=root, check=False)
    if email.code == 0 and email.stdout.strip():
        return
    runner.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=root,
    )
    runner.run(["git", "config", "user.name", "github-actions[bot]"], cwd=root)


def _record_from_entry(entry: dict):
    from cartas_raster.models import IndexRecord

    return IndexRecord(
        chart_id=entry["chart_id"],
        token=entry["chart_id"],
        name=entry.get("name") or entry["title"],
        scale=entry.get("scale"),
        scale_label=None,
        title=entry.get("official_title") or entry["title"],
        uuid=entry.get("uuid") or entry["chart_id"],
        status="published",
        revision_date=None,
        change_date=None,
        page_url=entry.get("page_url"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
