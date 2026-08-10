"""GitHub identity, immutable evidence, and frozen-diff analysis tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
from scripts import github as github_module
from scripts.github import (
    MAX_GITHUB_DIFF_BYTES,
    GitHubClient,
    _bounded_comments,
    analyze_frozen_diff,
    normalize_repo,
    parse_json_object,
    require_boolean,
    require_integer,
    require_object,
    require_string,
    resolve_issue_target,
    resolve_target,
    validate_path,
    validate_ref,
)
from scripts.models import EXIT_GITHUB, EXIT_PRECONDITION, JsonObject, PullRequest, ReviewLoopError
from scripts.process import CommandRunner

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
NULL_SHA = "0" * 40


def sample_pr(*, changed_paths: tuple[str, ...] = ("file.py",)) -> PullRequest:
    return PullRequest(
        repository="owner/repository",
        number=21,
        url="https://github.com/owner/repository/pull/21",
        title="Title",
        body="Body",
        author="author",
        state="OPEN",
        is_draft=False,
        base_ref="main",
        base_sha=SHA_A,
        head_ref="feature",
        head_sha=SHA_B,
        head_repository="owner/repository",
        changed_paths=changed_paths,
    )


def patch_for(
    *,
    old_path: str = "file.py",
    new_path: str = "file.py",
    old_sha: str = SHA_A,
    new_sha: str = SHA_B,
    body: str = " context\n-old\n+new\n",
    hunk: str = "@@ -1,2 +1,2 @@",
) -> bytes:
    return (
        f"diff --git a/{old_path} b/{new_path}\n"
        f"index {old_sha}..{new_sha} 100644\n"
        f"--- a/{old_path}\n"
        f"+++ b/{new_path}\n"
        f"{hunk}\n"
        f"{body}"
    ).encode()


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/owner/repository.git", "owner/repository"),
        ("ssh://git@github.com/owner/repository.git", "owner/repository"),
        ("git@github.com:owner/repository.git", "owner/repository"),
    ],
)
def test_normalize_repo_accepts_unambiguous_github_com_remotes(
    remote: str,
    expected: str,
) -> None:
    assert normalize_repo(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.com/owner/repository.git",
        "https://github.com/owner/repository.git?x=1",
        "https://github.com/owner/repository.git#fragment",
        "https://github.com/owner/repository/extra",
        " https://github.com/owner/repository.git",
        "https://github.com/owner/repository.git\n",
    ],
)
def test_normalize_repo_rejects_ambiguous_or_non_github_remotes(remote: str) -> None:
    with pytest.raises(ReviewLoopError) as captured:
        normalize_repo(remote)

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "repository"


def test_resolve_target_accepts_positive_number_and_canonical_url() -> None:
    assert resolve_target("21", "owner/repository") == (
        "owner/repository",
        21,
        "https://github.com/owner/repository/pull/21",
    )
    assert resolve_target("https://github.com/owner/repository/pull/21", None) == (
        "owner/repository",
        21,
        "https://github.com/owner/repository/pull/21",
    )


def test_resolve_issue_target_is_issue_specific() -> None:
    assert resolve_issue_target("7", "owner/repository") == (
        "owner/repository",
        7,
        "https://github.com/owner/repository/issues/7",
    )
    with pytest.raises(ReviewLoopError):
        resolve_issue_target("https://github.com/owner/repository/pull/7", None)


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "21 ",
        "https://github.com/owner/repository/pull/0",
        "https://github.com/owner/repository/pull/21?x=1",
        "https://github.com/owner/repository/pull/21#x",
        "https://github.com/owner/repository/issues/21",
    ],
)
def test_resolve_target_rejects_noncanonical_or_nonpositive_values(value: str) -> None:
    with pytest.raises(ReviewLoopError):
        resolve_target(value, "owner/repository")


def test_numeric_target_requires_origin() -> None:
    with pytest.raises(ReviewLoopError) as captured:
        resolve_target("21", None)

    assert captured.value.category == "input"


@pytest.mark.parametrize(
    "ref",
    ["", "-danger", ".hidden", "feature..other", "feature@{1}", "a b", "a~b", "x.lock"],
)
def test_validate_ref_rejects_unsafe_git_refs(ref: str) -> None:
    with pytest.raises(ReviewLoopError):
        validate_ref(ref)


def test_validate_ref_accepts_normal_feature_ref() -> None:
    validate_ref("feature/review-loop")


@pytest.mark.parametrize(
    "path",
    ["", "/absolute", "../escape", "dir/../escape", ".git/config", "dir/.GIT/x", "a\\b"],
)
def test_validate_path_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ReviewLoopError):
        validate_path(path)


def test_validate_path_returns_safe_posix_path() -> None:
    assert validate_path("src/file.py") == "src/file.py"


def test_json_object_and_required_field_helpers_are_strict() -> None:
    value = parse_json_object('{"s":"x","i":7,"b":true,"o":{}}', category="schema")

    assert require_string(value["s"], field="s") == "x"
    assert require_integer(value["i"], field="i") == 7
    assert require_boolean(value["b"], field="b") is True
    assert require_object(value["o"], field="o") == {}

    with pytest.raises(ReviewLoopError):
        parse_json_object("[]", category="schema")
    with pytest.raises(ReviewLoopError):
        require_integer(True, field="i")
    with pytest.raises(ReviewLoopError):
        require_boolean(1, field="b")
    with pytest.raises(ReviewLoopError):
        require_string("", field="s")
    with pytest.raises(ReviewLoopError):
        require_object([], field="o")


def test_frozen_diff_analysis_derives_anchors_shas_and_section_metadata() -> None:
    patch = patch_for()
    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.anchors == frozenset({
        ("file.py", "RIGHT", 1),
        ("file.py", "LEFT", 2),
        ("file.py", "RIGHT", 2),
    })
    file_analysis = analysis.files["file.py"]
    assert file_analysis.base_path == "file.py"
    assert file_analysis.old_sha == SHA_A
    assert file_analysis.new_sha == SHA_B
    assert file_analysis.byte_size == len(patch)
    assert file_analysis.line_count == patch.count(b"\n")


def test_frozen_diff_context_line_is_right_only() -> None:
    analysis = analyze_frozen_diff(patch_for(), frozenset({"file.py"}))

    assert ("file.py", "RIGHT", 1) in analysis.anchors
    assert ("file.py", "LEFT", 1) not in analysis.anchors


def test_frozen_diff_rename_maps_head_path_to_base_path() -> None:
    patch = (
        f"diff --git a/old.py b/new.py\n"
        "similarity index 80%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        f"index {SHA_A}..{SHA_B} 100644\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    ).encode()

    analysis = analyze_frozen_diff(patch, frozenset({"new.py"}))

    assert analysis.files["new.py"].base_path == "old.py"
    assert analysis.anchors == frozenset({
        ("new.py", "LEFT", 1),
        ("new.py", "RIGHT", 1),
    })


def test_frozen_diff_deletion_keeps_base_path_for_left_anchor() -> None:
    patch = (
        "diff --git a/file.py b/file.py\n"
        "deleted file mode 100644\n"
        f"index {SHA_A}..{NULL_SHA}\n"
        "--- a/file.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-removed\n"
    ).encode()

    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.files["file.py"].base_path == "file.py"
    assert analysis.files["file.py"].new_sha == NULL_SHA
    assert analysis.anchors == frozenset({("file.py", "LEFT", 1)})


def test_frozen_diff_new_file_uses_null_old_sha_and_right_anchor() -> None:
    patch = (
        "diff --git a/file.py b/file.py\n"
        "new file mode 100644\n"
        f"index {NULL_SHA}..{SHA_B}\n"
        "--- /dev/null\n"
        "+++ b/file.py\n"
        "@@ -0,0 +1 @@\n"
        "+added\n"
    ).encode()

    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.files["file.py"].old_sha == NULL_SHA
    assert analysis.anchors == frozenset({("file.py", "RIGHT", 1)})


@pytest.mark.parametrize(
    "patch",
    [
        patch_for(old_sha="a" * 7),
        patch_for(hunk="@@ -1,1 +1,1 @@", body=" context\n-old\n+new\n"),
        patch_for(body="?malformed\n"),
        (
            f"diff --git a/file.py b/file.py\n"
            f"index {SHA_A}..{SHA_B} 100644\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -1 +1 @@\n"
        ).encode(),
    ],
)
def test_malformed_or_unverifiable_diff_never_proves_anchors(patch: bytes) -> None:
    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.anchors == frozenset()


def test_diff_section_for_path_outside_inventory_is_ignored() -> None:
    analysis = analyze_frozen_diff(patch_for(), frozenset({"other.py"}))

    assert analysis.anchors == frozenset()
    assert analysis.files == {}


def test_duplicate_head_path_sections_fail_closed_for_that_path() -> None:
    patch = patch_for() + patch_for(old_sha=SHA_B, new_sha=SHA_C)
    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.anchors == frozenset()
    assert analysis.files == {}


def test_non_utf8_patch_fails_closed() -> None:
    with pytest.raises(ReviewLoopError) as captured:
        analyze_frozen_diff(b"\xff", frozenset({"file.py"}))

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "bundle"


def test_aggregate_github_truncation_boundary_removes_all_anchors(
    mocker: MockerFixture,
) -> None:
    patch = patch_for()
    mocker.patch.object(github_module, "MAX_GITHUB_DIFF_BYTES", len(patch))

    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.anchors == frozenset()
    assert "file.py" in analysis.files


def test_per_file_github_truncation_boundary_removes_only_oversized_anchors(
    mocker: MockerFixture,
) -> None:
    first = patch_for(old_path="a.py", new_path="a.py")
    second = patch_for(old_path="b.py", new_path="b.py", old_sha=SHA_B, new_sha=SHA_C)
    mocker.patch.object(github_module, "MAX_GITHUB_FILE_DIFF_BYTES", len(first))
    analysis = analyze_frozen_diff(first + second, frozenset({"a.py", "b.py"}))

    assert not any(anchor[0] == "a.py" for anchor in analysis.anchors)
    assert "a.py" in analysis.files
    assert "b.py" in analysis.files


def test_exact_no_newline_marker_does_not_consume_hunk_lines() -> None:
    patch = patch_for(body="-old\n\\ No newline at end of file\n+new\n")
    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.anchors == frozenset({
        ("file.py", "LEFT", 1),
        ("file.py", "RIGHT", 1),
    })


class FrozenDiffClient(GitHubClient):
    def __init__(
        self,
        patch: bytes,
        *,
        forced_paths: frozenset[str] = frozenset(),
        binary_shas: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(CommandRunner({"PATH": "/usr/bin"}), Path("."))
        self._patch = patch
        self._forced_paths = forced_paths
        self._binary_shas = binary_shas
        self.attr_calls: list[tuple[str, frozenset[str]]] = []
        self.binary_calls: list[str] = []

    @override
    def patch(self, _pull_request: PullRequest, *, max_output: int) -> bytes:
        del max_output
        return self._patch

    @override
    def paths_with_diff_unset(
        self,
        sha: str,
        paths: frozenset[str],
        *,
        max_output: int,
    ) -> frozenset[str]:
        del max_output
        self.attr_calls.append((sha, paths))
        return paths & self._forced_paths

    @override
    def blob_is_binary(self, sha: str, *, max_output: int) -> bool:
        del max_output
        self.binary_calls.append(sha)
        return sha in self._binary_shas


def test_diff_anchors_layers_attribute_and_binary_validation_after_parsing() -> None:
    pull_request = sample_pr()
    client = FrozenDiffClient(patch_for())

    anchors = client.diff_anchors(pull_request)

    assert anchors == frozenset({
        ("file.py", "RIGHT", 1),
        ("file.py", "LEFT", 2),
        ("file.py", "RIGHT", 2),
    })
    assert client.attr_calls == [
        (SHA_A, frozenset({"file.py"})),
        (SHA_B, frozenset({"file.py"})),
    ]
    assert set(client.binary_calls) == {SHA_A, SHA_B}


def test_diff_anchors_rejects_attribute_forced_text_path() -> None:
    client = FrozenDiffClient(patch_for(), forced_paths=frozenset({"file.py"}))

    assert client.diff_anchors(sample_pr()) == frozenset()
    assert client.binary_calls == []


def test_diff_anchors_rejects_only_side_backed_by_binary_blob() -> None:
    client = FrozenDiffClient(patch_for(), binary_shas=frozenset({SHA_A}))

    anchors = client.diff_anchors(sample_pr())

    assert ("file.py", "LEFT", 2) not in anchors
    assert ("file.py", "RIGHT", 1) in anchors
    assert ("file.py", "RIGHT", 2) in anchors


def test_diff_anchors_cache_binary_probe_per_blob_sha() -> None:
    client = FrozenDiffClient(patch_for())

    client.diff_anchors(sample_pr())

    assert client.binary_calls.count(SHA_A) == 1
    assert client.binary_calls.count(SHA_B) == 1


def test_review_event_preserves_self_review_comment_semantics() -> None:
    client = GitHubClient(CommandRunner({"PATH": "/usr/bin"}), Path("."))
    client.authenticated_login = "author"

    assert client.review_event(sample_pr(), "APPROVE") == "COMMENT"
    assert client.review_event(sample_pr(), "REQUEST_CHANGES") == "COMMENT"
    client.authenticated_login = "reviewer"
    assert client.review_event(sample_pr(), "APPROVE") == "APPROVE"


def test_review_event_rejects_unknown_verdict() -> None:
    client = GitHubClient(CommandRunner({"PATH": "/usr/bin"}), Path("."))

    with pytest.raises(ReviewLoopError):
        client.review_event(sample_pr(), "COMMENT")


def test_same_snapshot_depends_only_on_frozen_base_and_head() -> None:
    first = sample_pr()
    same = replace(first, title="Changed title")
    changed = replace(first, head_sha=SHA_C)

    assert GitHubClient.same_snapshot(first, same)
    assert not GitHubClient.same_snapshot(first, changed)


def test_bounded_comments_keeps_newest_comments_and_marks_oversized() -> None:
    comments = [
        {
            "author": {"login": f"user-{index}"},
            "body": "x" * (25_000 if index == 31 else 1),
            "createdAt": f"2026-01-{index:02d}T00:00:00Z",
        }
        for index in range(1, 32)
    ]

    result = _bounded_comments(comments)

    assert len(result) == 30
    assert result[0]["created_at"] == "2026-01-02T00:00:00Z"
    assert result[-1]["created_at"] == "2026-01-31T00:00:00Z"
    assert result[-1]["omitted"] is True
    assert result[-1]["body"] == ""


def test_bounded_comments_rejects_malformed_comment_entry() -> None:
    with pytest.raises(ReviewLoopError) as captured:
        _bounded_comments(["bad"])

    assert captured.value.code == EXIT_GITHUB


def test_parse_json_object_preserves_exact_json_values() -> None:
    value = parse_json_object(json.dumps({"path": " a.py ", "number": 1}), category="x")

    assert value == {"path": " a.py ", "number": 1}


def test_original_diff_limit_constant_remains_larger_than_minimal_patch() -> None:
    assert MAX_GITHUB_DIFF_BYTES > len(patch_for())
