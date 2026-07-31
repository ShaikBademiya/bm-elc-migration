"""Job 1 of deploy.yml: `setup` - Resolve environment.

What the job actually does: map the branch name to an environment name, or fail.
Everything downstream binds `environment: ${{ needs.setup.outputs.environment }}`,
which is how the run picks up that environment's secrets - COMPOSER_BUCKET above
all. So the failure that matters is not "the job errored", it is:

  * resolving to the WRONG environment  -> a dev push writes the prod bucket
  * resolving to an EMPTY string        -> the job binds no environment, falls
    back to repository-scoped secrets, and deploys somewhere unintended while
    reporting success

Both are silent. The tests below therefore assert on the value written to
$GITHUB_OUTPUT, not merely on the exit code.

Run:  python test_01_setup.py
"""

from __future__ import annotations

import re
import sys

import yaml
from harness import Suite, job, load_workflow, run_step, step_script

WF = "deploy.yml"
JOB = "setup"

SCRIPT = step_script(WF, JOB)
JOB_DEF = job(WF, JOB)
WORKFLOW = load_workflow(WF)

suite = Suite("deploy.yml :: job 1 `setup` (Resolve environment)")


# ---------------------------------------------------------------------------
# A. Behaviour: branch name -> environment
# ---------------------------------------------------------------------------
# (ref name, expected exit code, expected environment output or None, why)
CASES: list[tuple[str, int, str | None, str]] = [
    # -- the two supported paths
    ("dev", 0, "dev", "the dev branch resolves to the dev environment"),
    ("prod", 0, "prod", "the prod branch resolves to the prod environment"),
    # -- branches that exist in the repo but must never deploy
    ("master", 1, None, "master is retained but dead"),
    ("main", 1, None, "main is retained but dead"),
    # -- legacy arms that were deleted; they must not have come back
    ("ust", 1, None, "removed legacy arm"),
    ("sit", 1, None, "removed legacy arm"),
    # -- near misses: the case arms must be exact, not prefixes
    ("develop", 1, None, "prefix of dev must not match"),
    ("dev-hotfix", 1, None, "prefix of dev must not match"),
    ("devel", 1, None, "prefix of dev must not match"),
    ("prod-fix", 1, None, "prefix of prod must not match"),
    ("production", 1, None, "prefix of prod must not match"),
    ("dev/foo", 1, None, "a namespaced branch is not the dev branch"),
    ("feature/AMF-123", 1, None, "an ordinary feature branch cannot deploy"),
    ("DEV", 1, None, "git refs are case sensitive; DEV is a different branch"),
    ("Prod", 1, None, "git refs are case sensitive; Prod is a different branch"),
    (" dev", 1, None, "leading whitespace must not be trimmed into a match"),
    ("dev ", 1, None, "trailing whitespace must not be trimmed into a match"),
    # -- a tag ref: GITHUB_REF_NAME is the tag name for a tag push
    ("v0.1.2", 1, None, "a tag ref must not resolve to an environment"),
    # -- empty / unset
    ("", 1, None, "an empty ref must fail rather than resolve to an empty env"),
    # -- glob safety: the case WORD must not be treated as a pattern
    ("*", 1, None, "a literal asterisk must not match the dev arm"),
    ("d*", 1, None, "a glob must not match the dev arm"),
    ("?ev", 1, None, "a single-char glob must not match the dev arm"),
    ("[dp]*", 1, None, "a bracket glob must not match either arm"),
    # -- injection: the ref name is data, never code
    ("dev;echo PWNED", 1, None, "a semicolon must not start a new command"),
    ("$(echo prod)", 1, None, "command substitution must not be evaluated"),
    ("`echo prod`", 1, None, "backtick substitution must not be evaluated"),
    ("dev\nprod", 1, None, "a newline must not split into a second match"),
]

for ref, want_rc, want_env, why in CASES:
    label = repr(ref)
    result = run_step(SCRIPT, env={"GITHUB_REF_NAME": ref})

    suite.check(
        "A. exit code",
        f"{label} -> rc {want_rc}  ({why})",
        result.rc == want_rc,
        f"got rc={result.rc}; stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}",
    )

    got_env = result.outputs.get("environment")
    suite.check(
        "B. resolved environment",
        f"{label} -> environment={want_env!r}",
        got_env == want_env,
        f"got {got_env!r}; raw GITHUB_OUTPUT={result.raw_output_file!r}",
    )

    if want_rc != 0:
        # The dangerous failure is not a crash, it is a job that fails but still
        # leaves an output behind for a downstream `environment:` to bind.
        suite.check(
            "C. nothing leaks on the failure path",
            f"{label} writes no GITHUB_OUTPUT at all",
            result.raw_output_file.strip() == "",
            f"wrote {result.raw_output_file!r}",
        )
        named = not ref.strip() or ref.strip().splitlines()[0] in result.combined
        suite.check(
            "D. failure is legible",
            f"{label} emits an ::error:: annotation naming the rejected branch",
            "::error::" in result.combined and named,
            f"combined output={result.combined!r}",
        )

