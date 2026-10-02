#!/usr/bin/env python3
"""Apply figures_registry.jsonl to the private corpus (Issue #13, part B6/B7).

For every work the registry documents:

* publishable staged assets are copied into the work's media directory
  (``<work>/media/`` for unified works; ``tom-N/assets/media/`` already
  holds the extracted objects and is verified, not re-copied);
* figure records bound to a Markdown anchor but absent from the text
  (fb2-embedded images silently dropped at conversion, unbound objects
  with a resolved position) get an image reference or an explicit
  editorial omission marker inserted at the anchor;
* references whose filename no longer matches the staged asset
  (wmf/emf -> png conversion) are rewritten in place;
* per-work ``figures_registry.jsonl`` and ``FIGURES.md`` are written.

Rights-blocked and unresolved records are documented in
``review/FIGURES_OWNER_ACTIONS.md`` — never silently dropped.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath

FIGS_TAG_RX = re.compile(r'<!--\s*figs:([0-9A-Za-zА-Яа-яёЁ_,\-]+)\s*-->')
OMIT_GROUP_MARKER = ('*[В оригинальном издании здесь расположены '
                     'иллюстрации ({n} объекта). Иллюстрации опущены: '
                     'см. реестр графического слоя работы.]*')
OMIT_MARKER = ('*[В оригинальном издании здесь расположена иллюстрация{lab}. '
               'Не воспроизводится до правовой проверки и решения владельца; '
               'см. запись в FIGURES.md.]*')

MEDIA_SUBDIR_UNIFIED = 'media'
MEDIA_SUBDIR_TOM = 'assets/media'


def norm_text(t: str) -> str:
    t = re.sub(r'[#>*`_~]', '', t or '')
    return ' '.join(t.replace('\xad', '').split())


def staged_file(assets: Path, rec: dict) -> Path | None:
    pp = rec.get('public_path')
    if not pp:
        return None
    p = assets / rec['work_dir'] / Path(pp).name
    return p if p.is_file() else None


def target_name(rec: dict, staged: Path) -> str:
    """Corpus filename: keep the referenced basename when the format is
    unchanged, else swap the extension for the staged asset's."""
    base = rec.get('media_file') or rec.get('original_filename') \
        or (Path(staged.name).name)
    stem, ext = os.path.splitext(base)
    if ext.lower() == staged.suffix.lower():
        return base
    return stem + staged.suffix


def work_roots(rec: dict, unified: Path, tom_corpus: Path,
               works_dir: dict) -> tuple[Path, str]:
    wd = rec['work_dir']
    if wd.startswith('tom-'):
        return tom_corpus / wd, MEDIA_SUBDIR_TOM
    wdir = works_dir.get(rec['work_id'], {}).get('dir') or f'books/{wd}'
    return unified / wdir, MEDIA_SUBDIR_UNIFIED


def figures_md(work_id: str, figs: list) -> str:
    out = [f'# Графический слой произведения: {work_id}', '',
           'Реестр иллюстраций/объектов, найденных в источниках работы. '
           'Формируется автоматически из `review/figures_registry.jsonl`; '
           'статусы — по состоянию на сборку реестра, до правовой проверки '
           'и решения владельца.', '',
           '| # | Метка | Тип | Статус | Источник | Позиция | Подпись |',
           '|---|-------|-----|--------|----------|---------|---------|']
    for i, f in enumerate(figs, 1):
        pos = (f.get('anchor_file') or
               (f.get('ref_sites') or [None])[0] or '—')
        if f.get('anchor_line') is not None:
            pos += f':{f["anchor_line"]}'
        out.append('| {i} | {lab} | {mt} | {st} | {src} | {pos} | {cap} |'
                   .format(i=i,
                           lab=(f.get('figure_label') or '—'),
                           mt=f.get('media_type') or '—',
                           st=f.get('publication_status') or '—',
                           src=(f.get('source_file_or_url') or '—'),
                           pos=pos.replace('|', '\\|'),
                           cap=(f.get('caption') or '—')[:80]
                           .replace('|', '\\|')))
    return '\n'.join(out) + '\n'


