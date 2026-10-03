#!/usr/bin/env python3
"""Read-only site compiler for the edition-specific KOB corpus. Never edits source content.
Outputs a static browsing site and machine indices. Public mode fails closed.
"""
from __future__ import annotations
import argparse, collections, hashlib, html, json, re, shutil, os
from pathlib import Path
from urllib.parse import unquote, urlparse
import mistune
import semantic_layer as sem

VOLUMES=range(1,7)
DEFAULT_ROBOTS='noindex,nofollow'
MARKDOWN=mistune.create_markdown(escape=True,plugins=['footnotes','table','strikethrough','url'])
STYLE='''
:root{--paper:#f7f8fa;--ink:#1d2834;--muted:#536477;--line:#d8dfe8;--blue:#165d97;--surface:#fff}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,'Segoe UI',Arial,sans-serif;background:var(--paper);color:var(--ink);margin:0;line-height:1.62}
header{background:#18283d;color:#fff;padding:1.1rem max(1rem,calc((100vw - 1160px)/2))}header a{color:#fff;text-decoration:none}
.wrap{max-width:1160px;margin:0 auto;padding:1.2rem;display:grid;grid-template-columns:270px minmax(0,1fr);gap:1.2rem}
nav,article,.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:1.2rem}nav{position:sticky;top:14px;max-height:calc(100vh - 30px);overflow-y:auto}
nav ol{list-style:none;padding-left:.3rem;margin:.2rem 0}nav li{margin:.55rem 0;font-size:.9rem;line-height:1.34}a{color:var(--blue)}a:hover{text-decoration-thickness:2px}
article{min-width:0}article h1{font-size:clamp(1.5rem,3vw,2.1rem);line-height:1.3}h2,h3{line-height:1.3}article p,article li{overflow-wrap:break-word}article img{display:block;max-width:100%;height:auto;margin:1rem auto}article blockquote{border-left:4px solid var(--line);margin:1.2rem 0;padding:.1rem 1rem;color:var(--muted)}
article table{display:block;overflow-x:auto;border-collapse:collapse}th,td{border:1px solid var(--line);padding:7px 9px}pre{overflow-x:auto;padding:1rem;background:#eef3f7}
small,.muted{color:var(--muted)}.notice{background:#fff5db;border-left:3px solid #e3ae3a;padding:.6rem .9rem;margin:1rem 0}.card{margin:.7rem 0}
input{width:100%;padding:.8rem;border:1px solid #a2adbc;border-radius:6px;font:inherit}.search-result{padding:1rem 0;border-bottom:1px solid var(--line)}
code{white-space:pre-wrap;overflow-wrap:anywhere}.breadcrumbs{font-size:.85rem}.pager{border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:1rem;padding-top:1rem;margin-top:2rem}
button.cite{margin:.2rem .4rem .2rem 0;padding:.45rem .8rem;border:1px solid var(--line);border-radius:6px;background:#eef3f7;color:var(--ink);cursor:pointer;font:inherit;font-size:.85rem}button.cite:hover{background:#dde8f0}
@media(max-width:820px){.wrap{grid-template-columns:1fr}nav{position:static;max-height:280px}}
'''
READER_JS='''
async function copyText(t){try{await navigator.clipboard.writeText(t);return true;}catch(e){const ta=document.createElement('textarea');ta.value=t;document.body.append(ta);ta.select();try{document.execCommand('copy');return true;}finally{ta.remove();}}}
function citation(b){const sel=String(window.getSelection());const parts=[b.dataset.title,'«Основы социологии», ВП СССР, том '+b.dataset.volume];if(b.dataset.section)parts.push('раздел '+b.dataset.section);let src='полный раздел в этой библиотеке: '+(b.dataset.sectionUrl||'адрес текущей страницы');if(b.dataset.pdf){src+='; проверенное издание: '+b.dataset.pdf;if(b.dataset.page)src+=', стр. '+b.dataset.page+' (кандидат, требует сверки)';if(b.dataset.archive)src+='; общий архив оригинальных публикаций: '+b.dataset.archive;}parts.push(src);let txt=sel?('"'+sel+'" — '+parts.join(', ')):parts.join(', ');return txt;}
for(const b of document.querySelectorAll('button.cite')){b.addEventListener('click',async()=>{const ok=await copyText(citation(b));b.textContent=ok?'Скопировано':'Ошибка копирования';setTimeout(()=>{b.textContent='Копировать с источником';},1500);});}
'''
SEARCH_JS='''
const area=document.getElementById('results');
const input=document.getElementById('query');
let index=[];
fetch('data/search_index.json').then(r=>r.json()).then(x=>{index=x;document.getElementById('loading').textContent='Готово к поиску: '+index.length+' текстовых фрагментов';}).catch(()=>{document.getElementById('loading').textContent='Ошибка загрузки индекса. При открытии с file:// запустите локальный HTTP-сервер.';});
function el(tag,text,klass){const e=document.createElement(tag);if(text)e.textContent=text;if(klass)e.className=klass;return e;}
function doSearch(){const q=input.value.toLocaleLowerCase('ru').trim().split(/\\s+/).filter(Boolean);area.replaceChildren();if(!q.length)return;
const hits=[];for(const item of index){const text=(item.title+' '+item.text).toLocaleLowerCase('ru');if(q.every(w=>text.includes(w))){const score=q.reduce((s,w)=>s+(item.title.toLocaleLowerCase('ru').includes(w)?10:1),0);hits.push({item,score});}}
hits.sort((a,b)=>b.score-a.score);area.append(el('p','Найдено: '+hits.length+'. Показаны первые 50 совпадений.','muted'));
for(const v of hits.slice(0,50)){const block=el('div',null,'search-result');const a=el('a',v.item.title);if(/^[a-z][a-z0-9+.-]*:/i.test(v.item.url))continue;a.href=v.item.url;const par=el('p',v.item.text.slice(0,380));const sub=el('small',v.item.kind==='concept'?('понятие · '+v.item.section_id):('Том '+v.item.volume+' · '+v.item.section_id));block.append(a,par,sub);area.append(block);}}
input.addEventListener('input',doSearch);
'''

