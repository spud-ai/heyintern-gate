#!/usr/bin/env python3
"""The gate for heyintern/cloud's bootstrap stack. Runs from this repository, which only its owner
can write; heyintern/cloud's own code is never run with the bootstrap role except the OpenTofu
stack itself.

heyintern/cloud is on GitHub Free: no branch protection, so the bot can push main. What may reach
AWS, then, is only what the owner merged and so reviewed:

  plan (pull request)  record     the plan's changes (address and action of each resource that
                                  changes, and a digest of what each becomes) and the code it ran on,
                                  as the artifact bootstrap-plan. No values: the plan JSON holds the
                                  budget email and never leaves the runner.
                       unapplied  bootstrap commits on main since the last apply (bootstrap/applied.json),
                                  listed in the summary.
  apply (owner, main)  find       before any AWS credentials: the run is heyintern/cloud's
                                  bootstrap.yml on main, started by the owner; it applies merged pull
                                  requests (push: the one this commit merged; manual run: the numbers
                                  the owner typed); the newest one's plan ran on exactly main's code.
                       history    with the role: every bootstrap commit on main since the last apply
                                  belongs to those pull requests (no direct push rides along).
                       compare    the plan made now has the same changes as that pull request's.

Prints IDs, addresses, actions and digests only, never values. Standard library and the gh CLI.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

CLOUD = "heyintern/cloud"
GATE_WORKFLOW = "colegottdank/heyintern-gate/.github/workflows/bootstrap.yml@refs/heads/main"
CALLER = ".github/workflows/bootstrap.yml"   # heyintern/cloud's workflow that calls this gate
PATHS = ["infra/bootstrap", CALLER]          # what a bootstrap change is, in heyintern/cloud
ARTIFACT = "bootstrap-plan"
RECORD = "gate.json"
wait = time.sleep   # tests replace it


class Refused(Exception):
    """Nothing is applied; the message says why and what to do."""


# ---- the plan --------------------------------------------------------------------------------------

def plan_changes(plan):
    """[(address, actions, digest of after/after_unknown)] for every resource that changes."""
    out = []
    for rc in plan.get("resource_changes") or []:
        change = rc.get("change") or {}
        actions = list(change.get("actions") or [])
        if actions in (["no-op"], ["read"]):
            continue
        body = json.dumps({"after": change.get("after"), "after_unknown": change.get("after_unknown")},
                          sort_keys=True, separators=(",", ":"))
        out.append((rc["address"], actions, hashlib.sha256(body.encode()).hexdigest()))
    return sorted(out)


def plan_summary(plan):
    """The change lines (no values) and one digest over everything."""
    changes = plan_changes(plan)
    return {"changes": [f"{'+'.join(a)} {addr}" for addr, a, _ in changes],
            "digest": hashlib.sha256(json.dumps(changes, separators=(",", ":")).encode()).hexdigest()}


def compare(expected, now):
    if expected.get("digest") != now["digest"]:
        missing = sorted(set(expected.get("changes") or []) - set(now["changes"]))
        extra = sorted(set(now["changes"]) - set(expected.get("changes") or []))
        detail = "".join(f"\n  only in the pull request's plan: {c}" for c in missing) + \
            "".join(f"\n  only in the plan now: {c}" for c in extra)
        raise Refused("The plan now differs from the one the pull request showed%s%s. Nothing is applied: "
                      "AWS or the state changed since. Open a pull request to see the plan again."
                      % (":" if detail else " (the same resources, other values)", detail))


# ---- heyintern/cloud's code ------------------------------------------------------------------------

class Repo:
    def __init__(self, path):
        self.path = path

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, args, r.stdout, r.stderr)
        return r

    def out(self, *args):
        return self.git(*args).stdout.strip()

    def is_ancestor(self, a, b):
        return self.git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    def fingerprint(self, rev="HEAD"):
        """The exact code a plan ran on: git object IDs of the bootstrap paths at rev."""
        return {p: self.out("rev-parse", f"{rev}:{p}") for p in PATHS}

    def bootstrap_commits(self, since, until):
        """(sha, 'short author: subject') of commits in since..until that touch the bootstrap paths."""
        # --full-history: no commit on a merged side branch is pruned from the list.
        log = self.out("log", "--full-history", "--format=%H%x09%h %an: %s", f"{since}..{until}", "--", *PATHS)
        return [tuple(line.split("\t", 1)) for line in log.splitlines() if line]


def unapplied(repo, applied_commit, base):
    if not applied_commit:
        return None, "No record of the last apply (bootstrap/applied.json): see docs/setup.md, step 14."
    if not repo.is_ancestor(applied_commit, base):
        return None, f"The last applied commit {applied_commit[:12]} is not on main any more: check main's history."
    return [line for _, line in repo.bootstrap_commits(applied_commit, base)], None


def check_history(repo, applied_commit, head, allowed):
    """Every bootstrap commit on main since the last apply must belong to the pull requests applied now."""
    if not applied_commit:
        raise Refused("No record of the last apply (bootstrap/applied.json). Root writes it after its apply "
                      "(docs/setup.md, step 14); until then only root applies.")
    if not repo.is_ancestor(applied_commit, head):
        raise Refused(f"The last applied commit {applied_commit[:12]} is not on main any more. Nothing is applied.")
    other = [(sha, line) for sha, line in repo.bootstrap_commits(applied_commit, head) if sha not in allowed]
    if other:
        raise Refused("Main has bootstrap changes since the last apply that are not part of the pull requests "
                      "being applied:\n" + "\n".join(f"  {line}" for _, line in other) +
                      "\nNothing is applied. Review them; if they were merged by pull request, Run workflow with "
                      "every pull request number to apply them together. A direct push needs a pull request.")


# ---- GitHub --------------------------------------------------------------------------------------------

def gh_api(path, **params):
    args = ["gh", "api", "-X", "GET", path]
    for k, v in params.items():
        args += ["-f", f"{k}={v}"]
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)


def gh_api_pages(path):
    args = ["gh", "api", "--paginate", "--slurp", "-X", "GET", path]
    pages = json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
    return [x for page in pages for x in page]


def check_caller(env, event_kind):
    """heyintern/cloud's bootstrap.yml calls this gate; nothing else may (whatever it passes)."""
    wf = env.get("GITHUB_WORKFLOW_REF", "")
    if env.get("GITHUB_REPOSITORY") != CLOUD:
        raise Refused(f"Called from {env.get('GITHUB_REPOSITORY')!r}, not {CLOUD}.")
    if event_kind == "apply":
        if wf != f"{CLOUD}/{CALLER}@refs/heads/main" or env.get("GITHUB_REF") != "refs/heads/main":
            raise Refused(f"Called by {wf!r}: only {CLOUD}/{CALLER} on main may apply.")
    elif not re.fullmatch(rf"{re.escape(CLOUD)}/{re.escape(CALLER)}@refs/pull/\d+/merge", wf):
        raise Refused(f"Called by {wf!r}: plans come from {CLOUD}/{CALLER} on a pull request.")


