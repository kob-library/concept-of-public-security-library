#!/usr/bin/env python3
"""Issue #9 unified-library export: extends the github-first export of the
six «Основы социологии» volumes with additional works collections
(books/, analytics/, collections/) from the unified private corpus.

Runs export_github_first.export() for tom-1..6 first, then adds:

    <collection>/<work_id>/README.md     work index (title, source, provenance)
    <collection>/<work_id>/<NN>.md       section files (authorial text)
    CATALOG.md                           full works list by collection/series
    CHRONOLOGY.md                        works by year/date
    data/works.jsonl / data/sections.jsonl  merged indexes

Same transformations as volume sections: U+00AD stripped, unextracted media
become visible omission markers, links to non-exported files are neutralized
(text kept), private identifiers (Drive ids, local paths) never exported.
Authorial text is not edited.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent))
import export_github_first as gh  # noqa: E402

MOJI_MEDIA_RX = re.compile(r'!\[([^\]]*)\]\([^)]*\)')          # any image ref
IMG_TAG_RX = re.compile(r'<img\b[^>]*>', re.I)                 # pandoc/LO <img>
IMG_SRC_RX = re.compile(r'src=["\']([^"\']+)["\']', re.I)
MD_LINK_RX = gh.MD_LINK_RX
FN_DEF_RX = re.compile(r'(?m)^\[\^(\d+)\]:[^\S\n]*(.*?)(?=^\[\^\d+\]:|\Z)',
                       re.S)
FN_REF_RX = re.compile(r'\[\^(\d+)\](?!:)')

OMISSION_WORK = ('*[В оригинальном издании здесь расположен рисунок/схема{alt}. '
                 'Иллюстрация не включена в данный выпуск: ожидает '
                 'визуальной и правовой проверки. Файл источника: {name}]*')

COLLECTION_TITLE = {
    'books': 'Книги и самостоятельные работы',
    'analytics': 'Аналитические записки',
    'collections': 'Тематические подборки',
}

SOFT_HYPHEN = '\xad'

# Public-representation masking (owner decision): personal e-mail addresses of
# quoted private individuals are masked in the PUBLIC build only; the private
# source corpus is never modified. Organizational/imprint addresses stay
# verbatim (see EMAIL_DECISION_LIST.md for the full policy table).
_AT = chr(64)  # keep e-mail literals out of shipped source (scanner-clean)
PUBLIC_MASKS = {
    'arb' + _AT + 'usa.net': 'a…' + _AT + 'usa.net',
}

# Masking applies to authorial/index content only — never to shipped tooling.
MASK_PATH_RX = re.compile(r'^(books|analytics|collections|tom-\d+|translations|data)/'
                          r'|\.md$')

# Native endonym labels for LANGUAGES.md / work-page blocks. Editorial
# navigation labels only — no translation is generated anywhere.
LANG_NAMES = {
    'ar': 'العربية', 'cs': 'Čeština', 'de': 'Deutsch', 'en': 'English',
    'es': 'Español', 'fa': 'فارسی', 'fi': 'Suomi', 'fr': 'Français',
    'it': 'Italiano', 'kk': 'Қазақша', 'lt': 'Lietuvių', 'ro': 'Română',
    'sk': 'Slovenský', 'zh': '汉语', 'ru': 'Русский',
}

MATCH_STATUS_RU = {
    'exact_work_match': 'точное совпадение с работой корпуса',
    'exact_edition_match': 'та же редакция, что в корпусе',
    'exact_collection_match': 'перевод всего комплекта работ',
    'work_match_edition_unknown': 'работа найдена; редакция не подтверждена',
    'older_or_alternate_edition': 'другая редакция оригинала',
    'partial_translation': 'перевод соответствует части работы корпуса',
    'translation_only_no_ru_match': 'соответствующей работы в корпусе нет',
    'needs_review': 'требует ручной проверки соответствия',
}


def load_extra(extra: Path):
    works = [json.loads(l) for l in (extra / 'data_works.jsonl')
             .read_text(encoding='utf-8').splitlines() if l.strip()]
    excl_path = extra / 'excluded_works.json'
    excluded = {}
    if excl_path.exists():
        for e in json.loads(excl_path.read_text(encoding='utf-8')):
            excluded[e['work_id']] = e
    works = [w for w in works if w['work_id'] not in excluded]
    load_extra.excluded = excluded
    secs = {s['work_id']: [] for s in
            (json.loads(l) for l in (extra / 'data_sections.jsonl')
             .read_text(encoding='utf-8').splitlines() if l.strip())}
    for l in (extra / 'data_sections.jsonl').read_text(encoding='utf-8').splitlines():
        if l.strip():
            s = json.loads(l)
            secs.setdefault(s['work_id'], []).append(s)
    for w in works:
        w.setdefault('collection', 'works')
        secs.get(w['work_id'], []).sort(key=lambda s: s.get('reading_order', 0))
    # strip soft hyphens from index fields so they never reach any output
    for w in works:
        for k in ('title', 'title_doc'):
            if isinstance(w.get(k), str):
                w[k] = w[k].replace(SOFT_HYPHEN, '')
    for wsecs in secs.values():
        for s in wsecs:
            if isinstance(s.get('title'), str):
                t = s['title']
                if IMG_TAG_RX.search(t):
                    t = re.sub(r'\s+/\s*$', '',
                               re.sub(r'\s+', ' ', IMG_TAG_RX.sub('', t))
                               .strip())
                s['title'] = t.replace(SOFT_HYPHEN, '')
    return works, secs


def work_provenance(w: dict, notice_rel: str) -> str:
    src = (f'`{w["source_name"]}` sha256 `{w["sha256_source"][:16]}…`'
           if w.get('sha256_source') else 'источник не указан')
    lines = [
        f'> **Первичный текст.** {w["title"]}'
        + (f' — редакция {w["edition"]} г.' if w.get('edition') else '')
        + (f', {w["date"]}' if w.get('date') else '') + '.',
        f'> Источник: {src}'
        + (f'; [страница на dotu.ru]({w["dotu_url"]})' if w.get('dotu_url') else '') + '.',
        '> Автоматическая транскрипция DOC→Markdown; смысл и формулировки не правились.',
        f'> Контрольные суммы и статус проверки — в [NOTICE.md]({notice_rel}).',
        '> Содержание передаёт позицию авторов; утверждения не верифицированы библиотекой.',
    ]
    return '\n'.join(lines)


def work_footnote_defs(extra: Path, wsecs: list) -> dict:
    """footnote number -> def text, collected across all sections of a work
    (pandoc emits all defs at document end; after section splitting a section
    file can reference defs that live in a sibling file)."""
    defs = {}
    for s in wsecs:
        src = extra / s['path']
        body = gh.header_removed(
            src.read_text(encoding='utf-8')).replace(SOFT_HYPHEN, '')
        for m in FN_DEF_RX.finditer(body):
            defs.setdefault(m.group(1), m.group(0).rstrip())
    return defs


def transform_work_section(extra: Path, w: dict, s: dict, exported: set,
                           omitted: list, neutralized: list,
                           wdefs: dict = None) -> str:
    src = extra / s['path']
    body = gh.header_removed(src.read_text(encoding='utf-8')).replace(SOFT_HYPHEN, '')
    wdir = w['dir']

    # replicate footnote definitions referenced here but defined in a sibling
    # section (pandoc emits all defs at document end; GitHub renders footnotes
    # only when def and ref share a file). Injected BEFORE media/link
    # sanitization so defs go through the same transformations.
    if wdefs:
        local = {m.group(1) for m in FN_DEF_RX.finditer(body)}
        need = sorted({m.group(1) for m in FN_REF_RX.finditer(body)} - local,
                      key=lambda x: int(x))
        if need:
            body = body.rstrip() + '\n\n' + '\n\n'.join(wdefs[n] for n in need
                                                      if n in wdefs) + '\n'

    def img_repl(m):
        alt = m.group(1)
        ref = m.group(0)
        name = ref.split('(', 1)[1].rsplit(')', 1)[0].split('/')[-1]
        omitted.append(f'{wdir}:{s["section_id"]}:{name}')
        alt_txt = f' «{alt}»' if alt else ''
        return OMISSION_WORK.format(alt=alt_txt, name=name)

    def img_tag_repl(m):
        # pandoc/LibreOffice emit raw <img>; alt may carry local paths —
        # keep only the src basename, never the alt text
        src = IMG_SRC_RX.search(m.group(0))
        name = src.group(1).rsplit('/', 1)[-1] if src else 'image'
        omitted.append(f'{wdir}:{s["section_id"]}:{name}')
        return OMISSION_WORK.format(alt='', name=name)

    body = MOJI_MEDIA_RX.sub(img_repl, body)
    body = IMG_TAG_RX.sub(img_tag_repl, body)

    def link_fix(m):
        text, target = m.group(1), m.group(2)
        # local file links (file:/// or absolute drive paths, incl. paths
        # materialized by LO/Word resolving relative links at conversion)
        if (target.lower().startswith('file:')
                or re.match(r'^[A-Za-z]:[\\/]', unquote(target))):
            neutralized.append(f'{s["path"]} -> local-file-link')
            return text
        if '://' in target or target.startswith(('#', 'mailto:', 'data:')):
            return m.group(0)
        path_part = unquote(target.split('#')[0].split('?')[0])
        if not path_part:
            return m.group(0)
        parts = []
        for p in (PurePosixPath(s['path']).parent / path_part).parts:
            if p == '..':
                if not parts:
                    neutralized.append(f'{s["path"]} -> {target}')
                    return text
                parts.pop()
            elif p != '.':
                parts.append(p)
        if '/'.join(parts) in exported:
            return m.group(0)
        neutralized.append(f'{s["path"]} -> {target}')
        return text

    body = MD_LINK_RX.sub(link_fix, body)

    file_rel = PurePosixPath(s['path'])
    out = [work_provenance(w, gh.rel_link(file_rel, 'NOTICE.md')), '', '---', '']
    if not re.search(r'(?m)^#{1,6}\s', body):
        out.append(f'# {s["title"]}\n')
    out.append(body.rstrip() + '\n')
    links = [f'[К произведению]({gh.rel_link(file_rel, wdir + "/README.md")})',
             f'[Каталог]({gh.rel_link(file_rel, "CATALOG.md")})']
    return '\n'.join(out) + '\n'.join(['---', '', ' · '.join(links) + '\n'])


META_ONLY_STATUSES = {'partial_damage'}

# Obvious wholesale republications of standalone third-party material found
# by the pre-release structural check (owner rule: ship as metadata only —
# the full text stays out of the public build; the private corpus is not
# modified). External references where confirmed are on the work card.
THIRD_PARTY_REPUBLICATION = {
    'письмо-добренькова-путину',
    'послание-президента-фс-рф-10-05-2006',
    'послание-федеральному-собранию-2004',
    'выступление-и-дискуссия-на-мюнхенской-конференции',
    'стенографический-отчет-о-пресс-конференции',
    'правда-ру-о-событиях-в-москве',
}


def is_meta_only(w: dict) -> bool:
    return (w.get('text_status') in META_ONLY_STATUSES
            or w.get('work_id') in THIRD_PARTY_REPUBLICATION)


def meta_only_reason(w: dict) -> tuple:
    if w.get('work_id') in THIRD_PARTY_REPUBLICATION:
        return ('third_party_republication',
                '> **Текст не включён в данный выпуск.** Самостоятельный\n'
                '> материал третьих лиц (не произведение ВП СССР), в исходном\n'
                '> собрании присутствует как справочный контекст. Сохранена\n'
                '> только карточка метаданных.')
    return ('partial_damage',
            '> **Текст не включён в данный выпуск.** Исходный файл повреждён\n'
            '> при создании (утрачены отдельные символы, U+FFFD); надёжного\n'
            '> источника для восстановления не найдено. Сторонний материал —\n'
            '> не произведение ВП СССР. Сохранена только карточка.')


def build_meta_only_readme(w: dict) -> str:
    """Card for a work whose full text is deliberately not shipped."""
    src = (f'`{w["source_name"]}` sha256 `{w["sha256_source"]}`'
           if w.get('sha256_source') else 'источник не указан')
    ext = (f'Внешний источник: [dotu.ru]({w["dotu_url"]}).'
           if w.get('dotu_url') else 'Подтверждённая внешняя ссылка отсутствует.')
    status, reason = meta_only_reason(w)
    return (f'# {w["title"]}\n\n'
            f'{reason}\n\n'
            f'- Статус: `{w.get("text_status")}` / `{status}` (metadata only)\n'
            f'- Источник: {src}\n'
            f'- {ext}\n\n'
            f'[Каталог](../{"../" * (w["dir"].count("/"))}CATALOG.md)\n')


def build_work_readme(w: dict, secs: list) -> str:
    if is_meta_only(w):
        return build_meta_only_readme(w)
    items = '\n'.join(
        f'{i}. [{s["title"].replace("[^", "\\[^")}]'
        f'({PurePosixPath(s["path"]).name})'
        for i, s in enumerate(secs, 1)) or '(один файл)'
    meta = []
    if w.get('edition'):
        meta.append(f'редакция {w["edition"]} г.')
    if w.get('date'):
        meta.append(w['date'])
    if w.get('dotu_url'):
        meta.append(f'[dotu.ru]({w["dotu_url"]})')
    metatxt = (' — ' + '; '.join(meta)) if meta else ''
    return (f'# {w["title"]}{metatxt}\n\n'
            f'> Авторское содержание. Разделов: {len(secs)}. '
            f'[Каталог](../{'../' * (w["dir"].count("/"))}CATALOG.md)\n\n'
            f'{items}\n')


AI_USAGE = '''# Использование библиотеки ИИ-агентами

Короткий маршрут — читать не все произведения, а только нужные разделы:

1. `CATALOG.md` или `data/works.jsonl` → найдите `work_id` произведения
   (по `title`, году, серии).
2. `data/sections.jsonl` → список разделов работы (`section_id`, `path`,
   `title`, `reading_order`).
3. Откройте файл раздела `path` — полный контекст, сноски и ссылки
   «к произведению/каталогу» работают без JS.
4. Для поиска по всему корпусу используйте полнотекстовый поиск по `.md`
   (на GitHub — поиск по репозиторию), затем откройте сам раздел, а не
   пересказ.
5. Переводы (вторичный слой, источник — dotu.ru): `LANGUAGES.md` или
   `data/translations.jsonl` → `translation_id` → `translations/<lang>/<id>/`;
   соответствие оригиналу — поле `match`/`related_work_ids`, каноническим
   остаётся русский текст. Разделы переводов — `data/translation_sections.jsonl`.

## Правила цитирования

- Первичный текст — только авторское содержимое файлов разделов; `CATALOG.md`,
  `CHRONOLOGY.md`, `TOPICS.md`, `README.md` произведений — редакционная
  навигация, а не текст авторов.
- Индексы в `data/` — слой-указатель, а не источник цитаты.
- Указывайте произведение, раздел и редакцию; номера страниц PDF в заголовках
  разделов томов — кандидатные, сверяйте с изданием.
- Авторские политические, религиозные и научные утверждения — позиция авторов,
  а не верифицированный библиотекой факт; повторное цитирование одного тезиса
  в разных работах — не независимое подтверждение.
- Не выдавайте собственные пояснения и пересказы за текст первоисточника.

## Примеры задач

- **Найти работу:** `works.jsonl` → `title` содержит запрос → `work_id` →
  `dir/README.md` произведения.
- **Найти понятие:** поиск термина по `.md` → список разделов → читать
  раздел целиком.
- **Проследить понятие во времени:** те же разделы + `year`/`date` работы из
  `works.jsonl` → `CHRONOLOGY.md`.
- **Источник цитаты:** поиск фразы → раздел → provenance-блок в начале файла.
- **Упоминание vs само произведение:** название встречается в других работах —
  сравните `work_id` хита с работой, чей `title` совпадает с запросом.
- **Честный отказ:** если термин не находится в `sections.jsonl` и тексте
  разделов — ответ «в корпусе не найдено», не домысливать.

Ограничения выпуска (исключённые иллюстрации, повреждённые фрагменты,
статус проверки) — в `NOTICE.md`.
'''


VOL_META = {
    1: ('Основы социологии. Том 1', '2013'),
    2: ('Основы социологии. Том 2', '2016'),
    3: ('Основы социологии. Том 3', '2016'),
    4: ('Основы социологии. Том 4', '2016'),
    5: ('Основы социологии. Том 5', '2016'),
    6: ('Основы социологии. Том 6', '2016'),
}


def build_catalog(works: list) -> str:
    blocks = ['# Каталог произведений', '',
              'Редакционный указатель (не авторский текст). Статусы проверки — в NOTICE.md.', '',
              '## Основы социологии (6 томов)', '']
    for v in sorted(VOL_META):
        title, year = VOL_META[v]
        blocks.append(f'- [{title}](tom-{v}/README.md) — {year}')
    blocks.append('')
    by_col = {}
    for w in works:
        by_col.setdefault(w['collection'], []).append(w)
    for col in ('books', 'analytics', 'collections'):
        ws = by_col.get(col)
        if not ws:
            continue
        blocks.append(f'## {COLLECTION_TITLE.get(col, col)} ({len(ws)})')
        blocks.append('')
        for w in sorted(ws, key=lambda x: (x.get('date') or '', x['title'])):
            dt = (w.get('date') or '')[:4]
            ed = f", ред. {w['edition']}" if w.get('edition') else ''
            link = f'{w["dir"]}/README.md'
            dotu = f' · [dotu.ru]({w["dotu_url"]})' if w.get('dotu_url') else ''
            note = ''
            if is_meta_only(w):
                note = (' · *полный текст не включён (сторонний материал)*'
                        if w['work_id'] in THIRD_PARTY_REPUBLICATION else
                        ' · *полный текст не включён (повреждён)*')
            blocks.append(f'- [{w["title"]}]({link}) — {dt or "год не установлен"}{ed}{dotu}{note}')
        blocks.append('')
    return '\n'.join(blocks)


def build_chronology(works: list) -> str:
    blocks = ['# Хронология произведений', '',
              'Даты — по имени файла/титульным данным источника; не доказывают дату '
              'создания. Редакционная навигация, не авторский текст.', '']
    by_year = {}
    for v, (title, year) in VOL_META.items():
        by_year.setdefault(year, []).append(
            {'title': title, 'dir': f'tom-{v}'})
    for w in works:
        y = (w.get('date') or '')[:4] or (w.get('year') or '') or 'н/д'
        by_year.setdefault(y, []).append(w)
    for y in sorted(by_year):
        blocks.append(f'## {y}')
        blocks.append('')
        for w in sorted(by_year[y], key=lambda x: x['title']):
            note = (' · *полный текст не включён*'
                    if is_meta_only(w) else '')
            blocks.append(f'- [{w["title"]}]({w["dir"]}/README.md){note}')
        blocks.append('')
    return '\n'.join(blocks)


def build_external_references(extra: Path) -> str | None:
    """Metadata-only cards for dotu-only records without a shipped text:
    needs_review + external_reference. These are pointers, not corpus texts."""
    p = extra / 'dotu_only_status.json'
    if not p.exists():
        return None
    recs = json.loads(p.read_text(encoding='utf-8'))
    show = [r for r in recs if r.get('final_status') in
            ('needs_review', 'external_reference')]
    if not show:
        return None
    out = ['# Произведения без полного текста (внешние ссылки)', '',
           'Редакционный список записей, для которых в библиотеке нет полного',
           'текста. Это карточки-указатели, а не произведения корпуса:', '',
           '- `needs_review` — материал обнаружен на dotu.ru, но полный текст',
           '  не включён: PDF без извлекаемого текстового слоя или неясное',
           '  авторство; требует ручного решения.',
           '- `external_reference` — сторонний материал, старшая редакция или',
           '  дубликат: полный текст не включён, сохранена ссылка.', '']
    for st, label in (('needs_review', 'Требуют ручного решения'),
                      ('external_reference', 'Внешние ссылки')):
        group = [r for r in show if r['final_status'] == st]
        if not group:
            continue
        out.append(f'## {label} ({len(group)})')
        out.append('')
        for r in group:
            note = f' — {r.get("status_note")}' if r.get('status_note') else ''
            sha = (f' · sha256 `{r["sha256"][:16]}…`' if r.get('sha256') else '')
            out.append(f'- [{r["title"]}]({r["url"]}){sha}{note}')
        out.append('')
    return '\n'.join(out)


def build_translations(out: Path, ledger_path: Path, tbuild: Path,
                       report: dict, extra_works: list) -> list:
    """Issue #11: additive multilingual layer sourced ONLY from translations
    published on dotu.ru. Emits translations/<lang>/<translation_id>/,
    data/translations.jsonl, data/translation_sections.jsonl, LANGUAGES.md,
    and «Переводы на dotu.ru» blocks on matched Russian work pages.
    Returns the registry rows (with dir fields)."""
    rows = [json.loads(l) for l in ledger_path
            .read_text(encoding='utf-8').splitlines() if l.strip()]
    # works_index is rebuilt locally: called before data/works.jsonl is merged
    # (extra works) and normalized (six volumes) later in extend().
    works_index = {w['work_id']: w for w in extra_works}
    for v in '123456':
        title, year = VOL_META.get(v, (f'Том {v}', None))
        works_index[f'osnovy-sociologii-tom-{v}'] = {
            'work_id': f'osnovy-sociologii-tom-{v}', 'title': title,
            'dir': f'tom-{v}', 'year': year}
    registry, sec_index = [], []
    by_work = {}
    for r in sorted(rows, key=lambda x: (x['lang'], x['translation_id'])):
        lang = r['lang']
        tid = r['translation_id']
        rel = f'translations/{lang}/{tid}'
        tdir = out / rel
        src_dir = tbuild / lang / tid
        wid = r.get('work_id')
        related = r.get('related_work_ids') or ([wid] if wid else [])
        full = r.get('full_text_status') == 'full' and src_dir.is_dir()
        meta_lines = [
            f'# {r["title"]}',
            '',
            f'> **Перевод с dotu.ru** — язык: {lang} '
            f'({LANG_NAMES.get(lang, lang)}).',
            '> Канонический русский текст — первоисточник; этот перевод — '
            'вторичный материал для поиска и чтения.',
            f'> Страница перевода: [{r["url"]}]({r["url"]})'
            + (f'; источник файла: [{r["source_url"]}]({r["source_url"]})'
               if r.get('source_url') else '') + '.',
        ]
        if r.get('sha256'):
            meta_lines.append(
                f'> sha256 источника: `{r["sha256"]}` (формат {r.get("fmt")}).')
        if r.get('ru_title') or r.get('ru_url'):
            meta_lines.append(
                '> Оригинал на dotu.ru: '
                + (f'«{r["ru_title"]}» ' if r.get('ru_title') else '')
                + (f'[{r["ru_url"]}]({r["ru_url"]}).' if r.get('ru_url') else ''))
        status_ru = MATCH_STATUS_RU.get(r['match'], r['match'])
        meta_lines.append(f'> Статус соответствия: `{r["match"]}` — {status_ru}.')
        if wid and wid in works_index:
            w = works_index[wid]
            meta_lines.append(
                f'> Перевод работы: [{w["title"]}](../../../{w["dir"]}/README.md)'
                f' (`{wid}`).')
        elif related:
            lst = ', '.join(
                f'[{works_index[x]["title"]}](../../../{works_index[x]["dir"]}/README.md)'
                for x in related if x in works_index)
            if r['match'] == 'exact_collection_match':
                meta_lines.append(
                    f'> Перевод всего комплекта работ: {lst}.')
            elif r['match'] == 'partial_translation':
                meta_lines.append(
                    f'> Перевод части работы корпуса: {lst}.')
            else:
                meta_lines.append(
                    f'> Возможные оригиналы в корпусе: {lst}.')
        elif r.get('confirmation') == 'NO_RU_MATCH_CONFIRMED':
            meta_lines.append(
                '> `NO_RU_MATCH_CONFIRMED`: соответствующей работы в корпусе '
                'нет — подтверждено ручной проверкой provenance.')
        else:
            meta_lines.append('> В корпусе соответствующей работы не найдено — '
                              'карточка перевода без канонической привязки.')
        for ev in (r.get('evidence') or []):
            meta_lines.append(f'> Основание: {ev}.')
        if r.get('review_note'):
            meta_lines.append(f'> Примечание проверки: {r["review_note"]}.')
        if r['match'] == 'translation_only_no_ru_match':
            if r.get('confirmation') != 'NO_RU_MATCH_CONFIRMED':
                meta_lines.append(
                    '> Раскрытие: соответствие с корпусом НЕ подтверждено; '
                    'считать перевод отдельным изданием до ручной проверки.')
        elif r['match'] in ('work_match_edition_unknown', 'needs_review'):
            meta_lines.append(
                '> Раскрытие: совпадение редакции с корпусом НЕ подтверждено; '
                'считать перевод отдельным изданием до ручной проверки.')
        if not full:
            reasons = {
                'download_failed': 'файл источника не удалось загрузить '
                                   'воспроизводимо',
                'no_source_file': 'на странице dotu.ru нет загружаемого '
                                  'файла перевода',
                'text_unavailable': 'текст источника не извлекается надёжно '
                                    'без OCR',
            }
            reason = reasons.get(r.get('full_text_status'),
                                 'полный текст недоступен')
            meta_lines += ['',
                           '**Полный текст перевода в библиотеке отсутствует** — '
                           f'{reason}. Карточка оставлена как указатель на '
                           'публикацию dotu.ru.']
        else:
            meta_lines += ['', '### Разделы', '']
            n = 0
            for s in r.get('reading_order', []):
                n += 1
                fn = s['file']
                tdir.mkdir(parents=True, exist_ok=True)
                body = (src_dir / fn).read_text(encoding='utf-8')
                # same public policy as the corpus: media never shipped
                body = MOJI_MEDIA_RX.sub(
                    lambda m: OMISSION_WORK.format(
                        alt=(' — ' + m.group(1)) if m.group(1) else '',
                        name=(m.group(0).split('(')[-1].rstrip(')')
                              .rsplit('/', 1)[-1])),
                    body)
                body = IMG_TAG_RX.sub(
                    lambda m: OMISSION_WORK.format(
                        alt='',
                        name=((sm := IMG_SRC_RX.search(m.group(0)))
                              and sm.group(1).rsplit('/', 1)[-1])
                             or 'image'),
                    body)
                # neutralize links to files that are not exported;
                # strip local-file links (file:///, absolute drive paths)
                # left in the source by the original publisher
                def _dead(m):
                    target = m.group(2)
                    if (target.lower().startswith('file:')
                            or re.match(r'^[A-Za-z]:[\\/]', unquote(target))):
                        return m.group(1)
                    if re.match(r'^[a-z]+:', target) or target.startswith('#'):
                        return m.group(0)
                    if not (tdir / unquote(target.split('#')[0])).exists():
                        return m.group(1)
                    return m.group(0)
                body = MD_LINK_RX.sub(_dead, body)
                (tdir / fn).write_text(body, encoding='utf-8')
                ttl = s['title']
                if IMG_TAG_RX.search(ttl):
                    ttl = re.sub(r'\s+/\s*$', '',
                                 re.sub(r'\s+', ' ',
                                        IMG_TAG_RX.sub('', ttl)).strip())
                meta_lines.append(f'{n}. [{ttl}]({fn})')
                sec_index.append({'translation_id': tid, 'section_id': f'{n:02d}',
                                  'path': f'{rel}/{fn}', 'title': ttl,
                                  'reading_order': n})
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / 'README.md').write_text('\n'.join(meta_lines) + '\n',
                                       encoding='utf-8')
        registry.append({
            'translation_id': tid, 'language': lang,
            'language_name': LANG_NAMES.get(lang, lang), 'title': r['title'],
            'translation_of': wid, 'related_work_ids': related or None,
            'translation_scope': r.get('translation_scope'),
            'confirmation': r.get('confirmation'),
            'review_note': r.get('review_note'),
            'translation_status': 'dotu_ru_published',
            'edition_relation': r['match'], 'match': r['match'],
            'date': r.get('date'), 'source_url': r['url'],
            'source_file': r.get('source_url'),
            'source_sha256': r.get('sha256'), 'source_format': r.get('fmt'),
            'all_source_formats': r.get('all_fmts'),
            'ru_original_url': r.get('ru_url'), 'ru_title': r.get('ru_title'),
            'dir': rel, 'full_text_status': r.get('full_text_status'),
            'reading_order': r.get('reading_order'),
            'sections_count': r.get('sections_count'),
            'provenance_status': 'dotu_page+file_sha256' if r.get('sha256')
                                 else 'dotu_page_only',
            'evidence': r.get('evidence'),
        })
        for x in related:
            by_work.setdefault(x, []).append(registry[-1])

    (out / 'data' / 'translations.jsonl').write_text(
        ''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in registry),
        encoding='utf-8')
    if sec_index:
        (out / 'data' / 'translation_sections.jsonl').write_text(
            ''.join(json.dumps(x, ensure_ascii=False) + '\n'
                    for x in sec_index), encoding='utf-8')

    # «Переводы на dotu.ru» block on matched Russian work pages
    patched = 0
    for wid, trs in by_work.items():
        w = works_index.get(wid)
        if not w:
            continue
        wrp = out / w['dir'] / 'README.md'
        if not wrp.exists():
            continue
        up = '../' * (w['dir'].count('/') + 1)
        lines = ['', '## Переводы на dotu.ru', '']
        for t in trs:
            if t['match'] in ('exact_work_match', 'exact_edition_match'):
                st = ''
            elif t['match'] == 'exact_collection_match':
                st = ' — перевод всего комплекта'
            else:
                st = f' — {t["match"]}'
            lines.append(f'- {t["language_name"]} — '
                         f'[{t["title"]}]({up}{t["dir"]}/README.md){st}')
        lines += ['', '> Переводы — вторичный материал; канонический текст — '
                      'русский оригинал этой работы.']
        wrp.write_text(wrp.read_text(encoding='utf-8').rstrip()
                       + '\n' + '\n'.join(lines) + '\n', encoding='utf-8')
        patched += 1

    # LANGUAGES.md
    langs = {}
    for t in registry:
        langs.setdefault(t['language'], []).append(t)
    lg = ['# Переводы на других языках (источник: dotu.ru)', '',
          'Все тексты ниже — переводы, опубликованные на dotu.ru. Библиотека '
          'ничего не переводит сама: если перевода на язык нет на dotu.ru, '
          'его нет и здесь (`NO_TRANSLATION_AVAILABLE`). Каноническим текстом '
          'остаётся русский оригинал.', '']
    for lang in sorted(langs, key=lambda l: (-len(langs[l]), l)):
        trs = langs[lang]
        lg.append(f'## {LANG_NAMES.get(lang, lang)} (`{lang}`) — {len(trs)}')
        lg.append('')
        lg.append('| Перевод | Оригинал | Статус | Текст |')
        lg.append('|---|---|---|---|')
        for t in trs:
            rel = t.get('related_work_ids') or []
            if t['translation_of'] in works_index:
                orig = (f'[{works_index[t["translation_of"]]["title"]}]'
                        f'({works_index[t["translation_of"]]["dir"]}/README.md)')
            elif t['match'] == 'exact_collection_match' and rel:
                orig = f'комплект из {len(rel)} работ'
            elif rel:
                orig = (f'[{works_index[rel[0]]["title"]}]'
                        f'({works_index[rel[0]]["dir"]}/README.md)'
                        if rel[0] in works_index else
                        (t.get('ru_title') or '—'))
            else:
                orig = t.get('ru_title') or '—'
            st = MATCH_STATUS_RU.get(t['match'], t['match'])
            lg.append(f'| [{t["title"]}]({t["dir"]}/README.md) '
                      f'| {orig} | {st} '
                      f'| {"полный" if t["full_text_status"] == "full" else "карточка"} |')
        lg.append('')
    (out / 'LANGUAGES.md').write_text('\n'.join(lg), encoding='utf-8')

    report['translations'] = {
        'entries': len(registry), 'languages': len(langs),
        'full_text': sum(1 for t in registry
                         if t['full_text_status'] == 'full'),
        'cards': sum(1 for t in registry
                     if t['full_text_status'] != 'full'),
        'matched_works': patched,
        'by_match': {m: sum(1 for t in registry if t['match'] == m)
                     for m in sorted({t['match'] for t in registry})},
    }
    return registry


def extend(out: Path, extra: Path, report: dict,
           tr_ledger: Path | None = None, tr_build: Path | None = None) -> dict:
    works, secs = load_extra(extra)
    if not works:
        return report
    # pass 1: exported set for link audit (meta-only works ship just a card —
    # links to their section files must be neutralized, so paths stay absent)
    exported = {p.as_posix() for p in out.rglob('*') if p.is_file()}
    for w in works:
        exported.add(f'{w["dir"]}/README.md')
        if is_meta_only(w):
            continue
        for s in secs.get(w['work_id'], []):
            exported.add(s['path'])

    omitted, neutralized = [], []
    total_secs = 0
    fn_replicated = 0
    for w in works:
        wsecs = secs.get(w['work_id'], [])
        wdefs = work_footnote_defs(extra, wsecs)
        wdir = out / w['dir']
        wdir.mkdir(parents=True, exist_ok=True)
        for s in wsecs:
            if is_meta_only(w):
                continue
            before_defs = 0
            src_body = gh.header_removed(
                (extra / s['path']).read_text(encoding='utf-8'))
            local = {m.group(1) for m in FN_DEF_RX.finditer(src_body)}
            need = {m.group(1) for m in FN_REF_RX.finditer(src_body)} - local
            fn_replicated += len(need & set(wdefs))
            text = transform_work_section(extra, w, s, exported, omitted,
                                          neutralized, wdefs)
            (wdir / PurePosixPath(s['path']).name).write_text(text, encoding='utf-8')
            total_secs += 1
        (wdir / 'README.md').write_text(build_work_readme(w, wsecs), encoding='utf-8')

    (out / 'CATALOG.md').write_text(build_catalog(works), encoding='utf-8')
    (out / 'CHRONOLOGY.md').write_text(build_chronology(works), encoding='utf-8')
    (out / 'AI_USAGE.md').write_text(AI_USAGE, encoding='utf-8')
    ext_ref = build_external_references(extra)
    if ext_ref:
        (out / 'EXTERNAL_REFERENCES.md').write_text(ext_ref, encoding='utf-8')

    tr_registry = []
    if tr_ledger and tr_build and tr_ledger.exists() and tr_build.exists():
        tr_registry = build_translations(out, tr_ledger, tr_build, report, works)

    # NOTICE: provenance/exclusions for the extra corpus
    meta_n = sum(1 for w in works if is_meta_only(w))
    notice = out / 'NOTICE.md'
    nbody = notice.read_text(encoding='utf-8').rstrip() + f'''

## Дополнительные произведения (books/analytics/collections)

Источник каждой работы — имя файла и sha256 в `data/works.jsonl` и в
provenance-блоке её README/разделов; `dotu_url` — указатель на страницу
публикации (не подтверждение идентичности редакции, `dotu_match: auto`).
{meta_n} работ включены только карточкой `metadata only`: часть — исходные
тексты повреждены в источнике и надёжной замены не найдено, часть —
самостоятельные материалы третьих лиц (справочный контекст собрания), не
публикуемые целиком (список в `CATALOG.md`, пометка «полный текст не
включён»). Записи без полного текста
— в `EXTERNAL_REFERENCES.md`. Графика из дополнительных произведений
исключена так же, как из томов: явные пометки в местах известной разметки.
Один персональный e-mail цитируемого частного автора маскирован в публичном
представлении (`a…@usa.net`); организационные/издательские адреса в выходных
данных и ссылках на источники сохранены дословно. Приватный исходный корпус
не изменён — маскирование выполняется только на этапе экспорта, журнал
замен фиксируется в отчёте экспорта.
'''
    if tr_registry:
        nbody += f'''
## Переводы (слой dotu.ru)

{len(tr_registry)} переводов на {report.get('translations', {}).get('languages')} языков
— только тексты, реально опубликованные на dotu.ru; никакой машинный или
самостоятельный перевод не добавлялся и не генерировался. Канонический
текст — русский оригинал; переводы — вторичный справочный слой. Для каждого
перевода в `data/translations.jsonl` зафиксированы страница-источник, файл
источника и его sha256; статус соответствия редакции оригиналу указан явно,
неподтверждённые совпадения не повышались. Переводы без надёжно извлекаемого
текста представлены карточками-указателями (`{report.get('translations', {}).get('cards')}`
карточек). Навигация — `LANGUAGES.md`.
'''
    notice.write_text(nbody, encoding='utf-8')

    # merge public-safe index records (no drive ids, no private paths)
    # Six-volume records are normalized to the same unified schema
    # (work_id/title/kind/dir/sections/year) while keeping volume fields.
    # Years/titles of volumes — from title pages, see review/ACCEPTANCE_DOSSIER.md.
    def pub_work(w):
        return {k: v for k, v in {
            'work_id': w['work_id'], 'collection': w['collection'],
            'title': w['title'], 'edition': w.get('edition'),
            'year': w.get('year'), 'date': w.get('date'),
            'series': w.get('series'), 'dotu_url': w.get('dotu_url'),
            'dir': w['dir'],
            'sections': 0 if is_meta_only(w) else w['sections'],
            'sha256_source': w.get('sha256_source'),
            'source_name': w.get('source_name'),
            'text_status': w.get('text_status'),
            'public_status': ('metadata_only' if is_meta_only(w) else None),
        }.items() if v is not None}

    def pub_sec(s):
        return {'work_id': s['work_id'], 'section_id': s['section_id'],
                'path': s['path'], 'title': s['title'].replace(SOFT_HYPHEN, ''),
                'reading_order': s['reading_order']}

    def norm_vol_work(r):
        v = r.get('volume')
        title, year = VOL_META.get(v, (f'Том {v}', None))
        n = sum(1 for l in (out / 'data' / 'sections.jsonl')
                .read_text(encoding='utf-8').splitlines()
                if l.strip() and json.loads(l).get('volume') == v)
        r.update({'work_id': f'osnovy-sociologii-tom-{v}', 'title': title,
                  'kind': 'volume', 'collection': 'osnovy-sociologii',
                  'dir': f'tom-{v}', 'sections': n, 'year': year,
                  'sha256_source': r.get('doc_sha256')})
        return r

    _sec_order = {}

    def norm_vol_sec(r):
        v = r.get('volume')
        _sec_order[v] = _sec_order.get(v, 0) + 1
        r.update({'work_id': f'osnovy-sociologii-tom-{v}',
                  'section_id': r.get('id'),
                  'reading_order': _sec_order[v]})
        return r

    vol_recs = {}
    for name in ('works.jsonl', 'sections.jsonl'):
        dp = out / 'data' / name
        if dp.exists():
            vol_recs[name] = [json.loads(l) for l in
                              dp.read_text(encoding='utf-8').splitlines()
                              if l.strip()]
    for name, recs, norm in (
            ('works.jsonl', [pub_work(w) for w in works], norm_vol_work),
            ('sections.jsonl',
             [pub_sec(s) for w in works if not is_meta_only(w)
              for s in secs.get(w['work_id'], [])], norm_vol_sec)):
        base = [norm(r) for r in vol_recs.get(name, [])]
        (out / 'data' / name).write_text(
            ''.join(json.dumps(r, ensure_ascii=False) + '\n'
                    for r in base + recs), encoding='utf-8')

    # README: append collections block; surface translations in the root TOC
    readme = out / 'README.md'
    body = readme.read_text(encoding='utf-8')
    if tr_registry:
        tr = report['translations']
        tl = '[Тематический указатель](TOPICS.md)'
        pos = body.find(tl)
        if pos >= 0:
            eol = body.find('\n', pos)
            body = (body[:eol] + '\n\n'
                    f'[Переводы на других языках](LANGUAGES.md) — '
                    f'{tr["entries"]} переводов на {tr["languages"]} языков '
                    '(источник: dotu.ru; вторичный слой, канонический текст — '
                    'русский оригинал).' + body[eol:])
    add = ['## Дополнительные произведения', '',
           f'- [Каталог произведений](CATALOG.md) — {len(works)} работ '
           f'({sum(1 for w in works if w["collection"] == "books")} книг, '
           f'{sum(1 for w in works if w["collection"] == "analytics")} аналитических записок и др.).',
           '- [Хронология](CHRONOLOGY.md) — по годам источников.',
           *(['- [Без полного текста](EXTERNAL_REFERENCES.md) — '
              'карточки-указатели на материалы, которых нет в библиотеке.']
             if ext_ref else []),
           *(['- [Переводы](LANGUAGES.md) — опубликованные на dotu.ru '
              'переводы произведений на других языках.']
             if tr_registry else []), '']
    readme.write_text(body.rstrip() + '\n\n' + '\n'.join(add), encoding='utf-8')

    # public-representation masking; logged in the export report
    masks = []
    for f in sorted(out.rglob('*')):
        if not f.is_file() or f.name == 'EXPORT_MANIFEST.json':
            continue
        rel = f.relative_to(out).as_posix()
        if not MASK_PATH_RX.search(rel):
            continue
        try:
            btext = f.read_text(encoding='utf-8')
        except (UnicodeDecodeError, ValueError):
            continue
        ntext, fhits = btext, []
        for src, dst in PUBLIC_MASKS.items():
            n = ntext.count(src)
            if n:
                ntext = ntext.replace(src, dst)
                fhits.append({'from': src, 'to': dst, 'count': n})
        if ntext != btext:
            f.write_text(ntext, encoding='utf-8')
            masks.append({'path': rel, 'hits': fhits})

    # re-audit + rescan + rebuild manifest
    missing = gh.audit_md_links(out)
    problems, _ = gh.scan_tree(out, strict_review_refs=False)
    # the manifest file itself trips FORBIDDEN_NAMES (written after hashing in
    # gh.export()); only its own filename finding is filtered, not content hits
    problems = [p for p in problems
                if p != 'internal file name in output: EXPORT_MANIFEST.json']
    # E-mail addresses/paths INSIDE authorial sections are verbatim citations
    # of third-party material (footnotes quoting sources) — disclosed, not
    # blocking. Anything in generated/index files stays blocking.
    authorial, blocking = [], []
    for p in problems:
        loc = p.split(' found in ', 1)[-1].split(': ', 1)[-1]
        if re.match(r'(books|analytics|collections|tom-\d|translations)/', loc):
            authorial.append(p)
        else:
            blocking.append(p)
    manifest = [{'path': p.relative_to(out).as_posix(), 'bytes': p.stat().st_size,
                 'sha256': gh.sha256(p)}
                for p in sorted(out.rglob('*')) if p.is_file()
                and p.name != 'EXPORT_MANIFEST.json']
    (out / 'EXPORT_MANIFEST.json').write_text(
        json.dumps({'mode': report.get('mode', 'candidate'),
                    'scope': report.get('scope', {}),
                    'files': manifest,
                    'self_exclusion': 'EXPORT_MANIFEST.json is the only file '
                                      'not listed in its own index.'},
                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report.update({
        'works_added': len(works), 'work_sections_added': total_secs,
        'excluded_works': getattr(load_extra, 'excluded', {}),
        'work_media_omitted': len(set(omitted)),
        'work_links_neutralized': len(set(neutralized)),
        'footnote_defs_replicated': fn_replicated,
        'files_hashed': len(manifest), 'files_on_disk': len(manifest) + 1,
        'missing_md_links': missing,
        'public_representation_masks': masks,
        'forbidden_content': blocking,
        'authorial_embedded_findings': authorial,
        'verdict': ('CLEAN' if not blocking and not missing
                    else 'HAS-DEFECTS')})
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, required=True)
    ap.add_argument('--extra-corpus', type=Path, required=True)
    ap.add_argument('--approval', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--mode', choices=['candidate', 'public'], default='candidate')
    ap.add_argument('--review-dir', type=Path, default=gh.REPO / 'review')
    ap.add_argument('--forbid', action='append', default=[])
    ap.add_argument('--report', type=Path, default=None)
    ap.add_argument('--translations-ledger', type=Path, default=None,
                    help='enriched dotu.ru translations ledger (jsonl)')
    ap.add_argument('--translations-build', type=Path, default=None,
                    help='converted translations tree <lang>/<id>/NN.md')
    a = ap.parse_args()
    report = gh.export(a.corpus, a.output, a.approval, a.mode, a.review_dir,
                       forbid=tuple(a.forbid))
    report = extend(a.output, a.extra_corpus, report,
                    a.translations_ledger, a.translations_build)
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 8
                          else f'{len(v)} entries')
                      for k, v in report.items()}, ensure_ascii=False, indent=2))
    if a.report:
        a.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
    return 0 if report['verdict'] == 'CLEAN' else 1


if __name__ == '__main__':
    raise SystemExit(main())
