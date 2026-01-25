#!/bin/bash
set -e

if [ -z "$1" ]; then
  echo "Usage: tools/pr.sh \"Message\" [label] [body|body-file]"
  exit 1
fi

msg=$1
label=${2:-enhancement}  # enhancement or bug
body=${3:-""}

branch="pr-$(date +%s)"
git checkout -b "$branch"
git commit -am "$msg"
git push -u origin "$branch"
if [ -n "$body" ]; then
  if [ -f "$body" ]; then
    gh pr create --title "$msg" --body-file "$body" --label "$label"
  else
gh pr create --title "$msg" --body "$body" --label "$label"
  fi
else
  gh pr create --fill --label "$label"
fi

gh pr merge --squash --delete-branch
git checkout main
git pull --ff-only

echo "PR created with label '$label' and merged; main updated"
