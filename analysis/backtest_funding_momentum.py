"""
Post-Reset Funding Momentum Backtester
Simulates trading the post-reset price momentum discovered in the Event Study:
- Condition: Extreme positive funding (>= +0.06%) -> LONG
             Extreme negative funding (<= -0.05%) -> SHORT
- Entry Time: Exactly at T:00:00 (the open of the first candle post-settlement).
  Advantage: Zero funding fee paid/received (avoids cash drag).
- Exit Rules tested:
  1. Fixed time exit: 15m (1 bar)
  2. Fixed time exit: 30m (2 bars)
  3. Fixed time exit: 60m (4 bars)
  4. Dynamic ATR exit: SL = 1.5x ATR, TP = 2.5x ATR, max hold 4h (16 bars)
- Fees: 0.08% roundtrip (taker fee 0.04% + slippage 0.04%).
- Evaluated on In-Sample (75%) and Holdout (25%).
"""

import os
import glob
import numpy as np
import pandas as pd
from scipy import stats

DATA_DIR_FUNDING = "data_cache/funding"
DATA_DIR_15M = "data_cache/klines/15m"
EXCLUDE_SYMBOLS = [
    'ALUMINUM_USDT', 'COPPER_USDT', 'NICKEL_USDT', 'UKOIL_USDT', 'USOIL_USDT',
    'XAUT_USDT', 'SPX500_USDT', 'US30_USDT', 'NAS100_USDT'
]

def load_clean_crypto_symbols():
    files = glob.glob(os.path.join(DATA_DIR_FUNDING, "*.parquet"))
    symbols = []
    for f in files:
        sym = os.path.basename(f).replace(".parquet", "")
        if "STOCK" in sym or sym in EXCLUDE_SYMBOLS:
            continue
        kline_path = os.path.join(DATA_DIR_15M, f"{sym}.parquet")
        if os.path.exists(kline_path):
            symbols.append(sym)
    return sorted(symbols)

def run_backtest_all_symbols(
    pos_thresh=0.0006, # +0.06%
    neg_thresh=-0.0005, # -0.05%
    fee_roundtrip=0.0008 # 0.08%
):
    symbols = load_clean_crypto_symbols()
    all_trades = []
    
    for sym in symbols:
        df_fund = pd.read_parquet(os.path.join(DATA_DIR_FUNDING, f"{sym}.parquet"))
        df_kl = pd.read_parquet(os.path.join(DATA_DIR_15M, f"{sym}.parquet"))
        
        df_fund["settle_time"] = pd.to_datetime(df_fund["settle_time"], utc=True)
        df_kl["open_time"] = pd.to_datetime(df_kl["open_time"], utc=True)
        df_kl = df_kl.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True)
        
        kl_map = {t: idx for idx, t in enumerate(df_kl["open_time"])}
        opens = df_kl["open"].values
        highs = df_kl["high"].values
        lows = df_kl["low"].values
        closes = df_kl["close"].values
        n_kl = len(df_kl)
        
        # Calculate ATR(14)
        tr = np.maximum(
            highs - lows,
            np.maximum(
                np.abs(highs - np.roll(closes, 1)),
                np.abs(lows - np.roll(closes, 1))
            )
        )
        tr[0] = highs[0] - lows[0]
        atr = pd.Series(tr).rolling(14).mean().shift(1).values
        
        t_start = df_kl["open_time"].min()
        t_end = df_kl["open_time"].max()
        split_date = t_start + (t_end - t_start) * 0.75
        
        for _, row in df_fund.iterrows():
            t_settle = row["settle_time"]
            fr = row["funding_rate"]
            
            # Determine signal
            side = 0 # 1 = LONG, -1 = SHORT
            if fr >= pos_thresh:
                side = 1
            elif fr <= neg_thresh:
                side = -1
            else:
                continue
                
            if t_settle not in kl_map:
                continue
            idx_0 = kl_map[t_settle]
            
            if idx_0 + 16 >= n_kl:
                continue
                
            entry_price = opens[idx_0] # Enter at market open of first post-settlement bar
            cur_atr = atr[idx_0]
            if np.isnan(cur_atr) or entry_price <= 0:
                continue
                
            partition = "In-Sample" if t_settle < split_date else "Holdout"
            
            # 1. 15m exit (close of bar idx_0)
            ret_15m = side * (closes[idx_0] / entry_price - 1.0)
            net_15m = ret_15m - fee_roundtrip
            
            # 2. 30m exit (close of bar idx_0 + 1)
            ret_30m = side * (closes[idx_0 + 1] / entry_price - 1.0)
            net_30m = ret_30m - fee_roundtrip
            
            # 3. 60m exit (close of bar idx_0 + 3)
            ret_60m = side * (closes[idx_0 + 3] / entry_price - 1.0)
            net_60m = ret_60m - fee_roundtrip
            
            # 4. Dynamic ATR exit: SL 1.5x, TP 2.5x, max 16 bars
            sl_price = entry_price - 1.5 * cur_atr if side == 1 else entry_price + 1.5 * cur_atr
            tp_price = entry_price + 2.5 * cur_atr if side == 1 else entry_price - 2.5 * cur_atr
            
            dyn_exit_price = closes[idx_0 + 15]
            dyn_exit_reason = "time_exit"
            for step in range(16):
                bar_i = idx_0 + step
                if side == 1:
                    if lows[bar_i] <= sl_price:
                        dyn_exit_price = sl_price
                        dyn_exit_reason = "stop_loss"
                        break
                    elif highs[bar_i] >= tp_price:
                        dyn_exit_price = tp_price
                        dyn_exit_reason = "take_profit"
                        break
                else:
                    if highs[bar_i] >= sl_price:
                        dyn_exit_price = sl_price
                        dyn_exit_reason = "stop_loss"
                        break
                    elif lows[bar_i] <= tp_price:
                        dyn_exit_price = tp_price
                        dyn_exit_reason = "take_profit"
                        break
            ret_dyn = side * (dyn_exit_price / entry_price - 1.0)
            net_dyn = ret_dyn - fee_roundtrip
            
            all_trades.append({
                "settle_time": t_settle,
                "symbol": sym,
                "side": "LONG" if side == 1 else "SHORT",
                "funding_rate": fr,
                "partition": partition,
                "net_15m": net_15m * 100.0,
                "gross_15m": ret_15m * 100.0,
                "net_30m": net_30m * 100.0,
                "gross_30m": ret_30m * 100.0,
                "net_60m": net_60m * 100.0,
                "gross_60m": ret_60m * 100.0,
                "net_dyn": net_dyn * 100.0,
                "gross_dyn": ret_dyn * 100.0,
                "dyn_reason": dyn_exit_reason
            })
            
    df_trades = pd.DataFrame(all_trades)
    return df_trades

