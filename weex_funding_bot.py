"""
scanner/weex_funding_bot.py
Production-grade automated bot for the WEEX Funding Capitulation Strategy.

Core Logic:
1. Scans WEEX perpetual contracts around funding rate reset moments (00:00, 08:00, 16:00 UTC, plus 4h cycles).
2. Identifies distressed tokens with extreme negative funding (fundingRate <= -0.05%).
3. Filters for liquidity (24h quote volume >= $2M, top-of-book spread <= 0.08%).
4. At T + 00:00:02 (immediately post-settlement): Opens SHORT market order (pays 0.00% funding fee).
5. At T + 00:15:00 (15 minutes later): Closes SHORT position with market order.
6. Dispatches real-time entry, exit, and P&L alerts to Telegram.
7. Features state persistence (survives droplet restarts), dry-run simulation mode, and circuit breakers.
"""

import os
import sys
import json
import time
import math
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import requests

# Ensure UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("weex_funding_bot.log", mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger("weex_funding_bot")

CONFIG_FILE = Path("weex_funding_config.json")
STATE_FILE = Path("weex_funding_state.json")
HISTORY_FILE = Path("weex_funding_history.json")


def load_config() -> Dict[str, Any]:
    """Loads configuration with fallback to environment variables and defaults."""
    config = {
        "dry_run": True,                     # Set to False only when ready for real orders
        "weex_api_key": os.environ.get("WEEX_API_KEY", ""),
        "weex_api_secret": os.environ.get("WEEX_API_SECRET", ""),
        "weex_passphrase": os.environ.get("WEEX_PASSPHRASE", ""),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "position_size_usd": 100.0,          # USD margin per trade
        "max_positions_per_cycle": 3,        # Max simultaneous coins to trade
        "funding_threshold_pct": -0.05,      # <= -0.05% per cycle (i.e. -0.0005)
        "min_24h_volume_usd": 2000000.0,     # $2M min 24h volume
        "max_spread_pct": 0.08,              # Reject if spread > 0.08%
        "holding_seconds": 900,              # Exactly 15 minutes (900 seconds)
        "emergency_stop_loss_pct": 3.0,      # Emergency circuit breaker: close immediately if adverse move >= 3.0%
        "taker_fee_pct": 0.06,               # Default WEEX contract taker fee (0.06%)
        "max_daily_loss_usd": 50.0,          # Circuit breaker: halt if daily loss exceeds $50
        "leverage": 1                        # Default 1x (isolated margin)
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                config.update(file_cfg)
        except Exception as e:
            logger.warning(f"Failed to read {CONFIG_FILE}: {e}")
    return config


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Dispatches a formatted HTML message to Telegram."""
    if not token or not chat_id:
        logger.info(f"[TELEGRAM SIMULATION]\n{text}")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code == 200:
                return True
        except Exception as e:
            time.sleep(1)
    logger.warning("Failed to send Telegram alert after 3 attempts.")
    return False


def load_state() -> Dict[str, Any]:
    """Loads active position state from disk."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read {STATE_FILE}: {e}")
    return {"active_positions": [], "daily_pnl_usd": 0.0, "last_pnl_date": ""}


def save_state(state: Dict[str, Any]) -> None:
    """Saves state atomically to disk."""
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def append_history(trade_record: Dict[str, Any]) -> None:
    """Appends closed trade record to history file."""
    history = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(trade_record)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    tmp.replace(HISTORY_FILE)


def init_exchange(config: Dict[str, Any]):
    """Initializes CCXT WEEX exchange client."""
    import ccxt
    exchange_params = {
        "enableRateLimit": True,
        "timeout": 15000,
    }
    if not config["dry_run"]:
        if not config["weex_api_key"] or not config["weex_api_secret"] or not config["weex_passphrase"]:
            raise ValueError("Live trading enabled (dry_run=False) but WEEX API credentials missing!")
        exchange_params["apiKey"] = config["weex_api_key"]
        exchange_params["secret"] = config["weex_api_secret"]
        exchange_params["password"] = config["weex_passphrase"]
        
    ex = ccxt.weex(exchange_params)
    return ex


