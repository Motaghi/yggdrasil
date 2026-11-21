import argparse
from collections import Counter
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest


class Collector:
    def __init__(self):
        self.rows = []
        self.errors = []

    def pytest_collection_modifyitems(self, items):
        for item in items:
            ac = item.get_closest_marker('ac')
            release = item.get_closest_marker('release')
            params = getattr(getattr(item, 'callspec', None), 'params', {})
            match = re.search(r'_row(\d+)_', item.originalname or item.name)
            row = params.get('row', int(match[1]) if match else None)
            self.rows.append({'name': item.nodeid, 'criteria': list(ac.args) if ac else [],
                              'release': release.args[0] if release and len(release.args) == 1 else None,
                              'row': row})
            if not ac or not ac.args or not release or row is None:
                self.errors.append('missing AC, release or example row: ' + item.nodeid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', type=Path, default=Path(__file__).resolve().parents[1] / 'spec-a11-1.md')
    parser.add_argument('--tests', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--results', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    criteria = {}
    for match in re.finditer(r'(?ms)^\*\*(AC-\d+)\.(.*?)(?=^\*\*AC-|^## Items without|\Z)', args.spec.read_text()):
        identifier, block = match[1], match[2]
        release = re.search(r'^Release: ([^;\n]+); Lane: (.+)$', block, re.M)
        table = block.split('| Given | When | Then |', 1)[1]
        examples = []
        for line in table.splitlines()[2:]:
            if not line.startswith('|'):
                break
            examples.append(line)
        criteria[identifier] = {'release': release[1], 'lanes': release[2], 'examples': examples}
    collector = Collector()
    code = pytest.main([str(args.tests), '--collect-only', '-q'], plugins=[collector])
    parked = {line.split(':', 1)[0] for line in (args.tests / 'spec-gaps.md').read_text().splitlines() if line.strip()}
    covered = {ac for row in collector.rows for ac in row['criteria']}
    errors = list(collector.errors)
    for row in collector.rows:
        for ac in row['criteria']:
            if ac not in criteria or criteria[ac]['release'] != row['release']:
                errors.append('unknown AC or incorrect release: ' + row['name'])
            if ac in parked:
                errors.append('parked criterion has a test: ' + ac)
    for ac, criterion in criteria.items():
        found = {row['row'] for row in collector.rows if ac in row['criteria']}
        if ac not in parked and found != set(range(1, len(criterion['examples']) + 1)):
            errors.append('example rows missing or unknown: ' + ac + ' ' + str(sorted(found)))
    cases = {}
    if args.results:
        for case in ET.parse(args.results).iter('testcase'):
            cases[case.attrib['name']] = ('failed' if case.find('failure') is not None else
                'error' if case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed')
    for row in collector.rows:
        row['result'] = cases.get(row['name'].rsplit('::', 1)[-1])
    report = {'criteria': criteria, 'covered': sorted(covered), 'parked': sorted(parked),
              'missing': sorted(set(criteria) - covered - parked), 'errors': errors,
              'total': len(collector.rows), 'counts': dict(Counter(cases.values())), 'tests': collector.rows}
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ['covered', 'parked', 'missing', 'errors', 'total', 'counts']}, indent=2))
    return int(bool(code or errors or report['missing']))


if __name__ == '__main__':
    raise SystemExit(main())
