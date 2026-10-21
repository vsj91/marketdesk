'use strict';
const $=id=>document.getElementById(id),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(x,d=2)=>x==null?'—':Number(x).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:d});
const pct=x=>x==null?'—':`${x>0?'+':''}${fmt(x)}%`,cls=x=>x==null?'muted':x>=0?'up':'down';
const dt=x=>x?new Date(x).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'medium',timeStyle:'short'}):'Not available';
const day=x=>x?new Date(x).toLocaleDateString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'medium'}):'Not available';
const inr=x=>x==null?'—':(x<0?'−₹':'₹')+fmt(Math.abs(x));
let stocks=[],health={},tab='trader',selected='',detail=null,page=0,csrf='',busy=false,polling=false,detailRequest=0;
async function api(path,data){const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Local-CSRF':csrf},body:JSON.stringify(data)});const j=await r.json();if(!r.ok)throw new Error(j.error||'Request failed');return j;}
let toastTimer;function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,7000);}
function stat(label,value,note){return `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div><small>${note}</small></div>`;}
function metric(label,value){return `<div class="metric"><span>${esc(label)}</span><strong>${value}</strong></div>`;}
function closeCheckRow(cc,row){const days=Object.entries((cc&&cc.sessions)||{}).filter(([,v])=>v.compared);if(!days.length)return row('Independent price check (NSE)','Not run yet');const n=days.reduce((a,[,v])=>a+v.compared,0),bad=days.reduce((a,[,v])=>a+v.close_mismatches,0);return row('Independent price check (NSE bhavcopy)',`${bad} mismatches / ${fmt(n,0)} closes · last ${days.length} sessions`);}
function scorecardBody(sc){if(!sc)return '<p class="muted">Not available.</p>';const list=g=>`<ul class="gates">${g.map(x=>`<li class="${x.met?'ok':'no'}"><span class="gate-mark" aria-hidden="true">${x.met?'✓':'○'}</span><div><strong>${esc(x.label)}</strong><small>${esc(x.detail)}</small></div></li>`).join('')}</ul>`;return `<p>${sc.ready?'Every gate is met.':'Real money is not supported until every gate is met.'}</p><h4>All baskets</h4>${list(sc.shared)}${Object.entries(sc.baskets).map(([n,g])=>`<h4>${esc(typeof basketTitle==='function'?basketTitle(n):n)}</h4>${list(g)}`).join('')}<p class="muted">${esc(sc.note)}</p>`;}
function renderHealth(){
 const h=health,s=h.stream||{},c=h.counts||{},market=s.market_status?.NSE_EQ||'Unknown',marketLabel=({CLOSING_END:'Market closed',CLOSED:'Market closed',NORMAL_OPEN:'Market open',PRE_OPEN_START:'Pre-open',PRE_OPEN_END:'Pre-open'})[market]||market.replaceAll('_',' ').toLowerCase().replace(/^./,c=>c.toUpperCase());
 $('connection').textContent=s.status==='connected'?marketLabel:`Feed ${s.status||'disconnected'}`;$('connection').title=s.status==='connected'?'Live feed connected':'';
 $('connection').className='pill '+(s.status==='connected'?'up':'warn');
 $('asof').innerHTML=`Latest daily candle<br><strong>${esc(day(h.latest_candle))} session</strong>`;
 $('footerTime').textContent='Updated '+dt(h.now)+' IST';$('gapCount').textContent=fmt(h.actionable_partial_fundamentals??h.partial_fundamentals,0);
 const live=stocks.filter(x=>x.quote_status==='LIVE').length;
 $('stats').innerHTML=stat('Nifty 500 universe',fmt(c.instruments,0),h.universe?.unmatched?.length?`${h.universe.constituents} constituents · ${h.universe.unmatched.length} unavailable in instrument coverage`:'Active research instruments')+stat('Daily candles',fmt(c.candles,0),'Validated OHLCV observations')+stat('Live prices',fmt(live,0),s.status==='connected'&&['CLOSING_END','CLOSED'].includes(market)?`${fmt(c.quotes,0)} saved snapshots · market closed`:'Trade age ≤ 30 seconds')+stat('Annual inputs complete',fmt(h.complete_fundamentals,0),`${fmt(h.actionable_partial_fundamentals??h.partial_fundamentals,0)} non-financial stocks still incomplete`);
 const row=(label,val)=>`<div class="health-row"><span>${esc(label)}</span><strong>${esc(val)}</strong></div>`;
 const q=h.quality||{},checked=Object.values(h.close_check?.sessions||{}).filter(v=>v.compared),closes=checked.reduce((a,v)=>a+v.compared,0),mismatch=checked.reduce((a,v)=>a+v.close_mismatches,0);
 const open=new Set(Array.from($('health').querySelectorAll('details[open]')).map(el=>el.dataset.disclosure));
 const gaps=h.actionable_partial_fundamentals??h.partial_fundamentals;
 const tile=(title,big,note,toneName)=>`<div class="health-tile"><div class="tile-top"><span class="dot ${toneName}"></span>${esc(title)}</div><strong>${big}</strong><p>${note}</p></div>`;
 $('readiness').innerHTML=`<div class="health-tiles">${[
  tile('Prices',`${fmt(q.trader_ready,0)}<small> of ${fmt(q.stocks,0)}</small>`,checked.length?(mismatch?`${fmt(mismatch,0)} of ${fmt(closes,0)} closes differ from NSE.`:`All ${fmt(closes,0)} checked closes match NSE.`):'Independent NSE check has not run yet.',q.trader_ready>=q.stocks*.98&&!mismatch?'up':'warn'),
  tile('Company financials',`${fmt(h.complete_fundamentals,0)}<small> complete</small>`,`${fmt(gaps,0)} companies still have gaps.${h.financial_incomplete?` ${fmt(h.financial_incomplete,0)} lenders’ filings don’t carry these lines.`:''}`,gaps>30?'warn':'up'),
  tile('Live prices',`${fmt(live,0)}<small> live</small>`,s.status!=='connected'?'The live feed is not connected.':market==='NORMAL_OPEN'?'Prices under 30 seconds old.':'Market closed. Showing last prices.',s.status==='connected'?'up':'warn'),
  tile('Ready for Investor',`${fmt(q.investor_ready,0)}<small> of ${fmt(q.stocks,0)}</small>`,`${fmt(q.investor_ready_non_financial,0)} non-financial companies pass every check.`,'up')].join('')}</div>`;
 const g=(key,title,count,body)=>group('health-'+key,title,count,body,open.has('health-'+key));
 const reasons=Object.entries(q.reasons||{}).sort((x,y)=>y[1]-x[1]);
 const candleRepair=q.repair_queue||{},fr=h.fundamental_repair||{};
 $('healthCards').innerHTML=
  g('excluded','Why stocks are left out',reasons.length?`${reasons.length} reasons`:'',`${reasons.map(([k,v])=>row(k,fmt(v,0)+' stocks')).join('')||'<p class="muted">Nothing is excluded.</p>'}<p class="muted">${esc(q.calendar_basis||'')}. Large price jumps are screened as possible corporate actions; adjusted history is not independently reconciled.</p>`)+
  g('financials','Missing company financials',`${fmt(gaps,0)} companies`,`${Object.entries(h.missing_fields||{}).map(([k,v])=>row(k.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase()),fmt(v,0)+' companies')).join('')||'<p class="muted">None.</p>'}${h.bounded_debt?.length?`<p><strong>${h.bounded_debt.length} resolved by a safe bound:</strong> ${h.bounded_debt.map(stockReference).join(', ')}. Their borrowing or lease line is unreported, but debt stays within the Investor limit even if every liability were debt.</p>`:''}${h.financial_incomplete?`<p class="muted">${fmt(h.financial_incomplete,0)} banks, insurers and NBFCs lack only lines their audited filing format does not carry, so they aren’t counted as gaps.</p>`:''}<p class="muted">Borrowings, total liabilities and lease liabilities are distinct. Missing values are never assumed to be zero.</p>${fr.status?`<p><a href="/fundamental-repair.html">Last repair: ${fmt(fr.newly_complete,0)} newly complete · see before and after →</a></p>`:''}`)+
  g('sources','Data sources','',`${row('Full income statements',fmt(h.detailed_statements?.income_statement,0))}${row('Full balance sheets',fmt(h.detailed_statements?.balance_sheet,0))}${row('Full cash-flow statements',fmt(h.detailed_statements?.cash_flow,0))}${row('Failed saved responses',fmt(h.failed_endpoints,0))}${row('Histories under 80 days (left out)',fmt(h.short_histories,0))}${row('Recent listings (80–199 days)',fmt(h.recent_listings,0))}${row('Recorded validation issues',fmt(c.issues,0))}${closeCheckRow(h.close_check,row)}${row('Daily candles stored',fmt(c.candles,0))}`)+
  g('scorecard','Go-live checklist',h.scorecard?`${h.scorecard.met} of ${h.scorecard.total} met`:'',scorecardBody(h.scorecard))+
  g('audit','Audits and repairs','',`${row('Last data audit',dt(q.checked_at))}${row('Paper strategy validated','Not yet')}${row('Live trading','Not available · paper only')}${row('Candle repair',candleRepair.status==='attempted'?`${dt(candleRepair.finished_at)} · ${candleRepair.symbols?.length||0} stocks · ${candleRepair.errors||0} errors`:`${candleRepair.status||'not queued'} · ${candleRepair.symbols?.length||0} stocks`)}<p class="muted">The audit repeats every 5 minutes. Strategies need at least 80 completed daily sessions; stocks with 80–199 are tested as recent listings using every session available.</p>`);
 const u=h.universe||{};$('universeNote').hidden=!(u.enabled&&(u.unmatched?.length||u.pending_exit_holdings));$('universeNote').textContent=u.enabled?`${u.unmatched?.length?`${u.unmatched.length} constituents are outside supported instrument coverage: ${u.unmatched.join(', ')}. `:''}${u.pending_exit_holdings?`${u.pending_exit_holdings} outside-index holdings await valid exit fills; their quotes remain temporarily active.`:''}`:'';
 const repair=h.fundamental_repair||{};
 $('fundamentalRepair').hidden=repair.status!=='running';
 $('fundamentalRepair').innerHTML=repair.status?`<strong>Fundamentals repair · ${esc(repair.status)}</strong> · ${fmt(repair.done,0)} / ${fmt(repair.total,0)} checked · <strong>${fmt(repair.newly_complete,0)} newly complete</strong> · ${fmt(repair.incomplete_now,0)} still incomplete. <a href="/fundamental-repair.html">View before / after and remaining gaps →</a>`:'';
 const j=h.refresh||{};
 $('jobHelp').textContent=j.mode==='fundamental_repair'?'Targeted NSE / Yahoo repair of incomplete annual inputs. Checked stocks are not necessarily complete; unavailable values remain missing. Paper trading continues.':'Refresh calls Upstox at a controlled pace and retains completed stocks. Only Nifty 500 is refreshed. Keep the app running.';
$('job').textContent=j.status?`${({running:'Running',complete:'Finished',failed:'Failed',interrupted:'Interrupted',ready:'Ready'})[j.status]||j.status} · ${({fundamental_repair:'Annual data repair',both:'Candles and fundamentals'})[j.mode||'both']||j.mode} · ${j.done||0} / ${j.total||0}${j.current&&j.status==='running'?' · '+j.current:''} · ${j.errors||0} failed stocks · ${j.stage||'Upstox'}${j.error?' · '+j.error:''}${j.upstox_warning?' · '+j.upstox_warning:''}`:'No refresh running.';
 $('jobProgress').max=j.total||1;$('jobProgress').value=j.done||0;$('jobProgress').hidden=j.status!=='running';$('jobCard').classList.toggle('running',j.status==='running');$('refreshAll').disabled=j.status==='running';$('repairFundamentals').disabled=j.status==='running';$('refreshAll').textContent=['interrupted','failed'].includes(j.status)&&j.scope==='all'?'Resume repair':'Refresh Nifty 500 data';
 $('tokenStatus').textContent=h.token_saved?'Your access token is saved locally.':'No token saved.';
 $('feedStatus').textContent=`Feed: ${s.status||'disconnected'} · ${marketLabel}. ${fmt(c.quotes,0)} saved price snapshots. Last market message: ${dt(s.last_message)}.${s.error?' '+s.error:''}`;
}
function filtered(){const q=$('search').value.trim().toLowerCase(),f=$('filter').value;return stocks.filter(x=>(!q||`${x.symbol} ${x.name} ${x.sector||''}`.toLowerCase().includes(q))&&(f==='all'||f==='watch'&&x.watched||f==='history'&&x.technical.count>=200||f==='gaps'&&x.fundamentals_status!=='COMPLETE'||f==='live'&&x.quote_status==='LIVE'||f==='eligible'&&x.quality?.[tab==='investor'?'investor_ready':'trader_ready']));}
const FILTERS=[['all','All'],['watch','Watchlist'],['live','Live now'],['eligible','Passes checks'],['gaps','Financial gaps'],['history','200+ days']];
function renderChips(){
 const current=$('filter').value;
 $('filterChips').innerHTML=FILTERS.filter(([v])=>tab==='investor'||v!=='gaps').map(([v,label])=>`<button class="chip-btn ${v===current?'active':''}" data-filter="${v}" aria-pressed="${v===current}">${label}</button>`).join('');
 $('filterChips').querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{$('filter').value=b.dataset.filter;page=0;renderTable();});
}
// Outside live trading, show the official daily close when it is at least as recent as the last trade, so price and returns share one session.
function priceView(x){
 const t=x.technical,q=x.quote,candle=(t.as_of||'').slice(0,10),traded=q?new Date(q.trade_time+19800000).toISOString().slice(0,10):'';
 if(q&&x.quote_status==='LIVE')return {price:q.price,label:'Live',time:dt(new Date(q.trade_time).toISOString())+' IST',stale:false};
 if(t.close!=null&&(!q||candle>=traded))return {price:t.close,label:'Close',time:candle,stale:false};
 return {price:q.price,label:'Last trade',time:dt(new Date(q.trade_time).toISOString())+' IST · daily data only to '+candle,stale:true};
}
// Growth from a tiny or negative base is not a meaningful percentage.
function growthCell(value,current){
 if(value==null)return '<span class="flat">—</span>';
 if(current!=null&&current<0)return `<span class="down" title="Loss in the latest year (${esc(pct(value))} vs last year)">Loss</span>`;
 if(Math.abs(value)>300)return `<span class="flat" title="Change from a very small base (${esc(pct(value))}) is not meaningful">n.m.</span>`;
 return `<span class="${tone(value)}">${pct(value)}</span>`;
}
// Valuation ratios: missing input is —, a loss or non-positive denominator is n.m.
function ratioCell(value,unit){
 if(value==null)return '<span class="flat" title="An input is missing for the latest annual year">—</span>';
 if(value==='n.m.')return '<span class="flat" title="Loss or non-positive denominator: not meaningful">n.m.</span>';
 return unit==='%'?`${fmt(value,1)}%`:`${fmt(value,1)}×`;
}
function renderTable(){
 const rows=filtered(),size=40;page=Math.min(page,Math.max(0,Math.ceil(rows.length/size)-1));
 const investor=tab==='investor';
 $('viewTitle').textContent=investor?'Investor':'Trader';
 $('viewHelp').textContent=investor?'Company growth, valuation, returns on capital and where data is missing.':'Daily price trends, with live prices while the market is open.';
 renderChips();
 const chosen=(FILTERS.find(([v])=>v===$('filter').value)||FILTERS[0])[1];$('listTitle').textContent=chosen==='All'?'Nifty 500':chosen;
 $('rowcount').textContent=fmt(rows.length,0)+' stocks';$('pageLabel').textContent=rows.length?`${page*size+1}–${Math.min(rows.length,(page+1)*size)} of ${rows.length}`:'';$('prev').disabled=page===0;$('next').disabled=(page+1)*size>=rows.length;
 $('stockRows').innerHTML=rows.slice(page*size,(page+1)*size).map(x=>{
  const t=x.technical,v=priceView(x),sel=x.symbol===selected;
  const right=investor
   ?`<div class="r"><small>Revenue ${growthCell(x.summary.revenue_growth_yoy,x.summary.revenue)}</small><small>Profit ${growthCell(x.summary.net_profit_growth_yoy,x.summary.net_profit)}</small><small>P/E ${ratioCell(x.valuation?.pe)} · ROE ${ratioCell(x.valuation?.roe,'%')}</small></div><span class="chip ${x.fundamentals_status==='COMPLETE'?'up':'warn'}">${x.fundamentals_status==='COMPLETE'?'Complete':'Gaps'}</span>`
   :`<div class="r"><strong>₹${fmt(v.price)}</strong><small class="${v.label==='Live'?'up':v.stale?'warn':''}" title="${esc(v.time)}">${v.stale?'Last trade · data behind':v.label}${x.candle_issues?' · check data':''}</small></div><span class="chip ${tone(t.return_1m)}" title="1-month return to the ${esc((t.as_of||'').slice(0,10))} close">1M ${pct(t.return_1m)}</span>`;
  return `<div class="stock-row ${sel?'selected':''}" role="option" aria-selected="${sel}" tabindex="0" data-symbol="${esc(x.symbol)}"><div class="l"><strong>${x.watched?'<span class="star" aria-label="Watching">★</span> ':''}${stockReference(x.symbol)}</strong><small>${esc(x.sector||'Sector unavailable')}</small></div>${right}</div>`;
 }).join('')||'<div class="empty">No matching stocks.</div>';
 $('stockRows').querySelectorAll('[data-symbol]').forEach(r=>{r.onclick=()=>selectStock(r.dataset.symbol);r.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectStock(r.dataset.symbol);}};});
}
function chart(rows){const canvas=$('priceChart');if(!canvas)return;const ctx=canvas.getContext('2d'),ratio=window.devicePixelRatio||1,w=canvas.clientWidth,h=240;canvas.width=w*ratio;canvas.height=h*ratio;ctx.scale(ratio,ratio);if(rows.length<2){ctx.fillStyle='#86868b';ctx.fillText('At least two daily observations are needed.',20,50);return;}
 const values=rows.map(x=>x.close),min=Math.min(...values),max=Math.max(...values),pad=22,range=max-min||1;const x=i=>pad+i/(values.length-1)*(w-pad*2),y=v=>h-pad-(v-min)/range*(h-pad*2);
 const css=getComputedStyle(document.documentElement),lineColor=css.getPropertyValue('--chart').trim(),gridColor=css.getPropertyValue('--chart-grid').trim(),labelColor=css.getPropertyValue('--faint').trim();ctx.strokeStyle=gridColor;ctx.lineWidth=1;ctx.fillStyle=labelColor;ctx.font='10px sans-serif';for(let i=0;i<4;i++){let py=pad+i*(h-pad*2)/3;ctx.beginPath();ctx.moveTo(pad,py);ctx.lineTo(w-pad,py);ctx.stroke();ctx.fillText(fmt(max-i*range/3,0),pad+4,py-5);}
 const gradient=ctx.createLinearGradient(0,pad,0,h);gradient.addColorStop(0,lineColor+'33');gradient.addColorStop(1,lineColor+'00');ctx.beginPath();values.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.lineTo(x(values.length-1),h-pad);ctx.lineTo(x(0),h-pad);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();ctx.beginPath();values.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.strokeStyle=lineColor;ctx.lineWidth=2;ctx.stroke();}
