#!/usr/bin/env python3
"""Assert that a public-mode site build is refused by the release gate.

A passing result requires ALL of the following:
- site_builder exits non-zero;
- its output contains the gate refusal marker ``PUBLIC RELEASE BLOCKED``;
- every ``--expect`` substring appears in the refusal reasons;
- the requested output directory was NOT created (fail before emit).

Exit 0 = the block held; exit 1 = the check itself failed.
"""
import argparse, json, subprocess, sys
from pathlib import Path

BLOCK_MARKER = 'PUBLIC RELEASE BLOCKED'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--release-approval', type=Path, required=True)
    ap.add_argument('--expect', action='append', default=[],
                    help='Substring that must appear in the refusal output; repeatable')
    a = ap.parse_args()
    tools = Path(__file__).resolve().parent
    p = subprocess.run(
        [sys.executable, str(tools / 'site_builder.py'),
         '--corpus', str(a.corpus), '--output', str(a.output),
         '--mode', 'public', '--release-approval', str(a.release_approval)],
        capture_output=True, text=True)
    combined = (p.stdout or '') + (p.stderr or '')
    problems = []
    if p.returncode == 0:
        problems.append('public-mode build exited 0 (gate did not refuse)')
    if BLOCK_MARKER not in combined:
        problems.append(f'gate refusal marker {BLOCK_MARKER!r} absent from output')
    for needle in a.expect:
        if needle not in combined:
            problems.append(f'expected blocking reason missing: {needle!r}')
    if a.output.exists():
        problems.append(f'public output directory was created: {a.output}')
    print(json.dumps({'gate_refused': not problems, 'exit_code': p.returncode,
                      'problems': problems,
                      'refusal_head': combined[:600]}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not problems else 1)


if __name__ == '__main__':
    main()
