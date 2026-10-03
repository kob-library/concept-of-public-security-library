
const area=document.getElementById('results');
const input=document.getElementById('query');
let index=[];
fetch('data/search_index.json').then(r=>r.json()).then(x=>{index=x;document.getElementById('loading').textContent='Готово к поиску: '+index.length+' текстовых фрагментов';}).catch(()=>{document.getElementById('loading').textContent='Ошибка загрузки индекса. При открытии с file:// запустите локальный HTTP-сервер.';});
function el(tag,text,klass){const e=document.createElement(tag);if(text)e.textContent=text;if(klass)e.className=klass;return e;}
function doSearch(){const q=input.value.toLocaleLowerCase('ru').trim().split(/\s+/).filter(Boolean);area.replaceChildren();if(!q.length)return;
const hits=[];for(const item of index){const text=(item.title+' '+item.text).toLocaleLowerCase('ru');if(q.every(w=>text.includes(w))){const score=q.reduce((s,w)=>s+(item.title.toLocaleLowerCase('ru').includes(w)?10:1),0);hits.push({item,score});}}
hits.sort((a,b)=>b.score-a.score);area.append(el('p','Найдено: '+hits.length+'. Показаны первые 50 совпадений.','muted'));
for(const v of hits.slice(0,50)){const block=el('div',null,'search-result');const a=el('a',v.item.title);if(/^[a-z][a-z0-9+.-]*:/i.test(v.item.url))continue;a.href=v.item.url;const par=el('p',v.item.text.slice(0,380));const sub=el('small',v.item.kind==='concept'?('понятие · '+v.item.section_id):('Том '+v.item.volume+' · '+v.item.section_id));block.append(a,par,sub);area.append(block);}}
input.addEventListener('input',doSearch);
