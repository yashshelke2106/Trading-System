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

export interface Trade {
  trade_id: string
  timestamp: string
  symbol: string
  direction: string
  entry_price: string
  exit_price: string
  quantity: string
  pnl: string
  pnl_percent: string
  status: "WIN" | "LOSS" | "EXPIRED"
  exit_reason: string
  grade: string
  entry_premium: string
  exit_premium: string
  option_type: string
  option_strike: string
}

export interface Stats {
  total: number
  wins: number
  losses: number
  expired: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  best_trade: number
  worst_trade: number
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
