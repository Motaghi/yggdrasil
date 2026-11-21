import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    criteria = set(re.findall(r"(?m)^\*\*(AC-\d+)\.", args.spec.read_text()))
    tags = {}
    untagged = []
    for path in sorted(args.tests.rglob("test_*.py")):
        module = ast.parse(path.read_text(), filename=str(path))
        for function in module.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith("test_"):
                continue
            ids = []
            for decorator in function.decorator_list:
                if isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "pytest.mark.ac":
                    ids.extend(ast.literal_eval(item) for item in decorator.args)
            if not ids:
                untagged.append(function.name)
            tags[function.name] = ids
    used = {identifier for ids in tags.values() for identifier in ids}
    report = {
        "criteria": sorted(criteria),
        "covered": sorted(criteria & used),
        "missing": sorted(criteria - used),
        "unknown": sorted(used - criteria),
        "untagged": untagged,
        "test_functions": len(tags),
    }
    if args.results:
        root = ET.parse(args.results).getroot()
        rows = []
        for case in root.iter("testcase"):
            name = case.attrib["name"]
            status = ("failed" if case.find("failure") is not None else
                      "error" if case.find("error") is not None else
                      "skipped" if case.find("skipped") is not None else "passed")
            rows.append({"name": name, "criteria": tags[name.split("[", 1)[0]], "result": status})
        report["cases"] = rows
        report["counts"] = dict(Counter(row["result"] for row in rows))
        counts = Counter(identifier for row in rows for identifier in row["criteria"])
        report["tests_per_criterion"] = {identifier: counts[identifier] for identifier in sorted(criteria)}
        report["total"] = len(rows)
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text, end="")
    return int(bool(report["missing"] or report["unknown"] or report["untagged"]))


if __name__ == "__main__":
    raise SystemExit(main())
