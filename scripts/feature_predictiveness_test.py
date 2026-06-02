"""
Feature-predictiveness test (out-of-sample).

Question: does ANY signal-time feature, or combination, predict net-win
(after full statutory costs)? If a model can rank a profitable subset on
UNSEEN data, selective-fire has a real basis. If not, the data has no edge.

Honest discipline: time-ordered split (train older 70%, test newest 30%),
mirroring live deployment. In-sample fit is ignored — only OOS counts.
"""
import json, statistics
from collections import Counter
import numpy as np

entries = []
with open('logs/signal_journal.jsonl') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            pass

closed = [e for e in entries
          if e.get('outcome') and e.get('pnl_rupees') is not None and e.get('ts')]
closed.sort(key=lambda e: e['ts'])

# ---- cost model (same statutory stack as the audit) ----
lots = []
for e in closed:
    ep, xp, pnl = e.get('entry_prem'), e.get('exit_prem'), e.get('pnl_rupees')
    if ep and xp is not None and abs(xp - ep) > 0.05:
        l = abs(pnl / (xp - ep))
        if 10 <= l <= 10000:
            lots.append(l)
med_lot = statistics.median(lots) if lots else 400


def statutory(ep, xp, lot):
    bt, st = ep * lot, xp * lot
    return (40.0 + 0.000625 * st + 0.0003503 * (bt + st) + (bt + st) * 1e-6
            + 0.00003 * bt + 0.18 * (40.0 + 0.0003503 * (bt + st)))


def net_pnl(e):
    ep, xp, pnl = e.get('entry_prem'), e.get('exit_prem'), e.get('pnl_rupees')
    if ep and xp is not None:
        lot = abs(pnl / (xp - ep)) if abs(xp - ep) > 0.05 else med_lot
        if not (10 <= lot <= 10000):
            lot = med_lot
        return pnl - statutory(ep, xp, lot)
    return pnl


# ---- features: SIGNAL-TIME ONLY (no exit/outcome/mfe leakage) ----
top_patterns = [p for p, _ in Counter(
    p for e in closed for p in (e.get('patterns') or [])).most_common(20)]
grade_ord = {'C': 0, 'B': 1, 'A': 2, 'S': 3}


def hour_of(e):
    try:
        return int(e['ts'][11:13])
    except Exception:
        return 10


def dow_of(e):
    import datetime as dt
    try:
        return dt.date.fromisoformat(e['ts'][:10]).weekday()
    except Exception:
        return 0


feat_names = ['rsi', 'volume_ratio', 'score', 'vote_margin', 'iv_pct', 'delta',
              'theta', 'is_long', 'grade_ord', 'hour', 'dow', 'n_patterns',
              'bias_bull', 'bias_bear', 'vol_high']


def featurize(e):
    d = e.get('direction', 'long')
    bias = str(e.get('market_bias', 'neutral')).lower()
    vol = str(e.get('volatility', 'NORMAL')).upper()
    row = [
        float(e.get('rsi', 50) or 50),
        float(e.get('volume_ratio', 1) or 1),
        float(e.get('score', 0) or 0),
        float(e.get('vote_margin', 0) or 0),
        float(e.get('iv_pct', 0) or 0),
        float(e.get('delta', 0) or 0),
        float(e.get('theta', 0) or 0),
        1.0 if d == 'long' else 0.0,
        float(grade_ord.get(e.get('grade', 'C'), 0)),
        float(hour_of(e)),
        float(dow_of(e)),
        float(len(e.get('patterns') or [])),
        1.0 if 'bull' in bias else 0.0,
        1.0 if 'bear' in bias else 0.0,
        1.0 if ('HIGH' in vol or 'EXTREME' in vol) else 0.0,
    ]
    for p in top_patterns:
        row.append(1.0 if p in (e.get('patterns') or []) else 0.0)
    return row


allnames = feat_names + ['pat:' + p for p in top_patterns]
X = np.array([featurize(e) for e in closed])
nets = np.array([net_pnl(e) for e in closed])
y = (nets > 0).astype(int)
N = len(y)

print(f'Samples: {N} | overall net win rate: {100*y.mean():.1f}%')
wins, losses = nets[nets > 0], nets[nets < 0]
rr = wins.mean() / -losses.mean()
be = 100 / (1 + rr)
print(f'Payoff R:R = {rr:.1f}:1  ->  breakeven WR = {be:.1f}%')

cut = int(N * 0.70)
Xtr, Xte = X[:cut], X[cut:]
ytr, yte = y[:cut], y[cut:]
nets_te = nets[cut:]
print(f'Train: {len(ytr)} (WR {100*ytr.mean():.1f}%) | '
      f'Test: {len(yte)} (WR {100*yte.mean():.1f}%)')

from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score

sc = StandardScaler().fit(Xtr)
Xtr_s, Xte_s = sc.transform(Xtr), sc.transform(Xte)

lr = LogisticRegression(max_iter=2000, class_weight='balanced', C=0.5).fit(Xtr_s, ytr)
p_lr = lr.predict_proba(Xte_s)[:, 1]
gb = GradientBoostingClassifier(n_estimators=80, max_depth=3,
                                learning_rate=0.05).fit(Xtr, ytr)
p_gb = gb.predict_proba(Xte)[:, 1]

print('\n' + '=' * 70)
print('OUT-OF-SAMPLE (test = newest 30%, never seen in training)')
print('=' * 70)
for name, p in [('LogReg', p_lr), ('GBM', p_gb)]:
    try:
        auc = roc_auc_score(yte, p)
    except Exception:
        auc = float('nan')
    print(f'{name}: OOS AUC = {auc:.3f}   (0.50 = coin flip / no skill)')

print('\nSELECTIVE-FIRE on OOS test (rank by model P(win), fire top X%):')
hdr = f'{"model":7s} {"topX":>5s} {"n":>4s} {"netWR%":>7s} {"PF":>6s} {"Rs/trade":>9s} {"total":>9s}  edge'
print(hdr)
for name, p in [('LogReg', p_lr), ('GBM', p_gb)]:
    order = np.argsort(-p)
    for frac in [0.10, 0.25, 0.50]:
        k = max(5, int(len(p) * frac))
        sub = nets_te[order[:k]]
        w, l = sub[sub > 0], sub[sub < 0]
        wr = 100 * len(w) / len(sub)
        pf = w.sum() / -l.sum() if len(l) else 999
        flag = 'PROFIT' if sub.mean() > 0 else ''
        print(f'{name:7s} {int(frac*100):4d}% {len(sub):4d} {wr:7.1f} {pf:6.2f} '
              f'{sub.mean():+9.0f} {sub.sum():+9,.0f}  {flag}')

print('\nTOP FEATURES (GBM importance):')
for n, v in sorted(zip(allnames, gb.feature_importances_), key=lambda x: -x[1])[:10]:
    print(f'  {n:22s} {v:.3f}')
