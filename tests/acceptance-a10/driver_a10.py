import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess

from driver_a9 import DriverA9, INT, L1
import stub_a10


class DriverA10(DriverA9):
    def run(self, *args, **kwargs):
        if self.is_stub:
            return stub_a10.run()
        return super().run(*args, **kwargs)

    def external(self, arguments, *, cwd=None, env=None):
        if self.is_stub:
            return stub_a10.run()
        call_env = self.env.copy()
        call_env.update({key: str(value) for key, value in (env or {}).items()})
        return subprocess.run(list(map(str, arguments)), cwd=cwd or self.cwd,
                              env=call_env, input="", text=True, capture_output=True,
                              timeout=30, check=False)

    def git_result(self, directory, *args):
        return self.external(["git", "-C", directory, *args])

    def git_folder(self, relative):
        directory = self.make_cwd(relative)
        self.git(directory, "init", "-b", "main")
        self.git(directory, "config", "user.name", "acceptance")
        self.git(directory, "config", "user.email", "acceptance@example.invalid")
        return directory

    def init(self, directory, *args):
        return self.run("init", "--preset", "starter", "--non-interactive", *args,
                        cwd=directory, env={key: None for key in
                            ("YGG_CONFIG", "YGG_PROFILE", "YGG_TRACKER", "YGG_STATE_ROOT", "YGG_FILES_ROOT")})

    def executable(self, path, text):
        if self.is_stub:
            return stub_a10.nothing()
        self.write(path, "#!/bin/sh\n" + text)
        Path(path).chmod(0o755)

    def marker_script(self, name, *, path=None):
        marker = self.root / name
        script = path or self.root / (name + ".sh")
        self.executable(script, "touch " + shlex.quote(str(marker)) + "\nexit 0\n")
        return script, marker

    def plugin(self, directory, distribution, name):
        marker = self.root / ("imported-" + distribution)
        self.write(directory / (distribution + ".py"),
                   "from pathlib import Path\n"
                   + "Path(" + repr(str(marker)) + ").touch()\n"
                   + "api_version = 1\n\nclass Tracker:\n    api_version = api_version\n")
        metadata = directory / (distribution + "-0.1.dist-info")
        self.write(metadata / "METADATA",
                   "Metadata-Version: 2.1\nName: " + distribution + "\nVersion: 0.1\n")
        self.write(metadata / "entry_points.txt",
                   "[yggdrasil.trackers]\n" + name + " = " + distribution + ":Tracker\n")
        return marker

    def untrusted_plugins(self, *, tracker=False):
        folder = self.make_cwd("f")
        config = folder / "yggdrasil.yaml"
        directory = folder / "plugins"
        if tracker:
            marker = self.plugin(directory, "ygg_memtracker", "memory")
            self.env["YGG_PLUGIN_PATH"] = str(directory)
            self.write(config, "tracker: memory\n")
        else:
            marker = self.plugin(directory, "evilplug", "evil")
            self.write(config, "plugin_paths: [./plugins]\n")
        self.env.pop("YGG_CONFIG", None)
        # The inherited core fixture selects files through this variable.
        # These examples exercise selection from config instead.
        self.env.pop("YGG_TRACKER", None)
        return folder, config, marker

    def duplicate_plugins(self):
        directory = self.make_cwd("plugins")
        self.plugin(directory, "ygg_memtracker", "memory")
        self.plugin(directory, "ygg_othertracker", "memory")
        self.env["YGG_PLUGIN_PATH"] = str(directory)
        self.env.pop("YGG_TRACKER", None)

    def home_precommit(self, location):
        if location == "configured":
            folder = self.repo_a / "scripts" / "hooks"
            self.git(self.repo_a, "config", "core.hooksPath", folder)
        else:
            folder = self.repo_a / ".git" / "hooks"
        self.executable(folder / "pre-commit",
                        "printf '%s\\n' 'home pre-commit ran' >&2\nexit 1\n")

    def launch_and_wait(self, node):
        self.run("agents", "allow-minimal", "--yes")
        result = self.run("launch", node)
        self.wait_exit(node)
        return result

    def lane_commit(self):
        if self.is_stub:
            return stub_a10.run()
        launches = self.launches("pm1tl1")
        if not launches:
            raise AssertionError("Given requires a pm1tl1 lane worktree launch")
        return self.git_result(launches[-1]["cwd"], "commit", "--allow-empty", "-m", "x")

    def scanner(self):
        directory = self.git_folder("scan")
        denylist = self.root / "denylist.txt"
        self.write(denylist, "zebra-orchid-7\n\n# a comment\n")
        self.git(directory, "config", "core.hooksPath", self.core_path("scripts/hooks"))
        self.git(directory, "config", "ygg.leakDenylist", denylist)
        return directory

    def stage_file(self, directory, name, text):
        self.write(directory / name, text)
        self.git(directory, "add", "--", name)

    def history_seed(self, *, hit):
        directory = self.scanner()
        self.git(directory, "config", "core.hooksPath", "/dev/null")
        second = ""
        for number, message in enumerate(("first", "fix zebra-orchid-7" if hit else "fix", "third"), 1):
            self.stage_file(directory, "history.md", str(number) + "\n")
            self.git(directory, "commit", "-m", message)
            if number == 2:
                second = self.git(directory, "rev-parse", "HEAD").stdout.strip()
        return directory, second

    def scan_history(self, directory):
        return self.external([self.core_path("scripts/leak-scan"), "--history", "main"],
                             cwd=directory, env={"YGG_LEAK_DENYLIST": self.root / "denylist.txt"})

    def changed_texts(self, before, relative):
        if self.is_stub:
            return stub_a10.entries()
        previous = dict(before)
        return [text for name, text in self.documents(relative) if previous.get(name) != text]

    def newest_receipt(self, node):
        if self.is_stub:
            return stub_a10.nothing()
        receipts = self.a5_receipts(node)
        return receipts[-1] if receipts else None

    def mode(self, path):
        if self.is_stub:
            return stub_a10.nothing()
        try:
            return stat.S_IMODE(Path(path).stat().st_mode)
        except FileNotFoundError:
            return None

    def chmod(self, path, mode):
        if self.is_stub:
            return stub_a10.nothing()
        Path(path).chmod(mode)

    def newest_receipt_mode(self, node):
        if self.is_stub:
            return stub_a10.nothing()
        paths = [path for path in sorted((self.state / "activations").glob("*.json"))
                 if self.json_file(path).get("node") == node]
        return self.mode(paths[-1]) if paths else None

    def append_node(self, entry):
        if self.is_stub:
            return stub_a10.nothing()
        path = self.tracker / "projects" / "alpha-p1" / "nodes.json"
        data = self.json_file(path)
        data["nodes"].append(entry)
        self.write(path, json.dumps(data))

    def demo_seed(self):
        destination = self.root / "q" / "ygg-tracker"
        if not self.is_stub:
            shutil.copytree(self.core_path("examples/demo-programme/tracker"), destination)
        self.env["YGG_FILES_ROOT"] = str(destination)
        elsewhere = self.make_cwd("elsewhere")
        return elsewhere, os.path.realpath(self.root / "q" / "demo-home")

    def accepted_integration(self):
        self.ready()
        self.git_labels()
        self.branch(L1, "b2c3d4e")
        self.branch(INT, "b2c3d4e")
        self.package(criteria=True, authorise=True)
        self.run("wp", "complete", "--from", "pm1tl1", "--package", "WP-41")
        self.complete_package_review()
        self.run("review", "record", "--from", "pm1tl1", "--package", "WP-41",
                 "--round", "1", "--verdict", "pass")
        self.run("acceptance", "record", "--from", "pm1su1", "--package", "WP-41",
                 "--sha", self.sha("b2c3d4e"), "--verdict", "accept")

    def merge_lane(self):
        return self.run("merge", "lane", "alpha-p1", "--lane", "pm1tl1", "--repo", self.repo_a)

    def brief_seed(self, *, codex=False):
        self.ready()
        self.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--kind", "brief",
                 "--body", "Brief marker zebra-5813: ship saves")
        if codex:
            path = self.root / "codex.yaml"
            self.write(path, "launcher_cli: codex\n")
            self.env["YGG_CONFIG"] = str(path)

    def delivered_privately(self, launch, text):
        if self.is_stub:
            return stub_a10.nothing()
        if text in launch.get("stdin", ""):
            return True
        matches = []
        for argument in launch["argv"]:
            path = Path(argument)
            if not path.is_absolute():
                path = Path(launch["cwd"]) / path
            try:
                if path.is_file() and self.mode(path) == 0o600 and text in self.read(path):
                    matches.append(argument)
            except OSError:
                continue
        return len(matches) == 1
