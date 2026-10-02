#!/usr/bin/env python3
"""Conservative, reproducible DOC+PDF -> a browsable Markdown/JSONL volume.

Original works are source evidence, not verified real-world claims. Preserve source
text and quote provenance; never summarize or invent figure placements. This is
an AUTOMATIC transcription requiring subsequent visual/editorial acceptance.
"""
from __future__ import annotations
import argparse, collections, hashlib, json, re, shutil, subprocess, sys, unicodedata, zipfile
from pathlib import Path
import fitz, yaml

TITLE = 'Основы социологии'
RE_FOOTNOTE_DEF = re.compile(r'(?ms)^\[\^(\d+)\]:.*?(?=^\[\^\d+\]:|\Z)')
RE_FOOTNOTE_REF = re.compile(r'\[\^(\d+)\]')
RE_IMG = re.compile(r'<img\s+src="(?P<src1>[^"]+)"[^>]*\s*/>|!\[(?P<alt>[^]]*)\]\((?P<src2>[^)]+)\)(?:\{[^}]*\})?', re.I)
RE_CH = re.compile(r'^#\s+Глава\s+(\d+)\.', re.I)
RE_AP = re.compile(r'^#\s+Приложение[\s\u00a0]+(\d+)\.',re.I)
RE_SEC = re.compile(r'^#{2,4}\s+(\d+)\.(\d+)(?:\.(\d+))?\.\s+')
RE_PREF = re.compile(r'^#\s+Предисловие',re.I)


def run(command, timeout=300):
    p=subprocess.run(command,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('COMMAND FAILED: '+str(command)+'\n'+p.stderr[-3500:])
    return p.stdout,p.stderr


def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(2**20),b''):h.update(b)
    return h.hexdigest()


def plain(s):
    s=RE_FOOTNOTE_REF.sub('',s)
    s=re.sub(r'\]\([^)]+\)','',s)
    s=re.sub(r'\<[^>]+\>','',s)
    return re.sub(r'[*_`#]+','',s).strip()


def normalize(s):
    s=unicodedata.normalize('NFKC',s).casefold().replace('ё','е')
    return ''.join(x for x in s if (x.isalpha() or x.isdigit()))


def group_units(main_body):
    lines=main_body.splitlines(keepends=True)
    units=[];cur=('front','front','Титул и исходные выходные сведения');start=0
    current_ch=None;current_ap=None;have_toc=False
    for index,line in enumerate(lines):
        k=i=t=None
        if not have_toc and line.strip() in ['ОГЛАВЛЕНИЕ','Оглавление']:
            k,i,t='original_toc','toc','Оригинальное оглавление';have_toc=True
        elif RE_PREF.match(line):
            k,i,t='preface','preface','Предисловие'
        elif (m:=RE_CH.match(line)):
            current_ch=int(m[1]);current_ap=None
            k,i,t='chapter',f'ch-{current_ch:02}',plain(line.strip().lstrip('# '))
        elif (m:=RE_AP.match(line)):
            current_ap=int(m[1]);current_ch=None
            k,i,t='appendix',f'ap-{current_ap:02}',plain(line.strip().lstrip('# '))
        elif (m:=RE_SEC.match(line)):
            parts=list(map(int,[x for x in m.groups() if x]))
            # Appendix 3 contains "3.1"; keep separately from chapter 3.
            scope=f'ap-{current_ap:02}' if current_ap is not None else f'ch-{current_ch:02}' if current_ch is not None else 'unscoped'
            if len(parts)==2:
                k='section';i=f'{scope}-s{parts[0]:02}-{parts[1]:02}'
            else:
                k='subsection';i=f'{scope}-s{parts[0]:02}-{parts[1]:02}-{parts[2]:02}'
            t=plain(line.strip().lstrip('# '))
        if k:
            if start<index:
                units.append({'kind':cur[0],'id':cur[1],'title':cur[2],'raw':''.join(lines[start:index])})
            cur=(k,i,t);start=index
    if start<len(lines):units.append({'kind':cur[0],'id':cur[1],'title':cur[2],'raw':''.join(lines[start:])})
    assert ''.join(u['raw'] for u in units)==main_body,'MAIN_BODY_CONTENT_LOST'
    counts=collections.Counter(x['id'] for x in units)
    assert max(counts.values(),default=0)==1, 'DUPLICATE UNIT ID: '+str(counts.most_common(10))
    return units


