#!/usr/bin/env python3
"""Structural QA for automatically transcribed volumes; NOT visual verification."""
import argparse,hashlib,json,re,sys,zipfile
from pathlib import Path
import yaml

def digest(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for chunk in iter(lambda:f.read(2**20),b''):h.update(chunk)
 return h.hexdigest()

def loadjsonl(f):return [json.loads(x) for x in f.read_text(encoding='utf-8').splitlines() if x.strip()]

RE_FM_BODY=re.compile(r'\A---\n.*?\n---\n\n',re.S)
RE_MD_IMG=re.compile(r'!\[[^\]]*\]\(([^)\s]+)[^)]*\)')
RE_HTML_IMG=re.compile(r'<img\s+[^>]*src="([^"]+)"[^>]*>')

def norm_media_markup(t):
    """Equalize image markup forms; the referenced media basename is preserved
    so a deleted image reference still counts as a difference."""
    t=RE_MD_IMG.sub(lambda m:'IMG['+m.group(1).split('/')[-1].split('#')[0].split('?')[0]+']',t)
    t=RE_HTML_IMG.sub(lambda m:'IMG['+m.group(1).split('/')[-1].split('?')[0]+']',t)
    return t

def validate_one(root):
 man=json.loads((root/'data/manifest.json').read_text(encoding='utf-8'))
 v=man['volume'];prefix=f'osnovy-sociologii-tom-{v}'
 sections=loadjsonl(root/'data/sections.jsonl')
 chunks=loadjsonl(root/'data/chunks.jsonl')
 notes=loadjsonl(root/'data/footnotes.jsonl')
 figure_index=loadjsonl(root/'data/figures.jsonl')
 anchor_index=loadjsonl(root/'data/media_anchors.jsonl')
 errors=[];warnings=[]
 if digest(root/'source'/f'{prefix}.doc')!=man['doc_sha256']:errors.append('DOC SHA mismatch')
 if digest(root/'source'/f'{prefix}.pdf')!=man['pdf_sha256']:errors.append('PDF SHA mismatch')
 actual=(root/'data/raw_extracted_main.md').read_text(encoding='utf-8')
 if hashlib.sha256(actual.encode()).hexdigest()!=man['unmodified_extracted_main_sha256']:errors.append('Main raw text SHA mismatch')
 all_footnotes={x['id']:x for x in notes}
 if len(all_footnotes)!=len(notes):errors.append('Duplicate note definitions')
 from collections import defaultdict
 grouped=defaultdict(list)
 for c in chunks:grouped[c['section_id']].append(c)
 for s in sections:
  p=root/s['path']
  if not p.exists():errors.append('missing section '+s['path']);continue
  content=p.read_text(encoding='utf-8')
  ma=re.match(r'\A---\n(.*?)\n---\n',content,re.S)
  if not ma:errors.append('missing yaml '+s['path']);continue
  meta=yaml.safe_load(ma[1]);
  if meta['source_doc_sha256']!=man['doc_sha256'] or meta['source_pdf_sha256']!=man['pdf_sha256']:errors.append('source metadata mismatch '+s['path'])
  if meta['pdf_page_1_based_candidate']!=s['pdf_page_candidate']:errors.append('page metadata mismatch '+s['path'])
  if s['pdf_page_candidate'] is not None and not (1<=s['pdf_page_candidate']<=man['pdf_pages']):errors.append('out-of-bound PDF page '+s['path'])
  cs=grouped[s['id']];joined=''.join(c['text'] for c in cs)
  if joined!=s['text']:errors.append('loss of retrieval chunks '+s['path'])
  at=0
  for c in cs:
   if c['start_char']!=at or c['end_char']!=at+len(c['text']):errors.append('chunk discontinuity '+s['path'])
   at=c['end_char']
   if c['pdf_page_candidate']!=s['pdf_page_candidate']:errors.append('chunk page mismatch '+s['path'])
  if s['footnote_numbers']!=meta['footnote_numbers']:errors.append('footnotes yaml mismatch '+s['path'])
  if set(s['footnote_numbers'])-all_footnotes.keys():errors.append('missing note definitions '+s['path'])
  # Technical local image links, not URL anchors; only for our automatically inserted images.
  for lnk in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',content):
   if re.match(r'^(?:https?://|data:)',lnk):continue
   if not (p.parent/lnk.split('#')[0]).exists():errors.append('missing local image '+s['path']+' -> '+lnk)
  # The Markdown body actually rendered into the site must equal the indexed
  # section text plus its footnote definitions (audit finding F2). Image
  # markup form is normalized; a removed paragraph/footnote/image is an error.
  body=RE_FM_BODY.sub('',content,count=1)
  defs='\n\n'.join(all_footnotes[n]['definition_raw'] for n in s['footnote_numbers'] if n in all_footnotes)
  expected=s['text'].rstrip()+'\n'+('\n'+defs+'\n' if defs else '')
  if norm_media_markup(body)!=norm_media_markup(expected):
   errors.append('published Markdown body diverges from indexed text '+s['path'])
 if not set(s['id'] for s in sections)==set(grouped):errors.append('unmatched chunk section IDs')
 refs=[int(z) for s in sections for z in s['footnote_numbers']]
 if sorted(refs)!=sorted(all_footnotes):errors.append('footnote lost/duplication')
 media={f.name for f in (root/'assets/media_original').iterdir() if f.is_file()}
 seen_media={x['media_name'] for x in figure_index}
 if seen_media-media:errors.append('figure links to missing original media')
 if any(x['image'] not in media for x in anchor_index):errors.append('anchor links to missing media')
 if any(not (root/x['preview_path']).is_file() for x in anchor_index if x['preview_path']):errors.append('anchor preview missing')
 # Every media item, even if not inserted inline, must remain recoverable.
 if any(not (root/'assets/media_original'/m).exists() for m in media):errors.append('media preservation failure')
 visible={x['image'] for x in anchor_index}|seen_media
 if media-visible:warnings.append('media items without DOCX paragraph anchors: '+str(sorted(media-visible)))
 qa=json.loads((root/'data/qa.json').read_text(encoding='utf-8'))
 if qa['omitted_inline_media_unique']:warnings.append(str(qa['omitted_inline_media_unique'])+' unique media not inline in Markdown')
 if qa['OLE_objects']:warnings.append(str(qa['OLE_objects'])+' embedded OLE items need PDF visual check')
 return {'volume':v,'structural_test':'PASS' if not errors else 'FAIL','error_count':len(errors),'errors':errors[:30],
   'warnings':warnings,'sections':len(sections),'chunks':len(chunks),'footnotes':len(notes),
   'images_in_docx':len(media),'source_media_inline':len(seen_media),
   'source_media_not_inline':qa['omitted_inline_media_unique'],
   'images_with_docx_context':qa['omitted_inline_media_locatable_to_unit_unique'],
   'PDF_page_candidates_context':qa['page_mapping_statuses_v2'].get('auto_heading_plus_context_candidate',0),
   'visual_complete':False,'publication_ready':False}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('/mnt/data/kob_books_v0_4'));p.add_argument('--volumes',nargs='+',type=int,default=[2,3,4,5,6])
 # Publish preflight: warnings about untracked publishable media/OLE/page
 # candidates must block, not just inform. Source metrics stay unchanged.
 p.add_argument('--publish-strict',action='store_true',help='treat warnings as release-blocking failures')
 a=p.parse_args()
 results=[validate_one(a.root/f'tom-{v}') for v in a.volumes]
 if a.publish_strict:
  for r in results:
   if r['warnings']:r['errors']=r['errors']+['publish-strict: '+w for w in r['warnings']];r['structural_test']='FAIL'
 (a.root/'VALIDATION.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
 for r in results:print(json.dumps(r,ensure_ascii=False),flush=True)
 if any(x['structural_test']=='FAIL' for x in results):sys.exit(1)
