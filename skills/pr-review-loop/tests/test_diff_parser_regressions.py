"""Regression tests for frozen-diff parser review feedback."""

from __future__ import annotations

from scripts.github import MAX_GITHUB_FILE_DIFF_BYTES, analyze_frozen_diff

SHA_A = "a" * 40
SHA_B = "b" * 40
NULL_SHA = "0" * 40


def test_unicode_line_separator_cannot_split_a_physical_diff_line() -> None:
    payload = "prefix\u2028diff --git a/fake.py b/fake.py" + (
        "x" * MAX_GITHUB_FILE_DIFF_BYTES
    )
    patch = (
        "diff --git a/file.py b/file.py\n"
        "new file mode 100644\n"
        f"index {NULL_SHA}..{SHA_B} 100644\n"
        "--- /dev/null\n"
        "+++ b/file.py\n"
        "@@ -0,0 +1 @@\n"
        f"+{payload}\n"
    ).encode()

    analysis = analyze_frozen_diff(patch, frozenset({"file.py"}))

    assert analysis.anchors == frozenset()
    assert analysis.files["file.py"].byte_size == len(patch)


def test_unparseable_base_header_fails_closed_for_a_rename() -> None:
    patch = (
        'diff --git "a/old\\tname.py" b/new.py\n'
        f"index {SHA_A}..{SHA_B} 100644\n"
        '--- "a/old\\tname.py"\n'
        "+++ b/new.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    ).encode()

    analysis = analyze_frozen_diff(patch, frozenset({"new.py"}))

    assert analysis.anchors == frozenset()
    assert "new.py" not in analysis.files
