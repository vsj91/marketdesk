'use strict';
// Signal baskets inside the Paper baskets tab.
// Adds a card line and detail sections to the existing basket views; reuses helpers from app.js.
let signalData=null,signalBusy=false;
const SIGNAL_NAMES=['news_momentum','news_catalyst','ema_price_action','vinay_intraday','liquidity_sweep_reversal','order_flow_reversal','volatility_compression_breakout','orb_volume_breakout','sector_relative_strength'];
const INTRADAY=['ema_price_action','vinay_intraday','liquidity_sweep_reversal','order_flow_reversal','volatility_compression_breakout','orb_volume_breakout'];
function signalBasket(n){return signalData?.baskets?.find(b=>b.name===n);}
function signalCardLine(n){
 const b=signalBasket(n);if(!b)return '';
 const t=b.today||{},open=b.positions.length;
 if(n==='ema_price_action'){const pending=b.signals.filter(s=>s.status==='ENTRY_PENDING').length,partial=b.positions.filter(p=>p.partial_done).length;
  return `<div class="alloc-label"><span>${(t.ENTRY_PENDING||0)+(t.ENTRY_FILLED||0)+(t.REJECTED||0)+(t.EXPIRED||0)+(t.CANCELLED||0)} crossovers today · ${pending} armed</span><span>${open} open SL/TP${partial?` · ${partial} trailing`:''}</span></div>`;}
 if(n==='vinay_intraday'){const m=b.runner?.vinay_scan||{},trail=b.positions.filter(p=>p.trail_active).length;
  return `<div class="alloc-label"><span>${m.scanned??0} scanned · ${b.signals.filter(s=>s.status==='BUY_CANDIDATE').length} candidates</span><span>${open} open SL/TP${trail?` · ${trail} trailing`:''}</span></div>`;}
 if(n==='sector_relative_strength'){const x=b.checkpoints.sector||{};return `<div class="alloc-label"><span>${(x.sectors||[]).map(s=>esc(s.sector)).join(' · ')||'Waiting for Monday review'}</span><span>${open}/10 positions</span></div>`;}
 if(INTRADAY.includes(n)){const m=b.runner?.[n+'_scan']||{},trail=b.positions.filter(p=>p.trail_active||p.stage==='ATR_TRAILING').length;
  return `<div class="alloc-label"><span>${m.scanned??0} scanned · ${b.signals.filter(s=>s.status==='BUY_CANDIDATE').length} candidates</span><span>${open} open SL/TP${trail?` · ${trail} trailing`:''}</span></div>`;}
 if(n==='news_momentum')return `<div class="alloc-label"><span>${b.news.length} news items · ${b.signals.filter(s=>s.status==='WATCHLIST').length} watch</span><span>${b.signals.filter(s=>s.status==='BUY_CANDIDATE').length} confirmed</span></div>`;
 const best=b.signals.filter(s=>['BUY_CANDIDATE','WATCHLIST'].includes(s.status)).sort((x,y)=>(y.score||0)-(x.score||0))[0];
 return `<div class="alloc-label"><span>${b.signals.filter(s=>s.status==='BUY_CANDIDATE').length} selected · ${b.signals.filter(s=>s.status==='WATCHLIST').length} watch</span><span>${best?`top score ${fmt(best.score,0)}`:'no catalyst yet'}</span></div>`;
}
const STATUS_TONE={BUY_CANDIDATE:'up',ENTRY_PENDING:'up',ENTRY_FILLED:'up',WATCHLIST:'warn',SIGNAL_DETECTED:'warn',REJECTED:'',EXPIRED:'',CANCELLED:'',EXIT_REDUCE:'down'};
function signalRows(b){
 if(!b.signals.length)return '<p class="muted">No decisions yet.</p>';
 return b.signals.slice(0,40).map(s=>{const e=s.evidence||{};
  const levels=s.initial_stop!=null?`<small>${s.entry_trigger!=null?`Trigger ₹${fmt(s.entry_trigger)} · `:''}${s.limit_price!=null?`limit ₹${fmt(s.limit_price)} · `:''}SL ₹${fmt(s.initial_stop)} · TP ₹${fmt(s.initial_target)}${s.risk_reward!=null?` · RR ${fmt(s.risk_reward)}`:''}</small>`:'';
  const ema=b.name==='ema_price_action'&&e.ema9!=null?`<small>EMA9 ${fmt(e.ema9)} / EMA21 ${fmt(e.ema21)} / EMA200 ${fmt(e.ema200)} · RSI ${fmt(e.rsi14,1)} · ATR ${fmt(e.atr14)} (${fmt(e.atr_pct)}%) · support ${e.structural_level==null?'none':'₹'+fmt(e.structural_level)} · resistance ${e.resistance_level==null?'none':'₹'+fmt(e.resistance_level)}</small>`:'';
  const leader=b.name==='vinay_intraday'&&e.day_change_pct!=null?`<small>Day ${fmt(e.day_change_pct)}% · gap ${fmt(e.gap_pct)}% · volume ${fmt(e.rvol,1)}x · opening range high ₹${fmt(e.or_high)} · VWAP ₹${fmt(e.vwap)} (+${fmt(e.vwap_extension_pct)}%) · stop ${fmt(e.stop_pct)}%</small>`:'';
  const intraday=['liquidity_sweep_reversal','order_flow_reversal','volatility_compression_breakout','orb_volume_breakout'].includes(b.name)?`<small>${e.trigger_ts?`${esc(e.trigger_ts.slice(11,16))} · `:''}${e.rvol!=null?`RVOL ${fmt(e.rvol,1)}x · `:''}${e.depth_ratio!=null?`top-five depth ${fmt(e.depth_ratio,1)}:1 · `:''}${e.daily_ema50!=null?`daily EMA50 ₹${fmt(e.daily_ema50)} · `:''}${e.opening_range_high!=null?`opening range high ₹${fmt(e.opening_range_high)} · `:''}${e.level!=null?`reclaimed ₹${fmt(e.level)} · `:''}${e.bb_width_pct!=null?`BB width ${fmt(e.bb_width_pct)}% · `:''}stop ${fmt(e.stop_pct)}%</small>`:'';
  const sector=b.name==='sector_relative_strength'&&e.sector?`<small>${esc(e.sector)} · ${fmt((e.momentum||0)*100,1)}% stock ROC · ${fmt((e.sector_excess_return||0)*100,1)}% vs benchmark · ₹${fmt(e.avg_turnover,0)} average daily turnover</small>`:'';
  const news=!INTRADAY.includes(b.name)&&b.name!=='sector_relative_strength'&&e.components?`<small>Components: ${Object.entries(e.components).map(([k,v])=>`${esc(k)} ${fmt(v,0)}`).join(' · ')}${e.missing?.length?` · missing: ${esc(e.missing.join('; '))}`:''}</small>`:'';
  const score=b.name==='news_catalyst'&&e.score?`<small>Score parts: ${Object.entries(e.score.components||{}).map(([k,v])=>`${esc(k)} ${fmt(v,0)}`).join(' · ')} · coverage ${fmt(e.score.coverage*100,0)}%</small>`:'';
  return `<div class="event"><strong><span class="tag ${STATUS_TONE[s.status]||''}">${esc(s.status.replaceAll('_',' '))}</span> ${stockReference(s.symbol)}${s.score!=null?` · ${b.name==='ema_price_action'?'RR':'score'} ${fmt(s.score,b.name==='ema_price_action'||b.name==='vinay_intraday'?2:0)}`:''}</strong><p>${esc(s.explanation)}</p>${s.rejection_reason?`<p class="muted">Why not: ${esc(s.rejection_reason)}</p>`:''}${levels}${ema}${leader}${intraday}${sector}${news}${score}<small>${esc(dt(s.updated_at))}${s.expires_at?` · expires ${esc(s.expires_at.length>10?dt(s.expires_at):s.expires_at)}`:''}</small></div>`;}).join('');
}
function positionRows(b){
 if(!b.positions.length)return '<p class="muted">No open positions.</p>';
 return `<div class="table-scroll"><table><thead><tr><th>Stock</th><th>Stage</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Last</th></tr></thead><tbody>${b.positions.map(p=>`<tr><td>${stockReference(p.symbol)}<small>since ${esc(dt(p.entry_at))}</small></td><td><span class="tag">${esc(String(p.stage).replaceAll('_',' '))}</span>${p.be_trigger!=null?`<small>BE at ₹${fmt(p.be_trigger)}${['liquidity_sweep_reversal','order_flow_reversal','volatility_compression_breakout'].includes(b.name)?(p.trail_atr_mult?` · trail ${fmt(p.trail_atr_mult,1)} ATR`:''):` · partial at ${fmt(p.partial_trigger)}${p.partial_done?' ✓':''}${p.trail_active?(b.name==='vinay_intraday'?' · trailing':' · EMA20 trail'):''}`}</small>`:''}${p.exit_now?`<small>Exit pending: ${esc(p.exit_now[1])}</small>`:''}${p.pending_exit?`<small>Exit pending: ${esc(p.pending_exit[1])}</small>`:''}</td><td>${p.qty}${p.original_qty&&p.original_qty!==p.qty?' / '+p.original_qty:''}</td><td>${fmt(p.entry??p.average_cost)}</td><td>${p.stop==null?'—':fmt(p.stop)}${p.initial_stop!=null&&p.stop!==p.initial_stop?`<small>initial ${fmt(p.initial_stop)}</small>`:''}</td><td>${p.target==null||p.target_active===false?'—':fmt(p.target)}</td><td>${fmt(p.price)}</td></tr>`).join('')}</tbody></table></div>`;
}
function newsRows(b){
 if(!b.news.length)return '<p class="muted">No analysed announcements yet. NSE corporate announcements are polled every 5 minutes on weekdays.</p>';
 return b.news.map(x=>`<div class="event"><strong><span class="tag ${/Positive/.test(x.label)?'up':/Negative/.test(x.label)?'down':''}">${esc(x.label)}</span> ${stockReference(x.symbol)} · impact ${fmt(x.impact_score,0)} · confidence ${fmt(x.confidence)}</strong><p>${esc(x.subject)}: ${esc(x.headline)}</p><small>${esc(x.category.replaceAll('_',' '))}${x.amount_crore!=null?` · ₹${fmt(x.amount_crore,0)} cr`:''}${x.materiality_ratio!=null?` (${fmt(x.materiality_ratio*100,1)}% of revenue)`:''} · published ${esc(dt(x.published_at))} · retrieved ${esc(dt(x.retrieved_at))} · ${esc(x.verification)}</small>${x.notes?.length?`<small>${esc(x.notes.join(' · '))}</small>`:''}${x.missing?.length?`<small class="muted">Missing: ${esc(x.missing.join('; '))}</small>`:''}${x.url?`<small><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">NSE filing</a></small>`:''}</div>`).join('');
}
function performanceHtml(b){
 const p=b.performance,m=(x,d=2,s='')=>x==null?'—':fmt(x,d)+s;
 const curve=p.equity_curve||[];let spark='';
 if(curve.length>1){const v=curve.map(x=>x.equity),lo=Math.min(...v),hi=Math.max(...v),w=300,h=60;spark=`<svg viewBox="0 0 ${w} ${h}" class="spark" role="img" aria-label="Equity curve"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${v.map((x,i)=>`${(i/(v.length-1)*w).toFixed(1)},${(h-(hi>lo?(x-lo)/(hi-lo):.5)*h).toFixed(1)}`).join(' ')}"/></svg>`;}
 return `<div class="kpis">${kpi('Closed trades',String(p.trades),`${p.wins} wins · ${p.losses} losses`)}${kpi('Win rate',m(p.win_rate_pct,1,'%'),`Profit factor ${m(p.profit_factor)}`)}${kpi('Expectancy',p.expectancy==null?'—':signedInr(p.expectancy),`Avg win ${p.average_win==null?'—':inr(p.average_win)} · avg loss ${p.average_loss==null?'—':inr(p.average_loss)}`)}${kpi('Max drawdown',m(p.max_drawdown_pct,2,'%'),p.sharpe==null?esc(p.sharpe_note||''):`Sharpe ${fmt(p.sharpe)}`)}</div>
 <p class="muted">Realized ${signedInr(p.realized_pnl)} · fees ${inr(p.fees)} · average holding ${p.average_holding_minutes==null?'—':fmt(p.average_holding_minutes/60,1)+' h'} · capital utilization ${m(p.capital_utilization_pct,1,'%')}. PAPER results only; no profitability is implied until enough trades exist.</p>${spark}
 ${p.closed_trades.length?`<div class="table-scroll"><table><thead><tr><th>Stock</th><th>Opened</th><th>Closed</th><th>P&amp;L after fees</th></tr></thead><tbody>${p.closed_trades.map(c=>`<tr><td>${stockReference(c.symbol)}</td><td>${esc(dt(c.opened))}</td><td>${esc(dt(c.closed))}</td><td class="${tone(c.pnl)}">${signedInr(c.pnl)}</td></tr>`).join('')}</tbody></table></div>`:''}`;
}
function transitionRows(b){
 if(!b.transitions.length)return '<p class="muted">Nothing yet.</p>';
 return b.transitions.slice(0,60).map(t=>`<div class="event"><strong>${t.from_stage?esc(t.from_stage.replaceAll('_',' '))+' → ':''}${esc(t.to_stage.replaceAll('_',' '))}${t.symbol?' · '+stockReference(t.symbol):''}</strong><p>${esc(t.reason)}</p><small>${esc(dt(t.created_at))}</small></div>`).join('');
}
function paramForm(b){
 const fields=Object.entries(b.limits).map(([k,lim])=>{const v=b.params[k];
  const input=typeof lim[0]==='boolean'?`<select name="${esc(k)}"><option value="true" ${v===true?'selected':''}>Yes</option><option value="false" ${v===false?'selected':''}>No</option></select>`
   :typeof lim[0]==='string'&&!/^\d\d:\d\d$/.test(lim[0])?`<select name="${esc(k)}">${lim.map(o=>`<option ${o===v?'selected':''}>${esc(o)}</option>`).join('')}</select>`
   :/^\d\d:\d\d$/.test(String(lim[0]))?`<input name="${esc(k)}" value="${esc(v)}" pattern="\\d\\d:\\d\\d">`
   :lim.length>2?`<select name="${esc(k)}" data-number>${lim.map(o=>`<option ${o===v?'selected':''}>${o}</option>`).join('')}</select>`
   :`<input name="${esc(k)}" type="number" step="any" min="${lim[0]}" max="${lim[1]}" value="${esc(v)}" data-number>`;
  return `<label>${esc(k.replaceAll('_',' '))}<small>${lim.length===2&&typeof lim[0]==='number'?`${lim[0]}–${lim[1]}`:''}</small>${input}</label>`;}).join('');
 return `<form class="signal-config" data-signal-config="${esc(b.name)}"><div class="param-grid">${fields}</div><div class="actions"><button class="primary" type="submit">Save parameters</button></div></form>
 <form class="signal-config" data-signal-capital="${esc(b.name)}"><label>Starting capital (₹)<small>Only before the first trade</small><input name="capital" type="number" min="10000" max="10000000" step="1000" value="${esc(b.capital)}"></label><div class="actions"><button type="submit">Set capital</button></div></form>`;
}
function signalSection(n,open){
 const b=signalBasket(n);if(!b)return SIGNAL_NAMES.includes(n)?'<p class="muted">Loading signal details…</p>':'';
 const r=b.runner||{},news=b.checkpoints.news||{},ema=b.checkpoints.ema||{};
 const live=b.checkpoints.live||{},v=r.vinay_scan||{},misses=v.near_misses?Object.entries(v.near_misses).map(([k,x])=>`${x} ${esc(k)}`).join(', '):'';
 const scan=n+'_scan',liveScan=r[scan]||{};
 const feed=n==='vinay_intraday'?`Live-feed candles: last scan ${esc(dt(live.last_success))} · ${v.scanned??0} of ${live.symbols??0} Nifty 500 stocks had a full session${v.error?` · ${esc(v.error)}`:''}${misses?` · last scan misses: ${misses}`:''}`
  :['liquidity_sweep_reversal','order_flow_reversal','volatility_compression_breakout','orb_volume_breakout'].includes(n)?`Live 5-minute feed: ${esc(dt(liveScan.boundary))} · ${liveScan.scanned??0} symbols scanned · ${liveScan.candidates??0} candidates${liveScan.error?` · ${esc(liveScan.error)}`:''}`
  :n==='sector_relative_strength'?`Weekly rotation: ${esc(b.checkpoints.sector?.as_of||'awaiting completed market history')} · ${(b.checkpoints.sector?.sectors||[]).map(s=>`${esc(s.sector)} +${fmt(s.excess_return*100,1)}% vs benchmark`).join(' · ')||'waiting for Monday review'}`
  :INTRADAY.includes(n)?`Candles: last refresh ${esc(dt(ema.last_success))} · ${ema.symbols??0} symbols${r.ema_cycle?.error?` · ${esc(r.ema_cycle.error)}`:''}`:`NSE announcements: last success ${esc(dt(news.last_success))}${news.ok===false?` · failing: ${esc(news.error)}`:''}`;
 return `<div class="note"><strong>PAPER</strong> · ${esc(b.version)} · every order, stop and target here is simulated; nothing is sent to a broker. ${feed}</div>
 ${group(n+'-signals','Signals and decisions',`${b.signals.length} recent`,signalRows(b),open('signals'))}
 ${group(n+'-positions',INTRADAY.includes(n)?'Open trades: stop, target, stage':'Open positions: stop, target, review',String(b.positions.length),positionRows(b),open('positions'))}
 ${!INTRADAY.includes(n)&&n!=='sector_relative_strength'?group(n+'-news','Announcements and analysis',`${b.news.length}`,newsRows(b),open('news')):''}
 ${group(n+'-perf','Performance (PAPER)','',performanceHtml(b),open('perf'))}
 ${group(n+'-log','State machine and event log',`${b.transitions.length}`,transitionRows(b),open('log'))}
 ${group(n+'-params','Parameters','',paramForm(b),open('params'))}
 ${group(n+'-limits','Data limits','',`<ul>${b.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`,open('limits'))}`;
}
async function loadSignals(){if(signalBusy)return;signalBusy=true;try{signalData=await api('/api/signal-baskets');if(typeof renderPaper==='function')renderPaper();}catch(e){}finally{signalBusy=false;}}
document.addEventListener('submit',async e=>{
 const f=e.target;if(!f.matches('[data-signal-config],[data-signal-capital]'))return;e.preventDefault();
 const name=f.dataset.signalConfig||f.dataset.signalCapital;let body;
 if(f.dataset.signalCapital)body={portfolio:name,capital:Number(f.capital.value)};
 else{const params={},b=signalBasket(name);for(const el of f.elements){if(!el.name)continue;let v=el.value;if(v==='true')v=true;else if(v==='false')v=false;else if(el.dataset.number!==undefined)v=Number(v);if(JSON.stringify(v)!==JSON.stringify(b.params[el.name]))params[el.name]=v;}
  if(!Object.keys(params).length)return toast('No parameter changed.');body={portfolio:name,params};}
 try{await api('/api/signal-baskets/config',body);toast('Saved.');await loadSignals();}catch(err){toast(err.message);}
});
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{if(b.dataset.tab==='paper')loadSignals();}));
setInterval(()=>{if(!$('paper').hidden)loadSignals();},15000);
loadSignals();
