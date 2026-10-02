#!/usr/bin/env python3
"""Issue #13 media registry builder (private pipeline step).

Scans the private corpus for every graphical reference, resolves each to a
real source object (DOC/DOCX-embedded media, extracted previews, OLE
containers, PDF-crop originals), deduplicates by content hash, classifies
media type / criticality / rights, and stages publishable derivatives.

Outputs:
  --registry-out   private JSONL registry (all fields incl. local paths)
  --assets-out     staged public asset tree  <work_dir>/<figure_id>.<ext>
  --report-out     counters used by the candidate report

Nothing here reconstructs graphics: assets are verbatim extracts or faithful
format conversions (wmf/emf -> png via LibreOffice) with both hashes kept.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, struct, subprocess, sys, tempfile
import zipfile
from pathlib import Path, PurePosixPath

IMG_MD_RX = re.compile(r'!\[([^\]]*)\]\(([^)]*)\)')
IMG_TAG_RX = re.compile(r'<img\b[^>]*>', re.I)
IMG_SRC_RX = re.compile(r'src=["\']([^"\']+)["\']', re.I)
LABEL_RX = re.compile(
    r'(Рис(?:унок)?\.?\s*[\dIVXLCivxlc]*[.\-\d]*|Схема\s*\d[\d.]*|'
    r'Диаграмма\s*\d[\d.]*|Таблица\s*\d[\d.]*|Илл\.?\s*\d[\d.]*|'
    r'График\s*\d[\d.]*|Карта\s*[\dA-Za-z]*)', re.I)
CAPTION_RX = re.compile(
    r'(рис(?:унок|\.)\s*[\divxlcIVXLC.\-]*|схема|диаграмм|таблиц|иллюстрац|'
    r'график|картин|репродукц|фотограф|фото\b|карта\b|карты\b|обложк|плакат|'
    r'кадр\b|скриншот|икон)', re.I)
THIRD_PARTY_RX = re.compile(
    r'(репродукц|картин|фотограф|фото\b|кадр|плакат|скриншот|снимок\s+экрана|'
    r'сайт|http|www\.|карта\s+\w+ск|карты\b|обложк|икон[аыо]?[,\.\s)])',
    re.I)

VECTOR = {'.wmf', '.emf'}
RASTER = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff'}
SOFFICE = (os.environ.get('SOFFICE')
           or r'C:\Program Files\LibreOffice\program\soffice.exe')


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def image_size(data: bytes, ext: str):
    """(w, h) for png/jpeg/gif/bmp; None for vector/unknown."""
    try:
        if ext == '.png' and data[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack('>II', data[16:24])
            return w, h
        if ext == '.gif' and data[:6] in (b'GIF87a', b'GIF89a'):
            w, h = struct.unpack('<HH', data[6:10])
            return w, h
        if ext == '.bmp' and data[:2] == b'BM':
            w, h = struct.unpack('<ii', data[18:26])
            return abs(w), abs(h)
        if ext in ('.jpg', '.jpeg') and data[:2] == b'\xff\xd8':
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                ln = struct.unpack('>H', data[i + 2:i + 4])[0]
                i += 2 + ln
    except (struct.error, IndexError):
        pass
    return None


def media_type_of(caption: str, ext: str, label: str | None) -> str:
    t = (caption or '').lower()
    for kw, mt in (('схем', 'scheme'), ('диаграм', 'diagram'),
                   ('график', 'chart'), ('таблиц', 'table_image'),
                   ('карта', 'map'), ('карты', 'map'), ('фото', 'photo'),
                   ('фотограф', 'photo'), ('обложк', 'cover'),
                   ('репродукц', 'reproduction'), ('картин', 'reproduction'),
                   ('плакат', 'reproduction'), ('скан', 'scan_fragment')):
        if kw in t:
            return mt
    if ext in VECTOR:
        return 'scheme'
    if label:
        return 'other'
    return 'other'


def rights_of(media_type: str, caption: str, context: str, ext: str,
              label: str | None = None) -> str:
    ctx = (caption or '') + ' ' + (context or '')
    if THIRD_PARTY_RX.search(ctx) or media_type in (
            'photo', 'reproduction', 'map', 'cover'):
        return 'rights_review_required'
    if ext in VECTOR or media_type in ('scheme', 'diagram', 'chart',
                                       'table_image'):
        return 'authorial'
    # a numbered authorial figure («Рис. N» / «Схема N») with no third-party
    # signal is part of the author's own apparatus of illustrations
    if label and LABEL_RX.search(label):
        return 'authorial'
    return 'unknown'


def criticality_of(media_type: str, ext: str, label: str | None,
                   size_bytes: int, dims) -> str:
    tiny = (size_bytes < 2048) or (dims and max(dims) <= 64)
    if tiny or media_type == 'decorative':
        return 'decorative'
    if ext in VECTOR or media_type in ('scheme', 'diagram', 'chart'):
        return 'critical_for_understanding'
    if label or media_type in ('map', 'reproduction', 'photo'):
        return 'useful'
    return 'unclear'


# VML/DrawingML positioning junk that survives DOCX->text conversion
# ('left36195Рис…', '3654425-1789430Рис…', 'lefttopМемориальная…'): it
# clusters at paragraph start as latin-letters/digits/dashes glued to the
# first real (Cyrillic/quote) caption character
_VML_JUNK = re.compile(
    r'^\s*(?:[a-z]+-?\d*|\d+|-)+\s*[.…:]*\s*(?=[А-Яа-яЁё*«"(…])')


def caption_and_label(lines, idx):
    """Scan +-4 lines around a ref site for a figure label/caption."""
    win = []
    for j in range(max(0, idx - 4), min(len(lines), idx + 5)):
        if 'В оригинальном издании здесь расположена иллюстрац' in \
                lines[j] or 'figs:' in lines[j]:
            continue  # inserted omission markers / media tags are not captions
        t = re.sub(r'<[^>]+>', ' ', lines[j]).strip()
        t = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', t).strip()
        t = _VML_JUNK.sub(' ', t).strip()
        if t:
            win.append((abs(j - idx), j, t))
    win.sort()
    for _, j, t in win:
        if t.startswith('*') or LABEL_RX.search(t) or CAPTION_RX.search(t):
            if len(t) > 400:
                t = t[:400] + '…'
            lab = LABEL_RX.search(t)
            cap = t.strip('*').strip() if CAPTION_RX.search(t) else None
            if lab or cap:
                return (cap or (lab.group(0) if lab else None),
                        lab.group(0) if lab else None)
    return None, None


def fig_id(work: str, name: str, unbound=False) -> str:
    stem = re.sub(r'\.[A-Za-z0-9]+$', '', name)
    stem = re.sub(r'[^0-9A-Za-zА-Яа-яёЁ_\-]+', '_', stem).strip('_') or 'obj'
    tag = 'unbound-' if unbound else ''
    return f'{work}--{tag}{stem}'


class Builder:
    def __init__(self, a):
        self.a = a
        self.figs = []          # registry records
        self.staged = {}        # figure_id -> staged asset path
        self.seen_sha = {}      # object sha256 -> canonical figure_id
        self.to_convert = []    # (src_path, ext, figure_id)
        self.errors = []

    # ---------- asset staging ----------
    def stage(self, fig: dict, src_bytes: bytes | None,
              src_path: Path | None, orig_ext: str):
        """Decide derivative + stage asset; updates fig record."""
        if src_bytes is None and src_path is None:
            fig['publication_status'] = 'metadata_only_source'
            fig['notes'] = (fig.get('notes') or '') + \
                ' Объект отсутствует на диске — источник не восстановлен.'
            return
        data = src_bytes if src_bytes is not None else src_path.read_bytes()
        fig['source_sha256'] = sha256_bytes(data)
        fig['media_bytes'] = len(data)
        # global dedup by object bytes: canonical record keeps the asset;
        # later occurrences render via its public_path and are flagged
        canon = self.seen_sha.get(fig['source_sha256'])
        if canon:
            fig['publication_status'] = 'duplicate'
            fig['duplicate_of'] = canon['figure_id']
            fig['public_path'] = canon.get('public_path')
            if canon.get('publication_status') not in ('published',
                                                       'duplicate'):
                # canonical object itself unpublished -> same reason blocks us
                fig['duplicate_blocked_by'] = canon.get('publication_status')
            return
        self.seen_sha[fig['source_sha256']] = fig
        if fig['rights_status'] in ('rights_review_required', 'unknown'):
            fig['publication_status'] = 'metadata_only_rights'
            fig['notes'] = (fig.get('notes') or '') + \
                ' Публикация до правовой проверки заблокирована.'
            return
        if fig['criticality'] == 'decorative' and not fig['referenced_in_text']:
            fig['publication_status'] = 'decorative_excluded'
            return
        ext = orig_ext.lower()
        if ext in RASTER:
            self._write(fig, data, ext, 'verbatim_copy')
        elif ext in VECTOR:
            cdir = self.a.tmpdir / 'conv'
            cdir.mkdir(parents=True, exist_ok=True)
            src = cdir / (fig['figure_id'] + ext)
            src.write_bytes(data)
            self.to_convert.append((src, fig['figure_id'], fig))
            fig['_pending'] = True
        else:
            fig['publication_status'] = 'technical_failure'
            fig['notes'] = (fig.get('notes') or '') + \
                f' Неподдерживаемый формат {ext}.'

    def _write(self, fig: dict, data: bytes, ext: str, op: str):
        dest = (self.a.assets_out / fig['work_dir'] /
                f'{fig["figure_id"]}{ext}')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        fig['public_asset_sha256'] = sha256_bytes(data)
        fig['public_path'] = f'assets/figures/{fig["work_dir"]}/{fig["figure_id"]}{ext}'
        fig['publication_status'] = 'published'
        fig['conversion'] = op
        self.staged[fig['figure_id']] = dest

    def run_conversions(self):
        """Batch-convert pending wmf/emf -> png via LibreOffice."""
        cdir = self.a.tmpdir / 'conv'
        profile = (self.a.tmpdir.resolve() / 'lo_profile').as_uri()
        todo = self.to_convert
        while todo:
            batch, todo = todo[:40], todo[40:]
            cmd = [SOFFICE, '--headless',
                   f'-env:UserInstallation={profile}',
                   '--convert-to', 'png', '--outdir', str(cdir)]
            cmd += [str(p) for p, _, _ in batch]
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            for src, fid, fig in batch:
                out = src.with_suffix('.png')
                if out.is_file() and out.stat().st_size > 0:
                    self._write(fig, out.read_bytes(), '.png',
                                f'libreoffice {src.suffix[1:]}->png '
                                '(faithful rasterization)')
                else:
                    fig['publication_status'] = 'technical_failure'
                    fig['notes'] = (fig.get('notes') or '') + \
                        f' Конвертация {src.suffix}->png не удалась: ' \
                        f'{(r.stderr or r.stdout)[:200]!r}'

    # ---------- figures ----------
    def add_fig(self, **kw):
        kw.setdefault('source_kind', None)
        kw.setdefault('source_file_or_url', None)
        kw.setdefault('source_sha256', None)
        kw.setdefault('public_asset_sha256', None)
        kw.setdefault('source_page', None)
        kw.setdefault('source_bbox', None)
        kw.setdefault('original_filename', None)
        kw.setdefault('caption', None)
        kw.setdefault('figure_label', None)
        kw.setdefault('media_type', 'other')
        kw.setdefault('referenced_in_text', True)
        kw.setdefault('criticality', 'unclear')
        kw.setdefault('rights_status', 'unknown')
        kw.setdefault('publication_status', 'metadata_only_source')
        kw.setdefault('public_path', None)
        kw.setdefault('media_file', None)
        kw.setdefault('provenance_class', None)
        kw.setdefault('provenance_evidence', [])
        kw.setdefault('anchor_confidence', None)
        kw.setdefault('anchor_evidence', None)
        kw.setdefault('context_excerpt', None)
        kw.setdefault('docx_context', None)
        kw.setdefault('inline_in_text', None)
        kw.setdefault('manual_decision', None)
        kw.setdefault('notes', None)
        self.figs.append(kw)
        return kw


def load_sections_index(extra: Path):
    idx = {}
    p = extra / 'data_sections.jsonl'
    for l in p.read_text(encoding='utf-8').splitlines():
        if l.strip():
            s = json.loads(l)
            idx[s['path']] = s.get('section_id')
    return idx


FB2_NS = '{http://www.gribuser.ru/xml/fictionbook/2.0}'
XLINK = '{http://www.w3.org/1999/xlink}href'


def _norm_text(t: str) -> str:
    t = re.sub(r'[#>*`_~]', '', t or '')
    return ' '.join(t.replace('\xad', '').split())


def fb2_items(fb2: Path):
    """(<image> occurrences in document order, binary id -> bytes)."""
    import base64
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(fb2.read_bytes().decode('utf-8',
                                                     errors='replace'))
    except ET.ParseError:
        return [], {}
    items = []
    # flat event stream in document order: section titles, paragraphs, images
    events = []

    def walk(el, sec_title):
        for ch in el:
            tag = ch.tag.replace(FB2_NS, '')
            if tag == 'section':
                t = ch.find(f'{FB2_NS}title')
                walk(ch, _norm_text(''.join(t.itertext()))
                     if t is not None else sec_title)
            elif tag == 'title':
                txt = _norm_text(''.join(ch.itertext()))
                events.append(('title', txt, sec_title))
                for im in ch.iter(f'{FB2_NS}image'):
                    events.append(('image', im.get(XLINK, '').lstrip('#'),
                                   sec_title))
            elif tag == 'image':
                events.append(('image',
                               ch.get(XLINK, '').lstrip('#'), sec_title))
            elif tag == 'p':
                events.append(('p', _norm_text(''.join(ch.itertext())),
                               sec_title))
                for im in ch.findall(f'{FB2_NS}image'):
                    events.append(('image-inline',
                                   im.get(XLINK, '').lstrip('#'), sec_title))
            else:
                walk(ch, sec_title)
    body = root.find(f'{FB2_NS}body')
    if body is not None:
        walk(body, None)
    # anchor each image to the nearest preceding text event; caption may sit
    # in the next paragraph
    prev_txt = ''
    for i, ev in enumerate(events):
        if not ev[0].startswith('image'):
            if ev[1]:
                prev_txt = ev[1]
            continue
        nxt = ''
        for j in range(i + 1, len(events)):
            if events[j][0] in ('p', 'title'):
                nxt = events[j][1]
                break
        items.append({'id': ev[1], 'sec': ev[2], 'anchor': prev_txt,
                      'next': nxt, 'order': i,
                      'inline': ev[0] == 'image-inline'})
    bins = {}
    for bn in root.findall(f'{FB2_NS}binary'):
        try:
            bins[bn.get('id')] = (bn.get('content-type', 'image/png'),
                                  base64.b64decode(bn.text or ''))
        except Exception:
            pass
    return items, bins


# --------------------------------------------------------------------------
# DOCX internal layout: media member -> position in document.xml paragraphs
# (used for anchor recovery and for source-level caption/context evidence)
W_P = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'
A_BLIP = '{http://schemas.openxmlformats.org/drawingml/2006/main}blip'
VML_IMG = '{urn:schemas-microsoft-com:vml}imagedata'
OLE_OBJ = '{urn:schemas-microsoft-com:office:office}OLEObject'
R_EMBED = '{http://schemas.openxmlformats.org/officeDocument/2006/' \
          'relationships}embed'
R_LINK = '{http://schemas.openxmlformats.org/officeDocument/2006/' \
         'relationships}link'
R_ID = '{http://schemas.openxmlformats.org/officeDocument/2006/' \
       'relationships}id'


def docx_image_positions(docx: Path):
    """{media member basename: [{part, para_idx, para_text, prev, next}]}."""
    import xml.etree.ElementTree as ET
    try:
        z = zipfile.ZipFile(docx)
    except zipfile.BadZipFile:
        return {}
    parts = []
    for n in z.namelist():
        if n == 'word/document.xml' or re.fullmatch(
                r'word/(header|footer|footnotes|endnotes)\d*\.xml', n):
            parts.append(n)
    out = {}
    for part in parts:
        base = part.rsplit('/', 1)[-1]
        rels_name = f'word/_rels/{base}.rels'
        rels = {}
        if rels_name in z.namelist():
            try:
                for rel in ET.fromstring(z.read(rels_name)):
                    tgt = rel.get('Target') or ''
                    if 'media/' in tgt:
                        rels[rel.get('Id')] = os.path.basename(tgt)
            except ET.ParseError:
                continue
        try:
            tx = ET.fromstring(z.read(part))
        except (ET.ParseError, KeyError):
            continue
        paras = []
        for p in tx.iter(W_P):
            txt = ''.join(p.itertext())
            rids = set()
            for bl in p.iter(A_BLIP):
                rid = bl.get(R_EMBED) or bl.get(R_LINK)
                if rid:
                    rids.add(rid)
            for im in p.iter(VML_IMG):
                rid = im.get(R_ID)
                if rid:
                    rids.add(rid)
            for ob in p.iter(OLE_OBJ):
                rid = ob.get(R_ID)
                if rid:
                    rids.add(rid)
            paras.append((txt, rids))
        def _near(idx, step):
            j = idx + step
            while 0 <= j < len(paras):
                if paras[j][0].strip():
                    return _norm_text(paras[j][0])
                j += step
            return ''
        for i, (txt, rids) in enumerate(paras):
            for rid in rids:
                name = rels.get(rid)
                if not name:
                    continue
                out.setdefault(name, []).append({
                    'part': base, 'para_idx': i,
                    'para_text': _norm_text(txt),
                    'prev': _near(i, -1), 'next': _near(i, 1)})
    return out


def work_md_index(files):
    """{normalized line text: [(rel_path, line_idx)]} for anchor binding."""
    md = {}
    for f in files:
        for i, ln in enumerate(f[1].splitlines()):
            t = _norm_text(ln)
            if t:
                md.setdefault(t, []).append((f[0], i))
    return md


_STYLE_JUNK_RX = re.compile(r'^([a-z]+\d+\s*)+')


def recover_anchor(pos_list, mdidx, md_bodies=None):
    """Try to bind a docx media position to a corpus md line.

    Returns (anchor_dict_or_None, confidence, evidence)."""
    if not pos_list:
        return None, 'unbound_confirmed', 'no docx position recorded'
    pos = pos_list[0]

    def clean(t):
        # LO-converted DOC paragraphs often carry VML style junk glued to
        # the text ('center635', 'left12' …) — strip leading ascii tokens
        t2 = _STYLE_JUNK_RX.sub('', t or '')
        t2 = re.sub(r'([a-z]+\d+)(?=[А-Яа-яЁё])', '', t2)
        return t2.strip()

    # candidate (text, mode, own-flag): the paragraph that carries the image,
    # then preceding paragraph (insert after), then following (insert before)
    cands = [(pos['para_text'], 'after', True),
             (pos['prev'], 'after', False),
             (pos['next'], 'before', False)]
    for txt, mode, own in cands:
        txt = clean(txt)
        if not txt or len(txt) < 25:
            continue
        hits = [(r, i, txt) for r, i in (mdidx.get(txt) or [])]
        if not hits and md_bodies:
            # substring fallback: the docx paragraph may merge several md
            # lines (or vice versa) — store the matched md line's own norm
            for rel, body in md_bodies:
                for i, ln in enumerate(body.splitlines()):
                    nl = _norm_text(ln)
                    if len(nl) >= 25 and (nl in txt or txt in nl):
                        hits.append((rel, i, nl))
                if len(hits) > 10:
                    break
            hits = hits[:10]
        if not hits:
            continue
        ev = ('docx %s ¶%d %s paragraph' %
              (pos['part'], pos['para_idx'],
               'containing' if own else
               ('preceding' if mode == 'after' else 'following')))
        # the anchor sha pins the matched corpus line (not the docx text —
        # the md line may carry footnote refs / markup the source lacks)
        if len(hits) == 1:
            conf = 'anchor_exact' if len(txt) >= 40 else 'anchor_strong'
            return {'rel': hits[0][0], 'line': hits[0][1], 'mode': mode,
                    'sha': hashlib.sha256(hits[0][2]
                                          .encode('utf-8')).hexdigest()}, \
                conf, ev + f' -> {hits[0][0]}:{hits[0][1]}'
        # multiple identical paragraphs: disambiguate by the next paragraph
        nxt = clean(pos['next']) if mode != 'before' else None
        if nxt:
            survivors = []
            for rel, i, nl in hits:
                nlines = mdidx.get(nxt) or []
                if any(r == rel and j > i for r, j in nlines):
                    survivors.append((rel, i, nl))
            if len(survivors) == 1:
                return {'rel': survivors[0][0], 'line': survivors[0][1],
                        'mode': mode,
                        'sha': hashlib.sha256(survivors[0][2]
                                              .encode('utf-8'))
                        .hexdigest()}, 'anchor_strong', \
                    ev + ' (disambiguated by next paragraph)'
        return None, 'anchor_probable', \
            ev + f' ({len(hits)} identical paragraphs, ambiguous)'
    return None, 'unbound_confirmed', 'no paragraph text matched corpus'


# --------------------------------------------------------------------------
# Second-pass provenance classification (media resolution pass)
PROV_SCREENSHOT_RX = re.compile(
    r'(скриншот|снимок\s+экрана|screenshot|принтскрин|printscreen|'
    r'копия\s+(страниц|документ)|скан\s|сканированн|факсимил|'
    r'фотокопи)', re.I)
PROV_COVER_RX = re.compile(r'(обложк|титульн|cover\b)', re.I)
PROV_PHOTO_RX = re.compile(
    r'(фотограф|фото\b|фотоснимок|фотокарточк|кадр\s+из)', re.I)
PROV_REPRO_RX = re.compile(
    r'(репродукц|картин|плакат|афиш|из\s+книг|из\s+журнал|из\s+газет|'
    r'иллюстрац.{0,20}из\b|сайт|https?://|www\.)', re.I)
PROV_SCHEME_RX = re.compile(
    r'(схем|диаграмм|алгоритм|структур|иерархи|блок-|матриц)', re.I)
PROV_TABLE_RX = re.compile(r'(таблиц|график|гистограмм|крива[ях])', re.I)

AUTHORIAL_CLASSES = {'authorial_scheme_or_diagram', 'authorial_table_or_chart',
                     'authorial_formula_or_math'}
THIRD_PARTY_CLASSES = {'third_party_photo', 'third_party_reproduction',
                       'cover_or_external_art', 'screenshot_or_document_scan'}


def load_decisions(path):
    """review/media_decisions.jsonl: human/agent-verified verdicts for
    objects the automatic evidence cannot decide.  Fields:
    figure_id, provenance_class, decision(promote|block), evidence."""
    dec = {}
    if not path or not Path(path).is_file():
        return dec
    for l in Path(path).read_text(encoding='utf-8').splitlines():
        if l.strip():
            r = json.loads(l)
            dec[r['figure_id']] = r
    return dec


def classify_provenance(fig: dict):
    """(provenance_class, [evidence]) for one registry record."""
    ev = []
    ext = fig.get('_src_ext') or \
        os.path.splitext(fig.get('original_filename') or '')[1].lower()
    mt = fig.get('media_type') or 'other'
    cap = fig.get('caption') or ''
    lab = fig.get('figure_label') or ''
    ctx = (cap + ' ' + (fig.get('context_excerpt') or '') + ' ' +
           (fig.get('docx_context') or ''))
    # drop markdown image refs and tags before signal matching — the
    # editorial boilerplate in inserted/converted refs ('иллюстрация
    # восстановлена из DOC', 'Иллюстрация из исходного документа') is not
    # provenance evidence and false-fires PROV_REPRO_RX
    ctx = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', ctx)
    ctx = re.sub(r'<[^>]+>', ' ', ctx)
    # 1. third-party signals win (conservative)
    if PROV_COVER_RX.search(ctx) or mt == 'cover':
        ev.append('cover signal in context/media_type')
        return 'cover_or_external_art', ev
    if PROV_SCREENSHOT_RX.search(ctx) or mt == 'scan_fragment':
        ev.append('screenshot/scan signal in context')
        return 'screenshot_or_document_scan', ev
    if PROV_PHOTO_RX.search(ctx) or mt == 'photo':
        ev.append('photo signal in context/media_type')
        return 'third_party_photo', ev
    if PROV_REPRO_RX.search(ctx) or mt in ('reproduction', 'map'):
        ev.append('reproduction/external-source signal')
        return 'third_party_reproduction', ev
    # 2. authorial signals
    lab_l = lab.lower()
    if re.search(r'(таблиц|график)', lab_l):
        ev.append(f'numbered table/chart label {lab!r}')
        return 'authorial_table_or_chart', ev
    if PROV_TABLE_RX.search(cap):
        ev.append('table/chart caption')
        return 'authorial_table_or_chart', ev
    if ext in VECTOR:
        ev.append(f'vector source format {ext}')
        return 'authorial_scheme_or_diagram', ev
    if lab and LABEL_RX.search(lab):
        ev.append(f'numbered figure label {lab!r}')
        return 'authorial_scheme_or_diagram', ev
    if PROV_SCHEME_RX.search(cap):
        ev.append('scheme/diagram caption')
        return 'authorial_scheme_or_diagram', ev
    if mt in ('scheme', 'diagram', 'chart', 'table_image'):
        ev.append(f'media_type {mt}')
        return ('authorial_table_or_chart'
                if mt in ('chart', 'table_image')
                else 'authorial_scheme_or_diagram'), ev
    ev.append('no decisive authorial or third-party evidence')
    return 'unclear_after_second_pass', ev


def _restage_bytes(fig: dict):
    """Re-read source bytes for a record upgraded in the second pass."""
    src = fig.get('_src')
    if not src:
        return None
    kind, path, member = src
    try:
        if kind == 'docx':
            return zipfile.ZipFile(path).read(member)
        if kind == 'fb2bin':
            _, bins = fb2_items(Path(path))
            return bins.get(member, (None, None))[1]
        return Path(path).read_bytes()
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def _add_src(fig: dict, kind, path, member, ext):
    fig['_src'] = (kind, str(path), member)
    fig['_src_ext'] = ext


def reclassify(b: Builder, decisions=None):
    """Second pass: provenance classes + sha256-group rights aggregation.

    A media object is promoted only when at least one occurrence carries an
    authorial signal and no occurrence anywhere carries a third-party signal.
    `decisions` (figure_id -> verdict) overrides automatic classification for
    records that were reviewed by hand (e.g. formula images inside prose).
    """
    decisions = decisions or {}
    stats = {'promoted': 0, 'kept_blocked': 0, 'unclear': 0}
    for f in b.figs:
        cls, ev = classify_provenance(f)
        dec = decisions.get(f['figure_id'])
        if dec:
            cls = dec.get('provenance_class') or cls
            ev = ev + ['manual review decision: ' +
                       (dec.get('evidence') or '')]
            f['manual_decision'] = dec.get('decision')
        f['provenance_class'] = cls
        f['provenance_evidence'] = ev
    # fail closed: a record the first pass published on weak signals stays
    # blocked if the second pass finds third-party provenance evidence
    # (manual 'promote' decisions override)
    for f in b.figs:
        dec = decisions.get(f['figure_id'])
        if f['publication_status'] == 'published' \
                and f.get('provenance_class') in THIRD_PARTY_CLASSES \
                and not (dec and dec.get('decision') == 'promote'):
            f['publication_status'] = 'metadata_only_rights'
            f['public_path'] = None
            f['public_asset_sha256'] = None
            f['rights_status'] = 'rights_review_required'
            f['provenance_evidence'] = f.get('provenance_evidence', []) + [
                'demoted: second-pass third-party signal overrides '
                'first-pass publish']
            stats['kept_blocked'] += 1
    by_sha = {}
    for f in b.figs:
        if f.get('source_sha256'):
            by_sha.setdefault(f['source_sha256'], []).append(f)
    for sha, g in by_sha.items():
        classes = {f.get('provenance_class') for f in g}
        if classes & THIRD_PARTY_CLASSES:
            continue
        auth = classes & AUTHORIAL_CLASSES
        if not auth:
            stats['unclear'] += sum(
                1 for f in g
                if f['publication_status'] == 'metadata_only_rights')
            continue
        canon = b.seen_sha.get(sha)
        for f in g:
            if f['publication_status'] == 'metadata_only_rights' or \
                    f.get('duplicate_blocked_by') == 'metadata_only_rights':
                f['rights_status'] = 'authorial'
                f['provenance_class'] = sorted(auth)[0]
                f['provenance_evidence'] = f.get('provenance_evidence', []) + \
                    ['authorial signal confirmed on a duplicate occurrence '
                     'of the same object (sha256 group)']
                stats['promoted'] += 1
        if canon and canon['publication_status'] == 'metadata_only_rights':
            data = _restage_bytes(canon)
            ext = canon.get('_src_ext') or '.png'
            if data and ext in RASTER:
                b._write(canon, data, ext,
                         'verbatim_copy (second-pass promotion)')
            elif data and ext in VECTOR:
                cdir = b.a.tmpdir / 'conv'
                cdir.mkdir(parents=True, exist_ok=True)
                src = cdir / (canon['figure_id'] + ext)
                src.write_bytes(data)
                b.to_convert.append((src, canon['figure_id'], canon))
                canon['_pending'] = True
        for f in g:
            if f is canon or f['publication_status'] != 'duplicate':
                continue
            if canon and canon.get('public_path'):
                f['public_path'] = canon['public_path']
                f.pop('duplicate_blocked_by', None)
    return stats


def resolve_source(b: Builder, w: dict):
    """(kind, path) of the source container for a work, or (None, path)."""
    s = w.get('sha256_source')
    sn = w.get('source_name') or ''
    e = b.cl.get(s)
    if e:
        return 'docx', b.a.audit / 'docx' / f"{e['id']}.docx"
    e = b.dc.get(s)
    if e:
        stem = e['file'].rsplit('.', 1)[0]
        p = b.a.audit / 'dotu_docx' / f'{stem}.docx'
        if p.exists():
            return 'docx', p
        return e['ext'], b.a.audit / 'dotu_files' / e['file']
    if sn.startswith('dotu.ru:'):
        base = sn.split(':', 1)[1]
        stem = base.rsplit('.', 1)[0]
        for cand in (b.a.audit / 'repair_src' / f'{stem}.docx',
                     b.a.audit / 'dotu_files' / base):
            if cand.exists():
                return cand.suffix.lstrip('.').lower(), cand
    return None, None


def iter_refs(body: str):
    """yield (name, line_idx, body_lines)"""
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        for m in IMG_MD_RX.finditer(ln):
            tgt = m.group(2).split()[0] if ' ' in m.group(2) else m.group(2)
            if '://' in tgt:
                continue
            yield tgt.rsplit('/', 1)[-1], i, lines
        for m in IMG_TAG_RX.finditer(ln):
            src = IMG_SRC_RX.search(m.group(0))
            if src and '://' not in src.group(1):
                yield src.group(1).rsplit('/', 1)[-1], i, lines


def build_extra(b: Builder):
    extra = b.a.unified
    works = {json.loads(l)['work_id']: json.loads(l)
             for l in (extra / 'data_works.jsonl')
             .read_text(encoding='utf-8').splitlines() if l.strip()}
    secidx = load_sections_index(extra)
    smap = {r['work_id']: r for r in
            json.loads((b.a.audit / 'media_source_map.json')
                       .read_text(encoding='utf-8'))}
    excl = set()
    ep = extra / 'excluded_works.json'
    if ep.exists():
        excl = {e['work_id'] for e in json.loads(ep.read_text())}
    # source-resolution indexes (private, audit dir)
    b.cl = {json.loads(l)['sha256']: json.loads(l)
            for l in (b.a.audit / 'convert_log.jsonl')
            .read_text(encoding='utf-8').splitlines() if l.strip()}
    b.dc = {json.loads(l)['sha256']: json.loads(l)
            for l in (b.a.audit / 'dotu_classified.jsonl')
            .read_text(encoding='utf-8').splitlines() if l.strip()}
    for wid in sorted(works):
        w = works[wid]
        if wid in excl:
            continue
        kind, src = resolve_source(b, w)
        if kind == 'fb2' and src and src.exists():
            _build_fb2(b, w, src, secidx)
            continue
        docx = Path(src) if kind == 'docx' and src else None
        if docx is None or not docx.exists():
            for name in (smap.get(wid, {}).get('refs') or []):
                b.add_fig(figure_id=fig_id(wid, name), work_id=wid,
                          work_dir=wid, section_id=None,
                          original_filename=name,
                          source_kind='missing_source',
                          source_file_or_url=w.get('dotu_url'),
                          notes='Исходный документ недоступен на этом '
                                'компьютере.')
            if smap.get(wid):
                b.errors.append(f'{wid}: source docx missing {src}')
            continue
        z = zipfile.ZipFile(docx)
        zmedia = {os.path.basename(n): n for n in z.namelist()
                  if n.startswith('word/media/')}
        # media-resolution pass: in-document positions + corpus text index
        pos_map = docx_image_positions(docx)
        md_bodies = []
        # ref sites across all section files
        sites = {}   # name -> [(section_id, path, idx, lines)]
        md_files = sorted((extra / w['dir']).rglob('*.md'))
        for f in md_files:
            rel = f.relative_to(extra).as_posix()
            body = f.read_text(encoding='utf-8')
            md_bodies.append((rel, body))
            for name, idx, lines in iter_refs(body):
                sites.setdefault(name, []).append(
                    (secidx.get(rel), rel, idx, lines))
        mdidx = work_md_index(md_bodies)
        # bound figures
        bound_znames = set()
        zstems = {os.path.splitext(n)[0]: n for n in zmedia}
        for name, lst in sorted(sites.items(),
                                key=lambda x: x[1][0][2]):
            sec_id, rel, idx, lines = lst[0]
            cap, lab = caption_and_label(lines, idx)
            zname = zmedia.get(name)
            # ref may already carry the converted extension (apply-step
            # rewrote media/image10.wmf -> .png); fall back to stem match
            stem_hit = False
            if zname is None and \
                    os.path.splitext(name)[0] in zstems:
                zname = zmedia[zstems[os.path.splitext(name)[0]]]
                stem_hit = True
            ext = os.path.splitext(name)[1].lower()
            ctx = ' '.join(l.strip() for l in
                           lines[max(0, idx - 4):idx + 5])[:800]
            if zname is None:
                # ref without object in docx (malformed ref, e.g. #anchor)
                f = b.add_fig(
                    figure_id=fig_id(wid, name), work_id=wid, work_dir=wid,
                    section_id=sec_id, original_filename=name,
                    source_kind='unresolved_reference',
                    source_file_or_url=w.get('source_name'),
                    caption=cap, figure_label=lab,
                    notes='Ссылка в разметке не соответствует объекту в '
                          'исходном документе (вероятно, битая ссылка-якорь).')
                continue
            bound_znames.add(zname)
            data = z.read(zname)
            # classify/stage by the real member format, not the (possibly
            # rewritten) ref extension
            src_ext = os.path.splitext(zname)[1].lower()
            dims = image_size(data, src_ext)
            mt = media_type_of(cap, src_ext, lab)
            # source-level context from document.xml (stronger than the
            # converted-md window when the md conversion dropped the caption)
            dpos = pos_map.get(os.path.basename(zname)) or []
            dctx = ' '.join(t for t in (
                dpos[0]['prev'], dpos[0]['para_text'], dpos[0]['next'])
                if t)[:800] if dpos else ''
            if not cap and dctx:
                cap, lab = caption_and_label(
                    [dpos[0]['prev'], dpos[0]['para_text'],
                     dpos[0]['next']], 1)
            fig = b.add_fig(
                figure_id=fig_id(wid, name), work_id=wid, work_dir=wid,
                section_id=sec_id,
                source_kind='docx_embedded',
                source_file_or_url=w.get('dotu_url') or w.get('source_name'),
                source_page=None,
                original_filename=os.path.basename(zname)
                if stem_hit else name, caption=cap, figure_label=lab,
                media_type=mt,
                rights_status=rights_of(mt, cap,
                                        (ctx + ' ' + dctx).strip(),
                                        src_ext, lab),
                criticality=criticality_of(mt, src_ext, lab, len(data), dims),
                ref_count=len(lst), ref_sites=[x[1] for x in lst],
                containing_source=w.get('source_name'),
                containing_source_sha256=w.get('sha256_source'),
                media_file=name,
                context_excerpt=(ctx + ' ' + dctx).strip()[:800],
                docx_context=dctx or None)
            _add_src(fig, 'docx', docx, zname, src_ext)
            b.stage(fig, data, None, src_ext)
        # unbound zip media: recover position from document.xml order
        for zbase, zname in sorted(zmedia.items()):
            if zbase in sites or zname in bound_znames:
                continue
            data = z.read(zname)
            ext = os.path.splitext(zbase)[1].lower()
            dims = image_size(data, ext)
            mt = media_type_of(None, ext, None)
            dpos = pos_map.get(zbase) or []
            anch, conf, ev = recover_anchor(dpos, mdidx, md_bodies)
            dctx = ' '.join(t for t in (
                dpos[0]['prev'], dpos[0]['para_text'], dpos[0]['next'])
                if t)[:800] if dpos else ''
            cap = lab = None
            if dpos:
                cap, lab = caption_and_label(
                    [dpos[0]['prev'], dpos[0]['para_text'],
                     dpos[0]['next']], 1)
            fig = b.add_fig(
                figure_id=fig_id(wid, zbase, unbound=True),
                work_id=wid, work_dir=wid,
                section_id=secidx.get(anch['rel']) if anch else None,
                source_kind='docx_embedded_unbound',
                source_file_or_url=w.get('dotu_url') or w.get('source_name'),
                original_filename=zbase, media_type=mt,
                caption=cap, figure_label=lab,
                referenced_in_text=False,
                rights_status=rights_of(mt, cap, dctx, ext, lab),
                criticality=criticality_of(mt, ext, lab, len(data), dims),
                containing_source=w.get('source_name'),
                containing_source_sha256=w.get('sha256_source'),
                media_file=zbase,
                anchor_confidence=conf,
                anchor_evidence=ev,
                context_excerpt=dctx or None,
                docx_context=dctx or None,
                notes='Объект внедрён в исходный документ, но не встречается '
                      'в разметке Markdown (верхний колонтитул/плавающий '
                      'объект либо выпадение при конвертации).')
            if anch:
                fig['anchor_file'] = anch['rel']
                fig['anchor_line'] = anch['line']
                fig['anchor_mode'] = anch['mode']
                fig['anchor_sha256'] = anch['sha']
                if dpos and dpos[0]['part'] != 'document.xml':
                    fig['notes'] += f' Объект в {dpos[0]["part"]}.'
            _add_src(fig, 'docx', docx, zname, ext)
            b.stage(fig, data, None, ext)


def _build_fb2(b: Builder, w: dict, fb2: Path, secidx: dict):
    """FB2 sources embed images as base64 <binary>; the md conversion dropped
    every <image> element, so positions are recovered by anchoring each
    occurrence to its containing paragraph/section line in the corpus md."""
    wid = w['work_id']
    items, bins = fb2_items(fb2)
    if not items:
        return
    # normalized md line index for this work
    md = {}
    md_files = sorted((b.a.unified / w['dir']).rglob('*.md'))
    for f in md_files:
        rel = f.relative_to(b.a.unified).as_posix()
        for i, ln in enumerate(f.read_text(encoding='utf-8').splitlines()):
            t = _norm_text(ln)
            if t:
                md.setdefault(t, []).append((rel, i))
    CT_EXT = {'image/png': '.png', 'image/jpeg': '.jpeg', 'image/jpg': '.jpeg',
              'image/gif': '.gif', 'image/bmp': '.bmp'}
    for it in items:
        anchor = it['anchor'] or it['sec'] or ''
        loc = md.get(anchor)
        before = False
        # anchor not found -> bind to the following paragraph instead
        if not loc and it.get('next'):
            loc = md.get(it['next'])
            if loc:
                anchor, before = it['next'], True
        ct, data = bins.get(it['id'], (None, None))
        ext = CT_EXT.get(ct or '', '.png')
        name = it['id'] or f'bin_{it["order"]}'
        fig = b.add_fig(
            figure_id=fig_id(wid, f'fb2-{it["order"]:03d}-{name}'),
            work_id=wid, work_dir=wid,
            section_id=secidx.get(loc[0][0]) if loc else None,
            source_kind='fb2_embedded',
            source_file_or_url=w.get('dotu_url') or w.get('source_name'),
            original_filename=name,
            referenced_in_text=bool(loc),
            containing_source=w.get('source_name'),
            containing_source_sha256=w.get('sha256_source'),
            media_type='other',
            inline_in_text=it.get('inline'),
            media_file=(name if os.path.splitext(name)[1].lower() in RASTER
                        else name + ext))
        if loc:
            fig['anchor_file'] = loc[0][0]
            fig['anchor_line'] = loc[0][1]
            fig['anchor_mode'] = 'before' if before else 'after'
            fig['anchor_sha256'] = hashlib.sha256(
                anchor.encode('utf-8')).hexdigest()
            fig['anchor_confidence'] = 'anchor_exact'
            fig['anchor_evidence'] = \
                'fb2 document-order paragraph -> unique corpus line match'
        else:
            fig['notes'] = ('Позиция в разметке не найдена по тексту '
                            f'абзаца (fb2 section: {it["sec"]!r}).')
        if data is None:
            fig['publication_status'] = 'metadata_only_source'
            fig['notes'] = (fig.get('notes') or '') + \
                ' Объект отсутствует среди <binary> источника.'
            continue
        # caption often sits in the paragraph right after the image —
        # check the fb2 next-paragraph first, then md context
        if loc:
            rel, i = loc[0]
            lines = (b.a.unified / rel).read_text(encoding='utf-8') \
                .splitlines()
            cap, lab = caption_and_label(lines, i)
            nxt = it.get('next') or ''
            if not cap and LABEL_RX.search(nxt):
                cap, lab = nxt.strip('*').strip(), (LABEL_RX.search(nxt)
                                                  .group(0))
            fig['caption'], fig['figure_label'] = cap, lab
            ctx = ' '.join(l.strip() for l in
                           lines[max(0, i - 4):i + 5])[:800]
        else:
            ctx = ''
        ext_bin = os.path.splitext(name)[1].lower() or ext
        use_ext = ext_bin if ext_bin in RASTER | VECTOR else ext
        dims = image_size(data, use_ext)
        mt = media_type_of(fig['caption'], use_ext, fig['figure_label'])
        fig['media_type'] = mt
        fig['context_excerpt'] = ctx or None
        fig['rights_status'] = rights_of(mt, fig['caption'], ctx, use_ext, fig['figure_label'])
        fig['criticality'] = criticality_of(mt, use_ext,
                                           fig['figure_label'], len(data),
                                           dims)
        _add_src(fig, 'fb2bin', fb2, it['id'], use_ext)
        b.stage(fig, data, None, use_ext)


def build_tom(b: Builder):
    corpus = b.a.tom_corpus
    # review queue joins
    q = {}
    qp = b.a.queue_dir / ('media' + '_review_queue.jsonl')
    if qp.exists():
        for l in qp.read_text(encoding='utf-8').splitlines():
            if l.strip():
                r = json.loads(l)
                q[(r['volume'], r['image'])] = r
    # tom-1 authored figure index (pdf pages verified there)
    t1fig = {}
    t1f = corpus / 'tom-1' / 'data' / 'figures.jsonl'
    if t1f.exists():
        for l in t1f.read_text(encoding='utf-8').splitlines():
            if l.strip():
                r = json.loads(l)
                t1fig[r['source_media_filename']] = r
    vol_work = {v: (corpus / f'tom-{v}' / 'data' / 'works.jsonl')
                for v in range(1, 7)}
    doc_sha = {}
    for v, p in vol_work.items():
        if p.exists():
            r = json.loads(p.read_text(encoding='utf-8').splitlines()[0])
            doc_sha[v] = r.get('doc_sha256')
    for v in range(1, 7):
        book = corpus / f'tom-{v}'
        wid = f'osnovy-sociologii-tom-{v}'
        media_dir = book / 'assets' / 'media'
        orig_dir = book / 'assets' / 'media_original'
        media = {f.name: f for f in media_dir.iterdir()} if media_dir.is_dir() else {}
        orig = {f.name: f for f in orig_dir.iterdir()} if orig_dir.is_dir() else {}
        ostems = {}
        for n in orig:
            ostems[os.path.splitext(n)[0]] = n
        # ref sites: only files that the public export actually emits
        sj = book / 'data' / 'sections.jsonl'
        sec_paths = [json.loads(l)['path'] for l in
                     sj.read_text(encoding='utf-8').splitlines() if l.strip()]
        sites = {}
        md_bodies = []
        for rel in sec_paths:
            f = book / rel
            if not f.is_file():
                continue
            body = f.read_text(encoding='utf-8')
            md_bodies.append((rel, body))
            for name, idx, lines in iter_refs(body):
                sites.setdefault(name, []).append((rel, idx, lines))
        mdidx = work_md_index(md_bodies)
        # media-resolution pass: convert the DOC source to DOCX once and map
        # document.xml positions -> corpus media names (basename or sha256)
        pos_map, mem_sha = {}, {}
        src_doc = book / 'source' / f'osnovy-sociologii-tom-{v}.doc'
        tdocx = b.a.tmpdir / 'tomdocx' / \
            f'osnovy-sociologii-tom-{v}.docx'
        if src_doc.is_file() and not tdocx.exists():
            tdocx.parent.mkdir(parents=True, exist_ok=True)
            profile = (b.a.tmpdir.resolve() / 'lo_profile').as_uri()
            subprocess.run(
                [SOFFICE, '--headless', f'-env:UserInstallation={profile}',
                 '--convert-to', 'docx', '--outdir', str(tdocx.parent),
                 str(src_doc)], capture_output=True, timeout=900)
        if tdocx.exists():
            pos_map = docx_image_positions(tdocx)
            try:
                zt = zipfile.ZipFile(tdocx)
                mem_sha = {
                    sha256_bytes(zt.read(n)): os.path.basename(n)
                    for n in zt.namelist() if n.startswith('word/media/')}
            except zipfile.BadZipFile:
                mem_sha = {}
        def _pos_for(media_file, orig_file, name, key):
            if not pos_map:
                return []
            for cand in (key, name):
                if cand in pos_map:
                    return pos_map[cand]
            for srcf in (orig_file, media_file):
                if srcf:
                    mn = mem_sha.get(sha256_file(srcf))
                    if mn and mn in pos_map:
                        return pos_map[mn]
            return []
        # a figure record exists for every extracted object on disk AND for
        # every text reference; disk-only objects are unbound-but-present
        fig_dir = book / 'figures'
        fig_files = ({f.name: f for f in fig_dir.iterdir()}
                     if fig_dir.is_dir() else {})
        all_names = sorted(set(media) | set(sites) | set(fig_files))
        sites_by_stem = {}
        for n in sites:
            sites_by_stem.setdefault(os.path.splitext(n)[0], n)
        for name in all_names:
            stem = os.path.splitext(name)[0]
            mstems = {os.path.splitext(n): n for n in media}
            # canonical key: the media/ filename when it exists
            key = name if name in media else mstems.get(stem, name)
            lst = sites.get(name) or sites.get(key) or \
                (sites.get(sites_by_stem[stem]) if stem in sites_by_stem
                 else None)
            rel, idx, lines = (lst[0] if lst else (None, -1, []))
            cap = lab = None
            if lst:
                cap, lab = caption_and_label(lines, idx)
            qr = q.get((v, name), {}) or q.get((v, key), {})
            t1 = t1fig.get(name, {}) or t1fig.get(key, {})
            # resolve on-disk asset: exact name in media/, else stem match,
            # else figures/ dir (faithful pdf crops), else missing
            preview = media.get(key)
            orig_file = orig.get(ostems.get(stem)) if ostems.get(stem) else None
            ext = os.path.splitext(key)[1].lower()
            oext = os.path.splitext(orig_file.name)[1].lower() if orig_file else ext
            ctx = ' '.join(l.strip() for l in
                           lines[max(0, idx - 4):idx + 5])[:800]
            mt = media_type_of(cap, oext, lab)
            # media-resolution pass: source positions in the converted DOCX
            pos = _pos_for(preview, orig_file, name, key)
            dctx = ' '.join(t for t in (
                pos[0]['prev'], pos[0]['para_text'], pos[0]['next'])
                if t)[:800] if pos else ''
            if not cap and pos:
                cap, lab = caption_and_label(
                    [pos[0]['prev'], pos[0]['para_text'], pos[0]['next']], 1)
            anch = aconf = aev = None
            if not lst:
                anch, aconf, aev = recover_anchor(pos, mdidx, md_bodies)
            fig = b.add_fig(
                figure_id=fig_id(wid, stem), work_id=wid, work_dir=f'tom-{v}',
                section_id=rel,
                source_kind=('docx_embedded_preview'
                             if (preview or orig_file) else 'missing_source'),
                source_file_or_url=f'osnovy-sociologii-tom-{v}.doc',
                source_page=(qr.get('pdf_page_candidate')
                             or t1.get('pdf_page')),
                source_bbox=qr.get('pdf_image_bbox_candidate'),
                original_filename=orig_file.name if orig_file else name,
                caption=cap, figure_label=lab, media_type=mt,
                rights_status=rights_of(mt, cap,
                                        (ctx + ' ' + dctx + ' ' +
                                         (qr.get('source_context')
                                          or '')).strip(), oext, lab),
                criticality=None,  # set below
                referenced_in_text=bool(lst),
                ref_count=len(lst or []),
                ref_sites=[x[0] for x in (lst or [])],
                containing_source=f'osnovy-sociologii-tom-{v}.doc',
                containing_source_sha256=doc_sha.get(v),
                anchor_confidence=aconf,
                anchor_evidence=aev,
                context_excerpt=(ctx + ' ' + dctx).strip()[:800] or None,
                docx_context=dctx or None,
                notes=((('Автоматическая привязка к странице PDF — кандидатная, '
                         'требует визуальной сверки. ')
                        if qr.get('pdf_page_candidate') else '') +
                       (('Объект извлечён из исходного DOC, но прямой ссылки '
                         'на него в экспортируемых разделах нет.')
                        if not lst else '') or None),
                media_file=(key if preview else None))
            if anch:
                fig['anchor_file'] = anch['rel']
                fig['anchor_line'] = anch['line']
                fig['anchor_mode'] = anch['mode']
                fig['anchor_sha256'] = anch['sha']
            _add_src(fig, 'file', orig_file or preview or '', None, oext)
            if preview:
                data = preview.read_bytes()
                fig['criticality'] = criticality_of(
                    mt, oext, lab, len(data), image_size(data, ext))
                # source object hash = original when present, else preview
                src_data = (orig_file.read_bytes() if orig_file else data)
                fig['source_sha256'] = sha256_bytes(src_data)
                canon = b.seen_sha.get(fig['source_sha256'])
                if canon:
                    fig['publication_status'] = 'duplicate'
                    fig['duplicate_of'] = canon['figure_id']
                    fig['public_path'] = canon.get('public_path')
                else:
                    b.seen_sha[fig['source_sha256']] = fig
                    if fig['rights_status'] in ('rights_review_required',
                                                'unknown'):
                        fig['publication_status'] = 'metadata_only_rights'
                    else:
                        b._write(fig, data, ext,
                                 ('extracted_preview' if oext != ext else
                                  'verbatim_copy') +
                                 (f' (original {oext})' if oext != ext else ''))
            else:
                # try figures/ dir (faithful page crops like os3-…-p369.png)
                alt = book / 'figures' / name
                if alt.is_file():
                    data = alt.read_bytes()
                    _add_src(fig, 'file', alt, None, ext)
                    fig['source_kind'] = 'pdf_page_crop'
                    fig['source_file_or_url'] = f'osnovy-sociologii-tom-{v}.pdf'
                    fig['criticality'] = criticality_of(
                        mt, ext, lab, len(data), image_size(data, ext))
                    fig['source_sha256'] = sha256_bytes(data)
                    canon = b.seen_sha.get(fig['source_sha256'])
                    if canon:
                        fig['publication_status'] = 'duplicate'
                        fig['duplicate_of'] = canon['figure_id']
                        fig['public_path'] = canon.get('public_path')
                    else:
                        b.seen_sha[fig['source_sha256']] = fig
                        if fig['rights_status'] in ('rights_review_required',
                                                    'unknown'):
                            fig['publication_status'] = 'metadata_only_rights'
                        else:
                            b._write(fig, data, ext,
                                     'faithful_pdf_crop (prior pipeline)')
                else:
                    fig['criticality'] = criticality_of(mt, ext, lab, 0, None)
                    fig['publication_status'] = 'metadata_only_source'
                    fig['notes'] = ((fig.get('notes') or '') +
                                    ' Объект не найден в assets/media, '
                                    'media_original и figures/.')
        # OLE objects for this volume
        ole_dir = b.a.toolkit / 'ole'
        for ob in sorted(ole_dir.glob(f'tom-{v}-oleObject*.bin')) \
                if ole_dir.is_dir() else []:
            _handle_ole(b, ob, v, wid, doc_sha.get(v))


def _handle_ole(b: Builder, ob: Path, v: int, wid: str, doc_sha):
    import olefile
    fig = b.add_fig(
        figure_id=fig_id(wid, ob.stem), work_id=wid, work_dir=f'tom-{v}',
        section_id=None, source_kind='ole_object',
        source_file_or_url=f'osnovy-sociologii-tom-{v}.doc',
        original_filename=ob.name, media_type='other',
        referenced_in_text=False,
        containing_source_sha256=doc_sha,
        notes='OLE-объект DOC: тип/размещение в тексте не определены.')
    try:
        ole = olefile.OleFileIO(str(ob))
        streams = ['/'.join(s) for s in ole.listdir()]
        if ole.exists('\x03EPRINT'):
            emf = ole.openstream('\x03EPRINT').read()
            fig['notes'] = (fig['notes'] +
                            ' Содержит EMF-превью (EPRINT) и внедрённый '
                            'пакет данных.')
            pkg = ole.openstream('Package').read() if ole.exists('Package') \
                else None
            if pkg:
                pkgp = b.a.tmpdir / f'{ob.stem}.pkg.zip'
                pkgp.write_bytes(pkg)
                fig['notes'] += ' Пакет сохранён для ревью в рабочей папке.'
            fig['media_type'] = 'scheme'
            fig['criticality'] = 'unclear'
            fig['rights_status'] = 'authorial'
            fig['source_sha256'] = sha256_bytes(emf)
            # OLE placement in the text is unresolved — keep metadata-only
            # even though an EMF preview is extractable (issue policy: no
            # restoration claim without reliable placement)
            fig['publication_status'] = 'metadata_only_source'
            canon = b.seen_sha.get(fig['source_sha256'])
            if canon:
                fig['duplicate_of'] = canon['figure_id']
            else:
                b.seen_sha[fig['source_sha256']] = fig
        elif ole.exists('WordDocument'):
            fig['media_type'] = 'other'
            fig['rights_status'] = 'unknown'
            fig['publication_status'] = 'metadata_only_source'
            fig['notes'] += (' Содержит внедрённый Word-документ '
                             '(текстовый объект), графического превью нет; '
                             'требуется решение о размещении.')
            fig['source_sha256'] = sha256_file(ob)
        else:
            fig['publication_status'] = 'technical_failure'
            fig['notes'] += f' Нераспознанные потоки OLE: {streams}.'
            fig['source_sha256'] = sha256_file(ob)
        ole.close()
    except Exception as e:  # noqa: BLE001 - record, never crash the build
        fig['publication_status'] = 'technical_failure'
        fig['notes'] += f' Ошибка разбора OLE: {e!r}'
        fig['source_sha256'] = sha256_file(ob)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--unified', type=Path, required=True)
    ap.add_argument('--tom-corpus', type=Path, required=True)
    ap.add_argument('--audit', type=Path, required=True,
                    help='dir with media_source_map.json + docx sources')
    ap.add_argument('--queue-dir', type=Path, required=True)
    ap.add_argument('--toolkit', type=Path, required=True)
    ap.add_argument('--tmpdir', type=Path, required=True)
    ap.add_argument('--registry-out', type=Path, required=True)
    ap.add_argument('--assets-out', type=Path, required=True)
    ap.add_argument('--report-out', type=Path, required=True)
    ap.add_argument('--decisions', type=Path, default=None,
                    help='review/media_decisions.jsonl — verified verdicts '
                         'for objects the automatic pass cannot decide')
    a = ap.parse_args()
    a.tmpdir.mkdir(parents=True, exist_ok=True)
    b = Builder(a)
    build_tom(b)
    build_extra(b)
    dec_path = a.decisions or a.registry_out.parent / 'media_decisions.jsonl'
    rstats = reclassify(b, load_decisions(dec_path))
    b.run_conversions()
    # dedupe unresolved duplicate links now that paths exist
    rep = {}
    anchor_stats = {}
    prov_stats = {}
    for f in b.figs:
        for k in ('_pending', '_src', '_src_ext'):
            f.pop(k, None)
        rep[f['publication_status']] = rep.get(f['publication_status'], 0) + 1
        if f.get('anchor_confidence'):
            anchor_stats[f['anchor_confidence']] = \
                anchor_stats.get(f['anchor_confidence'], 0) + 1
        if f.get('provenance_class'):
            prov_stats[f['provenance_class']] = \
                prov_stats.get(f['provenance_class'], 0) + 1
    b.registry_out_parent = a.registry_out.parent
    a.registry_out.parent.mkdir(parents=True, exist_ok=True)
    with a.registry_out.open('w', encoding='utf-8') as fh:
        for f in b.figs:
            fh.write(json.dumps(f, ensure_ascii=False) + '\n')
    by_work = {}
    for f in b.figs:
        d = by_work.setdefault(f['work_id'], {'figs': 0, 'published': 0})
        d['figs'] += 1
        d['published'] += f['publication_status'] in ('published', 'duplicate')
    report = {'figures_total': len(b.figs), 'by_status': rep,
              'unique_objects': len(b.seen_sha), 'by_work': by_work,
              'staged_assets': len(b.staged),
              'anchor_recovery': anchor_stats,
              'provenance_classes': prov_stats,
              'second_pass': rstats, 'errors': b.errors}
    a.report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.exit(main())
