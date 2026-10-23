const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=x=>x==null?'—':(x>0?'+':'')+Number(x).toFixed(1)+'%';
const tone=x=>x==null||Math.abs(x)<.05?'flat':x>0?'up':'down';
const cell=x=>`<td class="${tone(x)}">${pct(x)}</td>`;
const TITLES={trader:'Trader',investor:'Investor',momentum:'Momentum (6-1)',lowvol:'Low volatility',reversion:'Mean reversion',high52:'52-week high',
 momentum12:'Momentum 12-1',alpha:'Alpha',clenow:'Stocks on the Move',vol_momentum:'Vol-adjusted momentum',residual_momentum:'Residual momentum',smooth_momentum:'Smooth momentum',volume_surge:'Volume surge',pullback:'Pullback in leaders',low_max:'Low max-return',reversal:'Short-term reversal',minervini:'Minervini trend template',vcp:'Minervini VCP',conservative:'Conservative Formula',turtle:'Turtle breakout',connors:'Connors RSI-2',dual_momentum:'Dual Momentum',golden_cross:'Golden cross + ADX',macd:'MACD crossover',bollinger_rsi:'Bollinger + RSI',quality_value:'Quality at a fair price',magic_formula:'Magic Formula',net_buyback:'Net buybacks',dividend_quality:'Dividend + quality',value_momentum:'Value + momentum',overnight:'Overnight momentum',gap_drift:'Gap-up drift',sector_rotation:'Sector rotation',
 smart_beta:'Multi-factor',risk_parity:'Risk parity',stat_arb:'Stat-arb',regime:'Regime switching',news_momentum:'News Momentum',
 news_catalyst:'News Catalyst',ema_price_action:'EMA + Price Action',vinay_intraday:'Vinay Intraday'};
const title=n=>TITLES[n]||n;
const VERDICT={pass:['up','Beat the hold'],mixed:['warn','Mixed'],fail:['down','Behind the hold'],untested:['','Not testable']};
function verdict(b){const [cls,label]=VERDICT[b?.verdict]||VERDICT.untested;return `<span class="tag ${cls}">${esc(label)}</span>`}
async function load(){
 let d;try{const r=await fetch('/api/backtests');d=await r.json();if(!r.ok)throw new Error(d.error||r.status)}catch(e){document.getElementById('rows').innerHTML=`<tr><td colspan="9">Could not load backtests: ${esc(e.message)}</td></tr>`;return}
 document.getElementById('asof').textContent=d.generated?`Backtests saved ${d.generated} · paper live`:'Paper live';
 if(d.missing){const m=document.getElementById('missing');m.hidden=false;m.textContent=d.missing}
 const b=d.benchmarks||{};
 document.getElementById('bench').innerHTML=[['Equal-weight hold, 2018–2026',b.ew_cagr,'500 most traded stocks each month, no costs'],['Nifty 500 index, 2018–2026',b.nifty500_cagr,'price index, no dividends'],
  ['Baskets beating the hold',null,`${d.baskets.filter(x=>x.backtest?.verdict==='pass').length} of ${d.baskets.filter(x=>x.backtest&&x.backtest.verdict!=='untested').length} tested`],['Paper record',null,(()=>{const s=d.baskets.map(x=>x.paper?.since).filter(Boolean).sort()[0];return s?`first trade ${s}`:'no trades yet'})()]]
  .map(([l,v,s])=>`<div class="stat"><div class="stat-label">${esc(l)}</div><div class="stat-value ${v==null?'':tone(v)}">${v==null?'':pct(v)+' a year'}</div><small>${esc(s)}</small></div>`).join('');
 document.getElementById('rows').innerHTML=d.baskets.map(x=>{const t=x.backtest||{},f=t.full||{},p=x.paper||{};
  const when=t.window?`${t.window[0].slice(0,7)} → ${t.window[1].slice(0,7)}${t.trades!=null?` · ${t.trades} trades`:''}`:'';
  const name=`<strong>${esc(title(x.name))}</strong>${x.state==='retired'?' <span class="tag">Retired</span>':x.state==='off'?' <span class="tag">Off</span>':x.state==='exits_only'?' <span class="tag" title="No new buys; stops and exits keep running">Exits only</span>':''}<small>${esc(when)}</small><small>${esc(t.method||'')}</small>`;
  return `<tr><td class="wrap">${name}</td>${cell(f.cagr)}${cell(t.hold_cagr)}<td>${f.maxdd==null?'—':'−'+f.maxdd+'%'}</td><td class="wrap">${verdict(t)}${t.note?`<small>${esc(t.note)}</small>`:''}</td>
   <td>${p.since?esc(p.since)+`<br><small class="muted">${p.days} day${p.days===1?"":"s"} · ${p.trades} trade${p.trades===1?"":"s"}</small>`:'<span class="muted">No trades yet</span>'}</td>${cell(p.return_pct)}${cell(p.nifty500_pct)}${cell(p.backtest_pace_pct)}</tr>`}).join('');
 const years=[...new Set(d.baskets.flatMap(x=>Object.keys(x.backtest?.years||{})))].sort();
 document.getElementById('yearHead').innerHTML=`<tr><th>Basket</th>${years.map(y=>`<th>${y}</th>`).join('')}</tr>`;
 const hold=b.ew_years||{};
 document.getElementById('years').innerHTML=`<tr><td><strong>Equal-weight hold</strong></td>${years.map(y=>cell(hold[y])).join('')}</tr>`+
  d.baskets.filter(x=>x.backtest?.years).map(x=>`<tr><td>${esc(title(x.name))}</td>${years.map(y=>cell(x.backtest.years[y])).join('')}</tr>`).join('');
 document.getElementById('candidates').innerHTML=(d.candidates||[]).map(c=>`<tr><td class="wrap"><strong>${esc(c.title)}</strong><small>${esc(c.family||'')}</small></td>${cell(c.cagr)}${cell(c.h1)}${cell(c.h2)}<td>${c.maxdd==null?'—':'−'+c.maxdd+'%'}</td>${cell(c.top200)}
  <td class="wrap">${c.passed?'<span class="tag up">Passed every check</span>':`<span class="tag down">Failed</span><small>${esc((c.failed||[]).join(', '))}</small>`}</td><td class="wrap">${c.basket?esc(title(c.basket)):`<small>${esc(c.why_not||'')}</small>`}</td></tr>`).join('');
 document.getElementById('method').innerHTML=(d.method||[]).map(m=>`<li>${esc(m)}</li>`).join('');
}
load();
