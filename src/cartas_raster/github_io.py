"""Thin wrappers around git and the GitHub CLI. Tests pass a fake runner."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cartas_raster.models import OpenPullRequest
from cartas_raster.review_text import parse_marker

RELEASE_TAG = "cartas"
RELEASE_TITLE = "Cartas raster DHN"
RELEASE_NOTES = (
    "Cartas náuticas raster (BSB/KAP) publicadas pela Diretoria de Hidrografia e "
    "Navegação (Marinha do Brasil) e empacotadas para o Chart Downloader do OpenCPN. "
    "Uso não comercial. Os direitos autorais permanecem com a DHN."
)


class CommandError(RuntimeError):
    pass


def require_actions_token() -> None:
    """Fail fast inside Actions when ``gh`` would refuse to start.

    On a GitHub-hosted runner ``gh`` does not pick up ``GITHUB_TOKEN`` by
    itself. The workflow has to set ``GH_TOKEN`` (usually ``${{ github.token }}``).
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    if os.environ.get("GH_TOKEN"):
        return
    raise CommandError(
        "GH_TOKEN não está definido. No GitHub Actions o gh não usa GITHUB_TOKEN "
        "sozinho. O job precisa de env: GH_TOKEN: ${{ github.token }}."
    )


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str


@dataclass
class SubprocessRunner:
    def run(self, args: list[str], *, cwd: Path, check: bool = True) -> CommandResult:
        if args and args[0] == "gh":
            require_actions_token()
        completed = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
        )
        result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if check and result.code != 0:
            raise CommandError(
                f"comando falhou ({result.code}): {' '.join(args)}\n{result.stdout}\n{result.stderr}"
            )
        return result


