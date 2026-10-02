#!/usr/bin/env python3
"""Assemble six-volume, internally checked scholarly corpus: t1 v0.3 + t2-t6 v0.4."""
from __future__ import annotations
import hashlib,json,shutil,zipfile
from pathlib import Path
ROOT=Path('/mnt/data')
OUT=ROOT/'kob_books_v0_4'
T1=ROOT/'kob_tom1_pilot_v0_3'
if not (OUT/'tom-1').exists():shutil.copytree(T1,OUT/'tom-1')
(OUT/'tools').mkdir(exist_ok=True)
for p in (ROOT/'kob_converter_v1').glob('*.py'):shutil.copy2(p,OUT/'tools'/p.name)
(OUT/'tools/README.md').write_text((ROOT/'kob_converter_v1/README.md').read_text(encoding='utf-8'),encoding='utf-8')
(OUT/'data').mkdir(exist_ok=True)

def sha(p):
 return hashlib.sha256(p.read_bytes()).hexdigest()

works=[];allchunks=[];allsections=[]
for n in range(1,7):
 d=OUT/f'tom-{n}'
 if n==1:
  p=d/'data/manifest.json';m=json.loads(p.read_text(encoding='utf-8'));r=json.loads((d/'data/sections.jsonl').read_text(encoding='utf-8').splitlines()[0]);
  doc=d/'source/osnovy-sociologii-tom-1.doc';pdf=d/'source/osnovy-sociologii-tom-1.pdf'
  info={'volume':n,'markdown_schema':'pilot_v0_3','doc_sha256':sha(doc),'pdf_sha256':sha(pdf),'pdf_pages':464,'status':'internal_automatic_with_partial_PDF_review'}
 else:
  m=json.loads((d/'data/manifest.json').read_text(encoding='utf-8'));info={'volume':n,'markdown_schema':'converter_v0_4','doc_sha256':m['doc_sha256'],'pdf_sha256':m['pdf_sha256'],'pdf_pages':m['pdf_pages'],'status':'internal_automatic_requires_visual_review'}
 info['root_path']=f'tom-{n}'
 info['original_doc_path']=f'tom-{n}/source/osnovy-sociologii-tom-{n}.doc'
 info['original_pdf_path']=f'tom-{n}/source/osnovy-sociologii-tom-{n}.pdf'
 info['toc_path']=f'tom-{n}/README.md'
 info['qa_path']=f'tom-{n}/QA_REPORT.md' if n==1 else f'tom-{n}/QA.md'
 works.append(info)
 if n==1:
  sc=[json.loads(x) for x in (d/'data/sections.jsonl').read_text(encoding='utf-8').splitlines()]
  ch=[json.loads(x) for x in (d/'data/chunks.jsonl').read_text(encoding='utf-8').splitlines()]
  for row in sc:
   allsections.append({'id':row['id'],'volume':n,'kind':row['kind'],'title':row['title'],'path':f'tom-1/{row["path"]}','pdf_page_candidate':row.get('pdf_page_start'),'pdf_page_status':row.get('pdf_locator_level'),'schema':'v0_3'})
  for row in ch:
   allchunks.append({'id':row['chunk_id'],'volume':n,'section_id':row['unit_id'],'path':f'tom-1/{row["source_section_path"]}','text':row['text'],
    'page_candidate':row.get('pdf_section_page_start'),'page_status':row.get('pdf_page_precision'),'schema':'v0_3'})
 else:
  for row in (json.loads(x) for x in (d/'data/sections.jsonl').read_text(encoding='utf-8').splitlines()):
   allsections.append({'id':row['id'],'volume':n,'kind':row['kind'],'title':row['title'],'path':f'tom-{n}/{row["path"]}',
    'pdf_page_candidate':row.get('pdf_page_candidate'),'pdf_page_status':row['page_mapping_status'],'schema':'v0_4'})
  for row in (json.loads(x) for x in (d/'data/chunks.jsonl').read_text(encoding='utf-8').splitlines()):
   allchunks.append({'id':row['id'],'volume':n,'section_id':row['section_id'],'path':f'tom-{n}/{row["source_path"]}',
    'text':row['text'],'page_candidate':row.get('pdf_page_candidate'),'page_status':'auto_candidate','schema':'v0_4'})

for name,rows in [('works',works),('sections',allsections),('chunks',allchunks)]:
 (OUT/'data'/f'{name}.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')

