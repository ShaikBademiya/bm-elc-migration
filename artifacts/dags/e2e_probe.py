# E2E probe - added 2026-07-29 to prove incremental deploy copies exactly the changed file.
# This file is deleted again by the same test run, which proves deletions propagate to the bucket.
# Expected destination: gs://<COMPOSER_BUCKET>/dags/udp/e2e_probe.py
E2E_MARKER = "probe-v1"

print(f"e2e probe {E2E_MARKER}")
