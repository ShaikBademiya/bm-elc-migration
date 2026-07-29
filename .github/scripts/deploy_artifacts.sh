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

  # How many objects would -d remove? Counted before the sync so a bundle that
  # is wrong-but-not-empty cannot quietly empty a prefix. Same ceiling as the
  # incremental path, for the same reason.
  remote_rel="$(gsutil ls -r "${DEST}/**" 2>/dev/null \
                | grep -vE ':$|/$|^$' \
                | sed "s|^${DEST}/||" \
                | sort -u || true)"
  local_rel="$(cd "${SRC}" && find . -type f | sed 's|^\./||' | sort -u)"
  doomed=$(comm -23 <(printf '%s\n' "${remote_rel}") <(printf '%s\n' "${local_rel}") | grep -c . || true)

  if [ "${doomed}" -gt "${MAX_DELETIONS}" ]; then
    log "::error::full reconciliation would delete ${doomed} object(s) from ${DEST}/ (limit ${MAX_DELETIONS}) - refusing"
    exit 1
  fi
  [ "${doomed}" -gt 0 ] && log "reconciliation will remove ${doomed} object(s) no longer in the source"

  # -c is not optional. The artifact bundle is built with a frozen mtime
  # (--mtime='UTC 2020-01-01') so it is reproducible, and gsutil stores that
  # mtime on the object. rsync's default comparator is size+mtime, so with the
  # mtime pinned, ANY edit that leaves the file the same size is invisible and
  # the sync reports success having copied nothing. Compare checksums instead.
  #
  # -d makes this an actual reconciliation: a file removed from the repo is
  # removed from the bucket. Safe here because DEST is domain-namespaced
  # (…/dags/udp), the empty-source check above runs first, and deploy manifests
  # live outside every deploy prefix.
  gsutil -m rsync -c -d -r "${SRC}/" "${DEST}/"
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
    C*)
      # Copy: git reports `C<score> <source> <destination>`. The new file is the
      # destination; deploying the source would re-copy something unchanged and
      # miss the file the commit actually added.
      [ -n "${newpath:-}" ] && copies+=("${newpath}")
      ;;
    A | M)
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
  # gsutil expands wildcards in an rm argument, so a path containing one could
  # remove far more than the single object the manifest named - and do it
  # without passing through the MAX_DELETIONS ceiling above.
  case "${rel}" in
    *'*'* | *'?'* | *'['*)
      log "::error::refusing to delete '${rel}': gsutil would treat it as a wildcard"
      rc=1
      continue
      ;;
  esac

  log "delete ${rel}"
  # An object that is already gone is not an error - the deploy is idempotent.
  # Anything else is: swallowing every failure meant a permission error, a bad
  # bucket name or a network fault all reported as a successful removal, and
  # the object stayed in the environment while the job went green.
  if err="$(gsutil rm "${DEST}/${rel}" 2>&1)"; then
    [ -n "${err}" ] && printf '%s\n' "${err}"
    continue
  fi
  case "${err}" in
    *"No URLs matched"* | *"NotFoundException"* | *"404"*)
      log "already absent: ${rel}"
      ;;
    *)
      log "::error::failed to delete ${rel}: ${err}"
      rc=1
      ;;
  esac
done

exit "${rc}"