(OUT/'AGENTS.md').write_text('''# Для ИИ-агентов — индекс шести томов (внутренний пилот)

## Что искать

1. Пользовательская тема: `tom-1/topics/` (первый экспериментальный
   тематический маршрут) или полнотекстовый поиск по `data/chunks.jsonl`.
2. Точный контекст: найдите `section_id` в `data/sections.jsonl`, затем откройте
   соответствующий `tom-N/sections/`, `subsections/` либо `chapters/`.
3. Сноски, которые относятся к фрагменту, проверяйте в том же Markdown-файле
   или в `tom-N/data/footnotes.jsonl`.
4. Для цитат в окончательном ответе откройте исходный PDF и убедитесь, что
   нужная фраза находится именно на странице. Автоматический
   `pdf_page_candidate` — только гипотеза поиска, не подтверждённая ссылка.
5. Если смысл связан с изображением, начните с `tom-N/RECOVERED_FIGURES.md`
   и `tom-N/data/media_anchors.jsonl`: Pandoc сохранил в Markdown не все
   изображения. Страницы PDF — обязательный контроль визуального содержания.
6. Отдельно помечайте авторский тезис, исследовательскую реконструкцию и
   современные независимые сведения. Не переносите старые оценки в настоящее
   без новой проверки; не трактуйте отсутствие найденного упоминания как
   доказательство отсутствия темы.

## Ограничение

Тексты могут включать недоказанные, оспариваемые, дискриминационные,
религиозные и политические утверждения. Представляйте их как атрибутированные
источниковые позиции, а не как подтверждённые факты или рекомендации.
Не представляйте эту автоматическую транскрипцию как юридически и визуально
сверенную публикацию. Сохранены исходники и журналы QA.
''',encoding='utf-8')

num_vols=len(works);notes=322+340+512+341+536+605
missing=sum(json.loads((OUT/f'tom-{v}/data/qa.json').read_text(encoding='utf-8'))['omitted_inline_media_unique'] for v in range(2,7))
readme=['# «Основы социологии» — структурированный корпус (внутренний пилот v0.4.1)',
'', '**Статус: архив для дальнейшей проверки, НЕ завершённая публикация.**',
'', 'Сохранены 6 исходных томов (DOC и PDF), главы/разделы/подразделы в Markdown, оригинальные сноски и иллюстрации, JSONL для поиска и точного указания источника.',
'', f'- Томов: **{num_vols}**; индексируемых единиц: **{len(allsections)}**; поисковых фрагментов: **{len(allchunks)}**; определений сносок: **{notes}**.',
 f'- В томах 2–6 **{missing} уникальных DOCX-медиа ещё не вставлены непосредственно в разделы Markdown**. Их оригиналы и превью доступны в `assets/` и в `RECOVERED_FIGURES.md`; часть объектов Word требует ручного осмотра PDF.',
 '- Том 1 подготовлен более ранним конвертером v0.3 и имеет собственный набор проверок.',
 '', '## Читать оригинальный порядок','']
for w in works:readme.append(f'- [Том {w["volume"]} — оглавление](tom-{w["volume"]}/README.md) · [DOC]({w["original_doc_path"]}) · [PDF]({w["original_pdf_path"]}) · [QA]({w["qa_path"]})')
readme+=['', '## Навигация по исследовательским вопросам', '',
'- [Пример: социальное время и изменение технологий](tom-1/topics/social-time-technological-change.md). Это редакционная навигация, не авторский текст.',
'', '## Для агентов / RAG','',
'- [Правила чтения](AGENTS.md) · [Работы](data/works.jsonl) · [Структура](data/sections.jsonl) · [Текстовые фрагменты](data/chunks.jsonl).',
'- `chunk → section_id → исходный раздел → PDF` — основной путь проверки цитаты.',
'- Для томов 2–6 конвертер и инструкция: [tools/README.md](tools/README.md).',
'- Все ссылки на страницы томов 2–6 автоматически получены из текста PDF: перед публичным цитированием необходима визуальная сверка.',
'', '## Перед выпуском', '',
'1. Провести ручную приёмку неперенесённых изображений/таблиц и встроенных OLE, не удаляя оригиналы.',
'2. Убедиться, что DOC и PDF одного произведения действительно относятся к одной редакции; проверить неточности заголовков/сносок.',
'3. Проверить условия распространения конкретных редакций и контекст источников.',
'4. Отдельно оформить страницы для человека (GitHub Pages или иной статический сайт) и полнотекстовый индекс.',
'', 'Никакая публикация на GitHub, Google Диск или другие площадки этой сборкой не выполняется.']
(OUT/'README.md').write_text('\n'.join(readme)+'\n',encoding='utf-8')

(OUT/'RELEASE_STATUS.json').write_text(json.dumps({'release':'internal_v0.4.1','publication_ready':False,
 'volumes':len(works),'sections_index_count':len(allsections),'retrieval_chunks_count':len(allchunks),
 'footnotes_total':notes,'docx_images_not_inline_t2_t6':missing,'has_visual_or_legal_validation':False},ensure_ascii=False,indent=2))
print(json.dumps({'volume':len(works),'units':len(allsections),'chunks':len(allchunks),'footnotes':notes,'images_not_inline':missing},ensure_ascii=False))
