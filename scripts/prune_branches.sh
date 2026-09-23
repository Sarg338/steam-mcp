#!/usr/bin/env bash
# Delete remote branches whose work already reached the default branch through a
# merged PR. Run nightly by the Release workflow; DRY_RUN=1 only reports.
#
# A branch is deleted only if ALL of these hold, so nothing unique can be lost:
#   - no open PR uses it (deleting a PR's head branch would close the PR)
#   - at least one merged PR came from it (branches pushed without a PR are left)
#   - its tip commit is on the default branch (catches commits added after merge)
# Everything else is kept and listed with the reason. GitHub can restore a
# deleted PR branch from the PR page.
#
# Needs: gh (GH_TOKEN), REPO=owner/name, optional DEFAULT_BRANCH (default main).
set -euo pipefail
repo="${REPO:?}"; default="${DEFAULT_BRANCH:-main}"
git fetch --quiet --prune origin "+refs/heads/*:refs/remotes/origin/*"
deleted=0; kept=0
while read -r b; do
  [ "$b" = "$default" ] && continue
  if [ "$(gh pr list -R "$repo" --head "$b" --state open --json number --jq length)" != 0 ]; then
    echo "keep   $b: open PR"; kept=$((kept+1)); continue; fi
  if [ "$(gh pr list -R "$repo" --head "$b" --state merged --json number --jq length)" = 0 ]; then
    echo "keep   $b: never merged through a PR"; kept=$((kept+1)); continue; fi
  if ! git merge-base --is-ancestor "origin/$b" "origin/$default"; then
    echo "keep   $b: has commits that aren't on $default"; kept=$((kept+1)); continue; fi
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "WOULD delete $b"; else
    git push --quiet origin --delete "$b" && echo "delete $b"; fi
  deleted=$((deleted+1))
done < <(gh api "repos/$repo/branches" --paginate --jq '.[].name')
echo "done: $deleted deleted, $kept kept"
