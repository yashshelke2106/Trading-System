"""
Walk-forward validation of the GBM ranker.

Decisive gate: is the single-split OOS edge real, or luck? Expanding-window
walk-forward — each fold trains only on the past, tests on the next unseen
block. Headline = POOLED out-of-sample (all folds concatenated). Per-fold
distribution shows stability. No test data leaks into feature definition.
"""
import json, statistics
from collections import Counter
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score

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
N = len(closed)

# cost model
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


# pattern columns from EARLIEST 40% only (no test leakage)
anchor = int(N * 0.40)
top_patterns = [p for p, _ in Counter(
    p for e in closed[:anchor] for p in (e.get('patterns') or [])).most_common(20)]
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


def featurize(e):
    d = e.get('direction', 'long')
    bias = str(e.get('market_bias', 'neutral')).lower()
    vol = str(e.get('volatility', 'NORMAL')).upper()
    row = [
        float(e.get('rsi', 50) or 50), float(e.get('volume_ratio', 1) or 1),
        float(e.get('score', 0) or 0), float(e.get('vote_margin', 0) or 0),
        float(e.get('iv_pct', 0) or 0), float(e.get('delta', 0) or 0),
        float(e.get('theta', 0) or 0), 1.0 if d == 'long' else 0.0,
        float(grade_ord.get(e.get('grade', 'C'), 0)), float(hour_of(e)),
        float(dow_of(e)), float(len(e.get('patterns') or [])),
        1.0 if 'bull' in bias else 0.0, 1.0 if 'bear' in bias else 0.0,
        1.0 if ('HIGH' in vol or 'EXTREME' in vol) else 0.0,
    ]
    for p in top_patterns:
        row.append(1.0 if p in (e.get('patterns') or []) else 0.0)
    return row


X = np.array([featurize(e) for e in closed])
nets = np.array([net_pnl(e) for e in closed])
y = (nets > 0).astype(int)

rr = nets[nets > 0].mean() / -nets[nets < 0].mean()
be = 100 / (1 + rr)
print(f'N={N}  net WR={100*y.mean():.1f}%  R:R={rr:.1f}:1  breakeven WR={be:.1f}%')
print(f'Pattern columns frozen from earliest {anchor} trades.\n')

# Expanding walk-forward: 5 folds over the last 60%
n_folds = 5
test_size = (N - anchor) // n_folds
print('=' * 82)
print('WALK-FORWARD FOLDS  (GBM, top-25% selective fire, net of full costs)')
print('=' * 82)
hdr = (f'{"fold":4s} {"trainN":>6s} {"testN":>5s} {"tWins":>5s} '
       f'{"AUC_lr":>6s} {"AUC_gb":>6s} | {"q25_n":>5s} {"q25_WR%":>7s} '
       f'{"q25_PF":>6s} {"q25_Rs/t":>8s} {"q25_tot":>9s}')
print(hdr)

pool_p_gb, pool_p_lr, pool_nets, pool_y = [], [], [], []
fold_profit = 0
for k in range(n_folds):
    tr_end = anchor + k * test_size
    te_end = anchor + (k + 1) * test_size if k < n_folds - 1 else N
    Xtr, ytr = X[:tr_end], y[:tr_end]
    Xte, yte = X[tr_end:te_end], y[tr_end:te_end]
    net_te = nets[tr_end:te_end]
    if len(yte) < 10 or ytr.sum() < 5:
        continue
    sc = StandardScaler().fit(Xtr)
    lr = LogisticRegression(max_iter=2000, class_weight='balanced', C=0.5)
    lr.fit(sc.transform(Xtr), ytr)
    p_lr = lr.predict_proba(sc.transform(Xte))[:, 1]
    gb = GradientBoostingClassifier(n_estimators=60, max_depth=3, learning_rate=0.05)
    gb.fit(Xtr, ytr)
    p_gb = gb.predict_proba(Xte)[:, 1]

    try:
        auc_lr = roc_auc_score(yte, p_lr)
    except Exception:
        auc_lr = float('nan')
    try:
        auc_gb = roc_auc_score(yte, p_gb)
    except Exception:
        auc_gb = float('nan')

    order = np.argsort(-p_gb)
    kq = max(5, int(len(p_gb) * 0.25))
    sub = net_te[order[:kq]]
    w, l = sub[sub > 0], sub[sub < 0]
    wr = 100 * len(w) / len(sub)
    pf = w.sum() / -l.sum() if len(l) else 999
    if sub.mean() > 0:
        fold_profit += 1
    print(f'{k+1:4d} {tr_end:6d} {len(yte):5d} {int(yte.sum()):5d} '
          f'{auc_lr:6.3f} {auc_gb:6.3f} | {len(sub):5d} {wr:7.1f} '
          f'{pf:6.2f} {sub.mean():+8.0f} {sub.sum():+9,.0f}')

    pool_p_gb.extend(p_gb); pool_p_lr.extend(p_lr)
    pool_nets.extend(net_te); pool_y.extend(yte)

# ---- POOLED out-of-sample (headline) ----
pool_p_gb = np.array(pool_p_gb); pool_nets = np.array(pool_nets)
pool_y = np.array(pool_y)
print('\n' + '=' * 82)
print('POOLED OUT-OF-SAMPLE  (every test prediction across all folds, concatenated)')
print('=' * 82)
print(f'Pooled OOS AUC (GBM):    {roc_auc_score(pool_y, pool_p_gb):.3f}')
print(f'Pooled OOS AUC (LogReg): {roc_auc_score(pool_y, np.array(pool_p_lr)):.3f}')
print(f'Folds with profitable top-quartile: {fold_profit}/{n_folds}\n')

order = np.argsort(-pool_p_gb)
print(f'{"fire top":9s} {"n":>4s} {"netWR%":>7s} {"PF":>6s} {"Rs/trade":>9s} {"total":>10s}  verdict')
for frac in [0.10, 0.25, 0.50, 1.00]:
    kq = max(5, int(len(pool_p_gb) * frac))
    sub = pool_nets[order[:kq]]
    w, l = sub[sub > 0], sub[sub < 0]
    wr = 100 * len(w) / len(sub)
    pf = w.sum() / -l.sum() if len(l) else 999
    verdict = 'PROFITABLE' if sub.mean() > 0 else 'loss'
    label = 'ALL (baseline)' if frac == 1.0 else f'{int(frac*100)}%'
    print(f'{label:9s} {len(sub):4d} {wr:7.1f} {pf:6.2f} {sub.mean():+9.0f} '
          f'{sub.sum():+10,.0f}  {verdict}')
