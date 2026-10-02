#!/usr/bin/env python3
"""Detect structural Markdown problems in a converted corpus volume set.

Reports (does NOT fix — author text is never edited automatically):
- image markup glued to surrounding text on the same line (renders inline,
  can split a word — e.g. ``текст![...](...)текст``);
- image/link markup whose local target file is missing.

Usage: python tools/check_markdown_media.py --corpus /path/to/kob_books_v0_4
Exit code 0 always; findings are informational for reviewers.
"""
import argparse, json, re
from pathlib import Path

RE_IMG = re.compile(r'!\[[^\]]*\]\(([^)\s]+)\)')


def scan_file(path: Path):
    findings = []
    text = path.read_text(encoding='utf-8', errors='replace')
    for m in RE_IMG.finditer(text):
        target = m.group(1)
        s, e = m.span()
        before = text[s - 1] if s else '\n'
        after = text[e] if e < len(text) else '\n'
        if before != '\n' or after != '\n':
            findings.append({'file': str(path), 'kind': 'inline_glued_image',
                             'target': target})
        if not re.match(r'^(?:https?://|data:)', target):
            if not (path.parent / target.split('#')[0]).exists():
                findings.append({'file': str(path), 'kind': 'missing_target',
                                 'target': target})
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, required=True)
    root = ap.parse_args().corpus
    all_findings = []
    for p in sorted(root.rglob('*.md')):
        all_findings += scan_file(p)
    by_kind = {}
    for f in all_findings:
        by_kind.setdefault(f['kind'], []).append(f)
    print(json.dumps({
        'files_scanned': sum(1 for _ in root.rglob('*.md')),
        'findings': len(all_findings),
        'by_kind': {k: len(v) for k, v in sorted(by_kind.items())},
        'details': all_findings[:200],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