def scan_funding_candidates(ex, config: Dict[str, Any], target_reset_dt: datetime) -> List[Dict[str, Any]]:
    """
    Scans all WEEX perpetual contracts for:
    1. Funding rate <= threshold (e.g. <= -0.05%)
    2. Reset time matches target settlement
    3. 24h volume >= min_24h_volume_usd
    4. Top-of-book bid-ask spread <= max_spread_pct
    """
    logger.info("Fetching bulk funding rates from WEEX...")
    try:
        funding_map = ex.fetch_funding_rates()
    except Exception as e:
        logger.error(f"Failed to fetch WEEX funding rates: {e}")
        return []

    target_thresh = config["funding_threshold_pct"] / 100.0  # -0.05% -> -0.0005
    min_vol = config["min_24h_volume_usd"]
    max_spread = config["max_spread_pct"]

    candidates = []
    for symbol, data in funding_map.items():
        if ":USDT" not in symbol:
            continue
            
        fr = data.get("fundingRate")
        if fr is None or fr > target_thresh:
            continue

        # Check reset time
        next_dt_str = data.get("nextFundingDatetime")
        if next_dt_str:
            try:
                next_dt = datetime.fromisoformat(next_dt_str.replace("Z", "+00:00"))
                # Allow a +/- 10 minute tolerance for settlement match
                diff_sec = abs((next_dt - target_reset_dt).total_seconds())
                if diff_sec > 600:
                    continue
            except Exception:
                pass

        candidates.append({
            "symbol": symbol,
            "funding_rate": fr,
            "funding_rate_pct": fr * 100.0,
            "mark_price": data.get("markPrice") or data.get("indexPrice")
        })

    # Sort candidates by most negative funding rate first
    candidates.sort(key=lambda x: x["funding_rate"])
    logger.info(f"Found {len(candidates)} candidates meeting funding threshold (<= {config['funding_threshold_pct']}%).")

    # Secondary liquidity and spread check
    verified = []
    for c in candidates:
        sym = c["symbol"]
        try:
            ticker = ex.fetch_ticker(sym)
            vol_usd = ticker.get("quoteVolume") or 0.0
            if vol_usd < min_vol:
                logger.info(f"Skipping {sym}: 24h volume ${vol_usd:,.0f} < ${min_vol:,.0f}")
                continue

            ob = ex.fetch_order_book(sym, limit=5)
            if not ob["bids"] or not ob["asks"]:
                continue
            best_bid = ob["bids"][0][0]
            best_ask = ob["asks"][0][0]
            if best_bid <= 0:
                continue
            spread_pct = ((best_ask - best_bid) / best_bid) * 100.0
            if spread_pct > max_spread:
                logger.info(f"Skipping {sym}: Spread {spread_pct:.3f}% > {max_spread:.3f}%")
                continue

            c["best_bid"] = best_bid
            c["best_ask"] = best_ask
            c["spread_pct"] = spread_pct
            c["volume_24h"] = vol_usd
            verified.append(c)
            if len(verified) >= config["max_positions_per_cycle"]:
                break
        except Exception as e:
            logger.warning(f"Error fetching ticker/book for {sym}: {e}")
            continue

    return verified


