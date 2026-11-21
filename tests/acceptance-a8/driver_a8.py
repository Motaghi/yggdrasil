"""A8 CLI, filesystem, trace, process and timing boundaries."""

from contextlib import contextmanager
import os
import re
import signal
import subprocess
import time

import yaml

from driver_a7 import DriverA7
import stub_a8


class DriverA8(DriverA7):
    def __init__(self, root):
        self._supervisors = {}
        self._unrelated = set()
        self._trace_dirs = set()
        super().__init__(root)
        self.safety = self.root / "state.safety"

    def run(self, *args, **kwargs):
        if self.is_stub:
            return stub_a8.run()
        return super().run(*args, **kwargs)

    @contextmanager
    def fresh(self, name):
        with DriverA8(self.root / name) as driver:
            driver.at("2026-11-16T10:00:00+01:00")
            driver.env.pop("YGG_NODE", None)
            driver.env.pop("YGG_ACTIVATION", None)
            driver.env.pop("YGG_SAFETY_ROOT", None)
            config = driver.root / "a8-config.yaml"
            driver.write_yaml(config, {
                "launch_confirm_seconds": 0.5,
                "settle_delay_seconds": 0,
                "capacity_backoff_seconds": 300,
                "checkpoint_batch_hours": 4,
                "stop_grace_seconds": 10,
            })
            driver.env["YGG_CONFIG"] = str(config)
            driver.agents()
            driver.core_tree()
            driver.ready()
            driver.quiesce()
            driver.clear_calls()
            yield driver

    def quiesce(self):
        if self.is_stub:
            return stub_a8.nothing()
        for _ in range(20):
            # The inherited helper refreshes load at wall time; A8 requires YGG_NOW.
            self.at(self.env["YGG_NOW"])
            self.tick()
            status = self.status()
            if not isinstance(status, dict):
                raise AssertionError("Quiet baseline needs status JSON")
            if not any(node["state"] in {"queued", "running"}
                       for node in status["nodes"]):
                return
            time.sleep(0.05)

    def agents(self, **modes):
        definitions = {
            node: ({"mode": "exit", "exit_code": 0} if mode == "exit0"
                   else {"mode": mode})
            for node, mode in modes.items()
        }
        return self.fake_behavior(
            claude={"mode": "exit", "exit_code": 0, "agents": definitions},
            codex={"mode": "exit", "exit_code": 0, "agents": definitions},
        )

    def message(self, body="Do WP-1"):
        return self.run("send", "--from", "pm1", "--to", "pm1tl1",
                        "--kind", "work", "--body", body)

    def tick(self, **kwargs):
        return self.run("conductor", "tick", **kwargs)

    def supervisor_tick(self, *, trace="sup", fault=None):
        directory = self.root / trace
        self._trace_dirs.add(directory)
        return self.tick(env={"YGG_SUPERVISOR_TRACE_DIR": str(directory),
                              "YGG_SUPERVISOR_FAULT_AT": fault})

    def receipts(self, node=None):
        if self.is_stub:
            return stub_a8.entries()
        return super().receipts(node)

    def activation_files(self, node):
        if self.is_stub:
            return stub_a8.entries()
        files = []
        for path in (self.state / "activations").glob("*.json"):
            record = self.json_file(path)
            if not isinstance(record, dict):
                raise AssertionError("Unparseable activation: " + str(path))
            if record.get("node") == node:
                files.append({"record": record, "mtime": path.stat().st_mtime_ns,
                              "name": path.name})
        return sorted(files, key=lambda item: (item["mtime"], item["name"]))

    def latest(self, node="pm1tl1"):
        files = self.activation_files(node)
        return files[-1]["record"] if files else None

    def receipt(self, activation):
        return self.json_file(self.state / "activations" / (activation + ".json"))

    def process_alive(self, pid):
        if self.is_stub:
            return stub_a8.nothing()
        return super().process_alive(pid)

    def process_start(self, pid):
        if self.is_stub:
            return stub_a8.text()
        if type(pid) is not int or pid <= 1:
            return ""
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def starts(self, node="pm1tl1", activation=None):
        if self.is_stub:
            return len(stub_a8.entries())
        time.sleep(1)
        calls = self.launches(node)
        if activation is not None:
            calls = [call for call in calls
                     if call.get("activation", call.get("env", {}).get("YGG_ACTIVATION"))
                     == activation]
        return len(calls)

    def wait_dead(self, pid, timeout):
        if self.is_stub:
            return stub_a8.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.process_alive(pid):
                return True
            time.sleep(0.05)
        return not self.process_alive(pid)

    def wait_starts(self, node, count, timeout=30):
        if self.is_stub:
            return len(stub_a8.entries())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.launches(node)) >= count:
                return self.starts(node)
            time.sleep(0.05)
        return self.starts(node)

    def wait_exited(self, node="pm1tl1", timeout=30):
        if self.is_stub:
            return stub_a8.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.latest(node)
            if record and record.get("exited_at"):
                return record
            time.sleep(0.05)
        return self.latest(node)

    def wait_supervisor(self, node="pm1tl1", trace="sup", timeout=30):
        if self.is_stub:
            return stub_a8.nothing()
        directory = self.root / trace
        self._trace_dirs.add(directory)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.latest(node)
            if record:
                activation = record["activation"]
                lines = self.read(directory / (activation + ".txt")).splitlines()
                if lines:
                    header = re.fullmatch(r"start (\S+) ([0-9]+)", lines[0])
                    if not header or header[1] != node:
                        raise AssertionError("Invalid supervisor trace header: " + lines[0])
                    pid = int(header[2])
                    self._supervisors.setdefault(pid, self.process_start(pid))
                    if (lines[-1].startswith(("done ", "fault "))
                            and not self.process_alive(pid)):
                        return {"activation": activation, "pid": pid, "lines": lines}
            time.sleep(0.05)
        raise AssertionError("Supervisor did not finish within 30 seconds: " + node)

    def trace_step(self, *args):
        if self.is_stub:
            return stub_a8.run(), stub_a8.text()
        return super().trace_step(*args)

    def fault_step(self, fault, *args):
        return self.run(*args, env={"YGG_FAULT_AT": fault, "YGG_FAULT_TRACE": None})


    @contextmanager
    def unrelated_sleep(self):
        if self.is_stub:
            yield stub_a8.nothing()
            return
        process = subprocess.Popen(["sleep", "3600"], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._unrelated.add(process.pid)
        try:
            yield process.pid
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            self._unrelated.discard(process.pid)

    def tracker_writable(self):
        if self.is_stub:
            return stub_a8.nothing()
        for path in (self.tracker, *self.tracker.rglob("*")):
            path.chmod((path.stat().st_mode & 0o777) | 0o200)

    def intents(self, scope="alpha"):
        if self.is_stub:
            return stub_a8.entries()
        paths = list((self.safety / "interlocks" / scope).glob("*.json"))
        return [path.stem for path in sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.name))]

    def journal_entries(self, programme="alpha"):
        if self.is_stub:
            return stub_a8.entries()
        text = self.tracker_text("initiatives/" + programme + "/documents/journal.md")
        blocks = re.findall(r"(?ms)^```yaml\s*\n(.*?)^```\s*$", text)
        entries = [yaml.safe_load(block) for block in blocks]
        if any(not isinstance(entry, dict) for entry in entries):
            raise AssertionError("Journal entries must be YAML mappings")
        return entries

    def journal_bytes(self):
        if self.is_stub:
            return stub_a8.snapshot()
        paths = list(self.tracker.glob("initiatives/*/documents/journal.md"))
        global_path = self.tracker / "_ygg/journal.md"
        if global_path.exists():
            paths.append(global_path)
        return {str(path.relative_to(self.tracker)): path.read_bytes() for path in paths}

    def cleanup_processes(self):
        if self.is_stub:
            return stub_a8.nothing()
        records = self.receipts() + getattr(self, "_pre_wipe_records", [])
        for record in records:
            pid = record.get("pid")
            if (pid not in self._unrelated and self.process_alive(pid)
                    and self.process_start(pid) == record.get("process_started_at")):
                self._kill_owned(pid)
        # Find unrecorded agents by the same environment evidence A8.7 names.
        result = subprocess.run(
            ["/bin/ps", "eww", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, check=False, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if (len(parts) == 2 and parts[0].isdecimal()
                    and re.search(r"(?:^|\s)YGG_STATE_ROOT=" + re.escape(str(self.state))
                                  + r"(?:\s|$)", parts[1])
                    and "YGG_ACTIVATION=" in parts[1]):
                pid = int(parts[0])
                if pid not in self._unrelated:
                    self._kill_owned(pid)
        for directory in self._trace_dirs:
            for path in directory.glob("*.txt"):
                lines = self.read(path).splitlines()
                if lines:
                    match = re.fullmatch(r"start \S+ ([0-9]+)", lines[0])
                    if match:
                        pid = int(match[1])
                        self._supervisors.setdefault(pid, self.process_start(pid))
        for pid, started in self._supervisors.items():
            if started and self.process_alive(pid) and self.process_start(pid) == started:
                self._kill_owned(pid)

    def _kill_owned(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @contextmanager
    def barrier_tick(self, name, node="pm1tl1"):
        if self.is_stub:
            yield stub_a8.nothing()
            return
        environment = self.env.copy()
        environment.update({
            "YGG_TEST_BARRIER": node + ":" + name,
            "YGG_TEST_BARRIER_DIR": str(self.root / "b"),
        })
        process = subprocess.Popen(
            [self.cli, "conductor", "tick"], cwd=self.cwd, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

    def wait_barrier(self, name, timeout=30):
        if self.is_stub:
            return stub_a8.nothing()
        suffix = "." + name + ".reached"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            paths = list((self.root / "b").glob("*" + suffix))
            if len(paths) > 1:
                raise AssertionError("Expected a single barrier reached file")
            if paths:
                text = self.read(paths[0]).strip()
                if text.isdecimal():
                    pid = int(text)
                    self._supervisors.setdefault(pid, self.process_start(pid))
                    return {"activation": paths[0].name[:-len(suffix)],
                            "pid": pid, "name": name}
            time.sleep(0.05)
        raise AssertionError("Barrier was not reached: " + name)

    def kill_launcher(self, process):
        if self.is_stub:
            return stub_a8.nothing()
        process.kill()
        process.wait(timeout=10)

    def continue_barrier(self, barrier):
        if self.is_stub:
            return stub_a8.nothing()
        self.write(self.root / "b" / (barrier["activation"] + "."
                                     + barrier["name"] + ".continue"), "")

    def timed_run(self, *args, **kwargs):
        if self.is_stub:
            return stub_a8.run(), stub_a8.nothing()
        started = time.monotonic()
        result = self.run(*args, **kwargs)
        return result, time.monotonic() - started

    def recovery_observation(self, node="pm1tl1"):
        if self.is_stub:
            return stub_a8.nothing()
        self.starts(node)
        return self.observe(node)
