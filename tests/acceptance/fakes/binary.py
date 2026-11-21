"""Recording external executables with the A1 scripted-agent protocol."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def main():
    binary = Path(sys.argv[0]).name
    config_path = Path(os.environ["YGG_FAKE_CONFIG"])
    config = json.loads(config_path.read_text())
    node = os.environ.get("YGG_NODE")
    behavior = config.get(binary, {})
    agent = dict(behavior)
    agent.update(behavior.get("agents", {}).get(node, {}))
    record = {"binary": binary, "argv": sys.argv[1:], "cwd": os.getcwd(),
              "env": dict(os.environ), "node": node,
              "activation": os.environ.get("YGG_ACTIVATION")}

    record["stdin"] = ""
    if (binary in {"claude", "codex", "phone-push"} and sys.stdin is not None
            and not sys.stdin.closed and not sys.stdin.isatty()):
        record["stdin"] = sys.stdin.read()

    def log(event, **fields):
        line = dict(record, event=event, at=datetime.now(timezone.utc).isoformat(), **fields)
        descriptor = os.open(os.environ["YGG_FAKE_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(line) + "\n").encode())
        finally:
            os.close(descriptor)

    log("launch")
    if binary == "gh" and sys.argv[1:2] == ["api"]:
        match = re.fullmatch(r"repos/(.+)/branches/main/protection", sys.argv[2] if len(sys.argv) > 2 else "")
        if match and behavior.get("protected", {}).get(match[1]):
            safe = {
                "enforce_admins": {"enabled": True},
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
                "required_pull_request_reviews": {"required_approving_review_count": 1},
            }
            body = behavior.get("protection_bodies", {}).get(match[1], safe)
            print(json.dumps(body))
            return 0
        if match and behavior.get("protected", {}).get(match[1]):
            print(json.dumps({"url": "https://example.invalid/protection"}))
            return 0
        print("HTTP 404: Branch not protected", file=sys.stderr)
        return 1
    if binary not in {"claude", "codex"}:
        if behavior.get("sleep_seconds"):
            time.sleep(behavior["sleep_seconds"])
        sys.stdout.write(behavior.get("stdout", ""))
        sys.stderr.write(behavior.get("stderr", ""))
        return behavior.get("exit", 0)
    if config.get("load_after_launch"):
        Path(os.environ["YGG_HOST_LOAD_FILE"]).write_text(config["load_after_launch"])
    control = config_path.parent / "fake-control" / (node or "unmanaged")
    control.mkdir(parents=True, exist_ok=True)

    def interrupted(signum, frame):
        log("interrupt", signal=signum)
        log("exit", exit_code=130)
        raise SystemExit(130)

    if agent.get("child"):
        child = agent["child"] if isinstance(agent["child"], dict) else {}
        child_config = dict(agent, **child)
        child_pid = os.fork()
        if child_pid == 0:
            child_record = dict(record)
            record["pid"] = os.getpid()
            descriptor = None
            if child_config.get("hold_index_lock"):
                lock = subprocess.run(["git", "rev-parse", "--git-path", "index.lock"],
                                      capture_output=True, text=True, check=True).stdout.strip()
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
            def child_signal(signum, frame):
                log("child-signal", signal=signum, pid=os.getpid())
                if signal.Signals(signum).name.removeprefix("SIG") not in child_config.get("ignore", []):
                    raise SystemExit(128 + signum)
            signal.signal(signal.SIGINT, child_signal)
            signal.signal(signal.SIGTERM, child_signal)
            log("child-start", pid=os.getpid())
            time.sleep(child_config.get("child_seconds", 600))
            if descriptor is not None:
                os.close(descriptor)
            log("child-exit", pid=os.getpid())
            os._exit(0)
        log("child", pid=child_pid)

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    mode = agent.get("mode", "script" if "steps" in agent else "exit")
    if mode == "capacity":
        print("HTTP 429 Too Many Requests", file=sys.stderr)
        log("exit", exit_code=1)
        return 1
    if mode == "crash":
        os.kill(os.getpid(), signal.SIGKILL)

    def ygg(*args):
        return subprocess.run(["ygg", *args], capture_output=True, text=True, check=False, timeout=30)

    def status():
        result = ygg("status", "--json")
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {}

    def condition(when):
        if not when:
            return True
        current = status()
        projects = {p["id"]: p for p in current.get("projects", [])}
        packages = {p["id"]: p for p in current.get("work_packages", [])}
        if "pid_approved" in when:
            project = projects.get(when["pid_approved"], {})
            if project.get("state") not in {"executing", "awaiting-approval", "exception"}:
                return False
        for project, expected in when.get("stage_state", {}).items():
            stage = projects.get(project, {}).get("stage") or {}
            if any(stage.get(key) != value for key, value in expected.items()):
                return False
        for name, records in (("project_state", projects), ("package_state", packages)):
            if any(records.get(key, {}).get("state") != value for key, value in when.get(name, {}).items()):
                return False
        if "has_criteria" in when:
            package = when["has_criteria"]
            project = packages.get(package, {}).get("project")
            if not project:
                return False
            path = Path(os.environ["YGG_FILES_ROOT"]) / "projects" / project / "issues" / (package + ".md")
            text = path.read_text() if path.exists() else ""
            if not re.search(r"(?m)^kind: criteria\s*$", text):
                return False
        return True

    if "steps" in agent:
        project = os.environ.get("YGG_PROJECT")
        rehydrate = Path(os.environ["YGG_FILES_ROOT"]) / "projects" / (project or "") / "documents" / ("rehydrate-" + (node or "") + ".md")
        if rehydrate.exists():
            rehydrate.read_text()
        if agent.get("auto_ack", True):
            ygg("inbox", node)
            for item in status().get("inbox_pending", []):
                if item.get("node") == node:
                    ygg("inbox", node, "--ack", item["msg_id"])
        if os.environ.get("YGG_REVIEW_DIR"):
            control = control / os.environ["YGG_ACTIVATION"]
            control.mkdir(parents=True, exist_ok=True)
        done_path = control / "done"
        done = set(done_path.read_text().splitlines()) if done_path.exists() else set()
        def expand(argument):
            argument = argument.replace("{node}", node or "")
            argument = re.sub(r"\{env:([^}]+)\}", lambda match: os.environ.get(match[1], ""), argument)
            def head(match):
                return subprocess.run(["git", "rev-parse", match[1]], check=True,
                                      capture_output=True, text=True).stdout.strip()
            return re.sub(r"\{head:([^}]+)\}", head, argument)

        for step in agent["steps"]:
            if step["id"] in done:
                continue
            if not condition(step.get("when", {})):
                break
            code = 0
            if "run" in step:
                arguments = [expand(argument) for argument in step["run"]]
                import shlex
                payload = {"cwd": os.getcwd(), "tool_use_id": "tu-" + step["id"],
                           "session_id": "s-1", "permission_mode": "dontAsk",
                           "transcript_path": str(config_path.parent / "transcript.jsonl"),
                           "tool_name": "Bash", "tool_input": {"command": shlex.join(arguments)}}
                def hook(event, body):
                    return subprocess.run(["ygg", "hook", event], input=json.dumps(body),
                                          capture_output=True, text=True, check=False, timeout=30)
                if binary == "claude" and agent.get("guarded"):
                    pre = hook("pretool", payload)
                    log("pretool", id=step["id"], exit_code=pre.returncode,
                        stdout=pre.stdout, stderr=pre.stderr)
                    if pre.returncode == 2:
                        log("step-refused", id=step["id"], exit_code=2, stderr=pre.stderr)
                        continue
                result = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=30)
                code = result.returncode
                if binary == "claude" and agent.get("posttool"):
                    post = dict(payload)
                    if code == 0:
                        post["tool_response"] = {"interrupted": False, "isImage": False,
                                                 "noOutputExpected": False, "stderr": result.stderr,
                                                 "stdout": result.stdout}
                    else:
                        post.update(error="Exit code " + str(code), is_interrupt=code < 0)
                    hook("posttool", post)
                if agent.get("guarded") or agent.get("posttool"):
                    log("step-output", id=step["id"], stdout=result.stdout, stderr=result.stderr)
            if "wait" in step:
                while not (control / step["wait"]).exists():
                    time.sleep(0.05)
            if "write_file" in step:
                target = Path(expand(step["write_file"]["path"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(step["write_file"]["text"])
            log("step", id=step["id"], exit_code=code)
            done.add(step["id"])
            done_path.write_text("".join(value + "\n" for value in sorted(done)))
    if mode == "persist":
        while not (control / "exit").exists():
            time.sleep(0.05)
    sys.stdout.write(agent.get("stdout", ""))
    sys.stderr.write(agent.get("stderr", ""))
    code = agent.get("exit_code", agent.get("exit", 0))
    log("exit", exit_code=code)
    return code


if __name__ == "__main__":
    sys.exit(main())