def read_jsonl(path):return [json.loads(l) for l in path.read_text(encoding='utf8').splitlines() if l.strip()]
def esc(x):return html.escape(str(x),quote=True)
def redirect_markdown_links(s):
    # Marked-up HTML produced with mistune escaping raw HTML.
    return re.sub(r'(href=["\'])([^"\']+?)\.md(?=(?:#[^"\']*)?["\'])',lambda m:m.group(1)+m.group(2)+'.html',s)
def standalone(title,main,side='',rootprefix='',robots=None,reader=False,desc=None,canonical=None,jsonld=None):
    if robots is None:robots=DEFAULT_ROBOTS
    robots_tag=f'<meta name="robots" content="{robots}">'
    desc_tag=f'<meta name="description" content="{esc(desc[:250])}">' if desc else ''
    canon_tag=f'<link rel="canonical" href="{esc(canonical)}">' if canonical else ''
    jsonld_tag=jsonld or ''
    reader_tag=f'<script src="{rootprefix}assets/reader.js" defer></script>' if reader else ''
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{robots_tag}{desc_tag}{canon_tag}{jsonld_tag}<title>{esc(title)}</title><link rel="stylesheet" href="{rootprefix}assets/style.css">{reader_tag}</head><body><header><a href="{rootprefix}index.html"><strong>Корпус «Основы социологии»</strong></a> · <span style="opacity:.8">структурированный архив первоисточников</span></header><div class="wrap"><nav><a href="{rootprefix}index.html">← Вся библиотека</a>{side}</nav><article>{main}</article></div></body></html>'''

def header_removed(md):return re.sub(r'\A---\s*\n.*?\n---\s*\n','',md,flags=re.S)

def rel_to_root(page:Path,out:Path):
    return '../'*len(page.relative_to(out).parts[:-1])

def ensure_public_gate(base:Path,release:Path):
    from release_gate import check_release
    good, reasons, notes=check_release(base,release)
    if not good:raise SystemExit('PUBLIC RELEASE BLOCKED:\n'+ '\n'.join('- '+x for x in reasons))

def _sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(2**20),b''):h.update(chunk)
    return h.hexdigest()

def load_media_allowlist(allowlist_path:Path|None):
    """Approved published-media map {(volume, filename): sha256} from
    review/public_media_allowlist.jsonl. Absent file -> empty map."""
    if not allowlist_path or not allowlist_path.is_file():return {}
    out={}
    for line in allowlist_path.read_text(encoding='utf8').splitlines():
        if not line.strip():continue
        e=json.loads(line)
        if e.get('decision')=='approved' and e.get('sha256') and e.get('volume') is not None:
            out[(int(e['volume']),Path(e['path']).name)]=e['sha256']
    return out

def load_release_scope(release:Path|None):
    """Reviewer-declared artifact composition from the approval file.
    Anything absent means the most restrictive interpretation: no media,
    no bundled PDFs, no volumes beyond those listed."""
    scope={}
    if release and release.is_file():
        try:
            s=json.loads(release.read_text(encoding='utf8')).get('release_scope')
            if isinstance(s,dict):scope=s
        except Exception:pass
    media=scope.get('media') if scope.get('media') in ('none','allowlist') else 'none'
    pdfs=scope.get('pdfs')
    canon=scope.get('canonical_pdf_url') or ''
    if pdfs not in ('ship','reference','none'):pdfs='none'
    if pdfs=='reference' and not canon.startswith('https://'):pdfs='none'
    vols=[v for v in (scope.get('volumes') or []) if isinstance(v,int) and 1<=v<=6]
    return {'media':media,'pdfs':pdfs,'volumes':sorted(set(vols)) or list(VOLUMES),
            'canonical_pdf_url':canon if canon.startswith('https://') else ''}

MEDIA_OMIT_CSS='.media-omitted{border:1px dashed var(--line);border-radius:8px;padding:.7rem .9rem;margin:1rem 0;background:#f2f5f8;font-size:.9rem}'

def _omit_media_imgs(html_out:str,keep:set,volume:int,section_id:str,noted:list):
    """Replace <img src="assets/media/..."> with a visible omission notice so a
    text-first release never ships a broken image nor silently drops a scheme."""
    def repl(m):
        tag,name=m.group(0),m.group(1)
        if (volume,name) in keep:return tag
        alt=re.search(r'alt="([^"]*)"',tag)
        alt_txt=f' «{html.unescape(alt.group(1))}»' if alt and alt.group(1) else ''
        noted.append(f'tom-{volume}:{section_id}:{name}')
        return (f'<div class="media-omitted">[Иллюстрация{alt_txt} не включена в данный выпуск: '
                f'ожидает визуальной и правовой проверки. Файл источника: assets/media/{esc(name)}]</div>')
    # Match any img whose src resolves to assets/media/<name>: plain,
    # relative ("../assets/media/...") or nested-path forms.
    return re.sub(r'<img[^>]*src="[^"]*assets/media/([\w.\-]+)"[^>]*>',repl,html_out)

def audit_internal_links(out:Path):
    """Every relative href/src in shipped HTML must resolve to a shipped file.
    Returns sorted 'page -> target' records for missing targets (also catches
    external <img> embeds separately in the caller's report)."""
    missing=[]
    for page in sorted(out.rglob('*.html')):
        h=page.read_text(encoding='utf8')
        for attr in ('href','src'):
            for m in re.finditer(attr+r'="([^"#]+?)"',h):
                tgt=m.group(1)
                if '://' in tgt or tgt.startswith(('mailto:','data:','#','/')):continue
                tgt=unquote(tgt).split('?')[0]
                if not tgt:continue
                if not (page.parent/tgt).resolve().exists():
                    missing.append(f'{page.relative_to(out).as_posix()} -> {tgt}')
    return sorted(set(missing))

# Thematic rubrics: each label maps to terms that must literally appear in
# real section titles — a rubric ships only if the corpus proves it.
RUBRICS = [
    ('Управление и теория управления', ['управлени']),
    ('Общество и социальные процессы', ['обществ', 'социальн', 'социолог']),
    ('История и исторические процессы', ['истори']),
    ('Методология и познание', ['методолог', 'познани', 'мировоззрени']),
    ('Цивилизация и глобальные процессы', ['цивилизац', 'глобальн']),
    ('Культура и мировоззрение', ['культур', 'мировоззрени']),
    ('Психология и личность', ['психолог', 'личност']),
    ('Экономика и хозяйство', ['экономи', 'хозяйств']),
    ('Эгрегоры и матрично-эгрегориальное управление', ['эгрегор', 'матрич']),
]

def build_topics_page(all_sections, out:Path, base_url:str):
    """Editorial 'Темы и разделы' page: rubric -> links to full sections.
    Only rubrics confirmed by real section titles are emitted."""
    blocks=[]
    used=set()
    for label,terms in RUBRICS:
        hits=[s for s in all_sections
              if s.get('kind')!='frontmatter' and any(t in s['title'].lower() for t in terms)
              and s['id'] not in used]
        if not hits:continue
        items='\n'.join(
            f'<li><a href="tom-{s["volume"]}/{esc(Path(s["path"]).with_suffix(".html").as_posix())}">{esc(s["title"])}</a> '
            f'<small>· том {s["volume"]}</small></li>' for s in hits[:15])
        for s in hits:used.add(s['id'])
        blocks.append(f'<h2>{esc(label)}</h2><ol>{items}</ol>')
    if not blocks:return None
    body=('<h1>Темы и разделы</h1>'
          '<p class="notice"><strong>Редакционная навигация, а не авторский текст.</strong> '
          'Рубрики выведены из реальных заголовков разделов источника; каждая ссылка ведёт '
          'на полный раздел с контекстом. Включение в рубрику не является утверждением '
          'истинности содержания раздела.</p>'+''.join(blocks))
    (out/'topics.html').write_text(standalone('Темы и разделы',body,rootprefix='',
        desc='Тематическая навигация по разделам библиотеки «Основы социологии» ВП СССР (редакционная, не авторский текст).',
        canonical=(base_url.rstrip('/')+'/topics.html') if base_url else None),encoding='utf8')
    return sum(1 for _ in blocks)

def build(corpus:Path,out:Path,mode:str,release:Path|None=None,base_url:str='',editorial:Path|None=None):
    global DEFAULT_ROBOTS
    # 'candidate' renders the exact public file set for review/dry-run but is
    # marked noindex and carries a not-for-publication banner; it never deploys.
    if mode not in ('internal','public','candidate'):raise SystemExit(f'unknown mode {mode}')
    # Editorial semantic layer (topics/concepts/citations/variants). Schema
    # errors are fatal everywhere: silently shipping a partial semantic map
    # is worse than no semantic layer at all.
    ed=sem.load_editorial(editorial if editorial is not None
                          else Path(__file__).resolve().parents[1]/'editorial')
    ed_errors=sem.validate_records(ed)
    if ed_errors:
        raise SystemExit('EDITORIAL RECORDS INVALID:\n- '+'\n- '.join(ed_errors))
    cite_by_sec={c['section_id']:c for c in ed['citations']
                 if c.get('status')=='active' and c.get('site_path')}
    DEFAULT_ROBOTS='index,follow' if mode=='public' else 'noindex,nofollow'
    if mode=='public':
        ensure_public_gate(corpus,release or Path('release/release_approval.json'))
        if not base_url or not base_url.startswith('https://'):
            # Citations embed the permanent address of the full section page;
            # a relative path in a copied citation is not a public reference.
            raise SystemExit('PUBLIC RELEASE BLOCKED: --base-url https://<final site origin> is required so citations carry permanent public URLs')
    # Published preview media are allowlist-driven: internal builds keep the
    # full corpus view; candidate/public copy only approved files and report
    # (candidate) or refuse (public) Markdown references to unapproved media.
    allow=load_media_allowlist((release.parent/'public_media_allowlist.jsonl') if release else None)
    scope=load_release_scope(release) if mode!='internal' else {'media':'allowlist','pdfs':'ship','volumes':list(VOLUMES),'canonical_pdf_url':''}
    # When a figure registry exists it is the decision artifact: only
    # registry-publishable files may ship, and non-publishable refs are
    # omitted with a note — not treated as unapproved publishable media.
    # Without a registry, legacy strict mode applies (every media ref is
    # publishable and must be allowlisted).
    publishable=None
    if mode!='internal' and scope['media']=='allowlist' and release:
        reg_path=release.parent/'figures_registry.jsonl'
        if reg_path.is_file():
            from release_gate import tom_publishable_media
            reg_rows=[json.loads(l) for l in reg_path.read_text(encoding='utf8').splitlines() if l.strip()]
            publishable={v:set(m) for v,m in tom_publishable_media(reg_rows,scope['volumes']).items()}
    unapproved=[];omitted_media=[]
    out.mkdir(parents=True,exist_ok=True)
    (out/'assets').mkdir(exist_ok=True)
    (out/'assets/style.css').write_text(STYLE+MEDIA_OMIT_CSS,encoding='utf8')
    (out/'assets/search.js').write_text(SEARCH_JS,encoding='utf8')
    (out/'assets/reader.js').write_text(READER_JS,encoding='utf8')
    (out/'data').mkdir(exist_ok=True)
    all_sections=[];all_chunks=[]
    # Detect colliding section titles across volumes ('Введение', frontmatter
    # etc.) up front so <title>/description can be disambiguated with the
    # volume + section id without touching authorial headings in the body.
    title_counts=collections.Counter()
    for v in scope['volumes']:
        book=corpus/f'tom-{v}'
        if not book.is_dir():raise SystemExit(f'Missing required volume {v}')
        secs=read_jsonl(book/'data/sections.jsonl')
        for s in secs:title_counts[s['title']]+=1
    for v in scope['volumes']:
        book=corpus/f'tom-{v}'
        secs=read_jsonl(book/'data/sections.jsonl')
        chunks=read_jsonl(book/'data/chunks.jsonl')
        all_sections.extend(secs);all_chunks.extend(chunks)
        od=out/f'tom-{v}'
        od.mkdir(exist_ok=True)
        for sec in secs:
            target=od/Path(sec['path']).with_suffix('.html')
            target.parent.mkdir(parents=True,exist_ok=True)
            src=book/sec['path']
            raw=header_removed(src.read_text(encoding='utf8'))
            content=redirect_markdown_links(MARKDOWN(raw))
            # Pandoc carries one broken relative news link from the original Word source.
            # Preserve visible anchor text without inventing a URL; flag in QA documentation.
            content=content.replace('<a href="../../%D0%94%D0%B5%D0%BB%D0%BE%D0%B2%D0%BE%D0%B9">“Деловой Петербург”</a>', '<span title="Недоступная относительная ссылка в исходном DOC">“Деловой Петербург”</span>')
            page=sec.get('pdf_page_candidate') or sec.get('pdf_page_start')
            page_label='Кандидат страницы PDF; требуется сверка' if page else 'Страница PDF не привязана'
            pdf_name=f'osnovy-sociologii-tom-{v}.pdf'
            # Source reference absolute relative to this section .html
            pref='../'*len(target.relative_to(od).parts[:-1])
            if mode=='internal' or scope['pdfs']=='ship':
                pdf_url=f'{pref}source/{pdf_name}'+(f'#page={page}' if page else '')
                pdf_link=f'<a href="{esc(pdf_url)}">Оригинальный PDF{(" · стр. " + str(page)) if page else ""}</a>'
            elif scope['pdfs']=='reference':
                # The canonical URL is the archive catalog page, not a direct
                # per-volume file; say so honestly and never append #page=.
                pdf_url=scope['canonical_pdf_url']
                pdf_link=(f'Внешний архив оригинальных публикаций: <a href="{esc(pdf_url)}" rel="noopener">выбрать том</a>'
                          f' · файл издания <code>{esc(pdf_name)}</code>'+(f' · стр. {page} (кандидат)' if page else '')
                          +' · соответствие редакции проверяется')
            else:
                pdf_url=''
                pdf_link=f'<code>{esc(pdf_name)}</code>'+(f' · стр. {page} (кандидат)' if page else '')
            # Metadata status: authors' text is not independent verification.
            meta=f'<p class="muted">Том {v} · {pdf_link} · {esc(page_label)} · источник: PDF/DOC данной редакции.</p>'
            # Permanent address of THIS section in the library is the primary
            # contextual source of the citation; the external archive is
            # supplementary provenance only.
            rel_page=f'tom-{v}/'+Path(sec['path']).with_suffix('.html').as_posix()
            sec_url=(base_url.rstrip('/')+'/'+rel_page) if base_url else rel_page
            cite_attrs=(f'data-title="{esc(sec["title"])}" data-volume="{v}"'
                        f' data-section="{esc(sec["id"])}" data-pdf="{esc(pdf_name)}"'
                        f' data-section-url="{esc(sec_url)}"'
                        + (f' data-archive="{esc(scope["canonical_pdf_url"])}"' if scope['pdfs']=='reference' else (f' data-archive="{esc(pdf_url)}"' if scope['pdfs']=='ship' else ''))
                        + (f' data-page="{page}"' if page else ''))
            cite=f'<button type="button" class="cite" {cite_attrs}>Копировать с источником</button><small class="muted"> копирует выделенное с библиографической ссылкой; сохраняйте контекст цитаты.</small>'
            ct=cite_by_sec.get(sec['id'])
            if ct:
                # Stable addressing layer chrome — the ct-id survives nav/
                # wording changes; it is NOT a separate page.
                cite+=(f'<small class="muted" id="{esc(ct["anchor"])}"> стабильный адрес цитирования: '
                       f'<code>{esc(ct["citation_target_id"])}</code></small>')
            nav=f'<p>{esc(sec["title"])}</p><p><a href="{pref}index.html">Оглавление тома {v}</a></p>'
            previous=sec.get('previous_path') or sec.get('previous');nxt=sec.get('next_path') or sec.get('next')
            def pageref(x):return esc(os.path.relpath(od/Path(x).with_suffix('.html'),target.parent).replace(os.sep,'/'))
            pager='<div class="pager"><span>'+ ('<a href="'+pageref(previous)+'">← Назад</a>' if previous and previous!='None' else '')+'</span><span>'+('<a href="'+pageref(nxt)+'">Вперёд →</a>' if nxt and nxt!='None' else '')+'</span></div>'
            rootpref=rel_to_root(target,out)
            foot={'internal':'<p class="notice"><strong>Примечание:</strong> материал передаёт позицию авторов соответствующей редакции. Исторические, научные и политические утверждения не являются автоматически подтверждёнными. Внутренний пилот: сверка схем и страниц продолжается.</p>',
                  'candidate':'<p class="notice"><strong>Кандидат выпуска — не для публикации.</strong> Состав файлов совпадает с будущей публичной сборкой; статус ревью проверяется отдельно.</p>'}.get(mode,'<p class="notice">Содержит авторские утверждения; их фактическая достоверность оценивается отдельно.</p>')
            if mode!='internal':
                # Media scope decides what happens to <img> references: under
                # 'allowlist' only approved files survive and the rest is a
                # gate violation; under 'none' every illustration is replaced
                # by a visible, honest omission note (never a silent drop).
                keep={(vv,n) for (vv,n) in allow if vv==v} if scope['media']=='allowlist' else set()
                replaced=[]
                content=_omit_media_imgs(content,keep,v,sec['id'],replaced)
                for ref in replaced:
                    name=ref.rsplit(':',1)[-1]
                    if scope['media']!='allowlist' or (publishable is not None and name not in publishable.get(v,())):
                        omitted_media.append(ref)
                    else:
                        unapproved.append(ref)
            # Same-titled sections in different volumes get disambiguating
            # context in <title>/description; authorial H1/body untouched.
            dup=title_counts[sec['title']]>1
            page_title=sec['title']+(f' — том {v}, раздел {sec["id"]}' if dup else '')
            desc=(f'Первичный текст: раздел «{sec["title"]}» — «Основы социологии», '
                  f'ВП СССР, том {v}'+(f', раздел {sec["id"]}' if dup else '')
                  +'. Полный раздел с контекстом, сносками и привязкой к изданию.')
            # Authorial markdown may already carry its own H1; only inject a
            # page H1 when the body has none (exactly one meaningful H1/page).
            h1='' if '<h1' in content else f'<h1>{esc(sec["title"])}</h1>'
            vol_index=(base_url.rstrip('/')+f'/tom-{v}/index.html') if base_url else f'{pref}index.html'
            sec_bc=sem.ld_breadcrumb([(f'Том {v}',vol_index),(sec['title'],sec_url)])
            target.write_text(standalone(page_title,
                f'<p class="breadcrumbs"><a href="{pref}index.html">Том {v}</a> / {esc(sec["title"])}</p>'
                +h1+meta+cite+foot+content+pager,
                nav,rootpref,reader=True,desc=desc,
                canonical=sec_url if base_url else None,
                jsonld=sem.ld_json([sec_bc])),encoding='utf8')
        if mode=='internal':
            shutil.copytree(book/'assets/media',od/'assets/media',dirs_exist_ok=True)
        elif scope['media']=='allowlist':
            mdest=od/'assets/media';mdest.mkdir(parents=True,exist_ok=True)
            for f in sorted((book/'assets/media').rglob('*')):
                if not f.is_file():continue
                want=allow.get((v,f.name))
                if want and _sha256(f)==want:shutil.copy2(f,mdest/f.name)
                elif publishable is not None and f.name not in publishable.get(v,()):
                    pass  # registry-decided non-publishable: never ships
                else:unapproved.append(f'tom-{v}:file:{f.name}')
        # scope 'none': ship no graphics at all (text-first release)
        if v==1 and mode=='internal': # Topic links reference additional PDF sources and evidence inside tom-1.
            shutil.copytree(book/'data',od/'data',dirs_exist_ok=True)
            if (book/'figures').is_dir():shutil.copytree(book/'figures',od/'figures',dirs_exist_ok=True)
            for extra in (book/'source').glob('*.pdf'):
                if extra.name!=f'osnovy-sociologii-tom-{v}.pdf':
                    (od/'source').mkdir(exist_ok=True)
                    shutil.copy2(extra,od/'source'/extra.name)
        if mode=='internal' or scope['pdfs']=='ship':
            src_pdf=book/'source'/f'osnovy-sociologii-tom-{v}.pdf'
            (od/'source').mkdir(exist_ok=True)
            shutil.copy2(src_pdf,od/'source'/src_pdf.name)
        # topic pages from annotated source (not authored chapters); they carry
        # status internal_review and reference out-of-scope PDFs, so they are
        # rendered only in internal builds
        top=book/'topics' if mode=='internal' else None
        if top is not None and top.exists():
            for x in top.glob('*.md'):
                target=od/'topics'/x.with_suffix('.html').name
                target.parent.mkdir(exist_ok=True)
                body=redirect_markdown_links(MARKDOWN(header_removed(x.read_text(encoding='utf8'))))
                target.write_text(standalone('Тематическая навигация',f'<p class="notice">Редакционная навигация, а не авторский текст. Проверяйте каждое утверждение в первоисточнике.</p>{body}',rootprefix=rel_to_root(target,out)),encoding='utf8')
        links='\n'.join(f'<li><a href="{esc(Path(s["path"]).with_suffix(".html").as_posix())}">{esc(s["title"])}</a> <small>· {esc(s["kind"])}</small></li>' for s in secs)
        content=f'<h1>Том {v}</h1><p>Авторский порядок чтения · {len(secs)} адресуемых единиц.</p><ol>{links}</ol>'
        vdesc=f'«Основы социологии» ВП СССР, том {v}: оглавление и полные разделы произведения в авторском порядке чтения.'
        v_index_url=(base_url.rstrip('/')+f'/tom-{v}/index.html') if base_url else None
        (od/'index.html').write_text(standalone(f'Основы социологии. Том {v}',content,rootprefix='../',desc=vdesc,
            canonical=v_index_url,
            jsonld=sem.ld_json([sem.ld_book(f'Основы социологии. Том {v}',
                v_index_url or f'tom-{v}/index.html')])),encoding='utf8')
    if mode=='public' and unapproved:
        raise SystemExit('Unapproved publishable media:\n- '+'\n- '.join(sorted(set(unapproved))[:50]))
    shutil.copy2(corpus/'data/works.jsonl',out/'data/works.jsonl')
    shutil.copy2(corpus/'data/sections.jsonl',out/'data/sections.jsonl')
    shutil.copy2(corpus/'data/chunks.jsonl',out/'data/chunks.jsonl')
    # Search index uses source text only and link to its enclosing unit.
    sec_lookup={s['id']:s for s in all_sections}
    idx=[]
    for c in all_chunks:
        unit_key=c.get('section_id') or c.get('unit_id')
        sec=sec_lookup.get(unit_key)
        if not sec:raise ValueError('Orphan retrieval chunk')
        idx.append({'title':sec['title'],'volume':int(sec['volume']),'section_id':sec['id'],
                    'url':f'tom-{sec["volume"]}/'+Path(sec['path']).with_suffix('.html').as_posix(),
                    'text':c['text'][:480]})
    topics_count=build_topics_page(all_sections,out,base_url)
    # --- Semantic layer: Topic/Concept pages + data exports -------------
    # Every emitted entity must pass the single indexable() policy; pages
    # are written only for indexable entities — unpublished/unsupported
    # records produce no page at all (not just noindex).
    sec_by_id={s['id']:s for s in all_sections}
    def sec_title(sid):
        s=sec_by_id.get(sid);return s['title'] if s else None
    def sec_text(sid):
        s=sec_by_id.get(sid)
        if not s:return None
        p=corpus/f'tom-{s["volume"]}'/s['path']
        return p.read_text(encoding='utf8') if p.is_file() else None
    def sec_page(sid):
        s=sec_by_id.get(sid)
        if not s:return None
        return f'tom-{s["volume"]}/'+Path(s['path']).with_suffix('.html').as_posix()
    b_abs=lambda p:(base_url.rstrip('/')+'/'+p) if base_url else None
    cite_by_ct={c['citation_target_id']:c for c in ed['citations']}
    ct_by_sec={c['section_id']:c for c in ed['citations']}
    # Concepts: verify evidence against real section titles/text; only
    # verified entries survive into the emitted record.
    published_concepts=[]
    for c in ed['concepts']:
        ev=sem.verify_evidence(c,sec_title,sec_text)
        if sem.indexable('concept',c,evidence=ev):
            published_concepts.append((c,ev))
    published_ids={c['concept_id'] for c,_ in published_concepts}
    # Topics: first-match membership (same rule as the rubric index —
    # a section is claimed by the first matching topic).
    used=set();published_topics=[]
    for t in ed['topics']:
        members=[s for s in sem.topic_members(t,all_sections)
                 if s['id'] not in used]
        for s in members:used.add(s['id'])
        if sem.indexable('topic',t,members=members):
            published_topics.append((t,members))
    sem_uris=[]
    concept_cards=[]
    # Topic detail pages breadcrumb up to the rubric index — but that page
    # only exists when corpus titles prove at least one rubric; otherwise
    # the valid parent is the landing page.
    topics_parent=b_abs('topics.html') if topics_count else b_abs('index.html')
    topics_parent_label='Темы и разделы' if topics_count else '«Основы социологии»'
    topics_parent_href='../topics.html' if topics_count else '../index.html'
    if published_concepts:
        (out/'concepts').mkdir(exist_ok=True)
        clinks=[]
        for c,ev in published_concepts:
            tname=c['canonical_term'];cid=c['concept_id']
            page=f'concepts/{cid}.html'
            scope_ru={'corpus_term':'термин корпуса (авторская терминология источников)',
                      'editorial_navigation':'редакционный навигационный термин',
                      'established_term':'общеупотребительный термин'}[c['definition_scope']]
            alias_html=(f'<p><small class="muted">Также встречается: '
                        f'{esc(", ".join(c.get("aliases") or []))}</small></p>'
                        if c.get('aliases') else '')
            ev_items=[]
            for e in ev:
                url=sec_page(e['section_id'])
                ttl=sec_title(e['section_id']) or e['section_id']
                ctid=ct_by_sec.get(e['section_id'])
                ctsmall=(f' <small>· <code>{esc(ctid["citation_target_id"])}</code></small>'
                         if ctid else '')
                ev_items.append(f'<li><a href="../{esc(url)}">{esc(ttl)}</a>{ctsmall}</li>'
                                if url else f'<li>{esc(ttl)} <small>(раздел вне сайта)</small></li>')
            topic_names=[t['label'] for t,_ in published_topics
                         if t['topic_id'] in (c.get('related_topic_ids') or [])]
            topic_items=''.join(f'<li><a href="../topics/{esc(t["topic_id"])}.html">{esc(t["label"])}</a></li>'
                                for t,_ in published_topics
                                if t['topic_id'] in (c.get('related_topic_ids') or []))
            pc_by_id={x['concept_id']:x for x,_ in published_concepts}
            rel_items=''.join(f'<li><a href="{esc(r)}.html">{esc(pc_by_id[r]["canonical_term"])}</a></li>'
                              for r in (c.get('related_concept_ids') or []) if r in pc_by_id)
            bc=sem.ld_breadcrumb([('Библиотека',b_abs('index.html')),
                                  ('Понятия',b_abs('concepts/index.html')),
                                  (tname,b_abs(page))])
            lds=[bc,sem.ld_definedterm(c,b_abs(page) or page,
                                       b_abs('concepts/index.html') or 'concepts/index.html')]
            body=(f'<p class="breadcrumbs"><a href="../index.html">Библиотека</a> / '
                  f'<a href="index.html">Понятия</a> / {esc(tname)}</p>'
                  f'<h1>{esc(tname)}</h1>'
                  f'<p class="notice"><strong>Редакционная навигация, а не авторский текст.</strong> '
                  f'Категория термина: <em>{scope_ru}</em>. Описание термина — редакционное; '
                  f'содержательные утверждения смотрите в приведённых разделах источника.</p>'
                  +alias_html
                  +f'<p>{esc(c.get("editorial_description") or "")}</p>'
                  +f'<h2>Источниковая база ({len(ev)})</h2><ol>{"".join(ev_items)}</ol>'
                  +(f'<h2>Рубрики</h2><ul>{topic_items}</ul>' if topic_items else '')
                  +(f'<h2>Связанные понятия</h2><ul>{rel_items}</ul>' if rel_items else ''))
            (out/page).write_text(standalone(f'{tname} — понятие корпуса',body,
                rootprefix='../',desc=(c.get('editorial_description') or tname)[:250],
                canonical=b_abs(page),jsonld=sem.ld_json(lds)),encoding='utf8')
            sem_uris.append('/'+page)
            clinks.append(f'<li><a href="{esc(cid)}.html"><strong>{esc(tname)}</strong></a>'
                          f' <small class="muted">· {scope_ru} · {len(ev)} раздел(а)</small></li>')
            concept_cards.append(c)
        # Glossary page: DefinedTermSet over the published terms only.
        termset=[sem.ld_definedterm(c,b_abs(f'concepts/{c["concept_id"]}.html') or
                                    f'concepts/{c["concept_id"]}.html',
                                    b_abs('concepts/index.html') or 'concepts/index.html')
                 for c,_ in published_concepts]
        cbc=sem.ld_breadcrumb([('Библиотека',b_abs('index.html')),
                               ('Понятия',b_abs('concepts/index.html'))])
        cbody=('<p class="breadcrumbs"><a href="../index.html">Библиотека</a> / Понятия</p>'
               '<h1>Понятия корпуса</h1>'
               '<p class="notice"><strong>Редакционный указатель терминов, а не авторский текст.</strong> '
               'Включено только то, что подтверждено реальными разделами корпуса и прошло '
               'редакционную проверку; отсутствие термина означает лишь отсутствие подтверждённой '
               'записи, а не отсутствие темы в корпусе.</p>'
               f'<ol>{"".join(clinks)}</ol>')
        (out/'concepts/index.html').write_text(standalone('Понятия корпуса',cbody,
            rootprefix='../',desc='Указатель подтверждённых корпусом терминов: определения — редакционные, доказательная база — разделы источника.',
            canonical=b_abs('concepts/index.html'),
            jsonld=sem.ld_json([cbc,sem.ld_definedtermset('Понятия корпуса',
                b_abs('concepts/index.html') or 'concepts/index.html',termset)])),
            encoding='utf8')
        sem_uris.append('/concepts/index.html')
    if published_topics:
        (out/'topics').mkdir(exist_ok=True)
        for t,members in published_topics:
            page=f'topics/{t["topic_id"]}.html'
            concept_links=''.join(
                f'<li><a href="../concepts/{esc(c["concept_id"])}.html">{esc(c["canonical_term"])}</a></li>'
                for c,_ in published_concepts
                if t['topic_id'] in (c.get('related_topic_ids') or []))
            items='\n'.join(
                f'<li><a href="../{esc(sec_page(s["id"]))}">{esc(s["title"])}</a> '
                f'<small>· том {s["volume"]}</small></li>' for s in members)
            bc=sem.ld_breadcrumb([('Библиотека',b_abs('index.html')),
                                  ('Темы' if topics_count else topics_parent_label,
                                   topics_parent),
                                  (t['label'],b_abs(page))])
            lds=[bc,sem.ld_collection(t['label'],b_abs(page) or page,
                    [{'@type':'ListItem','position':i+1,
                      'url':b_abs(sec_page(s['id'])) or sec_page(s['id']),
                      'name':s['title']} for i,s in enumerate(members)],
                    t.get('description') or '')]
            body=(f'<p class="breadcrumbs"><a href="../index.html">Библиотека</a> / '
                  f'<a href="{topics_parent_href}">{esc(topics_parent_label if not topics_count else "Темы")}</a> / {esc(t["label"])}</p>'
                  f'<h1>{esc(t["label"])}</h1>'
                  '<p class="notice"><strong>Редакционная рубрика, а не авторский текст.</strong> '
                  'Состав выведен из реальных заголовков разделов источника; включение раздела '
                  'не является утверждением истинности его содержания.</p>'
                  +f'<p>{esc(t.get("description") or "")}</p>'
                  +f'<h2>Разделы ({len(members)})</h2><ol>{items}</ol>'
                  +(f'<h2>Понятия рубрики</h2><ul>{concept_links}</ul>' if concept_links else ''))
            (out/page).write_text(standalone(t['label'],body,rootprefix='../',
                desc=(t.get('description') or t['label'])[:250],
                canonical=b_abs(page),jsonld=sem.ld_json(lds)),encoding='utf8')
            sem_uris.append('/'+page)
    # Machine-readable semantic export + stable citation ledger. Derived
    # data only — never an independent citation source.
    entity_rows=[]
    for t,members in published_topics:
        entity_rows.append({'kind':'topic','id':t['topic_id'],'label':t['label'],
            'members':len(members),'site_path':f'topics/{t["topic_id"]}.html',
            'membership_rule':t['membership_rule'],
            'membership_rule_version':t['membership_rule_version']})
    for c,ev in published_concepts:
        entity_rows.append({'kind':'concept','id':c['concept_id'],
            'term':c['canonical_term'],'aliases':c.get('aliases') or [],
            'definition_scope':c['definition_scope'],
            'site_path':f'concepts/{c["concept_id"]}.html',
            'evidence_section_ids':[e['section_id'] for e in ev],
            'related_topic_ids':c.get('related_topic_ids') or [],
            'related_concept_ids':[r for r in (c.get('related_concept_ids') or [])
                                   if r in published_ids]})
    for ct in ed['citations']:
        entity_rows.append({'kind':'citation_target','id':ct['citation_target_id'],
            'work_id':ct['work_id'],'section_id':ct['section_id'],
            'anchor':ct['anchor'],'site_path':ct.get('site_path'),
            'language':ct['language']})
    for v in ed['variants']:
        entity_rows.append({'kind':'language_variant','id':v['variant_id'],
            'entity_id':v['entity_id'],'language':v['language'],
            'canonical_name':v['canonical_name'],
            'translation_status':v['translation_status'],
            'review_status':v['review_status'],
            'canonical_variant_id':v.get('canonical_variant_id'),
            'indexable':sem.indexable('language_variant',v)})
    # retrieval entries: published concepts are discoverable through the same
    # site search surface as sections (retrieval-only: kind='concept').
    for c,_ev in published_concepts:
        idx.append({'title':c['canonical_term'],'kind':'concept',
                    'volume':'','section_id':c['concept_id'],
                    'url':f'concepts/{c["concept_id"]}.html',
                    'text':((c.get('editorial_description') or '')+' '
                            +' '.join(c.get('aliases') or []))[:480]})
    (out/'data/search_index.json').write_text(
        json.dumps(idx,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    (out/'data/entities.json').write_text(
        json.dumps({'generated_by':'semantic_layer','entities':entity_rows},
                   ensure_ascii=False,indent=1)+'\n',encoding='utf8')
    sem.write_jsonl(out/'data/citations.jsonl',ed['citations'])
    topics_link=('<p><a href="topics.html">Темы и разделы</a> <small>(редакционная навигация)</small></p>'
                 if topics_count else '')
    concepts_link=('<p><a href="concepts/index.html">Понятия корпуса</a> '
                   f'<small>({len(published_concepts)} подтверждённых термина, редакционный указатель)</small></p>'
                   if published_concepts else '')
    topic_detail_links=''.join(
        f'<p>· <a href="topics/{esc(t["topic_id"])}.html">{esc(t["label"])}</a></p>'
        for t,_ in published_topics)
    vol_links='\n'.join(f'<div class="card"><a href="tom-{v}/index.html"><strong>Том {v}</strong></a> · Оглавление и разделы</div>' for v in scope['volumes'])
    disclaimer={'internal':'<p class="notice"><strong>Внутренняя версия.</strong> Тексты и иллюстрации не прошли полную визуальную и правовую приёмку. Этот экземпляр не предназначен для общедоступного размещения.</p>',
                'candidate':'<p class="notice"><strong>Кандидат выпуска — не для публикации.</strong> Состав соответствует публичному режиму; релиз требует одобрения владельца и прохождения release gate.</p>'}.get(mode,'<p>Исследовательская библиотека первоисточников: изложенные в работах взгляды являются позицией их авторов.</p>')
    # Honest composition disclosure: a text-first release must say what it omits.
    compose=[]
    if mode!='internal':
        if scope['media']=='none':
            compose.append('<p class="notice"><strong>Состав выпуска — текстовая редакция.</strong> Включены полные авторские тексты разделов. Иллюстрации и обложки <em>не включены</em>: там, где место изображения известно разметке, стоит явная пометка. Часть графики источника (записи очередей медиа и встроенные OLE-объекты DOC) <em>не привязана к позиции в тексте и отсутствует без точечной пометки</em>; это ограничение выпуска, а не полная текстово-графическая редакция.</p>')
        if scope['pdfs']=='reference':
            compose.append(f'<p class="notice">Файлы PDF в этот выпуск не включены; разделы ссылаются на <a href="{esc(scope["canonical_pdf_url"])}" rel="noopener">внешний архив оригинальных публикаций</a> (выберите том; соответствие редакции проверяется), номера страниц помечены как кандидаты.</p>')
        elif scope['pdfs']=='none':
            compose.append('<p class="notice">Файлы PDF в этот выпуск не включены; указаны имена файлов и кандидатные страницы проверенного издания.</p>')
    topics_block='<h2>Тематические маршруты</h2><p><a href="tom-1/topics/social-time-technological-change.html">Социальное время и технологические изменения</a> <small>(редакционный указатель, не оригинальный авторский текст)</small></p>' if mode=='internal' else ''
    indexpage=f'<h1>«Основы социологии»: {len(scope["volumes"])} томов</h1>{disclaimer}{"".join(compose)}<p>Текст открыт для последовательного чтения и полнотекстового поиска. Указанные страницы PDF в неподтверждённых записях являются автоматическими кандидатами.</p><input id="query" placeholder="Найти в корпусе: культура, управление, социальное время..." aria-label="Поиск по корпусу"><small id="loading">Загрузка поискового индекса...</small><div id="results" aria-live="polite"></div><script src="assets/search.js" defer></script>{topics_block}{topics_link}<h2>Оглавления</h2>{vol_links}{topic_detail_links}{concepts_link}<h2>Данные для ИИ</h2><p><a href="data/works.jsonl">Реестр произведений</a> · <a href="data/sections.jsonl">Разделы</a> · <a href="data/chunks.jsonl">Поисковые фрагменты</a> · <a href="data/entities.json">Семантический слой</a> · <a href="data/citations.jsonl">Адреса цитирования</a></p><p>Автоматическая проверка нахождения текста в файле не доказывает истинность описанных в нём утверждений.</p>'
    index_desc='Исследовательская библиотека первоисточников: «Основы социологии» ВП СССР — полные разделы шести томов, полнотекстовый поиск, цитирование с указанием источника.'
    landing_url=(base_url.rstrip('/')+'/index.html') if base_url else 'index.html'
    datasets=[sem.ld_dataset(f'Основы социологии. Том {v}',
                             (base_url.rstrip('/')+f'/tom-{v}/index.html') if base_url else f'tom-{v}/index.html',
                             'Полные разделы тома с привязкой к изданию')
              for v in scope['volumes']]
    datasets.append(sem.ld_dataset('Машинные индексы корпуса',
        landing_url,'Реестры works/sections/chunks и семантический слой в JSON',
        downloads=[((base_url.rstrip('/')+'/data/works.jsonl') if base_url else 'data/works.jsonl','application/jsonlines'),
                   ((base_url.rstrip('/')+'/data/entities.json') if base_url else 'data/entities.json','application/json')]))
    landing_ld=sem.ld_json([sem.ld_datacatalog('Библиотека первоисточников ВП СССР',
        landing_url,datasets,index_desc)])
    (out/'index.html').write_text(standalone('«Основы социологии» ВП СССР — библиотека первоисточников',indexpage,rootprefix='',robots='index,follow' if mode=='public' else 'noindex,nofollow',desc=index_desc,
        canonical=(base_url.rstrip('/')+'/index.html') if base_url else None,jsonld=landing_ld),encoding='utf8')
    (out/'.nojekyll').write_text('',encoding='utf8')
    # llms.txt uses relative links — absolute host-root paths would escape a
    # GitHub Pages project subpath (/repo/).
    (out/'llms.txt').write_text('# Исследовательская библиотека: Основы социологии\n\nНейтральный архив авторских текстов. Все социальные и политические утверждения — позиции авторов, если внешняя проверка не дана.\n\n- index.html — оглавление и поиск\n- topics.html — редакционная навигация по темам\n- data/works.jsonl — произведения\n- data/sections.jsonl — структура\n- data/chunks.jsonl — полный индексированный текст\n',encoding='utf8')
    if mode in ('internal','candidate'):
        (out/'robots.txt').write_text('User-agent: *\nDisallow: /\n',encoding='utf8')
    elif mode=='public':
        # base_url is guaranteed https://<origin> (required above); it already
        # carries the GitHub Pages project subpath when applicable.
        base_url=base_url.rstrip('/')
        uris=['/index.html']+(['/topics.html'] if topics_count else [])+[f'/tom-{v}/index.html' for v in scope['volumes']]
        for s in all_sections:uris.append(f'/tom-{s["volume"]}/'+Path(s['path']).with_suffix('.html').as_posix())
        # Semantic pages were emitted only for indexable entities — the
        # same predicate that admitted the page admits it to the sitemap.
        uris+=sem_uris
        doc=''.join('<url><loc>'+esc(base_url+p)+'</loc></url>' for p in uris)
        (out/'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+doc+'</urlset>',encoding='utf8')
        (out/'robots.txt').write_text(f'User-agent: *\nAllow: /\nSitemap: {esc(base_url)}/sitemap.xml\n',encoding='utf8')
    missing=audit_internal_links(out)
    print(json.dumps({'mode':mode,'volumes':len(scope['volumes']),'pages':len(all_sections),'chunks':len(idx),
                      'semantic':{'topics':len(published_topics),
                                  'concepts':len(published_concepts),
                                  'citation_targets':len(ed['citations']),
                                  'language_variants':len(ed['variants'])},
                      'scope':{k:scope[k] for k in ('media','pdfs')},
                      'unapproved_media_refs':sorted(set(unapproved)) if mode!='internal' else [],
                      'omitted_media':sorted(set(omitted_media)) if mode!='internal' else [],
                      'missing_file_refs':missing,
                      'output':str(out)},ensure_ascii=False))
    if mode=='public' and missing:
        raise SystemExit('Public artifact has links to missing files:\n- '+'\n- '.join(missing[:50]))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--corpus',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--mode',choices=['internal','public','candidate'],default='internal');ap.add_argument('--release-approval',type=Path);ap.add_argument('--base-url',default='');ap.add_argument('--editorial',type=Path,default=None);args=ap.parse_args()
    build(args.corpus,args.output,args.mode,args.release_approval,args.base_url,args.editorial)
