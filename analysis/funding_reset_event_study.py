"""
Funding Rate Reset Event Study
Analyzes price dynamics around the 8-hour funding rate reset (00:00, 08:00, 16:00 UTC).
Tests multiple holding windows before and after settlement:
- Pre-settlement windows: [-60m, -15m], [-30m, 0], [-15m, 0]
- Post-settlement windows: [0, +15m], [0, +30m], [0, +60m], [0, +4h]
- Reversal window: [-30m, 0] vs [0, +30m]

Conditioned on:
- Funding Rate deciles (10 quantiles)
- Extreme funding buckets (< -0.05%, [-0.05%, -0.01%], [-0.01%, +0.01%], [+0.01%, +0.03%], [+0.03%, +0.06%], > +0.06%)

Respects holdout discipline: 75% in-sample, 25% holdout.
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

def run_funding_event_study():
    symbols = load_clean_crypto_symbols()
    print(f"Loaded {len(symbols)} clean crypto symbols with funding & 15m kline data.")
    
    events = []
    
    for sym in symbols:
        df_fund = pd.read_parquet(os.path.join(DATA_DIR_FUNDING, f"{sym}.parquet"))
        df_kl = pd.read_parquet(os.path.join(DATA_DIR_15M, f"{sym}.parquet"))
        
        df_fund["settle_time"] = pd.to_datetime(df_fund["settle_time"], utc=True)
        df_kl["open_time"] = pd.to_datetime(df_kl["open_time"], utc=True)
        
        # Sort and index klines by open_time
        df_kl = df_kl.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True)
        kl_map = {t: idx for idx, t in enumerate(df_kl["open_time"])}
        closes = df_kl["close"].values
        opens = df_kl["open"].values
        n_kl = len(df_kl)
        
        # Split dates for this symbol (75% in-sample / 25% holdout)
        t_start = df_kl["open_time"].min()
        t_end = df_kl["open_time"].max()
        split_date = t_start + (t_end - t_start) * 0.75
        
        for _, row in df_fund.iterrows():
            t_settle = row["settle_time"]
            fr = row["funding_rate"]
            
            # The settlement happens at t_settle (e.g. 00:00:00).
            # The candle opening at t_settle (00:00:00) is the first post-settlement candle.
            # The candle opening at t_settle - 15m (23:45:00) closes right at t_settle.
            if t_settle not in kl_map:
                continue
            idx_0 = kl_map[t_settle]
            
            # Ensure we have enough history before and after
            # -4 bars = -60m, +16 bars = +4h
            if idx_0 < 4 or idx_0 + 16 >= n_kl:
                continue
                
            # Prices:
            # P_m60 = Close of bar idx_0 - 4 (i.e. T - 60m)
            # P_m30 = Close of bar idx_0 - 2 (i.e. T - 30m)
            # P_m15 = Close of bar idx_0 - 1 (which closes at T - 0m, the moment of settlement)
            # P_0 = Close of bar idx_0 - 1 (same as settlement price)
            # P_p15 = Close of bar idx_0 (first 15m candle after settlement, closing at T + 15m)
            # P_p30 = Close of bar idx_0 + 1 (closes at T + 30m)
            # P_p60 = Close of bar idx_0 + 3 (closes at T + 60m)
            # P_p4h = Close of bar idx_0 + 15 (closes at T + 4h)
            
            p_m60 = closes[idx_0 - 4]
            p_m30 = closes[idx_0 - 2]
            p_settle = closes[idx_0 - 1] # Settlement price (T:00:00)
            p_p15 = closes[idx_0]
            p_p30 = closes[idx_0 + 1]
            p_p60 = closes[idx_0 + 3]
            p_p4h = closes[idx_0 + 15]
            
            # Calculate returns in percent
            ret_pre_60_15 = (closes[idx_0 - 1] / p_m60 - 1.0) * 100.0
            ret_pre_30_0 = (p_settle / p_m30 - 1.0) * 100.0
            ret_pre_15_0 = (p_settle / closes[idx_0 - 2] - 1.0) * 100.0 # final 15m bar
            
            ret_post_0_15 = (p_p15 / p_settle - 1.0) * 100.0
            ret_post_0_30 = (p_p30 / p_settle - 1.0) * 100.0
            ret_post_0_60 = (p_p60 / p_settle - 1.0) * 100.0
            ret_post_0_4h = (p_p4h / p_settle - 1.0) * 100.0
            
            partition = "In-Sample" if t_settle < split_date else "Holdout"
            
            events.append({
                "settle_time": t_settle,
                "symbol": sym,
                "funding_rate": fr,
                "funding_rate_pct": fr * 100.0,
                "partition": partition,
                "ret_pre_30_0": ret_pre_30_0,
                "ret_pre_15_0": ret_pre_15_0,
                "ret_post_0_15": ret_post_0_15,
                "ret_post_0_30": ret_post_0_30,
                "ret_post_0_60": ret_post_0_60,
                "ret_post_0_4h": ret_post_0_4h,
            })
            
    df_events = pd.DataFrame(events)
    print(f"Total events extracted: {len(df_events)}")
    return df_events

def analyze_event_study(df_events: pd.DataFrame):
    os.makedirs("results", exist_ok=True)
    df_events.to_parquet("results/funding_event_study_events.parquet")
    
    # Define funding categories
    def categorize_funding(fr):
        if fr < -0.0005: # < -0.05%
            return "1. Extreme Neg (< -0.05%)"
        elif fr < -0.0001: # -0.05% to -0.01%
            return "2. Moderate Neg (-0.05% to -0.01%)"
        elif fr <= 0.0001: # -0.01% to +0.01%
            return "3. Neutral (-0.01% to +0.01%)"
        elif fr <= 0.0003: # +0.01% to +0.03%
            return "4. Moderate Pos (+0.01% to +0.03%)"
        elif fr <= 0.0006: # +0.03% to +0.06%
            return "5. High Pos (+0.03% to +0.06%)"
        else: # > +0.06%
            return "6. Extreme Pos (> +0.06%)"
            
    df_events["funding_bucket"] = df_events["funding_rate"].apply(categorize_funding)
    
    # Calculate deciles for In-Sample
    df_in = df_events[df_events["partition"] == "In-Sample"].copy()
    df_in["decile"] = pd.qcut(df_in["funding_rate"].rank(method="first"), q=10, labels=[f"D{i+1}" for i in range(10)])
    
    # Print Bucket Analysis for In-Sample
    print("\n" + "="*80)
    print("IN-SAMPLE EVENT STUDY BY ECONOMIC FUNDING BUCKET")
    print("="*80)
    
    bucket_summary = []
    for b_name, grp in df_in.groupby("funding_bucket"):
        n = len(grp)
        t_pre30, p_pre30 = stats.ttest_1samp(grp["ret_pre_30_0"], 0)
        t_post15, p_post15 = stats.ttest_1samp(grp["ret_post_0_15"], 0)
        t_post30, p_post30 = stats.ttest_1samp(grp["ret_post_0_30"], 0)
        t_post60, p_post60 = stats.ttest_1samp(grp["ret_post_0_60"], 0)
        t_post4h, p_post4h = stats.ttest_1samp(grp["ret_post_0_4h"], 0)
        
        bucket_summary.append({
            "Bucket": b_name,
            "N": n,
            "Mean_FR%": grp["funding_rate_pct"].mean(),
            "Pre_30m%": grp["ret_pre_30_0"].mean(),
            "Pre_30m_p": p_pre30,
            "Post_15m%": grp["ret_post_0_15"].mean(),
            "Post_15m_p": p_post15,
            "Post_30m%": grp["ret_post_0_30"].mean(),
            "Post_30m_p": p_post30,
            "Post_60m%": grp["ret_post_0_60"].mean(),
            "Post_60m_p": p_post60,
            "Post_4h%": grp["ret_post_0_4h"].mean(),
            "Post_4h_p": p_post4h
        })
        
    df_b_summary = pd.DataFrame(bucket_summary).sort_values("Bucket")
    print(df_b_summary[["Bucket", "N", "Mean_FR%", "Pre_30m%", "Pre_30m_p", "Post_15m%", "Post_15m_p", "Post_30m%", "Post_30m_p", "Post_4h%", "Post_4h_p"]].to_string(index=False))
    
    # Decile Analysis
    print("\n" + "="*80)
    print("IN-SAMPLE EVENT STUDY BY 10 FUNDING DECILES")
    print("="*80)
    decile_summary = []
    for d_name, grp in df_in.groupby("decile", observed=False):
        n = len(grp)
        t_pre30, p_pre30 = stats.ttest_1samp(grp["ret_pre_30_0"], 0)
        t_post15, p_post15 = stats.ttest_1samp(grp["ret_post_0_15"], 0)
        t_post30, p_post30 = stats.ttest_1samp(grp["ret_post_0_30"], 0)
        t_post4h, p_post4h = stats.ttest_1samp(grp["ret_post_0_4h"], 0)
        
        decile_summary.append({
            "Decile": d_name,
            "N": n,
            "Min_FR%": grp["funding_rate_pct"].min(),
            "Max_FR%": grp["funding_rate_pct"].max(),
            "Mean_FR%": grp["funding_rate_pct"].mean(),
            "Pre_30m%": grp["ret_pre_30_0"].mean(),
            "Pre_30m_p": p_pre30,
            "Post_15m%": grp["ret_post_0_15"].mean(),
            "Post_15m_p": p_post15,
            "Post_30m%": grp["ret_post_0_30"].mean(),
            "Post_30m_p": p_post30,
            "Post_4h%": grp["ret_post_0_4h"].mean(),
            "Post_4h_p": p_post4h
        })
    df_d_summary = pd.DataFrame(decile_summary)
    print(df_d_summary.to_string(index=False))
    
    df_b_summary.to_parquet("results/funding_event_study_buckets.parquet")
    df_d_summary.to_parquet("results/funding_event_study_deciles.parquet")
    print("\nSaved event study results to results/")

if __name__ == "__main__":
    cache_path = "results/funding_event_study_events.parquet"
    if os.path.exists(cache_path):
        print(f"Loading cached events from {cache_path}...")
        df_ev = pd.read_parquet(cache_path)
    else:
        df_ev = run_funding_event_study()
    analyze_event_study(df_ev)