def owner_started(run, owner_id):
    """Both who the run is for and who started this attempt (a re-run by another account)."""
    return str((run.get("actor") or {}).get("id")) == owner_id and str((run.get("triggering_actor") or {}).get("id")) == owner_id


def pick_pr_for_push(pulls, sha):
    for pr in pulls:
        if pr.get("merged_at") and pr.get("merge_commit_sha") == sha and (pr.get("base") or {}).get("ref") == "main":
            return pr
    return None


def check_pr_merged(pr, is_ancestor):
    if not pr.get("merged_at") or (pr.get("base") or {}).get("ref") != "main":
        raise Refused(f"#{pr.get('number')} is not a pull request merged into main.")
    if not is_ancestor(pr["merge_commit_sha"]):
        raise Refused(f"#{pr.get('number')}'s merge {pr['merge_commit_sha'][:12]} is not on main.")
    return pr


def parse_prs(text):
    nums = [n for n in re.split(r"[\s,]+", text or "") if n]
    if not nums or not all(n.isdigit() for n in nums):
        raise Refused(f"Give the merged pull request numbers to apply, comma-separated (got {text!r}).")
    return sorted(set(int(n) for n in nums))


def pick_plan_run(runs, pr):
    """The latest successful run of heyintern/cloud's bootstrap.yml on the pull request's last commit."""
    head = pr["head"]
    ok = [r for r in runs
          if r.get("event") == "pull_request" and r.get("conclusion") == "success" and r.get("path") == CALLER
          and r.get("head_sha") == head["sha"] and r.get("head_branch") == head["ref"]]
    return max(ok, key=lambda r: (r.get("created_at") or "", r.get("id") or 0)) if ok else None


