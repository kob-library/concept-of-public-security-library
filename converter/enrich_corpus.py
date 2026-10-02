#!/usr/bin/env python3
"""Improve PDF page candidates, recover DOCX-anchored (Pandoc-omitted) image locations.

This creates an *auxiliary evidence index*; it never invents locations or silently
inserts images into source prose. Locator status is ALWAYS automatic/provisional.
"""
from __future__ import annotations
import argparse, json, re, sys, zipfile, hashlib, unicodedata, collections
from pathlib import Path
from lxml import etree
import fitz,yaml

def norm(s):
    s=unicodedata.normalize('NFKC',s).casefold().replace('ё','е')
    return ''.join(c for c in s if c.isalnum())

def detag(s):
    s=re.sub(r'\[\^(\d+)\]','',s)
    s=re.sub(r'<[^>]+>','',s)
    s=re.sub(r'!\[[^]]*\]\([^)]*\)','',s)
    s=re.sub(r'\[([^]]+)\]\([^)]+\)',r'\1',s)
    return re.sub(r'[*_#`]+','',s)

def line_context(s):
    paras=re.split(r'\n\s*\n',s)
    result=[]
    for p in paras[1:15]:
        v=norm(detag(p))
        if len(v)>=58:result.append(v[:min(95,len(v))])
        if len(result)>=5:break
    return result

def match_pages(rows,pdf):
    texts=[norm(p.get_text(sort=True)) for p in pdf]
    # The first chapter has a content heading in the beginning of body and an
    # earlier TOC hit. Prefer title+body context to avoid mislabeling TOC pages.
    last=4;status_counter=collections.Counter()
    for row in rows:
        kind=row['kind']
        if kind not in ('chapter','appendix','section','subsection','preface'):
            row['pdf_page_candidate']=None
            row['page_mapping_status']='not_applicable';continue
        name=norm(row['title'])
        if len(name)<8:
            row['pdf_page_candidate']=None
            row['page_mapping_status']='title_too_short';status_counter['title_too_short']+=1;continue
        name=name[:95]
        contexts=line_context(row['text'])
        pages=[j for j in range(max(3,last-2),len(pdf)) if name in texts[j]]
        if not pages:
            row['pdf_page_candidate']=None
            row['page_mapping_status']='not_found';status_counter['not_found']+=1;continue
        withctx=[]
        for j in pages:
            hits=sum(any(k in texts[v] for v in range(j,min(j+2,len(pdf)))) for k in contexts)
            if hits:withctx.append((j,hits))
        if withctx:
            # Earliest candidate with at least one substantive continuation
            # snippet on that or the next page; later same-title running heads
            # should not take precedence.
            j=withctx[0][0]
            st='auto_heading_plus_context_candidate'
        else:
            # Ambiguous if first candidate before chapter start or not enough
            # context; prefer caution over invented precision.
            j=pages[0]
            st='auto_heading_only_candidate'
        row['pdf_page_candidate']=j+1
        row['page_mapping_status']=st
        last=max(last,j)
        status_counter[st]+=1
    return status_counter

NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
    'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'v':'urn:schemas-microsoft-com:vml'}

def docx_context(docx):
    with zipfile.ZipFile(docx) as z:
        relroot=etree.fromstring(z.read('word/_rels/document.xml.rels'))
        rels={x.attrib['Id']:Path(x.attrib.get('Target','')).name for x in relroot}
        root=etree.fromstring(z.read('word/document.xml'))
        para=root.xpath('.//w:p',namespaces=NS)
        txt=[''.join(x.xpath('.//w:t/text()',namespaces=NS)) for x in para]
        refs=[]
        for idx,x in enumerate(para):
            ids=x.xpath('.//a:blip/@r:embed',namespaces=NS)+x.xpath('.//v:imagedata/@r:id',namespaces=NS)
            # Different shapes can point to the same media within a single w:p.
            for name in dict.fromkeys(rels.get(r,'') for r in ids):
                if not name:continue
                refs.append({'name':name,'paragraph_index':idx,
                             'text':txt[idx],
                             'preceding_paragraph_text':txt[idx-1] if idx else '',
                             'following_paragraph_text':txt[idx+1] if idx<len(para)-1 else ''})
        return refs