def assign_paths(units):
    kind_dirs={'chapter':'chapters','appendix':'appendices','section':'sections','subsection':'subsections'}
    for u in units:
        if u['kind'] in kind_dirs:
            u['path']=f'{kind_dirs[u["kind"]]}/{u["id"]}.md'
        else:
            u['path']=('original-toc.md' if u['kind']=='original_toc' else 'preface.md' if u['kind']=='preface' else 'frontmatter.md')


def footnote_partition(raw_text,units):
    start=re.search(r'^\[\^\d+\]:',raw_text,re.M)
    assert start,'PANDOC_NOTE_DEFS_NOT_FOUND'
    notes=dict((int(m[1]),m[0].rstrip()) for m in RE_FOOTNOTE_DEF.finditer(raw_text[start.start():]))
    assert len(notes)==len(list(RE_FOOTNOTE_DEF.finditer(raw_text[start.start():]))),'DUPLICATE_NOTE_ID'
    owned={};repeats=[]
    for u in units:
        calls=[int(s) for s in RE_FOOTNOTE_REF.findall(u['raw'])]
        u['note_ids']=calls
        for n in calls:
            if n in owned:repeats.append((n,u['id'],owned[n]))
            owned[n]=u['id']
    missing=sorted(set(owned)-set(notes));unowned=sorted(set(notes)-set(owned))
    assert not missing,'UNDEFINED FOOTNOTES '+str(missing)
    assert not repeats,'REPEATED FOOTNOTE MARKERS '+str(repeats[:8])
    return notes,unowned


def make_assets(out,docx):
    with zipfile.ZipFile(docx) as z:
        media={Path(n).name:z.read(n) for n in z.namelist() if n.startswith('word/media/') and not n.endswith('/')}
        ole=[n for n in z.namelist() if n.startswith('word/embeddings/') and not n.endswith('/')]
    (out/'assets/media').mkdir(parents=True,exist_ok=True)
    (out/'assets/media_original').mkdir(parents=True,exist_ok=True)
    failures=[]
    for name,raw in media.items():
        (out/'assets/media_original'/name).write_bytes(raw)
        newname=Path(name).with_suffix('.png').name if Path(name).suffix.lower() in ('.wmf','.emf') else name
        target=out/'assets/media'/newname
        if Path(name).suffix.lower() in ('.wmf','.emf'):
            try:
                run(['magick','-density','300',str(out/'assets/media_original'/name),'-trim','+repage','-resize','1500x1500','-strip',str(target)],timeout=45)
                if target.stat().st_size<200:raise ValueError('preview unexpectedly small')
            except Exception as e:failures.append({'name':name,'reason':str(e)[:300]})
        else:target.write_bytes(raw)
    return media,ole,failures


def media_rewriter(content,u,media,out):
    found=[];broken=[]
    def replace(m):
        src=m['src1'] or m['src2']
        if not re.search(r'\bimage\d+\.(jpeg|jpg|png|emf|wmf|gif|svg)\b',src,re.I):
            return m[0]  # This is an external hyperlink/image, not DOCX media.
        name=Path(src.strip()).name
        if name not in media:
            broken.append(name);return m[0]
        found.append(name)
        new=Path(name).with_suffix('.png').name if Path(name).suffix.lower() in ('.emf','.wmf') else name
        has=(out/'assets/media'/new).exists()
        if not has:broken.append(name);return f'**[НЕУДАЛОСЬ СОЗДАТЬ ПРЕВЬЮ {name}; оригинал сохранён в assets/media_original/]**'
        rel=('../' if '/' in u['path'] else '')+'assets/media/'+new
        return f'![Иллюстрация из исходного DOC: {name}]({rel})'
    result=RE_IMG.sub(replace,content)
    return result,found,broken


