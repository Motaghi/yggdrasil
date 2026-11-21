"""Pure decoding of the public A1 documents and JSON responses."""

import re

import yaml

from observations import frontmatter


def header(text):
    return yaml.safe_load(frontmatter(text)) or {}


def records(text, heading="Records"):
    match = re.search(r"(?ms)^## " + re.escape(heading) + r"\s*\n(.*?)(?=^## |\Z)", text)
    blocks = re.findall(r"(?ms)^```yaml\s*\n(.*?)^```", match[1] if match else "")
    return [yaml.safe_load(block) for block in blocks]


def entry(status, collection, identifier):
    assert isinstance(status, dict), "Expected a JSON object from the CLI"
    key = "number" if collection == "stages" else "id"
    found = [item for item in status[collection] if item[key] == identifier]
    assert len(found) == 1, "Missing or duplicate " + str(identifier)
    return found[0]


def alerts(driver, kind):
    return [line for line in driver.state_lines("alerts.jsonl") if line["kind"] == kind]


def sections(text):
    return re.findall(r"(?m)^## (.+?)\s*$", text)
