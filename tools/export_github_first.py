#!/usr/bin/env python3
"""github-first export: assemble a public GitHub repository whose *content* is
the corpus itself — full authorial Markdown sections browsable with native
GitHub rendering, no static site and no Pages required.

Layout produced (all links are relative .md paths that GitHub renders
natively):

    README.md            library overview, honest composition, how to read/cite
    TOPICS.md            editorial topic index (not authorial text)
    NOTICE.md            provenance: source editions + sha256, limits, © notice
    LICENSE              MIT — applies to repository CODE only, not the texts
    tom-N/README.md      volume table of contents in authorial order
    tom-N/<section>.md   authorial sections, private frontmatter rewritten as a
                         public provenance note; known media placeholders become
                         visible omission markers
    data/*.jsonl         machine indexes (already privacy-clean)
    tools/ converter/ tests/ .github/workflows/   MIT code (reproducibility)

Two modes:
  candidate — builds the file set marked "release candidate", does NOT
              require approvals (review artifact only). Its bytes differ
              from a public build in exactly two places: the README banner
              and the `mode` field of EXPORT_MANIFEST.json. Therefore the
              candidate manifest is NOT the exact manifest of the public
              package — after real approval a separate `--mode public`
              export must be built and re-hashed before any push;
  public    — requires release_gate.check_release to pass first (fail closed).
Neither mode edits approvals, and neither performs any push/deploy.

Manifest contract: EXPORT_MANIFEST.json lists sha256 of every exported file
except itself (written after hashing — recursive self-hashing is impossible).
So: manifest_paths == files_on_disk - {EXPORT_MANIFEST.json}.
"""
from __future__ import annotations
import argparse, hashlib, json, re, shutil, sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent))
from scan_public_output import scan_tree          # noqa: E402
from site_builder import (RUBRICS, header_removed, load_media_allowlist,
                          load_release_scope)    # noqa: E402
from release_gate import check_release            # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# MIT-licensed code/tooling copied verbatim; texts are added separately and
# are NOT covered by that license. export/* release docs stay internal.
CODE_FILES = ['requirements.txt', '.gitignore', 'AGENTS.md']
CODE_DIRS = ['tools', 'converter', 'tests', '.github/workflows']
SKIP_NAMES = {'__pycache__'}
# publish.yml is a manual GitHub Pages deploy of the static site; a book-only
# repository without a site must not carry a workflow that suggests one.
SKIP_FILES = {'publish.yml'}

FRONTMATTER_RX = re.compile(r'\A---\s*\n.*?\n---\s*\n', re.S)
MD_IMG_RX = re.compile(r'!\[([^\]]*)\]\([^)]*assets/media/([\w.\-]+)[^)]*\)')
MD_MEDIA_LINK_RX = re.compile(r'(?<!!)\[([^\]]*)\]\([^)]*assets/media/([\w.\-]+)[^)]*\)')
MD_LINK_RX = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)\s]+)\)')

OMISSION = ('*[В оригинальном издании здесь расположен рисунок/схема{alt}. '
            'Иллюстрация не включена в данный выпуск: ожидает визуальной '
            'и правовой проверки. Файл источника: assets/media/{name}]*')

ARCHIVE_NOTE = 'соответствие редакции этой копии источнику проверяется'


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(2 ** 20), b''):
            h.update(chunk)
    return h.hexdigest()


def rel_link(from_file: PurePosixPath, to_path: str) -> str:
    """Proper relative POSIX link from an exported file to an exported path."""
    fp = list(from_file.parent.parts)
    tp = list(PurePosixPath(to_path).parts)
    i = 0
    while i < len(fp) and i < len(tp) and fp[i] == tp[i]:
        i += 1
    return '/'.join(['..'] * (len(fp) - i) + tp[i:])


def sec_page(sec: dict):
    return sec.get('pdf_page_candidate') or sec.get('pdf_page_start')


def provenance_block(sec: dict, v: int, scope: dict, notice_rel: str) -> str:
    page = sec_page(sec)
    page_txt = (f'Страница издания: стр. {page} PDF — кандидатная привязка, '
                'требует сверки' if page else 'страница издания не привязана')
    pdf_name = f'osnovy-sociologii-tom-{v}.pdf'
    if scope['pdfs'] == 'reference' and scope['canonical_pdf_url']:
        src = (f'[внешний архив оригинальных публикаций]({scope["canonical_pdf_url"]}) '
               f'— выбрать том; файл издания `{pdf_name}`; {ARCHIVE_NOTE}')
    else:
        src = f'файл издания `{pdf_name}` (PDF в выпуск не включён)'
    lines = [
        f'> **Первичный текст.** «Основы социологии», том {v}. {page_txt}.',
        f'> Источник: {src}. Контрольные суммы и статус проверки — в [NOTICE.md]({notice_rel}).',
        '> Содержание передаёт позицию авторов издания; утверждения не верифицированы библиотекой.',
    ]
    return '\n'.join(lines)


