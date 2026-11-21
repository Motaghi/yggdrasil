"""All subprocess, environment, fake, and filesystem boundaries live here."""

import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime

import yaml

import stub


class Driver:
    def __init__(self, root):
        self.root = Path(root)
        self.state = self.root / "state"
        self.tracker = self.root / "tracker"
        self.platform = self.root / "platform"
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.cwd = self.root / "work" / "cwd"
        self.log = self.root / "fake-calls.jsonl"
        self.fake_config = self.root / "fake-config.json"
        self.load = self.root / "load"
        self.processes = []
        self.permission_paths = []
        self.labels = {}
        self.core = Path(os.environ["YGG_CORE_ROOT"]) if os.environ.get("YGG_CORE_ROOT") else None
        self.real_profile = os.environ.get("YGG_PROFILE")
        self.denylist = os.environ.get("YGG_LEAK_DENYLIST")
        self.cli = os.environ.get("YGG_BIN", "ygg")
        target = os.environ.get("ACCEPTANCE_TARGET", "real")
        if target not in {"real", "stub"}:
            raise ValueError("ACCEPTANCE_TARGET must be real or stub")
        self.is_stub = target == "stub"
        self.env = {
            "PATH": str(self.root / "bin") + os.pathsep + os.defpath,
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "YGG_STATE_ROOT": str(self.state),
            "YGG_FILES_ROOT": str(self.tracker),
            "YGG_PLATFORM_STATE": str(self.platform),
            "YGG_TRACKER": "files",
            "YGG_TEST_MODE": "1",
            "YGG_HOST_LOAD_FILE": str(self.load),
            "YGG_T3_BIN": str(self.root / "missing-t3"),
            "YGG_CLAUDE_BIN": str(self.root / "bin" / "claude"),
            "YGG_CODEX_BIN": str(self.root / "bin" / "codex"),
            "YGG_GH_BIN": str(self.root / "bin" / "gh"),
            "YGG_FAKE_LOG": str(self.log),
            "YGG_FAKE_CONFIG": str(self.fake_config),
        }
        if not self.is_stub:
            resolved = shutil.which(self.cli)
            if resolved:
                self.cli = resolved
            elif os.path.sep not in self.cli:
                self.cli = str(self.root / "missing-ygg")
        if os.environ.get("YGG_RELEASE_FILE"):
            self.env["YGG_RELEASE_FILE"] = os.environ["YGG_RELEASE_FILE"]
        self._tool_sequence = 0
        self._prepare()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if hasattr(self, "_delay_cancel"):
            self._delay_cancel.set()
            self.run("stop", self._delayed_node)
            for reached in self._delay_directory.glob("*.before-gate.reached"):
                reached.with_suffix(".continue").touch()
            self._delay_pool.shutdown(wait=True)
        self.cleanup_processes()
        self.restore_permissions()

    def _prepare(self):
        if self.is_stub:
            return stub.nothing()
        for path in (self.state, self.tracker, self.platform, self.cwd,
                     self.root / "bin", self.root / "home", self.root / "tmp"):
            path.mkdir(parents=True, exist_ok=True)
        fake = (Path(__file__).parent / "fakes" / "binary.py").read_text()
        for name in ("t3", "claude", "codex", "gh", "osascript", "open", "phone-push"):
            destination = self.root / "bin" / name
            destination.write_text("#!" + sys.executable + "\n" + fake)
            destination.chmod(0o755)
        self.write(self.fake_config, json.dumps({"gh": {"protected": {
            "example/repo-a": True, "example/repo-b": True}}}))
        # Scripted agents call the same executable selected by YGG_BIN.
        wrapper = self.root / "bin" / "ygg"
        import shlex
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(self.cli) + ' "$@"\n')
        wrapper.chmod(0o755)
        self.set_load("0.10")

    def run(self, *args, env=None, stdin=None, cwd=None, timeout=None):
        crash_point = self.__dict__.pop('_next_crash_point', None)
        if crash_point is not None:
            env = dict(env or {}, YGG_FAULT_AT='boundary:' + crash_point,
                       YGG_FAULT_TRACE=None)
        if self.is_stub:
            return stub.run()
        if timeout is None:
            timeout = 180 if args[:2] == ("conductor", "tick") else 30
        call_env = self.env.copy()
        for key, value in (env or {}).items():
            if value is None:
                call_env.pop(key, None)
            else:
                call_env[key] = str(value)
        try:
            return subprocess.run(
                [self.cli, *map(str, args)], input=stdin, text=True,
                capture_output=True, cwd=cwd or self.cwd, env=call_env,
                timeout=timeout, check=False,
            )
        except FileNotFoundError as error:
            return subprocess.CompletedProcess([], 127, "", str(error))

    def command_json(self, *args, **kwargs):
        result = self.run(*args, **kwargs)
        if self.is_stub:
            return stub.nothing()
        try:
            return json.loads(result.stdout)
        except ValueError:
            return None

    def status(self, **kwargs):
        return self.command_json("status", "--json", **kwargs)

    def pending(self, **kwargs):
        return self.command_json("pending", "--json", **kwargs)

    def write(self, path, text):
        if self.is_stub:
            return stub.nothing()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def read(self, path):
        if self.is_stub:
            return stub.text()
        try:
            return Path(path).read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def state_text(self, relative):
        return self.read(self.state / relative)

    def tracker_text(self, relative):
        return self.read(self.tracker / relative)

    def tracker_json(self, relative):
        value = self.tracker_text(relative)
        if self.is_stub:
            return stub.nothing()
        try:
            return json.loads(value)
        except ValueError:
            return None

    def documents(self, relative):
        if self.is_stub:
            return stub.entries()
        directory = self.tracker / relative
        return [(str(path.relative_to(self.tracker)), path.read_text())
                for path in sorted(directory.rglob("*.md"))]

    def state_lines(self, relative):
        text = self.state_text(relative)
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def snapshot(self, scope="tracker"):
        if self.is_stub:
            return stub.snapshot()
        directory = self.tracker if scope == "tracker" else self.root
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()
                and not (scope == "outside-tracker" and path.is_relative_to(self.tracker))}

    def set_load(self, value, *, now=None, age_seconds=0):
        if self.is_stub:
            return stub.nothing()
        self.write(self.load, value)
        timestamp = datetime.fromisoformat(now).timestamp() if now else time.time()
        os.utime(self.load, (timestamp - age_seconds, timestamp - age_seconds))

    def hook(self, payload, *, node=None, cwd=None):
        payload, environment, directory = self._hook_payload(payload, node, cwd)
        return self.run("hook", "pretool", env=environment,
                        stdin=json.dumps(payload), cwd=directory)

    def _hook_payload(self, payload, node, cwd):
        self._tool_sequence += 1
        launches = self.launches(node) if node else []
        launch = launches[-1] if launches else {}
        directory = cwd or launch.get("cwd") or self.cwd
        receipt = next((entry for entry in reversed(self.a5_receipts(node)) if entry), {}) if node else {}
        defaults = {"cwd": str(directory), "tool_use_id": "tu-" + str(self._tool_sequence),
                    "session_id": "s-1", "permission_mode": "dontAsk",
                    "transcript_path": str(self.root / "transcript.jsonl")}
        defaults.update(payload)
        environment = {"YGG_NODE": node,
                       "YGG_ACTIVATION": launch.get("env", {}).get("YGG_ACTIVATION")
                       or receipt.get("id") or receipt.get("activation")}
        return defaults, environment, directory

    def posttool(self, payload, *, node=None, cwd=None):
        payload, environment, directory = self._hook_payload(payload, node, cwd)
        if "error" not in payload and "tool_response" not in payload:
            payload["tool_response"] = {"interrupted": False, "isImage": False,
                                        "noOutputExpected": False, "stderr": "", "stdout": ""}
        if "error" in payload:
            payload.setdefault("is_interrupt", False)
        return self.run("hook", "posttool", env=environment,
                        stdin=json.dumps(payload), cwd=directory)

    def trusted_git(self, directory, *args):
        if self.is_stub:
            return stub.run()
        return subprocess.run(["git", "-C", str(directory), "-c", "core.hooksPath=/dev/null",
                               *map(str, args)], text=True, capture_output=True,
                              env=self.env, check=False, timeout=30)

    def raise_alert(self, kind, **fields):
        arguments = ["test", "alert", kind]
        for key, value in fields.items():
            arguments.extend(["--" + key.replace("_", "-"),
                              str(value).lower() if isinstance(value, bool) else str(value)])
        return self.run(*arguments)

    def stamp_output(self, activation, seconds_before_now):
        if self.is_stub:
            return stub.nothing()
        path = self.state / "output" / activation / "stdout.log"
        if not path.exists():
            self.write(path, "")
        timestamp = datetime.fromisoformat(self.env["YGG_NOW"].replace("Z", "+00:00")).timestamp()
        os.utime(path, (timestamp - seconds_before_now, timestamp - seconds_before_now))

    def seed_acceptance(self, project, package, sha):
        return self.run("test", "acceptance", project, package, sha)

    def seed_cancel(self, project, package):
        return self.run("test", "cancel", project, package)

    def crash_at(self, point):
        self._next_crash_point = point

    def release_off(self, switch):
        switches = self.env.get("YGG_TEST_RELEASE_OFF", "").split(",")
        self.env["YGG_TEST_RELEASE_OFF"] = ",".join(dict.fromkeys([s for s in switches if s] + [switch]))

    def start_delay(self, seconds, node="pm1tl1"):
        if self.is_stub:
            return stub.nothing()
        directory = self.make_cwd("start-delay")
        self._delayed_node = node
        self._delay_directory = directory
        self.env.update(YGG_TEST_BARRIER=node + ":before-gate", YGG_TEST_BARRIER_DIR=str(directory))
        self._delay_cancel = threading.Event()
        def release():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not self._delay_cancel.is_set():
                reached = list(directory.glob("*.before-gate.reached"))
                if reached:
                    if self._delay_cancel.wait(seconds):
                        return
                    reached[0].with_suffix(".continue").touch()
                    return
                time.sleep(0.05)
        self._delay_pool = ThreadPoolExecutor(max_workers=1)
        self._delay_pool.submit(release)

    def make_cwd(self, relative):
        path = self.root / relative
        if self.is_stub:
            stub.nothing()
        else:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _git(self, directory, *args):
        if self.is_stub:
            return stub.run()
        return subprocess.run(["git", "-C", str(directory), *map(str, args)],
                              text=True, capture_output=True, env=self.env,
                              check=True, timeout=30)

    def _repo(self, path):
        if self.is_stub:
            return stub.nothing()
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init", "-b", "main")
        self._git(path, "config", "user.name", "acceptance")
        self._git(path, "config", "user.email", "acceptance@example.invalid")
        self._git(path, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "fixture")

    def domain_repositories(self):
        self._repo(self.repo_a)
        self._repo(self.repo_b)

    def at(self, timestamp):
        self.env["YGG_NOW"] = timestamp
        self.set_load("0.10", now=timestamp)

    def core_tree(self):
        if self.is_stub:
            return stub.nothing()
        self._repo(self.repo_a)
        self._repo(self.repo_b)
        remote = self.root / "remote-a.git"
        remote.mkdir()
        self._git(remote, "init", "--bare")
        self._git(self.repo_a, "remote", "add", "origin", remote)
        self.run("programme", "create", "alpha", "--domain", "alpha", "--home", self.repo_a, "--kind", "git-repo")
        self.run("programme", "create", "beta", "--domain", "beta", "--home", self.repo_b, "--kind", "git-repo")
        self.frozen_agent_fixture()
        for project, programme, pm in (("alpha-p1", "alpha", "pm1"),
                                       ("beta-p1", "beta", "pm2"),
                                       ("alpha-p2", "alpha", "pm3")):
            self.run("project", "create", project, "--programme", programme, "--pm", pm)
        for node, role, parent, pair in (
            ("pm1tl1", "tl", "pm1", None), ("pm1tl2", "tl", "pm1", None),
            ("pm1su1", "su", "pm1", "pm1tl1"), ("pm1su2", "su", "pm1", "pm1tl2"),
            ("pm1tl1w1", "worker", "pm1tl1", None), ("pm1tl1w2", "worker", "pm1tl1", None),
            ("pm1tl2w1", "worker", "pm1tl2", None),
            ("pm1su1w1", "worker", "pm1su1", None), ("pm1su1w2", "worker", "pm1su1", None),
            ("pm2tl1", "tl", "pm2", None), ("pm2su1", "su", "pm2", "pm2tl1"),
        ):
            args = ["node", "add", node, "--role", role, "--parent", parent]
            if pair:
                args += ["--pairs-with", pair]
            self.run(*args)
        self.pid()

    def package(self, package="WP-41", lane="pm1tl1", *, criteria=False, authorise=False):
        status = self.status() or {}
        stage = next((project["stage"]["number"]
                      for project in status.get("projects", [])
                      if project["id"] == "alpha-p1"
                      and project.get("stage")
                      and project["stage"]["state"] == "executing"), 1)
        self.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                 "--stage", str(stage), "--kind", "stage-plan", "--body", "Stage " + str(stage) + " plan")
        self.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                 "--stage", str(stage), "--package", package, "--kind", "work-package",
                 "--body", "Save module split")
        if criteria:
            senior = "pm1su1" if lane == "pm1tl1" else "pm1su2"
            self.run("tracker", "write", "--as", senior, "--project", "alpha-p1",
                     "--package", package, "--kind", "criteria", "--body", package)
        if authorise:
            self.run("wp", "authorise", "alpha-p1", "--package", package, "--lane", lane)

    def quiesce(self):
        if self.is_stub:
            return stub.nothing()
        for _ in range(20):
            self.set_load("0.10")
            self.run("conductor", "tick")
            status = self.status()
            if not isinstance(status, dict):
                return
            if not any(node.get("state") in {"queued", "running"} for node in status.get("nodes", [])):
                return
            time.sleep(0.05)

    def clear_calls(self):
        return self.write(self.log, "")

    def calls(self, binary=None):
        if self.is_stub:
            return stub.entries()
        calls = [json.loads(line) for line in self.read(self.log).splitlines() if line.strip()]
        return [call for call in calls if binary is None or call["binary"] == binary]

    def launches(self, node=None):
        calls = self.calls()
        return [call for call in calls if call["binary"] in {"claude", "codex"}
                and call.get("event", "launch") == "launch"
                and (node is None or call["env"].get("YGG_NODE") == node)]

    def fake_behavior(self, **config):
        config.setdefault("gh", {"protected": {"example/repo-a": True, "example/repo-b": True}})
        if getattr(self, "_frozen_reviewer", False):
            script = Path(__file__).parent / "fakes" / "approve_review.py"
            reviewer = {"mode": "script", "steps": [{"id": "approve-review",
                "run": [sys.executable, str(script)]}]}
            for binary in ("claude", "codex"):
                config.setdefault(binary, {}).setdefault("agents", {}).setdefault("pm1rv1", reviewer)
        return self.write(self.fake_config, json.dumps(config))

    def active_envelope(self):
        self.env["YGG_NOW"] = "2026-11-16T02:00:00+01:00"
        self.set_load("0.10", now=self.env["YGG_NOW"])
        self.run("envelope", "grant", "alpha-p1", "--until", "2026-11-16T08:00+01:00")

    def restore_permissions(self):
        if self.is_stub:
            return stub.nothing()
        for path, mode in reversed(self.permission_paths):
            if path.exists():
                path.chmod(mode)
        self.tracker.chmod(0o755)
        for path in self.tracker.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)

    def pid_body(self, project="alpha-p1", programme="alpha", repo=None, *, omit=(), **overrides):
        repo = self.repo_a if repo is None else repo
        data = {
            "schema": "ygg.pid.v1", "project": project, "programme": programme,
            "repos": [] if repo is False else [{"path": str(repo),
                "github": "example/repo-b" if programme == "beta" else "example/repo-a"}],
            "stages": [{"number": n, "name": name, "planned_end": end}
                       for n, name, end in (
                           (1, "Foundations", "2026-11-16T17:00:00Z"),
                           (2, "Build", "2026-11-17T17:00:00Z"),
                           (3, "Stage 3", "2026-11-17T17:00:00Z"),
                           (4, "Stage 4", "2026-11-17T17:00:00Z"))],
            "tolerances": {"time": "P1D"},
            "business_case": {
                "why": "text", "benefit": "text",
                "cost": {"elapsed": "P3D", "user_minutes": 60},
                "risks": ["text"], "stop_if": "text",
            },
        }
        data.update(overrides)
        for name in omit:
            data.pop(name, None)
        return "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n# PID\n"

    def pid(self, project="alpha-p1", pm="pm1", programme="alpha", repo=None, **overrides):
        body = self.pid_body(project, programme, repo, **overrides)
        self.run("tracker", "write", "--as", pm, "--project", project, "--kind", "pid", "--body", body)
        return body

    def ready(self, project="alpha-p1", **overrides):
        if project == "beta-p1":
            self.pid(project, "pm2", "beta", self.repo_b, **overrides)
        else:
            self.pid(project, **overrides)
        return self.run("pid", "approve", project)

    def stage_executing(self, number):
        self.ready()
        for stage in range(1, number):
            self.report(stage)
            self.run("stage", "end", "alpha-p1", "--within-tolerance", "yes",
                     "--report", "end-stage-" + str(stage) + ".md")
            self.run("stage", "approve", "alpha-p1")

    def report(self, number):
        self.write(self.cwd / ("end-stage-" + str(number) + ".md"), "# End of stage " + str(number) + "\n")

    def json_file(self, path):
        if self.is_stub:
            return stub.nothing()
        try:
            return json.loads(self.read(path))
        except ValueError:
            return None

    def json_records(self, relative):
        if self.is_stub:
            return stub.entries()
        return [self.json_file(path) for path in sorted((self.tracker / relative).glob("*.json"))]

    def write_yaml(self, path, data):
        self.write(path, yaml.safe_dump(data, sort_keys=False))

    def change_json(self, path, **changes):
        if self.is_stub:
            return stub.nothing()
        data = self.json_file(path)
        if not isinstance(data, dict):
            raise AssertionError("Fixture JSON missing at " + str(path))
        data.update(changes)
        self.write(path, json.dumps(data))

    def append(self, path, text):
        self.write(path, self.read(path) + text)

    def set_header(self, path, **changes):
        if self.is_stub:
            return stub.nothing()
        text = self.read(path)
        match = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.S)
        if not match:
            raise AssertionError("Fixture frontmatter missing at " + str(path))
        data = yaml.safe_load(match[1])
        data.update(changes)
        self.write(path, "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n" + match[2])

    def empty_state(self):
        if self.is_stub:
            return stub.nothing()
        shutil.rmtree(self.state)
        self.state.mkdir()

    def route_map(self, project="alpha-p1", pm="pm1", frontier=None, blockers=(), forecast=None):
        header = {"schema": "ygg.routemap.v1", "project": project, "standard_version": 1}
        if forecast:
            header["forecast_end"] = [{"stage": 2, "date": forecast}]
        sections = {"Destination": [], "Frontier": frontier or [], "Blockers": blockers,
                    "Decisions so far": [], "Fog": [], "Out of scope": []}
        body = "---\n" + yaml.safe_dump(header, sort_keys=False) + "---\n"
        for heading, lines in sections.items():
            body += "## " + heading + "\n" + "".join("- " + line + "\n" for line in lines)
        path = self.cwd / "route-map.md"
        self.write(path, body)
        return self.run("routemap", "write", "--as", pm, "--project", project, "--file", path)

    def decision(self, *, number=None, scope=None, question="Ship tutorial skip?", reversible="yes"):
        if number:
            self.run("test", "set-next", "decision", str(number))
        args = ["decision", "raise", "--from", "pm1", "--question", question,
                "--option", "A", "--option", "B", "--recommended", "A",
                "--reversible", reversible, "--aligns", "yes"]
        if reversible == "yes":
            args += ["--undo", "revert commit"]
        if scope:
            args += ["--scope", scope]
        return self.run(*args)

    def seed_activation(self, node, *args):
        return self.run("test", "activation", node, *args)

    def git(self, directory, *args):
        return self._git(directory, *args)

    def repository(self, path):
        return self._repo(path)

    def ref(self, branch, repo=None):
        return self._git(repo or self.repo_a, "rev-parse", branch).stdout.strip()

    def branch(self, branch, label="a1b2c3d", repo=None):
        self._git(repo or self.repo_a, "branch", "-f", branch, self.sha(label))

    def sha(self, label):
        if label == "0000000":
            return "0" * 40
        if self.is_stub:
            return stub.text()
        return self.labels[label]

    def symbolic(self, text):
        for label, sha in self.labels.items():
            text = text.replace(sha, label)
        return text

    def commit_label(self, label, parent="a1b2c3d", *, workflow=False):
        if self.is_stub:
            return stub.text()
        self._git(self.repo_a, "checkout", "--detach", self.sha(parent))
        if workflow:
            self.write(self.repo_a / ".github" / "workflows" / "ci.yml", "name: fixture\non: push\njobs: {}\n")
            self._git(self.repo_a, "add", ".github/workflows/ci.yml")
        self._git(self.repo_a, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", label)
        self.labels[label] = self.ref("HEAD")
        self._git(self.repo_a, "checkout", "main")
        return self.labels[label]

    def git_labels(self):
        if self.is_stub:
            return stub.nothing()
        self.labels["a1b2c3d"] = self.ref("main")
        for label in ("b2c3d4e", "c3d4e5f", "d4e5f6a", "9f8e7d6"):
            self.commit_label(label)
        self.commit_label("e5f6a7b", "b2c3d4e", workflow=True)

    def advance_label(self, label, parent):
        return self.commit_label(label, parent)

    def push_guard(self, local, local_label, remote, remote_label):
        line = " ".join([local, self.sha(local_label), remote, self.sha(remote_label)]) + "\n"
        return self.run("push-guard", self.repo_a, "origin", self.root / "remote-a.git", stdin=line)

    def refs(self):
        if self.is_stub:
            return stub.snapshot()
        result = self._git(self.repo_a, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
        return dict(line.split() for line in result.stdout.splitlines())

    def same_path(self, left, right):
        if self.is_stub:
            return stub.nothing()
        return Path(left).resolve() == Path(right).resolve()

    def beneath(self, path, directory):
        if self.is_stub:
            return stub.nothing()
        return Path(path).resolve().is_relative_to(Path(directory).resolve())

    def readonly(self, path):
        if self.is_stub:
            return stub.nothing()
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        paths = [path, *path.rglob("*")]
        for entry in paths:
            mode = entry.stat().st_mode & 0o777
            self.permission_paths.append((entry, mode))
            entry.chmod(mode & ~0o222)

    def exit_agent(self, node):
        self.write(self.root / "fake-control" / node / "exit", "")
        self.wait_exit(node)

    def wait_exit(self, node, timeout=10):
        if self.is_stub:
            return stub.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            receipts = [self.json_file(p) for p in (self.state / "activations").glob("*.json")]
            selected = [r for r in receipts if r and r.get("node") == node]
            if selected and all(r.get("exited_at") for r in selected):
                return
            time.sleep(0.05)

    def wait_file(self, path, timeout=5):
        if self.is_stub:
            return stub.text()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(path).exists():
                return self.read(path)
            time.sleep(0.05)
        return ""

    @contextmanager
    def conductor(self):
        if self.is_stub:
            yield stub.nothing()
            return
        process = subprocess.Popen([self.cli, "conductor", "run"], cwd=self.cwd,
                                   env=self.env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(process)
        try:
            self.wait_file(self.state / "conductor.lock")
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

    def concurrent(self, *commands):
        if self.is_stub:
            return [stub.run() for command in commands]
        import threading
        barrier = threading.Barrier(len(commands))
        def invoke(command):
            barrier.wait(timeout=5)
            return self.run(*command)
        with ThreadPoolExecutor(max_workers=len(commands)) as pool:
            return list(pool.map(invoke, commands))

    def cleanup_processes(self):
        if self.is_stub:
            return stub.nothing()
        for receipt in (self.state / "activations").glob("*.json"):
            data = self.json_file(receipt)
            pid = data.get("pid") if isinstance(data, dict) else None
            if isinstance(pid, int) and pid > 1 and not data.get("exited_at"):
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def example_profile(self, **overrides):
        profile = {"name": "example", "hub_base_url": "http://127.0.0.1:9999",
                   "pools": {"l1_claude": "pool-live-a", "launched_claude": "pool-bg-a",
                             "launched_codex": "pool-bg-b"}}
        profile.update(overrides)
        path = self.root / "profile.yaml"
        self.write_yaml(path, profile)
        self.env["YGG_PROFILE"] = str(path)

    def load_profile(self, path, **overrides):
        if self.is_stub:
            return stub.nothing()
        profile = yaml.safe_load(self.read(path))
        if not isinstance(profile, dict):
            raise AssertionError("The profile runner must provide the real YGG_PROFILE")
        self.denylist = profile.get("leak_denylist")
        profile.update(overrides)
        destination = self.root / "profile.yaml"
        self.write_yaml(destination, profile)
        self.env["YGG_PROFILE"] = str(destination)

    def core_path(self, relative):
        if self.is_stub:
            return self.root / "empty-core" / relative
        if self.core is None:
            raise AssertionError("YGG_CORE_ROOT must name the installed core checkout")
        return self.core / relative

    def exists(self, path):
        if self.is_stub:
            return stub.nothing()
        return Path(path).exists()

    def copy_demo(self):
        if self.is_stub:
            return stub.nothing()
        shutil.copytree(self.core_path("examples/demo-programme/tracker"), self.tracker, dirs_exist_ok=True)

    def remove(self, path):
        if self.is_stub:
            return stub.nothing()
        Path(path).unlink(missing_ok=True)

    def use_real_profile(self, **overrides):
        if self.is_stub:
            return stub.nothing()
        path = self.real_profile or os.environ.get("YGG_PROFILE")
        if not path:
            raise RuntimeError("the profile suite needs YGG_PROFILE (its runner supplies the default)")
        self.load_profile(path, **overrides)

    def quickstart(self):
        if self.is_stub:
            return stub.run()
        readme = self.read(self.core_path("README.md"))
        section = re.search(r"(?ms)^## Quickstart\s*\n(.*?)(?=^## |\Z)", readme)
        blocks = re.findall(r"(?ms)^```bash\s*\n(.*?)^```", section[1] if section else "")
        if len(blocks) != 1:
            return subprocess.CompletedProcess([], 1, "", "Quickstart requires one bash block")
        fresh = self.make_cwd("quickstart")
        return subprocess.run(["bash", "-e", "-c", blocks[0]], cwd=fresh, env=self.env,
                              text=True, capture_output=True, timeout=30, check=False)

    def leak_matches(self, scope, *, denylist=None):
        if self.is_stub:
            return stub.entries()
        root = self.core_path("")
        deny_path = denylist or self.denylist
        if scope != "secret-history" and not deny_path:
            raise AssertionError("YGG_LEAK_DENYLIST must name the leak-scan fixture")
        terms = [line.strip().casefold() for line in self.read(deny_path).splitlines()
                 if line.strip() and not line.lstrip().startswith("#")] if deny_path else []
        excluded = Path(deny_path).resolve() if deny_path else None
        if scope == "tree":
            names = self._git(root, "ls-files", "-z").stdout.split("\0")
            texts = []
            for name in names:
                if not name or (root / name).resolve() == excluded:
                    continue
                path = root / name
                content = os.readlink(path) if path.is_symlink() else self.read(path)
                texts.append((name, content))
        else:
            args = ["log", "-p", "--all", "--format=", "--", "."]
            if excluded and excluded.is_relative_to(root.resolve()):
                args.append(":(exclude)" + str(excluded.relative_to(root.resolve())))
            texts = [("diffs", self._git(root, *args).stdout)]
        if scope == "secret-history":
            pattern = re.compile(r"(lin_api_|lin_oauth_|sk-ant-|sk-proj-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}")
            return [(name, match.group()) for name, content in texts for match in pattern.finditer(content)]
        return [(name, term) for name, content in texts for term in terms if term in content.casefold()]

    def schema_errors(self, name, data):
        if self.is_stub:
            return stub.entries()
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads(self.read(Path(__file__).parent / "schemas" / (name + ".schema.json")))
        return [error.message for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data)]

    def git_metadata(self, directory):
        if self.is_stub:
            return stub.entries()
        return [str(path) for path in Path(directory).rglob(".git")]

    def skeleton(self):
        if self.is_stub:
            return stub.nothing()
        self._repo(self.repo_a)
        self.run("programme", "create", "alpha", "--domain", "alpha", "--home", self.repo_a, "--kind", "git-repo")
        self.frozen_agent_fixture()
        self.run("project", "create", "alpha-p1", "--programme", "alpha", "--pm", "pm1")
        self.run("node", "add", "pm1tl1", "--role", "tl", "--parent", "pm1")
        self.run("node", "add", "pm1su1", "--role", "su", "--parent", "pm1", "--pairs-with", "pm1tl1")
        self.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--kind", "brief", "--body", "# Save module\n")
        def write_step(identifier, node, kind, body, *extra, **fields):
            return dict(id=identifier, run=["ygg", "tracker", "write", "--as", node,
                "--project", "alpha-p1", *extra, "--kind", kind, "--body", body], **fields)
        route = "\n".join("## " + heading for heading in
            ("Destination", "Frontier", "Blockers", "Decisions so far", "Fog", "Out of scope")) + "\n"
        pm = [
            write_step("pid", "pm1", "pid", self.pid_body()),
            {"id": "route-file", "write_file": {"path": "route-map.md", "text": route}},
            {"id": "route", "run": ["ygg", "routemap", "write", "--as", "pm1", "--project", "alpha-p1", "--file", "route-map.md"]},
            write_step("product", "pm1", "product-description", "# Save module\n"),
            write_step("plan", "pm1", "stage-plan", "Stage 1 plan", "--stage", "1"),
            write_step("package", "pm1", "work-package", "Save module", "--stage", "1", "--package", "WP-1"),
            {"id": "request-criteria", "run": ["ygg", "send", "--from", "pm1", "--to", "pm1su1", "--kind", "work", "--body", "write criteria for WP-1"]},
            write_step("pid-ready", "pm1", "pid-ready", "PID v1"),
            {"id": "authorise", "when": {"pid_approved": "alpha-p1"}, "run": ["ygg", "wp", "authorise", "alpha-p1", "--package", "WP-1", "--lane", "pm1tl1"]},
            {"id": "report", "when": {"package_state": {"WP-1": "accepted"}}, "write_file": {"path": "end-stage-1.md", "text": "# End of stage 1\n"}},
            {"id": "end", "when": {"package_state": {"WP-1": "accepted"}}, "run": ["ygg", "stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", "end-stage-1.md"]},
        ]
        lead = [
            write_step("checkpoint", "pm1tl1", "checkpoint", "WP-1 done", "--package", "WP-1", when={"package_state": {"WP-1": "authorised"}}),
            {"id": "review", "run": ["ygg", "review", "record", "--from", "pm1tl1", "--package", "WP-1", "--round", "1", "--verdict", "pass"]},
            {"id": "complete", "run": ["ygg", "wp", "complete", "--from", "pm1tl1", "--package", "WP-1"]},
        ]
        senior = [
            write_step("criteria", "pm1su1", "criteria", "Save module", "--package", "WP-1"),
            {"id": "accept", "when": {"package_state": {"WP-1": "ready-for-test"}}, "run": ["ygg", "acceptance", "record", "--from", "pm1su1", "--package", "WP-1", "--sha", "{head:ygg/alpha-p1/lane/pm1tl1}", "--verdict", "accept"]},
        ]
        self.fake_behavior(claude={"agents": {"pm1": {"mode": "script", "steps": pm},
            "pm1tl1": {"mode": "script", "steps": lead}, "pm1su1": {"mode": "script", "steps": senior}}})
        approved = False
        for _ in range(20):
            self.set_load("0.10")
            self.run("conductor", "tick", timeout=180)
            for node in ("pm1", "pm1tl1", "pm1su1"):
                if self.launches(node):
                    self.wait_exit(node)
            ready = any(line.get("kind") == "ygg-pid-ready" for line in self.state_lines("alerts.jsonl"))
            issue = self.tracker_text("projects/alpha-p1/issues/WP-1.md")
            if ready and not approved and re.search(r"(?m)^kind: criteria\s*$", issue):
                self.run("pid", "approve", "alpha-p1")
                approved = True
            status = self.status()
            if (isinstance(status, dict)
                    and not any(node.get("state") in {"queued", "running"} for node in status["nodes"])
                    and not any(package.get("state") == "in-review"
                                for package in status.get("work_packages", []))):
                break

    def crowded_pm(self):
        self.ready()
        packages = []
        for number in range(1, 41):
            package = "WP-" + str(number)
            packages.append({"id": package + "-0", "run": ["ygg", "tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--stage", "1", "--package", package, "--kind", "work-package", "--body", "Save module"]})
        self.fake_behavior(claude={"agents": {"pm1": {"mode": "script", "steps": packages}}})
        self.run("launch", "pm1")
        self.wait_exit("pm1", timeout=60)
        steps = []
        for number in range(12):
            steps.append({"id": "decision-" + str(number), "run": ["ygg", "decision", "raise", "--from", "pm1",
                "--question", "Ship tutorial skip?", "--option", "A", "--option", "B", "--recommended", "A",
                "--reversible", "no", "--aligns", "yes"]})
        for number in range(1, 41):
            package = "WP-" + str(number)
            self.run("tracker", "write", "--as", "pm1su1", "--project", "alpha-p1", "--package", package, "--kind", "criteria", "--body", package,
                     env={"YGG_NODE": None})
            commands = (
                ["wp", "authorise", "alpha-p1", "--package", package, "--lane", "pm1tl1"],
                ["wp", "complete", "--from", "pm1tl1", "--package", package],
            )
            for index, command in enumerate(commands, 2):
                steps.append({"id": package + "-" + str(index), "run": ["ygg", *command]})
            steps.append({"id": package + "-wait", "wait": package + "-reviewed"})
        self.fake_behavior(claude={"agents": {"pm1": {"mode": "script", "steps": steps}}})
        self.run("launch", "pm1")
        for number in range(1, 41):
            package = "WP-" + str(number)
            self.complete_package_review(package)
            self.write(self.root / "fake-control" / "pm1" / (package + "-reviewed"), "")
        self.wait_exit("pm1", timeout=60)

    def a5_t3(self, **behavior):
        if self.is_stub:
            return stub.nothing()
        folder = Path(__file__).parent / "fakes"
        destination = self.root / "bin" / "t3"
        self.write(destination, "#!" + sys.executable + "\n" + self.read(folder / "t3_a5.py"))
        destination.chmod(0o755)
        self.env["YGG_T3_BIN"] = str(destination)
        self.env["YGG_FAKE_T3_FIXTURE"] = str(folder / "m1" / "t3" / "t3-cli-0.1.9-observed.json")
        config = json.loads(self.read(self.fake_config))
        config["t3"] = behavior
        self.write(self.fake_config, json.dumps(config))

    def a5_t3_behavior(self, **behavior):
        if self.is_stub:
            return stub.nothing()
        config = json.loads(self.read(self.fake_config))
        config.setdefault("t3", {}).update(behavior)
        self.write(self.fake_config, json.dumps(config))

    def a5_t3_calls(self, action, node=None):
        calls = [call for call in self.calls("t3") if call["argv"][:2] == action.split()]
        if node is None:
            return calls
        return [call for call in calls if call.get("node") == node
                or ("--title" in call["argv"]
                    and call["argv"][call["argv"].index("--title") + 1].split()[0] == node)]

    def a5_receipts(self, node=None):
        if self.is_stub:
            return stub.entries()
        entries = [json.loads(self.read(path)) for path in sorted((self.state / "activations").glob("*.json"))]
        return [entry for entry in entries if node is None or entry.get("node") == node]

    def a5_logged_fallbacks(self, result):
        if self.is_stub:
            return stub.entries()
        texts = [result.stdout, result.stderr]
        texts.extend(self.read(path) for path in self.state.rglob("*")
                     if path.is_file() and path.suffix in {".json", ".jsonl", ".log"})
        return [line for text in texts for line in text.splitlines() if "fallback" in line.casefold()]

    def a5_version_lists(self, cli, server):
        if self.is_stub:
            return stub.nothing()
        rows = self.command_json("config", "explain", "--json")
        if not isinstance(rows, list):
            raise AssertionError("config explain must expose the configurable T3 version lists")
        overlay = {}
        selected = []
        for kind, values in (("cli", cli), ("server", server)):
            matches = [row for row in rows if kind in row["key"].casefold()
                       and "version" in row["key"].casefold() and isinstance(row["value"], list)]
            if len(matches) != 1:
                raise AssertionError("Cannot identify the public " + kind + " version-list setting")
            key = matches[0]["key"]
            cursor = overlay
            parts = key.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = list(values)
            selected.append((key, list(values)))
        path = self.root / "a5-versions.yaml"
        self.write_yaml(path, overlay)
        self.env["YGG_CONFIG"] = str(path)
        for key, values in selected:
            observed = self.command_json("config", "explain", key, "--json")
            if not isinstance(observed, dict) or observed.get("value") != values:
                raise AssertionError("The version-list Given was not applied: " + key)

    def a5_live_thread(self):
        self.a5_t3()
        self.ready()
        for item in (self.status() or {}).get("inbox_pending", []):
            if item["node"] == "pm1tl1":
                self.run("inbox", "pm1tl1", "--ack", item["msg_id"])
        self.run("launch", "pm1tl1")
        self.run("test", "set-next", "message", "10")
        self.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "start WP-3")
        self.a5_t3_behavior(thread_send={"outcome": "unknown", "deliver": True})
        self.clear_calls()

    def a5_uncertain_start(self):
        self.a5_t3(thread_start={"outcome": "unknown", "deliver": False})
        self.ready()
        self.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "start WP-3")
        self.clear_calls()
        return self.run("conductor", "tick")

    @contextmanager
    def a5_linear(self):
        import graphql
        self.linear_dir = self.root / "linear"
        self.env["YGG_TRACKER"] = "linear"
        self.env["YGG_LINEAR_FAKE"] = str(self.linear_dir)
        if self.is_stub:
            yield stub.nothing()
            return
        self.linear_dir.mkdir()
        token = {"client_id": "test-client", "client_secret": "test-client-secret",
                 "access_token": "test-token", "expires_at": "2099-01-01T00:00:00Z"}
        token_path = self.linear_dir / "token-store.json"
        self.write(token_path, json.dumps(token))
        token_path.chmod(0o600)
        fake = Path(__file__).parent / "fakes" / "linear_a5.py"
        process = subprocess.Popen([sys.executable, str(fake), "--directory", str(self.linear_dir)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not (self.linear_dir / "url").exists():
                if process.poll() is not None:
                    raise AssertionError("Linear fake failed: " + process.stderr.read())
                if time.monotonic() >= deadline:
                    raise AssertionError("Linear fake did not become ready")
                time.sleep(0.05)
            yield self.linear_dir
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()

    def a5_linear_state(self):
        if self.is_stub:
            return stub.snapshot()
        return self.json_file(self.linear_dir / "state.json")

    def a5_linear_requests(self):
        if self.is_stub:
            return stub.entries()
        return [json.loads(line) for line in self.read(self.linear_dir / "requests.jsonl").splitlines()
                if line.strip()]

    def a5_linear_clear_requests(self):
        return self.write(self.linear_dir / "requests.jsonl", "")

    def a5_linear_control(self, name, value):
        return self.write(self.linear_dir / (name + ".json"), json.dumps(value))

    def a5_linear_schema_errors(self):
        from graphql import GraphQLError, build_schema, get_operation_ast, get_variable_values, parse, validate
        if self.is_stub:
            return stub.entries()
        schema = build_schema(self.read(Path(__file__).parent / "fakes" / "m1" /
                                       "linear-schema" / "schema.graphql"))
        errors = []
        for record in self.a5_linear_requests():
            if record["path"] != "/graphql":
                continue
            body = record["body"]
            try:
                document = parse(body["query"])
                current = validate(schema, document)
                operation = get_operation_ast(document, body.get("operationName"))
                if operation is None:
                    errors.append("A GraphQL request did not select one operation")
                elif not current:
                    variables = get_variable_values(schema, operation.variable_definitions or (),
                                                   body.get("variables") or {})
                    if isinstance(variables, list):
                        current.extend(variables)
                errors.extend(str(error) for error in current)
            except (GraphQLError, ValueError, KeyError, TypeError) as error:
                errors.append(str(error))
        return errors

    def a5_linear_expire_token(self):
        return self.change_json(self.linear_dir / "token-store.json", expires_at="2000-01-01T00:00:00Z")

    def a5_linear_credentials(self):
        if self.is_stub:
            return stub.snapshot()
        return self.json_file(self.linear_dir / "token-store.json")

    def a5_linear_refuse_connections(self):
        if self.is_stub:
            return stub.nothing()
        import socket
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self.write(self.linear_dir / "url", "http://127.0.0.1:" + str(reserved.getsockname()[1]) + "\n")


    def a5_linear_live_schema_errors(self):
        from graphql import (
            GraphQLError, build_schema, extend_schema, get_operation_ast,
            get_variable_values, parse, validate,
        )
        if self.is_stub:
            return stub.entries()
        directory = Path(__file__).parent / "fakes" / "m1" / "linear-schema"
        schema = extend_schema(build_schema(self.read(directory / "schema.graphql")),
                               parse(self.read(directory / "live-delta.graphql")))
        errors = []
        for record in self.a5_linear_requests():
            if record["path"] != "/graphql":
                continue
            body = record["body"]
            try:
                document = parse(body["query"])
                current = validate(schema, document)
                operation = get_operation_ast(document, body.get("operationName"))
                if operation is None:
                    current.append(GraphQLError("A GraphQL request did not select one operation"))
                elif not current:
                    variables = get_variable_values(schema, operation.variable_definitions or (),
                                                   body.get("variables") or {})
                    if isinstance(variables, list):
                        current.extend(variables)
                errors.extend(str(error) for error in current)
            except (GraphQLError, ValueError, KeyError, TypeError) as error:
                errors.append(str(error))
        return errors

    def read_bytes(self, path):
        if self.is_stub:
            return stub.binary()
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return b""

    def m3_t3_seed(self, *, owes_message=False):
        self.a5_t3(cli_version="0.1.9", server_version="0.1.9")
        self.ready()
        for item in (self.status() or {}).get("inbox_pending", []):
            if item["node"] == "pm1tl1":
                self.run("inbox", "pm1tl1", "--ack", item["msg_id"])
        self.run("launch", "pm1tl1")
        if owes_message:
            self.run("test", "set-next", "message", "10")
            self.run("send", "--from", "pm1", "--to", "pm1tl1",
                     "--kind", "work", "--body", "start WP-3")

    def m3_t3_thread(self, *, last_assistant=None, **fields):
        if self.is_stub:
            return stub.nothing()
        path = self.root / "t3-state.json"
        state = self.json_file(path)
        thread = state["threads"]["th-4"]
        thread.update(fields)
        if last_assistant is not None:
            thread.setdefault("messages", []).append({"role": "assistant", "text": last_assistant})
        self.write(path, json.dumps(state))

    def m3_t3_message_files(self, message, cwd):
        import stat

        if self.is_stub:
            return stub.entries()
        files = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            names = (str(path), os.path.relpath(path, cwd))
            if any(name in message for name in names):
                files.append({"path": str(path), "mode": stat.S_IMODE(path.stat().st_mode),
                              "text": self.read(path)})
        return files


    def frozen_agent_fixture(self):
        self._frozen_reviewer = True
        path = self.root / "home" / ".config" / "yggdrasil" / "config.yaml"
        current = yaml.safe_load(self.read(path)) or {}
        current.setdefault("alert_forwarder", "jsonl")
        self.write_yaml(path, current)
        self.run("agents", "allow-minimal", "--yes")
        self.fake_behavior()

    def complete_package_review(self, package="WP-41"):
        if self.is_stub:
            return stub.nothing()
        state = self.status() or {}
        row = next((item for item in state.get("work_packages", []) if item["id"] == package), {})
        if row.get("state") == "ready-for-test":
            return
        for _ in range(10):
            self.set_load("0.10")
            self.run("conductor", "tick")
            for launch in self.launches("pm1rv1"):
                self.wait_exit("pm1rv1")
            state = self.status() or {}
            row = next((item for item in state.get("work_packages", []) if item["id"] == package), {})
            if row.get("state") == "ready-for-test":
                return
            time.sleep(0.02)
        raise AssertionError("Scripted approving review did not settle " + package)
