from ib_insync import *
from datetime import datetime, timedelta
import pandas as pd
import logging
import math
import os
import requests
import time
from pytz import timezone
import sys
from datetime import datetime as _dt, time as _time, timedelta
import pytz

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# =================== Telegram ===================
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; skipping message.")
        return

    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': str(message)}
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code != 200:
            print(f"Failed to send message: {response.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

send_telegram_message("Intraday futures trading bot started")

# Logging
log_directory = os.path.join(BASE_DIR, "logs")
os.makedirs(log_directory, exist_ok=True)
log_filename = f"{log_directory}/log_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    filename=log_filename,
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.info("Logging has started.")

# IB/TWS 
util.startLoop()
ib = IB()
ib.connect('127.0.0.1', 7497, clientId=2)  # Update port as needed: paper 7497, live 7496


time.sleep(10)

util.logToConsole()
[v for v in ib.accountValues() if v.tag == 'NetLiquidationByCurrency' and v.currency == 'BASE']


spy_contract = Stock('SPY', 'SMART', 'USD')

# Globals & Config 

# Runtime state flags
trade_closed = False          # True once done for the day
in_position = False           # currently hold MES
took_trade_today = False      # already entered once today (only one trade allowed, like backtest)
entry_price = None            # SPY price at entry moment (used for trailing math)
entry_inflight = False        # currently submitting entry order
last_entry_ts = 0.0           # cooldown
ENTRY_COOLDOWN_SEC = 5        # cheap guard to not spam orders in the same second
DEBUG_BAR_LOGS = True

# Phase / exit management
post_full_mode = False        # False = Phase 1 (pre full gap fill), True = Phase 2 (after full gap fill)
peak_favor = 0.0              # best unrealized pnl per MES contract after full gap fill
TRAIL_RETRACE_FRAC = 0.20     # must keep 80% of best win

# VWAP 2-minute flip stop state (Phase 1 only)
VWAP_2MIN_CONSEC = 2          # same as CONFIG["VWAP_2MIN_CONSEC"]
vwap_last_minute = None       # last completed gate_minute processed
vwap_consec_wrong = 0         # how many consecutive completed minutes ES+NQ disagreed with us

# gap entry filters
MIN_REMAIN_GAP_PCT = 0.10     
MES_FULL_SIZE = 4             

# Timezones for futures -> ET conversion
CHICAGO = pytz.timezone('America/Chicago')
NEWYORK = pytz.timezone('America/New_York')
VWAP_INCLUDE_FORMING = False  # only use last COMPLETED minute (E1)

# SPY 5-second stream
data = ib.reqHistoricalData(
    spy_contract,
    endDateTime='',
    durationStr='1 D',
    barSizeSetting='5 secs',
    whatToShow='TRADES',
    useRTH=True,
    keepUpToDate=True
)
df = util.df(data)

#  Daily strict open/close 
data_daily = ib.reqHistoricalData(
    spy_contract,
    endDateTime='',
    durationStr='2 D',
    barSizeSetting='1 day',
    whatToShow='TRADES',
    useRTH=True,
    keepUpToDate=False,
)

previous_day_close = data_daily[-2].close
today_open = data_daily[-1].open

gap_up = today_open > previous_day_close     # gap up => SHORT
gap_down = today_open < previous_day_close   # gap down => LONG
gap_start = today_open
gap_end = previous_day_close                 # full target is prev close (always)
gap_size = abs(previous_day_close - today_open)
gap_size_percent = ((today_open - previous_day_close) / previous_day_close) * 100

# Helpers for remaining-gap logic 
def remaining_gap_pct(current_price, prev_close, gap_up, gap_down):
    """
    How much of the gap is still unfilled, in % of prev_close.
    Used ONLY as an entry gate.
    """
    if gap_up:
        rem = current_price - prev_close
    elif gap_down:
        rem = prev_close - current_price
    else:
        rem = 0.0
    rem = max(rem, 0.0)
    return (rem / prev_close) * 100.0

def gap_already_filled(current_price, prev_close, gap_up, gap_down):
    """
    True if the entire gap is already filled at this price.
    """
    if gap_up:
        return current_price <= prev_close
    if gap_down:
        return current_price >= prev_close
    return False

# Near-half diagnostic
NEAR_HALF_FRAC = 0.10
def entry_too_close_to_half(entry_price, gap_up, gap_down, today_open, gap_size):
    """
    Diagnostic only. In the final backtest config, this does not change behavior,
    it just gets logged.
    """
    if gap_size is None or gap_size <= 0:
        return False
    buffer = NEAR_HALF_FRAC * gap_size
    if gap_down:
        half_level = today_open + (gap_size / 2.0)
        return entry_price >= (half_level - buffer)
    elif gap_up:
        half_level = today_open - (gap_size / 2.0)
        return entry_price <= (half_level + buffer)
    return False

# ES/NQ 1-minute streams
def front_future(sym):
    cds = ib.reqContractDetails(Future(symbol=sym, exchange='CME', currency='USD'))
    valid = sorted(
        [cd for cd in cds if cd.contract.lastTradeDateOrContractMonth and not cd.contract.includeExpired],
        key=lambda cd: cd.contract.lastTradeDateOrContractMonth
    )
    if not valid:
        raise RuntimeError(f"No {sym} contracts")
    today_ymd = pd.Timestamp.utcnow().strftime('%Y%m%d')
    for cd in valid:
        ymd = cd.contract.lastTradeDateOrContractMonth
        if ymd and ymd > today_ymd:
            return cd.contract
    return valid[-1].contract

es_contract  = front_future('ES')
nq_contract  = front_future('NQ')
mes_contract = front_future('MES')  

logging.info(f"ES contract: {es_contract}")
logging.info(f"NQ contract: {nq_contract}")
logging.info(f"MES contract: {mes_contract}")

es_bars = ib.reqHistoricalData(
    es_contract, endDateTime='', durationStr='2 D',
    barSizeSetting='1 min', whatToShow='TRADES', useRTH=False, keepUpToDate=True
)
nq_bars = ib.reqHistoricalData(
    nq_contract, endDateTime='', durationStr='2 D',
    barSizeSetting='1 min', whatToShow='TRADES', useRTH=False, keepUpToDate=True
)

df_es = util.df(es_bars)
df_nq = util.df(nq_bars)

# Gate state 
es_state = {"gate_min": None, "close": None, "vwap": None}
nq_state = {"gate_min": None, "close": None, "vwap": None}

def _to_et_df(bars):
    """
    Convert historical bars to ET tz-aware DataFrame with 'date' column.
    CME timestamps come back in exchange tz (Chicago). Convert to New York.
    """
    d = util.df(bars).copy()
    if d.empty or 'date' not in d.columns:
        return d
    d['date'] = pd.to_datetime(d['date'], errors='coerce')
    if d['date'].dt.tz is None:
        d['date'] = d['date'].dt.tz_localize(CHICAGO).dt.tz_convert(NEWYORK)
    else:
        d['date'] = d['date'].dt.tz_convert(NEWYORK)
    return d

def _session_slice_et_calendar(d: pd.DataFrame) -> pd.DataFrame:
    """
    Slice ES/NQ bars by ET calendar day (midnight ET → now ET).
    """
    if d.empty or 'date' not in d.columns:
        return d.iloc[0:0]
    now_et = _dt.now(NEWYORK)
    start = NEWYORK.localize(_dt.combine(now_et.date(), _time(0, 0)))
    end   = now_et
    dd = d[(d['date'] >= start) & (d['date'] <= end)]
    # E1 behavior: use last COMPLETED minute, not forming
    if not VWAP_INCLUDE_FORMING and not dd.empty:
        forming_ts = dd.iloc[-1]['date']
        dd = dd[dd['date'] <= forming_ts.floor('T')]
    return dd

def calculate_vwap(d_):
    """
    No rounding to 2 decimals.
    This is cumulative VWAP over today's ET session slice.
    """
    if d_ is None or len(d_) == 0:
        return None
    d = d_.copy()
    if 'average' in d.columns and pd.notna(d['average']).any():
        price = pd.to_numeric(d['average'], errors='coerce')
    else:
        high  = pd.to_numeric(d.get('high'),  errors='coerce')
        low   = pd.to_numeric(d.get('low'),   errors='coerce')
        close = pd.to_numeric(d.get('close'), errors='coerce')
        price = (high + low + close) / 3.0
    vol = pd.to_numeric(d.get('volume'), errors='coerce').fillna(0.0)
    mask = (vol > 0) & price.notna()
    if not mask.any():
        return None
    num = (price[mask] * vol[mask]).cumsum().iloc[-1]
    den = vol[mask].cumsum().iloc[-1]
    return float(num / den)   # <-- NO rounding 

def _freeze_gate_from_stream(bars, state, name):
    """
    Build the snapshot use for BOTH entry confirmation and VWAP_2MIN_STOP.
    use the last FULLY CLOSED minute, not the forming minute.
    VWAP over ET calendar day (00:00 ET -> now).
    """
    d = _to_et_df(bars)
    if d.empty or 'date' not in d.columns:
        return

    d = _session_slice_et_calendar(d)
    # need at least 2 rows: last-1 is last CLOSED minute,
    # last row may still be forming
    if len(d) < 2:
        return

    d = d.sort_values('date').reset_index(drop=True)

    last_closed = d.iloc[-2]
    gate_min = pd.Timestamp(last_closed['date']).floor('T')

    # compute VWAP only up to that gate_min
    vwap_df = d[d['date'] <= gate_min]
    vwap_value = calculate_vwap(vwap_df)

    state["gate_min"] = gate_min
    state["close"]    = float(last_closed["close"])
    state["vwap"]     = float(vwap_value) if vwap_value is not None else None

    if DEBUG_BAR_LOGS:
        logging.info(
            f"GATE_{name} ET window {d['date'].iloc[0]}→{d['date'].iloc[-1]} | "
            f"gate_min_ET={gate_min} | close={state['close']:.2f} "
            f"vwap={state['vwap'] if state['vwap'] is not None else 'NA'}"
        )

def _on_es_update(bars, hasNewBar):
    global df_es
    if hasNewBar:
        df_es = util.df(bars)
        _freeze_gate_from_stream(bars, es_state, "ES")

def _on_nq_update(bars, hasNewBar):
    global df_nq
    if hasNewBar:
        df_nq = util.df(bars)
        _freeze_gate_from_stream(bars, nq_state, "NQ")

es_bars.updateEvent += _on_es_update
nq_bars.updateEvent += _on_nq_update
_freeze_gate_from_stream(es_bars, es_state, "ES")
_freeze_gate_from_stream(nq_bars, nq_state, "NQ")

# Gap floor (logic: >= 0.1%)
if not (abs(gap_size_percent) >= 0.1):
    message = f"Gap {gap_size_percent:.2f}% is <= 0.1% — skipping."
    print(message)
    logging.info(message)
    send_telegram_message(message)
    ib.disconnect()
    sys.exit(0)

send_telegram_message(
    f"MES mode. Gap {'Up' if gap_up else 'Down'}: {gap_start:.2f} → {gap_end:.2f} "
    f"({gap_size_percent:.2f}%). Target = prev close {previous_day_close:.2f}"
)

# SPY df helper: slice today's ET bars 
def _spy_today_df(df_any):
    """
    Convert SPY 5-sec data to ET tz-aware and keep only today's ET date.
    """
    d = df_any.copy()
    if d.empty or 'date' not in d.columns:
        return d.iloc[0:0]
    d['date'] = pd.to_datetime(d['date'], errors='coerce')
    if d['date'].dt.tz is None:
        d['date'] = d['date'].dt.tz_localize('America/New_York')
    else:
        d['date'] = d['date'].dt.tz_convert('America/New_York')
    today_et = pd.Timestamp.now(tz='America/New_York').date()
    return d[d['date'].dt.date == today_et].copy()

#  skip day if full gap already filled before start scanning 
def full_gap_touched_pre_scan(df_5s_today, gap_up, gap_down, prev_close, scan_start_ts_et):
    """
    Backtest refuses to trade if the full gap target was already touched
    before even start scanning for entries.
    Replicate that here.
    """
    if df_5s_today.empty:
        return False

    for _, row in df_5s_today.iterrows():
        ts = row['date']
        if ts.tzinfo is None:
            ts = pytz.timezone('America/New_York').localize(ts)
        if ts < scan_start_ts_et:
            high_ = float(row['high'])
            low_  = float(row['low'])
            # TP_USE_TOUCH=True, so use high/low
            if gap_down:
                # long case, need price to tag prev_close above 
                if high_ >= prev_close:
                    return True
            else:
                # short case, need price to tag prev_close below 
                if low_ <= prev_close:
                    return True
    return False

scan_start_ts_et = (
    pd.Timestamp.now(tz='America/New_York').normalize()
    + pd.Timedelta(hours=9, minutes=30, seconds=10)
)

df_today_init = _spy_today_df(df)
if full_gap_touched_pre_scan(df_today_init, gap_up, gap_down, previous_day_close, scan_start_ts_et):
    send_telegram_message("Skip day: full gap filled before scan window. Exiting.")
    ib.disconnect()
    sys.exit(0)

# MES order helpers 
def _mes_position_qty() -> int:
    qty = 0
    for p in ib.positions():
        if p.contract.secType == 'FUT' and p.contract.symbol == 'MES':
            qty += int(p.position)
    return qty

def _flatten_mes_market(order_ref='EOD'):
    """
    Market flatten all MES, regardless long/short.
    """
    for p in ib.positions():
        if p.contract.secType == 'FUT' and p.contract.symbol == 'MES':
            qty = int(p.position)
            if qty != 0:
                side = 'SELL' if qty > 0 else 'BUY'
                c = Contract(conId=p.contract.conId)
                ib.qualifyContracts(c)
                o = MarketOrder(side, abs(qty)); o.orderRef = order_ref
                ib.placeOrder(c, o)
                ib.waitOnUpdate()

def _enter_mes_market(direction_short: bool, qty: int, order_ref: str):
    """
    Market entry full size (no scaling). One trade per day.
    """
    c = Contract(conId=mes_contract.conId)
    ib.qualifyContracts(c)
    action = 'SELL' if direction_short else 'BUY'
    o = MarketOrder(action, int(qty)); o.orderRef = order_ref
    t = ib.placeOrder(c, o)
    while not t.isDone():
        ib.waitOnUpdate(timeout=0.1)
    return t

# on_new_bar 
async def on_new_bar(bars: BarDataList, hasNewBar: bool):
    global trade_closed, in_position, took_trade_today
    global entry_price, entry_inflight, last_entry_ts
    global post_full_mode, peak_favor
    global vwap_last_minute, vwap_consec_wrong

    if trade_closed:
        return

    # ---- Forced EOD flat at 15:59:55 ET ----
    try:
        now_et_time = datetime.now(timezone('America/New_York')).time()
        if now_et_time >= datetime.strptime("15:59:55", "%H:%M:%S").time():
            _flatten_mes_market(order_ref='EOD')
            trade_closed = True
            in_position = False
            entry_inflight = False
            send_telegram_message("EOD flatten executed (MES).")
            ib.disconnect()
            return
    except Exception as e:
        logging.exception(f"EOD flatten check error: {e}")

    if not hasNewBar:
        return

    # refresh SPY intraday data slice
    global df
    df = util.df(bars)
    df_today = _spy_today_df(df)
    if df_today.empty:
        return

    # sync futures gate state (must use same completed minute for ES & NQ)
    if es_state["gate_min"] is None or nq_state["gate_min"] is None:
        return
    try:
        es_min = pd.Timestamp(es_state["gate_min"]).tz_convert('America/New_York').floor('T')
        nq_min = pd.Timestamp(nq_state["gate_min"]).tz_convert('America/New_York').floor('T')
        if DEBUG_BAR_LOGS:
            logging.info(f"SYNC_CHECK ES_min={es_min.isoformat()} NQ_min={nq_min.isoformat()}")
        if es_min != nq_min:
            return
    except Exception:
        # fallback compare if tz_convert fails
        if es_state["gate_min"] != nq_state["gate_min"]:
            return

    es_close_local = es_state["close"]; es_vwap_local = es_state["vwap"]
    nq_close_local = nq_state["close"]; nq_vwap_local = nq_state["vwap"]

    if None in (es_close_local, es_vwap_local, nq_close_local, nq_vwap_local):
        return

    # snapshot SPY latest bar info
    spy_close = float(df_today['close'].iloc[-1])
    spy_high_now = float(df_today['high'].iloc[-1])
    spy_low_now  = float(df_today['low'].iloc[-1])

    first_bar_low  = float(df_today.iloc[0]['low'])
    first_bar_high = float(df_today.iloc[0]['high'])

    # Check if full gap already touched by now (for entry gating and for phase switch)
    full_target_touched_now = (
        (gap_down and spy_high_now >= previous_day_close) or
        (gap_up   and spy_low_now  <= previous_day_close)
    )

    # In-gap check: price actually inside the gap, not fully reverted
    entered_down_gap = gap_up   and (spy_close < today_open)  # gap up
    entered_up_gap   = gap_down and (spy_close > today_open)  # gap down
    in_gap_now = entered_down_gap or entered_up_gap

    # Futures confirmation (ENTRY_REQUIRE_BOTH = True in backtest)
    short_side_ok = (es_close_local < es_vwap_local) and (nq_close_local < nq_vwap_local)
    long_side_ok  = (es_close_local > es_vwap_local) and (nq_close_local > nq_vwap_local)

    # Remaining-gap gate
    rem_pct = remaining_gap_pct(
        current_price=spy_close,
        prev_close=previous_day_close,
        gap_up=gap_up,
        gap_down=gap_down
    )
    already_filled_now = gap_already_filled(
        current_price=spy_close,
        prev_close=previous_day_close,
        gap_up=gap_up,
        gap_down=gap_down
    )
    enough_remaining = (rem_pct >= MIN_REMAIN_GAP_PCT) and (not already_filled_now)

    # Track VWAP_2MIN_STOP state (Phase 1 only)
    short_position = gap_up  
    wrong_es = (es_close_local > es_vwap_local) if short_position else (es_close_local < es_vwap_local)
    wrong_nq = (nq_close_local > nq_vwap_local) if short_position else (nq_close_local < nq_vwap_local)
    wrong_now = (wrong_es and wrong_nq)

    gate_minute = es_min  # both are synced so can reuse

    # Only update vwap_consec_wrong once per new completed minute
    if vwap_last_minute is None or gate_minute != vwap_last_minute:
        if wrong_now:
            vwap_consec_wrong += 1
        else:
            vwap_consec_wrong = 0
        vwap_last_minute = gate_minute

        logging.info(
            f"VWAP_CHECK minute={gate_minute} wrong_now={wrong_now} "
            f"vwap_consec_wrong={vwap_consec_wrong}"
        )

    # PHASE MANAGEMENT
  
    mes_qty_now = _mes_position_qty()
    in_position = (mes_qty_now != 0)

    # if gap has filled and never got a MES trade, stop script 
    if (not in_position) and (not took_trade_today) and full_target_touched_now and (not trade_closed):
        trade_closed = True
        send_telegram_message(
            f"FULL GAP filled before any MES entry. "
            f"Stopping script for today. SPY={spy_close:.2f}, prev_close={previous_day_close:.2f}"
        )
        ib.disconnect()
        return

    # PHASE SHIFT: If in position and not yet post_full_mode,
    # and SPY has finally touched the full gap target (prev_close),
    # DO NOT exit now. Instead flip to post_full_mode and initialize trailing
    if in_position and (not post_full_mode) and full_target_touched_now:
        post_full_mode = True

        # stamped_px mimics backtest TP_EXIT_BAR_OFFSET=0 using bar close
        stamped_px = spy_close
        if entry_price is None:
            entry_price = spy_close  # safety, should already be set on entry

        if gap_up:   # short
            unreal_per = (entry_price - stamped_px)
        else:        # long
            unreal_per = (stamped_px - entry_price)

        peak_favor = max(0.0, unreal_per)

        send_telegram_message(
            f"FULL GAP FILLED -> POST_FULL mode. "
            f"stamped_px={stamped_px:.2f}, unreal_per={unreal_per:.2f}"
        )

   
        # VWAP stop is now disabled after this point.


    # EXIT LOGIC

    # Phase 2 (post_full_mode): Trailing giveback only.
    if in_position and post_full_mode:
        cur_px = spy_close

        if gap_up:   # short
            unreal_now = (entry_price - cur_px)
        else:        # long
            unreal_now = (cur_px - entry_price)

        if unreal_now > peak_favor:
            peak_favor = unreal_now

        lock_keep = 1.0 - TRAIL_RETRACE_FRAC  # keep 80% of best win
        lock_keep = min(max(lock_keep, 0.0), 1.0)
        min_profit_kept = peak_favor * lock_keep

        if gap_up:
            # short: require cur_px <= entry_price - min_profit_kept
            trail_stop_px = entry_price - min_profit_kept
            trail_break = cur_px > trail_stop_px
        else:
            # long: require cur_px >= entry_price + min_profit_kept
            trail_stop_px = entry_price + min_profit_kept
            trail_break = cur_px < trail_stop_px

        logging.info(
            f"POST_FULL TRAIL chk cur={cur_px:.2f} peak={peak_favor:.2f} "
            f"keep={min_profit_kept:.2f} trail_stop={trail_stop_px:.2f} break={trail_break}"
        )

        if trail_break:
            _flatten_mes_market(order_ref='POST_FULL_TRAIL')
            trade_closed = True
            in_position = False
            took_trade_today = True
            entry_inflight = False
            send_telegram_message(
                f"POST_FULL_TRAIL exit. cur={cur_px:.2f} trail_stop={trail_stop_px:.2f} peak={peak_favor:.2f}"
            )
            ib.disconnect()
            return

        # If post_full_mode skip VWAP stop completely.
        # No other exits here.
        return

    # Phase 1 (pre-full-fill): VWAP_2MIN_STOP applies
    if in_position and (not post_full_mode):
        if vwap_consec_wrong >= VWAP_2MIN_CONSEC:
            _flatten_mes_market(order_ref='VWAP_2MIN_STOP')
            trade_closed = True
            in_position = False
            took_trade_today = True
            entry_inflight = False
            send_telegram_message(
                f"Exit VWAP_2MIN_STOP after {vwap_consec_wrong} bad minutes. "
                f"ES {es_close_local:.2f}/{es_vwap_local:.2f}, "
                f"NQ {nq_close_local:.2f}/{nq_vwap_local:.2f}"
            )
            ib.disconnect()
            return
        # If still in Phase 1 and not stopped, just keep holding.
        # No price 1:1 stop because USE_PRICE_1TO1_STOP=False in CONFIG.


    # ENTRY LOGIC

    #  Only consider entry if:
    # - Not already flat+finished for the day
    # - Not already in a position
    # - Not currently sending an order
    # - haven't taken a trade yet today
    # - Cooldown
    open_entry_exists = any(
        ((getattr(t.order, "orderRef", "") or "").startswith("ENTRY_")) and
        (t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive"))
        for t in ib.openTrades()
    )

    can_fire = (
        (not took_trade_today) and
        (not in_position) and
        (not entry_inflight) and
        (time.time() - last_entry_ts >= ENTRY_COOLDOWN_SEC) and
        (not open_entry_exists)
    )

    if not can_fire:
        return

    # futures confirm in the correct direction?
    if gap_up:
        futures_confirm = short_side_ok
    else:
        futures_confirm = long_side_ok

    # cannot enter if SPY already tagged the full target (prev_close) up to now.
    # Backtest: if ts >= full_touch_ts, break without entry.
    if full_target_touched_now:
        return

    # final entry gate:
    if in_gap_now and futures_confirm and enough_remaining:
        # log diagnostic near-half note (N0 active = just note, do NOT resize, do NOT partial)
        if entry_too_close_to_half(spy_close, gap_up, gap_down, today_open, gap_size):
            logging.info(
                f"NEAR_HALF_OVERRIDE_NOTE entry_px={spy_close:.2f} "
                f"open={today_open:.2f} gap={gap_size:.2f} ({gap_size_percent:.2f}%)"
            )
            send_telegram_message(
                f"Entry near/past half-gap (diagnostic). SPY {spy_close:.2f}, "
                f"open {today_open:.2f}, gap ${gap_size:.2f} ({gap_size_percent:.2f}%)."
            )

        # enter MES full size, direction based on gap
        entry_inflight = True
        last_entry_ts = time.time()
        entry_price = spy_close  # lock SPY ref px for trailing math later

        direction_short = gap_up  # gap_up -> short MES, gap_down -> long MES
        _enter_mes_market(direction_short=direction_short, qty=int(MES_FULL_SIZE), order_ref='ENTRY_MES_FULL')

        in_position = True
        took_trade_today = True
        entry_inflight = False

        logging.info(
            f"ENTRY {'SHORT' if direction_short else 'LONG'} (MES_FULL_SIZE={MES_FULL_SIZE}) "
            f"@ SPY={spy_close} rem_pct={rem_pct:.3f}% "
            f"| ES {es_close_local}/{es_vwap_local} "
            f"| NQ {nq_close_local}/{nq_vwap_local} "
            f"| first_low={first_bar_low} first_high={first_bar_high} "
            f"| today_open={today_open} prev_close={previous_day_close}"
        )

        send_telegram_message(
            f"ENTRY {'SHORT' if direction_short else 'LONG'} MES x{MES_FULL_SIZE} "
            f"SPY={spy_close:.2f} rem_gap={rem_pct:.3f}% "
            f"ES {es_close_local:.2f}/{es_vwap_local:.2f} "
            f"NQ {nq_close_local:.2f}/{nq_vwap_local:.2f}"
        )

#  Order events 
def onOrderStatus(trade):
    st = trade.orderStatus
    try:
        if st.status in ('Filled', 'PartiallyFilled'):
            c = trade.contract
            side = trade.order.action
            qty = getattr(trade.order, 'totalQuantity', None)
            msg = f"{st.status}: {side} {qty} {c.localSymbol or c.symbol} avg {st.avgFillPrice}"
            logging.info(msg)
            send_telegram_message(msg)
    except Exception:
        pass

def onExecDetails(trade, fill):
    try:
        c = fill.contract
        ex = fill.execution
        msg = f"EXEC {ex.side} {ex.shares} {c.localSymbol or c.symbol} @ {ex.price}"
        logging.info(msg)
        send_telegram_message(msg)
    except Exception:
        pass

ib.execDetailsEvent += onExecDetails
ib.orderStatusEvent += onOrderStatus
data.updateEvent += on_new_bar

# Main loop 
ib.run()
