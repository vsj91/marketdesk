"""Research-only entry confirmation; consumes completed candles, never broker orders."""
from collections import deque

NAME='confirmed_breakout_v1'
SPEC={
    'opening_minutes':15,
    'confirmation_minutes':3,
    'loss_cooldown_sessions':3,
    'description':'Complete traded opening range; three consecutive minute closes above the opening high, prior 20-day high and cumulative minute VWAP estimate; three subsequent observed sessions blocked after a losing exit.',
}


class ConfirmedBreakout:
    def __init__(self):
        self.blocked_through={}
        self.states={}

    def start_day(self,index,eligible):
        self.day_index=index
        self.states={symbol:{'opening_count':0,'opening_high':0.,'pv':0.,'volume':0,'recent':deque(maxlen=3),'minute':-1} for symbol in eligible}

    def observe(self,minute,bars,signals):
        for symbol,state in self.states.items():
            bar=bars.get(symbol)
            if not bar or bar['volume']<=0:
                state['recent'].clear();state['minute']=minute
                continue
            if minute!=state['minute']+1:state['recent'].clear()
            state['minute']=minute
            typical=(bar['high']+bar['low']+bar['close'])/3
            state['pv']+=typical*bar['volume'];state['volume']+=bar['volume']
            if minute<SPEC['opening_minutes']:
                state['opening_count']+=1
                state['opening_high']=max(state['opening_high'],bar['high'])
                state['recent'].clear()
            else:
                threshold=max(state['opening_high'],signals[symbol]['high20'],state['pv']/state['volume'])
                state['recent'].append(bar['close']>threshold)

    def permits(self,symbol,price):
        state=self.states.get(symbol)
        if self.day_index<=self.blocked_through.get(symbol,-1):return False
        if not state or state['opening_count']!=SPEC['opening_minutes']:return False
        if len(state['recent'])!=SPEC['confirmation_minutes'] or not all(state['recent']):return False
        return price>max(state['opening_high'],state['pv']/state['volume'])

    def exited(self,symbol,realized_pnl):
        # Each losing partial exit starts/extends the cooldown; no future outcome is needed.
        if realized_pnl<0:self.blocked_through[symbol]=self.day_index+SPEC['loss_cooldown_sessions']