def recover_mapping(rows,images,linked):
    hay=[norm(row['text']+'\n'+'\n'.join(str(z) for z in row['footnotes'].values())) for row in rows]
    outcome=[]
    for img in images:
        # Determine unique likely text unit from current / preceding / next DOCX
        # paragraph, not the original PDF page. Never call it PDF-confirmed.
        probes=[]
        for label,key in [('same_paragraph','text'),('preceding_paragraph','preceding_paragraph_text'),('following_paragraph','following_paragraph_text')]:
            needle=norm(img[key])
            if len(needle)>=40:
                probes.append((label,needle[:min(90,len(needle))]))
        choices=[]
        for label,needle in probes:
            candidates=[row['path'] for row,h in zip(rows,hay) if needle in h]
            if len(candidates)==1:
                choices.append((label,candidates[0]));break
        if choices:
            via,path=choices[0];st='docx_paragraph_context_unique_auto'
        else:
            via,path,st=None,None,'docx_paragraph_context_ambiguous_or_missing'
        original=img['name'];linked_before=original in linked
        suffix=Path(original).suffix.lower()
        prev=Path(original).with_suffix('.png').name if suffix in ['.wmf','.emf'] else original
        outcome.append({'image':original,'already_inline_in_markdown':linked_before,
            'suggested_unit_path':path,'location_method':via,'status':st,
            'docx_paragraph_index':img['paragraph_index'],
            'docx_anchor_text_excerpt':img['text'][:220],
            'docx_prev_text_excerpt':img['preceding_paragraph_text'][:150],
            'preview_path':'assets/media/'+prev,
            'original_path':'assets/media_original/'+original,
            'not_pdf_verified':True})
    return outcome

