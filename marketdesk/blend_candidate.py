"""Research-only replacement buy rules. Never imported by the trading engine.

Original, transparent hypotheses inspired by quality/growth/value research and
liquid trend trading. These do not reproduce any investor's proprietary strategy.
Thresholds are fixed in v1; no parameter search or automatic activation.
"""
from datetime import datetime,timezone
from .storage import number
VERSION='nifty500-blend-v1'


def trader(signal,price,data_ready=True):
    reasons=[]
    if not signal or not data_ready or not signal.get('quality'):
        return {'eligible':False,'reasons':['Price data checks do not pass'],'score':None}
    if signal['close']<=signal['high20'] or signal['dma50']<=signal['dma200']:reasons.append('Completed breakout and positive long-term trend required')
    if price<20 or signal['turnover20']<50_000_000:reasons.append('Price at least ₹20 and average daily turnover at least ₹5 crore required')
    if signal['volume_ratio']<1.2 or signal['rank']<=0:reasons.append('Volume confirmation and positive 63-session momentum required')
    atr=signal['atr'];vol=atr/signal['close'] if signal['close']>0 else 0
    if not 0<vol<=.04:reasons.append('ATR must be positive and no more than 4% of close')
    if not signal['high20']<=price<=signal['close']+.5*atr:reasons.append('Avoid failed breakouts and entry gaps above half an ATR')
    return {'eligible':not reasons,'reasons':reasons,'score':signal['rank']/vol if vol>0 else None}


def investor(details,quality,sector,isin,review=None,clock=None):
    clock=clock or datetime.now(timezone.utc);review=review or {};reasons=[]
    latest=details.get('latest') or {};summary=details.get('summary') or {}
    if not quality.get('investor_ready'):reasons.append('Annual, growth, source and price checks must pass')
    # Banks/insurers/NBFCs need their own capital and asset-quality model.
    financial=any(k in (sector or '').lower() for k in ('bank','finance','nbfc','insurance','asset management'))
    if financial:reasons.append('Financial-sector capital/asset-quality review required')
    values={k:number(summary.get(k)) for k in ('net_profit','revenue_growth_yoy','net_profit_growth_yoy')}
    equity=number(latest.get('equity'));debt=number(latest.get('total_borrowings'));leases=number(latest.get('total_lease_liabilities'));pbt=number(latest.get('profit_before_tax'));cost=number(latest.get('finance_costs'))
    if any(v is None for v in values.values()) or any(v is None for v in (equity,debt,leases,pbt,cost)):
        reasons.append('Comparable profitability and balance-sheet inputs missing')
    else:
        if equity<=0 or values['net_profit']<=0 or pbt<=0:reasons.append('Positive equity and annual profit required')
        elif values['net_profit']/equity<.15:reasons.append('Annual profit / closing equity below 15% screen')
        if values['revenue_growth_yoy']<10 or values['net_profit_growth_yoy']<10:reasons.append('Comparable revenue and profit growth must both reach 10%')
        if not financial and (equity<=0 or (debt+leases)/equity>.75):reasons.append('Borrowings plus leases / equity exceeds 0.75')
        if cost<0 or (cost>0 and (pbt+cost)/cost<4):reasons.append('Finance-cost coverage below 4 times')
    quantitative=not reasons
    # Market-cap category and AMFI average market cap are NOT current valuation.
    try:
        stamp=datetime.fromisoformat(review['observed_at'])
        valid=review.get('isin')==isin and 0<=(clock-stamp).total_seconds()<=90*86400
    except (KeyError,TypeError,ValueError):valid=False
    for field in ('valuation','governance','business_quality'):
        item=review.get(field,{})
        if not valid or item.get('approved') is not True or not str(item.get('source_url','')).startswith('https://'):
            reasons.append('Current documented '+field.replace('_',' ')+' review required')
    return {'eligible':not reasons,'quantitative_pass':quantitative,'reasons':reasons}
