import json
import os
from pathlib import Path
import re


activation = os.environ['YGG_ACTIVATION']
records = [json.loads(line) for line in Path(os.environ['YGG_FAKE_LOG']).read_text().splitlines() if line.strip()]
launch = next(record for record in reversed(records)
              if record.get('event') == 'launch' and record.get('activation') == activation)
texts = [launch.get('stdin', '')]
for argument in launch['argv']:
    if len(argument) > 4096:
        continue
    path = Path(argument)
    if not path.is_absolute():
        path = Path(launch['cwd']) / path
    try:
        if path.is_file():
            texts.append(path.read_text())
    except OSError:
        pass
match = re.search(r'\brv-\d{4}\b', '\n'.join(texts))
if match is None:
    raise SystemExit('assigned review id is missing from the public launch prompt')
Path(os.environ['YGG_REVIEW_DIR'], 'verdict.json').write_text(json.dumps({'review': match[0], 'verdict': 'approve'}))