def transform_section(corpus: Path, sec: dict, v: int, scope: dict, allow: set,
                      exported: set, omitted: list, neutralized: list) -> str:
    """Authorial text is preserved verbatim except: private frontmatter is
    replaced by a public provenance note, media references become explicit
    omission markers (or are kept for allowlisted files), and relative links
    pointing at files that are not exported lose their link but keep text."""
    src = corpus / f'tom-{v}' / sec['path']
    body = header_removed(src.read_text(encoding='utf-8')).replace('\xad', '')

    def img_repl(m):
        alt, name = m.group(1), m.group(2)
        if scope['media'] == 'allowlist' and (v, name) in allow:
            return m.group(0)
        omitted.append(f'tom-{v}:{sec["id"]}:{name}')
        alt_txt = f' «{alt}»' if alt else ''
        return OMISSION.format(alt=alt_txt, name=name)

    def link_repl(m):
        text, name = m.group(1), m.group(2)
        if scope['media'] == 'allowlist' and (v, name) in allow:
            return m.group(0)
        omitted.append(f'tom-{v}:{sec["id"]}:{name}')
        return text + ' ' + OMISSION.format(alt='', name=name)

    body = MD_IMG_RX.sub(img_repl, body)
    body = MD_MEDIA_LINK_RX.sub(link_repl, body)

    def link_fix(m):
        text, target = m.group(1), m.group(2)
        if '://' in target or target.startswith(('#', 'mailto:', 'data:')):
            return m.group(0)
        path_part = unquote(target.split('#')[0].split('?')[0])
        if not path_part:
            return m.group(0)
        parts = []
        for p in (PurePosixPath(sec['path']).parent / path_part).parts:
            if p == '..':
                if not parts:
                    neutralized.append(f'tom-{v}/{sec["path"]} -> {target}')
                    return text
                parts.pop()
            elif p != '.':
                parts.append(p)
        norm = f'tom-{v}/' + '/'.join(parts)
        if norm in exported:
            return m.group(0)
        neutralized.append(f'tom-{v}/{sec["path"]} -> {target}')
        return text

    body = MD_LINK_RX.sub(link_fix, body)

    file_rel = PurePosixPath(f'tom-{v}') / sec['path']
    toc_rel = rel_link(file_rel, f'tom-{v}/README.md')

    out = [provenance_block(sec, v, scope, rel_link(file_rel, 'NOTICE.md')),
           '', '---', '']
    # Authorial heading structure is trusted: inject a title heading only when
    # the body carries none at all (never duplicate an existing ## heading).
    if not re.search(r'(?m)^#{1,6}\s', body):
        out.append(f'# {sec["title"]}\n')
    out.append(body.rstrip() + '\n')

    prev_p = sec.get('previous_path') or sec.get('previous')
    next_p = sec.get('next_path') or sec.get('next')
    nav = ['---', '']
    links = []
    if prev_p and prev_p != 'None':
        links.append(f'[← Назад]({rel_link(file_rel, "tom-" + str(v) + "/" + prev_p)})')
    links.append(f'[Оглавление тома {v}]({toc_rel})')
    if next_p and next_p != 'None':
        links.append(f'[Вперёд →]({rel_link(file_rel, "tom-" + str(v) + "/" + next_p)})')
    nav.append(' · '.join(links) + '\n')
    return '\n'.join(out) + '\n'.join(nav)


def notice_section_link(volumes_secs: dict, v: int = 1) -> str:
    """The authorial © notice lives in the opening section of each volume;
    link to whatever opening/front file is actually exported."""
    secs = volumes_secs.get(v) or []
    opening = next((s for s in secs if s.get('kind') in ('opening', 'front')),
                   secs[0] if secs else None)
    return f'tom-{v}/{opening["path"]}' if opening else f'tom-{v}/README.md'


