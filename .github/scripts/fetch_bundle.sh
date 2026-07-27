#!/usr/bin/env bash
#
# Fetch a published artifact bundle and extract it.
#
# This is the "deploy many" half of build-once-deploy-many. Deploys read from
# the extracted bundle rather than from the git working tree, so every
# environment publishes the same bytes that were built and tested once.
#
# Usage: fetch_bundle.sh <version> <destination-dir>
# Requires: REGION, PROJECT_ID, BUNDLE_REPO in the environment.
set -euo pipefail

VERSION="${1:?version required}"
DEST_DIR="${2:?destination directory required}"
: "${REGION:?REGION required}"
: "${PROJECT_ID:?PROJECT_ID required}"
: "${BUNDLE_REPO:?BUNDLE_REPO required}"

PACKAGE="artifacts"
BUNDLE="artifacts-${VERSION}.tar.gz"

rm -rf "${DEST_DIR}"
mkdir -p "${DEST_DIR}/_download"

echo "fetching ${PACKAGE}:${VERSION} from ${BUNDLE_REPO}"
gcloud artifacts generic download \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --repository="${BUNDLE_REPO}" \
  --package="${PACKAGE}" \
  --version="${VERSION}" \
  --destination="${DEST_DIR}/_download"

ARCHIVE="${DEST_DIR}/_download/${BUNDLE}"
if [ ! -f "${ARCHIVE}" ]; then
  # Older gcloud releases name the download after the package rather than the
  # uploaded filename; accept whatever single archive arrived.
  ARCHIVE="$(find "${DEST_DIR}/_download" -type f -name '*.tar.gz' | head -1)"
fi

if [ -z "${ARCHIVE}" ] || [ ! -f "${ARCHIVE}" ]; then
  echo "::error::no archive found in the downloaded bundle for ${PACKAGE}:${VERSION}"
  ls -R "${DEST_DIR}/_download" || true
  exit 1
fi

echo "sha256: $(sha256sum "${ARCHIVE}" | cut -d' ' -f1)"
tar -xzf "${ARCHIVE}" -C "${DEST_DIR}"

if [ ! -d "${DEST_DIR}/artifacts" ]; then
  echo "::error::bundle does not contain an artifacts/ directory"
  exit 1
fi

echo "extracted $(find "${DEST_DIR}/artifacts" -type f | wc -l) file(s) to ${DEST_DIR}/artifacts"
