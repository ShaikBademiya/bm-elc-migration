#!/usr/bin/env bash
#
# Deploy one artifact class to its Composer prefix, file by file.
#
# Whole-directory `gsutil rsync -d` replaced the entire destination prefix on
# every run: slow, and it made every deploy a potential mass deletion. This
# copies only what changed and removes only what was deleted, which is the
# pattern etp-etl-core already uses.
#
# Two modes:
#   incremental - apply a manifest of changed paths (the normal case)
#   full        - reconcile the whole directory, used when the diff cannot be
#                 trusted (new branch, workflow_dispatch, force-push) so an
#                 unreliable diff can never silently deploy nothing
#
# The manifest arrives on stdin as tab-separated `status<TAB>path[<TAB>newpath]`
# lines, exactly as `git diff --name-status -M` emits them. It is read as data,
# never interpolated into the script, so a crafted filename cannot execute.
#
# Usage: deploy_artifacts.sh <source-dir> <gcs-destination> <mode> [manifest-prefix]
#
# manifest-prefix is the path these files have IN THE MANIFEST, which is not
# always where they sit on disk. Since deploys read from an extracted bundle the
# source is `_bundle/artifacts/dags` while the manifest still says
# `artifacts/dags/...`, because git diff is repo-relative. Conflating the two
# made every incremental deploy match nothing and report success having copied
# nothing. Defaults to the source directory for callers where they agree.
set -euo pipefail

SRC="${1:?source directory required}"
DEST="${2:?gcs destination required}"
MODE="${3:-incremental}"
MANIFEST_PREFIX="${4:-$1}"

# A single run should never delete more than this many files. A larger deletion
# is treated as a bad diff and stopped, not carried out.
MAX_DELETIONS="${MAX_DELETIONS:-25}"

SRC="${SRC%/}"
DEST="${DEST%/}"
MANIFEST_PREFIX="${MANIFEST_PREFIX%/}"

log() { printf '%s\n' "$*"; }

if [ ! -d "${SRC}" ]; then
  log "${SRC} does not exist - nothing to deploy"
  exit 0
fi

# ---------------------------------------------------------------- full mode

if [ "${MODE}" = "full" ]; then
  log "FULL reconciliation of ${SRC} -> ${DEST}/"
  count=$(find "${SRC}" -type f | wc -l)
  if [ "${count}" -eq 0 ]; then
    log "refusing to reconcile from an empty source directory"
    exit 1
  fi
  # -r without -d: additive. Removals in full mode are deliberate operations,
  # not a side effect of a sync.
  gsutil -m rsync -r "${SRC}/" "${DEST}/"
  log "reconciled ${count} file(s)"
  exit 0
fi

# ------------------------------------------------------------- incremental

manifest="$(cat)"

if [ -z "${manifest//[[:space:]]/}" ]; then
  log "no changes under ${SRC} - nothing to deploy"
  exit 0
fi

copies=()
deletes=()

# `read -r` with an explicit tab IFS: the filename is data. Never expand a
# manifest value into a command position.
#
# Paths are filtered to this artifact class and made relative to it here rather
# than in the workflow, so the shell plumbing lives in one testable place.
while IFS=$'\t' read -r status path newpath; do
  [ -z "${status:-}" ] && continue

  # Tolerate a CRLF manifest. A stray \r on the final field silently turns every
  # path into one that does not exist, so the deploy would skip every file and
  # still report success.
  status="${status%$'\r'}"
  path="${path%$'\r'}"
  newpath="${newpath:-}"
  newpath="${newpath%$'\r'}"

  # Only this artifact class, and only paths genuinely under it. Matched
  # against the manifest prefix, which is repo-relative and may differ from
  # where the files actually sit on disk.
  case "${path}" in
    "${MANIFEST_PREFIX}/"*) ;;
    *) continue ;;
  esac
  path="${path#"${MANIFEST_PREFIX}/"}"
  if [ -n "${newpath:-}" ]; then
    case "${newpath}" in
      "${MANIFEST_PREFIX}/"*) newpath="${newpath#"${MANIFEST_PREFIX}/"}" ;;
      *) newpath="" ;;
    esac
  fi

  case "${status}" in
    R*)
      # Rename: the old object goes, the new one is copied.
      [ -n "${path:-}" ]    && deletes+=("${path}")
      [ -n "${newpath:-}" ] && copies+=("${newpath}")
      ;;
    D)
      [ -n "${path:-}" ] && deletes+=("${path}")
      ;;
    A | M | C*)
      [ -n "${path:-}" ] && copies+=("${path}")
      ;;
    *)
      log "ignoring unrecognised status '${status}' for ${path:-<none>}"
      ;;
  esac
done <<< "${manifest}"

log "${#copies[@]} file(s) to copy, ${#deletes[@]} to delete"

if [ "${#deletes[@]}" -gt "${MAX_DELETIONS}" ]; then
  log "::error::refusing to delete ${#deletes[@]} objects (limit ${MAX_DELETIONS}) - this looks like a bad diff rather than an intentional removal"
  exit 1
fi

rc=0

for rel in "${copies[@]}"; do
  local_path="${SRC}/${rel}"
  if [ ! -f "${local_path}" ]; then
    # Added then deleted within the same range, or filtered out upstream.
    log "skip copy, not present locally: ${rel}"
    continue
  fi
  log "copy   ${rel}"
  if ! gsutil -q cp "${local_path}" "${DEST}/${rel}"; then
    log "::error::failed to copy ${rel}"
    rc=1
  fi
done

for rel in "${deletes[@]}"; do
  log "delete ${rel}"
  # An object that is already gone is not an error - the deploy is idempotent.
  if ! gsutil -q rm "${DEST}/${rel}" 2>/dev/null; then
    log "already absent: ${rel}"
  fi
done

exit "${rc}"