def build_readme(scope: dict, volumes_secs: dict, mode: str, media_q: int,
                 ole_q: int, inline_omitted: int) -> str:
    banner = ('> **Кандидат выпуска — не для публикации.** Состав файлов '
              'соответствует будущему публичному репозиторию; статус ревью '
              'проверяется отдельно.\n\n' if mode == 'candidate' else '')
    vols = '\n'.join(f'- [Том {v}](tom-{v}/README.md) — {len(volumes_secs[v])} разделов'
                     for v in sorted(volumes_secs))
    pdf_line = ('PDF в выпуск не включены; разделы ссылаются на [внешний архив '
                f'оригинальных публикаций]({scope["canonical_pdf_url"]}) '
                f'({ARCHIVE_NOTE}); номера страниц помечены как кандидаты.'
                if scope['pdfs'] == 'reference' else
                'PDF в выпуск не включены; указаны имена файлов и кандидатные страницы.')
    return f'''# «Основы социологии» — библиотека первоисточников

{banner}Исследовательская библиотека первичных текстов: постановочные материалы учебного курса «Основы социологии» ВП СССР в шести томах. Изложенные в работах взгляды являются позицией их авторов; библиотека не утверждает их истинность.

Библиотека — исследовательский корпус первоисточников: тексты воспроизводятся без изменения их содержательного смысла. Редакционные элементы — навигация, каталоги, индексы, provenance и технические пометки — отделены от авторского текста и не являются его частью. Цель библиотеки — обеспечить доступ к первоисточникам для исследования, поиска и цитирования с сохранением контекста.

## Состав выпуска (текстовая редакция)

Включены полные авторские тексты разделов. Иллюстрации и обложки **не включены**: на {inline_omitted} местах, известных разметке, стоят явные пометки об исключении. Ещё {media_q} записей ревью медиа и {ole_q} встроенных OLE-объектов исходных DOC **не привязаны к позиции в тексте и отсутствуют без индивидуальной пометки** — это ограничение выпуска, а не полная текстово-графическая редакция издания. {pdf_line}

## Оглавление

{vols}

[Тематический указатель](TOPICS.md) — редакционная навигация по реальным заголовкам разделов (не авторский текст).

## Как пользоваться

- **Читать:** откройте `README.md` нужного тома и переходите по ссылкам разделов — весь текст читается прямо в браузере GitHub.
- **Скачать:** `Code → Download ZIP` либо `git clone`.
- **Цитировать:** укажите том и заголовок раздела и приложите постоянную ссылку на файл раздела в этом репозитории (адрес вида `…/blob/main/tom-N/….md`); номера страниц издания в заголовках разделов — кандидатные, сверяйте с оригиналом.
- **Проверить происхождение:** см. [NOTICE.md](NOTICE.md) — контрольные суммы исходных DOC/PDF и статус проверки. Машинные индексы — в [`data/`](data/works.jsonl).

## Правовой статус

Авторское уведомление, напечатанное в начале каждого тома (например, [титульная часть тома 1]({notice_section_link(volumes_secs)})), предоставляет полное право копировать и тиражировать материалы — в полном объёме или фрагментарно, в том числе с коммерческими целями — и отдельно возлагает на использующего персональную ответственность за искажение смысла. Это авторское разрешение на копирование, а **не** перевод в public domain и не лицензия MIT/CC; точный текст уведомления сохранён в авторских файлах без изменений. Права на сторонние произведения (обложки, репродукции) остаются у их правообладателей; такие материалы в выпуск не включены. `LICENSE` (MIT) относится только к коду этого репозитория и не распространяется на тексты.
'''


def _esc_title(t: str) -> str:
    return t.replace('[^', '\\[^')


def build_volume_readme(v: int, secs: list, scope: dict) -> str:
    items = '\n'.join(f'{i}. [{_esc_title(s["title"])}]({s["path"]})'
                      for i, s in enumerate(secs, 1))
    pdf = f'osnovy-sociologii-tom-{v}.pdf'
    return f'''# «Основы социологии» — Том {v}

> Оглавление в авторском порядке чтения. Файл издания `{pdf}` (в выпуск не включён). Страницы PDF в разделах — кандидатные привязки. [Вся библиотека](../README.md) · [Тематический указатель](../TOPICS.md)

{items}
'''