def find_pdf_pages(units,pdf):
    """Conservative auto-candidates. Not a human verification of page fidelity."""
    pages=[normalize(p.get_text(sort=True)) for p in pdf]
    last=4  # skip cover and very early TOC; earliest body typically PDF p5
    for u in units:
        u['pdf_page']=None;u['page_match']='not_located'
        if u['kind'] not in ('chapter','appendix','section','subsection','preface'):continue
        s=u['title']
        key=normalize(s)
        # Very short titles are ambiguous; require >= 14 chars after markup removal.
        if len(key)<14:continue
        # References to figures/table numbers can intrude; title fragments good for candidate only.
        key=key[:min(len(key),95)]
        positions=[x for x in range(max(4,last-1),len(pages)) if key in pages[x]]
        if not positions:continue
        # To disambiguate TOC and body: first unambiguously located chapter/section
        # should come after p4; heading strings may appear in original TOC until p7.
        # Candidate not accepted as confirmed without manual page rendering.
        candidate=positions[0]
        u['pdf_page']=candidate+1
        u['page_match']='auto_heading_candidate_needs_review'
        last=max(last,candidate)


def chunk_content(text,limit=3200):
    # Exact non-overlapping character ranges; always reconstruct exact text.
    pieces=[];start=0
    while start<len(text):
        stop=min(start+limit,len(text))
        if stop<len(text):
            idx=text.rfind('\n\n',start+max(1200,limit//3),stop)
            if idx>start:stop=idx+2
        if stop<=start:stop=min(start+limit,len(text))
        pieces.append((start,stop,text[start:stop]));start=stop
    assert ''.join(x[2] for x in pieces)==text
    return pieces


def build(volume,inputs,out_root,workspace,allow_existing=False):
    source_doc=inputs/f'Основы социологии (том {volume}).doc'
    source_pdf=inputs/f'Основы социологии (том {volume}).pdf'
    assert source_doc.exists() and source_pdf.exists(),(source_doc,source_pdf)
    vdir=out_root/f'tom-{volume}'
    if vdir.exists():shutil.rmtree(vdir)
    for n in ['data','source','sections','subsections','chapters','appendices','assets','notes']:(vdir/n).mkdir(parents=True,exist_ok=True)
    docx=workspace/'converted'/f'Основы социологии (том {volume}).docx'
    rawmd=workspace/f'vol{volume}.md'
    cachefile=workspace/f'vol{volume}.input_doc_sha256'
    input_hash=digest(source_doc)
    if not cachefile.exists() or cachefile.read_text(encoding='utf-8').strip()!=input_hash:
        for stale in (docx,rawmd):
            if stale.exists():stale.unlink()
    if not docx.exists():
        docx.parent.mkdir(parents=True,exist_ok=True)
        run(['libreoffice',f'-env:UserInstallation=file:///tmp/lo_kob_uv{volume}','--headless','--convert-to','docx','--outdir',str(docx.parent),str(source_doc)],timeout=420)
    if not rawmd.exists() or rawmd.stat().st_mtime<docx.stat().st_mtime:
        run(['pandoc','-f','docx','-t','gfm','--wrap=none','--extract-media='+str(workspace/f'vol{volume}_assets'),str(docx),'-o',str(rawmd)],timeout=210)
    cachefile.write_text(input_hash+'\n',encoding='utf-8')
    whole=rawmd.read_text(encoding='utf-8')
    note_pos=re.search(r'^\[\^\d+\]:',whole,re.M)
    main=whole[:note_pos.start()].rstrip('\n')+'\n' if note_pos else whole
    units=group_units(main)
    assign_paths(units)
    notes,orphans=footnote_partition(whole,units)
    media,ole,failures=make_assets(vdir,docx)
    srcname=f'osnovy-sociologii-tom-{volume}'
    dest_doc=vdir/'source'/f'{srcname}.doc'
    dest_pdf=vdir/'source'/f'{srcname}.pdf'
    shutil.copy2(source_doc,dest_doc);shutil.copy2(source_pdf,dest_pdf)
    pdf=fitz.open(source_pdf)
    find_pdf_pages(units,pdf)
    doc_hash=digest(source_doc);pdf_hash=digest(source_pdf);raw_hash=hashlib.sha256(main.encode('utf-8')).hexdigest()
    edition_id='os-'+str(volume)+'-'+doc_hash[:12]
    sections=[];chunks=[]; figures=[];errors=[]
    for ordinal,u in enumerate(units):
        path=vdir/u['path'];path.parent.mkdir(exist_ok=True,parents=True)
        cleaned,figs,missing=media_rewriter(u['raw'],u,media,vdir)
        local_notes=[]
        for n in u['note_ids']:
            txt,imgs,broken=media_rewriter(notes[n],u,media,vdir)
            figs+=imgs;missing+=broken
            local_notes.append(txt)
        if missing:errors.append({'unit':u['id'],'images':sorted(set(missing))})
        mdmeta={
           'id':edition_id+'-'+u['id'],'work_id':'osnovy-sociologii','volume':volume,
           'kind':u['kind'],'title':u['title'],
           'source_doc':'../source/'+dest_doc.name if '/' in u['path'] else 'source/'+dest_doc.name,
           'source_pdf':'../source/'+dest_pdf.name if '/' in u['path'] else 'source/'+dest_pdf.name,
           'source_doc_sha256':doc_hash,'source_pdf_sha256':pdf_hash,
           'pdf_page_1_based_candidate':u['pdf_page'],'pdf_page_status':u['page_match'],
           'transcription_status':'automated_unreviewed','text_is_author_position_not_factcheck':True,
           'footnote_numbers':u['note_ids'],'media_original':sorted(set(figs)),
           'origin':'DOC→LibreOffice DOCX→Pandoc GFM; page locations from PDF heuristics'
        }
        header='---\n'+yaml.safe_dump(mdmeta,sort_keys=False,allow_unicode=True,width=110).rstrip()+'\n---\n\n'
        foots='\n\n'.join(local_notes)
        content=header+cleaned.rstrip()+'\n'+ ('\n'+foots+'\n' if foots else '')
        path.write_text(content,encoding='utf-8')
        u['markdown_body']=cleaned
        sec={
            'id':mdmeta['id'],'kind':u['kind'],'title':u['title'],'path':u['path'],
            'volume':volume,'reading_order':ordinal,
            'source_doc_sha256':doc_hash,'source_pdf_sha256':pdf_hash,
            'source_original_text_sha256':hashlib.sha256(u['raw'].encode('utf-8')).hexdigest(),
            'source_original_characters':len(u['raw']),
            'pdf_page_candidate':u['pdf_page'],'page_mapping_status':u['page_match'],
            'footnote_numbers':u['note_ids'],'images_original':sorted(set(figs)),
            'previous_path':units[ordinal-1]['path'] if ordinal else None,
            'next_path':units[ordinal+1]['path'] if ordinal<len(units)-1 else None,
            'text':cleaned,'footnotes':{str(x):notes[x] for x in u['note_ids']}}
        sections.append(sec)
        for j,(a,b,s) in enumerate(chunk_content(cleaned)):
            chunks.append({'id':mdmeta['id']+f'-chunk-{j:04}',
                           'section_id':mdmeta['id'],'source_path':u['path'],
                           'volume':volume,'pdf_page_candidate':u['pdf_page'],
                           'start_char':a,'end_char':b,'text':s})
        for name in figs:
            orig=Path(name);outname=orig.with_suffix('.png').name if orig.suffix.lower() in ('.wmf','.emf') else name
            figures.append({'volume':volume,'unit_path':u['path'],'media_name':name,
                            'preview_path':'assets/media/'+outname if (vdir/'assets/media'/outname).exists() else None,
                            'original_path':'assets/media_original/'+name,
                            'pdf_page_candidate':u['pdf_page'],
                            'status':'DOC_extracted_not_visually_confirmed_in_PDF'})
    rawjoined=''.join(u['raw'] for u in units)
    assert rawjoined==main,'LOSS in main body during split'
    assert all(''.join(c['text'] for c in chunks if c['section_id']==s['id'])==s['text'] for s in sections)
    assert len(chunks)>=len(sections)
    (vdir/'data/raw_extracted_main.md').write_text(main,encoding='utf-8')
    (vdir/'data/sections.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in sections),encoding='utf-8')
    (vdir/'data/chunks.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in chunks),encoding='utf-8')
    (vdir/'data/figures.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in figures),encoding='utf-8')
    (vdir/'data/footnotes.jsonl').write_text(''.join(json.dumps({'id':n,'owner':next((u['path'] for u in units if n in u['note_ids']),None),'definition_raw':txt},ensure_ascii=False)+'\n' for n,txt in sorted(notes.items())),encoding='utf-8')
    if orphans:
        (vdir/'notes/unattached.md').write_text('# Не привязаны к выноске (проверить)\n\n'+'\n\n'.join(notes[n] for n in orphans),encoding='utf-8')
    with (vdir/'data/manifest.json').open('w',encoding='utf-8') as f:
        json.dump({'volume':volume,'doc_sha256':doc_hash,'pdf_sha256':pdf_hash,
                   'input_doc':source_doc.name,'input_pdf':source_pdf.name,
                   'unmodified_extracted_main_sha256':raw_hash,
                   'converter_version':'0.4.0','source_representation':'DOC text; PDF visual reference',
                   'original_authorial_year_from_cover_requires_manual_verification':None,
                   'pdf_pages':len(pdf),'docx_media_count':len(media),'ole_embedded_objects_count':len(ole)},f,ensure_ascii=False,indent=2)
    readme=[f'# «Основы социологии», том {volume}: автоматическая разметка (v0.4)',
            '', '**Статус: внутренний, автоматический, требует визуальной сверки.** Это цифровое представление авторского текста, а не проверка изложенных в нём утверждений.',
            '',f'[Оригинал DOC](source/{dest_doc.name}) · [PDF](source/{dest_pdf.name}) · [отчёт](QA.md) · [разделы в JSONL](data/sections.jsonl) · [чанки для RAG](data/chunks.jsonl)',
            '',f'**SHA-256 DOC:** `{doc_hash}` · **SHA-256 PDF:** `{pdf_hash}`.',
            '', 'Номер страницы PDF, если указан, является **кандидатом автоматического поиска заголовка**, а не подтверждённой вручную страницей.',
            '', '## Оглавление и переходы', '']
    for u in units:
        title=re.sub(r'\|','\\|',u['title'])
        if u['kind'] in ('chapter','appendix'):readme+=['',f'### [{title}]({u["path"]})','']
        elif u['kind'] in ('section','subsection'):
            prefix='    ' if u['kind']=='subsection' else ''
            readme.append(prefix+f'- [{title}]({u["path"]})')
        else:readme.append(f'- [{title}]({u["path"]})')
    readme+=['', '## Обработка', '',
        '- Авторский текст при разбиении не сокращён: объединение исходных текстовых блоков даёт `data/raw_extracted_main.md` посимвольно.',
        '- Глобально нумерованные сноски размещены в конце файлов, содержащих соответствующие выноски; номера сохраняются.',
        '- Изображения из DOCX сохранены в `assets/media_original/`, предварительные просмотры — в `assets/media/`.',
        '- Вложенные OLE-объекты не обещаны как полноценно воспроизведённые в Markdown; оригинальные DOC/PDF остаются основным средством сверки.',
        '- Оригинальное оглавление DOC извлечено в файл, но его старые внутрифайловые ссылки могут не работать; рабочая навигация размещена здесь.',
        '- До публикации требуется проверка редакции, корректности визуальных объектов и условий распространения.', '']
    (vdir/'README.md').write_text('\n'.join(readme),encoding='utf-8')
    count=collections.Counter(u['kind'] for u in units)
    unresolved_media=sorted(set(media)-set(f['media_name'] for f in figures))
    qa={'volume':volume,'unit_counts':count,'source_text_split_lossless':rawjoined==main,
        'footer_notes_total':len(notes),'notes_unattached':orphans,'all_notes_placed':not orphans,
        'pdf_pages':len(pdf),'heading_page_candidates':sum(u['pdf_page'] is not None for u in units),
        'heading_page_not_located':sum(u['pdf_page'] is None and u['kind'] in ['chapter','appendix','section','subsection','preface'] for u in units),
        'media_total':len(media),'media_placements':len(figures),
        'docx_media_not_placed':unresolved_media,
        'media_preview_failures':failures,'media_broken_markup':errors,
        'OLE_objects':len(ole),'chunks':len(chunks),
        'source_doc_sha256':doc_hash,'source_pdf_sha256':pdf_hash,
        'status':'INTERNAL_AUTOMATIC_NEEDS_VISUAL_QA'}
    with (vdir/'data/qa.json').open('w',encoding='utf-8') as f:json.dump(qa,f,ensure_ascii=False,indent=2)
    (vdir/'QA.md').write_text('# Отчёт автоматического конвертера\n\n'+
        f'- Том: **{volume}**; PDF страниц: **{len(pdf)}**.\n'+
        '- Единицы текста: '+', '.join(f'{k}: {v}' for k,v in sorted(count.items()))+'.\n'+
        f'- Сноски: **{len(notes)}**; потерянных определений без выноски: **{len(orphans)}**.\n'+
        f'- Медиа из DOCX: **{len(media)}**; размещений в Markdown (включая сноски): **{len(figures)}**; без определённого места: **{len(unresolved_media)}**.\n'+
        f'- Встроенные OLE: **{len(ole)}**, передача содержимого в Markdown не подтверждена.\n'+
        f'- Совпавшие заголовки со страницами PDF (автоматические кандидаты): **{qa["heading_page_candidates"]}**; НЕ являются ручной проверкой.\n'+
        f'- Непересекающиеся индексные блоки: **{len(chunks)}**.\n'+
        '- Потерь при распределении основного извлечённого текста: **нет** (точное сравнение Unicode).\n'+
        '- **Нет посимвольной сверки DOC ↔ PDF, нет гарантии передачи всех OLE, у таблиц и рисунков могут быть пропуски.**\n'+
        f'- Неиспользованные медиа: `{unresolved_media}`.\n'+
        f'- Ошибки превью: `{failures}`.\n'+
        f'- Проблемы ссылок на медиа: `{errors[:30]}`.\n'+
        '\n**До публикации:** выборочно и прицельно сопоставить картинки, таблицы, формулы, вложения и сноски с PDF.\n',encoding='utf-8')
    pdf.close()
    return qa


def main():
    pa=argparse.ArgumentParser(description=__doc__)
    pa.add_argument('--input-dir',type=Path,default=Path('/mnt/data'))
    pa.add_argument('--workspace',type=Path,default=Path('/mnt/data/kob_converter_workspace'))
    pa.add_argument('--output-dir',type=Path,default=Path('/mnt/data/kob_books_v0_4'))
    pa.add_argument('--volumes',nargs='+',type=int,default=[2,3,4,5,6])
    args=pa.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    for n in args.volumes:
        result=build(n,args.input_dir,args.output_dir,args.workspace)
        print(json.dumps({k:v for k,v in result.items() if k in ('volume','unit_counts','footer_notes_total','notes_unattached','heading_page_candidates','media_total','media_placements','docx_media_not_placed','media_preview_failures','OLE_objects','chunks')},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
