import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import config


@dataclass
class ComponentHealth:
    name: str
    score: float = 100.0
    error_count: int = 0
    success_count: int = 0
    last_error: Optional[str] = None
    last_error_time: Optional[str] = None
    consecutive_failures: int = 0


@dataclass
class RepairRecord:
    timestamp: str
    component: str
    error_type: str
    error_msg: str
    fix_applied: str
    success: bool


class HealthMonitor:
    DECAY = 15.0
    RECOVERY = 2.0
    DEGRADED_THRESHOLD = 50.0
    CRITICAL_THRESHOLD = 20.0

    def __init__(self):
        self._components: Dict[str, ComponentHealth] = {}

    def _get(self, name: str) -> ComponentHealth:
        if name not in self._components:
            self._components[name] = ComponentHealth(name=name)
        return self._components[name]

    def record_success(self, name: str):
        h = self._get(name)
        h.score = min(100.0, h.score + self.RECOVERY)
        h.success_count += 1
        h.consecutive_failures = 0

    def record_failure(self, name: str, exc: Exception):
        h = self._get(name)
        h.score = max(0.0, h.score - self.DECAY)
        h.error_count += 1
        h.consecutive_failures += 1
        h.last_error = type(exc).__name__
        h.last_error_time = datetime.now().isoformat()

    def is_degraded(self, name: str) -> bool:
        return self._get(name).score < self.DEGRADED_THRESHOLD

    def is_critical(self, name: str) -> bool:
        return self._get(name).score < self.CRITICAL_THRESHOLD

    def all_components(self) -> Dict[str, ComponentHealth]:
        return dict(self._components)


class ParameterAdaptor:
    """Gradually tightens or loosens SIGNAL_CONFIG/FILTER_CONFIG based on session metrics."""

    SIGNAL_BOUNDS = {
        'min_votes':           (2, 5),
        'min_vote_lead':       (1, 3),
        'rsi_oversold':        (25, 45),
        'rsi_overbought':      (60, 82),
        'vol_surge_threshold': (1.2, 2.5),
        'min_strength':        (25, 55),
    }
    TARGET_MIN = 3    # ideal lower bound for signals per cycle
    TARGET_MAX = 15   # ideal upper bound

    def adapt(self, metrics: Dict) -> List[Dict]:
        changes = []

        avg = metrics.get('avg_signals_per_cycle')
        if avg is not None:
            if avg > self.TARGET_MAX * 1.5:
                changes += self._nudge('min_votes', +1, f'avg_signals={avg:.1f} > {self.TARGET_MAX * 1.5:.0f}')
            elif avg < self.TARGET_MIN * 0.5:
                changes += self._nudge('min_votes', -1, f'avg_signals={avg:.1f} < {self.TARGET_MIN * 0.5:.1f}')

        fpr = metrics.get('filter_pass_rate')
        if fpr is not None:
            fc = config.FILTER_CONFIG
            if fpr < 0.05:
                old = fc['min_candle_size']
                new = round(max(0.3, old - 0.1), 2)
                if new != old:
                    fc['min_candle_size'] = new
                    changes.append({'param': 'filter.min_candle_size', 'old': old, 'new': new,
                                    'reason': f'filter_pass_rate={fpr:.1%} < 5% (too strict)'})
            elif fpr > 0.85:
                old = fc['min_candle_size']
                new = round(min(1.0, old + 0.1), 2)
                if new != old:
                    fc['min_candle_size'] = new
                    changes.append({'param': 'filter.min_candle_size', 'old': old, 'new': new,
                                    'reason': f'filter_pass_rate={fpr:.1%} > 85% (too loose)'})
        return changes

    def _nudge(self, param: str, delta: int, reason: str) -> List[Dict]:
        lo, hi = self.SIGNAL_BOUNDS[param]
        old = config.SIGNAL_CONFIG.get(param, lo)
        new = max(lo, min(hi, old + delta))
        if new == old:
            return []
        config.SIGNAL_CONFIG[param] = new
        return [{'param': f'signal.{param}', 'old': old, 'new': new, 'reason': reason}]

    def snapshot(self) -> Dict:
        out = {k: config.SIGNAL_CONFIG.get(k) for k in self.SIGNAL_BOUNDS}
        out['filter.min_candle_size'] = config.FILTER_CONFIG.get('min_candle_size')
        return out

    def apply_snapshot(self, saved: Dict):
        """Restore previously adapted params from a persisted snapshot."""
        applied = []
        for key, val in saved.items():
            if val is None:
                continue
            if '.' not in key:
                if key in config.SIGNAL_CONFIG and key in self.SIGNAL_BOUNDS:
                    lo, hi = self.SIGNAL_BOUNDS[key]
                    val = max(lo, min(hi, val))
                    old = config.SIGNAL_CONFIG[key]
                    if old != val:
                        config.SIGNAL_CONFIG[key] = val
                        applied.append(f"{key}: {old}->{val}")
            elif key == 'filter.min_candle_size':
                old = config.FILTER_CONFIG.get('min_candle_size')
                if old != val:
                    config.FILTER_CONFIG['min_candle_size'] = val
                    applied.append(f"filter.min_candle_size: {old}->{val}")
        if applied:
            print(f"  [REPAIR] Restored params from last session: {', '.join(applied)}")