def build_topics(all_secs: list) -> str | None:
    used = set()
    blocks = []
    for label, terms in RUBRICS:
        hits = [s for s in all_secs
                if s.get('kind') != 'frontmatter'
                and any(t in s['title'].lower() for t in terms)
                and s['id'] not in used]
        if not hits:
            continue
        for s in hits:
            used.add(s['id'])
        items = '\n'.join(
            f'- [{_esc_title(s["title"])}](tom-{s["volume"]}/{s["path"]}) · том {s["volume"]}'
            for s in hits[:15])
        blocks.append(f'## {label}\n\n{items}\n')
    head = ('# Темы и разделы\n\n> **Редакционная навигация, а не авторский текст.** '
            'Рубрики выведены из реальных заголовков разделов источника; каждая ссылка '
            'ведёт на полный раздел с контекстом. Включение в рубрику не утверждает '
            'истинность содержания раздела.\n\n')
    if not blocks:
        return head + ('*В данном объёме выпуска ни одна редакционная рубрика не '
                       'подтверждена заголовками разделов.*\n')
    return head + '\n'.join(blocks)


def build_notice(volumes_secs: dict, works: list, scope: dict,
                 media_q: int, ole_q: int, inline_omitted: int,
                 neutralized: list, max_md: int) -> str:
    rows = []
    for w in sorted(works, key=lambda x: x['volume']):
        if w['volume'] not in volumes_secs:
            continue
        rows.append(
            f'| {w["volume"]} | `osnovy-sociologii-tom-{w["volume"]}.doc` '
            f'| `{w.get("doc_sha256", "?")}` | `osnovy-sociologii-tom-{w["volume"]}.pdf` '
            f'({w.get("pdf_pages", "?")} стр.) | `{w.get("pdf_sha256", "?")}` |')
    table = ('| Том | Исходный DOC | DOC sha256 | Исходный PDF | PDF sha256 |\n'
             '|---|---|---|---|---|\n' + '\n'.join(rows))
    neut = ('\n'.join(f'- `{x}`' for x in sorted(set(neutralized))) or '- нет')
    return f'''# Происхождение и статус проверки

## Источники

{table}

Контрольные суммы относятся к файлам конкретной редакции источника; внешний архив оригинальных публикаций: {scope['canonical_pdf_url'] or 'не задан'} ({ARCHIVE_NOTE}).

## Транскрипция

DOC → DOCX → Markdown выполнена автоматически (детерминированный конвейер в `converter/`); визуальная сверка с изданием не завершена. Привязка страниц PDF — кандидатная (`auto_*`/`approximate_*`), если иное не указано человеком-проверяющим.

## Ограничения выпуска

- {inline_omitted} известных мест иллюстраций заменены явными пометками об исключении.
- {media_q} записей очереди ревью медиа и {ole_q} встроенных OLE-объектов не привязаны к позиции в тексте — отсутствуют без индивидуальной пометки.
- Исходные DOC/PDF и графика в выпуск не входят.
- Самый большой файл раздела: {max_md} байт — в пределах лимита отображения Markdown на GitHub; разбивка не потребовалась.
- Относительные ссылки источника, ведущие на невключённые файлы, раскрыты в текст без URL (список ниже) — содержимое не выдумывалось.

{neut}

## Права

Дословный текст авторского уведомления (титульная часть каждого тома, напр. [{notice_section_link(volumes_secs)}]({notice_section_link(volumes_secs)})):

> © Публикуемые материалы являются достоянием Русской культуры, по какой причине никто не обладает в отношении них персональными авторскими правами. В случае *присвоения себе в установленном законом порядке* авторских прав юридическим или физическим лицом, совершивший это столкнется с воздаянием за воровство, выражающемся в неприятной “мистике”, выходящей за пределы юриспруденции. Тем не менее, каждый желающий имеет полное право, исходя из свойственного ему понимания *общественной пользы*, копировать и тиражировать, *в том числе с коммерческими целями*, настоящие материалы в полном объёме или фрагментарно всеми доступными ему средствами. Использующий настоящие материалы в своей деятельности, при фрагментарном их цитировании, либо же при ссылках на них, принимает на себя персональную ответственность, и в случае порождения им смыслового контекста, извращающего смысл *настоящих материалов, как целостности*, он имеет шансы столкнуться с “мистическим”, внеюридическим воздаянием.

Точный текст авторского уведомления напечатан в начале каждого тома (например, [{notice_section_link(volumes_secs)}]({notice_section_link(volumes_secs)})) и не изменён: оно предоставляет полное право копировать и тиражировать материалы, в том числе с коммерческими целями, и возлагает на использующего персональную ответственность за искажение смысла при фрагментарном цитировании и ссылках. Это разрешение на копирование, а не public domain и не MIT/CC0/CC BY. Обложки и репродукции третьих лиц — отдельные права, в выпуск не включены. Код репозитория — MIT (см. LICENSE), лицензия кода на тексты не распространяется.
'''


