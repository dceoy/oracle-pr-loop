#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  printf 'RESULT=ERROR\nREASON=invalid_arguments\n'
  exit 3
fi

repo="$1"
pr_number="$2"
review_token="$3"

if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
  [[ ! "$pr_number" =~ ^[1-9][0-9]*$ ]] ||
  [[ ! "$review_token" =~ ^[A-Za-z0-9_-]+$ ]]; then
  printf 'RESULT=ERROR\nREASON=invalid_arguments\n'
  exit 3
fi

marker="<!-- oracle-pr-review:${review_token} -->"
checks=0

check_reviews() {
  checks=$((checks + 1))

  local output
  if ! output="$(
    gh api --paginate "repos/${repo}/pulls/${pr_number}/reviews?per_page=100" \
      --jq ".[] | select(.state == \"COMMENTED\" and ((.body // \"\") | contains(\"${marker}\"))) | .id"
  )"; then
    printf 'RESULT=ERROR\nREASON=github_read_failed\nCHECKS=%d\n' "$checks"
    return 3
  fi

  local count=0
  local review_id=''
  local id
  while IFS= read -r id; do
    if [ -n "$id" ]; then
      count=$((count + 1))
      review_id="$id"
    fi
  done <<< "$output"

  if [ "$count" -eq 1 ]; then
    printf 'RESULT=FOUND\nREVIEW_ID=%s\nCHECKS=%d\n' "$review_id" "$checks"
    return 0
  fi
  if [ "$count" -gt 1 ]; then
    printf 'RESULT=MULTIPLE\nMATCHES=%d\nCHECKS=%d\n' "$count" "$checks"
    return 2
  fi

  return 10
}

if check_reviews; then
  exit 0
else
  result=$?
fi
if [ "$result" -ne 10 ]; then
  exit "$result"
fi

for _ in {1..15}; do
  sleep 60
  if check_reviews; then
    exit 0
  else
    result=$?
  fi
  if [ "$result" -ne 10 ]; then
    exit "$result"
  fi
done

printf 'RESULT=NOT_FOUND\nCHECKS=%d\nWAIT_SECONDS=900\n' "$checks"
exit 1