def main(root,workspace,volumes):
 for vol in volumes:
    folder=root/f'tom-{vol}'
    path=folder/'data/sections.jsonl'
    rows=[json.loads(x) for x in path.read_text(encoding='utf-8').splitlines()]
    pdfpath=folder/'source'/f'osnovy-sociologii-tom-{vol}.pdf'
    pdf=fitz.open(pdfpath)
    statuses=match_pages(rows,pdf)
    pdf.close()
    for row in rows:
        p=folder/row['path'];md=p.read_text(encoding='utf-8')
        m=re.match(r'\A---\n(.*?)\n---\n',md,re.S)
        if not m:raise ValueError('Missing YAML header '+str(p))
        meta=yaml.safe_load(m[1]);meta['pdf_page_1_based_candidate']=row['pdf_page_candidate'];meta['pdf_page_status']=row['page_mapping_status']
        p.write_text('---\n'+yaml.safe_dump(meta,allow_unicode=True,sort_keys=False,width=110).rstrip()+'\n---\n'+md[m.end():],encoding='utf-8')
    path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
    chunks_path=folder/'data/chunks.jsonl';chunks=[json.loads(x) for x in chunks_path.read_text(encoding='utf-8').splitlines()]
    by_id={x['id']:x for x in rows}
    for c in chunks:c['pdf_page_candidate']=by_id[c['section_id']]['pdf_page_candidate']
    chunks_path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in chunks),encoding='utf-8')
    fig_path=folder/'data/figures.jsonl';figures=[json.loads(x) for x in fig_path.read_text(encoding='utf-8').splitlines()]
    by_path={x['path']:x for x in rows}
    for f in figures:f['pdf_page_candidate']=by_path[f['unit_path']]['pdf_page_candidate']
    fig_path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in figures),encoding='utf-8')
    docx=workspace/'converted'/f'Основы социологии (том {vol}).docx'
    references=docx_context(docx)
    old_inline={f['media_name'] for f in figures}
    recovered=recover_mapping(rows,references,old_inline)
    rec_path=folder/'data/media_anchors.jsonl';rec_path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in recovered),encoding='utf-8')
    # Per-media overview deduplicates repeated Word XML placements while
    # retaining repeated anchors in the JSONL source of truth.
    missing=sorted({x['image'] for x in recovered if not x['already_inline_in_markdown']})
    byimage=collections.defaultdict(list)
    for x in recovered:byimage[x['image']].append(x)
    md=['# Иллюстрации, пропущенные автоматическим извлечением Pandoc','',
      '**Это указатель местоположений по XML DOCX, а не подтверждённая раскладка оригинала.** '+
      'PNG/JPEG уже сохранены, но картинки, пропущенные Pandoc, нельзя молча вставлять в произвольную точку текста. '+
      'Проверяйте их по PDF перед публикацией.','',
      f'Иллюстраций без встроенного отображения в Markdown: **{len(missing)}** (уникальных файлов).','']
    for name in missing:
        items=byimage[name]
        selected=next((x for x in items if x['suggested_unit_path']),items[0])
        dest=selected['suggested_unit_path']
        safe=(Path(name).with_suffix('.png').name if Path(name).suffix.lower() in ['.emf','.wmf'] else name)
        md.extend([f'## {name}','',
          f'![Файл из исходного Word — {name}](assets/media/{safe})','',
          f'- Возможный раздел: '+(f'[{dest}]({dest})' if dest else '**не установлен**')+'.',
          f'- Контекст из Word: {selected["docx_anchor_text_excerpt"][:120].replace(chr(10)," ") or "[изображение без текста в своём абзаце]"}',
          f'- Метод: `{selected["status"]}`.',
          '- **Положение на странице PDF не проверено.**',''])
    (folder/'RECOVERED_FIGURES.md').write_text('\n'.join(md),encoding='utf-8')
    qa_file=folder/'data/qa.json';qa=json.loads(qa_file.read_text(encoding='utf-8'))
    qa['page_mapping_statuses_v2']=dict(statuses)
    qa['image_anchor_mentions_docx']=len(recovered)
    qa['omitted_inline_media_unique']=len(missing)
    qa['omitted_inline_media_locatable_to_unit_unique']=sum(any(x['suggested_unit_path'] for x in byimage[n]) for n in missing)
    qa['omitted_inline_media_not_locatable_unique']=len(missing)-qa['omitted_inline_media_locatable_to_unit_unique']
    qa_file.write_text(json.dumps(qa,ensure_ascii=False,indent=2),encoding='utf-8')
    qa_md=folder/'QA.md'
    if '## Дополнительная сверка v0.4.1' not in qa_md.read_text(encoding='utf-8'):qa_md.write_text(qa_md.read_text(encoding='utf-8')+
      '\n## Дополнительная сверка v0.4.1 (без содержательной редакции)\n\n'+
      '- Улучшено сопоставление заголовков и первых абзацев с PDF (не ручная верификация).\n'+
      '- Сохранена карта медиавставок DOCX: `data/media_anchors.jsonl`.\n'+
      f'- Изображений, отсутствующих в теле Markdown: {len(missing)} уникальных; '+
      f'из них {qa["omitted_inline_media_locatable_to_unit_unique"]} имеют автоматически определённый раздел.\n'+
      '- Редакторский обзор: [RECOVERED_FIGURES.md](RECOVERED_FIGURES.md).\n'+
      '- Не следует считать совпадение абзаца с PDF доказательством визуальной полноты рисунка.\n',encoding='utf-8')
    rd=folder/'README.md'
    if '## Иллюстрации вне основного текста' not in rd.read_text(encoding='utf-8'):rd.write_text(rd.read_text(encoding='utf-8')+
      '\n## Иллюстрации вне основного текста\n\n'+
      f'[Индекс извлечённых из Word иллюстраций, которые Pandoc не вставил в Markdown]'+
      f'(RECOVERED_FIGURES.md) — {len(missing)} файлов для визуальной сверки. '+
      'Списки не заменяют точного расположения на исходных страницах PDF.\n',encoding='utf-8')
    print(json.dumps({'volume':vol,'page_status':dict(statuses),'omitted_inline':len(missing),
      'located':qa['omitted_inline_media_locatable_to_unit_unique'],'unlocated':qa['omitted_inline_media_not_locatable_unique']},ensure_ascii=False),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('/mnt/data/kob_books_v0_4'));p.add_argument('--workspace',type=Path,default=Path('/mnt/data/kob_converter_workspace'));p.add_argument('--volumes',nargs='+',type=int,default=[2,3,4,5,6]);v=p.parse_args();main(v.root,v.workspace,v.volumes)
