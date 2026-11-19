"""Command line used by GitHub Actions and by a local dry run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from cartas_raster.catalog import asset_name, write_pages_index
from cartas_raster.chartdldr import assert_plugin_compatible
from cartas_raster.dhn import DhnCatalog, RASTER_BASE, extra_suffix_names, variant_stem
from cartas_raster.edition import (
    EditionView,
    bsb_number_matches,
    choose_newest,
    is_newer,
    manifest_has_same_bytes,
    parse_http_datetime,
    retire_absorbed,
    view_from_bsb,
    view_from_manifest,
)
from cartas_raster.paper_catalogue import (
    SKIPPED_COMPARISON,
    cache_path,
    edition_gaps,
    edition_year,
    refresh_paper_catalogue,
)
from cartas_raster.errors import SourceLayoutError
from cartas_raster.github_io import GitHub, SubprocessRunner, commit_paths, push_head
from cartas_raster.http_client import UrllibTransport
from cartas_raster.models import PreparedChart, RemoteFile, Unresolved
from cartas_raster.pipeline import ChartPackageError, manifest_entry, package_chart, write_catalog
from cartas_raster.proposal import (
    confined_zip_path,
    materialize_verified_zip,
    require_chart_id,
    require_known_charts,
    require_raster_url,
    require_sha256,
    require_zip_name,
)
from cartas_raster.plan import select_for_publish, select_for_review
from cartas_raster.review_text import proposal_document, pull_request_body, pull_request_title
from cartas_raster.store import (
    CATALOG_PATH,
    EDITION_GAPS_PATH,
    PROPOSALS_DIR,
    UNRESOLVED_PATH,
    VARIANT_CACHE_PATH,
    load_variant_cache,
    add_rejection,
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

    orphans = sub.add_parser("delete-orphans", help="Remove assets da Release que o manifesto não cita")
    orphans.add_argument("--root", type=Path, default=Path.cwd())
    orphans.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))

    pages = sub.add_parser("render-pages", help="Gera a página e o XML a partir do manifesto local")
    pages.add_argument("--root", type=Path, default=Path.cwd())
    pages.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))

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
        if args.command == "delete-orphans":
            return delete_orphans(args)
        if args.command == "render-pages":
            return render_pages(args)
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
        f"{len(discovery.charts)} cartas com ZIP; "
        f"{len(discovery.unresolved)} sem arquivo; "
        f"{discovery.head_requests} HEADs de variantes."
    )
    ordered = _prioritize_by_change_date(discovery.charts, manifest)
    paper_years, paper_warning = refresh_paper_catalogue(
        catalog.transport, discovery.catalogue_pdf_url, cache_path(root)
    )
    if paper_warning:
        print(f"AVISO: {paper_warning}", flush=True)
    else:
        print(f"Catálogo em papel: {len(paper_years)} cartas em {discovery.catalogue_pdf_url}.", flush=True)
    download_dir = args.download_dir or (root / "run" / "downloads")
    variant_cache = load_variant_cache(root)
    nu_warnings: list[str] = []
    prepared, failures, retired = _select_editions(
        catalog.transport,
        ordered,
        manifest,
        download_dir,
        paper_years,
        variant_cache=variant_cache,
        nu_warnings=nu_warnings,
    )
    if not args.dry_run:
        save_json(root / VARIANT_CACHE_PATH, variant_cache)
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
    _stamp_change_dates(manifest, ordered)
    compared = bool(paper_years)
    gaps = _gaps_for_run(manifest, content, paper_years) if compared else []
    _write_gaps(root, gaps, paper_warning, discovery.catalogue_pdf_url, compared)
    indexed = {group.record.chart_id for group in discovery.charts}
    indexed.update(item.chart_id for item in discovery.unresolved if item.chart_id and item.chart_id != "?")
    absent = sorted(chart_id for chart_id in manifest.get("charts", {}) if chart_id not in indexed)
    for chart_id, entry in manifest.get("charts", {}).items():
        _note_bsb_number(nu_warnings, chart_id, entry.get("bsb_number"))
    transient = [item for item in discovery.unresolved if str(item.reason).startswith("erro temporário")]
    _write_step_summary(
        gaps,
        paper_warning,
        len(paper_years),
        compared,
        absent=absent,
        transient=transient,
        nu_warnings=nu_warnings,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    unresolved = list(discovery.unresolved)
    for failure in failures:
        unresolved.append(Unresolved(chart_id="?", title=failure, reason="falha ao ler o ZIP"))
    if args.mode == "auto":
        return _apply_auto(
            args, root, manifest, rejected, content, header_only, unresolved, now, retired
        )
    return _apply_review(
        args, root, manifest, rejected, content, header_only, unresolved, now, retired
    )


def publish_merged(args) -> int:
    root: Path = args.root.resolve()
    document = json.loads(args.proposal.read_text(encoding="utf-8"))
    proposed = document["proposed"]
    chart_id = require_chart_id(str(proposed["chart_id"]))
    zip_name = require_zip_name(str(proposed.get("zip_name") or ""))
    source_url = require_raster_url(str(proposed["source_url"]), zip_name)
    expected_sha = require_sha256(str(proposed["sha256"]))
    if proposed.get("zip_filename") != asset_name(chart_id):
        raise ChartPackageError(f"{chart_id}: zip_filename deve ser {asset_name(chart_id)}")
    manifest = load_manifest(root)
    retire_ids = require_known_charts(list(document.get("retire_chart_ids") or []), manifest["charts"])
    previous_chart_count = len(manifest["charts"])
    print(f"Publicando carta {chart_id} a partir de {args.proposal}…")
    if not args.dry_run:
        projected = {key: value for key, value in manifest["charts"].items()}
        projected[chart_id] = proposed
        for retired_id in retire_ids:
            projected.pop(retired_id, None)
        _preflight_orphan_cleanup(
            GitHub(SubprocessRunner(), root, args.repository), projected, previous_chart_count
        )
    transport = UrllibTransport()
    result = transport.get(source_url)
    if result.status != 200:
        raise ChartPackageError(f"{chart_id}: a DHN respondeu HTTP {result.status} ao enviar o ZIP de novo")
    download_dir = root / "run" / "downloads"
    raw_path = materialize_verified_zip(download_dir, zip_name, result.body, expected_sha)
    remote = RemoteFile(
        chart_id=chart_id,
        record=_record_from_entry(proposed),
        source_url=source_url,
        zip_name=zip_name,
        content_length=len(result.body),
        last_modified=proposed.get("last_modified"),
        etag=proposed.get("etag"),
    )
    packaged = package_chart(remote, raw_path)
    if packaged.sha256 != expected_sha:
        raise ChartPackageError(
            f"{chart_id}: o ZIP na DHN mudou desde a aprovação "
            f"(sha256 {packaged.sha256}, proposta {expected_sha}). "
            "Nada foi publicado; rode o workflow de atualização para abrir uma proposta nova."
        )
    if args.dry_run:
        print(f"Dry-run: SHA-256 confere para a carta {chart_id}. Nenhum upload nem commit.")
        return 0
    asset = confined_zip_path(download_dir, asset_name(chart_id))
    shutil.copyfile(raw_path, asset)
    GitHub(SubprocessRunner(), root, args.repository).upload_asset(asset)
    manifest["charts"][chart_id] = proposed
    for retired_id in retire_ids:
        removed = manifest["charts"].pop(retired_id, None)
        if removed:
            print(f"Remove a entrada duplicada da carta {retired_id}.")
    manifest["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    catalog_path = root / CATALOG_PATH
    checked = datetime.now(timezone.utc)
    write_catalog(
        catalog_path,
        manifest["charts"],
        args.repository,
        checked,
    )
    write_pages_index(root / "docs" / "index.html", manifest["charts"], checked)
    save_json(root / "data" / "manifest.json", manifest)
    proposal_path = root / PROPOSALS_DIR / f"{chart_id}.json"
    if proposal_path.exists():
        proposal_path.unlink()
    runner = SubprocessRunner()
    _ensure_identity(runner, root)
    committed = commit_paths(
        runner,
        root,
        [root / "data" / "manifest.json", catalog_path, root / "docs" / "index.html", proposal_path],
        f"Publica carta {chart_id}",
    )
    if committed:
        push_head(runner, root, "main")
    removed = _finish_orphan_cleanup(
        GitHub(runner, root, args.repository),
        manifest["charts"],
        previous_chart_count,
    )
    if removed:
        print(f"Assets órfãos removidos: {', '.join(removed)}")
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


def _preflight_orphan_cleanup(github: GitHub, charts: dict, previous_chart_count: int) -> None:
    """Print the deletion list and refuse before any asset is removed."""
    from cartas_raster.edition import OrphanCleanupRefused, assert_orphan_cleanup_allowed

    try:
        names = assert_orphan_cleanup_allowed(
            charts,
            list(github.release_asset_sizes()),
            previous_chart_count=previous_chart_count,
        )
    except OrphanCleanupRefused as exc:
        print(f"Limpeza de órfãos recusada: {exc}", flush=True)
        raise
    preview = ", ".join(names) if names else "nenhum"
    print(f"Limpeza de órfãos prevista: {len(names)} asset(s): {preview}.", flush=True)


def render_pages(args) -> int:
    """Rebuild the Pages files from the manifest already in the repository.

    Does not contact the DHN, change the release, or delete assets.
    """
    root: Path = args.root.resolve()
    manifest = load_manifest(root)
    checked = _pages_checked_at(root)
    write_catalog(root / CATALOG_PATH, manifest["charts"], args.repository, checked)
    write_pages_index(root / "docs" / "index.html", manifest["charts"], checked)
    print(
        f"Página do catálogo gerada com {len(manifest.get('charts', {}))} cartas, "
        "só a partir do manifesto local.",
        flush=True,
    )
    return 0


def _pages_checked_at(root: Path) -> datetime:
    path = root / "data" / "last_checked.txt"
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def delete_orphans(args) -> int:
    """Remove release assets the committed manifest no longer cites.

    Used after GitHub Pages has published the catalogue that dropped those charts.
    """
    root: Path = args.root.resolve()
    manifest = load_manifest(root)
    removed = GitHub(SubprocessRunner(), root, args.repository).cleanup_orphans(manifest["charts"])
    if removed:
        print(f"Assets órfãos removidos: {', '.join(removed)}")
    else:
        print("Nenhum asset órfão.")
    return 0


def _finish_orphan_cleanup(github: GitHub, charts: dict, previous_chart_count: int) -> list[str]:
    if os.environ.get("DEFER_ASSET_DELETION") == "1":
        print(
            "Exclusão de órfãos adiada até o catálogo novo ser publicado no Pages.",
            flush=True,
        )
        return []
    return github.cleanup_orphans(charts, previous_chart_count=previous_chart_count)


def _apply_auto(args, root, manifest, rejected, content, header_only, unresolved, now, retired: list[str]) -> int:
    accepted, skipped = select_for_publish(content, rejected)
    for chart_id in skipped:
        print(f"Mantida rejeição da carta {chart_id}.")
    if args.dry_run:
        for chart in accepted:
            entry = manifest_entry(chart)
            print(f"Publicaria {entry['chart_id']} ({entry['title']}) edição {_edition(entry)} tamanho {entry['zip_size']}")
        print(f"Dry-run: {len(accepted)} carta(s), sem upload e sem commit.")
        return 0
    previous_chart_count = len(manifest.get("charts") or {})
    runner = SubprocessRunner()
    _ensure_identity(runner, root)
    if accepted or retired:
        projected = {key: value for key, value in manifest["charts"].items() if key not in set(retired)}
        for chart in accepted:
            projected[chart.remote.chart_id] = {"zip_filename": asset_name(chart.remote.chart_id)}
        _preflight_orphan_cleanup(
            GitHub(runner, root, args.repository), projected, previous_chart_count
        )
    _apply_header_updates(manifest, header_only)
    for chart_id in retired:
        if manifest["charts"].pop(chart_id, None):
            print(f"A carta {chart_id} usava um ZIP que agora é edição de outra carta.")
    published: list[PreparedChart] = []
    failures: list[str] = []

    removed_charts = bool(retired)

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
            rewrite_catalog=removed_charts or bool(published),
        )

    github = GitHub(runner, root, args.repository)
    try:
        if accepted:
            failures = upload_new_assets(accepted, manifest, github, published, persist)
    finally:
        persist()
    heal_release_digests(root, manifest, github, UrllibTransport())
    if published or retired:
        _finish_orphan_cleanup(github, manifest["charts"], previous_chart_count)
    if failures:
        print(f"{len(published)} carta(s) ficaram no manifesto. Rode de novo para continuar.", flush=True)
        for line in failures:
            print(line, flush=True)
        return 1
    return 0


def _apply_review(args, root, manifest, rejected, content, header_only, unresolved, now, retired: list[str]) -> int:
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
    pages_index = root / "docs" / "index.html"
    write_pages_index(pages_index, manifest["charts"], now)
    cache_file = root / VARIANT_CACHE_PATH
    review_paths = [manifest_path, unresolved_path, pages_index]
    if cache_file.exists():
        review_paths.append(cache_file)
    if header_only and commit_paths(
        runner,
        root,
        review_paths,
        "Atualiza cabeçalhos HTTP sem mudança de conteúdo",
    ):
        push_head(runner, root, "main")
    elif commit_paths(
        runner,
        root,
        [unresolved_path, pages_index, *([cache_file] if cache_file.exists() else [])],
        "Atualiza a lista de cartas sem ZIP",
    ):
        push_head(runner, root, "main")
    if not decision.opens and not decision.supersede:
        return 0
    github.ensure_labels()
    for pr in decision.supersede:
        print(f"Fechando PR #{pr.number} da carta {pr.chart_id} (edição substituída).")
        github.supersede(pr.number, pr.chart_id)
    for chart in decision.opens:
        absorbed = _retired_for(chart, retired, manifest)
        _open_one_pr(args, root, runner, github, manifest, chart, absorbed)
    runner.run(["git", "checkout", "main"], cwd=root)
    return 0


def _open_one_pr(args, root: Path, runner: SubprocessRunner, github: GitHub, manifest: dict, chart: PreparedChart, retire: list[str] | None = None) -> None:
    entry = manifest_entry(chart)
    previous = manifest["charts"].get(chart.remote.chart_id)
    branch = f"review/{chart.remote.chart_id}-{chart.sha256[:12]}"
    print(f"Abrindo PR da carta {entry['chart_id']} na branch {branch}…")
    runner.run(["git", "checkout", "-B", branch, "main"], cwd=root)
    proposal_path = root / PROPOSALS_DIR / f"{chart.remote.chart_id}.json"
    save_json(proposal_path, proposal_document(entry, previous, retire or None))
    body_path = root / "run" / f"{chart.remote.chart_id}.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(
        pull_request_body(entry, previous, args.repository, retire or None),
        encoding="utf-8",
    )
    commit_paths(runner, root, [proposal_path], pull_request_title(entry))
    push_head(runner, root, branch)
    github.open_pull_request(branch=branch, title=pull_request_title(entry), body_file=body_path)


def _retired_for(chart: PreparedChart, retired: list[str], manifest: dict) -> list[str]:
    url = chart.remote.source_url
    return [
        chart_id
        for chart_id in retired
        if manifest.get("charts", {}).get(chart_id, {}).get("source_url") == url
    ]


def _prioritize_by_change_date(charts, manifest: dict):
    stored = manifest.get("charts") or {}

    def rank(group):
        chart_id = group.record.chart_id
        current = group.record.change_date or ""
        previous = (stored.get(chart_id) or {}).get("change_date") or ""
        changed = bool(current) and current != previous
        if changed:
            print(
                f"Carta {chart_id} reavaliada: changeDate do GeoNetwork "
                f"{previous or 'ausente'} → {current}. As variantes seguem sendo consultadas.",
                flush=True,
            )
        return (0 if changed else 1, chart_id)

    return sorted(charts, key=rank)


def _stamp_change_dates(manifest: dict, charts) -> None:
    for group in charts:
        entry = manifest.get("charts", {}).get(group.record.chart_id)
        if entry is not None and group.record.change_date:
            entry["change_date"] = group.record.change_date


def _gaps_for_run(manifest: dict, content: list[PreparedChart], paper_years: dict) -> list[dict]:
    overlay = {chart_id: dict(entry) for chart_id, entry in manifest.get("charts", {}).items()}
    for chart in content:
        overlay[chart.remote.chart_id] = manifest_entry(chart)
    return edition_gaps(overlay, paper_years)


def _write_gaps(root: Path, gaps: list[dict], warning: str | None, url: str | None, compared: bool) -> None:
    save_json(
        root / EDITION_GAPS_PATH,
        {
            "compared": compared,
            "available": compared and warning is None,
            "catalogue_url": url,
            "warning": None if compared else SKIPPED_COMPARISON,
            "detail": warning,
            "charts": gaps if compared else [],
        },
    )


def _write_step_summary(
    gaps: list[dict],
    warning: str | None,
    paper_count: int,
    compared: bool,
    *,
    absent: list[str] | None = None,
    transient: list | None = None,
    nu_warnings: list[str] | None = None,
) -> None:
    lines = ["## Catálogo em papel da DHN", ""]
    if not compared:
        lines.append(SKIPPED_COMPARISON)
        if warning:
            lines.append(f"Aviso: {warning}")
    else:
        if warning:
            lines.append(f"Aviso: {warning}")
        else:
            lines.append(f"Catálogo lido com {paper_count} cartas.")
        lines.append(
            f"Raster mais antigo que a última edição em papel: {len(gaps)}. "
            "Essas cartas continuam publicadas."
        )
    if compared:
        for gap in gaps[:40]:
            lines.append(
                f"- {gap['chart_id']}: raster {gap['zip_year']} ({gap.get('zip_name')}), "
                f"papel {gap['catalogue_year']}"
            )
        if len(gaps) > 40:
            lines.append(f"- … e mais {len(gaps) - 40}. Lista completa em data/edition_gaps.json.")
    if absent:
        lines.extend(["", "## Possível cancelamento", ""])
        lines.append(
            "Estas cartas publicadas não apareceram no índice da DHN nesta execução. "
            "Elas continuam no catálogo."
        )
        lines.extend(f"- {chart_id}" for chart_id in absent[:40])
        if len(absent) > 40:
            lines.append(f"- … e mais {len(absent) - 40}.")
    if nu_warnings:
        lines.extend(["", "## Número do BSB diferente do identificador", ""])
        lines.append("A carta continua publicada. Confira o cabeçalho CHT/NU.")
        lines.extend(f"- {item}" for item in nu_warnings[:40])
        if len(nu_warnings) > 40:
            lines.append(f"- … e mais {len(nu_warnings) - 40}.")
    if transient:
        lines.extend(["", "## Erros temporários na DHN", ""])
        lines.append("A versão já publicada foi mantida.")
        for item in transient[:40]:
            lines.append(f"- {item.chart_id}: {item.reason}")
        if len(transient) > 40:
            lines.append(f"- … e mais {len(transient) - 40}.")
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)


def _select_editions(
    transport,
    charts,
    manifest: dict,
    download_dir: Path,
    paper_years: dict | None = None,
    variant_cache: dict | None = None,
    nu_warnings: list[str] | None = None,
):
    """Download variants that differ from the manifest and keep the newest edition."""
    download_dir.mkdir(parents=True, exist_ok=True)
    stored = manifest.get("charts", {})
    prepared: list[PreparedChart] = []
    failures: list[str] = []
    chosen_urls: dict[str, str] = {}
    considered: dict[str, str] = {}
    if variant_cache is None:
        variant_cache = {"version": 1, "charts": {}}
    variant_cache.setdefault("charts", {})
    notes = nu_warnings if nu_warnings is not None else []
    for group in charts:
        chart_id = group.record.chart_id
        previous = stored.get(chart_id)
        chart_cache = variant_cache["charts"].setdefault(chart_id, {})
        if not isinstance(chart_cache, dict):
            chart_cache = {}
            variant_cache["charts"][chart_id] = chart_cache
        for remote in group.variants:
            considered[remote.source_url] = chart_id
        options = []
        downloaded: dict[str, PreparedChart] = {}

        def fetch(remote: RemoteFile) -> PreparedChart | None:
            destination = download_dir / f"{chart_id}-{remote.zip_name}"
            try:
                result = transport.get(remote.source_url)
                if result.status != 200:
                    raise ChartPackageError(
                        f"{chart_id}: HTTP {result.status} ao baixar {remote.source_url}"
                    )
                destination.write_bytes(result.body)
                return package_chart(remote, destination)
            except (ChartPackageError, OSError) as exc:
                failures.append(str(exc))
                return None

        for remote in group.variants:
            cached = chart_cache.get(remote.zip_name) if isinstance(chart_cache, dict) else None
            if _variant_unchanged(cached, remote):
                options.append((remote, _view_from_variant(cached)))
                _note_bsb_number(notes, chart_id, cached.get("bsb_number"))
                continue
            if manifest_has_same_bytes(previous, remote.zip_name, remote.content_length, remote.last_modified):
                options.append((remote, view_from_manifest(previous)))
                chart_cache[remote.zip_name] = _variant_record(remote, previous)
                _note_bsb_number(notes, chart_id, previous.get("bsb_number"))
                continue
            packaged = fetch(remote)
            if packaged is None:
                continue
            downloaded[remote.source_url] = packaged
            chart_cache[remote.zip_name] = _variant_record(remote, manifest_entry(packaged))
            options.append((remote, view_from_bsb(packaged.bsb, packaged.zip_datetime)))
            _note_bsb_number(notes, chart_id, packaged.bsb.number)
        if not options:
            continue
        best_remote, best_view = choose_newest(options)
        best_remote, best_view = _extend_if_paper_is_newer(
            transport,
            group,
            options,
            downloaded,
            best_remote,
            best_view,
            paper_years or {},
            download_dir,
        )
        if previous is not None and not is_newer(best_view, view_from_manifest(previous)):
            print(
                f"Carta {chart_id}: a edição publicada não é mais antiga que "
                f"{best_remote.zip_name}. Sem rebaixamento.",
                flush=True,
            )
            if previous.get("source_url"):
                chosen_urls.setdefault(previous["source_url"], chart_id)
            continue
        chosen_urls[best_remote.source_url] = chart_id
        packaged = downloaded.get(best_remote.source_url)
        if packaged is None:
            packaged = fetch(best_remote)
            if packaged is not None:
                chart_cache[best_remote.zip_name] = _variant_record(best_remote, manifest_entry(packaged))
                _note_bsb_number(notes, chart_id, packaged.bsb.number)
        if packaged is None:
            continue
        prepared.append(packaged)
    retired = retire_absorbed(stored, chosen_urls, considered)
    return prepared, failures, retired


def _extend_if_paper_is_newer(transport, group, options, downloaded, best_remote, best_view, paper_years, download_dir):
    """When the paper catalogue is newer, look past the first missing suffix. Never drops the chart."""
    paper = paper_years.get(group.record.chart_id)
    zip_year = edition_year(best_view.source_date)
    if paper is None or zip_year is None or zip_year >= paper or not hasattr(transport, "head"):
        return best_remote, best_view
    stem = variant_stem(group.record.token)
    names = extra_suffix_names(stem, [remote.zip_name for remote, _view in options])
    print(
        f"Carta {group.record.chart_id}: raster {zip_year} anterior ao catálogo em papel {paper}. "
        f"Conferindo também {', '.join(names)}.",
        flush=True,
    )
    for name in names:
        remote = _remote_from_head(transport, group.record, name)
        if remote is None:
            continue
        destination = download_dir / f"{group.record.chart_id}-{name}"
        try:
            result = transport.get(remote.source_url)
            if result.status != 200:
                continue
            destination.write_bytes(result.body)
            packaged = package_chart(remote, destination)
        except (ChartPackageError, OSError) as exc:
            print(f"AVISO: {exc}", flush=True)
            continue
        downloaded[remote.source_url] = packaged
        options.append((remote, view_from_bsb(packaged.bsb, packaged.zip_datetime)))
    return choose_newest(options)


def _remote_from_head(transport, record, zip_name: str) -> RemoteFile | None:
    result = transport.head(RASTER_BASE + zip_name)
    if getattr(result, "status", 0) != 200:
        return None
    headers = getattr(result, "headers", {}) or {}
    content_type = headers.get("content-type", "")
    if "zip" not in content_type.lower() and "octet-stream" not in content_type.lower():
        return None
    try:
        length = int(headers.get("content-length") or 0)
    except (TypeError, ValueError):
        return None
    if length <= 0:
        return None
    return RemoteFile(
        chart_id=record.chart_id,
        record=record,
        source_url=RASTER_BASE + zip_name,
        zip_name=zip_name,
        content_length=length,
        last_modified=headers.get("last-modified"),
        etag=headers.get("etag"),
    )


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

    An asset already on the release with the same SHA-256 is not uploaded again.
    The same byte size with a different digest is uploaded. A failure on one
    chart does not discard charts already stored in ``manifest``.
    """
    failures: list[str] = []
    assets = github.release_assets()
    since_commit = 0
    for chart in accepted:
        asset = _asset_file(chart)
        chart_id = chart.remote.chart_id
        try:
            remote_sha = (assets.get(asset.name) or {}).get("sha256")
            if remote_sha and remote_sha == chart.sha256:
                print(
                    f"{asset.name} já está na release (sha256 confere); sem novo upload.",
                    flush=True,
                )
            else:
                print(f"Enviando {asset.name}…", flush=True)
                github.upload_asset(asset)
                assets[asset.name] = {"size": asset.stat().st_size, "sha256": chart.sha256}
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


