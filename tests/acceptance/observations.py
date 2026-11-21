"""Pure readers for the small frontmatter fields named by the examples."""

import ast
import re


def frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return ""
    try:
        return "\n".join(lines[1:lines.index("---", 1)])
    except ValueError:
        return ""


def unquote(value):
    value = value.strip()
    if value.startswith(("'", '"')):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
    return value


def field(text, name):
    match = re.search(r"(?m)^" + re.escape(name) + r":[ \t]*(.*)$", frontmatter(text))
    return unquote(match[1]) if match else None


def labels(text):
    header = frontmatter(text)
    match = re.search(r"(?m)^labels:[ \t]*(.*)$", header)
    if not match:
        return []
    value = match[1].strip()
    if value.startswith("[") and value.endswith("]"):
        return [unquote(item) for item in value[1:-1].split(",") if item.strip()]
    if value:
        return [unquote(value)]
    values = []
    for line in header[match.end():].splitlines():
        if not line.strip():
            continue
        item = re.match(r"^[ \t]*-[ \t]+(.*)$", line)
        if not item:
            break
        values.append(unquote(item[1]))
    return values


def has_label(documents, label):
    return any(label in labels(text) for _, text in documents)


def document_body(text):
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            return parts[1].lstrip("\r\n")
    return text


def document_title(text):
    title = field(text, "title")
    if title is not None:
        return title
    match = re.search(r"(?m)^#[ \t]+(.+?)[ \t]*#*[ \t]*$", document_body(text))
    return match[1] if match else None


def comments(text):
    match = re.search(r"(?m)^## Comments[ \t]*$", text)
    if not match:
        return None
    remainder = text[match.end():]
    boundary = re.search(r"(?m)^#{1,2}[ \t]+", remainder)
    return remainder[:boundary.start()] if boundary else remainder
