"""Thin wrappers around git and the GitHub CLI. Tests pass a fake runner."""

from __future__ import annotations

import json
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


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str


@dataclass
class SubprocessRunner:
    def run(self, args: list[str], *, cwd: Path, check: bool = True) -> CommandResult:
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
        view = self.runner.run(
            ["gh", "release", "view", RELEASE_TAG, "--repo", self.repository],
            cwd=self.root,
            check=False,
        )
        if view.code == 0:
            return
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
