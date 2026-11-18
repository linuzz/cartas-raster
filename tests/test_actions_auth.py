"""The Actions failure mode: gh exits unless the workflow sets GH_TOKEN."""

from pathlib import Path

import pytest

from cartas_raster.github_io import CommandError, require_actions_token

ROOT = Path(__file__).resolve().parents[1]
TOKEN_LINE = "GH_TOKEN: ${{ github.token }}"


def _jobs(text: str) -> dict[str, str]:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == "jobs:")
    jobs: dict[str, str] = {}
    name = None
    buffer: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            if name is not None:
                jobs[name] = "\n".join(buffer)
            name = line.strip()[:-1]
            buffer = [line]
        elif name is not None:
            buffer.append(line)
    if name is not None:
        jobs[name] = "\n".join(buffer)
    return jobs


def _uses_gh(body: str) -> bool:
    return "cartas_raster.cli" in body or "gh " in body


def test_jobs_that_call_gh_set_the_token_and_can_write():
    for workflow in ("update.yml", "review.yml"):
        jobs = _jobs((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
        callers = [name for name, body in jobs.items() if _uses_gh(body)]
        assert callers, workflow
        for name in callers:
            body = jobs[name]
            assert TOKEN_LINE in body, f"{workflow} job {name} não define {TOKEN_LINE}"
            assert "contents: write" in body, f"{workflow} job {name} não pode gravar o repositório"


def test_deploy_jobs_do_not_need_gh():
    for workflow in ("update.yml", "review.yml"):
        jobs = _jobs((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
        assert "deploy" in jobs
        assert not _uses_gh(jobs["deploy"])
        assert "pages: write" in (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert "id-token: write" in (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")


def test_actions_without_gh_token_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "present-but-not-enough")
    with pytest.raises(CommandError, match="GH_TOKEN"):
        require_actions_token()


def test_actions_with_gh_token_passes(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GH_TOKEN", "token")
    require_actions_token()


def test_local_run_does_not_require_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    require_actions_token()


def test_real_gh_command_is_refused_without_the_token(monkeypatch, tmp_path: Path):
    from cartas_raster.github_io import SubprocessRunner

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(CommandError, match="GH_TOKEN"):
        SubprocessRunner().run(["gh", "release", "view", "cartas"], cwd=tmp_path)
