"""The basic dashboard and its read-only server (A11 lane 4: AC-08, AC-09, AC-45 to AC-50; release switches
`dashboard` and `dashboard_serve`).

The conductor's `dashboard` step writes `<state root>/dashboard/<programme>.html` for every programme; `ygg
dashboard <programme> [--no-open]` regenerates it, prints its path and opens it. The page is static: no script,
no form, no external URL, no `url(`; every agent-written text is redacted and HTML-escaped. Markup contract:
each column is an element with `data-column` (`to-do`, `working`, `checking`, `done`, `needs-you`), and each
package an element with `data-package` inside its column's element.

`ygg dashboard serve <programme> --port N` (user-only) serves that one file on 127.0.0.1 only: GET `/` answers
200 with the file, any other path 404 `Not found`, any other method 405, and a Host header that isn't
loopback or in `dashboard_allowed_hosts` 403 `Forbidden`.
"""

from __future__ import annotations

import html
import subprocess
from pathlib import Path

from . import config, release, state, tracker, util

COLUMNS = (("to-do", "To do"), ("working", "Working on it"), ("checking", "Being checked"), ("done", "Done"),
           ("needs-you", "Needs you"))
COLUMN_OF = {"planned": "to-do", "authorised": "working", "rejected": "working", "in-review": "checking",
             "ready-for-test": "checking", "accepted": "done", "merged": "done", "stopped": "needs-you",
             "review-exhausted": "needs-you", "suspended": "needs-you"}
ROLE_WORDS = {"pm": "project manager", "tl": "team lead", "su": "senior user", "worker": "worker",
              "reviewer": "reviewer"}
SUMMARY_LINES = 5
STALE = "Yggdrasil isn't running. This page may be out of date."
STYLE = ("body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:1.5em;color:#1d2433}"
         ".board{display:flex;gap:1em;align-items:flex-start}.col{flex:1;background:#f3f4f7;padding:.6em;"
         "border-radius:6px}.item{background:#fff;margin:.4em 0;padding:.4em;border-radius:4px}"
         ".banner{background:#fde8e8;padding:.5em}")


def path_for(programme: str) -> Path:
    return state.root() / "dashboard" / (programme + ".html")


def _text(value) -> str:
    """Agent-written text as it may appear on the page: redacted, one line, HTML-escaped."""
    clean = util.redact(str(value if value is not None else "")).replace("\r", " ").replace("\n", " ")
    return html.escape(clean, quote=True)


def _zone():
    name = config.get("timezone") or (config.profile() or {}).get("timezone")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:  # noqa: BLE001 (an unknown zone shows UTC)
        return None


def _updated() -> str:
    moment = util.now()
    zone = _zone()
    if zone is not None:
        moment = moment.astimezone(zone)
    return "Updated " + moment.strftime("%Y-%m-%d %H:%M")


def _stale() -> bool:
    text = util.read_text(state.root() / "conductor.heartbeat").strip()
    if not text:
        return True
    try:
        age = (util.now() - util.parse_time(text)).total_seconds()
    except ValueError:
        return True
    return age > 3 * float(config.get("tick_seconds", 15) or 15)


def _paused_line(programme: str) -> str | None:
    from . import interlock
    view = interlock.paused_view()
    if view["global"]:
        return "Paused: everything."
    if programme in view["programmes"]:
        return "Paused: programme " + programme + "."
    return None


def _checkpoint_summary(project_id: str) -> list[str] | None:
    folder = tracker.updates_dir(project_id)
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*-checkpoint.md"))
    if not files:
        return None
    _, body = util.split_frontmatter(util.read_text(files[-1]))
    lines = [line for line in (body or "").splitlines() if line.strip()]
    return lines or None


def _decision_items(project_id: str) -> list[tuple[str, str]]:
    from . import decisions
    items = []
    for issue in tracker.issues(project_id):
        if issue.get("kind") == "decision" and decisions.is_open(issue):
            items.append((issue.id, "Decision " + _text(issue.id) + ": " + _text(issue.get("question")) +
                          ". Ask your main session about " + _text(issue.id) + "."))
    return items


def _decision_packages(project_id: str) -> set[str]:
    from . import decisions
    found = set()
    for issue in tracker.issues(project_id):
        if issue.get("kind") == "decision" and decisions.is_open(issue):
            scope = issue.get("scope") or {}
            if isinstance(scope, dict) and scope.get("id"):
                found.add(str(scope["id"]))
    return found


