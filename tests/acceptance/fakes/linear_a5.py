"""A5.8 stateful HTTP double, executed against the pinned published SDL."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import time
from urllib.parse import parse_qs
import uuid

from graphql import (
    GraphQLError, build_schema, execute_sync, get_operation_ast,
    get_variable_values, parse, validate,
)
from graphql import extend_schema


ASSETS = Path(__file__).parent / "m1"
COLLECTIONS = {
    "Initiative": "initiatives", "InitiativeToProject": "links",
    "Project": "projects", "ProjectMilestone": "milestones", "Issue": "issues",
    "IssueLabel": "labels", "Comment": "comments", "ProjectUpdate": "updates",
    "Document": "documents",
}
PLURALS = {"initiatives": "Initiative", "initiativeToProjects": "InitiativeToProject",
           "projects": "Project", "projectMilestones": "ProjectMilestone",
           "issues": "Issue", "issueLabels": "IssueLabel", "comments": "Comment",
           "projectUpdates": "ProjectUpdate", "documents": "Document", "teams": "Team"}
RELATIONS = {"initiative": ("Initiative", "initiativeId"),
             "project": ("Project", "projectId"), "team": ("Team", "teamId"),
             "parent": ("Issue", "parentId"), "issue": ("Issue", "issueId"),
             "projectMilestone": ("ProjectMilestone", "projectMilestoneId")}


def load_schema():
    source = ASSETS / "linear-schema" / "schema.graphql"
    pin = (source.parent / "PIN.txt").read_text()
    expected = next(line.split(": ", 1)[1] for line in pin.splitlines()
                    if line.startswith("sha256: "))
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Vendored Linear schema does not match PIN.txt")
    return build_schema(source.read_text())


class Workspace:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.wire = json.loads((ASSETS / "linear-wire.json").read_text())
        self.schema = load_schema()
        self.schema = extend_schema(
            self.schema, parse((ASSETS / "linear-schema" / "live-delta.graphql").read_text()))
        self.path = self.directory / "state.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {name: [] for name in COLLECTIONS.values()}
            viewer = self.fixture("linear", "01-viewer.json")["body"]["data"]
            team = self.fixture("linear", "02-team-by-key.json")["body"]["data"]["teams"]["nodes"][0]
            self.state.update(viewer=viewer["viewer"], organization=viewer["organization"], teams=[team])
            self.save()
        self.payloads = {}
        self.defaults = {}
        for folder in self.wire.values():
            for pairs in folder.values():
                for pair in pairs:
                    data = pair["response"]["body"].get("data", {})
                    for operation, payload in data.items():
                        if isinstance(payload, dict) and "success" in payload:
                            fields = [key for key in payload if key != "success"]
                            self.payloads[operation] = fields
                            for field in fields:
                                self.defaults.setdefault(field, {}).update(payload[field] or {})

    def fixture(self, folder, name):
        return self.wire[folder][name][0]["response"]

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.replace(self.path)

    def rows(self, kind):
        return self.state[COLLECTIONS.get(kind, "teams")]

    def find(self, kind, identifier):
        return next((row for row in self.rows(kind)
                     if row["id"] == identifier or row.get("identifier") == identifier), None)

    def relation(self, row, field):
        kind, key = RELATIONS[field]
        return self.find(kind, row.get(key))

    def matches(self, row, filters):
        for field, condition in (filters or {}).items():
            if field == "and":
                if not all(self.matches(row, item) for item in condition):
                    return False
                continue
            if field == "or":
                if not any(self.matches(row, item) for item in condition):
                    return False
                continue
            value = self.relation(row, field) if field in RELATIONS else row.get(field)
            if not isinstance(condition, dict):
                raise GraphQLError("Unsupported fake filter: " + field)
            for operator, expected in condition.items():
                if operator == "eq":
                    matched = value == expected
                elif operator == "neq":
                    matched = value != expected
                elif operator == "in":
                    matched = value in expected
                elif operator == "nin":
                    matched = value not in expected
                elif operator == "contains":
                    matched = expected in (value or "")
                elif operator == "startsWith":
                    matched = (value or "").startswith(expected)
                elif operator == "null":
                    matched = (value is None) == expected
                elif isinstance(value, dict):
                    matched = self.matches(value, {operator: expected})
                else:
                    raise GraphQLError("Unsupported fake filter operator: " + operator)
                if not matched:
                    return False
        return True

    def connection(self, rows, arguments):
        rows = [row for row in rows if not row.get("trashed")
                and (arguments.get("includeArchived") or not row.get("archivedAt"))
                and self.matches(row, arguments.get("filter"))]
        after = arguments.get("after")
        start = 0
        if after is not None:
            indexes = [index for index, row in enumerate(rows) if row["id"] == after]
            if not indexes:
                raise GraphQLError("Unknown pagination cursor")
            start = indexes[0] + 1
        size = min(arguments.get("first", 50), 50)
        page = rows[start:start + size]
        return {"nodes": page, "edges": [{"node": row, "cursor": row["id"]} for row in page],
                "pageInfo": {"hasNextPage": start + len(page) < len(rows),
                             "hasPreviousPage": start > 0,
                             "startCursor": page[0]["id"] if page else None,
                             "endCursor": page[-1]["id"] if page else None}}

    def mutate(self, field, arguments):
        suffix = next((ending for ending in ("Create", "Update", "Delete") if field.endswith(ending)), None)
        if suffix is None:
            raise GraphQLError("Unsupported fake mutation: " + field)
        stem = field[:-len(suffix)]
        kind = stem[0].upper() + stem[1:]
        if kind not in COLLECTIONS:
            raise GraphQLError("Unsupported fake mutation: " + field)
        values = deepcopy(arguments.get("input", {}))
        if kind == "Document":
            # DocumentUpdateInput leaves content unchanged for null or empty input.
            if suffix == "Update" and not values.get("content"):
                values.pop("content", None)
            values["updatedById"] = self.state["viewer"]["id"]
        if suffix == "Create":
            row = deepcopy(self.defaults.get(stem, {}))
            # Fixture identities and relationships are replaced by this workspace's inputs.
            for key in ("id", "identifier", "url", "labels", *RELATIONS):
                row.pop(key, None)
            row.update(values)
            row["id"] = values.get("id") or str(uuid.uuid4())
            row["__typename"] = kind
            row["createdAt"] = row["updatedAt"] = datetime.now(timezone.utc).isoformat()
            row["creatorId"] = self.state["viewer"]["id"]
            row["trashed"] = False
            row["archivedAt"] = None
            if kind == "Issue":
                row["identifier"] = "YGG-" + str(len(self.rows(kind)) + 1)
                row["url"] = "https://linear.app/example-org/issue/" + row["identifier"]
                row.setdefault("labelIds", [])
            if kind == "Project":
                row["url"] = "https://linear.app/example-org/project/" + row["id"]
            if kind == "Document":
                row["slugId"] = row["id"]
                row.setdefault("sortOrder", float(len(self.rows(kind))))
                row["url"] = "https://linear.app/example-org/document/" + row["id"]
            self.rows(kind).append(row)
        else:
            row = self.find(kind, arguments.get("id"))
            if row is None:
                raise GraphQLError("Unknown " + kind + " id")
            row.update(values)
            row["updatedAt"] = datetime.now(timezone.utc).isoformat()
            if suffix == "Delete":
                row.update(trashed=True, archivedAt=row["updatedAt"])
        self.save()
        fields = self.payloads.get(field, [stem] if suffix != "Delete" else [])
        return {"success": True, **{key: row for key in fields}}

    def resolve(self, source, info, **arguments):
        field = info.field_name
        if info.parent_type == self.schema.mutation_type:
            return self.mutate(field, arguments)
        if info.parent_type == self.schema.query_type:
            if field in ("viewer", "organization"):
                return self.state[field]
            if field in PLURALS:
                return self.connection(self.rows(PLURALS[field]), arguments)
            kind = field[0].upper() + field[1:]
            if kind in COLLECTIONS or kind == "Team":
                return self.find(kind, arguments.get("id"))
            raise GraphQLError("Unsupported fake query: " + field)
        source = source or {}
        if field in source:
            return source[field]
        if field in RELATIONS:
            return self.relation(source, field)
        if field == "updatedBy" and info.parent_type.name == "Document":
            return self.state["viewer"] if source.get("updatedById") else None
        if field in ("creator", "user"):
            return self.state["viewer"]
        if field == "organization":
            return self.state["organization"]
        if field == "labels":
            return self.connection([row for row in self.rows("IssueLabel")
                                    if row["id"] in source.get("labelIds", [])], arguments)
        if field == "teams":
            return self.connection([row for row in self.rows("Team")
                                    if row["id"] in source.get("teamIds", [])], arguments)
        if field in PLURALS or field == "children":
            kind = "Issue" if field == "children" else PLURALS[field]
            rows = self.rows(kind)
            parent_kind = source.get("__typename", info.parent_type.name)
            if parent_kind == "Team" and kind == "Project":
                return self.connection([row for row in rows
                                        if source["id"] in row.get("teamIds", [])], arguments)
            if parent_kind == "Initiative" and kind == "Project":
                ids = [link["projectId"] for link in self.state["links"]
                       if link["initiativeId"] == source["id"]]
                rows = [row for row in rows if row["id"] in ids]
            elif parent_kind == "Project" and kind == "Initiative":
                ids = [link["initiativeId"] for link in self.state["links"]
                       if link["projectId"] == source["id"]]
                rows = [row for row in rows if row["id"] in ids]
            else:
                key = "parentId" if field == "children" else parent_kind[0].lower() + parent_kind[1:] + "Id"
                rows = [row for row in rows if row.get(key) == source.get("id")]
            return self.connection(rows, arguments)
        if field in ("description", "content", "body", "email", "displayName"):
            return None
        raise GraphQLError("Unsupported fake field: " + info.parent_type.name + "." + field)

    def graphql(self, body, *, execute=True):
        try:
            document = parse(body["query"])
            errors = validate(self.schema, document)
            operation = get_operation_ast(document, body.get("operationName"))
            if operation is None:
                errors.append(GraphQLError("Select one operation"))
            if not errors:
                variables = get_variable_values(self.schema, operation.variable_definitions or (),
                                               body.get("variables") or {})
                if isinstance(variables, list):
                    errors.extend(variables)
            if errors:
                return 400, {"errors": [error.formatted for error in errors]}, False
            if not execute:
                return 200, {}, True
            result = execute_sync(self.schema, document, variable_values=body.get("variables"),
                                  operation_name=body.get("operationName"), field_resolver=self.resolve)
            response = {"data": result.data}
            if result.errors:
                response["errors"] = [error.formatted for error in result.errors]
            return 200, response, True
        except (GraphQLError, KeyError, TypeError) as error:
            return 400, {"errors": [{"message": str(error)}]}, False


def serve(directory, port=0):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(directory)
    normal_headers = workspace.fixture("linear", "01-viewer.json")["headers"]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            content_type = self.headers.get("Content-Type", "")
            try:
                body = json.loads(raw) if "json" in content_type or self.path == "/graphql" else {
                    key: values[-1] for key, values in parse_qs(raw).items()}
            except ValueError:
                body = None
            record = {"path": self.path, "body": body, "headers": dict(self.headers),
                      "received_at": time.monotonic(), "schema_valid": None}
            status, headers = 404, {}
            response = {"errors": [{"message": "Unknown endpoint"}]}
            control = directory / "fail-next.json"
            if self.path == "/graphql":
                status, response, record["schema_valid"] = workspace.graphql(body, execute=False)
            if self.path == "/graphql" and not record["schema_valid"]:
                pass
            elif control.exists():
                failure = json.loads(control.read_text())
                control.unlink()
                status = failure["status"]
                headers = failure.get("headers", {})
                response = failure.get("body", {})
            elif self.path == "/oauth/token":
                endpoint = directory / "token-endpoint.json"
                token = json.loads(endpoint.read_text()) if endpoint.exists() else {
                    "status": 200, "body": {"access_token": "test-token", "token_type": "Bearer",
                    "expires_in": 2591999, "scope": "read write initiative:read initiative:write"}}
                status, response = token["status"], token["body"]
            elif self.path == "/graphql":
                status, response, record["schema_valid"] = workspace.graphql(body)
            response_headers = {**normal_headers, **headers} if status == 200 else dict(headers)
            response_headers.setdefault("Content-Type", "application/json")
            record["response"] = {"status": status, "headers": response_headers, "body": response}
            with (directory / "requests.jsonl").open("a") as log:
                log.write(json.dumps(record) + "\n")
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    (directory / "url").write_text("http://127.0.0.1:" + str(server.server_address[1]) + "\n")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    serve(args.directory, args.port)