def audit_md_links(out: Path):
    """Every relative markdown link/image target must resolve to an exported
    file. Returns sorted 'file -> target' misses."""
    missing = []
    for f in sorted(out.rglob('*.md')):
        text = f.read_text(encoding='utf-8')
        for m in re.finditer(r'!?\[[^\]]*\]\(([^)\s]+)\)', text):
            tgt = m.group(1)
            if '://' in tgt or tgt.startswith(('#', 'mailto:', 'data:')):
                continue
            p = unquote(tgt.split('#')[0].split('?')[0])
            if not p:
                continue
            if not (f.parent / p).resolve().exists():
                missing.append(f'{f.relative_to(out).as_posix()} -> {tgt}')
    return sorted(set(missing))


def check_output_path(out: Path, corpus: Path, review_dir: Path | None):
    """Fail before shutil.rmtree: the staging dir must not equal, contain or
    be contained by the repo, the corpus or the private review dir."""
    o = out.resolve()
    if len(o.parts) < 3:
        raise SystemExit(f'Refusing unsafe --output (too close to filesystem root): {o}')
    protected = [REPO, corpus.resolve()]
    if review_dir:
        protected.append(review_dir.resolve())
    for p in protected:
        if o == p or p.is_relative_to(o) or o.is_relative_to(p):
            raise SystemExit(f'Refusing --output overlapping protected path {p}: {o}')


