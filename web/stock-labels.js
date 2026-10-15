'use strict';
(() => {
 let data={stocks:{}},queued=false;
 const excluded='script,style,pre,code,textarea,input,select,option,.stock-identity,.stock-cap,.cap-source-note';
 const labels=new Set(['Large cap','Mid cap','Small cap']);
 function badge(symbol){
  const item=data.stocks[symbol],label=labels.has(item?.label)?item.label:'Unclassified';
  const el=document.createElement('small');el.className='stock-cap '+label.toLowerCase().replaceAll(' ','-');el.textContent=label;
  el.title=label==='Unclassified'?'No verified ISIN match in the latest published AMFI list.':`AMFI: ${label}; rank ${item.rank}; six-month period ending ${data.period}. Latest published classification, including on historical reports.`;
  el.setAttribute('aria-label',`${symbol}: ${el.title}`);return el;
 }
 function identity(text,symbol){
  const el=document.createElement('span');el.className='stock-identity';el.dataset.capSymbol=symbol;
  el.append(document.createTextNode(text),badge(symbol));return el;
 }
 function annotate(){
  queued=false;observer.disconnect();
  // Explicit stock references cover current UI, including names inside activity messages.
  document.querySelectorAll('[data-stock-symbol]').forEach(el=>{
   if(!el.querySelector('.stock-cap'))el.append(badge(el.dataset.stockSymbol));
  });
  const aliases=new Map();
  Object.entries(data.stocks).forEach(([symbol,item])=>{aliases.set(symbol,symbol);if(item.name&&item.name!==symbol){if(!aliases.has(item.name))aliases.set(item.name,symbol);else if(aliases.get(item.name)!==symbol)aliases.set(item.name,null);}});
  // Also cover saved reports and dynamically rebuilt tables, without touching evidence/code.
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode(node){
   return node.parentElement&&!node.parentElement.closest(excluded+', [data-stock-symbol]')&&node.textContent.trim()?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT;
  }});const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  nodes.forEach(node=>{
   const raw=node.textContent,trim=raw.trim(),clean=trim.replace(/^★\s*/,''),symbol=aliases.get(clean);
   if(symbol){node.replaceWith(identity(raw,symbol));return;}
   // Lists and event headers use commas or middle dots as explicit separators.
   const parts=raw.split(/([,·]\s*)/);if(parts.length<3)return;
   if(!parts.some((p,i)=>i%2===0&&aliases.get(p.trim())))return;
   const out=document.createDocumentFragment();parts.forEach((p,i)=>{const s=i%2===0?aliases.get(p.trim()):null;out.append(s?identity(p,s):document.createTextNode(p));});node.replaceWith(out);
  });
  // A historical symbol missing from today's database still gets an explicit unknown label.
  document.querySelectorAll('table').forEach(table=>{
   const headers=Array.from(table.querySelectorAll('thead th'));const columns=headers.map((h,i)=>/^(stock|symbol|company|time \/ symbol)$/i.test(h.textContent.trim())?i:-1).filter(i=>i>=0);
   table.querySelectorAll('tbody tr').forEach(row=>columns.forEach(i=>{
    const cell=row.cells[i];if(!cell||cell.querySelector('.stock-cap'))return;
    const leaf=cell.querySelector('strong')||cell;const text=Array.from(leaf.childNodes).find(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim());
    const s=text?.textContent.trim().replace(/^★\s*/,'');if(s&&/^[A-Z0-9&_.-]{2,30}$/.test(s))text.replaceWith(identity(text.textContent,s));
   }));
  });
  observer.observe(document.body,{childList:true,subtree:true,characterData:true});
 }
 const observer=new MutationObserver(()=>{if(!queued){queued=true;requestAnimationFrame(annotate);}});
 async function load(){
  try{const response=await fetch('/api/stock-classifications');if(!response.ok)throw Error();data=await response.json();}
  catch{if(!data.period)data={stocks:{}};}
  let note=document.querySelector('.cap-source-note');if(!note){note=document.createElement('p');note.className='cap-source-note';(document.querySelector('main')||document.body).append(note);}
  note.replaceChildren(document.createTextNode(data.period?`Size labels: latest published AMFI list, period ending ${data.period}. Historical reports also show this classification. `:'Size labels unavailable. Stocks remain unclassified. '));
  const link=document.createElement('a');link.href='https://www.amfiindia.com/otherdata/categorisation-of-stocks';link.target='_blank';link.rel='noopener noreferrer';link.textContent='AMFI source and definitions';note.append(link);
  observer.disconnect();
  document.querySelectorAll('.stock-identity,[data-stock-symbol]').forEach(el=>{const old=el.querySelector('.stock-cap');if(old)old.replaceWith(badge(el.dataset.capSymbol||el.dataset.stockSymbol));});
  annotate();
 }
 load();setInterval(load,300000);
})();
