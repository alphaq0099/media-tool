"""Exercise packaged PDF/image/crypto workers through normal job execution."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from PIL import Image
from pypdf import PdfReader
from media_tool.domain import FileRecord
from media_tool.documents import plan_documents, document_snapshot
from media_tool.jobs import Store, execute_job
from media_tool.planning import PlanRequest
from media_tool.private_values import remember

parser = argparse.ArgumentParser()
parser.add_argument('--exe', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
report = args.report.resolve()
work = report.with_suffix('')
work.mkdir(parents=True, exist_ok=False)
source = work / 'source.png'
Image.new('RGB', (320, 240), '#5bd4bf').save(source)
before = hashlib.sha256(source.read_bytes()).hexdigest()
sys.executable = str(args.exe.resolve(strict=True))
sys.frozen = True
store = Store(work / 'history')
cases = []
def run(task, inputs, **options):
    output = work / task
    output.mkdir()
    plan = plan_documents([FileRecord.from_path(p) for p in inputs], PlanRequest('documents',
        document_task=task, destination=str(output), extension=options.pop('extension', 'pdf'), **options))
    job = store.enqueue(task, 'documents', document_snapshot(plan))
    execute_job(store, job)
    rows = store.items(job)
    assert all(row['state'] == 'succeeded' for row in rows), [row['message'] for row in rows]
    cases.append(task)
    return [Path(item.output) for item in plan.items]
try:
    pdf = run('images_pdf', [source])[0]
    secret = remember('Synthetic PDF only - Unicode 日本')
    locked = run('protect', [pdf], document_protection='set', document_output_secret=secret)[0]
    reader = PdfReader(locked)
    assert reader.is_encrypted and reader.decrypt('Synthetic PDF only - Unicode 日本') and len(reader.pages) == 1
    rendered = run('render', [locked], extension='png', document_input_secret=secret)[0]
    with Image.open(rendered) as image:
        image.load()
        assert image.width > 0 and image.height > 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
finally:
    store.close()
report.write_text(json.dumps(dict(passed=True, packaged_workers=True, cases=cases), indent=2))
print(report.read_text())
