"""Unit tests for gate.py: fake GitHub answers and plans, throwaway git repos. No AWS, no network.

python3 -I -B -m unittest discover -s . -p 'test_gate.py'
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gate", os.path.join(HERE, "gate.py"))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

OWNER, BOT = "18726574", "258956192"
EMAIL = "owner@example.com"   # synthetic, as the budget email would sit in a plan


def change(address, actions, after=None, unknown=None):
    return {"address": address, "change": {"actions": actions, "after": after, "after_unknown": unknown or {}}}


PLAN = {"resource_changes": [
    change("aws_iam_role.bootstrap", ["create"], {"name": "heyintern-ci-bootstrap"}, {"arn": True}),
    change("aws_sns_topic_subscription.budget_stop_email", ["update"], {"endpoint": EMAIL}),
    change("aws_budgets_budget.monthly", ["no-op"], {"limit_amount": "120.00"}),
    change("data.archive_file.budget_stop", ["read"], {}),
    change("aws_iam_role_policy.old", ["delete"], None),
]}


def pr(number=7, merged=True, base="main", merge_sha="m" * 40, head_sha="h" * 40, ref="a-branch", merged_at="2026-10-25T10:00:00Z"):
    return {"number": number, "merged_at": merged_at if merged else None, "base": {"ref": base},
            "merge_commit_sha": merge_sha, "head": {"sha": head_sha, "ref": ref}}


def run(id_, sha="h" * 40, ref="a-branch", event="pull_request", conclusion="success", path=gate.CALLER, created="2026-10-25T09:00:00Z"):
    return {"id": id_, "head_sha": sha, "head_branch": ref, "event": event, "conclusion": conclusion, "path": path, "created_at": created}


class GitRepo:
    """A throwaway repository shaped like heyintern/cloud."""

    def __init__(self):
        self.path = tempfile.mkdtemp()
        self.g("init", "-q", "-b", "main")
        self.g("config", "user.email", "ci@example.com")
        self.g("config", "user.name", "synthetic")

    def g(self, *a):
        return subprocess.run(["git", "-C", self.path, *a], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, path, msg):
        full = os.path.join(self.path, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a") as f:
            f.write(msg + "\n")
        self.g("add", path)
        self.g("commit", "-q", "-m", msg)
        return self.g("rev-parse", "HEAD")


class Plan(unittest.TestCase):
    def test_only_changes_count_and_no_value_is_shown(self):
        s = gate.plan_summary(PLAN)
        self.assertEqual(sorted(s["changes"]), sorted(["create aws_iam_role.bootstrap", "delete aws_iam_role_policy.old",
                                                       "update aws_sns_topic_subscription.budget_stop_email"]))
        self.assertNotIn(EMAIL, json.dumps(s), "values (the budget email) never leave the runner")

    def test_the_digest_depends_on_every_value_not_on_order(self):
        a = gate.plan_summary(PLAN)
        self.assertEqual(a["digest"], gate.plan_summary({"resource_changes": list(reversed(PLAN["resource_changes"]))})["digest"])
        other = json.loads(json.dumps(PLAN))
        other["resource_changes"][1]["change"]["after"]["endpoint"] = "other@example.com"
        self.assertNotEqual(gate.plan_summary(other)["digest"], a["digest"], "another budget email is another plan")

    def test_compare_refuses_another_plan(self):
        expected = gate.plan_summary(PLAN)
        gate.compare(expected, gate.plan_summary(PLAN))
        more = json.loads(json.dumps(PLAN))
        more["resource_changes"].append(change("aws_iam_user.intruder", ["create"], {"name": "x"}))
        with self.assertRaises(gate.Refused) as e:
            gate.compare(expected, gate.plan_summary(more))
        self.assertIn("only in the plan now: create aws_iam_user.intruder", str(e.exception))


class Caller(unittest.TestCase):
    def test_only_heyintern_clouds_bootstrap_yml_calls_the_gate(self):
        ok = {"GITHUB_REPOSITORY": "heyintern/cloud", "GITHUB_REF": "refs/heads/main",
              "GITHUB_WORKFLOW_REF": "heyintern/cloud/.github/workflows/bootstrap.yml@refs/heads/main"}
        gate.check_caller(ok, "apply")
        for bad in ({**ok, "GITHUB_REPOSITORY": "spud-ai/cloud"},
                    {**ok, "GITHUB_WORKFLOW_REF": "heyintern/cloud/.github/workflows/evil.yml@refs/heads/main"},
                    {**ok, "GITHUB_WORKFLOW_REF": "heyintern/cloud/.github/workflows/bootstrap.yml@refs/heads/other"},
                    {**ok, "GITHUB_REF": "refs/heads/other"}):
            with self.assertRaises(gate.Refused):
                gate.check_caller(bad, "apply")
        gate.check_caller({**ok, "GITHUB_WORKFLOW_REF": "heyintern/cloud/.github/workflows/bootstrap.yml@refs/pull/14/merge"}, "plan")
        with self.assertRaises(gate.Refused):
            gate.check_caller({**ok, "GITHUB_WORKFLOW_REF": "heyintern/cloud/.github/workflows/evil.yml@refs/pull/14/merge"}, "plan")

    def test_only_the_owner_starts_an_apply_even_a_rerun(self):
        self.assertTrue(gate.owner_started({"actor": {"id": int(OWNER)}, "triggering_actor": {"id": int(OWNER)}}, OWNER))
        self.assertFalse(gate.owner_started({"actor": {"id": int(OWNER)}, "triggering_actor": {"id": int(BOT)}}, OWNER))
        self.assertFalse(gate.owner_started({"actor": {"id": int(BOT)}, "triggering_actor": {"id": int(BOT)}}, OWNER))

    def test_the_gate_runs_its_own_commit_only(self):
        good = {"job_workflow_ref": gate.GATE_WORKFLOW, "job_workflow_sha": "g" * 40}
        gate.check_self(good, "g" * 40)
        for claims, head in (({**good, "job_workflow_sha": "n" * 40}, "g" * 40),
                             ({**good, "job_workflow_ref": "spud-ai/heyintern-gate/.github/workflows/bootstrap.yml@refs/heads/main"}, "g" * 40)):
            with self.assertRaises(gate.Refused):
                gate.check_self(claims, head)

    def test_expected_subjects_name_the_gate_file_and_the_owner(self):
        cfg = {"repository_owner_id": "346177048", "repository_id": "1430717248", "bootstrap_actors": {"colegottdank": OWNER},
               "bootstrap_gate": {"repository": "colegottdank/heyintern-gate", "workflow": ".github/workflows/bootstrap.yml", "ref": "refs/heads/main"}}
        apply = gate.expected_subjects(cfg, "apply")
        self.assertEqual(len(apply), 2)
        self.assertTrue(all("job_workflow_ref:colegottdank/heyintern-gate/.github/workflows/bootstrap.yml@refs/heads/main" in s and s.endswith(OWNER) for s in apply))
        self.assertTrue(gate.expected_subjects(cfg, "plan")[0].endswith(":event_name:pull_request:actor_id:*"))


class PullRequests(unittest.TestCase):
    def test_a_push_applies_only_the_pull_request_it_merged_into_main(self):
        self.assertEqual(gate.pick_pr_for_push([pr(1, merged=False), pr(2, base="other"), pr(3, merge_sha="x" * 40), pr(4)], "m" * 40)["number"], 4)
        self.assertIsNone(gate.pick_pr_for_push([pr(3, merge_sha="x" * 40)], "m" * 40), "a direct push has none")

    def test_a_manual_run_names_merged_pull_requests(self):
        self.assertEqual(gate.parse_prs("12, 14 12"), [12, 14])
        for bad in ("", "12;rm", "#12"):
            with self.assertRaises(gate.Refused):
                gate.parse_prs(bad)
        gate.check_pr_merged(pr(5), lambda c: True)
        for bad, anc in ((pr(5, merged=False), True), (pr(5, base="other"), True), (pr(5), False)):
            with self.assertRaises(gate.Refused):
                gate.check_pr_merged(bad, lambda c, anc=anc: anc)

    def test_the_plan_run_is_the_latest_successful_one_of_the_caller_on_the_last_commit(self):
        runs = [run(1, created="2026-10-25T08:00:00Z"), run(2, created="2026-10-25T09:00:00Z"),
                run(3, conclusion="failure", created="2026-10-25T09:30:00Z"), run(4, sha="o" * 40),
                run(5, event="push"), run(6, path=".github/workflows/evil.yml", created="2026-10-25T09:59:00Z"), run(7, ref="other")]
        self.assertEqual(gate.pick_plan_run(runs, pr())["id"], 2)

    def test_the_record_must_be_this_pull_requests_on_exactly_this_code(self):
        code = {p: f"obj-{i}" for i, p in enumerate(gate.PATHS)}
        record = {"pr": 7, "head_sha": "h" * 40, "code": dict(code)}
        gate.check_record(record, pr(), dict(code))
        for bad in ({**record, "pr": 8}, {**record, "head_sha": "o" * 40}):
            with self.assertRaises(gate.Refused):
                gate.check_record(bad, pr(), dict(code))
        with self.assertRaises(gate.Refused) as e:
            gate.check_record(record, pr(), {**code, "infra/bootstrap": "pushed-after-the-plan"})
        self.assertIn("infra/bootstrap", str(e.exception))


class History(unittest.TestCase):
    """Nothing reaches AWS that the owner did not merge: a direct push since the last apply stops it."""

    def setUp(self):
        self.r = GitRepo()
        self.repo = gate.Repo(self.r.path)
        self.applied = self.r.commit("infra/bootstrap/main.tf", "applied")

    def test_the_merged_pull_request_alone_goes_ahead(self):
        self.r.commit("README.md", "not bootstrap")
        merged = self.r.commit("infra/bootstrap/budget.tf", "the pull request (squash)")
        gate.check_history(self.repo, self.applied, merged, {merged})

    def test_a_direct_push_since_the_last_apply_stops_it(self):
        pushed = self.r.commit("infra/bootstrap/operator.tf", "the bot, straight to main")
        merged = self.r.commit("infra/bootstrap/budget.tf", "the pull request")
        with self.assertRaises(gate.Refused) as e:
            gate.check_history(self.repo, self.applied, merged, {merged})
        self.assertIn("the bot, straight to main", str(e.exception))
        gate.check_history(self.repo, self.applied, merged, {merged, pushed})   # named by the owner (a merged pull request)

    def test_a_changed_caller_workflow_counts_as_a_bootstrap_change(self):
        pushed = self.r.commit(".github/workflows/bootstrap.yml", "the bot rewires the caller")
        merged = self.r.commit("infra/bootstrap/budget.tf", "the pull request")
        with self.assertRaises(gate.Refused):
            gate.check_history(self.repo, self.applied, merged, {merged})
        self.assertTrue(pushed)

    def test_a_merge_commit_brings_only_its_pull_requests_commits(self):
        self.r.g("checkout", "-q", "-b", "feature")
        c1 = self.r.commit("infra/bootstrap/a.tf", "feature 1")
        self.r.g("checkout", "-q", "main")
        sneaky = self.r.commit("infra/bootstrap/b.tf", "pushed to main meanwhile")
        self.r.g("merge", "-q", "--no-ff", "-m", "Merge pull request #7", "feature")
        merge = self.r.g("rev-parse", "HEAD")
        with self.assertRaises(gate.Refused):
            gate.check_history(self.repo, self.applied, merge, {merge, c1})
        gate.check_history(self.repo, self.applied, merge, {merge, c1, sneaky})

    def test_no_record_or_a_rewritten_main_stops_it(self):
        head = self.r.commit("infra/bootstrap/budget.tf", "x")
        with self.assertRaises(gate.Refused):
            gate.check_history(self.repo, "", head, {head})
        with self.assertRaises(gate.Refused):
            gate.check_history(self.repo, "0" * 40, head, {head})

    def test_unapplied_lists_what_main_has_since_the_last_apply(self):
        self.r.commit("README.md", "not bootstrap")
        self.r.commit("infra/bootstrap/budget.tf", "pushed to main")
        commits, note = gate.unapplied(self.repo, self.applied, "HEAD")
        self.assertIsNone(note)
        self.assertEqual([c.split(": ", 1)[1] for c in commits], ["pushed to main"])
        self.assertIn("No record", gate.unapplied(self.repo, "", "HEAD")[1])


class Find(unittest.TestCase):
    """cmd_find end to end, with GitHub and the artifact download faked."""

    def setUp(self):
        self.r = GitRepo()
        self.r.commit("infra/bootstrap/main.tf", "base")
        self.r.commit(".github/workflows/bootstrap.yml", "caller")
        self.sha = self.r.g("rev-parse", "HEAD")
        self.tmp = tempfile.mkdtemp()
        self.saved = (gate.gh_api, gate.gh_api_pages, gate.subprocess.run, dict(os.environ), gate.wait)
        self.addCleanup(self.restore)
        os.environ.update(GITHUB_REPOSITORY="heyintern/cloud", GITHUB_SHA=self.sha, GITHUB_RUN_ID="99", GITHUB_REF="refs/heads/main",
                          GITHUB_WORKFLOW_REF="heyintern/cloud/.github/workflows/bootstrap.yml@refs/heads/main",
                          GITHUB_OUTPUT=os.path.join(self.tmp, "out"))
        the_pr = pr(merge_sha=self.sha)
        self.answers = {
            "repos/heyintern/cloud/actions/runs/99": {"actor": {"id": int(OWNER)}, "triggering_actor": {"id": int(OWNER)}},
            f"repos/heyintern/cloud/commits/{self.sha}/pulls": [the_pr],
            "repos/heyintern/cloud/pulls/7": the_pr,
            "repos/heyintern/cloud/actions/workflows/bootstrap.yml/runs": {"workflow_runs": [run(42)]},
        }
        self.record = {"pr": 7, "head_sha": "h" * 40, "code": gate.Repo(self.r.path).fingerprint(self.sha),
                       "changes": ["create aws_iam_role.bootstrap"], "digest": "d"}
        gate.gh_api = lambda path, **params: self.answers[path]
        gate.gh_api_pages = lambda path: [{"sha": "c" * 40}]
        gate.wait = lambda s: None
        real_run = self.saved[2]

        def fake_run(args, **kw):
            if args[:3] == ["gh", "run", "download"]:
                assert args[3] == "42" and args[4:6] == ["-n", "bootstrap-plan"], args
                with open(os.path.join(args[args.index("-D") + 1], "gate.json"), "w") as f:
                    json.dump(self.record, f)
                return subprocess.CompletedProcess(args, 0)
            return real_run(args, **kw)
        gate.subprocess.run = fake_run

    def restore(self):
        gate.gh_api, gate.gh_api_pages, gate.subprocess.run = self.saved[:3]
        gate.wait = self.saved[4]
        os.environ.clear()
        os.environ.update(self.saved[3])

    def find(self, event="push", prs=""):
        return gate.main(["find", "--repo", self.r.path, "--event", event, "--prs", prs, "--owner-id", OWNER,
                          "--dir", os.path.join(self.tmp, "plan")])

    def test_the_owners_merge_of_a_planned_pull_request_goes_ahead(self):
        self.assertEqual(self.find(), 0)
        with open(os.environ["GITHUB_OUTPUT"]) as f:
            out = f.read()
        self.assertIn("prs=7\n", out)
        with open(os.path.join(self.tmp, "plan", "allowed.json")) as f:
            self.assertEqual(set(json.load(f)), {self.sha, "c" * 40})

    def test_a_manual_run_names_the_pull_requests(self):
        self.assertEqual(self.find("workflow_dispatch", "7"), 0)
        self.assertEqual(self.find("workflow_dispatch", ""), 1)

    def test_code_changed_after_the_plan_stops_it(self):
        self.record["code"]["infra/bootstrap"] = "changed-after-the-plan"
        self.assertEqual(self.find(), 1)

    def test_a_rerun_by_the_bot_another_caller_or_a_direct_push_stops_it(self):
        self.answers["repos/heyintern/cloud/actions/runs/99"]["triggering_actor"] = {"id": int(BOT)}
        self.assertEqual(self.find(), 1)
        self.answers["repos/heyintern/cloud/actions/runs/99"]["triggering_actor"] = {"id": int(OWNER)}
        os.environ["GITHUB_WORKFLOW_REF"] = "heyintern/cloud/.github/workflows/evil.yml@refs/heads/main"
        self.assertEqual(self.find(), 1)
        os.environ["GITHUB_WORKFLOW_REF"] = "heyintern/cloud/.github/workflows/bootstrap.yml@refs/heads/main"
        self.answers[f"repos/heyintern/cloud/commits/{self.sha}/pulls"] = []
        self.assertEqual(self.find(), 1)
        self.assertEqual(self.find("schedule"), 1)


if __name__ == "__main__":
    unittest.main()
