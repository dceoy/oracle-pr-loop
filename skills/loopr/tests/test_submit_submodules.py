"""Regression coverage for submit-time submodule availability checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_submit_command import GIT, ScenarioRunner, _fixture_repo, _git, _run_process

from scripts.models import EXIT_PRECONDITION, LooprError
from scripts.submit_guard import execute_submit


def test_forged_tracking_ref_cannot_publish_an_unpublished_gitlink(
    tmp_path: Path,
) -> None:
    """Local tracking refs cannot prove that a gitlink exists remotely."""
    repo, remote, state, _base, _head = _fixture_repo(tmp_path)

    submodule_remote = tmp_path / "submodule.git"
    submodule_source = tmp_path / "submodule-source"
    _run_process([GIT, "init", "--bare", str(submodule_remote)], cwd=tmp_path)
    _run_process(
        [GIT, "clone", str(submodule_remote), str(submodule_source)],
        cwd=tmp_path,
    )
    _git(submodule_source, "config", "user.name", "Loopr Test")
    _git(submodule_source, "config", "user.email", "loopr@example.invalid")
    (submodule_source / "submodule.txt").write_text(
        "published\n",
        encoding="utf-8",
    )
    _git(submodule_source, "add", "submodule.txt")
    _git(submodule_source, "commit", "-m", "published submodule")
    _git(submodule_source, "branch", "-M", "main")
    _git(submodule_source, "push", "-u", "origin", "main")
    _git(submodule_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    published_submodule_head = _git(submodule_source, "rev-parse", "HEAD")

    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(submodule_remote),
        "vendor/submodule",
    )
    submodule = repo / "vendor/submodule"
    _git(submodule, "config", "user.name", "Loopr Test")
    _git(submodule, "config", "user.email", "loopr@example.invalid")
    _git(repo, "add", ".gitmodules", "vendor/submodule")
    _git(repo, "commit", "-m", "add submodule")
    expected_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", f"{expected_head}:refs/heads/feature")
    state["headRefOid"] = expected_head

    (submodule / "submodule.txt").write_text("unpublished\n", encoding="utf-8")
    _git(submodule, "commit", "-am", "unpublished submodule")
    unpublished_submodule_head = _git(submodule, "rev-parse", "HEAD")
    assert unpublished_submodule_head != published_submodule_head
    _git(
        submodule,
        "update-ref",
        "refs/remotes/origin/main",
        unpublished_submodule_head,
    )
    assert (
        _git(submodule, "rev-parse", "refs/remotes/origin/main")
        == unpublished_submodule_head
    )

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=expected_head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "submodule"
    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == expected_head
    remote_submodule_head = _git(
        submodule,
        "ls-remote",
        str(submodule_remote),
        "refs/heads/main",
    ).split()[0]
    assert remote_submodule_head == published_submodule_head
