#!/usr/bin/env python3
"""Assemble a clean public-export candidate tree from this private repo.

Whitelist-based: only explicitly listed paths are copied. The export never
contains the corpus, review queues, media_qa artifacts, personal Drive links
or the private git history. After copying, the whole tree is scanned for
forbidden content and every exported file is hashed into EXPORT_MANIFEST.json.
"""
import argparse, hashlib, json, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scan_public_output import scan_tree  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# Whitelist: every path below is individually approved for export.
# docs/PUBLIC_EXPORT_PLAN.md stays private: it names the operator account and
# describes the private repo's history — an internal runbook, not public doc.
WHITELIST_FILES = [
    'requirements.txt', '.gitignore', 'AGENTS.md',
    'export/README.md', 'export/LICENSE', 'export/PUBLIC_STATUS.md',
]
WHITELIST_DIRS = ['tools', 'converter', 'tests', '.github/workflows']
# Inside whitelisted dirs, skip caches and the publish workflow is kept (it is
# code and documents the gated process).
SKIP_NAMES = {'__pycache__'}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(2 ** 20), b''):
            h.update(chunk)
    return h.hexdigest()


def collect():
    files = []
    for rel in WHITELIST_FILES:
        src = REPO / rel
        if src.is_file():
            files.append((src, rel))
    for d in WHITELIST_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob('*')):
            if p.is_file() and not (set(p.parts) & SKIP_NAMES):
                files.append((p, p.relative_to(REPO).as_posix()))
    # public README becomes the exported repo's README.md
    out_files = [(s, 'README.md' if r == 'export/README.md' else r)
                 for s, r in files]
    out_files = [(s, 'LICENSE' if r == 'export/LICENSE' else r)
                 for s, r in out_files]
    return out_files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--report', type=Path, default=None)
    a = ap.parse_args()
    out = a.output
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    manifest = []
    for src, rel in collect():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        manifest.append({'path': rel, 'bytes': dest.stat().st_size,
                         'sha256': sha256(dest)})
    problems, _ = scan_tree(out, strict_review_refs=False)
    (out / 'EXPORT_INDEX.json').write_text(
        json.dumps({'files': manifest}, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    report = {'output': str(out), 'files': len(manifest),
              'forbidden_content': problems,
              'verdict': 'CLEAN' if not problems else 'FORBIDDEN-CONTENT'}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if a.report:
        a.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
