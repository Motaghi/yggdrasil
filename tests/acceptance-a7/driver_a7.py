"""A7 boundary calls extend the existing, read-only acceptance driver."""

from driver import Driver

from contextlib import contextmanager
import os
import signal
import subprocess
import sys
import time

import stub_a7


class DriverA7(Driver):
    def run(self, *args, **kwargs):
        if self.is_stub:
            return stub_a7.run()
        return super().run(*args, **kwargs)

    def stalled_suspect_seed(self):
        self.at("2026-11-16T10:05:00+01:00")
        self.seed_activation(
            "pm1tl1", "--state", "running", "--process", "live",
            "--heartbeat", "2026-11-15T00:00:00+01:00",
        )
        return self.run("conductor", "tick")

    @contextmanager
    def fresh(self, name):
        with DriverA7(self.root / name) as driver:
            driver.at("2026-11-16T10:00:00+01:00")
            driver.fake_behavior(claude={"mode": "exit", "exit_code": 0},
                                 codex={"mode": "exit", "exit_code": 0})
            driver.core_tree()
            driver.ready()
            driver.quiesce()
            driver.clear_calls()
            try:
                yield driver
            finally:
                if not driver.is_stub:
                    driver.run("stop", "alpha")

    def trace_step(self, *args):
        path = self.root / "ygg-a7-trace.txt"
        self.write(path, "")
        result = self.run(*args, env={"YGG_FAULT_TRACE": str(path),
                                     "YGG_FAULT_AT": None})
        return result, self.read(path)

    def fault_step(self, fault, *args):
        return self.run(*args, env={"YGG_FAULT_AT": fault,
                                   "YGG_FAULT_TRACE": None})

    def receipts(self, node=None):
        if self.is_stub:
            return stub_a7.entries()
        records = [self.json_file(path) for path in
                   sorted((self.state / "activations").glob("*.json"))]
        if any(not isinstance(record, dict) for record in records):
            raise AssertionError("Unparseable activation receipt")
        return [record for record in records
                if node is None or record.get("node") == node]

    def process_alive(self, pid):
        if self.is_stub:
            return stub_a7.nothing()
        if not isinstance(pid, int) or pid <= 1:
            return False
        result = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)], text=True,
            capture_output=True, check=False, timeout=5,
        )
        state = result.stdout.strip()
        return result.returncode == 0 and bool(state) and not state.startswith("Z")

    def observe(self, node):
        if self.is_stub:
            return stub_a7.nothing()
        records = self.receipts(node)
        return {
            "status": self.status(),
            "records": records,
            "live": [record for record in records
                     if not record.get("exited_at")
                     and self.process_alive(record.get("pid"))],
            "starts": len(self.launches(node)),
        }

    def wait_launch(self, node, timeout=10):
        if self.is_stub:
            return stub_a7.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.launches(node):
                return
            time.sleep(0.05)
        raise AssertionError("Fake agent did not record a launch for " + node)

    def launch_capacity_agent(self, node):
        if self.is_stub:
            return stub_a7.run()
        gate = self.root / "capacity-exit"
        agent = self.root / "bin" / "claude"
        wrapper = self.root / "bin" / "claude-capacity"
        # Hold the fake alive through launch confirmation. Releasing it afterwards
        # leaves the exit for the specified classifying tick, outside the seed launch.
        self.write(wrapper, "#!" + sys.executable + "\n"
                   "import os, sys, time\n"
                   "from pathlib import Path\n"
                   "while not Path(" + repr(str(gate)) + ").exists():\n"
                   "    time.sleep(0.05)\n"
                   "os.execv(" + repr(str(agent)) + ", [" + repr(str(agent))
                   + ", *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        self.env["YGG_CLAUDE_BIN"] = str(wrapper)
        try:
            return self.run("launch", node)
        finally:
            self.write(gate, "")

    def wipe_state(self):
        if self.is_stub:
            return stub_a7.nothing()
        self._pre_wipe_records = self.receipts()
        return self.empty_state()

    def cleanup_processes(self):
        super().cleanup_processes()
        if self.is_stub:
            return stub_a7.nothing()
        for record in getattr(self, "_pre_wipe_records", []):
            pid = record.get("pid")
            if not record.get("exited_at") and self.process_alive(pid):
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
