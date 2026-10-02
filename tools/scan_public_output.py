#!/usr/bin/env python3
"""Scan a built public site tree for content that must never ship.

Fails (exit 1) if the output contains personal Drive links, workspace paths,
e-mail addresses, OLE binaries, raw extraction dumps, undeclared PDF files,
internal review/QA artifacts, or any operator-supplied --forbid strings.

Beyond raw bytes and printable-string extraction, the scanner decodes the
metadata carriers where identity data realistically hides: PNG tEXt/zTXt/iTXt
chunks, FlateDecode streams inside PDFs (XMP/Info), UTF-16 text files and
base64 blobs inside text files.

Limit: a 'clean' verdict is a negative control, not proof that no personal
data exists — steganographic content and unknown container formats are out
of scope; the final release manifest requires human review.
"""
import argparse, base64, json, re, struct, sys, zlib
from pathlib import Path

FORBIDDEN_BYTES = [
    (re.compile(rb'drive\.google', re.I), 'personal Drive link'),
    (re.compile(rb'google' rb'drive|docs\.google\.com/drive', re.I), 'personal Drive link'),
    (re.compile(rb'[\w.+-]{2,}@(?!users\.noreply\.github\.com)[\w-]{2,}\.[\w.]{2,}', re.I), 'e-mail address'),
    (re.compile(rb'[A-Za-z]:\\Users\\'), 'personal path/username'),
]
# Additionally strict for rendered site output: review-queue internals and
# internal workspace paths must not appear in published HTML (code and docs
# may legitimately mention them, e.g. portability notes).
SITE_ONLY_BYTES = [
    (re.compile(rb'media_review_queue|ole_review_queue|release_approval'), 'internal review data'),
    (re.compile(rb'/mnt/data|kob_pilot_t1|kob_converter_workspace'), 'internal workspace path'),
    (re.compile(rb'javascript\s*:', re.I), 'javascript: URL in output'),
    (re.compile(rb'<script(?![^>]*assets/(reader|search)\.js)', re.I), 'inline/foreign script tag'),
    # bare opaque file-sharing ids (e.g. drive links without the domain)
    (re.compile(rb'[?&]id=[\w-]{20,}'), 'opaque external file id'),
]

ALLOWED_PDF = {f'osnovy-sociologii-tom-{v}.pdf' for v in range(1, 7)}
FORBIDDEN_NAMES = re.compile(r'(raw_extracted|_qa|manifest\.json|qa\.json|OLE|\.bin$|\.docx?$|media_qa)', re.I)
TEXT_EXT = {'.html', '.htm', '.css', '.js', '.json', '.jsonl', '.md', '.txt',
            '.svg', '.xml', '.yml', '.yaml', '.py', '.csv'}
PRINTABLE_RUN = re.compile(rb'[\x20-\x7e]{6,}')
BASE64_RUN = re.compile(rb'[A-Za-z0-9+/]{40,}={0,2}')
MAX_TEXT_SCAN = 5 * 1024 * 1024
MAX_STREAM_SCAN = 2 * 1024 * 1024

# Raw binary regions (compressed image/PDF data) produce camel/digit noise
# looking like short e-mails; require lowercase domains there. Decoded
# metadata variants below use the full case-insensitive patterns.
BINARY_PATTERNS = [
    (re.compile(rb'[\w.+-]{2,}@[a-z0-9-]{2,}\.[a-z]{2,}'), 'e-mail address'),
] + [p for p in FORBIDDEN_BYTES if p[1] != 'e-mail address']


def _check(data: bytes, rel: str, patterns, problems, via=''):
    for rx, label in patterns:
        if rx.search(data):
            problems.append(f'{label} found in {rel}{via}')


def png_text_chunks(data: bytes):
    """Yield decoded PNG textual metadata (tEXt/zTXt/iTXt)."""
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return
    pos, out = 8, []
    while pos + 12 <= len(data):
        length, ctype = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        try:
            if ctype == b'tEXt':
                out.append(body)
            elif ctype == b'zTXt' and b'\x00' in body:
                _, rest = body.split(b'\x00', 1)
                if rest and rest[0] == 0:
                    out.append(zlib.decompress(rest[1:]))
            elif ctype == b'iTXt' and b'\x00' in body:
                _, rest = body.split(b'\x00', 1)
                flag = rest[0] if rest else 0
                parts = rest[2:].split(b'\x00', 2)  # method, langtag, translated
                out.append(zlib.decompress(parts[-1]) if flag == 1 else parts[-1])
        except (zlib.error, ValueError, IndexError):
            pass
        pos += 12 + length
        if ctype == b'IEND':
            break
    return out


