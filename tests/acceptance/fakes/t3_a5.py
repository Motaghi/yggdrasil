"""Stateful A5.4 external t3 double; response shapes come from M1."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import select
import sys
import time
import uuid


def main():
    argv = sys.argv[1:]
    config_path = Path(os.environ["YGG_FAKE_CONFIG"])
    config = json.loads(config_path.read_text()).get("t3", {})
    fixtures = json.loads(Path(os.environ["YGG_FAKE_T3_FIXTURE"]).read_text())
    shapes = {step["name"]: step for step in fixtures}
    state_path = config_path.parent / "t3-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "threads": deepcopy(config.get("threads", {})), "projects": {}, "next_thread": 4,
    }
    record = {"binary": "t3", "event": "launch", "argv": argv,
              "cwd": os.getcwd(), "env": dict(os.environ),
              "node": os.environ.get("YGG_NODE"),
              "activation": os.environ.get("YGG_ACTIVATION"),
              "at": datetime.now(timezone.utc).isoformat()}
    record["stdin"] = ""
    if (argv[:2] in (["thread", "start"], ["thread", "send"])
            and sys.stdin is not None and not sys.stdin.closed
            and not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]):
        record["stdin"] = sys.stdin.read()
    descriptor = os.open(os.environ["YGG_FAKE_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record) + "\n").encode())
    finally:
        os.close(descriptor)

    if config.get("sleep_seconds"):
        time.sleep(config["sleep_seconds"])
    if "exit" in config:
        sys.stdout.write(config.get("stdout", ""))
        sys.stderr.write(config.get("stderr", ""))
        return config["exit"]

    def save():
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(state_path)

    def output(value):
        print(json.dumps(value))

    def option(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    def positional(start):
        values = []
        index = start
        while index < len(argv):
            value = argv[index]
            if value in {"--title", "--provider", "--model", "--messages", "--project"}:
                index += 2
            elif value.startswith("--"):
                index += 1
            else:
                values.append(value)
                index += 1
        return values

    def thread_object(identifier, thread):
        result = deepcopy(shapes["thread-show"]["stdout"]["thread"])
        result.update(threadId=identifier, title=thread["title"], worktreePath=thread["project"])
        result["projectId"] = next((key for key, value in state["projects"].items()
                                    if value["path"] == thread["project"]), result["projectId"])
        result["link"] = "https://app.t3.codes/threads/fake/" + identifier
        result["providerInstanceId"] = thread.get("provider", result["providerInstanceId"])
        result["model"] = thread.get("model", result["model"])
        result["status"] = thread.get("status", result["status"])
        if "active" in thread:
            result["activeRunId"] = result["latestRunId"] if thread["active"] else None
        result["archived"] = thread.get("archived", result["archived"])
        result["pendingRequestCount"] = thread.get("pending_requests", result["pendingRequestCount"])
        return result

    if argv == ["--version"]:
        print("t3 v" + config.get("cli_version", "0.1.9"))
        return 0
    if argv == ["service", "status"]:
        text = shapes["server-version"]["stdout"]
        text = re.sub(r"t3@\d+\.\d+\.\d+", "t3@" + config.get("server_version", "0.1.9"), text, count=1)
        sys.stdout.write(text)
        return 0
    if argv[:2] == ["project", "add"]:
        identifier = str(uuid.uuid4())
        title, path = option("--title"), positional(2)[-1]
        state["projects"][identifier] = {"title": title, "path": path}
        save()
        print("Added project " + identifier + " (" + title + ") at " + path + ".")
        return 0
    if argv[:2] == ["project", "remove"]:
        identifier = positional(2)[-1]
        project = state["projects"].pop(identifier)
        state["threads"] = {key: value for key, value in state["threads"].items()
                            if value["project"] != project["path"]}
        save()
        print("Removed project " + identifier + " (" + project["title"] + ").")
        return 0
    if argv[:2] == ["thread", "list"]:
        output([thread_object(key, value) for key, value in state["threads"].items()
                if value["project"] == option("--project")])
        return 0
    if argv[:2] == ["thread", "show"]:
        identifier = positional(2)[-1]
        thread = state["threads"].get(identifier)
        if thread is None:
            print("Thread " + identifier + " is no longer available.", file=sys.stderr)
            return 1
        if "show" in thread:
            response = thread["show"]
            sys.stdout.write(response.get("stdout", ""))
            sys.stderr.write(response.get("stderr", ""))
            return response.get("exit", 0)
        result = deepcopy(shapes["thread-show"]["stdout"])
        result["thread"] = thread_object(identifier, thread)
        messages = thread.get("messages", [])
        limit = option("--messages")
        messages = messages[-int(limit):] if limit else messages
        result["messages"] = [dict(message, runId=message.get("runId", result["thread"]["latestRunId"]))
                              for message in messages]
        output(result)
        return 0
    if argv[:2] in (["thread", "start"], ["thread", "send"]):
        start = argv[1] == "start"
        behavior = config.get("thread_start" if start else "thread_send", {})
        outcome = behavior.get("outcome", "ok")
        if outcome == "failed":
            output({"status": "failed", "error": "run failed"})
            return 1
        values = positional(2)
        if start:
            identifier = "th-" + str(state["next_thread"])
            state["next_thread"] += 1
            project, text = values[-2:]
            thread = {"project": project, "title": option("--title", ""),
                      "provider": option("--provider"), "model": option("--model"),
                      "messages": [{"role": "user", "text": text}]}
            result = deepcopy(shapes["thread-start-worktree-wait"]["stdout"])
        else:
            identifier, text = values[-2:]
            thread = state["threads"].get(identifier)
            if thread is None:
                print("Thread " + identifier + " is no longer available.", file=sys.stderr)
                return 1
            thread = deepcopy(thread)
            thread.setdefault("messages", []).append({"role": "user", "text": text})
            result = deepcopy(shapes["thread-send"]["stdout"])
        if outcome == "ok" or behavior.get("deliver", False):
            state["threads"][identifier] = thread
        save()
        if outcome == "unknown":
            print("request timed out", file=sys.stderr)
            return 1
        result["threadId"] = identifier
        result["runId"] = "run:thread:" + identifier + ":ordinal:" + str(len(thread["messages"]))
        if "link" in result:
            result["link"] = thread_object(identifier, thread)["link"]
        output(result)
        return 0
    print("unsupported fake t3 command", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