def check_record(record, pr, current):
    if str(record.get("pr")) != str(pr["number"]) or record.get("head_sha") != pr["head"]["sha"]:
        raise Refused(f"The plan record is for #{record.get('pr')} at {str(record.get('head_sha'))[:12]}, not #{pr['number']}.")
    differs = [p for p in PATHS if (record.get("code") or {}).get(p) != current.get(p)]
    if differs:
        raise Refused("Main's bootstrap code is not what #%s's plan ran on (%s differ): something reached main after "
                      "that plan. Nothing is applied. Open a pull request: its plan shows main's bootstrap changes."
                      % (pr["number"], ", ".join(differs)))


def oidc_claims():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com"
    req = urllib.request.Request(url, headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        jwt = json.load(r)["value"]
    print(f"::add-mask::{jwt}")
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def expected_subjects(cfg, kind):
    """As heyintern/cloud's infra/bootstrap/bootstrap_ci.tf builds them."""
    prefix = f"repository_owner_id:{cfg['repository_owner_id']}:repository_id:{cfg['repository_id']}"
    g = cfg["bootstrap_gate"]
    ref = f"{g['repository']}/{g['workflow']}@{g['ref']}"
    if kind == "plan":
        return [f"{prefix}:pull_request:job_workflow_ref:{ref}:event_name:pull_request:actor_id:*"]
    return [f"{prefix}:ref:refs/heads/main:job_workflow_ref:{ref}:event_name:{e}:actor_id:{a}"
            for e in ("push", "workflow_dispatch") for a in cfg["bootstrap_actors"].values()]


# ---- commands ------------------------------------------------------------------------------------------

def load(path):
    with open(path) as f:
        return json.load(f)


def output(**kv):
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def applied_commit(path):
    if path and os.path.exists(path):
        try:
            return str(load(path).get("commit") or "")
        except ValueError:
            return ""
    return ""


def cmd_record(a):
    repo = Repo(a.repo)
    record = {"pr": int(a.pr), "head_sha": a.head_sha, "code": repo.fingerprint(), **plan_summary(load(a.plan_json))}
    with open(a.out, "w") as f:
        json.dump(record, f, indent=1)
    print(f"{len(record['changes'])} resources change; digest {record['digest'][:16]}")


def cmd_unapplied(a):
    commits, note = unapplied(Repo(a.repo), applied_commit(a.applied), a.base)
    if commits:
        print("**Already on main, not applied yet. The owner's merge of this pull request does not apply them: "
              "they need their own pull requests, applied together with Run workflow (README.md).**")
        for c in commits:
            print(f"- {c}")
    else:
        print(note or "Nothing else on main is waiting to be applied.")


def cmd_oidc(a):
    import fnmatch
    check_caller(os.environ, a.kind)
    want = expected_subjects(load(a.config), a.kind)
    c = oidc_claims()
    print(json.dumps({k: c.get(k) for k in ("sub", "job_workflow_ref", "workflow_ref", "event_name", "ref", "actor", "actor_id")}, indent=2))
    if not any(fnmatch.fnmatchcase(c.get("sub", ""), w) for w in want):
        raise Refused(f"The OIDC subject {c.get('sub')!r} is not one the bootstrap {a.kind} role trusts ({want}).")
    print(f"OIDC subject matches the bootstrap {a.kind} role's trust.")


def check_self(claims, gate_head):
    """The gate code checked out is the commit this workflow file came from (job_workflow_sha)."""
    if claims.get("job_workflow_ref") != GATE_WORKFLOW:
        raise Refused(f"Running as {claims.get('job_workflow_ref')!r}, not {GATE_WORKFLOW}.")
    if claims.get("job_workflow_sha") != gate_head:
        raise Refused(f"The gate checked out ({gate_head[:12]}) is not the workflow's commit ({str(claims.get('job_workflow_sha'))[:12]}): "
                      "the gate changed while this ran. Run it again.")


def cmd_self(a):
    check_self(oidc_claims(), Repo(a.dir).out("rev-parse", "HEAD"))
    print(f"The gate is {GATE_WORKFLOW}, at the commit this workflow came from.")


def cmd_find(a):
    env, owner = os.environ, a.owner_id
    check_caller(env, "apply")
    repo, sha = Repo(a.repo), env["GITHUB_SHA"]
    run = gh_api(f"repos/{CLOUD}/actions/runs/{env['GITHUB_RUN_ID']}")
    if not owner_started(run, owner):
        raise Refused("This run was not started by the owner: the bootstrap is applied only by the owner's merge or manual run.")
    if a.event == "push":
        pr = None
        for _ in range(4):   # the API learns of a merge within seconds
            pr = pick_pr_for_push(gh_api(f"repos/{CLOUD}/commits/{sha}/pulls"), sha)
            if pr:
                break
            wait(10)
        if not pr:
            raise Refused(f"{sha[:12]} is not the merge of a pull request into main: only merged pull requests are applied.")
        prs = [pr]
    elif a.event == "workflow_dispatch":
        prs = [check_pr_merged(gh_api(f"repos/{CLOUD}/pulls/{n}"), lambda c: repo.is_ancestor(c, sha)) for n in parse_prs(a.prs)]
    else:
        raise Refused(f"Event {a.event!r} never applies.")
    allowed = set()
    for pr in prs:
        allowed.add(pr["merge_commit_sha"])
        allowed.update(c["sha"] for c in gh_api_pages(f"repos/{CLOUD}/pulls/{pr['number']}/commits"))
    newest = max(prs, key=lambda p: p["merged_at"])
    runs = gh_api(f"repos/{CLOUD}/actions/workflows/{os.path.basename(CALLER)}/runs", event="pull_request",
                  head_sha=newest["head"]["sha"], status="success", per_page="50").get("workflow_runs") or []
    plan_run = pick_plan_run(runs, newest)
    if not plan_run:
        raise Refused(f"#{newest['number']} has no successful plan on its last commit {newest['head']['sha'][:12]}.")
    os.makedirs(a.dir, exist_ok=True)
    subprocess.run(["gh", "run", "download", str(plan_run["id"]), "-n", ARTIFACT, "-D", a.dir, "-R", CLOUD], check=True)
    record = load(os.path.join(a.dir, RECORD))
    check_record(record, newest, repo.fingerprint(sha))
    with open(os.path.join(a.dir, "allowed.json"), "w") as f:
        json.dump(sorted(allowed), f)
    numbers = ",".join(str(p["number"]) for p in prs)
    print(f"Applying #{numbers}: #{newest['number']}'s plan run {plan_run['id']} ran on exactly main's bootstrap code; "
          f"{len(record['changes'])} resources change.")
    output(prs=numbers, plan_run=plan_run["id"], record=os.path.join(a.dir, RECORD), allowed=os.path.join(a.dir, "allowed.json"))


def cmd_history(a):
    check_history(Repo(a.repo), applied_commit(a.applied), os.environ["GITHUB_SHA"], set(load(a.allowed)))
    print("Every bootstrap change since the last apply belongs to the pull requests applied now.")


def cmd_compare(a):
    now = plan_summary(load(a.plan_json))
    compare(load(a.expected), now)
    print(f"The plan matches the pull request's: {len(now['changes'])} resources change, digest {now['digest'][:16]}.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--repo", required=True)
    r.add_argument("--plan-json", required=True)
    r.add_argument("--pr", required=True)
    r.add_argument("--head-sha", required=True)
    r.add_argument("--out", default=RECORD)
    u = sub.add_parser("unapplied")
    u.add_argument("--repo", required=True)
    u.add_argument("--applied", default="")
    u.add_argument("--base", default="HEAD^1")
    se = sub.add_parser("self")
    se.add_argument("--dir", required=True)
    o = sub.add_parser("oidc")
    o.add_argument("--kind", required=True, choices=["plan", "apply"])
    o.add_argument("--config", required=True)
    f = sub.add_parser("find")
    f.add_argument("--repo", required=True)
    f.add_argument("--event", required=True)
    f.add_argument("--prs", default="")
    f.add_argument("--owner-id", required=True)
    f.add_argument("--dir", default="bootstrap-plan")
    h = sub.add_parser("history")
    h.add_argument("--repo", required=True)
    h.add_argument("--applied", default="")
    h.add_argument("--allowed", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--expected", required=True)
    c.add_argument("--plan-json", required=True)
    a = p.parse_args(argv)
    try:
        {"record": cmd_record, "unapplied": cmd_unapplied, "self": cmd_self, "oidc": cmd_oidc, "find": cmd_find,
         "history": cmd_history, "compare": cmd_compare}[a.cmd](a)
    except Refused as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
