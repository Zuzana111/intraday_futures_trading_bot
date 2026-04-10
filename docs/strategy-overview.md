# Strategy Overview

This project uses an event-driven intraday workflow:

1. Pull live SPY, ES, and NQ data through the Interactive Brokers API.
2. Monitor SPY for intraday signal conditions.
3. Confirm directional bias using ES and NQ behavior.
4. Enter MES futures when entry conditions are aligned.
5. Manage exits using rule-based conditions and end-of-day flattening.

The goal of this repository is to present the engineering structure of the system rather than to publish a complete public trading playbook.
