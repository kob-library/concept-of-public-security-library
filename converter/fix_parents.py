import json,re,yaml,argparse
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('/mnt/data/kob_books_v0_4'));p.add_argument('--volumes',nargs='+',type=int,default=[2,3,4,5,6]);args=p.parse_args()
for v in args.volumes:
 root=args.root/f'tom-{v}'
 p=root/'data/sections.jsonl';rows=[json.loads(z) for z in p.read_text(encoding='utf-8').splitlines()]
 for i,r in enumerate(rows):
  if r['kind'] not in ('chapter','section') or r['page_mapping_status']!='auto_heading_only_candidate':continue
  later=[]
  for child in rows[i+1:]:
   if r['kind']=='chapter' and child['kind'] in ('chapter','appendix'):break
   if r['kind']=='section' and child['kind'] not in ('subsection',):break
   if child['page_mapping_status']=='auto_heading_plus_context_candidate' and child['pdf_page_candidate'] is not None:
    later.append(child['pdf_page_candidate']);break
  if later and r['pdf_page_candidate'] is not None and later[0] > r['pdf_page_candidate'] and later[0]-r['pdf_page_candidate']<=6:
   r['pdf_page_candidate']=later[0]
   r['page_mapping_status']='auto_derived_from_child_needs_review'
 for r in rows:
  pp=root/r['path'];md=pp.read_text(encoding='utf-8')
  m=re.match(r'\A---\n(.*?)\n---\n',md,re.S)
  meta=yaml.safe_load(m[1]);meta['pdf_page_1_based_candidate']=r['pdf_page_candidate'];meta['pdf_page_status']=r['page_mapping_status']
  pp.write_text('---\n'+yaml.safe_dump(meta,sort_keys=False,allow_unicode=True,width=110).rstrip()+'\n---\n'+md[m.end():],encoding='utf-8')
 p.write_text(''.join(json.dumps(z,ensure_ascii=False)+'\n' for z in rows),encoding='utf-8')
 chunks=root/'data/chunks.jsonl';arr=[json.loads(z) for z in chunks.read_text(encoding='utf-8').splitlines()];idx={z['id']:z for z in rows}
 for c in arr:c['pdf_page_candidate']=idx[c['section_id']]['pdf_page_candidate']
 chunks.write_text(''.join(json.dumps(z,ensure_ascii=False)+'\n' for z in arr),encoding='utf-8')
 figs=root/'data/figures.jsonl';arr=[json.loads(z) for z in figs.read_text(encoding='utf-8').splitlines()];idx={z['path']:z for z in rows}
 for c in arr:c['pdf_page_candidate']=idx[c['unit_path']]['pdf_page_candidate']
 figs.write_text(''.join(json.dumps(z,ensure_ascii=False)+'\n' for z in arr),encoding='utf-8')
 print(v,[(r['title'][:45],r['pdf_page_candidate'],r['page_mapping_status']) for r in rows if r['page_mapping_status']=='auto_derived_from_child_needs_review'])