def pdf_flate_streams(data: bytes):
    """Yield zlib-decompressed('FlateDecode') stream contents of a PDF."""
    out = []
    for m in re.finditer(rb'stream\r?\n', data):
        head = data[max(0, m.start() - 300):m.start()]
        if b'FlateDecode' not in head:
            continue
        start = m.end()
        end = data.find(b'endstream', start)
        if end < 0:
            continue
        raw = data[start:end].rstrip(b'\r\n')
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if len(dec) > MAX_STREAM_SCAN:
            dec = dec[:MAX_STREAM_SCAN]
        out.append(dec)
    return out


def decoded_variants(data: bytes, suffix: str):
    """Extra byte-strings extracted from a file to full-check."""
    variants = []
    if suffix == '.png':
        variants += [(t, ' (PNG metadata)') for t in png_text_chunks(data)]
    elif suffix == '.pdf':
        variants += [(t, ' (PDF embedded stream)') for t in pdf_flate_streams(data)]
    elif suffix in ('.jpg', '.jpeg'):
        # JPEG comment markers may carry text directly
        for m in re.finditer(rb'\xff\xfe', data):
            end = data.find(b'\xff', m.end() + 2)
            if end > m.end():
                variants.append((data[m.end() + 2:end], ' (JPEG comment)'))
    if suffix in TEXT_EXT:
        # UTF-16 text (BOM or dense NUL interleave)
        if data[:2] in (b'\xff\xfe', b'\xfe\xff') or (len(data) > 16 and data[1::2].count(0) > len(data) // 4):
            for enc in ('utf-16', 'utf-16-le'):
                try:
                    variants.append((data.decode(enc, errors='ignore').encode(), f' ({enc})'))
                    break
                except UnicodeError:
                    pass
        # base64 blobs inside text
        for run in BASE64_RUN.findall(data):
            try:
                dec = base64.b64decode(run, validate=True)
            except (base64.binascii.Error, ValueError):
                continue
            if dec.count(b'\x00') == 0 and PRINTABLE_RUN.search(dec):
                variants.append((dec, ' (base64-decoded)'))
    return variants


def scan_tree(root: Path, strict_review_refs: bool = True, forbid=()):
    problems = []
    patterns = FORBIDDEN_BYTES + (SITE_ONLY_BYTES if strict_review_refs else [])
    bin_patterns = BINARY_PATTERNS + (SITE_ONLY_BYTES if strict_review_refs else [])
    extra = [(re.compile(re.escape(x.lower().encode()), re.I), f'forbidden string {x!r}')
             for x in forbid]
    files = [p for p in root.rglob('*') if p.is_file()]
    for p in files:
        rel = p.relative_to(root).as_posix()
        suffix = p.suffix.lower()
        if suffix == '.pdf' and p.name not in ALLOWED_PDF:
            problems.append(f'undeclared PDF in output: {rel}')
        if FORBIDDEN_NAMES.search(p.name):
            problems.append(f'internal file name in output: {rel}')
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if suffix in TEXT_EXT:
            _check(data[:MAX_TEXT_SCAN], rel, patterns + extra, problems)
        else:
            for run in PRINTABLE_RUN.findall(data):
                _check(run, rel, bin_patterns + extra, problems)
        for variant, via in decoded_variants(data, suffix):
            _check(variant, rel, FORBIDDEN_BYTES + extra, problems, via=via)
    return problems, files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', type=Path, required=True)
    ap.add_argument('--forbid', action='append', default=[],
                    help='literal operator-identity string that must not appear anywhere')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    if not a.site.is_dir():
        print(f'not a directory: {a.site}', file=sys.stderr)
        return 2
    problems, files = scan_tree(a.site, forbid=a.forbid)
    report = {'site': str(a.site), 'files_scanned': len(files),
              'problems': problems, 'clean': not problems}
    print(json.dumps(report, ensure_ascii=False, indent=2) if a.json else
          '\n'.join([f'{len(files)} files scanned'] +
                    [f'FORBIDDEN: {p}' for p in problems] +
                    ([] if problems else ['No forbidden content found'])))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
