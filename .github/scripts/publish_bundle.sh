#!/usr/bin/env bash
#
# Package artifacts/ into one versioned bundle and publish it to the Generic
# Artifact Registry repository.
#
# This is the "build once" half of build-once-deploy-many. Everything under
# artifacts/ becomes a single immutable object identified by the commit's
# version, and every environment afterwards deploys that object rather than a
# fresh git checkout. Without it, each environment builds its own copy from
# source and nothing guarantees prod runs the bytes dev tested.
#
# Usage: publish_bundle.sh <version>
# Requires: REGION, PROJECT_ID, BUNDLE_REPO in the environment.
set -euo pipefail

VERSION="${1:?version required}"
: "${REGION:?REGION required}"
: "${PROJECT_ID:?PROJECT_ID required}"
: "${BUNDLE_REPO:?BUNDLE_REPO required}"

PACKAGE="artifacts"
BUNDLE="artifacts-${VERSION}.tar.gz"

if [ ! -d artifacts ]; then
  echo "::error::artifacts/ does not exist - nothing to package"
  exit 1
fi

file_count=$(find artifacts -type f | wc -l)
if [ "${file_count}" -eq 0 ]; then
  echo "::error::artifacts/ is empty - refusing to publish an empty bundle"
  exit 1
fi

# Already published? Versions are immutable, so re-running a workflow must be a
# no-op rather than a 409. This is the same reasoning as the wheel upload.
if gcloud artifacts versions describe "${VERSION}" \
     --package="${PACKAGE}" \
     --repository="${BUNDLE_REPO}" \
     --location="${REGION}" \
     --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "bundle ${PACKAGE}:${VERSION} is already published; nothing to do"
  exit 0
fi

# --sort=name and a fixed mtime/owner make the tarball reproducible: the same
# tree produces the same bytes, so a bundle can be compared across builds.
tar --sort=name \
    --mtime='UTC 2020-01-01' \
    --owner=0 --group=0 --numeric-owner \
    -czf "${BUNDLE}" artifacts

echo "packaged ${file_count} file(s) into ${BUNDLE} ($(wc -c < "${BUNDLE}") bytes)"
echo "sha256: $(sha256sum "${BUNDLE}" | cut -d' ' -f1)"

gcloud artifacts generic upload \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --repository="${BUNDLE_REPO}" \
  --package="${PACKAGE}" \
  --version="${VERSION}" \
  --source="${BUNDLE}"

echo "published ${PACKAGE}:${VERSION}"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "| Bundle | Value |"
    echo "| --- | --- |"
    echo "| package | \`${PACKAGE}:${VERSION}\` |"
    echo "| files | \`${file_count}\` |"
    echo "| sha256 | \`$(sha256sum "${BUNDLE}" | cut -d' ' -f1)\` |"
  } >> "${GITHUB_STEP_SUMMARY}"
fi
