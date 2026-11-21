import json
import os
import re
import shlex
import time

import yaml

from driver import Driver
import stub_a9


L1 = "ygg/alpha-p1/lane/pm1tl1"
L2 = "ygg/alpha-p1/lane/pm1tl2"
INT = "ygg/alpha-p1/_integration"

SAFE = {
    "enforce_admins": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "required_pull_request_reviews": {"required_approving_review_count": 1},
}


class DriverA9(Driver):
    def run(self, *args, **kwargs):
        if self.is_stub:
            return stub_a9.run()
        return super().run(*args, **kwargs)

    def run_json(self, *args, **kwargs):
        result = self.run(*args, **kwargs)
        if self.is_stub:
            return result, stub_a9.nothing()
        try:
            return result, json.loads(result.stdout)
        except ValueError:
            return result, None

    def core_tree(self):
        if self.is_stub:
            return stub_a9.nothing()
        super().core_tree()
        # A9 adds an origin explicitly only in rows that name one.
        self.git(self.repo_a, "remote", "remove", "origin")

    def remote_fixture(self, *, origin=True):
        if self.is_stub:
            return stub_a9.nothing()
        remote_b = self.make_cwd("remote-b.git")
        self.git(remote_b, "init", "--bare")
        if origin:
            self.git(self.repo_a, "remote", "add", "origin", self.root / "remote-a.git")

    def push_seed(self, *, remote=True, branches=(L1, L2, INT)):
        self.remote_fixture(origin=remote)
        self.ready()
        self.git_labels()
        for branch in branches:
            self.branch(branch, "b2c3d4e")

    def push(self, branch=L1, local="b2c3d4e", remote="a1b2c3d", *,
             url=None, node=None, repo=None, env=None):
        line = " ".join(("refs/heads/" + branch, self.sha(local),
                         "refs/heads/" + branch, self.sha(remote))) + "\n"
        args = ["push-guard", self.repo_a if repo is None else repo, "origin"]
        if url is not None:
            args.append(url)
        call_env = dict(env or {}, YGG_NODE=node)
        return self.run(*args, stdin=line, env=call_env)

    def delete_workflow_commit(self):
        if self.is_stub:
            return stub_a9.nothing()
        self.git(self.repo_a, "checkout", "--detach", self.sha("e5f6a7b"))
        self.git(self.repo_a, "rm", ".github/workflows/ci.yml")
        self.git(self.repo_a, "-c", "core.hooksPath=/dev/null", "commit", "-m", "f6a7b8c")
        self.labels["f6a7b8c"] = self.ref("HEAD")
        self.git(self.repo_a, "checkout", "main")

    def protection_body(self, body):
        self.fake_behavior(gh={
            "protected": {"example/repo-a": True, "example/repo-b": True},
            "protection_bodies": {"example/repo-a": body},
        })

    def protection_alerts(self):
        return [entry for entry in self.state_lines("alerts.jsonl")
                if entry.get("kind") == "ygg-protection-missing"]

    def completed_package(self):
        self.git_labels()
        self.branch(L1, "b2c3d4e")
        self.package(criteria=True, authorise=True)
        self.run("wp", "complete", "--from", "pm1tl1", "--package", "WP-41")
        self.complete_package_review()

    def package_records(self):
        if self.is_stub:
            return stub_a9.entries()
        text = self.tracker_text("projects/alpha-p1/issues/WP-41.md")
        sections = re.split(r"(?m)^## Records\s*$", text, maxsplit=1)
        if len(sections) != 2:
            return []
        records = re.split(r"(?m)^## ", sections[1], maxsplit=1)[0]
        return [yaml.safe_load(block) for block in
                re.findall(r"(?ms)^```yaml\s*\n(.*?)^```\s*$", records)]

    def beta_decision(self):
        self.run("test", "set-next", "decision", "5")
        return self.run("decision", "raise", "--from", "pm2", "--question",
                        "Ship tutorial skip?", "--option", "A", "--option", "B",
                        "--recommended", "A", "--reversible", "no", "--aligns", "yes")

    def launch_settings(self, node):
        if self.is_stub:
            return stub_a9.nothing()
        launches = self.launches(node)
        if not launches:
            return None
        argv = launches[-1]["argv"]
        if "--settings" not in argv:
            return None
        return self.json_file(argv[argv.index("--settings") + 1])

    def expected_hook_command(self):
        if self.is_stub:
            return stub_a9.text()
        return shlex.join([os.path.realpath(self.root / "bin" / "ygg"), "hook", "pretool"])

    def system_path(self):
        return os.defpath

    def corrupt_nodes(self, mode):
        if self.is_stub:
            return stub_a9.nothing()
        path = self.tracker / "projects" / "alpha-p1" / "nodes.json"
        if mode == "malformed":
            self.write(path, "{not json")
        elif mode == "directory":
            self.remove(path)
            path.mkdir()
        else:
            raise ValueError(mode)

    def keep_running(self):
        self.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "persist"}}})

    def a5_receipts(self, node=None):
        if self.is_stub:
            return stub_a9.entries()
        entries = [json.loads(self.read(path)) for path in sorted((self.state / "activations").glob("*.json"))]
        return [entry for entry in entries if node is None or entry.get("node") == node]

    def fake_agent_pid(self, node):
        if self.is_stub:
            return stub_a9.nothing()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipts = self.a5_receipts(node)
            if receipts:
                pid = receipts[-1].get("pid")
                if isinstance(pid, int) and pid > 1:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        return pid
            time.sleep(0.05)
        raise AssertionError("Given requires one live fake agent for " + node)

    def fake_agent_running(self, pid):
        if self.is_stub:
            return stub_a9.nothing()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            time.sleep(0.05)
        return True

    def foreign_worktree(self):
        if self.is_stub:
            return stub_a9.nothing()
        path = self.repo_a / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "clone", self.repo_b, path)

    def outside_file(self, name):
        path = self.root / "outside" / name
        if name == "map.md":
            text = "".join("## " + heading + "\n" for heading in
                           ("Destination", "Frontier", "Blockers", "Decisions so far",
                            "Fog", "Out of scope"))
        else:
            text = "# End of stage 1\n"
        self.write(path, text)
        return path

    def quoted_repo(self):
        repo = self.root / "repo c"
        self.repository(repo)
        self.ready(repo=repo)
        self.git(repo, "branch", "-f", INT, "main")
        return repo
