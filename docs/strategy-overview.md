# Strategy Overview

This project is based on a daily gap-fill concept in SPY. A daily gap occurs when the current regular-session open is above or below the previous regular-session close. The previous close becomes the reference level for a potential full gap fill.

The system does not trade SPY directly. It uses SPY to define the gap and direction, then executes the trade in MES futures.

The Interactive Brokers connection is handled with `ib_insync` rather than the lower-level native IB API. This keeps the market data subscriptions, event callbacks, and order-management flow easier to organize in a Python script.

## High-Level Workflow

1. Pull live SPY, ES, and NQ data through the Interactive Brokers API.
2. Compare today's SPY open with the previous regular-session close.
3. Define the full gap-fill target as the previous SPY close.
4. Monitor whether SPY moves into the gap.
5. Confirm directional bias using ES and NQ VWAP behavior.
6. Enter MES futures when entry conditions are aligned.
7. Manage exits using rule-based phase logic and end-of-day flattening.

## Entry Concept

- Gap up in SPY: the system looks for short-side conditions.
- Gap down in SPY: the system looks for long-side conditions.
- The bot does not enter if the full gap has already been touched.
- The bot requires enough remaining gap before entry.
- ES and NQ must confirm the trade direction using completed-minute VWAP snapshots.

## Exit Concept

The exit logic is split into two phases.

**Phase 1: Pre-fill management**

Before SPY reaches the previous close, the bot uses a VWAP-based invalidation rule. If ES and NQ both move against the trade direction for the configured number of completed minutes, the MES position is flattened.

**Phase 2: Post-fill management**

After SPY touches the previous close, the bot switches into post-fill mode. The VWAP stop is disabled, and the position is managed with a trailing giveback rule based on the favorable move from entry.

**End-of-day handling**

The script also includes a time-based end-of-day flatten rule, so the position is not intentionally carried beyond the intraday trading window.

The goal of this repository is to present the engineering structure of the system rather than to publish a complete public trading playbook.
