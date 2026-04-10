# Intraday Futures Trading Bot

Python-based intraday MES futures trading bot with Interactive Brokers API integration.

## Overview

This project is a live trading automation script built around an event-driven decision engine. It reacts to intraday market data, evaluates signal conditions across multiple instruments, and manages automated order execution for MES futures through the Interactive Brokers API.

The strategy logic uses SPY as the primary signal driver and confirms direction with ES and NQ futures behavior before entering a trade. It also includes rule-based exit handling, end-of-day flattening, logging, and optional Telegram notifications.

The broker integration is implemented with `ib_insync`, a higher-level Python wrapper around the Interactive Brokers API. This makes the event-driven market data and order workflow easier to structure than working directly with the lower-level native IB API callbacks.

## Strategy Idea

The core idea is based on daily gaps in SPY. When SPY opens above or below the previous regular-session close, the script treats the previous close as the "full gap fill" reference level.

- If SPY opens above the previous close, the system looks for short-side conditions.
- If SPY opens below the previous close, the system looks for long-side conditions.
- The script avoids entering after the full gap has already been filled.
- A remaining-gap filter prevents entries when there is not enough gap left to justify the setup.

The trade is executed in MES futures, while SPY is used as the signal driver. ES and NQ are used as confirmation instruments because they provide broader futures-market context around the intraday direction.

## Entry Logic

The bot waits for the market to move into the gap and then checks whether ES and NQ confirm the same directional bias using VWAP-based conditions. It uses the latest completed minute for futures confirmation rather than a still-forming bar, which helps avoid reacting to unstable partial-bar data.

The script is designed to take at most one trade per day.

## Exit Logic

The exit management has two phases:

- **Phase 1: Before the full gap fill.** The system monitors whether both ES and NQ move against the trade relative to VWAP. If this happens for the configured number of completed minutes, the bot exits with a VWAP-based stop.
- **Phase 2: After the full gap fill.** Once SPY touches the previous close, the bot switches into post-fill management. At that point, the VWAP stop is disabled and the system uses a trailing giveback rule to protect a portion of the favorable move.
- **End-of-day protection.** The script includes an end-of-day flatten rule so positions are not intentionally held after the intraday session logic is complete.

## Features

- Real-time data processing with intraday SPY, ES, and NQ market data
- Event-driven architecture using `ib_insync`
- Multi-instrument confirmation logic
- Automated MES futures order execution
- Rule-based exit conditions and end-of-day flattening
- Logging and Telegram notifications

## Tech Stack

- Python
- ib_insync
- Real-Time Data Processing
- Event-Driven Architecture
- API Integration
- Algorithmic Trading

## Repository Structure

```text
intraday-futures-trading-bot/
  README.md
  requirements.txt
  .env.example
  .gitignore
  docs/
    strategy-overview.md
  src/
    intraday_futures_trading_bot.py
```

## Setup

1. Create and activate a Python environment.
2. Install dependencies from `requirements.txt`.
3. Copy `.env.example` to your own local env file or export the variables in your shell.
4. Make sure Interactive Brokers TWS or IB Gateway is running and that the port in the script matches your setup.
5. Run the script from the repository root.

## Environment Variables

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

If the variables are not set, the script skips Telegram notifications and continues running.

## Notes

- This repository is shared as a portfolio project and engineering example.
- It is not investment advice.
- Before publishing publicly, review the script and remove or generalize any broker, strategy, or infrastructure details you do not want to expose.
