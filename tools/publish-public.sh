#!/bin/sh
# Copies this commit's files into the public repo (quarterso/quarter) as one release commit by Quarter.
# The public repo never gets this repo's history, so no personal names or internal notes go with it.
# Usage: tools/publish-public.sh <version>   (run from a clean checkout of main)
set -eu
version=$1
here=$(cd "$(dirname "$0")/.." && pwd)
[ -z "$(git -C "$here" status --porcelain)" ] || { echo "commit or stash your changes first" >&2; exit 1; }
work=$(mktemp -d)
git clone -q git@github.com:quarterso/quarter.git "$work/public"
find "$work/public" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git -C "$here" archive HEAD | tar -x -C "$work/public"
rm -f "$work/public/.github/CODEOWNERS" "$work/public/ledger.db"
cd "$work/public"
git add -A
GIT_AUTHOR_NAME=Quarter GIT_AUTHOR_EMAIL=hello@quarterfi.com GIT_COMMITTER_NAME=Quarter GIT_COMMITTER_EMAIL=hello@quarterfi.com \
  git commit -qm "Release $version"
git tag "v$version"
git push -q origin HEAD:main "v$version"
echo "Published v$version to github.com/quarterso/quarter. The image builds from the tag."