async function selectStock(symbol){const request=++detailRequest;selected=symbol;renderTable();try{const next=await api('/api/stock?symbol='+encodeURIComponent(symbol));if(request!==detailRequest)return;detail=next;renderDetail();}catch(e){toast(e.message);}}
function valuationBlock(v){return `<h3>Valuation &amp; returns</h3><p>${esc(v.period||'No annual period')} · market cap = price ₹${fmt(v.price)} × shares (Upstox profit ÷ basic EPS) · ₹ crore${'roce' in v?'':' · ROCE not used for lenders and insurers'}</p><div class="metrics">${metric('Market cap',fmt(v.market_cap,0))}${metric('P/E',ratioCell(v.pe))}${metric('P/B',ratioCell(v.pb))}${metric('ROE',ratioCell(v.roe,'%'))}${'roce' in v?metric('ROCE',ratioCell(v.roce,'%')):''}${metric('Net margin',ratioCell(v.net_margin,'%'))}</div>`;}
function renderDetail(){
 if(!detail)return;const x=stocks.find(x=>x.symbol===selected);if(!x)return;const t=x.technical,f=detail.fundamentals,l=f.latest||{},rows=detail.candles,v=priceView(x),missing=f.missing_fields||[];
 $('detail').innerHTML=`<div class="detail-heading"><div><h2>${stockReference(x.symbol)}</h2><p>${esc(x.sector||'Sector unavailable')}</p></div><button id="watch" aria-pressed="${!!x.watched}">${x.watched?'★ Watching':'☆ Watch'}</button></div><div class="price-line"><div class="price" id="detailPrice">₹${fmt(v.price)}</div><span class="chip ${tone(t.return_1m)}">${pct(t.return_1m)} · 1M</span></div><p id="priceTime" class="${v.stale?'warn':'muted'}">${esc(v.label+' · '+v.time)}</p><canvas class="chart" id="priceChart" aria-label="Daily closing price chart"></canvas><div class="chart-labels"><span>${esc(rows[0]?.ts?.slice(0,10)||'')}</span><span>Daily close · ${rows.length} observations</span><span>${esc(rows.at(-1)?.ts?.slice(0,10)||'')}</span></div>
 ${tab==='investor'?`<h3>Annual fundamentals <span class="tag warn">${esc(f.status||'MISSING')}</span></h3><p>${esc(l.period||'No detailed annual period')} · ${esc(l.statement_type||'Basis unavailable')} · ${esc(l.units_in||'Units unavailable')} · ${esc(f.selected_source||'Upstox')}</p><div class="metrics">${metric('Profit before tax',fmt(l.profit_before_tax))}${metric('Finance costs',fmt(l.finance_costs))}${metric('Equity',fmt(l.equity))}${metric('Borrowings',fmt(l.total_borrowings))}${metric('Lease liabilities',fmt(l.total_lease_liabilities))}${metric('Cash equivalents',fmt(l.cash_and_cash_equivalents))}</div>${valuationBlock(x.valuation||{})}<div class="note">${missing.length?'Missing: '+esc(missing.join(', ').replaceAll('_',' ')):'Detailed fields present. This is not an independently audited investment qualification.'}</div>`:`<div class="metrics">${metric('50-day average',fmt(t.dma50))}${metric('200-day average',fmt(t.dma200))}${metric('Volume ratio',fmt(t.volume_ratio)+'×')}${metric('1-month return',pct(t.return_1m))}${metric('1-year return',pct(t.return_1y))}${metric('Trend checklist',fmt(t.trend_score,0)+' / 100')}</div><p class="muted">Checklist: price above 50/200-day averages, 50 above 200, positive 1-month return, and volume ratio ≥ 1.5. Twenty points each; not a buy recommendation.</p>`}
 ${tab==='investor'&&f.source_conflicts?.length?`<div class="note">Source discrepancy: ${f.source_conflicts.map(c=>`${esc(c.field.replaceAll('_',' '))}: ${c.annual_report!==undefined?`Selected ${fmt(c.selected_value)} / annual report ${fmt(c.annual_report)}`:`Upstox ${fmt(c.upstox)} / NSE ${fmt(c.nse)}`} ${esc(c.units)}`).join('; ')}. Existing selected values are retained; conflicting evidence blocks Investor buys.</div>`:''}
 ${tab==='investor'&&f.fallbacks?`<details><summary>NSE / Yahoo fallback coverage</summary><p>NSE: ${esc(f.fallbacks.nse?.status||'not requested')} · Yahoo: ${esc(f.fallbacks.yahoo?.status||'not requested')}</p>${f.fallbacks.yahoo?.annual?.length?`<p>Yahoo annual research · ${esc(f.fallbacks.yahoo.annual[0].period)} · ${esc(f.fallbacks.yahoo.annual[0].currency||'currency unknown')} · consolidated/standalone basis unspecified. Kept separate.</p><div class="metrics">${Object.entries(f.fallbacks.yahoo.annual[0]).filter(([k,v])=>typeof v==='number').map(([k,v])=>metric(k.replaceAll('_',' '),fmt(v))).join('')}</div>`:''}</details>`:''}
 ${x.candle_issues?'<div class="note">Some source candles failed validation. Review the source issues before relying on calculated trends.</div>':''}
 <div class="status-banner"><span class="dot ${x.quality?.[tab==='investor'?'investor_ready':'trader_ready']?'up':'warn'}"></span><div><strong>${x.quality?.[tab==='investor'?'investor_ready':'trader_ready']?'Data ready for new buys':'New buys blocked by data checks'}</strong><p>${esc([...(x.quality?.price_reasons||['Audit pending']),...(tab==='investor'?(x.quality?.financial_reasons||[]):[])].join('; ')||'Entry signals and fresh bid/ask depth still required. Corporate actions are not independently reconciled.')}</p></div></div>
 <div class="actions"><button id="refreshStock">Refresh this stock</button><span class="muted">Reloads financials and completed daily candles.</span></div><div class="order-box"><h3>Paper order</h3><form id="orderForm"><div class="order-fields"><div><label for="orderPortfolio">Portfolio</label><select id="orderPortfolio">${(paperData?paperData.portfolios.map(p=>p.name):['trader','investor']).filter(n=>!paperData?.automation?.config?.[n]?.retired).map(n=>`<option value="${n}" ${tab===n?'selected':''}>${esc(basketTitle(n))}</option>`).join('')}</select></div><div><label for="orderSide">Side</label><select id="orderSide"><option>BUY</option><option>SELL</option></select></div><div><label for="orderQty">Shares</label><input id="orderQty" type="number" min="1" max="1000000" step="1" value="1" required></div></div><button id="orderSubmit" class="primary" type="submit">Place paper order</button><p class="muted">Fresh live price and bid/ask depth required. Partial fills cancel the remainder. Delivery-style charges are estimates. Paper only.</p></form></div><details><summary>Source responses & extraction evidence</summary><pre id="evidence"></pre></details>`;
 $('evidence').textContent=JSON.stringify({fundamentals:detail.fundamentals,issues:detail.issues,endpoints:detail.payloads},null,2);chart(rows);
 $('watch').onclick=async()=>{try{await api('/api/watch',{symbol:selected,watched:!x.watched});x.watched=!x.watched;renderTable();renderDetail();}catch(e){toast(e.message);}};
 $('refreshStock').onclick=()=>refresh([selected]);
 let pending=null;
 $('orderForm').onsubmit=async e=>{e.preventDefault();if(busy)return;const data={portfolio:$('orderPortfolio').value,side:$('orderSide').value,quantity:Number($('orderQty').value),symbol:selected};const key=JSON.stringify(data);if(!pending||pending.key!==key)pending={key,id:crypto.randomUUID()};busy=true;$('orderSubmit').disabled=true;try{const fill=await api('/api/paper/order',{...data,request_id:pending.id});pending=null;toast(`Paper fill: ${fill.quantity} / ${fill.requested_quantity} shares. ${fill.quantity<fill.requested_quantity?'Remainder cancelled.':''}`);await renderPortfolios();}catch(e){toast(e.message);}finally{busy=false;if($('orderSubmit'))$('orderSubmit').disabled=false;}};
}
function stockReference(symbol){return `<span data-stock-symbol="${esc(symbol)}">${esc(symbol)}</span>`;}
let automationAction=false;
function statusTag(state){const t={monitoring:['WATCHING','up'],market_wait:['WAITING · MARKET FILTER','warn'],risk_wait:['WAITING · RISK CAP','warn'],data_wait:['WAITING · DATA','warn'],error:['ERROR','down'],paused:['PAUSED','warn'],waiting:['WAITING','warn']};return t[state]||[esc(String(state||'status').replaceAll('_',' ').toUpperCase()),'warn'];}
function buyUniverseDetails(u){
 if(!u||u.name!=='Nifty 500')return '';
 return `<p><strong>New buys: Nifty 500 only</strong></p><p>${u.status==='ok'?`${fmt(u.data_ready,0)} pass data checks / ${fmt(u.matched,0)} matched stocks · ${fmt(u.constituents,0)} real constituents in the official list. Verified ${esc(dt(u.fetched_at))} IST.`:esc(u.message)}</p>`;
}
function executionDetails(t,openDetails){let e;try{e=JSON.parse(t.execution||'{}');}catch{return '';}if(!e.model)return '';return `<details data-disclosure="${esc(t.id)}" ${openDetails.has(t.id)?'open':''}><summary>${esc(e.status)}</summary><p>${e.unfilled_quantity} unfilled shares cancelled · slippage vs last trade: ${fmt(e.slippage_vs_ltp_bps)} bps.</p><p>${Object.entries(e.charges||{}).map(([k,v])=>`${esc(k)}: ₹${fmt(v)}`).join(' · ')}</p>${e.audit?`<p>Risk: ${e.audit.risk_used_inr==null?'unverified':'₹'+fmt(e.audit.risk_used_inr)} · ${e.audit.risk_pct_equity==null?'equity % unverified':fmt(e.audit.risk_pct_equity)+'% of equity'} · constraints: ${esc((e.audit.binding_constraints||[]).join(', ')||'none identified')}</p>`:''}<p>Depth observed: ${esc(dt(e.book?.timestamp))} · ${esc(e.charge_basis)}</p></details>`;}
function strategyPerformance(p){
 const s=p.strategy_performance;if(!s)return '';
 if(s.status!=='ok')return `<section><h3>Performance by entry strategy</h3><p>${esc(s.message)}</p></section>`;
 return `<section><h3>Performance by entry strategy</h3><p>Each purchase retains its recorded strategy version. Legacy versions are inferred from recorded order evidence; unknown settings are not assumed.</p><div class="table-scroll"><table><thead><tr><th>Entry strategy / version</th><th>Buys / open stocks</th><th>Realized after fees</th><th>Open P&amp;L</th><th>Net after charged fees</th><th>Fees</th></tr></thead><tbody>${s.cohorts.map(c=>`<tr><td>${esc(c.label)}<small>${esc(c.key)}</small></td><td>${c.buy_fills} / ${c.open_symbols.length}</td><td>${fmt(c.realized_net)}</td><td>${fmt(c.unrealized_net)}</td><td>${fmt(c.net_after_charged_costs)}</td><td>${fmt(c.fees_charged)}</td></tr>`).join('')}</tbody></table></div><p class="muted">Sales retain the entry cohort using proportional average cost. Booked fees are already deducted. Open P&amp;L excludes future exit costs.</p></section>`;
}
function exposurePanel(p){
 const e=p.exposure;if(!e)return '';
 return `<section><h3>Sector and correlation exposure</h3><p class="muted">${esc(e.basis)}</p><div class="table-scroll"><table><thead><tr><th>Sector / holdings</th><th>Market value</th><th>% invested</th><th>% equity</th></tr></thead><tbody>${e.sectors.map(s=>`<tr><td>${esc(s.sector)}${s.pct_invested>40?' <span class="tag warn">CONCENTRATED</span>':''}<small>${s.symbols.map(stockReference).join(', ')}</small></td><td>₹${fmt(s.value)}</td><td>${fmt(s.pct_invested)}%</td><td>${fmt(s.pct_equity)}%</td></tr>`).join('')}</tbody></table></div><details><summary>Pairwise daily-return correlations</summary><div class="table-scroll"><table><thead><tr><th>Pair</th><th>Correlation</th><th>Common returns</th><th>Through</th></tr></thead><tbody>${e.correlations.map(c=>`<tr><td>${esc(c.a)} / ${esc(c.b)}</td><td>${c.correlation==null?'Insufficient data or constant returns':fmt(c.correlation,3)}</td><td>${c.observations}</td><td>${esc(c.last||'unavailable')}</td></tr>`).join('')}</tbody></table></div></details></section>`;
}
const BASKETS={
 trader:{title:'Trader',blurb:'Buys stocks breaking out to new 20-day highs and rides the trend with a trailing stop.'},
 investor:{title:'Investor',blurb:'Owns profitable, growing, low-debt companies while the price stays above its 200-day average.'},
 momentum:{title:'Momentum',blurb:'Holds the 10 strongest stocks of the past six months. Reviewed at the start of each month.'},
 lowvol:{title:'Low volatility',blurb:'Holds the 10 calmest stocks that are still in an uptrend. Reviewed at the start of each month.'},
 alpha:{title:'Alpha',blurb:'Holds the 10 stocks with the most return over the past year beyond what their market sensitivity explains. Reviewed at the start of each month.',caution:'Research basket, added as a twin of Momentum 12-1: about 78% of its holdings overlap, so the two show how far close rules drift apart.'},
 overnight:{title:'Overnight momentum',blurb:'Holds the 10 stocks whose gains have come mostly overnight, between one close and the next open. Reviewed at the start of each month.',caution:'Research basket: in the 2018–2026 backtest it beat the equal-weight hold overall but not among the 200 most traded stocks.'},
 gap_drift:{title:'Gap-up drift',blurb:'Buys stocks that gapped up 4%+ on three times normal volume and held the gain, then holds them up to 60 sessions.',caution:'Research basket: 8.1% a year in the 2018–2026 backtest, behind the 12.1% equal-weight hold.'},
 sector_rotation:{title:'Sector rotation',blurb:'Holds the strongest stocks in the 3 sectors with the best six-month momentum. Reviewed at the start of each month.',caution:'Research basket: 1.5% a year in the 2018–2026 backtest, behind the 12.1% equal-weight hold.'},
 clenow:{title:'Stocks on the Move',blurb:'Holds the 10 Nifty 500 stocks climbing most steadily over the past 90 sessions, buying only while the Nifty 500 is above its 200-day average. Reviewed at the start of each month.',caution:'Research basket (Andreas Clenow’s rules): 15.0% a year vs 12.1% for the hold in the 2018–2026 backtest, with a 37.8% worst fall vs 54.7%. No per-stock stop loss, and it buys nothing while the Nifty 500 is below its 200-day average.'},
 momentum12:{title:'Momentum 12-1',blurb:'Holds the 10 strongest stocks of the past year, skipping the latest month, while above the 200-day average. Reviewed at the start of each month.'},
 high52:{title:'52-week high',blurb:'Holds the 10 stocks trading closest to their one-year high while above the 200-day average. Reviewed at the start of each month.'},
 smart_beta:{title:'Multi-factor',blurb:'Ranks stocks on value, quality, momentum and low volatility and holds the top 15, at most 30% in one sector. Signals refresh daily.'},
 risk_parity:{title:'Risk parity',blurb:'Holds the top multi-factor names, giving calmer stocks more money so no single stock dominates the swings.'},
 stat_arb:{title:'Stat-arb',blurb:'Buys sound large and mid caps after an unusually sharp fall below their 20-day average while the long trend still rises; sells at the average.'},
 regime:{title:'Regime switching',blurb:'Shifts between momentum, quality, low-volatility and value stocks depending on whether the market is rising, choppy or falling.'},
 news_momentum:{title:'AI News Momentum Trader',blurb:'Buys verified positive NSE filings once the next completed session confirms volume, trend and a reaction that is not already priced in. Holds 1–20 sessions.'},
 news_catalyst:{title:'AI News Catalyst Investor',blurb:'Owns companies whose verified business catalyst is backed by growth, financial quality and a reasonable valuation. Holds months, reviewed weekly.'},
 vinay_intraday:{title:'Vinay Intraday Trader',blurb:'One intraday trade a day at most: a stock that opened 1–2.5% higher and broke above its opening range and VWAP. 2.5% stop loss, sold by 15:10. Fees capped at ₹500 a month.',caution:'Paper experiment, starts switched off: lost money after Upstox fees in every 2022–2026 backtest year (−4% to −23%), on large and mid-cap Nifty 500 stocks.'},
 ema_price_action:{title:'EMA + Price Action Trader',blurb:'Intraday 5-minute EMA 9/21 crossovers above the 200 EMA with RSI, ATR, structural stops, partial profits and an EMA20 trail. Squares off at 15:12.'},
 vol_momentum:{title:'Vol-adjusted momentum',blurb:'Holds the 10 stocks with the best 6- and 12-month returns per unit of volatility. Reviewed monthly.'},
 residual_momentum:{title:'Residual momentum',blurb:'Holds the 10 stocks whose recent gains are least explained by the market’s move. Reviewed monthly.'},
 smooth_momentum:{title:'Smooth momentum',blurb:'Among last year’s winners, holds the 10 that climbed in the steadiest small steps. Reviewed monthly.'},
 volume_surge:{title:'Volume surge',blurb:'Holds stocks rising on at least twice their normal trading volume. Reviewed monthly.'},
 pullback:{title:'Pullback in leaders',blurb:'Buys last year’s strongest stocks when they dip back to their 50-day average.'},
 low_max:{title:'Low max-return',blurb:'Holds the 10 stocks with the smallest single-day jump last month, avoiding “lottery” stocks. Reviewed monthly.'},
 reversal:{title:'Short-term reversal',blurb:'Buys the biggest one-month losers among the 100 most traded stocks while their long trend still rises.'},
 minervini:{title:'Minervini trend template',blurb:'Holds strong leaders that pass Mark Minervini’s trend template, with an 8% stop.'},
 conservative:{title:'Conservative Formula',blurb:'Holds the 10 calmer-half stocks with the best 12-month momentum. Reviewed monthly.'},
 turtle:{title:'Turtle breakout',blurb:'Buys 55-day highs and sells at a 20-day low or a 2-ATR stop, as the Turtles did.'},
 connors:{title:'Connors RSI-2',blurb:'Buys sharp 2-day dips in rising stocks and sells on the bounce above the 5-day average.'},
 dual_momentum:{title:'Dual Momentum',blurb:'Holds 12-month leaders only while the Nifty 500 beats T-bills; otherwise cash.'},
 golden_cross:{title:'Golden cross + ADX',blurb:'Buys fresh 50/200-day EMA golden crosses with a strong trend, sells on the cross back.'},
 macd:{title:'MACD crossover',blurb:'Buys MACD signal-line crosses above zero, sells when MACD crosses back down.'},
 bollinger_rsi:{title:'Bollinger + RSI',blurb:'Buys closes below the lower Bollinger band with RSI under 30, sells at the middle band.'},
 quality_value:{title:'Quality at a fair price',blurb:'Buffett-style: high and steady ROE, rising profit, low debt burden, cheapest first. Reviewed monthly.'},
 magic_formula:{title:'Magic Formula',blurb:'Greenblatt: high earnings yield plus high return on capital, each pick held a year.'},
 net_buyback:{title:'Net buybacks',blurb:'Holds profitable companies that shrank their share count by 1%+ last year. Reviewed monthly.'},
 dividend_quality:{title:'Dividend + quality',blurb:'Holds the highest dividend yields among profitable, well-run companies. Reviewed monthly.'},
 value_momentum:{title:'Value + momentum',blurb:'Holds stocks that are both cheap on earnings and rising over the past year. Reviewed monthly.'},
 reversion:{title:'Mean reversion',blurb:'Buys sharp two-day dips in uptrending stocks and sells on the bounce.',caution:'In the one-year backtest, trading fees outweighed the gains at ₹10,000 per trade, so this basket starts switched off.'}};