class AutoRepairEngine:
    """
    Wraps each pipeline stage call. On exception: logs, attempts fix, returns safe default.
    Tracks per-component health scores (0-100). Adapts signal parameters based on
    session quality metrics. Persists repair log and adapted params across sessions.

    Usage:
        result = engine.watched_call('scanner', self.run_scanner)
        result = engine.watched_call('signal_engine', self.run_signal_generation, symbols)
        engine.record_cycle(raw_count, filtered_count)
        engine.maybe_adapt()
    """

    _SAFE_DEFAULTS: Dict[str, Any] = {
        'scanner':           [],
        'market_bias':       {},
        'time_filter':       {},
        'market_breadth':    {},
        'signal_engine':     [],
        'volatility_filter': [],
        'fake_filter':       [],
        'order_flow':        [],
        'strike_selection':  [],
        'ai_filter':         [],
        'execution':         [],
    }

    def __init__(self):
        self.health = HealthMonitor()
        self.adaptor = ParameterAdaptor()
        self._repair_log: List[RepairRecord] = []
        self._signal_counts: List[int] = []
        self._filter_rates: List[float] = []
        self._cycle_count = 0
        self._history_path = config.AUTO_REPAIR_CONFIG.get('history_file', 'logs/repair_history.json')
        os.makedirs(os.path.dirname(self._history_path) or '.', exist_ok=True)
        self._load_history()

    # ── Public API ─────────────────────────────────────────────────────────

    def watched_call(self, component: str, fn: Callable, *args, **kwargs) -> Any:
        """Call fn(*args, **kwargs). On exception: repair, return safe default."""
        try:
            result = fn(*args, **kwargs)
            self.health.record_success(component)
            return result
        except Exception as exc:
            self.health.record_failure(component, exc)
            self._log_failure(component, exc)
            repaired = self._attempt_fix(component, exc, fn, args, kwargs)
            if repaired is not None:
                return repaired
            default = self._SAFE_DEFAULTS.get(component)
            print(f"  [REPAIR] {component} unrecoverable -> safe default")
            return default

    def record_cycle(self, raw_signals: int, filtered_signals: int):
        """Feed signal counts after each pipeline run to power parameter adaptation."""
        self._signal_counts.append(raw_signals)
        if raw_signals > 0:
            self._filter_rates.append(filtered_signals / raw_signals)
        self._cycle_count += 1

    def maybe_adapt(self) -> List[Dict]:
        """Check if enough cycles have passed to adapt params. Returns list of changes."""
        cfg = config.AUTO_REPAIR_CONFIG
        interval = cfg.get('adaptation_interval', 10)
        min_samples = cfg.get('min_samples_for_adaptation', 5)
        if self._cycle_count < min_samples:
            return []
        if self._cycle_count % interval != 0:
            return []
        metrics = {
            'avg_signals_per_cycle': (
                sum(self._signal_counts) / len(self._signal_counts)
                if self._signal_counts else None
            ),
            'filter_pass_rate': (
                sum(self._filter_rates) / len(self._filter_rates)
                if self._filter_rates else None
            ),
        }
        changes = self.adaptor.adapt(metrics)
        if changes:
            for c in changes:
                print(f"  [REPAIR:ADAPT] {c['param']}: {c['old']} -> {c['new']}  ({c['reason']})")
            self._persist_param_changes(changes)
        return changes

    def health_report(self) -> Dict:
        return {
            'cycle_count': self._cycle_count,
            'total_repairs': len(self._repair_log),
            'components': {
                name: {
                    'score': round(h.score, 1),
                    'errors': h.error_count,
                    'successes': h.success_count,
                    'consecutive_failures': h.consecutive_failures,
                    'last_error': h.last_error,
                    'status': (
                        'CRITICAL' if self.health.is_critical(name) else
                        'DEGRADED' if self.health.is_degraded(name) else 'OK'
                    ),
                }
                for name, h in self.health.all_components().items()
            },
            'adapted_params': self.adaptor.snapshot(),
        }

    def print_health(self):
        r = self.health_report()
        print(f"\n[REPAIR] Health | cycles={r['cycle_count']} repairs={r['total_repairs']}")
        for name, c in r['components'].items():
            print(f"  {name:22s}  score={c['score']:5.1f}  err={c['errors']}  [{c['status']}]")
        snap = r['adapted_params']
        print(f"  Adapted: votes={snap.get('min_votes')} lead={snap.get('min_vote_lead')} "
              f"rsi={snap.get('rsi_oversold')}/{snap.get('rsi_overbought')} "
              f"candle={snap.get('filter.min_candle_size')}")

    # ── Fix logic ──────────────────────────────────────────────────────────

    def _attempt_fix(self, component: str, exc: Exception,
                     fn: Callable, args: tuple, kwargs: dict) -> Optional[Any]:
        # Network/API failure -> previously enabled mock data, but that silently
        # produces fake signals on a live feed. Now blocked unless explicitly
        # opted-in via env var TRADING_ALLOW_MOCK_FALLBACK=1. Default: just log
        # the failure and let the caller surface it.
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            allow_mock = os.environ.get("TRADING_ALLOW_MOCK_FALLBACK", "0") == "1"
            if not config.USE_MOCK_DATA and allow_mock:
                config.USE_MOCK_DATA = True
                print(f"  [REPAIR] {component}: API unavailable -> mock fallback enabled (opt-in)")
                self._record(component, exc, 'enable_mock_fallback', True)
                try:
                    r = fn(*args, **kwargs)
                    self.health.record_success(component)
                    return r
                except Exception:
                    pass
            return None

        # Calculation/data errors: log and skip (safe default applied by caller)
        fix_map = {
            ZeroDivisionError: 'zero_div_skip',
            ValueError:        'value_error_skip',
            TypeError:         'type_error_skip',
            IndexError:        'index_error_skip',
            KeyError:          'key_error_skip',
            AttributeError:    'attr_error_skip',
            RuntimeError:      'runtime_error_skip',
        }
        fix = fix_map.get(type(exc), 'unhandled_skip')
        self._record(component, exc, fix, True)
        return None

    def _record(self, component: str, exc: Exception, fix: str, success: bool):
        rec = RepairRecord(
            timestamp=datetime.now().isoformat(),
            component=component,
            error_type=type(exc).__name__,
            error_msg=str(exc)[:200],
            fix_applied=fix,
            success=success,
        )
        self._repair_log.append(rec)
        self._append_to_file(rec)

    def _log_failure(self, component: str, exc: Exception):
        h = self.health.all_components().get(component)
        n = h.consecutive_failures if h else 0
        level = 'CRIT' if n >= 5 else ('WARN' if n >= 2 else 'INFO')
        print(f"  [REPAIR:{level}] {component}: {type(exc).__name__}: {exc}")
        if n >= 3:
            print(f"    -> {n} consecutive failures in {component}")

    # ── Persistence ────────────────────────────────────────────────────────

    def _load_history(self):
        """Load and apply adapted params from previous session."""
        data = self._read_file()
        saved = data.get('adapted_params', {})
        if saved:
            self.adaptor.apply_snapshot(saved)

    def _append_to_file(self, rec: RepairRecord):
        data = self._read_file()
        data.setdefault('repairs', []).append(asdict(rec))
        data['repairs'] = data['repairs'][-500:]
        self._write_file(data)

    def _persist_param_changes(self, changes: List[Dict]):
        data = self._read_file()
        history = data.setdefault('parameter_history', [])
        for c in changes:
            history.append({'ts': datetime.now().isoformat(), **c})
        data['parameter_history'] = history[-200:]
        data['adapted_params'] = self.adaptor.snapshot()
        self._write_file(data)

    def _read_file(self) -> Dict:
        if not os.path.exists(self._history_path):
            return {}
        try:
            with open(self._history_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_file(self, data: Dict):
        try:
            with open(self._history_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  [REPAIR] History write failed: {e}")