def owner_actions_md(by_status: dict, figs: list) -> str:
    """Compact decision pack: only items still needing a human decision.

    Items resolved automatically by the second pass (provenance
    reclassification + anchor recovery) are summarised, not listed."""
    total = len(figs)
    resolved = sum(1 for f in figs if f['publication_status'] in
                   ('published', 'duplicate', 'decorative_excluded'))
    rights = by_status.get('metadata_only_rights', [])
    srcs = by_status.get('metadata_only_source', [])
    unres = by_status.get('unresolved_reference', [])
    anchor_rev = [f for f in figs if f.get('anchor_confidence')
                  in ('anchor_probable', 'unbound_confirmed')
                  and not f.get('ref_sites')]
    out = [
        '# Действия владельца по графическому слою (Issue #13)',
        '',
        'Пакет после автоматического второго прохода (классификация '
        'происхождения + восстановление позиций). Записи ниже НЕ '
        'опубликованы. Перевод в публичный выпуск возможен только через '
        '`review/public_media_allowlist.jsonl` после правовой и визуальной '
        'проверки владельцем.',
        '',
        '## Сводка',
        '',
        f'- записей в реестре: {total}',
        f'- разрешено автоматически (published/duplicate/excluded): '
        f'{resolved}',
        f'- осталось решений владельца: '
        f'{len(rights) + len(srcs) + len(unres) + len(anchor_rev)}',
        '',
    ]
    # rights-blocked: group by provenance class so same-kind decisions
    # collapse into one bullet group
    by_class = {}
    for f in rights:
        by_class.setdefault(f.get('provenance_class') or 'unclassified',
                            []).append(f)
    class_titles = {
        'unclear_after_second_pass':
            '### Неясно после второго прохода — требуется визуальная проверка',
        'third_party_photo': '### Сторонние фотографии',
        'third_party_reproduction': '### Сторонние репродукции/карты',
        'cover_or_external_art': '### Обложки и сторонняя графика',
        'screenshot_or_document_scan': '### Скриншоты и сканы документов',
    }
    out += ['## Заблокировано правовым статусом', '',
            'Решение по каждому пункту: подтвердить права / оставить только '
            'метаданные. Для `unclear` нужна визуальная сверка объекта с '
            'оригиналом.', '']
    for cls, fl in sorted(by_class.items(),
                          key=lambda x: -len(x[1])):
        out += [class_titles.get(cls, f'### {cls}') +
                f' — {len(fl)} объектов', '']
        if cls == 'unclear_after_second_pass':
            # identical evidence on every item — group by work to keep the
            # pack compact; each object still needs individual visual review
            by_work_u = {}
            for f in fl:
                by_work_u.setdefault(f['work_id'], []).append(f)
            for wid, wfl in sorted(by_work_u.items()):
                ids = ', '.join(
                    f.get('original_filename') or f['figure_id']
                    for f in wfl)
                out.append(f'- `{wid}` — {len(wfl)} объектов: {ids}')
            out.append('')
            continue
        for f in fl:
            ev = '; '.join(f.get('provenance_evidence') or [])
            out.append(
                f"- `{f['figure_id']}` — {f.get('original_filename')}; "
                f"{f['work_id']}; {(f.get('caption') or '')[:70]}"
                + (f' [{ev[:80]}]' if ev else ''))
        out.append('')
    if srcs:
        out += ['## Только метаданные — источник не извлечён', '',
                'Для каждого пункта нужен указанный источник/действие.', '']
        for f in srcs:
            need = ('предоставить ' +
                    (f.get('source_file_or_url') or 'исходный файл'))
            out.append(f"- `{f['figure_id']}` — {f.get('source_kind')}; "
                       f"{f['work_id']}; нужно: {need}; "
                       f"{(f.get('notes') or '')[:80]}")
        out.append('')
    if anchor_rev:
        out += ['## Позиция в тексте не подтверждена', '',
                'Объект найден в источнике, но место в тексте определено '
                'лишь вероятностно либо не определено. Нужна ручная '
                'постановка (указать раздел/абзац) или решение о пропуске.',
                '']
        for f in anchor_rev:
            out.append(
                f"- `{f['figure_id']}` — {f.get('original_filename')}; "
                f"{f['work_id']}; {f['anchor_confidence']}: "
                f"{(f.get('anchor_evidence') or '')[:110]}")
        out.append('')
    if unres:
        out += ['## Неразрешённые ссылки в разметке', '']
        for f in unres:
            out.append(f"- `{f['figure_id']}` — "
                       f"{f.get('original_filename')}; {f['work_id']}; "
                       f"{(f.get('notes') or '')[:90]}")
        out.append('')
    return '\n'.join(out) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--registry', type=Path, required=True)
    ap.add_argument('--unified', type=Path, required=True)
    ap.add_argument('--tom-corpus', type=Path, required=True)
    ap.add_argument('--assets', type=Path, required=True)
    ap.add_argument('--owner-doc', type=Path, default=None)
    ap.add_argument('--report', type=Path, default=None)
    a = ap.parse_args()

    figs = [json.loads(l) for l in
            a.registry.read_text(encoding='utf-8').splitlines() if l.strip()]
    by_id = {f['figure_id']: f for f in figs}
    works_dir = {}
    dw = a.unified / 'data_works.jsonl'
    if dw.is_file():
        for l in dw.read_text(encoding='utf-8').splitlines():
            if l.strip():
                w = json.loads(l)
                works_dir[w['work_id']] = w

    by_work = {}
    for f in figs:
        by_work.setdefault(f['work_id'], []).append(f)

    report = {'copied': 0, 'inserted_refs': 0, 'inserted_markers': 0,
              'rewritten_refs': 0, 'anchor_mismatch': [], 'anchor_review': [],
              'missing_staged': [], 'tom_missing_disk': []}

    for wid, wfigs in sorted(by_work.items()):
        root, msub = work_roots(wfigs[0], a.unified, a.tom_corpus, works_dir)
        mdir = root / msub
        # 1) stage publishable assets into the corpus
        for f in wfigs:
            if f['publication_status'] not in ('published', 'duplicate'):
                continue
            canon = f
            if f['publication_status'] == 'duplicate':
                canon = by_id.get(f.get('duplicate_of') or '', f)
                if canon['publication_status'] != 'published':
                    # duplicate of a rights-blocked/missing object stays
                    # blocked — documented, not an error
                    continue
            src = staged_file(a.assets, canon)
            if src is None:
                report['missing_staged'].append(f['figure_id'])
                continue
            if msub == MEDIA_SUBDIR_TOM:
                # tom assets already extracted on disk — verify presence
                tgt = mdir / (f.get('media_file') or '')
                if not tgt.is_file():
                    report['tom_missing_disk'].append(f['figure_id'])
                    continue
                f['_corpus_media'] = str(tgt.resolve())
                continue
            mdir.mkdir(parents=True, exist_ok=True)
            name = target_name(f, src)
            tgt = mdir / name
            if not tgt.is_file() or \
                    hashlib.sha256(tgt.read_bytes()).hexdigest() != \
                    hashlib.sha256(src.read_bytes()).hexdigest():
                shutil.copy2(src, tgt)
                report['copied'] += 1
            f['_corpus_media'] = str(tgt.resolve())
        # 2) rewrite refs whose staged format changed the extension
        for f in wfigs:
            cm = f.get('_corpus_media')
            if not cm or f['publication_status'] not in ('published',
                                                       'duplicate'):
                continue
            base = f.get('media_file') or ''
            if os.path.splitext(base)[1].lower() == \
                    os.path.splitext(cm)[1].lower():
                continue
            for site in (f.get('ref_sites') or []):
                sp = a.unified / site
                if not sp.is_file():
                    continue
                t = sp.read_text(encoding='utf-8')
                nt = t.replace(f'/{base})', f'/{Path(cm).name})')
                # raw <img src="..."> tags carry the same stale name
                nt = nt.replace(f'/{base}"', f'/{Path(cm).name}"')
                if nt != t:
                    sp.write_text(nt, encoding='utf-8')
                    report['rewritten_refs'] += 1
                    # corpus file now carries the converted name — record it
                    f['media_file'] = Path(cm).name
        # 3) insert refs/markers at anchors for unreferenced bound figures
        ins = {}
        for f in wfigs:
            if not f.get('anchor_file'):
                continue
            # only exact/strong recovered anchors auto-place; probable or
            # unconfirmed stays in the owner-review queue
            if f.get('anchor_confidence') in ('anchor_probable',
                                              'unbound_confirmed'):
                report.setdefault('anchor_review', []).append(
                    f['figure_id'])
                continue
            # fb2 images were silently dropped at conversion — the anchor
            # binding is the only record of their position, so they always
            # need insertion even though the record is "bound"
            if f['source_kind'] != 'fb2_embedded' and \
                    f.get('referenced_in_text'):
                continue
            ins.setdefault(f['anchor_file'], []).append(f)
        for rel, lst in ins.items():
            sp = (root if msub == MEDIA_SUBDIR_TOM else a.unified) / rel
            if not sp.is_file():
                continue
            lines = sp.read_text(encoding='utf-8').splitlines()
            # group by anchor line so several images at one anchor keep
            # their document order; process lines bottom-up
            by_line = {}
            for f in lst:
                by_line.setdefault(f['anchor_line'], []).append(f)
            for i in sorted(by_line, reverse=True):
                group = sorted(by_line[i], key=lambda x: x['figure_id'])
                want = group[0].get('anchor_sha256')
                got = (hashlib.sha256(norm_text(lines[i]).encode('utf-8'))
                       .hexdigest() if i < len(lines) else None)
                if want and got != want:
                    # line drift after earlier insertions — re-find the
                    # anchor line within a window before giving up
                    j = next((k for k in range(max(0, i - 40),
                                               min(len(lines), i + 41))
                              if hashlib.sha256(
                                  norm_text(lines[k]).encode('utf-8'))
                              .hexdigest() == want), None)
                    if j is None:
                        # stored index may be badly stale (corpus edited
                        # since the registry build) — accept a whole-file
                        # match only when the anchor text is unique
                        hits = [k for k, ln in enumerate(lines)
                                if hashlib.sha256(
                                    norm_text(ln).encode('utf-8'))
                                .hexdigest() == want]
                        if len(hits) == 1:
                            j = hits[0]
                    if j is None:
                        report['anchor_mismatch'].extend(
                            f['figure_id'] for f in group)
                        continue
                    i = j
                before = group[0].get('anchor_mode') == 'before'
                pos = i if before else i + 1
                # idempotent, per figure: if this figure's ref was already
                # inserted near the anchor (a rebuilt registry may re-bind
                # a few lines away), skip it instead of duplicating
                window = lines[max(0, pos - 6):pos + 7]
                ref_covered = set()
                mark_covered = set()
                for ln in window:
                    m = FIGS_TAG_RX.search(ln)
                    if not m:
                        continue
                    ids = m.group(1).split(',')
                    if ln.lstrip().startswith('!['):
                        ref_covered.update(ids)
                    else:
                        mark_covered.update(ids)
                n_refs = n_marks = 0
                block = []
                pending_mark = []
                for f in group:
                    fid = f['figure_id']
                    if f['publication_status'] in ('published', 'duplicate') \
                            and f.get('_corpus_media'):
                        relp = Path(os.path.relpath(
                            f['_corpus_media'], sp.parent)).as_posix()
                        if fid in ref_covered or \
                                any(f'({relp})' in ln or
                                    f'({PurePosixPath(f["_corpus_media"])
                                             .name})'
                                    in ln for ln in window):
                            # ref already present — repair a stale/dirty alt
                            # (e.g. a caption that was polluted before the
                            # marker filter was added)
                            alt = (f.get('caption') or
                                   f.get('figure_label') or
                                   f.get('original_filename') or
                                   'иллюстрация')
                            alt = re.sub(r'[\[\]]', '', alt)
                            alt = re.sub(r'([a-z]+\d+)(?=[А-Яа-яЁё])', '',
                                         alt)
                            alt = re.sub(r'^([a-z]+\d+\s*)+', '', alt)
                            alt = (' '.join(alt.split())[:120]
                                   or 'иллюстрация')
                            for wi, wl in enumerate(lines):
                                if fid not in wl or \
                                        not wl.lstrip().startswith('!['):
                                    continue
                                m = re.match(
                                    r'!\[[^\]]*\]\(([^)]*)\)(<!--.*-->)?',
                                    wl.strip())
                                if m and Path(m.group(1)).name == \
                                        Path(relp).name:
                                    nl = (f'![{alt}]({m.group(1)})'
                                          f'{m.group(2) or ""}')
                                    if nl != wl.strip():
                                        lines[wi] = nl
                                        report.setdefault(
                                            'alt_repaired', 0)
                                        report['alt_repaired'] += 1
                                break
                            continue
                        alt = (f.get('caption') or f.get('figure_label') or
                               f.get('original_filename') or 'иллюстрация')
                        # brackets inside alt break the ![..](..) syntax;
                        # strip VML-style junk tokens leaking from docx
                        # context and keep the alt short
                        alt = re.sub(r'[\[\]]', '', alt)
                        alt = re.sub(r'([a-z]+\d+)(?=[А-Яа-яЁё])', '', alt)
                        alt = re.sub(r'^([a-z]+\d+\s*)+', '', alt)
                        alt = ' '.join(alt.split())[:120] or 'иллюстрация'
                        ref = f'![{alt}]({relp})<!-- figs:{fid} -->'
                        # upgrade an omission marker already placed at this
                        # anchor for this figure (markers carry figs: ids;
                        # legacy markers without a tag belong to this anchor)
                        upgraded = False
                        for wi in range(pos, min(len(lines), pos + 6)):
                            ln = lines[wi].lstrip()
                            if not ln.startswith(
                                    '*[В оригинальном издании здесь '
                                    'расположена иллюстрац'):
                                continue
                            m = FIGS_TAG_RX.search(ln)
                            ids = (m.group(1).split(',') if m
                                   else [fid])
                            if fid not in ids:
                                continue  # marker belongs to other figures
                            rest = [x for x in ids if x != fid]
                            if rest:
                                lines[wi] = (FIGS_TAG_RX.sub(
                                    '', lines[wi]).rstrip() +
                                    f' <!-- figs:{",".join(rest)} -->')
                            else:
                                lines[wi] = ref
                            upgraded = True
                            break
                        if not upgraded:
                            block.append(ref)
                        n_refs += 1
                    else:
                        if fid in mark_covered:
                            continue
                        pending_mark.append(f)
                if pending_mark:
                    ids = ','.join(f['figure_id'] for f in pending_mark)
                    lab = next((f' «{f["figure_label"]}»'
                                for f in pending_mark
                                if f.get('figure_label')), '')
                    n_marks = len(pending_mark)
                    if n_marks == 1:
                        block.append(OMIT_MARKER.format(lab=lab) +
                                     f' <!-- figs:{ids} -->')
                    else:
                        block.append(OMIT_GROUP_MARKER.format(
                            n=n_marks) + f' <!-- figs:{ids} -->')
                if not block:
                    report['inserted_refs'] += n_refs
                    report['inserted_markers'] += n_marks
                    continue
                # exact-block check also covers whole-group re-runs
                if lines[pos:pos + len(block)] == block or \
                        lines[pos + 1:pos + 1 + len(block)] == block:
                    continue
                lines[pos:pos] = ['', *block, '']
                report['inserted_refs'] += n_refs
                report['inserted_markers'] += n_marks
            sp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        # 4) per-work docs
        reg_dir = root / 'data' if msub == MEDIA_SUBDIR_TOM else root
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / 'figures_registry.jsonl').write_text(
            '\n'.join(json.dumps({k: v for k, v in f.items()
                                  if not k.startswith('_')},
                                 ensure_ascii=False)
                      for f in wfigs) + '\n', encoding='utf-8')
        fmd = root / 'FIGURES.md'
        if not fmd.is_file():
            fmd.write_text(figures_md(wid, wfigs), encoding='utf-8')

    if a.owner_doc:
        by_kind = {}
        for f in figs:
            key = (f['publication_status'] if f['publication_status'] in
                   ('metadata_only_rights', 'metadata_only_source')
                   else f['source_kind'])
            by_kind.setdefault(key, []).append(f)
        a.owner_doc.write_text(owner_actions_md(by_kind, figs),
                               encoding='utf-8')
    for k in ('copied', 'inserted_refs', 'inserted_markers',
              'rewritten_refs'):
        print(f'{k}: {report[k]}')
    print('anchor_mismatch:', len(report['anchor_mismatch']))
    print('anchor_review:', len(report.get('anchor_review') or []))
    print('missing_staged:', len(report['missing_staged']))
    print('tom_missing_disk:', len(report['tom_missing_disk']))
    if a.report:
        rep = {k: (v if isinstance(v, int) else v[:50])
               for k, v in report.items()}
        a.report.write_text(json.dumps(rep, ensure_ascii=False, indent=2) +
                            '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
