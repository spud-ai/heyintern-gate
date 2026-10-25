# heyintern-gate

The bootstrap gate for heyintern/cloud (private). It is the one workflow that may plan and apply
heyintern/cloud's `infra/bootstrap` stack (budgets and the budget stop, the guards, the CI roles,
the operator, the switchboard's waker): AWS trusts its roles only to
`colegottdank/heyintern-gate/.github/workflows/bootstrap.yml@refs/heads/main`, called by heyintern/cloud
on main, on push or a manual run, by colegottdank (heyintern/cloud's `infra/bootstrap/bootstrap_ci.tf`).

Only colegottdank can write this repository; that is the point. heyintern/cloud is on GitHub Free,
without branch protection, so its bot can push its main and change any file there, workflows
included. None of that changes what runs with the bootstrap roles: only this repository does.

## What it does

- `.github/workflows/bootstrap.yml` (`on: workflow_call` only; it never runs by itself):
  - **pull request** in heyintern/cloud: plans with the read-only bootstrap plan role, shows the
    plan in the run summary with the bootstrap commits on main that are not applied yet, and keeps
    a record of the plan (changes and digests, no values) for the apply.
  - **push to main or a manual run** by colegottdank: applies, if `gate.py` agrees, before any AWS
    credentials:
    - the run is heyintern/cloud's `.github/workflows/bootstrap.yml` on main, started by the owner
      (a re-run by anyone else is refused);
    - it applies merged pull requests: the one this push merged, or the numbers the owner typed;
    - every bootstrap commit on main since the last apply belongs to them (a direct push to main
      stops it);
    - the newest one's plan ran on exactly main's code, and the plan made now matches it.
- `gate.py`: those checks. Standard library and the `gh` CLI; prints IDs and digests, never values.
- `test_gate.py`: unit tests (fake GitHub answers, throwaway git repositories, no network), run by
  the workflow before every plan and apply.
- `caller/bootstrap.yml`: the workflow heyintern/cloud keeps at `.github/workflows/bootstrap.yml` to
  call this one. Kept here for review; it does nothing in this repository.

It runs only its own code (checked out at the commit the workflow came from, checked against the
OIDC token's `job_workflow_sha`) and heyintern/cloud's OpenTofu stack.

## Changing it

The bot proposes changes in spud-ai/heyintern-gate, this repository's upstream. colegottdank reads
the comparison on this fork and taps **Sync fork**. Nothing reaches the gate before that.
