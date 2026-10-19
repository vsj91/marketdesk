'use strict';
// Quant baskets tab. Reuses helpers from app.js ($, esc, fmt, pct, inr, tone, signedInr, kpi, group, stockReference, api).
let quantData=null,quantPick='smart_beta',quantBusy=false;try{quantPick=localStorage.getItem('marketdesk-quant')||quantPick;}catch{}
const QUANT_RULES={
 multifactor:'Ranks Nifty 500 stocks on value (EBIT/EV, book/price), quality (ROE, low debt, cash-backed profit), 12-1 month momentum and low volatility. Holds the top 15 equally, at most 30% per sector. Rebalances monthly.',
 risk_parity:'Takes the top 20 multi-factor names and weights each by 1 / 60-day volatility, so calm stocks get more money and no single stock dominates drawdowns. Rebalances every two weeks.',
 stat_arb:'Buys large and mid caps with sound fundamentals when price falls 2+ standard deviations below its 20-day mean while the 200-day average still rises. Sells at the mean (Z ≥ 0) or stops out at Z < −3.2. Checked daily, 10 slots.',
 trend:'Buys 50-day breakouts with the 50-day above the 200-day average and volume over 1.5× normal. Sizes each so a 2×ATR move costs 1% of the basket; trims when ATR expands; exits on a 3×ATR trailing stop. Checked daily.',
 regime:'Reads the Nifty 500 trend and volatility. Bull: 70% momentum, 30% quality. Choppy: 50% quality, 50% low-volatility. Bear: 60% low-beta defensive, 40% deep value. A regime change needs 3 sessions of confirmation.'};