def calc_stats(series, gross_series):
    n = len(series)
    if n == 0:
        return {}
    win_rate = (series > 0).mean() * 100.0
    mean_net = series.mean()
    mean_gross = gross_series.mean()
    cum_net = series.sum()
    wins = series[series > 0]
    losses = series[series < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else np.nan
    t_stat, p_val = stats.ttest_1samp(series, 0)
    return {
        "n": n,
        "win_rate": win_rate,
        "pf": pf,
        "mean_net%": mean_net,
        "mean_gross%": mean_gross,
        "cum_net%": cum_net,
        "t_stat": t_stat,
        "p_val": p_val
    }

def print_strategy_report(df_trades):
    print("\n" + "="*85)
    print("POST-RESET FUNDING MOMENTUM STRATEGY RESULTS (NET OF 0.08% FEES)")
    print("="*85)
    
    rows = []
    for part in ["In-Sample", "Holdout"]:
        sub = df_trades[df_trades["partition"] == part]
        for mode in ["15m", "30m", "60m", "dyn"]:
            s_net = sub[f"net_{mode}"]
            s_gross = sub[f"gross_{mode}"]
            st = calc_stats(s_net, s_gross)
            rows.append({
                "Partition": part,
                "Exit Mode": mode,
                "Trades": st["n"],
                "WinRate%": f"{st['win_rate']:.1f}%",
                "ProfitFactor": f"{st['pf']:.2f}",
                "NetMean%": f"{st['mean_net%']:+.3f}%",
                "GrossMean%": f"{st['mean_gross%']:+.3f}%",
                "CumNet%": f"{st['cum_net%']:+.1f}%",
                "t_stat": f"{st['t_stat']:.2f}",
                "p_val": f"{st['p_val']:.4f}"
            })
            
    df_rep = pd.DataFrame(rows)
    print(df_rep.to_string(index=False))
    df_trades.to_parquet("results/post_reset_funding_trades.parquet")
    print("\nSaved trades to results/post_reset_funding_trades.parquet")

if __name__ == "__main__":
    df_trades = run_backtest_all_symbols()
    print_strategy_report(df_trades)