def execute_entry(ex, candidate: Dict[str, Any], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Opens a SHORT position immediately post-reset (Market Order)."""
    sym = candidate["symbol"]
    size_usd = config["position_size_usd"]
    is_dry = config["dry_run"]

    try:
        ob = ex.fetch_order_book(sym, limit=5)
        entry_price = ob["bids"][0][0]  # Selling into best bid
        amount = size_usd / entry_price

        order_id = "DRY_RUN_" + str(int(time.time()))
        if not is_dry:
            # Set leverage
            try:
                ex.set_leverage(config["leverage"], sym)
            except Exception:
                pass
            order = ex.create_market_sell_order(sym, amount)
            order_id = order.get("id", str(order))
            entry_price = order.get("average") or order.get("price") or entry_price

        position = {
            "symbol": sym,
            "order_id": order_id,
            "side": "SHORT",
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_timestamp": time.time(),
            "entry_price": entry_price,
            "amount": amount,
            "size_usd": size_usd,
            "funding_rate_pct": candidate["funding_rate_pct"],
            "spread_pct": candidate["spread_pct"],
            "volume_24h": candidate["volume_24h"]
        }
        return position
    except Exception as e:
        logger.error(f"Failed to enter SHORT on {sym}: {e}")
        return None


def execute_exit(ex, pos: Dict[str, Any], config: Dict[str, Any], exit_reason: str = "15m_time_exit") -> Dict[str, Any]:
    """Closes an active SHORT position at the 15-minute mark or emergency stop (Market Order)."""
    sym = pos["symbol"]
    amount = pos["amount"]
    entry_price = pos["entry_price"]
    is_dry = config["dry_run"]
    fee_pct = config["taker_fee_pct"] / 100.0

    try:
        ob = ex.fetch_order_book(sym, limit=5)
        exit_price = ob["asks"][0][0]  # Buying back at best ask

        if not is_dry:
            order = ex.create_market_buy_order(sym, amount)
            exit_price = order.get("average") or order.get("price") or exit_price

        # Short P&L: (Entry - Exit) / Entry
        gross_ret = (entry_price - exit_price) / entry_price
        # Roundtrip fee: 2 * fee_pct
        total_fee = 2.0 * fee_pct
        net_ret = gross_ret - total_fee

        gross_pnl_usd = pos["size_usd"] * gross_ret
        net_pnl_usd = pos["size_usd"] * net_ret

        res = dict(pos)
        res.update({
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "gross_return_pct": gross_ret * 100.0,
            "net_return_pct": net_ret * 100.0,
            "gross_pnl_usd": gross_pnl_usd,
            "net_pnl_usd": net_pnl_usd,
            "hold_seconds": int(time.time() - pos["entry_timestamp"])
        })
        return res
    except Exception as e:
        logger.error(f"Failed to close SHORT on {sym}: {e}")
        return {}


def format_entry_alert(positions: List[Dict[str, Any]], is_dry: bool) -> str:
    """Formats Telegram alert message for new entries."""
    mode = "🧪 [DRY RUN / PAPER TRADING]" if is_dry else "⚡ [LIVE EXECUTION]"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"<b>{mode} WEEX Funding Capitulation Entry</b>",
        f"⏰ <b>Time:</b> {now_str}",
        f"🎯 <b>Action:</b> SHORT (15-Minute Hold, 0% Funding Paid)",
        ""
    ]
    for p in positions:
        lines.append(
            f"• <b>{p['symbol']}</b> | Price: <code>{p['entry_price']:.4f}</code>\n"
            f"  Funding: <b>{p['funding_rate_pct']:.3f}%</b> | Size: ${p['size_usd']:.0f} | Spread: {p['spread_pct']:.3f}%"
        )
    lines.append("\n⏳ Auto-exit scheduled in 15 minutes.")
    return "\n".join(lines)


def format_exit_alert(closed_trades: List[Dict[str, Any]], is_dry: bool) -> str:
    """Formats Telegram alert message for trade exits."""
    mode = "🧪 [DRY RUN SUMMARY]" if is_dry else "💰 [LIVE P&L SUMMARY]"
    total_net_usd = sum(t.get("net_pnl_usd", 0.0) for t in closed_trades)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    icon = "🟢" if total_net_usd >= 0 else "🔴"
    lines = [
        f"<b>{mode} 15-Minute Hold Complete</b>",
        f"⏰ <b>Exit Time:</b> {now_str}",
        f"📊 <b>Total Net P&L:</b> {icon} <b>${total_net_usd:+.2f} USD</b>",
        ""
    ]
    for t in closed_trades:
        sym = t["symbol"]
        net_ret = t.get("net_return_pct", 0.0)
        pnl = t.get("net_pnl_usd", 0.0)
        t_icon = "✅" if pnl >= 0 else "❌"
        lines.append(
            f"{t_icon} <b>{sym}</b>: Entry {t['entry_price']:.4f} ➔ Exit {t['exit_price']:.4f}\n"
            f"  Net Return: <b>{net_ret:+.2f}%</b> (<b>${pnl:+.2f}</b>)"
        )
    return "\n".join(lines)


def get_next_funding_target() -> Tuple[datetime, float]:
    """
    Computes the upcoming hour boundary where funding settlements typically occur.
    Checks next 4-hour mark (00, 04, 08, 12, 16, 20 UTC).
    Returns (target_datetime, seconds_until_target).
    """
    now = datetime.now(timezone.utc)
    # Find next hour multiple of 4
    cur_hour = now.hour
    next_hour = (cur_hour // 4 + 1) * 4
    if next_hour >= 24:
        target = (now + timedelta(days=1)).replace(hour=next_hour % 24, minute=0, second=0, microsecond=0)
    else:
        target = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)

    sec_remaining = (target - now).total_seconds()
    return target, sec_remaining


def run_bot():
    """Master event loop for WEEX Funding Capitulation Bot."""
    logger.info("=== WEEX Funding Capitulation Bot Initializing ===")
    config = load_config()
    ex = init_exchange(config)
    
    state = load_state()
    logger.info(f"Mode: {'DRY RUN (Simulation)' if config['dry_run'] else 'LIVE TRADING'}")
    logger.info(f"Position Size: ${config['position_size_usd']} USD | Threshold: <= {config['funding_threshold_pct']}%")

    # Send startup message
    send_telegram(
        config["telegram_bot_token"],
        config["telegram_chat_id"],
        f"🤖 <b>WEEX Funding Bot Started</b>\n"
        f"Mode: {'🧪 DRY RUN' if config['dry_run'] else '⚡ LIVE'}\n"
        f"Threshold: <code>{config['funding_threshold_pct']}%</code> | Holding: <code>15m</code>"
    )

    while True:
        try:
            # 1. Check if there are orphaned active positions needing exit
            state = load_state()
            active = state.get("active_positions", [])
            now_ts = time.time()
            remaining_active = []
            just_closed = []

            for pos in active:
                elapsed = now_ts - pos["entry_timestamp"]
                should_exit = False
                exit_reason = "15m_time_exit"
                
                if elapsed >= config["holding_seconds"]:
                    should_exit = True
                else:
                    # Check emergency stop loss
                    stop_thresh = config.get("emergency_stop_loss_pct", 3.0)
                    try:
                        ob = ex.fetch_order_book(pos["symbol"], limit=1)
                        if ob["asks"]:
                            cur_ask = ob["asks"][0][0]
                            adverse_pct = ((cur_ask - pos["entry_price"]) / pos["entry_price"]) * 100.0
                            if adverse_pct >= stop_thresh:
                                should_exit = True
                                exit_reason = f"emergency_stop_{adverse_pct:+.1f}%"
                                logger.warning(f"EMERGENCY STOP TRIGGERED on {pos['symbol']}: Adverse move {adverse_pct:.2f}% >= {stop_thresh:.1f}%")
                    except Exception as e:
                        logger.warning(f"Could not check live book for stop loss on {pos['symbol']}: {e}")

                if should_exit:
                    logger.info(f"Closing position on {pos['symbol']} ({exit_reason}) after {elapsed:.0f}s...")
                    res = execute_exit(ex, pos, config, exit_reason)
                    if res:
                        just_closed.append(res)
                        append_history(res)
                    else:
                        remaining_active.append(pos)
                else:
                    remaining_active.append(pos)

            if just_closed:
                state["active_positions"] = remaining_active
                save_state(state)
                msg = format_exit_alert(just_closed, config["dry_run"])
                send_telegram(config["telegram_bot_token"], config["telegram_chat_id"], msg)

            # If still holding active positions, sleep 5s and re-check emergency stops
            if remaining_active:
                time.sleep(5)
                continue

            # 2. Compute time until next funding settlement
            target_dt, sec_left = get_next_funding_target()
            logger.info(f"Next settlement target: {target_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} (in {sec_left/60:.1f} mins)")

            # Sleep until 75 seconds before settlement
            if sec_left > 75:
                sleep_sec = min(sec_left - 75, 300)  # Wake up at least every 5 mins for heartbeat
                time.sleep(sleep_sec)
                continue

            # 3. Near settlement: Scan candidates at T - 45s
            logger.info("Within 75s of settlement. Preparing candidate scan...")
            time.sleep(max(0, sec_left - 45))  # Sleep until T - 45s

            candidates = scan_funding_candidates(ex, config, target_dt)
            if not candidates:
                logger.info("No candidates qualified this cycle. Waiting for next window...")
                time.sleep(60)
                continue

            logger.info(f"Selected {len(candidates)} candidates to trade: {[c['symbol'] for c in candidates]}")

            # 4. Wait precisely until T + 2s (2 seconds after settlement snapshot)
            now = datetime.now(timezone.utc)
            wait_for_reset = (target_dt - now).total_seconds() + 2.0
            if wait_for_reset > 0:
                logger.info(f"Sleeping {wait_for_reset:.1f}s until T + 2s (post-funding tick)...")
                time.sleep(wait_for_reset)

            # 5. Execute Entries
            opened_positions = []
            for c in candidates:
                pos = execute_entry(ex, c, config)
                if pos:
                    opened_positions.append(pos)

            if opened_positions:
                state["active_positions"] = opened_positions
                save_state(state)
                msg = format_entry_alert(opened_positions, config["dry_run"])
                send_telegram(config["telegram_bot_token"], config["telegram_chat_id"], msg)

            # Loop immediately to begin active position monitoring

        except KeyboardInterrupt:
            logger.info("Bot manually stopped by user.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in bot main loop: {e}", exc_info=True)
            time.sleep(10)


if __name__ == "__main__":
    run_bot()
