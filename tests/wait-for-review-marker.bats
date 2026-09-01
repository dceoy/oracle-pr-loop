#!/usr/bin/env bats

setup() {
  repo_root="$(git -C "${BATS_TEST_DIRNAME}" rev-parse --show-toplevel)"
  recovery_script="${repo_root}/skills/oracle-pr-review/scripts/wait-for-review-marker.sh"
  fake_bin="${BATS_TEST_TMPDIR}/bin"
  mkdir -p "${fake_bin}"

  cat > "${fake_bin}/sleep" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
count=0
if [ -f "${SLEEP_COUNT_FILE}" ]; then
  read -r count < "${SLEEP_COUNT_FILE}"
fi
printf '%d\n' "$((count + 1))" > "${SLEEP_COUNT_FILE}"
EOF
  chmod +x "${fake_bin}/sleep"
}

@test "recovers an immediately persisted review" {
  cat > "${fake_bin}/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "${GH_ARGS_FILE}"
printf '12345\n'
EOF
  chmod +x "${fake_bin}/gh"

  run env \
    PATH="${fake_bin}:${PATH}" \
    GH_ARGS_FILE="${BATS_TEST_TMPDIR}/gh-args" \
    SLEEP_COUNT_FILE="${BATS_TEST_TMPDIR}/sleep-count" \
    bash "${recovery_script}" dceoy/example 7 token_123

  [ "${status}" -eq 0 ]
  [ "${output}" = $'RESULT=FOUND\nREVIEW_ID=12345\nCHECKS=1' ]
  grep -Fx 'repos/dceoy/example/pulls/7/reviews?per_page=100' "${BATS_TEST_TMPDIR}/gh-args"
  grep -F '<!-- oracle-pr-review:token_123 -->' "${BATS_TEST_TMPDIR}/gh-args"
  [ ! -e "${BATS_TEST_TMPDIR}/sleep-count" ]
}

@test "stops after fifteen one-minute polling intervals" {
  cat > "${fake_bin}/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
count=0
if [ -f "${GH_COUNT_FILE}" ]; then
  read -r count < "${GH_COUNT_FILE}"
fi
printf '%d\n' "$((count + 1))" > "${GH_COUNT_FILE}"
EOF
  chmod +x "${fake_bin}/gh"

  run env \
    PATH="${fake_bin}:${PATH}" \
    GH_COUNT_FILE="${BATS_TEST_TMPDIR}/gh-count" \
    SLEEP_COUNT_FILE="${BATS_TEST_TMPDIR}/sleep-count" \
    bash "${recovery_script}" dceoy/example 7 token_123

  [ "${status}" -eq 4 ]
  [ "${output}" = $'RESULT=CONTINUE\nCHECKS=9\nWAIT_SECONDS=480' ]

  run env \
    PATH="${fake_bin}:${PATH}" \
    GH_COUNT_FILE="${BATS_TEST_TMPDIR}/gh-count" \
    SLEEP_COUNT_FILE="${BATS_TEST_TMPDIR}/sleep-count" \
    bash "${recovery_script}" dceoy/example 7 token_123 continue

  [ "${status}" -eq 1 ]
  [ "${output}" = $'RESULT=NOT_FOUND\nCHECKS=7\nWAIT_SECONDS=420' ]
  [ "$(cat "${BATS_TEST_TMPDIR}/gh-count")" -eq 16 ]
  [ "$(cat "${BATS_TEST_TMPDIR}/sleep-count")" -eq 15 ]
}

@test "fails closed when multiple reviews match" {
  cat > "${fake_bin}/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '12345\n67890\n'
EOF
  chmod +x "${fake_bin}/gh"

  run env \
    PATH="${fake_bin}:${PATH}" \
    SLEEP_COUNT_FILE="${BATS_TEST_TMPDIR}/sleep-count" \
    bash "${recovery_script}" dceoy/example 7 token_123

  [ "${status}" -eq 2 ]
  [ "${output}" = $'RESULT=MULTIPLE\nMATCHES=2\nCHECKS=1' ]
  [ ! -e "${BATS_TEST_TMPDIR}/sleep-count" ]
}

@test "fails closed when the GitHub read fails" {
  cat > "${fake_bin}/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "${fake_bin}/gh"

  run env \
    PATH="${fake_bin}:${PATH}" \
    SLEEP_COUNT_FILE="${BATS_TEST_TMPDIR}/sleep-count" \
    bash "${recovery_script}" dceoy/example 7 token_123

  [ "${status}" -eq 3 ]
  [ "${output}" = $'RESULT=ERROR\nREASON=github_read_failed\nCHECKS=1' ]
  [ ! -e "${BATS_TEST_TMPDIR}/sleep-count" ]
}