function quantCard(b){
 const gain=b.nav-b.capital,invested=b.nav?Math.max(0,Math.min(100,(b.nav-b.cash)/b.nav*100)):0;
 return `<div class="basket ${b.basket===quantPick?'selected':''}" role="button" tabindex="0" aria-pressed="${b.basket===quantPick}" data-quant="${esc(b.basket)}"><div class="basket-top"><span class="basket-name">${esc(b.title)}</span></div><div class="basket-value">${inr(b.nav)}</div><div class="basket-change ${tone(gain)}">${signedInr(gain)} · ${pct(gain/b.capital*100)}</div><div class="alloc"><i data-width="${invested.toFixed(1)}"></i></div><div class="alloc-label"><span>${fmt(invested,0)}% invested</span><span>${b.holdings.length} held</span></div><div class="basket-status"><span class="dot ${b.as_of?'up':''}"></span>${b.as_of?`Marked ${esc(b.as_of)}${b.regime?' · '+esc(b.regime):''}`:'Starts after the next completed session'}</div></div>`;
}
function quantHoldings(b){
 if(!b.holdings.length)return `<div class="list"><div class="row plain"><div class="l"><small>No holdings yet. Orders are queued on a completed session and filled on the next one at its typical price.</small></div></div></div>`;
 return `<div class="list">${[...b.holdings].sort((x,y)=>y.market_value-x.market_value).map(h=>`<div class="row"><div class="l"><strong>${stockReference(h.ticker)}</strong><small>${h.shares} shares · entry ₹${fmt(h.entry_price)} on ${esc(h.entry_day)} · weight ${fmt(h.weight_pct,1)}%${h.target_weight_pct!=null?` (target ${fmt(h.target_weight_pct,1)}%)`:''}</small></div><div class="r"><strong>${inr(h.market_value)}</strong><small>₹${fmt(h.current_price)} close · ${signedInr(h.unrealized_pnl)}</small></div><span class="chip ${tone(h.unrealized_pnl)}">${pct(h.unrealized_pnl_pct)}</span></div>`).join('')}</div>`;
}
function quantLog(b){
 if(!b.rebalance_log.length)return '<p class="muted">Nothing yet.</p>';
 return b.rebalance_log.map(r=>`<div class="event"><strong>${esc(r.action)}${r.ticker?' · '+stockReference(r.ticker):''}</strong><p>${esc(r.reason)}</p><small>${esc(r.day)}</small></div>`).join('');
}
function quantDetail(b,open){
 const p=b.performance,st=quantData.status||{};
 const pending=b.pending_orders.length?b.pending_orders.map(o=>`<div class="event"><strong>${o.side==='BUY'?'Buy':'Sell'} ${o.shares} ${stockReference(o.ticker)}</strong><p>${esc(o.reason)}</p><small>Signal ${esc(o.signal_day)} · fills next session at the typical price</small></div>`).join(''):'<p class="muted">No orders waiting.</p>';
 const metric=(x,d=2)=>x==null?'—':fmt(x,d);
 return `<div class="detail-top"><div><h2>${esc(b.title)}</h2><p>${esc(QUANT_RULES[b.strategy]||'')}</p></div></div>
 <div class="status-banner"><span class="dot ${st.state==='ok'?'up':st.state==='error'?'down':'warn'}"></span><div><strong>${st.state==='ok'?'Running daily':st.state==='error'?'Daily run failed':'Waiting'}</strong><p>${esc(st.message||'')}</p><small class="muted">Checked ${esc(dt(st.checked_at))} · last rebalance ${esc(b.last_rebalance||'not yet')}</small></div></div>
 <div class="kpis">${kpi('Value',inr(b.nav),`Started with ${inr(b.capital)}`)}${kpi('Return',`<span class="${tone(p.cumulative_return_pct)}">${pct(p.cumulative_return_pct)}</span>`,`Nifty 500 ${pct(p.benchmark_return_pct)}`)}${kpi('Sharpe / Sortino',`${metric(p.sharpe)} / ${metric(p.sortino)}`,`Volatility ${p.annualized_volatility_pct==null?'—':fmt(p.annualized_volatility_pct,1)+'%'}`)}${kpi('Max drawdown',p.max_drawdown_pct==null?'—':fmt(p.max_drawdown_pct,1)+'%',`Ulcer index ${metric(p.ulcer_index)}`)}</div>
 <p class="muted">${p.sessions} session${p.sessions===1?'':'s'} tracked · ${p.fills} fills · charges ${inr(p.total_charges)} · realized ${signedInr(p.realized_pnl)} · cash ${inr(b.cash)}. Ratios need a few weeks of history before they mean much.</p>
 <div class="section-title"><h3>Holdings</h3><span>Gain after buy charges</span></div>${quantHoldings(b)}
 ${group('q-'+b.basket+'-pending','Orders for the next session',b.pending_orders.length?String(b.pending_orders.length):'',pending,open.has('q-'+b.basket+'-pending'))}
 ${group('q-'+b.basket+'-log','Rebalance audit log',b.rebalance_log.length?`latest ${b.rebalance_log.length}`:'',quantLog(b),open.has('q-'+b.basket+'-log'))}`;
}
function renderQuant(){
 if(!quantData)return;const list=quantData.baskets,root=$('quant');
 if(!list.length){$('quantGrid').innerHTML='';$('quantDetail').innerHTML=`<div class="empty">${esc(quantData.status?.message||'Baskets are created on the first daily run.')}</div>`;return;}
 if(!list.some(b=>b.basket===quantPick))quantPick=list[0].basket;
 const open=new Set(Array.from(root.querySelectorAll('details[open]')).map(el=>el.dataset.disclosure));
 const total=list.reduce((a,b)=>a+b.nav,0),start=list.reduce((a,b)=>a+b.capital,0);
 $('quantTotal').innerHTML=`<div class="muted">All quant baskets · paper</div><div class="big">${inr(total)}</div><div class="${tone(total-start)}">${signedInr(total-start)} · ${pct((total-start)/start*100)}</div>`;
 $('quantGrid').innerHTML=list.map(quantCard).join('');
 $('quantGrid').querySelectorAll('[data-width]').forEach(el=>el.style.width=el.dataset.width+'%');
 $('quantDetail').innerHTML=quantDetail(list.find(b=>b.basket===quantPick),open);
 root.querySelectorAll('[data-quant]').forEach(card=>{const pick=()=>{quantPick=card.dataset.quant;try{localStorage.setItem('marketdesk-quant',quantPick);}catch{}renderQuant();};card.onclick=pick;card.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target===card){e.preventDefault();pick();}};});
}
async function loadQuant(){if(quantBusy)return;quantBusy=true;try{quantData=await api('/api/quant-baskets');renderQuant();}catch(e){$('quantDetail').innerHTML=`<div class="empty">${esc(e.message)}</div>`;}finally{quantBusy=false;}}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{
 const on=b.dataset.tab==='quant';$('quant').hidden=!on;
 if(on){$('stats').hidden=true;$('universeNote').hidden=true;loadQuant();}
}));
setInterval(()=>{if(!$('quant').hidden)loadQuant();},30000);
