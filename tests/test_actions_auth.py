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


def _run_scripts(text: str) -> list[str]:
    import re

    scripts: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^(\s+)run:\s*(\|)?\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        indent = len(match.group(1))
        if match.group(2):
            index += 1
            body: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                    break
                body.append(line)
                index += 1
            scripts.append("\n".join(body))
        else:
            scripts.append(match.group(3))
            index += 1
    return scripts


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
        assert "pages: write" in jobs["deploy"]
        assert "id-token: write" in jobs["deploy"]
        for name, body in jobs.items():
            if name == "deploy":
                continue
            assert "id-token: write" not in body, f"{workflow} job {name} não precisa de id-token"


def test_run_scripts_do_not_interpolate_expressions():
    for name in ("update.yml", "review.yml", "ci.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for script in _run_scripts(text):
            assert "${{" not in script, name


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
