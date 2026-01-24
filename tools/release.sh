#!/bin/bash
set -e

bump=${1:-patch}
version=$(hatch version)

git pull --ff-only
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "Tag v$version already exists; bump version first"
  exit 1
fi
git add -A
git commit -m "v$version" || true  # ok if nothing to commit
git tag "v$version"
git push
git push --tags

echo "Released v$version"

hatch version $bump
git add -A
git commit -m "bump to $(hatch version)"
git push

echo "Dev now at $(hatch version)"