def export(corpus: Path, out: Path, approval: Path, mode: str,
           review_dir: Path | None, forbid=()):
    check_output_path(out, corpus, review_dir)
    if mode == 'public':
        ok, reasons, _ = check_release(corpus, approval)
        if not ok:
            raise SystemExit('PUBLIC EXPORT BLOCKED:\n' + '\n'.join('- ' + r for r in reasons))
    scope = load_release_scope(approval)
    allow = load_media_allowlist(
        approval.parent / 'public_media_allowlist.jsonl') if approval else {}

    # Review-queue counts are disclosed honestly in README/NOTICE; queues
    # themselves are never exported.
    def queue_len(name):
        p = (review_dir / name) if review_dir else None
        if not p or not p.is_file():
            return 0
        return sum(1 for l in p.read_text(encoding='utf-8').splitlines() if l.strip())

    media_q = queue_len('media_review_queue.jsonl')
    ole_q = queue_len('ole_review_queue.jsonl')

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Pass 1: decide the exported file set so links can be audited exactly.
    volumes_secs = {}
    exported = {'README.md', 'TOPICS.md', 'NOTICE.md', 'LICENSE', 'AGENTS.md'}
    for v in scope['volumes']:
        book = corpus / f'tom-{v}'
        sj = book / 'data/sections.jsonl'
        if not sj.is_file():
            raise SystemExit(f'tom-{v}: sections.jsonl missing')
        secs = [json.loads(l) for l in sj.read_text(encoding='utf-8').splitlines() if l.strip()]
        secs.sort(key=lambda s: s.get('reading_order', 0))
        for s in secs:
            if isinstance(s.get('title'), str):
                s['title'] = s['title'].replace('\xad', '')
            src = book / s['path']
            if not src.is_file():
                raise SystemExit(f'tom-{v}: section file missing: {s["path"]}')
            exported.add(f'tom-{v}/{s["path"]}')
        exported.add(f'tom-{v}/README.md')
        volumes_secs[v] = secs
        if scope['media'] == 'allowlist':
            for f in (book / 'assets/media').rglob('*'):
                if f.is_file() and (v, f.name) in allow:
                    exported.add(f'tom-{v}/assets/media/{f.name}')

    # Pass 2: write transformed sections + navigation.
    omitted, neutralized, copied_media, manifest = [], [], [], []
    for v, secs in volumes_secs.items():
        for sec in secs:
            rel = f'tom-{v}/{sec["path"]}'
            text = transform_section(corpus, sec, v, scope, allow, exported,
                                     omitted, neutralized)
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding='utf-8')
        (out / f'tom-{v}/README.md').write_text(
            build_volume_readme(v, secs, scope), encoding='utf-8')
        if scope['media'] == 'allowlist':
            for f in (corpus / f'tom-{v}/assets/media').rglob('*'):
                if not f.is_file():
                    continue
                want = allow.get((v, f.name))
                if want and sha256(f) == want:
                    dest = out / f'tom-{v}/assets/media/{f.name}'
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    copied_media.append(f'tom-{v}/{f.name}')
                elif want:
                    raise SystemExit(f'tom-{v}: allowlisted media bytes differ: {f.name}')

    # Data indexes (top-level jsonl are already privacy-verified): filtered to
    # the declared volumes so no out-of-scope metadata leaks.
    (out / 'data').mkdir(exist_ok=True)
    for name in ('works.jsonl', 'sections.jsonl', 'chunks.jsonl'):
        src = corpus / 'data' / name
        if not src.is_file():
            continue
        rows = [json.loads(l) for l in src.read_text(encoding='utf-8').splitlines() if l.strip()]
        vol_ids = {s['id'] for secs in volumes_secs.values() for s in secs}
        keep = [r for r in rows
                if (r.get('volume') in volumes_secs
                    or r.get('section_id') in vol_ids
                    or r.get('id') in vol_ids)]
        (out / 'data' / name).write_text(
            ''.join(json.dumps(r, ensure_ascii=False).replace('\xad', '') + '\n'
                    for r in keep),
            encoding='utf-8')
    works = [json.loads(l) for l in (corpus / 'data/works.jsonl')
             .read_text(encoding='utf-8').splitlines() if l.strip()]

    all_secs = [dict(s, volume=v) for v, secs in volumes_secs.items() for s in secs]
    (out / 'TOPICS.md').write_text(build_topics(all_secs), encoding='utf-8')

    max_md = max((out / f'tom-{v}' / s['path']).stat().st_size
                 for v, secs in volumes_secs.items() for s in secs)
    (out / 'README.md').write_text(
        build_readme(scope, volumes_secs, mode, media_q, ole_q, len(set(omitted))),
        encoding='utf-8')
    (out / 'NOTICE.md').write_text(
        build_notice(volumes_secs, works, scope, media_q, ole_q,
                     len(set(omitted)), neutralized, max_md), encoding='utf-8')
    shutil.copy2(REPO / 'export/LICENSE', out / 'LICENSE')
    for rel in CODE_FILES:
        src = REPO / rel
        if src.is_file():
            shutil.copy2(src, out / rel)
    for d in CODE_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob('*')):
            if (p.is_file() and not (set(p.parts) & SKIP_NAMES)
                    and p.name not in SKIP_FILES):
                dest = out / p.relative_to(REPO)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)

    missing = audit_md_links(out)
    problems, _ = scan_tree(out, strict_review_refs=False, forbid=forbid)
    manifest = [{'path': p.relative_to(out).as_posix(), 'bytes': p.stat().st_size,
                 'sha256': sha256(p)}
                for p in sorted(out.rglob('*')) if p.is_file()]
    (out / 'EXPORT_MANIFEST.json').write_text(
        json.dumps({'mode': mode, 'scope': scope, 'files': manifest,
                    'self_exclusion': 'EXPORT_MANIFEST.json is the only file '
                                      'not listed in its own index (written '
                                      'after hashing).'},
                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    report = {'mode': mode, 'output': str(out),
              'files_hashed': len(manifest),
              'files_on_disk': len(manifest) + 1,
              'candidate_public_diff': ([] if mode != 'candidate' else
                                        ['README.md candidate banner',
                                         'EXPORT_MANIFEST.json mode field']),
              'volumes': sorted(volumes_secs),
              'sections': sum(len(s) for s in volumes_secs.values()),
              'media_omitted_inline': sorted(set(omitted)),
              'media_copied': copied_media,
              'links_neutralized': sorted(set(neutralized)),
              'missing_md_links': missing,
              'max_section_md_bytes': max_md,
              'media_queue_entries': media_q, 'ole_queue_entries': ole_q,
              'forbidden_content': problems,
              'verdict': 'CLEAN' if not problems and not missing else 'HAS-DEFECTS'}
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 8 else f'{len(v)} entries')
                      for k, v in report.items()}, ensure_ascii=False, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, required=True)
    ap.add_argument('--approval', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--mode', choices=['candidate', 'public'], default='candidate')
    ap.add_argument('--review-dir', type=Path, default=REPO / 'review',
                    help='private review dir for queue COUNTS only (never exported)')
    ap.add_argument('--forbid', action='append', default=[],
                    help='extra forbidden substring (e.g. operator login)')
    ap.add_argument('--report', type=Path, default=None)
    a = ap.parse_args()
    report = export(a.corpus, a.output, a.approval, a.mode, a.review_dir,
                    forbid=tuple(a.forbid))
    if a.report:
        a.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
    return 0 if report['verdict'] == 'CLEAN' else 1


if __name__ == '__main__':
    sys.exit(main())
