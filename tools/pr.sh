#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

if [[ $# -lt 1 ]]; then
  echo "Usage: tools/pr.sh \"Message\" [label] [body|body-file]"
  exit 1
fi

msg=$1
label=${2:-enhancement}   # enhancement or bug
body=${3:-""}

base_branch=${BASE_BRANCH:-main}
remote=${REMOTE:-origin}

cleanup() {
  git switch "$base_branch" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Ensure we're branching from an up-to-date main
git switch "$base_branch"
git pull --ff-only "$remote" "$base_branch"

# Create a unique branch name
branch="pr-$(date +%s)-$RANDOM"
git switch -c "$branch"

# Commit tracked changes only (and whatever is already staged).
# NOTE: New/untracked files must be staged manually beforehand.
if git diff --quiet && git diff --cached --quiet; then
  echo "Nothing to commit (no tracked changes and nothing staged)."
  exit 1
fi

# This will:
# - include all tracked modifications/deletions (via -a)
# - also include anything already staged (e.g. new files you chose to git add)
git commit -am "$msg" || {
  echo "Commit failed. If you added only new files, stage them first (git add <files>) and re-run."
  exit 1
}

git push -u "$remote" "$branch"

# Create PR targeting main
if [[ -n "$body" ]]; then
  if [[ -f "$body" ]]; then
    gh pr create --title "$msg" --body-file "$body" --label "$label" --base "$base_branch"
  else
    gh pr create --title "$msg" --body "$body" --label "$label" --base "$base_branch"
  fi
else
  gh pr create --fill --label "$label" --base "$base_branch"
fi

# Merge PR for current branch; delete local+remote branch
head_sha=$(git rev-parse HEAD)
gh pr merge --squash --delete-branch --match-head-commit "$head_sha" --subject "$msg"

# Update local main to include the squash merge commit
git switch "$base_branch"
git pull --ff-only "$remote" "$base_branch"

echo "PR created with label '$label' and merged; $base_branch updated"