def _project_needs(project: dict) -> list[str]:
    from . import mergecheck
    items = []
    project_id = project["id"]
    if project.get("state") == "pid-ready":
        items.append("The plan of " + _text(project_id) + " waits for your approval. Ask your main session to "
                     "show it to you.")
    stages = tracker.milestones(project_id).get("stages") or []
    for stage in stages:
        if stage.get("state") == "awaiting-approval":
            items.append("Stage " + _text(stage.get("number")) + " of " + _text(project_id) + " waits for your "
                         "approval. Ask your main session to show you the stage report.")
    if mergecheck.record(project_id).get("state") == "failed":
        items.append("The merge check of " + _text(project_id) + " failed. Nothing to do yet; the team is "
                     "fixing it.")
    return [item for item in items]


def _package_label(issue) -> str:
    title = issue.get("title")
    return _text(issue.id) + (": " + _text(title) if title else "")


def _agents(programme: str) -> list[str]:
    nodes = state.all_nodes()
    lines = []
    for node_id in sorted(nodes):
        node = nodes[node_id]
        if node.get("programme") != programme:
            continue
        runtime = state.runtime(node_id)
        if runtime.get("state") not in ("running", "stalled-suspect"):
            continue
        line = _text(node_id) + " (" + ROLE_WORDS.get(str(node.get("role")), _text(node.get("role"))) + ")"
        package = _current_package(node)
        if package:
            line += " on " + _text(package)
        doing = _in_flight(node_id, runtime.get("activation"))
        if doing:
            line += ": running " + _text(doing)
        lines.append(line)
    return lines


def _current_package(node: dict) -> str | None:
    if not node.get("project"):
        return None
    lane = node["id"] if node.get("role") == "tl" else node.get("parent")
    working = [issue for issue in tracker.issues(node["project"]) if issue.get("kind") == "work-package"
               and issue.get("lane") == lane and issue.get("state") in ("authorised", "rejected")]
    working.sort(key=lambda issue: _number(issue.id))
    return working[0].id if working else None


def _number(identifier: str) -> int:
    try:
        return int(str(identifier).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _in_flight(node_id: str, activation) -> str | None:
    """The first word of the agent's tool call in flight, from the guard's in-flight record
    (`<state root>/inflight/<activation>/<tool_use_id>.json`, lane 1), if any."""
    if not activation:
        return None
    folder = state.root() / "inflight" / str(activation)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json")):
        data = util.read_json(path, {}) if path.is_file() else {}
        command = str((data or {}).get("command") or "").split() if isinstance(data, dict) else []
        if command:
            return command[0]
    return None


def _rulings(programme: str) -> list[str]:
    """Recorded rulings (lane 2's `ygg ruling record`, read through `review.rulings`), oldest first."""
    try:
        from . import review
        source = getattr(review, "rulings", None)
    except ImportError:
        source = None
    if source is None:
        return []
    found = []
    for project in tracker.projects():
        if project.get("programme") != programme:
            continue
        for row in source(project["id"]):
            if isinstance(row, dict) and row.get("id"):
                found.append("Ruling " + _text(row["id"]) + ": " + _text(row.get("text")))
    return found


def render(programme: str, *, check_stale: bool = False) -> str:
    projects = [p for p in tracker.projects() if p.get("programme") == programme and p.get("state") != "closed"]
    columns: dict[str, list[str]] = {key: [] for key, _ in COLUMNS}
    needs: list[str] = []
    summaries: list[tuple[str, list[str] | None]] = []
    for project in projects:
        held = _decision_packages(project["id"])
        for issue in sorted(tracker.issues(project["id"]), key=lambda i: _number(i.id)):
            if issue.get("kind") != "work-package":
                continue
            column = COLUMN_OF.get(str(issue.get("state", "planned")))
            if column is None:
                continue  # cancelled (or unknown): not shown
            if issue.id in held:
                column = "needs-you"
            columns[column].append('<div class="item" data-package="' + _text(issue.id) + '">' +
                                   _package_label(issue) + "</div>")
        needs += [text for _, text in _decision_items(project["id"])]
        needs += _project_needs(project)
        summaries.append((project["id"], _checkpoint_summary(project["id"])))
    out = []
    paused = _paused_line(programme)
    if paused:
        out.append(paused)
    out += ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
            "<title>" + _text(programme) + " dashboard</title>", "<style>" + STYLE + "</style>", "</head>",
            "<body>", "<h1>" + _text(programme) + "</h1>", "<p>" + _updated() + "</p>"]
    if paused:
        out.append('<p class="banner">' + paused + "</p>")
    if check_stale and _stale():
        out.append('<p class="banner">' + STALE + "</p>")
    out.append('<div class="board">')
    for key, heading in COLUMNS:
        out.append('<section class="col" data-column="' + key + '">')
        out.append("<h2>" + heading + "</h2>")
        out += columns[key]
        if key == "needs-you":
            out += ['<div class="item">' + item + "</div>" for item in needs]
        out.append("</section>")
    out.append("</div>")
    out.append("<h2>Summary</h2>")
    shown = False
    for project_id, lines in summaries:
        if not lines:
            continue
        shown = True
        out.append("<h3>" + _text(project_id) + "</h3>")
        out += ["<p>" + _text(line) + "</p>" for line in lines[:SUMMARY_LINES]]
        if len(lines) > SUMMARY_LINES:
            out.append("<p>(shortened)</p>")
    if not shown:
        out.append("<p>No summary yet.</p>")
    out.append("<h2>Agents</h2>")
    agents = _agents(programme)
    out += ["<p>" + line + "</p>" for line in agents] or ["<p>No agent is running.</p>"]
    rulings = _rulings(programme)
    out.append("<h2>Rulings</h2>")
    out += ["<p>" + line + "</p>" for line in rulings] or ["<p>No rulings yet.</p>"]
    out += ["</body>", "</html>"]
    return "\n".join(out) + "\n"


