"""The real Claude Code, launched by `ygg launch` with a clean settings folder and an untrusted repository.

Opt-in: set YGG_REAL_CLAUDE_BIN to an installed `claude`. No model is called and nothing leaves the machine: a
local stand-in for the Messages API scripts one tool call per turn, and the real permission checks decide.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from driver import Driver

REAL = os.environ.get("YGG_REAL_CLAUDE_BIN")
pytestmark = pytest.mark.skipif(not REAL or not os.access(REAL, os.X_OK) or
                                os.environ.get("ACCEPTANCE_TARGET", "real") != "real",
                                reason="set YGG_REAL_CLAUDE_BIN to run against the installed Claude Code")


def _events(block, stop):
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "scripted", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})]
    if block["type"] == "tool_use":
        events += [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})]
    else:
        events += [("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": block["text"]}})]
    return events + [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                     ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                        "usage": {"output_tokens": 1}}),
                     ("message_stop", {"type": "message_stop"})]


def _scripted_api(steps, results, bodies=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if bodies is not None:
                bodies.append(json.dumps(body))
            if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens": 1}')
                return
            seen = [c for m in body.get("messages", []) if m.get("role") == "user" and isinstance(m.get("content"), list)
                    for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]
            if body.get("tools"):
                results[:] = [{"error": bool(c.get("is_error")), "text": json.dumps(c.get("content"))[:300]}
                              for c in seen]
            step = len(seen) if body.get("tools") else len(steps)
            block = dict(steps[step], type="tool_use", id=f"toolu_{step}") if step < len(steps) else \
                {"type": "text", "text": "done"}
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for name, data in _events(block, "tool_use" if block["type"] == "tool_use" else "end_turn"):
                self.wfile.write(("event: " + name + "\ndata: " + json.dumps(data) + "\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_real_claude_does_exactly_what_the_launch_settings_allow(tmp_path):
    with Driver(tmp_path) as driver:
        driver.core_tree()
        driver.ready()
        folder = driver.root / "agent-claude"
        folder.mkdir()
        outside = Path(os.path.realpath(driver.root)) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n")
        worktree = Path(os.path.realpath(driver.repo_a)) / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1"
        rehydrate = Path(os.path.realpath(driver.tracker)) / "projects" / "alpha-p1" / "documents" / \
            "rehydrate-pm1tl1.md"
        steps = [
            {"name": "Bash", "input": {"command": "ygg inbox pm1tl1", "description": "inbox"}},
            {"name": "Read", "input": {"file_path": str(rehydrate)}},
            {"name": "Write", "input": {"file_path": str(worktree / "notes.md"), "content": "notes\n"}},
            {"name": "Write", "input": {"file_path": str(outside / "evil.md"), "content": "x\n"}},
            {"name": "Bash", "input": {"command": "touch " + str(outside / "evil2"), "description": "touch"}},
            {"name": "Read", "input": {"file_path": str(outside / "secret.txt")}},
        ]
        results = []
        server = _scripted_api(steps, results)
        try:
            profile = driver.root / "profile.yaml"
            driver.write(profile, "schema: ygg.config.v1\nname: clean\nagent_claude_config_dir: " + str(folder) +
                         "\nlaunch_env:\n"
                         "  ANTHROPIC_BASE_URL: http://127.0.0.1:" + str(server.server_address[1]) + "\n"
                         "  ANTHROPIC_API_KEY: scripted-placeholder\n"
                         "  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1'\n  DISABLE_AUTOUPDATER: '1'\n")
            driver.env["YGG_PROFILE"] = str(profile)
            driver.env["YGG_CLAUDE_BIN"] = REAL
            agreed = driver.run("agents", "allow-minimal")
            assert agreed.returncode == 0, agreed.stderr
            result = driver.run("launch", "pm1tl1")
            assert result.returncode == 0, result.stderr
            driver.wait_exit("pm1tl1", timeout=120)
        finally:
            server.shutdown()
        assert [r["error"] for r in results] == [False, False, False, True, True, True], results
        assert (worktree / "notes.md").exists()
        assert sorted(os.listdir(outside)) == ["secret.txt"]



def test_the_real_claude_with_the_standard_rules_reads_claude_md_but_not_the_projects_settings(tmp_path):
    import subprocess
    with Driver(tmp_path) as driver:
        driver.core_tree()
        repo = Path(os.path.realpath(driver.repo_a))
        marks = driver.root / "marks"
        marks.mkdir()
        (repo / "CLAUDE.md").write_text("Project rule: MARKER-CLAUDE-MD-5521\n")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "touch " + str(marks / "project-hook-ran")}]}]}}))
        for args in (["add", "CLAUDE.md", ".claude/settings.json"], ["commit", "-q", "-m", "project files"]):
            subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                            "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)
        driver.ready()
        folder = driver.root / "agent-claude"
        folder.mkdir()
        outside = Path(os.path.realpath(driver.root)) / "outside"
        outside.mkdir()
        lane = "ygg/alpha-p1/lane/pm1tl1"
        steps = [
            {"name": "Bash", "input": {"command": "git status", "description": "status"}},
            {"name": "Bash", "input": {"command": "git log --oneline -n 1", "description": "log"}},
            {"name": "Bash", "input": {"command": "true", "description": "the programme's test command"}},
            {"name": "Bash", "input": {"command": "git diff --output=" + str(outside / "o.txt"), "description": "x"}},
            {"name": "Bash", "input": {"command": "git push origin " + lane, "description": "push the lane"}},
            {"name": "Bash", "input": {"command": "git push origin ygg/alpha-p1/lane/other", "description": "x"}},
            {"name": "Bash", "input": {"command": "git checkout -b scratch", "description": "not read-only"}},
            {"name": "Bash", "input": {"command": "git diff --stat --no-index /etc/hosts /dev/null", "description": "x"}},
            {"name": "Bash", "input": {"command": "git log -- " + str(outside), "description": "outside"}},
            {"name": "Bash", "input": {"command": "git commit --no-verify --allow-empty -m x", "description": "x"}},
        ]
        results, bodies = [], []
        server = _scripted_api(steps, results, bodies)
        try:
            profile = driver.root / "profile.yaml"
            driver.write(profile, "schema: ygg.config.v1\nname: clean\nagent_claude_config_dir: " + str(folder) +
                         "\nagent_commands:\n  alpha: ['true']\nlaunch_env:\n"
                         "  ANTHROPIC_BASE_URL: http://127.0.0.1:" + str(server.server_address[1]) + "\n"
                         "  ANTHROPIC_API_KEY: scripted-placeholder\n"
                         "  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1'\n  DISABLE_AUTOUPDATER: '1'\n")
            driver.env["YGG_PROFILE"] = str(profile)
            driver.env["YGG_CLAUDE_BIN"] = REAL
            agreed = driver.run("agents", "allow-standard", "--yes")
            assert agreed.returncode == 0, agreed.stderr
            result = driver.run("launch", "pm1tl1")
            assert result.returncode == 0, result.stderr
            driver.wait_exit("pm1tl1", timeout=120)
        finally:
            server.shutdown()
        # Denied by Claude Code's rules ("Permission to use") or by Yggdrasil's guard hook ("denied").
        denied = ["denied" in r["text"].lower() for r in results]
        assert denied == [False, False, False, True, False, True, True, True, True, True], results
        assert any("MARKER-CLAUDE-MD-5521" in body for body in bodies)  # the project's instructions reach the agent
        assert not (marks / "project-hook-ran").exists()  # the project's settings (its hooks) do not
        assert not (outside / "o.txt").exists()
