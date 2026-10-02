#!/usr/bin/env python3
"""Issue #11 private pipeline stage: convert downloaded dotu.ru translation
sources to Markdown without any editorial change.

Reads the raw translation ledger produced by the dotu.ru multilingual crawl
(default ../_audit_tmp/dotu_tr_match.jsonl), converts each downloaded source
(fb2/doc/docx/odt/epub via LibreOffice+pandoc, or the fb2 XML reader) into
`<build>/<lang>/<translation_id>/NN.md` section files, and writes an enriched
ledger with per-entry conversion status.

NO translation, no stylistic edit, no OCR. PDFs are accepted only if the
text layer extracts; otherwise the entry is marked ``text_unavailable`` and
will be exported as a metadata-only card.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, unicodedata
from pathlib import Path

SOFFICE = os.environ.get('SOFFICE', r'C:\Program Files\LibreOffice\program\soffice.exe')
try:
    import pypandoc
except ImportError:
    pypandoc = None

FB2_NS = 'http://www.gribuser.ru/xml/fictionbook/2.0'


def fb2_to_md(path: Path) -> str:
    import xml.etree.ElementTree as ET
    data = path.read_bytes()
    enc = 'utf-8'
    m = re.match(br'<\?xml[^>]*encoding="([^"]+)"', data)
    if m:
        enc = m.group(1).decode('ascii', 'replace')
    try:
        raw = data.decode(enc, errors='strict')
    except (UnicodeDecodeError, LookupError):
        raw = data.decode('utf-8', errors='replace')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # recover: drop the XML prolog, escape bare '&', strip illegal chars
        body = re.sub(r'^<\?[^?]*\?>', '', raw)
        body = re.sub(r'&(?!(amp|lt|gt|quot|apos|#)\w*;)', '&amp;', body)
        body = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', body)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            # last resort: treat as flat tag soup, keep paragraph text
            body = re.sub(r'<section[^>]*>', '\n\n## ', body)
            body = re.sub(r'</section[^>]*>', '\n', body)
            body = re.sub(r'<(title|subtitle)[^>]*>', '\n\n## ', body)
            body = re.sub(r'</(title|subtitle)>', '\n\n', body)
            body = re.sub(r'<(p|v)[^>]*>', '\n', body)
            body = re.sub(r'<(empty-line/?)>', '\n', body)
            body = re.sub(r'<[^>]+>', '', body)
            import html as h
            return h.unescape(body)
    ns = {'f': FB2_NS}
    out = []

    def emit(sec, depth):
        title = sec.find('f:title', ns)
        if title is not None:
            tt = ' '.join(''.join(title.itertext()).split())
            if tt:
                out.append(f'\n{"#" * min(depth + 1, 6)} {tt}\n')
        for p in sec.findall('f:p', ns):
            out.append(' '.join(''.join(p.itertext()).split()) + '\n')
        for sub in sec.findall('f:section', ns):
            emit(sub, depth + 1)

    bodies = root.findall('f:body', ns)
    if bodies:
        for sec in bodies[0].iter(f'{{{FB2_NS}}}section'):
            emit(sec, 1)
        for b in bodies[1:]:
            if b.get('name') == 'notes':
                out.append('\n## Notes\n')
                for sec in b.findall('f:section', ns):
                    txt = ' '.join(' '.join(''.join(p.itertext()).split())
                                   for p in sec.findall('f:p', ns))
                    if txt:
                        out.append(txt + '\n')
    return '\n'.join(out)


def html_to_md(html: str) -> str:
    """Tiny <article> extractor for pages without downloadable files."""
    m = re.search(r'<article[^>]*>(.*?)</article>', html, re.S)
    art = m.group(1) if m else html
    art = re.sub(r'<(script|style|aside|nav|footer|header)[^>]*>.*?</\1>', '',
                 art, flags=re.S)
    art = re.sub(r'<h([1-4])[^>]*>', lambda m: f'\n{"#" * int(m.group(1))} ', art)
    art = re.sub(r'<br[^>]*>', '\n', art)
    art = re.sub(r'</(p|div|li|ul|ol|blockquote|table|tr)>', '\n\n', art)
    art = re.sub(r'<li[^>]*>', '- ', art)
    art = re.sub(r'<[^>]+>', '', art)
    import html as h
    art = h.unescape(art)
    return re.sub(r'\n{3,}', '\n\n', art).strip() + '\n'


def pdf_to_md(src: Path) -> str:
    """Accept PDF only when the embedded text layer extracts; no OCR."""
    p = subprocess.run(['pdftotext', '-enc', 'UTF-8', '-layout',
                        str(src), '-'],
                       capture_output=True, timeout=300)
    if p.returncode:
        raise RuntimeError('pdftotext failed: '
                           + p.stderr.decode('utf-8', 'replace')[:200])
    text = p.stdout.decode('utf-8', 'replace')
    text = re.sub(r'\f+', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(re.sub(r'\W', '', text)) < 500:
        raise RuntimeError('pdf text layer too thin / missing')
    return text + '\n'


def convert_source(src: Path, fmt: str, tmpdir: Path) -> str:
    if fmt == 'fb2':
        return fb2_to_md(src)
    if fmt == 'pdf':
        return pdf_to_md(src)
    if fmt == 'html':
        # crawler already extracted the <article> body as text/markdown
        return src.read_text(encoding='utf-8', errors='replace')
    if fmt in ('odt', 'epub'):
        if pypandoc:
            return pypandoc.convert_file(str(src), 'gfm',
                                         extra_args=['--wrap=none'])
    # doc/docx/(odt|epub without pypandoc path) -> docx -> gfm
    docx = src
    if fmt != 'docx':
        subprocess.run([SOFFICE, '--headless', '--convert-to', 'docx',
                        '--outdir', str(tmpdir), str(src)],
                       capture_output=True, timeout=600)
        cand = tmpdir / (src.stem + '.docx')
        if not cand.exists():
            raise RuntimeError(f'libreoffice failed for {src.name}')
        docx = cand
    if pypandoc:
        return pypandoc.convert_file(str(docx), 'gfm',
                                     extra_args=['--wrap=none'])
    p = subprocess.run(['pandoc', '-f', 'docx', '-t', 'gfm', '--wrap=none',
                        str(docx)], capture_output=True, timeout=300)
    if p.returncode:
        raise RuntimeError('pandoc failed: ' + p.stderr.decode('utf-8', 'replace')[:200])
    return p.stdout.decode('utf-8', 'replace')


def split_sections(md: str) -> list[tuple[str, str]]:
    """Split md on level-1/2 headings -> [(title, body)]."""
    parts = re.split(r'(?m)^(#{1,2})\s+(.*?)\s*$', md)
    secs = []
    head = parts[0].strip()
    if head:
        secs.append(('text', head))
    i = 1
    while i + 2 <= len(parts):
        title = parts[i + 1].strip()
        body = parts[i + 2].strip()
        secs.append((title, body))
        i += 3
    return secs or [('text', md.strip())]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ledger', required=True,
                    help='dotu_tr_match.jsonl from the private crawl')
    ap.add_argument('--files-dir', required=True,
                    help='directory with downloaded translation sources')
    ap.add_argument('--build', required=True,
                    help='output dir: <build>/<lang>/<translation_id>/NN.md')
    ap.add_argument('--out-ledger', required=True,
                    help='enriched ledger jsonl for the export stage')
    a = ap.parse_args()

    build = Path(a.build)
    build.mkdir(parents=True, exist_ok=True)
    tmpdir = build / '_docx_tmp'
    tmpdir.mkdir(exist_ok=True)

    rows = [json.loads(l) for l in Path(a.ledger)
            .read_text(encoding='utf-8').splitlines() if l.strip()]
    used_ids = {}
    stats = {'converted': 0, 'text_unavailable': 0, 'no_source': 0, 'err': 0}
    for r in rows:
        slug = re.sub(r'[^\w.-]+', '_',
                      unicodedata.normalize('NFKC',
                          r['url'].rstrip('/').rsplit('/', 1)[-1]))[:60]
        slug = slug.lower() or 'item'
        base = slug
        n = used_ids.get(base, 0)
        used_ids[base] = n + 1
        tid = base if n == 0 else f'{base}-{n + 1}'
        r['translation_id'] = f"{r['lang']}-{tid}" if not tid.startswith(
            r['lang'] + '_') else tid.replace('_', '-')
        r['translation_id'] = re.sub(r'[^\w-]+', '-', r['translation_id'])

        tdir = build / r['lang'] / r['translation_id']
        src = Path(a.files_dir) / Path(r['file']).name if r.get('file') else None
        if not src or not src.exists():
            r['full_text_status'] = ('download_failed'
                                     if r.get('dl_error') else 'no_source_file')
            r['translation_status'] = 'unknown'
            stats['no_source'] += 1
            continue
        try:
            md = convert_source(src, r['fmt'], tmpdir)
        except Exception as e:
            r['full_text_status'] = 'text_unavailable'
            r['conversion_error'] = str(e)[:200]
            stats['text_unavailable'] += 1
            continue
        md = md.replace('\xad', '')
        if len(re.sub(r'\W', '', md)) < 200:
            r['full_text_status'] = 'text_unavailable'
            r['conversion_error'] = 'extracted text too short'
            stats['text_unavailable'] += 1
            continue
        tdir.mkdir(parents=True, exist_ok=True)
        secs = split_sections(md)
        order = []
        for i, (title, body) in enumerate(secs, 1):
            fn = f'{i:02d}.md'
            (tdir / fn).write_text(f'## {title}\n\n{body}\n' if title != 'text'
                                  else body + '\n', encoding='utf-8')
            order.append({'n': i, 'file': fn, 'title': title[:200]})
        r['full_text_status'] = 'full'
        r['sections_count'] = len(order)
        r['reading_order'] = order
        r['md_sha256'] = hashlib.sha256(
            ''.join((tdir / f'{i:02d}.md').read_text(encoding='utf-8')
                    for i in range(1, len(order) + 1))
            .encode('utf-8')).hexdigest()
        stats['converted'] += 1

    Path(a.out_ledger).write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n',
        encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