# Injection probes: assert the payload never executed, whatever the exit code.
for payload, marker in (("dev;echo PWNED", "PWNED"), ("$(echo prod)", "\nprod\n")):
    result = run_step(SCRIPT, env={"GITHUB_REF_NAME": payload})
    suite.check(
        "E. injection",
        f"{payload!r} does not execute",
        marker not in result.stdout or "Resolved deployment environment" not in result.stdout,
        f"stdout={result.stdout!r}",
    )
    suite.check(
        "E. injection",
        f"{payload!r} resolves no environment",
        "environment" not in result.outputs,
        f"outputs={result.outputs!r}",
    )

# GITHUB_REF_NAME entirely unset. The step has no `set -u`, so this expands to
# the empty string; it must still be rejected rather than resolving to "".
unset_result = run_step(SCRIPT, unset=("GITHUB_REF_NAME",))
suite.check(
    "F. unset variable",
    "GITHUB_REF_NAME unset -> rc 1",
    unset_result.rc == 1,
    f"got rc={unset_result.rc}, outputs={unset_result.outputs!r}",
)
suite.check(
    "F. unset variable",
    "GITHUB_REF_NAME unset -> no environment output",
    "environment" not in unset_result.outputs,
    f"outputs={unset_result.outputs!r}",
)

# The success path must also write the step summary, and write exactly one
# key=value line - a second line would be a second output to a consumer.
for ref in ("dev", "prod"):
    result = run_step(SCRIPT, env={"GITHUB_REF_NAME": ref})
    lines = [line for line in result.raw_output_file.splitlines() if line.strip()]
    suite.check(
        "G. output shape",
        f"{ref}: exactly one key=value line in GITHUB_OUTPUT",
        lines == [f"environment={ref}"],
        f"got {lines!r}",
    )
    suite.check(
        "G. output shape",
        f"{ref}: step summary records the environment and the branch",
        ref in result.summary and "Deploying to" in result.summary,
        f"summary={result.summary!r}",
    )


# ---------------------------------------------------------------------------
# H. Static contract: the wiring around the job
# ---------------------------------------------------------------------------

step = JOB_DEF["steps"][0]
declared = JOB_DEF.get("outputs", {}).get("environment", "")

suite.check(
    "H. wiring",
    "job output `environment` references the step id that actually exists",
    step.get("id") == "set-env" and "steps.set-env.outputs.environment" in declared,
    f"step id={step.get('id')!r}, job output expr={declared!r}",
)

# Every job that binds an environment from this output must depend on setup, or
# it evaluates to empty and the job silently runs with repository-scoped secrets.
consumers = [
    name
    for name, spec in WORKFLOW["jobs"].items()
    if "needs.setup.outputs.environment" in yaml.dump(spec)
]
suite.check(
    "H. wiring",
    "at least one job consumes the resolved environment",
    bool(consumers),
    f"consumers={consumers}",
)
for name in consumers:
    spec = WORKFLOW["jobs"][name]
    needs = spec.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    suite.check(
        "H. wiring",
        f"job `{name}` consumes setup's output and declares `needs: setup`",
        "setup" in needs,
        f"needs={needs!r}",
    )

# No job may bypass the guard. If any job is reachable without setup having
# succeeded, a push from an unsupported branch still runs something.
jobs = WORKFLOW["jobs"]


def needs_of(name: str) -> list[str]:
    value = jobs[name].get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def depends_on_setup(name: str, seen: set[str] | None = None) -> bool:
    seen = seen or set()
    if name in seen:
        return False
    seen.add(name)
    parents = needs_of(name)
    return "setup" in parents or any(depends_on_setup(p, seen) for p in parents)


for name in jobs:
    if name == "setup":
        continue
    suite.check(
        "I. no job bypasses the guard",
        f"`{name}` is gated on setup (directly or transitively)",
        depends_on_setup(name),
        f"needs={needs_of(name)!r}",
    )

# The push trigger and the case arms must describe the same set of branches.
accepted = set(re.findall(r"^\s+([A-Za-z0-9_.-]+)\)\s*$", SCRIPT, re.MULTILINE))
triggers = set(WORKFLOW["on"]["push"]["branches"])
suite.check(
    "J. trigger/guard agreement",
    f"case arms {sorted(accepted)} == push branches {sorted(triggers)}",
    accepted == triggers,
    f"arms-only={sorted(accepted - triggers)}, triggers-only={sorted(triggers - accepted)}",
)

suite.check(
    "K. job hygiene",
    "setup declares a timeout",
    "timeout-minutes" in JOB_DEF,
    f"timeout-minutes={JOB_DEF.get('timeout-minutes')}",
)
suite.check(
    "K. job hygiene",
    "setup binds no environment (it holds no environment-scoped secrets)",
    "environment" not in JOB_DEF,
    f"environment={JOB_DEF.get('environment')!r}",
)
suite.check(
    "K. job hygiene",
    "setup checks out nothing (a resolver needs no source)",
    all("uses" not in s for s in JOB_DEF["steps"]),
    f"steps={[s.get('uses') or s.get('name') for s in JOB_DEF['steps']]}",
)

sys.exit(suite.report())