def _persist_publication(
    runner,
    root,
    manifest,
    published,
    header_only,
    unresolved,
    repository,
    now,
    rewrite_catalog: bool = False,
) -> None:
    manifest_path = root / "data" / "manifest.json"
    catalog_path = root / CATALOG_PATH
    unresolved_path = _write_unresolved(root, unresolved)
    if published or header_only or rewrite_catalog:
        manifest["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    save_json(manifest_path, manifest)
    if published or rewrite_catalog or _catalog_missing(catalog_path):
        write_catalog(catalog_path, manifest["charts"], repository, now)
    pages_index = root / "docs" / "index.html"
    write_pages_index(pages_index, manifest["charts"], now)
    extra = [
        path
        for path in (root / EDITION_GAPS_PATH, cache_path(root), root / VARIANT_CACHE_PATH)
        if path.exists()
    ]
    if commit_paths(
        runner,
        root,
        [manifest_path, catalog_path, pages_index, unresolved_path, *extra],
        _commit_message(published, header_only, removed=rewrite_catalog and not published),
    ):
        push_head(runner, root, "main")
        print(f"Manifesto gravado em main ({len(published)} carta(s) nesta execução).", flush=True)


def heal_release_digests(root: Path, manifest: dict, github: GitHub, transport) -> None:
    """Re-upload release assets whose digest is not the SHA-256 stored in the manifest."""
    assets = github.release_assets()
    download_dir = root / "run" / "downloads"
    for chart_id, entry in sorted(manifest.get("charts", {}).items()):
        name = entry.get("zip_filename")
        expected = str(entry.get("sha256") or "").lower()
        zip_name = str(entry.get("zip_name") or "")
        source = str(entry.get("source_url") or "")
        if not name or not expected:
            continue
        remote_sha = (assets.get(name) or {}).get("sha256")
        if remote_sha and remote_sha == expected:
            continue
        try:
            require_raster_url(source, zip_name)
        except ChartPackageError as exc:
            print(f"AVISO: {chart_id}: não reenvio o asset ({exc}).", flush=True)
            continue
        print(f"Enviando de novo {name}: o SHA-256 da Release difere do manifesto.", flush=True)
        try:
            result = transport.get(source)
            if getattr(result, "status", 0) != 200:
                print(f"AVISO: {chart_id}: HTTP {getattr(result, 'status', '?')} ao enviar o ZIP de novo.", flush=True)
                continue
            body = result.body
            if hashlib.sha256(body).hexdigest() != expected:
                print(
                    f"AVISO: {chart_id}: o ZIP atual da DHN não confere com o manifesto. Sem upload.",
                    flush=True,
                )
                continue
            raw_path = materialize_verified_zip(download_dir, zip_name, body, expected)
            asset = confined_zip_path(download_dir, name)
            if asset != raw_path:
                shutil.copyfile(raw_path, asset)
            github.upload_asset(asset)
        except (ChartPackageError, OSError) as exc:
            print(f"AVISO: {chart_id}: {exc}", flush=True)


def _variant_unchanged(cached: dict | None, remote: RemoteFile) -> bool:
    if not isinstance(cached, dict) or not cached.get("sha256"):
        return False
    return (
        cached.get("content_length") == remote.content_length
        and (cached.get("last_modified") or None) == (remote.last_modified or None)
        and (cached.get("etag") or None) == (remote.etag or None)
    )


def _view_from_variant(cached: dict) -> EditionView:
    edition = cached.get("source_edition")
    return EditionView(
        source_edition=edition if isinstance(edition, int) else None,
        source_date=cached.get("source_date"),
        ntm_edition=cached.get("ntm_edition"),
        ntm_date=cached.get("ntm_date"),
        last_modified=parse_http_datetime(cached.get("last_modified")),
    )


def _variant_record(remote: RemoteFile, entry: dict) -> dict:
    return {
        "content_length": remote.content_length,
        "last_modified": remote.last_modified,
        "etag": remote.etag,
        "sha256": entry.get("sha256"),
        "source_edition": entry.get("source_edition"),
        "source_date": entry.get("source_date"),
        "ntm_edition": entry.get("ntm_edition"),
        "ntm_date": entry.get("ntm_date"),
        "bsb_number": entry.get("bsb_number"),
    }


def _note_bsb_number(warnings: list[str], chart_id: str, number) -> None:
    if bsb_number_matches(chart_id, number):
        return
    text = f"{chart_id} (BSB NU={number})"
    if text not in warnings:
        warnings.append(text)


def _asset_file(chart: PreparedChart) -> Path:
    source = Path(chart.zip_path)
    target = source.with_name(asset_name(chart.remote.chart_id))
    if source != target:
        shutil.copyfile(source, target)
    return target


def _catalog_missing(path: Path) -> bool:
    return not path.exists()


def _commit_message(accepted: list[PreparedChart], header_only: list[PreparedChart], removed: bool = False) -> str:
    if not accepted and removed:
        return "Atualiza o catálogo após retirar cartas duplicadas"
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
