#!/usr/bin/env bash
# Usage: tools/release.sh v0.1.0
# Runs the full test sequence, then tags and pushes. The GitHub workflow builds the zip and publishes the release.
set -euo pipefail
cd "$(dirname "$0")/.."

tag="${1:?usage: tools/release.sh vX.Y.Z}"
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "tag must look like v1.2.3"; exit 1; }
[[ "$(git branch --show-current)" == main ]] || { echo "switch to main first"; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "commit or stash your changes first"; exit 1; }
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && { echo "tag $tag already exists"; exit 1; }
git fetch -q origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo "main differs from origin/main; push or pull first"; exit 1; }

python3 tools/run_tests.py
python3 tools/build_zip.py

git tag -a "$tag" -m "$tag"
git push origin "$tag"
echo "pushed $tag; the release appears on GitHub in a minute or two"
