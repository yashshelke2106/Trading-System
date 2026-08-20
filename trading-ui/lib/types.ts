export interface PerTF {
  present: boolean
  vol_ratio: number
  direction: string
  strength: number
}

export interface Signal {
  symbol: string
  direction: "long" | "short"
  confluence_grade: "A" | "B" | "C"
  confluence_score: number
  entry_price: number
  sl_price: number
  sl_tight: number
  target_1: number
  target_price: number
  rr_ratio: number
  patterns_combined: string[]
  reason: string
  ts: string
  rsi: number
  volume_ratio: number
  vote_margin: number
  per_tf: { "5m"?: PerTF; "15m"?: PerTF; "1d"?: PerTF }
  option_strike?: number
  option_type?: "CE" | "PE"
  option_expiry?: string
  entry_prem?: number
  target_prem?: number
  sl_prem?: number
  delta?: number
  iv_pct?: number
  prem_source?: "live" | "live_quote" | "theoretical"
}

export interface JournalRecord {
  signal_id: string
  symbol: string
  direction: string
  entry_price: number
  sl_price: number
  target_price: number
  patterns: string[]
  score: number
  ai_prob: number
  grade: string
  ts: string
  outcome: "TARGET_HIT" | "SL_HIT" | "EXPIRED" | null
  exit_price: number | null
  exit_ts: string | null
  pnl_rupees: number | null
  option_type?: string
  option_strike?: number
  entry_prem?: number
  exit_prem?: number
  prem_source?: string
}

// A settled row from the signal journal — the source of record. Numbers are
// numbers here, not strings: this used to mirror logs/trades.csv, whose CSV
// reader made everything a string and whose legacy header silently dropped
// the premium/option fields on write.
export interface Trade {
  trade_id: string
  timestamp: string          // exit time
  entry_ts: string
  symbol: string
  direction: string
  entry_price: number | null
  exit_price: number | null
  sl_price: number | null
  target_price: number | null
  quantity: number
  /** false = contract size unknown; the row is shown but excluded from cash totals */
  lot_resolved: boolean
  /** premium cash at the resolved lot; null when the lot is unknown */
  pnl: number | null
  pnl_percent: number | null
  /** theta/IV-denoised spot move — the signal's own result */
  spot_pct: number | null
  spot_outcome: string | null
  status: "WIN" | "LOSS" | "EXPIRED"
  exit_reason: string
  grade: string | null
  entry_premium: number | null
  exit_premium: number | null
  option_type: string | null
  option_strike: number | null
  prem_source: string | null
}

/** Spot-basis metrics from core.honest_performance — the same gate the
 *  Verdict tab renders, so the two tabs cannot disagree. */
export interface HonestPerf {
  trustworthy: boolean
  note: string
  n_clean: number
  n_excluded: number
  win_rate: number | null
  profit_factor: number | null
  expectancy_pct: number | null
  median_pct: number | null
  total_pct: number | null
  alarms: string[]
}

export interface Stats {
  total: number
  qualified: number
  skipped_incomplete: number
  /** rows whose contract size could not be resolved — kept out of the cash total */
  unresolved_lot: number
  wins: number
  losses: number
  expired: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  best_trade: number
  worst_trade: number
  basis: string
  honest?: HonestPerf
}

export interface IndexQuote {
  ltp: number
  chg: number
  pct: number
}

export interface MarketStatus {
  market_status: "LIVE" | "CLOSED" | "PRE-MKT" | "WEEKEND"
  market_detail: string
  ist_time: string
  ist_date: string
  is_live: boolean
  elapsed_min: number
  trade_token: { valid: boolean; hours_left: number | null; needs_refresh: boolean; message: string }
  data_api: { valid: boolean; message: string }
}

export interface Position {
  symbol: string
  qty: number
  avg_price: number
  ltp: number
  unrealized: number
}

export interface VolumeRow {
  symbol: string
  price: number
  vol_1h?: number
  expected_1h?: number
  vol_last5?: number
  vol_prev5?: number
  ratio: number
  flag: string
}

export interface ChainRow {
  strike: number
  ce_ltp?: number
  pe_ltp?: number
  ce_oi?: number
  pe_oi?: number
  ce_iv?: number
  pe_iv?: number
  is_atm?: boolean
}

export interface SpikeAlert {
  symbol: string
  direction: "long" | "short"
  confidence: number
  vol_ratio: number
  price: number
  vwap: number
  price_move_pct: number
  in_prime: boolean
  is_breakout: boolean
  reason: string
}

export interface TrackingEntry {
  symbol: string
  direction: string
  entry_price: number
  current_price?: number
  sl_price: number
  target_price: number
  status: string
  elapsed_h?: number
  pnl_pct?: number
}