class GitHub:
    def __init__(self, runner: SubprocessRunner, root: Path, repository: str):
        self.runner = runner
        self.root = root
        self.repository = repository
        self._release_ready = False

    def list_open_prs(self) -> list[OpenPullRequest]:
        raw = self.runner.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository,
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,body,labels",
            ],
            cwd=self.root,
        )
        found: list[OpenPullRequest] = []
        for item in json.loads(raw.stdout or "[]"):
            parsed = parse_marker(item.get("body") or "")
            if not parsed:
                continue
            chart_id, sha256 = parsed
            found.append(OpenPullRequest(number=int(item["number"]), chart_id=chart_id, sha256=sha256))
        return found

    def ensure_labels(self) -> None:
        for name, color, description in (
            ("carta-raster", "1D76DB", "Atualização de uma carta raster da DHN"),
            ("superseded", "B60205", "Substituída por uma edição mais nova"),
        ):
            self.runner.run(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "--repo",
                    self.repository,
                    "--color",
                    color,
                    "--description",
                    description,
                    "--force",
                ],
                cwd=self.root,
            )

    def open_pull_request(self, *, branch: str, title: str, body_file: Path) -> None:
        self.runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                self.repository,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                str(body_file),
                "--label",
                "carta-raster",
            ],
            cwd=self.root,
        )

    def supersede(self, number: int, chart_id: str) -> None:
        self.runner.run(
            [
                "gh",
                "pr",
                "edit",
                str(number),
                "--repo",
                self.repository,
                "--add-label",
                "superseded",
            ],
            cwd=self.root,
        )
        self.runner.run(
            [
                "gh",
                "pr",
                "close",
                str(number),
                "--repo",
                self.repository,
                "--comment",
                (
                    f"Esta proposta da carta {chart_id} foi substituída por uma edição mais nova "
                    "e não conta como rejeição."
                ),
            ],
            cwd=self.root,
        )

    def ensure_release(self) -> None:
        if self._release_ready:
            return
        view = self.runner.run(
            ["gh", "release", "view", RELEASE_TAG, "--repo", self.repository],
            cwd=self.root,
            check=False,
        )
        if view.code != 0:
            self.runner.run(
                [
                    "gh",
                    "release",
                    "create",
                    RELEASE_TAG,
                    "--repo",
                    self.repository,
                    "--title",
                    RELEASE_TITLE,
                    "--notes",
                    RELEASE_NOTES,
                ],
                cwd=self.root,
            )
        self._release_ready = True

    def release_assets(self) -> dict[str, dict]:
        """Name → ``{"size", "sha256"}`` for assets on the ``cartas`` release.

        ``sha256`` comes from the ``digest`` field (``sha256:…``). It is ``None``
        when the API does not send one, and the caller must not treat that as a match.
        """
        self.ensure_release()
        view = self.runner.run(
            [
                "gh",
                "release",
                "view",
                RELEASE_TAG,
                "--repo",
                self.repository,
                "--json",
                "assets",
            ],
            cwd=self.root,
        )
        payload = json.loads(view.stdout or "{}")
        found: dict[str, dict] = {}
        for asset in payload.get("assets") or []:
            name = asset.get("name")
            if not name:
                continue
            found[str(name)] = {
                "size": int(asset.get("size") or 0),
                "sha256": asset_sha256(asset.get("digest")),
            }
        return found

    def release_asset_sizes(self) -> dict[str, int]:
        """Name → size for assets already on the ``cartas`` release."""
        return {name: int(info.get("size") or 0) for name, info in self.release_assets().items()}

    def delete_asset(self, name: str) -> None:
        self.runner.run(
            [
                "gh",
                "release",
                "delete-asset",
                RELEASE_TAG,
                name,
                "--repo",
                self.repository,
                "--yes",
            ],
            cwd=self.root,
        )

    def cleanup_orphans(
        self,
        charts: dict,
        *,
        previous_chart_count: int | None = None,
        max_deletions: int | None = None,
        max_deletion_ratio: float | None = None,
        min_retain_ratio: float | None = None,
    ) -> list[str]:
        """Delete release assets the manifest does not reference.

        Prints the deletion list first. Refuses, without deleting, when any
        manifest entry lacks ``zip_filename``, when the chart count fell below
        the retain ratio, or when the list is longer than the cap.
        """
        from cartas_raster.edition import OrphanCleanupRefused, assert_orphan_cleanup_allowed

        release_names = list(self.release_asset_sizes())
        try:
            names = assert_orphan_cleanup_allowed(
                charts,
                release_names,
                previous_chart_count=previous_chart_count,
                max_deletions=max_deletions,
                max_deletion_ratio=max_deletion_ratio,
                min_retain_ratio=min_retain_ratio,
            )
        except OrphanCleanupRefused as exc:
            print(f"Limpeza de órfãos recusada: {exc}", flush=True)
            raise
        preview = ", ".join(names) if names else "nenhum"
        print(f"Limpeza de órfãos: {len(names)} asset(s) serão removidos: {preview}.", flush=True)
        for name in names:
            print(f"Removendo asset órfão {name}.", flush=True)
            self.delete_asset(name)
        return names

    def upload_asset(self, path: Path) -> None:
        self.ensure_release()
        self.runner.run(
            [
                "gh",
                "release",
                "upload",
                RELEASE_TAG,
                str(path),
                "--repo",
                self.repository,
                "--clobber",
            ],
            cwd=self.root,
        )


def asset_sha256(digest: str | None) -> str | None:
    if not digest or not isinstance(digest, str):
        return None
    hexpart = digest.split(":", 1)[-1].strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", hexpart):
        return hexpart
    return None


def commit_paths(runner: SubprocessRunner, root: Path, paths: list[Path], message: str) -> bool:
    """Commit ``paths`` when they changed. Returns False when there is nothing to commit."""
    relative = [str(path.relative_to(root)) if path.is_absolute() else str(path) for path in paths]
    runner.run(["git", "add", "-A", "--", *relative], cwd=root)
    status = runner.run(["git", "status", "--porcelain", "--", *relative], cwd=root)
    if not status.stdout.strip():
        return False
    runner.run(["git", "commit", "-m", message], cwd=root)
    return True


def push_head(runner: SubprocessRunner, root: Path, branch: str) -> None:
    runner.run(["git", "push", "-u", "origin", branch], cwd=root)
