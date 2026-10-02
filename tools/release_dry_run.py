#!/usr/bin/env python3
"""Release dry-run: build the exact public file set, enumerate it, scan it.

Runs the release gate (report only), builds the site in 'candidate' mode,
hashes every output file and runs the forbidden-content scanner. Produces a
manifest + report; deploys nothing.
"""
import argparse, contextlib, hashlib, io, json, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from release_gate import check_release  # noqa: E402
from scan_public_output import scan_tree  # noqa: E402
import site_builder  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(2 ** 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, required=True)
    ap.add_argument('--approval', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True, help='staging dir')
    ap.add_argument('--report', type=Path, default=None)
    a = ap.parse_args()
    ok, reasons, notes = check_release(a.corpus, a.approval)
    # Staging dir must not contain leftovers from earlier runs.
    if a.output.exists():
        shutil.rmtree(a.output)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        site_builder.build(a.corpus, a.output, 'candidate',
                           release=a.approval)
    build_info = json.loads(buf.getvalue().strip().splitlines()[-1])
    files = sorted(p for p in a.output.rglob('*') if p.is_file())
    manifest = [{'path': p.relative_to(a.output).as_posix(),
                 'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in files]
    problems, _ = scan_tree(a.output)
    report = {
        'kind': 'release_dry_run',
        'gate_approved': ok,
        'gate_blocking_reasons': reasons,
        'gate_notes': notes,
        'output_dir': str(a.output),
        'file_count': len(manifest),
        'build_info': build_info,
        'files': manifest,
        'forbidden_content': problems,
        'verdict': 'READY-TO-REVIEW' if ok and not problems else
                   ('BLOCKED-BY-GATE' if not ok else 'FORBIDDEN-CONTENT'),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.report:
        a.report.write_text(text + '\n', encoding='utf-8')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
