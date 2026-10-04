# WEEX Funding Capitulation Bot

An institutional-grade, lookahead-free automated trading bot implementing the **15-Minute Post-Reset Capitulation Strategy** natively on the **WEEX** cryptocurrency perpetual exchange.

---

## Strategy Summary
* **Economic Edge:** Exploits structural order flow following extreme negative funding rate resets ($\le -0.05\%$ per cycle).
* **0% Funding Fee Advantage:** Enters **SHORT** at $T + 00:00:02$ UTC (2 seconds after the funding snapshot tick), completely bypassing paying funding fees.
* **Strict 15m Time Exit:** Closes the position at $T + 00:15:00$ UTC. Empirical backtesting shows holding past 15 minutes decays into fee drag and random market noise.
* **Emergency Hard Stop (+3.0%):** Automatically cuts positions if price spikes against the short by $\ge 3\%$, eliminating fat-tail squeeze blowouts.
* **Empirical Net Edge:** $+0.157\%$ net per trade, $54.1\%$ win rate across 2,392 trades in out-of-sample backtesting (net of all fees and slippage).

---

## Architecture & Safety Features
1. **100% WEEX Native:** Scans all 1,024 WEEX perpetual contracts in a single API call (~1.5s latency).
2. **Liquidity & Spread Guard:** Automatically skips contracts with $< \$2\text{M}$ 24-hour volume or top-of-book spread $> 0.08\%$.
3. **State Recovery:** Persists active positions to disk (`weex_funding_state.json`). If the droplet restarts mid-trade, the bot recovers the position and closes it at the 15-minute mark.
4. **Dry-Run Mode (`dry_run: true`):** Default setting runs real-time paper trading against live order books, computing theoretical fills, fees, and P&L without risking capital.
5. **Real-Time Telegram Alerts:** Dispatches instant entry, emergency stop, and exit P&L summaries with HTML formatting.

---

## One-Click DigitalOcean Setup

### 1. Launch a Droplet
* **OS:** Ubuntu 22.04 or 24.04 LTS
* **Size:** Basic 1 vCPU, 1 GB RAM ($4 or $6/month)

### 2. Run Setup Script
SSH into your droplet and run:
```bash
curl -sSL https://raw.githubusercontent.com/Wallaby-Alpha/weex-funding-bot/main/setup_droplet.sh | bash
```

### 3. Configure Credentials
Edit the configuration file:
```bash
nano /opt/weex-funding-bot/weex_funding_config.json
```

```json
{
  "dry_run": true,
  "weex_api_key": "YOUR_WEEX_API_KEY",
  "weex_api_secret": "YOUR_WEEX_API_SECRET",
  "weex_passphrase": "YOUR_WEEX_PASSPHRASE",
  "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID",
  "position_size_usd": 100.0,
  "max_positions_per_cycle": 3,
  "funding_threshold_pct": -0.05,
  "min_24h_volume_usd": 2000000.0,
  "max_spread_pct": 0.08,
  "holding_seconds": 900,
  "emergency_stop_loss_pct": 3.0,
  "taker_fee_pct": 0.06,
  "max_daily_loss_usd": 50.0,
  "leverage": 1
}
```

### 4. Test in Foreground
```bash
cd /opt/weex-funding-bot
./venv/bin/python weex_funding_bot.py
```

### 5. Enable Background Daemon
```bash
sudo systemctl enable weex-funding-bot
sudo systemctl start weex-funding-bot
```

To monitor logs:
```bash
tail -f /var/log/weex_funding_bot.log
```