def write(programme: str, *, check_stale: bool = False) -> Path:
    path = path_for(programme)
    util.ensure_dir(path.parent)
    util.atomic_write(path, render(programme, check_stale=check_stale))
    return path


def tick_step() -> None:
    """The conductor's `dashboard` step (AFTER_LAUNCH_STEPS): rewrite every programme's page."""
    if not release.on("dashboard"):
        return
    for programme in tracker.programmes():
        try:
            write(programme["id"])
        except Exception:  # noqa: BLE001, S112 (a page that can't be written never stops the tick)
            continue


def _programme(name: str) -> str:
    util.check_id(name, "programme")
    if tracker.programme(name) is None:
        raise util.usage("unknown programme '" + name + "'")
    return name


def command(words: list[str], no_open: bool, port) -> str:
    """`ygg dashboard <programme> [--no-open]` and `ygg dashboard serve <programme> --port N`."""
    if words and words[0] == "serve":
        release.refuse("dashboard_serve")
        if len(words) != 2:
            raise util.usage("ygg dashboard serve <programme> --port <port>")
        state.check_user_only("dashboard serve")
        serve(_programme(words[1]), int(port or 0))
        return ""
    release.refuse("dashboard")
    if len(words) != 1:
        raise util.usage("ygg dashboard <programme> [--no-open]")
    path = write(_programme(words[0]), check_stale=True)
    if not no_open:
        try:
            subprocess.run(["open", str(path)], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "Dashboard: " + str(path) + "\n"


def _allowed_hosts(port: int) -> set[str]:
    names = {"127.0.0.1", "localhost"}
    extra = config.get("dashboard_allowed_hosts") or []
    if isinstance(extra, list):
        names |= {str(name).lower() for name in extra}
    return names | {name + ":" + str(port) for name in names}


def serve(programme: str, port: int) -> None:
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    path = path_for(programme)
    if not path.exists():
        write(programme)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Yggdrasil"
        sys_version = ""

        def log_message(self, *args):
            return

        def _answer(self, code: int, body: str, extra: dict | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if code == 200 else
                             "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            return host in allowed

        def do_GET(self):
            if not self._host_ok():
                return self._answer(403, "Forbidden")
            if self.path.split("?", 1)[0] != "/":
                return self._answer(404, "Not found")
            try:
                body = path.read_text()
            except OSError:
                return self._answer(404, "Not found")
            return self._answer(200, body)

        def do_HEAD(self):
            return self.do_GET()

        def _refuse(self):
            if not self._host_ok():
                return self._answer(403, "Forbidden")
            return self._answer(405, "Method not allowed", {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    server = HTTPServer(("127.0.0.1", port), Handler)
    bound = server.server_address[1]
    allowed = _allowed_hosts(bound)
    sys.stdout.write("Serving http://127.0.0.1:" + str(bound) + "/ (read-only). Press Ctrl-C to stop.\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