const basketTitle=n=>BASKETS[n]?.title||String(n).charAt(0).toUpperCase()+String(n).slice(1);
const signedInr=x=>x==null?'—':(x>0?'+':'')+inr(x);
const tone=x=>x==null||Math.abs(x)<.005?'flat':x>0?'up':'down';
let basket='trader',paperData=null,backtestData=null,backtestAt=0,detailGroup='live';try{basket=localStorage.getItem('marketdesk-basket')||basket;detailGroup=localStorage.getItem('marketdesk-detail-group')||detailGroup;}catch{}
// One detail panel at a time: under the live group or under the daily group, whichever was tapped last.
function applyDetailGroup(){$('paper').dataset.detail=detailGroup;$('basketDetail').hidden=detailGroup!=='live';$('quantDetail').hidden=detailGroup!=='daily';}
function useDetailGroup(group,scroll){detailGroup=group;try{localStorage.setItem('marketdesk-detail-group',group);}catch{}applyDetailGroup();if(scroll)setTimeout(()=>$(group==='live'?'basketDetail':'quantDetail').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'}),0);}
[['basketGrid','[data-basket]','live'],['quantGrid','[data-quant]','daily']].forEach(([grid,card,group])=>{
 const pick=e=>{if(e.target.closest(card)&&!e.target.closest('[data-toggle]'))useDetailGroup(group,group==='daily');};
 $(grid).addEventListener('click',pick,true);$(grid).addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')pick(e);},true);
});
function friendlyStatus(enabled,s){
 if(!enabled)return ['Off',''];
 return ({monitoring:['Active','up'],market_wait:['Waiting for the market trend','warn'],risk_wait:['Paused by a risk limit','warn'],data_wait:['Waiting for data','warn'],waiting:['Waiting for the market to open',''],error:['Stopped after an error','down']})[s?.state]||['Checking…',''];
}
function switchHtml(name,on){return `<button class="switch" role="switch" aria-checked="${on?'true':'false'}" aria-label="${on?'Turn off':'Turn on'} ${esc(basketTitle(name))}" data-toggle="${esc(name)}" ${automationAction?'disabled':''}></button>`;}
function kpi(label,value,note){return `<div class="kpi"><span>${esc(label)}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;}
function group(key,title,count,body,open){return `<details class="group" data-disclosure="${esc(key)}" ${open?'open':''}><summary>${esc(title)}${count?`<small>${esc(count)}</small>`:''}</summary><div>${body}</div></details>`;}
function todayBuys(p){const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});return p.trades.filter(t=>t.side==='BUY'&&t.source==='automatic'&&new Date(t.created_at).toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'})===today).length;}
// What the basket's backtest showed, kept beside its live paper record so a failed rule is never mistaken for a tested one.
const VERDICTS={pass:['Passed every backtest check','up','Backtest passed'],mixed:['Mixed backtest: beat the hold, failed other checks','warn','Backtest mixed'],fail:['Failed its backtest','down','Backtest failed'],untested:['Not backtested','flat','Not backtested']};
function backtestRow(n){return backtestData?.baskets?.find(x=>x.name===n);}
function backtestTag(n){const v=VERDICTS[backtestRow(n)?.backtest?.verdict];return v?` <span class="tag ${v[1]}" title="${esc(backtestRow(n).backtest.note||'')}">${v[2]}</span>`:'';}
function backtestCard(n){
 const row=backtestRow(n);if(!row)return '';const b=row.backtest||{},v=VERDICTS[b.verdict||'untested']||VERDICTS.untested,f=b.full||{},cfg=paperData?.automation?.config?.[n]||{};
 const span=b.window?`${b.window[0].slice(0,4)}–${b.window[1].slice(0,4)}`:'',lines=[];
 if(f.cagr!=null)lines.push(`<strong class="${tone(b.hold_cagr==null?null:f.cagr-b.hold_cagr)}">${pct(f.cagr)} a year</strong>${b.hold_cagr!=null?` vs ${pct(b.hold_cagr)} for holding the same stocks`:''}${span?` · ${span}`:''}${f.maxdd!=null?` · worst fall −${fmt(f.maxdd,1)}%`:''}`);
 if(b.h1?.cagr!=null&&b.h2?.cagr!=null)lines.push(`First half ${pct(b.h1.cagr)} · second half ${pct(b.h2.cagr)}`);
 if(b.note)lines.push(esc(b.note));
 const p=row.paper||{};if(p.days)lines.push(`Paper so far: ${pct(p.return_pct)} over ${p.days} day${p.days===1?'':'s'} · backtest pace ${pct(p.backtest_pace_pct)} · Nifty 500 ${pct(p.nifty500_pct)}`);
 if(p.days_needed){const left=Math.max(0,p.days_needed-(p.days||0)),m=p.days_needed/30.4;lines.push(`<strong>Paper test length: about ${fmt(p.days_needed,0)} days${m>=2?` (~${m>=24?fmt(m/12,1)+' years':fmt(m,0)+' months'})`:''}</strong> to reach ${p.judge_trades} closed trades at its backtest pace of ${fmt(p.backtest_trades_per_year,0)} a year · day ${fmt(p.days||0,0)} of ${fmt(p.days_needed,0)}${left?` · ${fmt(left,0)} to go`:' · ready to judge'}${p.judge_on?` (${esc(p.judge_on)})`:''} · ${fmt(p.closed_trades||0,0)} of ${p.judge_trades} closed so far`);}
 else lines.push(`<strong>Paper test length: judge after ${p.judge_trades||30} closed trades</strong> (no backtest trade rate) · ${fmt(p.closed_trades||0,0)} closed so far`);
 if(cfg.comparison_only)lines.push(`<strong>${esc(cfg.comparison_reason||'Comparison-only basket.')}</strong>`);
 return `<div class="backtest-card ${v[1]}"><div class="backtest-head"><span class="tag ${v[1]}">${esc(v[0])}</span>${b.method?`<small>${esc(b.method)}</small>`:''}</div>${lines.map(l=>`<p>${l}</p>`).join('')}<p><a href="/backtests.html">All backtests and live results →</a></p></div>`;
}
function basketCard(p,auto){
 const n=p.name,cfg=auto.config[n]||{},[label,dotTone]=friendlyStatus(cfg.enabled,auto.status[n]);
 const invested=p.equity?Math.min(100,Math.max(0,(p.equity-p.cash)/p.equity*100)):0;
 return `<div class="basket ${n===basket?'selected':''}" role="button" tabindex="0" aria-pressed="${n===basket}" data-basket="${esc(n)}"><div class="basket-top"><span class="basket-name">${esc(basketTitle(n))}${cfg.enabled&&cfg.exits_only?' <span class="tag warn" title="No new buys; stops and exits keep running">Exits only</span>':''}${backtestTag(n)}</span>${switchHtml(n,cfg.enabled)}</div><div class="basket-value">${inr(p.equity)}</div><div class="basket-change ${tone(p.pnl)}">${signedInr(p.pnl)} · ${pct(p.pnl==null?null:p.pnl/p.initial_cash*100)}</div><div class="alloc"><i data-width="${invested.toFixed(1)}"></i></div><div class="alloc-label"><span>${fmt(invested,0)}% invested</span><span>${p.positions.length} held</span></div>${typeof signalCardLine==='function'?signalCardLine(n):''}<div class="basket-status"><span class="dot ${dotTone}"></span>${esc(label)}</div></div>`;
}
function eventHtml(e){
 const m=e.kind==='fill'&&/^(BUY|SELL) (\d+) \/ (\d+) shares \(([A-Z_]+)\): ([\s\S]*)$/.exec(e.reason||'');
 const many=e.symbols?.length>1,who=many?`${e.symbols.length} stocks`:e.symbol?stockReference(e.symbol):'';
 const head=m?`${m[1]==='BUY'?'Bought':'Sold'} ${m[2]}${m[2]!==m[3]?' of '+m[3]:''} ${who}`:e.kind==='blocked'?`Skipped ${who}`:({enabled:'Turned on',paused:'Turned off',error:'Stopped after an error',risk_halt:'Daily loss limit reached',reset:'Ledger reset'})[e.kind]||esc(e.kind);
 return `<div class="event"><strong>${head}</strong><p>${esc(m?m[5]:e.reason)}</p>${many?`<p><small>${e.symbols.map(stockReference).join(', ')}</small></p>`:''}<small>${esc(dt(e.created_at))}</small></div>`;
}
function holdingsHtml(p){
 if(!p.positions.length)return '<div class="list"><div class="row plain"><div class="l"><small>No holdings yet. The basket buys when its rules find a match and prices are live.</small></div></div></div>';
 const source=x=>x.mark_source==='official_daily_close'?'at close':x.mark_source==='last_trade'?'live':'no price';
 return `<div class="list">${[...p.positions].sort((a,b)=>(b.value||0)-(a.value||0)).map(x=>`<div class="row"><div class="l"><strong>${stockReference(x.symbol)}</strong><small>${x.quantity} share${x.quantity===1?"":"s"} · avg ₹${fmt(x.cost/x.quantity)}</small></div><div class="r"><strong>${inr(x.value)}</strong><small>₹${fmt(x.mark)} ${source(x)}</small></div><span class="chip ${tone(x.unrealized_pnl)}">${pct(x.cost&&x.unrealized_pnl!=null?x.unrealized_pnl/x.cost*100:null)}</span></div>`).join('')}</div>`;
}
function ledgerHtml(p,openDetails){
 if(!p.trades.length)return '<p class="muted">No paper orders yet.</p>';
 return `<div class="table-scroll" data-scroll="ledger-${esc(p.name)}"><table><thead><tr><th>Stock</th><th>Side</th><th>Shares</th><th>Price</th><th>Fees</th></tr></thead><tbody>${p.trades.map(t=>`<tr><td>${stockReference(t.symbol)}<small>${esc(dt(t.created_at))}</small>${t.reason?`<small title="${esc(t.reason)}">${esc(t.reason.length>70?t.reason.slice(0,70)+'…':t.reason)}</small>`:''}</td><td class="${t.side==='BUY'?'up':'down'}">${t.side==='BUY'?'Buy':'Sell'}<small>${esc(t.source||'manual')}</small></td><td>${t.quantity}${t.requested_quantity&&t.requested_quantity!==t.quantity?' / '+t.requested_quantity:''}</td><td>${fmt(t.price)}${executionDetails(t,openDetails)}</td><td>${fmt(t.fee)}</td></tr>`).join('')}</tbody></table></div>`;
}
function rulesHtml(r){
 const u=r.buy_universe;
 return `<p><strong>Buys:</strong> ${esc(r.entry)}</p><p><strong>Sells:</strong> ${esc(r.exit)}</p><p><strong>Risk:</strong> ${esc(r.risk||'')}</p><p><strong>Limits:</strong> ${[Number.isFinite(r.max_positions)?`up to ${r.max_positions} holdings`:'',Number.isFinite(r.position_pct)?`about ${r.position_pct}% of starting money each`:'',Number.isFinite(r.max_buys_per_day)?`at most ${r.max_buys_per_day} new buys a day`:''].filter(Boolean).join(', ')||'set by the basket’s plan'}.${r.entry_window?' New buys: '+esc(r.entry_window)+'.':''}</p>${u?.name==='Nifty 500'?`<p class="muted">${u.status==='ok'?`Nifty 500 only · ${fmt(u.data_ready,0)} of ${fmt(u.matched,0)} stocks pass data checks · list verified ${esc(dt(u.fetched_at))}.`:esc(u.message)}</p>`:''}${r.family?`<p class="muted">Strategy family: ${esc(r.family)}</p>`:''}`;
}
function basketDetail(p,auto,openDetails){
 const n=p.name,cfg=auto.config[n]||{},st=auto.status[n]||{},r=auto.rules[n]||{},meta=BASKETS[n]||{},[label,dotTone]=friendlyStatus(cfg.enabled,st);
 const invested=p.positions.reduce((a,x)=>a+(x.value||0),0),events=auto.events.filter(e=>e.portfolio===n),buys=todayBuys(p);
 const close=p.official_close_equity!=null?`Last close ${inr(p.official_close_equity)}`:`Started with ₹${fmt(p.initial_cash)}`;
 const stale=st.stale_holdings?.length?`<p class="callout">Waiting for fresh prices: ${st.stale_holdings.map(q=>stockReference(q.symbol)).join(', ')}. Older prices are used for the value shown, never for orders.</p>`:'';
 const open=k=>openDetails.has(n+'-'+k);
 return `<div class="detail-top"><div><h2>${esc(basketTitle(n))}</h2><p>${esc(meta.blurb||r.name||'')}</p></div><div class="toggle"><span title="${cfg.exits_only?esc(cfg.exits_only_reason||''):''}">${cfg.enabled?(cfg.exits_only?'Exits only':'On'):'Off'}</span>${switchHtml(n,cfg.enabled)}</div></div>
 ${backtestCard(n)}${meta.caution&&!cfg.enabled?`<div class="note">${esc(meta.caution)}</div>`:''}
 <div class="status-banner"><span class="dot ${dotTone}"></span><div><strong>${esc(label)}</strong><p>${esc(cfg.enabled?(st.message||'Waiting for the next check.'):'Automatic buying and selling is off. Holdings stay as they are until you turn it back on.')}</p>${stale}${cfg.enabled&&Number.isFinite(r.max_buys_per_day)&&buys>=r.max_buys_per_day?'<p class="callout">Today’s new-buy limit is reached. Sell checks continue.</p>':''}<small class="muted">Checked ${esc(dt(st.checked_at))}</small></div></div>
 <div class="kpis">${kpi('Value',inr(p.equity),esc(close))}${kpi('Total gain',`<span class="${tone(p.pnl)}">${signedInr(p.pnl)}</span>`,pct(p.pnl==null?null:p.pnl/p.initial_cash*100))}${kpi('Invested',inr(invested),Number.isFinite(r.max_positions)?`${p.positions.length} of ${r.max_positions} holdings`:`${p.positions.length} holdings`)}${kpi('Cash',inr(p.cash),Number.isFinite(r.max_buys_per_day)?`${buys} of ${r.max_buys_per_day} buys today`:`${buys} buys today`)}</div>
 <div class="section-title"><h3>Holdings</h3><span>Gain shown after buy fees</span></div>${holdingsHtml(p)}
 ${group(n+'-activity','Recent activity',events.length?`${events.length} events`:'',events.length?events.map(eventHtml).join(''):'<p class="muted">Nothing yet.</p>',open('activity'))}
 ${group(n+'-ledger','Trade history',`${p.trades.length} orders`,ledgerHtml(p,openDetails),open('ledger'))}
 ${p.exposure?.sectors?.length?group(n+'-sectors','Sector mix','',exposurePanel(p),open('sectors')):''}
 ${n==='trader'&&p.strategy_performance?group(n+'-performance','Performance by entry strategy','',strategyPerformance(p),open('performance')):''}
 ${typeof signalSection==='function'?signalSection(n,open):''}
 ${group(n+'-rules','How this basket decides','',rulesHtml(r),open('rules'))}`;
}
function renderPaper(){
 const data=paperData;if(!data)return;const auto=data.automation,list=data.portfolios.filter(p=>!auto.config[p.name]?.retired||p.positions.length);
 if(!list.some(p=>p.name===basket))basket=list[0]?.name||'trader';
 const root=$('paper'),openDetails=new Set(Array.from(root.querySelectorAll('details[open]')).map(el=>el.dataset.disclosure));
 const scrolls=new Map(Array.from(root.querySelectorAll('[data-scroll]')).map(el=>[el.dataset.scroll,[el.scrollLeft,el.scrollTop]]));
 const total=list.reduce((a,p)=>a+(p.equity??NaN),0),start=list.reduce((a,p)=>a+p.initial_cash,0),live=list.filter(p=>auto.config[p.name]?.enabled).length,exitsOnly=list.filter(p=>auto.config[p.name]?.enabled&&auto.config[p.name]?.exits_only).length;
 $('paperTotal').innerHTML=Number.isFinite(total)?`<div class="muted">${live} of ${list.length} on${exitsOnly?` · ${exitsOnly} exits only`:''}</div><div class="big">${inr(total)}</div><div class="${tone(total-start)}">${signedInr(total-start)} · ${pct((total-start)/start*100)}</div>`:'';
 // Best total return first, so the leaders are easy to spot; baskets without a value yet go last, ties keep their order.
 const ret=p=>p.pnl!=null&&p.initial_cash?p.pnl/p.initial_cash:-Infinity;
 $('basketGrid').innerHTML=list.map((p,i)=>[p,i]).sort((a,b)=>ret(b[0])-ret(a[0])||a[1]-b[1]).map(([p])=>basketCard(p,auto)).join('');
 $('basketGrid').querySelectorAll('[data-width]').forEach(el=>el.style.width=el.dataset.width+'%');
 if(typeof quantData!=='undefined'&&quantData&&typeof renderQuant==='function')renderQuant();
 const current=list.find(p=>p.name===basket);$('basketDetail').innerHTML=current?basketDetail(current,auto,openDetails):'<div class="empty">No baskets.</div>';
 root.querySelectorAll('[data-scroll]').forEach(el=>{const pos=scrolls.get(el.dataset.scroll);if(pos){el.scrollLeft=pos[0];el.scrollTop=pos[1];}});
 applyDetailGroup();
 root.querySelectorAll('[data-basket]').forEach(card=>{const pick=()=>{basket=card.dataset.basket;try{localStorage.setItem('marketdesk-basket',basket);}catch{}renderPaper();$('basketDetail').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});};card.onclick=pick;card.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target===card){e.preventDefault();pick();}};});
 root.querySelectorAll('[data-toggle]').forEach(sw=>sw.onclick=async e=>{e.stopPropagation();if(automationAction)return;automationAction=true;const name=sw.dataset.toggle,on=sw.getAttribute('aria-checked')!=='true';sw.setAttribute('aria-checked',String(on));try{await api('/api/paper/automation',{portfolio:name,enabled:on});toast(`${basketTitle(name)} ${on?'turned on':'turned off'}.`);}catch(err){toast(err.message);}finally{automationAction=false;await renderPortfolios();}});
}
async function renderPortfolios(){try{
 if(Date.now()-backtestAt>300000){backtestAt=Date.now();api('/api/backtests').then(d=>{backtestData=d;renderPaper();if(typeof renderQuant==='function'&&typeof quantData!=='undefined'&&quantData)renderQuant();}).catch(()=>{backtestAt=0;});}
 paperData=await api('/api/paper');renderPaper();}catch(e){toast(e.message);}}
async function refresh(symbols=[]){try{await api('/api/refresh',{symbols,mode:!symbols.length&&health.refresh?.scope==='all'&&['failed','interrupted'].includes(health.refresh.status)?health.refresh.mode:'both'});toast('Data refresh started. See Data health for progress.');await update();}catch(e){toast(e.message);}}
async function update(){if(polling)return;polling=true;try{const [h,s]=await Promise.all([api('/api/health'),api('/api/stocks')]);health=h;csrf=h.csrf;stocks=s.stocks;if(selected&&!stocks.some(x=>x.symbol===selected)){selected='';detail=null;$('detail').textContent='Select a Nifty 500 stock.';}renderHealth();renderTable();if(!selected&&stocks.length)await selectStock(stocks.find(x=>x.symbol==='MARUTI')?.symbol||stocks[0].symbol);else if(selected&&$('detailPrice')){const x=stocks.find(x=>x.symbol===selected);if(x){const v=priceView(x);$('detailPrice').textContent='₹'+fmt(v.price);$('priceTime').textContent=v.label+' · '+v.time;$('priceTime').className=v.stale?'warn':'muted';}}if(tab==='paper')await renderPortfolios();}catch(e){$('connection').textContent='LOCAL SERVER UNAVAILABLE';}finally{polling=false;}}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('active',n===b));$('research').hidden=!['trader','investor'].includes(tab);$('paper').hidden=tab!=='paper';$('stats').hidden=true;$('health').hidden=tab!=='health';if(tab==='paper')renderPortfolios();else if(['trader','investor'].includes(tab)){renderTable();renderDetail();}});
$('search').oninput=$('filter').onchange=()=>{page=0;renderTable();};$('prev').onclick=()=>{page--;renderTable();};$('next').onclick=()=>{page++;renderTable();};$('settingsOpen').onclick=()=>$('settings').showModal();$('settingsClose').onclick=()=>$('settings').close();$('refreshAll').onclick=()=>refresh();$('repairFundamentals').onclick=async()=>{try{await api('/api/refresh',{mode:'fundamental_repair'});toast('Targeted fundamentals repair started.');await update();}catch(e){toast(e.message);}};
$('tokenForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/token',{token:$('token').value});$('token').value='';toast('Token saved locally.');await update();}catch(e){toast(e.message);}};
$('connect').onclick=async()=>{try{await api('/api/feed/start',{});toast('Connecting to Upstox…');await update();}catch(e){toast(e.message);}};
$('disconnect').onclick=async()=>{try{await api('/api/feed/stop',{});await update();}catch(e){toast(e.message);}};
window.addEventListener('resize',()=>{if(detail&&!$('research').hidden)chart(detail.candles);});update();setInterval(update,5000);
