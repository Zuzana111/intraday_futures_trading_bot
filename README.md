# Intraday Futures Trading Bot

Python-based intraday MES futures trading bot with Interactive Brokers API integration.

## Overview

This project is a live trading automation script built around an event-driven decision engine. It reacts to intraday market data, evaluates signal conditions across multiple instruments, and manages automated order execution for MES futures through the Interactive Brokers API.

The strategy logic uses SPY as the primary signal driver and confirms direction with ES and NQ futures behavior before entering a trade. It also includes rule-based exit handling, end-of-day flattening, logging, and optional Telegram notifications.

## Features

- Real-time data processing with intraday SPY, ES, and NQ market data
- Event-driven architecture using `ib_insync`
- Multi-instrument confirmation logic
- Automated MES futures order execution
- Rule-based exit conditions and end-of-day flattening
- Logging and Telegram notifications

## Tech Stack

- Python
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
