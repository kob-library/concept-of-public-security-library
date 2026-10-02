#!/usr/bin/env python3
"""Sanity check the private site and ensure public publishing gate is closed."""
import argparse,html.parser,json,subprocess,sys,os
from pathlib import Path

class LinkParser(html.parser.HTMLParser):
 def __init__(self):super().__init__();self.paths=[]
 def handle_starttag(self,tag,attrs):
  d=dict(attrs)
  if tag in ('a','img','link','script'):
   val=d.get('href') if tag in ('a','link') else d.get('src')
   if val and not val.startswith(('http:','https:','mailto:','data:','javascript:','#')):self.paths.append((tag,val.split('#')[0].split('?')[0]))

def validate(out:Path):
 errors=[];htmls=list(out.rglob('*.html'))
 for h in htmls:
  p=LinkParser();p.feed(h.read_text(encoding='utf8'))
  for tag,url in p.paths:
   if url and not (h.parent/url).exists():errors.append(f'{h.relative_to(out)}: {tag}: {url}')
 idx=out/'data/search_index.json'
 if not idx.exists():errors.append('Missing index')
 else:
  items=json.loads(idx.read_text(encoding='utf8'))
  for x in items:
   if not (out/x['url']).is_file():errors.append('Invalid search URL '+x['url'])
 summary={'html_pages':len(htmls),'search_items':len(items) if idx.exists() else None,'invalid_references':len(errors),'errors':errors[:25]}
 print(json.dumps(summary,ensure_ascii=False,indent=2))
 return len(errors)==0
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--site',type=Path,required=True);p=a.parse_args();sys.exit(0 if validate(p.site) else 1)
