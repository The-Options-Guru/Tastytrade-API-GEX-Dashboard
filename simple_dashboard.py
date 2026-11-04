"""
Simple GEX Dashboard - Direct WebSocket approach (no background threads)
Fetches data once when you click "Fetch Data", then displays it
Works on weekends with Friday's closing data
"""
import streamlit as st
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.graph_objects as go
import requests
import sqlite3
import math
from utils import flow_store
from utils.auth import ensure_streamer_token
from utils.snapshot_feed import (
    connect_websocket, get_underlying_price, get_underlying_snapshot,
    generate_option_symbols, fetch_option_data,
)
from utils.gex_calculator import GEXCalculator
from utils.exposure_profile import (
    build_exposure_profile, calculate_max_pain, get_wall_bias, get_wall_levels, get_open_to_wall_context,
)
from utils.option_chain import fetch_expiry_chain
from utils.market_hours import market_state, MarketState
from utils.intraday_gex import (
    VIEWS as INTRADAY_GEX_VIEWS, UNITS as INTRADAY_GEX_UNITS,
    build_intraday_gex_chart, capture_frame, gex_changes, gex_scope, make_gex_capture,
)
from utils.snapshot_history import (
    COLUMNS as HISTORY_COLUMNS, DEFAULT_CONFIG, HistoryStore, confirmed_previous_close,
    capture_reason, crossing_history, csv_text, day_label, history_style, snapshot_metrics,
    time_label, validate_config, wall_strength_history,
)

st.set_page_config(page_title="GEX Dashboard", page_icon="📊", layout="wide")

# Preset symbol configuration
PRESET_SYMBOLS = {
    "SPX": {"option_prefix": "SPXW", "default_price": 6000, "increment": 5},
    "NDX": {"option_prefix": "NDXP", "default_price": 20000, "increment": 25},
    "SPY": {"option_prefix": "SPY", "default_price": 680, "increment": 1},
    "QQQ": {"option_prefix": "QQQ", "default_price": 612, "increment": 1},
    "IWM": {"option_prefix": "IWM", "default_price": 240, "increment": 1},
    "DIA": {"option_prefix": "DIA", "default_price": 450, "increment": 1},
}

def aggregate_by_strike(option_data):
    """Aggregate volume and OI by strike from option data"""
    from utils.gex_calculator import parse_option_symbol

    strike_data = {}

    for symbol, data in option_data.items():
        parsed = parse_option_symbol(symbol)
        if not parsed:
            continue

        strike = parsed['strike']
        opt_type = parsed['type']

        if strike not in strike_data:
            strike_data[strike] = {
                'call_oi': 0,
                'put_oi': 0,
                'call_volume': 0,
                'put_volume': 0,
                'call_iv': None,
                'put_iv': None
            }

        # Convert to numbers (might be strings from WebSocket or NaN)
        import math

        try:
            oi = float(data.get('oi', 0) or 0)
            if math.isnan(oi):
                oi = 0
        except (ValueError, TypeError):
            oi = 0

        try:
            volume = float(data.get('volume', 0) or 0)
            if math.isnan(volume):
                volume = 0
        except (ValueError, TypeError):
            volume = 0

        iv = data.get('iv')

        if opt_type == 'C':
            strike_data[strike]['call_oi'] += int(oi)
            strike_data[strike]['call_volume'] += int(volume)
            if iv:
                strike_data[strike]['call_iv'] = iv
        else:
            strike_data[strike]['put_oi'] += int(oi)
            strike_data[strike]['put_volume'] += int(volume)
            if iv:
                strike_data[strike]['put_iv'] = iv

    # Convert to DataFrame
    rows = []
    for strike, data in strike_data.items():
        rows.append({
            'strike': strike,
            'call_oi': data['call_oi'],
            'put_oi': data['put_oi'],
            'call_volume': data['call_volume'],
            'put_volume': data['put_volume'],
            'total_oi': data['call_oi'] + data['put_oi'],
            'total_volume': data['call_volume'] + data['put_volume'],
            'call_iv': data['call_iv'],
            'put_iv': data['put_iv']
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('strike').reset_index(drop=True)
    return df


def format_heatmap_value(value):
    if pd.isna(value):
        return "N/A"
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:,.2f}{suffix}"
    formatted = f"{value:,.2f}".rstrip("0").rstrip(".") if value else "0"
    return "0" if formatted == "-0" else formatted


def select_visible_strikes(profile, levels, strike_count):
    visible = profile.sort_values("strike", ascending=False).reset_index(drop=True)
    if strike_count != "All" and len(visible) > int(strike_count):
        count = int(strike_count)
        center = visible.index[visible["strike"] == levels["atm_strike"]][0]
        start = max(0, min(center - count // 2, len(visible) - count))
        visible = visible.iloc[start:start + count].reset_index(drop=True)
    return visible


def build_strike_heatmap(profile, levels, symbol, expiration, measure, strike_count):
    """Use bounded row coordinates for both cells and wall overlays."""
    visible = select_visible_strikes(profile, levels, strike_count)
    gamma_view = measure == "Gamma notional"
    columns = ["net_volume_gex", "net_oi_gex"] if gamma_view else ["call_volume", "put_volume"]
    labels = (
        ["Directionalized", "Volume GEX", "OI GEX"]
        if gamma_view else ["Directionalized", "Call volume", "Put volume"]
    )
    values = [[None, *row] for row in visible[columns].values.tolist()]
    finite_values = visible[columns].stack()
    magnitude = max(float(finite_values.abs().max()), 1) if not finite_values.empty else 1
    heatmap = go.Figure(go.Heatmap(
        x=[0, 1, 2],
        y=list(range(len(visible))),
        z=values,
        zmin=-magnitude if gamma_view else 0,
        zmax=magnitude,
        colorscale=(
            [[0, "#ed6376"], [0.5, "#191b22"], [1, "#65c9ae"]]
            if gamma_view else [[0, "#191b22"], [1, "#65c9ae"]]
        ),
        showscale=False,
        hoverinfo="skip",
        hoverongaps=False,
        xgap=2,
        ygap=1,
    ))
    text_x, text_y, text_values, details = [], [], [], []
    for index, row in enumerate(values):
        strike = visible.iloc[index]["strike"]
        for column, value in enumerate(row):
            text_x.append(column)
            text_y.append(index)
            text_values.append(format_heatmap_value(value))
            exact_value = (
                f"${value:,.2f} / 1% move" if gamma_view else f"{value:,.0f} contracts"
            ) if pd.notna(value) else "Unavailable"
            details.append([f"{strike:,.2f}", labels[column], exact_value])
    heatmap.add_trace(go.Scatter(
        x=text_x, y=text_y, text=text_values, customdata=details,
        mode="text", textfont=dict(color="#f1f5f9", size=12),
        hovertemplate="Strike %{customdata[0]}<br>%{customdata[1]}: %{customdata[2]}<extra></extra>",
        showlegend=False,
    ))
    for row in range(len(visible)):
        heatmap.add_shape(
            type="rect", xref="paper", x0=0, x1=1,
            y0=row - 0.5, y1=row + 0.5,
            fillcolor="#191b22" if row % 2 == 0 else "#101217",
            line_width=0, layer="below",
        )
    for name, strike, color, dash, offset, shift in (
        ("Call wall", levels["call_wall"]["strike"] if levels["call_wall"] else None, "#36cba5", "dash", -0.35, 9),
        ("Put wall", levels["put_wall"]["strike"] if levels["put_wall"] else None, "#f45b76", "dash", 0.35, -9),
        ("ATM", levels["atm_strike"], "#36a9ff", "solid", 0, 0),
    ):
        matches = visible.index[visible["strike"] == strike]
        if len(matches):
            row = int(matches[0]) + offset
            heatmap.add_shape(
                type="line", xref="paper", x0=0, x1=1, y0=row, y1=row,
                line=dict(color=color, width=1.5, dash=dash),
            )
            heatmap.add_annotation(
                xref="paper", x=1, y=row, text=f"{name} {strike:,.2f}",
                showarrow=False, xanchor="right", yshift=shift,
                font=dict(color=color, size=11),
                bgcolor="#101217",
            )
    expiry_display = datetime.strptime(expiration, "%y%m%d").strftime("%m/%d/%Y")
    heatmap.update_layout(
        title=dict(text=f"{symbol} strike heatmap | {expiry_display}", font=dict(size=16)),
        template="plotly_dark",
        paper_bgcolor="#101217", plot_bgcolor="#101217",
        font=dict(color="#e5e7eb"),
        height=150 + 24 * len(visible),
        margin=dict(l=75, r=15, t=100, b=25),
        xaxis=dict(
            side="top", tickmode="array", tickvals=[0, 1, 2],
            ticktext=labels, range=[-0.5, 4],
            showgrid=False, zeroline=False, fixedrange=True,
        ),
        yaxis=dict(
            tickmode="array", tickvals=list(range(len(visible))),
            ticktext=[f"{strike:,.2f}".rstrip("0").rstrip(".") for strike in visible["strike"]],
            range=[len(visible) - 0.5, -0.5],
            showgrid=False, zeroline=False, fixedrange=True,
        ),
        showlegend=False,
    )
    return heatmap


def build_gex_bar_chart(profile, levels, symbol, expiration, strike_count, max_pain=None):
    visible = select_visible_strikes(profile, levels, strike_count)
    values = visible["net_oi_gex"] / 1e6
    figure = go.Figure(go.Bar(
        x=values, y=list(range(len(visible))), orientation="h",
        marker_color=["#36cba5" if value >= 0 else "#f45b76" for value in values],
        customdata=[[strike] for strike in visible["strike"]],
        hovertemplate="Strike %{customdata[0]:,.2f}<br>$%{x:,.2f}M / 1% move<extra></extra>",
        name="Net OI GEX",
    ))
    overlays = [
        ("Call wall", levels["call_wall"]["strike"] if levels["call_wall"] else None, "#36cba5", -0.35, 9),
        ("Put wall", levels["put_wall"]["strike"] if levels["put_wall"] else None, "#f45b76", 0.35, -9),
        ("ATM", levels["atm_strike"], "#36a9ff", 0, 0),
    ]
    for strike in sorted(set((max_pain or {}).get("strikes", []))):
        if strike in ((max_pain or {})["strikes"][0], (max_pain or {})["strikes"][-1]):
            overlays.append(("Max pain", strike, "#f5bf55", 0, 0))
    for label, strike, color, offset, shift in overlays:
        matches = visible.index[visible["strike"] == strike]
        if len(matches):
            row = int(matches[0]) + offset
            figure.add_shape(
                type="line", xref="paper", x0=0, x1=1, y0=row, y1=row,
                line=dict(color=color, width=1.5, dash="dot" if label == "Max pain" else "dash"),
            )
            figure.add_annotation(
                xref="paper", x=1, y=row, text=f"{label} {strike:,.2f}",
                showarrow=False, xanchor="right", yshift=shift,
                font=dict(color=color, size=11), bgcolor="#101217",
            )
    expiry_display = datetime.strptime(expiration, "%y%m%d").strftime("%m/%d/%Y")
    figure.update_layout(
        title=dict(text=f"{symbol} net OI GEX | {expiry_display}", font=dict(size=16)),
        template="plotly_dark", paper_bgcolor="#101217", plot_bgcolor="#101217",
        font=dict(color="#e5e7eb"), showlegend=False,
        height=150 + 24 * len(visible), margin=dict(l=75, r=15, t=100, b=25),
        xaxis=dict(title="GEX ($ millions per 1% spot move)", zeroline=True, zerolinecolor="#88909e"),
        yaxis=dict(
            tickmode="array", tickvals=list(range(len(visible))),
            ticktext=[f"{strike:,.2f}".rstrip("0").rstrip(".") for strike in visible["strike"]],
            range=[len(visible) - 0.5, -0.5], showgrid=False, fixedrange=True,
        ),
    )
    return figure


def market_sentiment(prices, price_is_fallback=False, now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    today = datetime.fromtimestamp(now_ms / 1000, flow_store.NEW_YORK).date()
    day_id = (today - datetime(1970, 1, 1).date()).days
    last, opened, timestamp = (
        flow_store.finite_number(prices.get(key)) for key in ("last", "open", "last_time")
    )
    result = dict(label="Unavailable", reason="", change=None, percent=None)
    if price_is_fallback or last is None or opened is None or last <= 0 or opened <= 0:
        result["reason"] = "Observed last trade and day open are required; fallback prices are excluded."
    elif (
        type(prices.get("day_id")) is not int or type(prices.get("last_day_id")) is not int
        or prices["day_id"] != day_id or prices["last_day_id"] != day_id
    ):
        result["reason"] = "Trade and open must both belong to today's ET provider session."
    elif timestamp is None or timestamp <= 0:
        result["reason"] = "Last trade timestamp unavailable."
    elif not 0 <= now_ms - timestamp <= 120000:
        result["reason"] = "Last trade is stale (over 120 seconds old) or has a future timestamp."
    elif datetime.fromtimestamp(timestamp / 1000, flow_store.NEW_YORK).date() != today:
        result["reason"] = "Last trade timestamp is not from today (ET)."
    else:
        change = last - opened
        result.update(
            label="Bullish" if change > 0 else "Bearish" if change < 0 else "Neutral",
            change=change, percent=change / opened * 100,
        )
    return result


@st.fragment(run_every="5s")
def render_market_sentiment(symbol, prices, price_is_fallback=False):
    sentiment = market_sentiment(prices, price_is_fallback)
    with st.container(border=True):
        direction, trade, opened = st.columns(3)
        change = sentiment["change"]
        direction.metric(
            f"{symbol} market sentiment", sentiment["label"],
            delta=f"{change:+,.2f} ({sentiment['percent']:+.2f}%) vs open" if change is not None else None,
        )
        for column, label, field in ((trade, "Last observed trade", "last"), (opened, "Provider day open", "open")):
            value = flow_store.finite_number(prices.get(field))
            column.metric(label, f"${value:,.2f}" if value is not None and value > 0 else "Unavailable")
        st.caption(
            f"Price timestamp: {time_label(prices.get('last_time')) or 'Unavailable'} | "
            f"Open session: {day_label(prices.get('day_id')) or 'Unavailable'}. "
            "Bullish = above today's open; bearish = below; neutral = equal. "
            "Observed price movement only, not a forecast or GEX signal. "
            "Trade freshness limit: 120 seconds; status rechecked every 5 seconds."
        )
        if sentiment["reason"]:
            st.caption(f"Sentiment unavailable: {sentiment['reason']}")


def render_market_summary(profile, levels, spot_price, max_pain, price_is_fallback, market_prices=None, option_data=None):
    market_prices = market_prices or {}
    history_metrics = snapshot_metrics(option_data or {}, spot_price)
    rows = []
    day_id = market_prices.get("day_id")
    summary_day = "Provider session date unavailable"
    if isinstance(day_id, int) and not isinstance(day_id, bool):
        summary_day = f"Provider session {(datetime(1970, 1, 1) + timedelta(days=day_id)).strftime('%m/%d/%Y')}"
    for label, field in (
        ("Open price", "open"), ("High price", "high"), ("Low price", "low"),
        ("Last known close / latest price", "last"),
    ):
        value = market_prices.get(field)
        basis = summary_day
        if field == "last":
            timestamp = flow_store.finite_number(market_prices.get("last_time"))
            basis = (
                "Last provider trade: " + datetime.fromtimestamp(timestamp / 1000, flow_store.NEW_YORK).strftime("%m/%d/%Y %H:%M:%S ET")
                if timestamp and timestamp > 0 else "Last trade timestamp unavailable; not a confirmed final close"
            )
        rows.append({
            "Metric": label, "Strike / status": f"${value:,.2f}" if value is not None else "Unavailable",
            "Strength / basis": basis,
        })
    previous_close = confirmed_previous_close(market_prices)
    rows.append({
        "Metric": "Previous official close",
        "Strike / status": f"${previous_close:,.2f}" if previous_close is not None else "Unavailable",
        "Strength / basis": (
            f"Provider previous session {day_label(market_prices.get('prev_day_id'))}; FINAL price only. "
            f"Received type: {market_prices.get('prev_close_type') or 'unavailable'}"
        ),
    })
    for side in ("call", "put"):
        wall = levels[f"{side}_wall"]
        measurements = profile[f"{side}_oi_gex"]
        share = (
            f"{wall['gex'] / measurements.sum():.1%} of fetched {side} OI gamma"
            if wall and measurements.notna().all() and measurements.sum() > 0
            else "Concentration unavailable (missing inputs)"
        )
        rows.append({
            "Metric": f"{side.title()} wall",
            "Strike / status": f"${wall['strike']:,.2f}" if wall else "Unavailable",
            "Strength / basis": f"${format_heatmap_value(wall['gex'])} / 1% move; {share}" if wall else share,
        })
        oi = history_metrics.get(f"{side.upper()} WALL OI")
        rows.append({
            "Metric": f"{side.title()} wall OI",
            "Strike / status": f"{oi:,.0f} contracts" if oi is not None else "Unavailable",
            "Strength / basis": "Same-side open interest at the wall strike; fetched range",
        })
    for key, label in (("ABS", "Absolute GEX peak"), ("NET", "Net GEX peak")):
        strike, value = (history_metrics[f"{key} GEX {field}"] for field in ("STRIKE", "VALUE"))
        rows.append({
            "Metric": label,
            "Strike / status": f"${strike:,.2f}" if strike is not None else "Unavailable",
            "Strength / basis": (
                f"${format_heatmap_value(value)} / 1% move; "
                + ("largest call + put GEX" if key == "ABS" else "largest |call - put GEX|; signed value")
            ) if value is not None else "Complete fetched-range OI and gamma required",
        })
    for sign in ("Positive", "Negative"):
        strike, value = (history_metrics[f"{sign.upper()} NET GEX {field}"] for field in ("STRIKE", "VALUE"))
        complete = not profile.empty and profile["net_oi_gex"].notna().all()
        rows.append({
            "Metric": f"{sign} net GEX peak",
            "Strike / status": f"${strike:,.2f}" if strike is not None else "Unavailable",
            "Strength / basis": (
                f"${format_heatmap_value(value)} / 1% move; "
                f"{'largest positive' if sign == 'Positive' else 'most negative'} call - put OI GEX"
            ) if value is not None else (
                f"No {sign.lower()} net OI GEX in fetched range" if complete
                else "Complete fetched-range OI and gamma required"
            ),
        })
    for key in ("OI", "VOLUME"):
        value = history_metrics[f"P/C RATIO FOR {key}"]
        rows.append({
            "Metric": f"P/C ratio for {'OI' if key == 'OI' else 'Volume'}",
            "Strike / status": f"{value:.4f}" if value is not None else "Unavailable",
            "Strength / basis": f"Put / call totals; fetched range; {history_metrics[f'{key} COVERAGE']} valid contracts",
        })
    context = get_open_to_wall_context(
        market_prices.get("open"), market_prices.get("last"), levels,
        market_prices.get("comparable", False) and not price_is_fallback,
    )
    movement = "Unavailable"
    if context["change"] is not None:
        movement = f"{context['direction']} {context['change']:+,.2f} ({context['pct']:+.2f}%)"
    rows.append({
        "Metric": "Market direction from open", "Strike / status": movement,
        "Strength / basis": "Latest trade minus same-session open; descriptive, not a forecast",
    })
    approaches = []
    for side, change in context["approach"].items():
        description = "unchanged distance" if abs(change) < 1e-9 else f"{abs(change):,.2f} points {'closer' if change < 0 else 'farther'}"
        approaches.append(f"{side.title()}: {description} vs open")
    rows.append({
        "Metric": "Price vs call / put wall", "Strike / status": f"Nearest: {context['nearest']}",
        "Strength / basis": "; ".join(approaches) or "Matching session open and latest trade are required",
    })
    pain_strikes = (max_pain or {}).get("strikes", [])
    pain_text = "Unavailable"
    if pain_strikes:
        pain_text = f"${pain_strikes[0]:,.2f}"
        if len(pain_strikes) > 1:
            pain_text += f" - ${pain_strikes[-1]:,.2f} (tied minima)"
    coverage = (
        f"{max_pain['observed']}/{max_pain['expected']} listed contracts with valid OI"
        if max_pain and "expected" in max_pain else "Fetch data to load the full selected-expiry chain"
    )
    rows.append({"Metric": "Max pain (full expiry)", "Strike / status": pain_text, "Strength / basis": coverage})
    bias = get_wall_bias(profile, spot_price, price_is_fallback)
    for label, threshold in (("Bullish scenario", "upper"), ("Bearish scenario", "lower")):
        direction = "Above" if threshold == "upper" else "Below"
        rows.append({
            "Metric": label,
            "Strike / status": f"{direction} ${bias[threshold]:,.2f}" if bias[threshold] is not None else "Unavailable",
            "Strength / basis": "Unvalidated rule: spot above/below BOTH walls",
        })
    rows.append({
        "Metric": "Possible close direction",
        "Strike / status": f"{bias['bias']} (unvalidated)" if bias["bias"] != "Unavailable" else "Unavailable",
        "Strength / basis": "Snapshot rule only; no probability or closing-price target",
    })
    table = pd.DataFrame(rows)

    def color_row(row):
        color = {
            "Call wall": "#36cba5", "Put wall": "#f45b76",
            "Call wall OI": "#36cba5", "Put wall OI": "#f45b76",
            "Positive net GEX peak": "#36cba5", "Negative net GEX peak": "#f45b76",
            "Max pain (full expiry)": "#f5bf55",
            "Bullish scenario": "#36cba5", "Bearish scenario": "#f45b76",
            "Possible close direction": {
                "Bullish": "#36cba5", "Bearish": "#f45b76",
            }.get(bias["bias"], "#a9b6c8"),
            "Market direction from open": {"Up": "#36cba5", "Down": "#f45b76"}.get(context["direction"], "#a9b6c8"),
        }.get(row["Metric"], "#cbd5e1")
        return [f"color: {color}; background-color: #101217"] * len(row)

    st.subheader("Market summary")
    values = table.set_index("Metric")["Strike / status"].to_dict()
    overview = {"Current price": values.pop("Last known close / latest price")}
    for side in ("call", "put"):
        label = side.title()
        wall = levels[f"{side}_wall"]
        overview[f"{label} wall strike"] = values.pop(f"{label} wall")
        overview[f"{label} wall strength"] = (
            f"${format_heatmap_value(wall['gex'])} / 1% move" if wall else "Unavailable"
        )
        overview[f"{label} wall OI"] = values.pop(f"{label} wall OI")
    for sign in ("Positive", "Negative"):
        overview[f"{sign} net GEX strike"] = values.pop(f"{sign} net GEX peak")
        value = history_metrics[f"{sign.upper()} NET GEX VALUE"]
        overview[f"{sign} net GEX value"] = (
            f"${format_heatmap_value(value)} / 1% move" if value is not None else "Unavailable"
        )
    overview.update(values)
    summary = pd.DataFrame([overview]).style.hide(axis="index").format(escape="html")
    summary = summary.set_properties(**{
        "background-color": "#101217", "color": "#e5e7eb",
        "padding": "12px", "white-space": "nowrap", "border": "1px solid #334155",
    }).set_table_styles([{
        "selector": "th", "props": [
            ("background-color", "#18212f"), ("color", "#e5e7eb"),
            ("padding", "12px"), ("white-space", "nowrap"), ("text-align", "left"),
        ],
    }])
    for prefix, color in (
        ("Call wall", "#36cba5"), ("Put wall", "#f45b76"),
        ("Positive net GEX", "#36cba5"), ("Negative net GEX", "#f45b76"),
    ):
        summary = summary.set_properties(
            subset=[column for column in overview if column.startswith(prefix)],
            **{"color": color},
        )
    summary = summary.set_properties(subset=["Max pain (full expiry)"], **{"color": "#f5bf55"})
    st.html(
        '<div class="gex-summary-scroll" role="region" aria-label="Market summary table" '
        'tabindex="0" style="overflow-x:auto; max-width:100%; border-radius:8px;">'
        + summary.to_html(table_attributes='class="gex-summary-table" style="border-collapse:collapse;"')
        + '</div>'
    )
    st.caption("One row with a separate column for each metric. Scroll horizontally for all columns; green = call wall, red = put wall.")
    st.caption(
        "Call/put walls remain separate side-specific OI-gamma peaks. Positive/negative net GEX "
        "subtracts put exposure from call exposure at each strike, then selects the largest positive "
        "and most negative values across the full fetched range, on either side of price. "
        "These are separately labeled peaks, not verified Unusual Whales wall equivalents. "
        "No matching sign or incomplete inputs means unavailable, not zero."
    )
    with st.expander("Metric definitions and data coverage"):
        st.dataframe(table.style.apply(color_row, axis=1), hide_index=True, width="stretch", height=38 + 35 * len(rows))
    st.caption(
        "Open/high/low are provider session-summary values. Latest price is the last received underlying trade, "
        "not a guaranteed final settlement. Wall approach compares endpoint distances from open; "
        "both walls can become closer, and no future movement is implied."
    )
    st.caption(
        "UNVALIDATED directional rule: above both walls = bullish; below both = bearish; "
        "between or at the walls = neutral. This is not a calibrated close forecast. "
        "Strength is measured OI-gamma concentration in the fetched range, not a probability. "
        "Max pain minimizes listed-contract intrinsic payout at expiry; it is not a price target. "
        "OI and spot are provider snapshots; market-event freshness is not verified."
    )
    if max_pain and max_pain.get("reason"):
        st.warning(f"Max pain unavailable: {max_pain['reason']}")
    if price_is_fallback:
        st.warning("The underlying price is a configured fallback; directional estimates are disabled.")


@st.cache_data(ttl=5, show_spinner=False)
def load_observed_flow(symbol, expiration):
    return flow_store.read_flow_snapshot(flow_store.FLOW_DB, symbol, expiration)


def build_flow_chart(snapshot, symbol, expiration, height=750):
    figure = go.Figure()
    prices = {row["time_ms"]: row["close"] for row in snapshot["candles"]}

    def et(timestamp):
        # Plotly's date axis displays these explicit New York wall-clock times.
        return datetime.fromtimestamp(timestamp / 1000, flow_store.NEW_YORK).replace(tzinfo=None)

    if prices:
        figure.add_trace(go.Scatter(
            x=[et(timestamp) for timestamp in prices], y=list(prices.values()),
            name=f"{symbol} 1m close", mode="lines", yaxis="y2",
            line=dict(color="#f1f5f9", width=1.8), connectgaps=False,
            hovertemplate="%{x|%H:%M} ET<br>1m close %{y:,.2f}<extra></extra>",
        ))
    cumulative = 0
    xs, ys = [], []
    last_connection = None
    for minute in snapshot["minutes"]:
        if last_connection is not None and last_connection != minute["connection_id"]:
            xs.append(None)
            ys.append(None)
        cumulative += minute["signed_premium"]
        xs.append(et(minute["time_ms"]))
        ys.append(cumulative / 1_000_000)
        last_connection = minute["connection_id"]
    if ys:
        for positive, color in ((True, "#36cba5"), (False, "#f45b76")):
            values = [
                None if value is None else (max(value, 0) if positive else min(value, 0))
                for value in ys
            ]
            figure.add_trace(go.Scatter(
                x=xs, y=values, name="Positive recorded net" if positive else "Negative recorded net",
                mode="lines", fill="tozeroy", connectgaps=False,
                line=dict(color=color, width=1.3), opacity=0.55,
                hoverinfo="skip", showlegend=False,
            ))
        figure.add_trace(go.Scatter(
            x=xs, y=ys, name="Cumulative recorded net premium", mode="lines",
            line=dict(color="#c4b5fd", width=1), connectgaps=False,
            hovertemplate="%{x|%H:%M} ET<br>Recorded net $%{y:,.3f}M<extra></extra>",
        ))
    located = [trade for trade in snapshot["trades"] if (trade["time_ms"] // 60000) * 60000 in prices]
    if located:
        largest = max(trade["premium"] for trade in located)
        figure.add_trace(go.Scatter(
            x=[et(trade["time_ms"]) for trade in located],
            y=[prices[(trade["time_ms"] // 60000) * 60000] for trade in located],
            yaxis="y2", mode="markers", name="Largest observed classified prints",
            marker=dict(
                size=[6 + 28 * math.sqrt(trade["premium"] / largest) for trade in located],
                color=["#36cba5" if trade["signed_premium"] > 0 else "#f45b76" for trade in located],
                opacity=0.7, line=dict(width=1, color="#101217"),
            ),
            customdata=[[trade["symbol"], trade["signed_premium"]] for trade in located],
            hovertemplate=(
                "%{x|%H:%M:%S} ET<br>%{customdata[0]}<br>Signed premium $%{customdata[1]:,.0f}"
                "<br>Containing 1m close %{y:,.2f}<extra></extra>"
            ),
        ))
    if not snapshot["minutes"]:
        figure.add_annotation(
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            text="No classified option trades collected<br>Flow unavailable - not zero",
            font=dict(color="#cbd5e1", size=15), bgcolor="#101217", borderpad=12,
        )
    # These are socket/subscription windows, not independently certified exchange coverage.
    for previous, following in zip(snapshot["intervals"], snapshot["intervals"][1:]):
        end = previous["ended_ms"] or previous["last_seen_ms"]
        if following["started_ms"] > end:
            figure.add_vrect(
                x0=et(end), x1=et(following["started_ms"]),
                fillcolor="#f5bf55", opacity=0.15, line_width=0, layer="below",
            )
    expiry = datetime.strptime(expiration, "%y%m%d").strftime("%m/%d/%Y")
    figure.update_layout(
        title=dict(text=f"{symbol} observed Flow | {expiry}", font=dict(size=16)),
        template="plotly_dark", paper_bgcolor="#101217", plot_bgcolor="#101217",
        font=dict(color="#e5e7eb"), height=height,
        margin=dict(l=65, r=65, t=100, b=65),
        xaxis=dict(title="New York time (ET)", tickformat="%H:%M", showgrid=False),
        yaxis=dict(title="Recorded net premium ($M)", zerolinecolor="#88909e"),
        yaxis2=dict(title=f"{symbol} price", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=-0.13, font=dict(size=10)),
    )
    return figure


def render_observed_flow(symbol, expiration, height):
    try:
        snapshot = load_observed_flow(symbol, expiration)
    except (sqlite3.Error, OSError, ValueError) as error:
        st.error(f"Cannot read the local Flow recording ({type(error).__name__}); no flow estimate is shown.")
        return
    status = snapshot["status"]
    fresh = time.time() * 1000 - status.get("heartbeat_ms", 0) < 20000
    state = status.get("state", "not started") if fresh else "stopped / stale"
    st.caption(f"Recorder (self-reported): {state} | SPX / SPXW active PM expiry | Port 8502")
    st.plotly_chart(
        build_flow_chart(snapshot, symbol, expiration, height),
        width="stretch", key="observed_option_flow", theme=None, config={"displayModeBar": False},
    )
    if status.get("error"):
        st.warning(status["error"])
    if fresh and status.get("state") == "paused":
        st.info(f"Recorder paused: {status.get('pause_reason', 'Outside scheduled trading hours')}")
        if status.get("next_open"):
            st.caption("Next scheduled open: " + datetime.fromisoformat(status["next_open"]).strftime("%m/%d/%Y %I:%M %p ET"))
    elif not fresh or status.get("state") != "listening":
        st.warning("Recorder is not currently listening. Start start_flow_recorder.bat; old observations remain available.")
    elif symbol != "SPX" or expiration != status.get("expiry"):
        st.info("The active recorder follows SPXW's current/next PM expiry; this selection shows only its own stored observations.")
    classified = sum(row["count"] for row in snapshot["counts"] if row["reason"] is None)
    excluded = sum(row["count"] for row in snapshot["counts"] if row["reason"] is not None)
    st.caption(
        f"Collected indexed states: {classified:,} classified | {excluded:,} excluded / cancelled. "
        "No full-session or complete-exchange-tape guarantee."
    )
    st.caption(
        "Local signed-premium convention (not Tradytics methodology): call BUY / put SELL = green; "
        "call SELL / put BUY = red. Premium = option price x contracts x listed multiplier. "
        "Unknown aggressors, spread legs and invalid ticks are excluded. This is not directionalized GEX."
    )
    st.caption(
        "Net line sums only collected classified prints; corrections restate earlier totals. "
        "Bubbles: up to 200 largest classified prints, anchored to their containing 1-minute close, "
        "not exact trade-time spot. White line: provider 1-minute price history, which can precede recording. "
        "Amber shading marks reconnect gaps; missing flow is never backfilled."
    )
    with st.expander("Recorded connection windows / exclusions"):
        if snapshot["intervals"]:
            windows = pd.DataFrame([{
                "Subscription requested (ET)": datetime.fromtimestamp(row["started_ms"] / 1000, flow_store.NEW_YORK).strftime("%m/%d/%Y %H:%M:%S"),
                "Last heartbeat / end (ET)": datetime.fromtimestamp((row["ended_ms"] or row["last_seen_ms"]) / 1000, flow_store.NEW_YORK).strftime("%m/%d/%Y %H:%M:%S"),
                "Contracts requested": row["contract_count"],
                "End reason": row["reason"] or "Open interval; heartbeat is self-reported",
            } for row in snapshot["intervals"]])
            st.dataframe(windows, hide_index=True, width="stretch")
        else:
            st.info("No recording windows for this underlying / expiry. The recorder follows SPXW's active PM expiry, including overnight rollover.")
        if snapshot["counts"]:
            st.dataframe(pd.DataFrame([{
                "Classification": row["reason"] or "classified (local sign convention)",
                "Indexed states": row["count"],
            } for row in snapshot["counts"]]), hide_index=True, width="stretch")
        st.caption(
            "The recorder is separate from the browser and retains SQLite data through restarts. "
            "Keep the machine awake and the recorder running. Automatic start after Windows reboot is not configured."
        )


def render_wall_heatmap(option_data, spot_price, symbol, expiration, max_pain=None, price_is_fallback=False, market_prices=None):
    profile = build_exposure_profile(option_data, spot_price)
    if profile.empty:
        st.warning("No option observations are available for the wall/volume heatmap.")
        return
    levels = get_wall_levels(profile, spot_price)
    expiry_display = datetime.strptime(expiration, "%y%m%d").strftime("%m/%d/%Y")
    st.header("Strike Heatmap & Call / Put Walls")
    st.caption(
        f"{symbol} | Expiration {expiry_display} | Selected strike range only. "
        "Walls are the largest available side-specific gamma x open-interest concentrations; "
        "they are estimates, not proven support/resistance or dealer positions."
    )
    incomplete = int(profile[["call_oi_gex", "put_oi_gex"]].isna().sum().sum())
    if incomplete:
        st.warning(
            f"{incomplete} strike-side OI gamma values are unavailable. "
            "Walls use valid observations only and may change when missing data arrives."
        )
    call_col, put_col, atm_col, directional_col = st.columns(4)
    for side, column in (("call", call_col), ("put", put_col)):
        wall = levels[f"{side}_wall"]
        column.metric(
            f"{side.title()} wall (OI gamma)",
            f"${wall['strike']:,.2f}" if wall else "Unavailable",
        )
        if wall:
            column.caption(f"${format_heatmap_value(wall['gex'])} per 1% move")
    atm_col.metric("Nearest fetched strike", f"${levels['atm_strike']:,.2f}")
    directional_col.metric("Directionalized GEX", "Unavailable")
    render_market_summary(profile, levels, spot_price, max_pain, price_is_fallback, market_prices, option_data)
    st.info(
        "Directionalized GEX is unavailable: this dashboard's snapshots do not contain "
        "verified, full-session option trade-side history. Call-minus-put volume is not "
        "trade-direction classification and is not an Unusual Whales equivalent."
    )

    measure_col, range_col = st.columns([3, 1])
    with measure_col:
        measure = st.radio(
            "Heatmap measure",
            ["Gamma notional", "Contract volume"],
            horizontal=True,
            key="wall_heatmap_measure",
        )
    with range_col:
        strike_count = st.selectbox(
            "Visible strikes", ["25", "50", "All"], key="wall_heatmap_strikes",
        )
    if measure == "Gamma notional":
        columns = ["net_volume_gex", "net_oi_gex"]
        st.caption(
            "Per 1% spot move: gamma x contracts x 100 x spot^2 x 0.01. "
            "Both columns use calls minus puts (a sign convention, not inferred buying/selling). "
            "Volume weights cumulative traded contracts; OI weights outstanding contracts. "
            "These estimates use current gamma and differ from the original GEX panel's units."
        )
    else:
        columns = ["call_volume", "put_volume"]
        st.caption("Cumulative contracts traded, without inferred buy/sell direction.")

    visible = select_visible_strikes(profile, levels, strike_count)
    outside = [
        f"{side.title()} wall {levels[f'{side}_wall']['strike']:,.2f}"
        for side in ("call", "put")
        if levels[f"{side}_wall"] and levels[f"{side}_wall"]["strike"] not in set(visible["strike"])
    ]
    if outside:
        st.info(
            f"{'; '.join(outside)} outside the displayed strike range "
            f"({visible['strike'].min():,.2f} - {visible['strike'].max():,.2f}). "
            "Set Visible strikes to All to show both wall lines."
        )
    st.caption("Bar colors show net GEX: green = positive calls-minus-puts; red = negative. Calls and puts are not separate bars.")
    st.subheader("Intraday Flow & GEX")
    flow_col, bar_col = st.columns([1.35, 1])
    gex_bars = build_gex_bar_chart(profile, levels, symbol, expiration, strike_count, max_pain)
    with flow_col:
        render_observed_flow(symbol, expiration, gex_bars.layout.height)
    with bar_col:
        st.caption("Selected-expiry OI GEX snapshot | Call wall green / put wall red / max pain amber")
        st.plotly_chart(
            gex_bars, width="stretch", key="horizontal_gex_bars", theme=None,
            config={"displayModeBar": False},
        )
        st.caption(
            "Tastytrade snapshot data, not Tradytics flow. Green = positive calls-minus-puts "
            "OI gamma; red = negative. Only levels inside the displayed strike range are drawn."
        )
        st.link_button("Open Tradytics reference", "https://tradytics.com/options-market")
    st.subheader("Strike heatmap")
    matrix = profile[columns]
    if matrix.notna().any().any():
        heatmap = build_strike_heatmap(
            profile, levels, symbol, expiration, measure, strike_count,
        )
        st.caption(
            f"Showing {len(heatmap.data[0].y)} of {len(profile)} fetched strikes, highest first. "
            "Wall metrics use the full fetched range; off-screen walls are not drawn. "
            "Gamma columns share one color scale. N/A means unavailable, not zero."
        )
        st.plotly_chart(
            heatmap, width="stretch", key="wall_volume_heatmap", theme=None,
            config={"displayModeBar": False},
        )
        missing = int(matrix.isna().sum().sum())
        if missing:
            st.warning(f"{missing} heatmap cells lack valid inputs and are shown as N/A, not set to zero.")
    else:
        st.warning("No valid values are available for the selected heatmap measure.")
    with st.expander("Wall / volume snapshot values"):
        st.dataframe(profile, hide_index=True, width="stretch")
    st.divider()


@st.fragment(run_every=1)
def auto_refresh_tick():
    if st.session_state.auto_refresh and time.time() >= st.session_state.next_ui_tick:
        st.rerun(scope="app")


def require_fetch_session(symbol, option_root, expiration):
    schedule = market_state(symbol, option_root, expiration)
    if not schedule.allowed:
        raise ValueError(f"Automatic snapshot paused: {schedule.label}")


def build_wall_strength_history_chart(summary):
    figure = go.Figure()
    for side, color in (("Call", "#36cba5"), ("Put", "#f45b76")):
        values = summary.loc[summary["Side"] == side]
        if values.empty:
            continue
        latest = values["Latest valid strength"]
        statuses = values["Wall status"]
        figure.add_trace(go.Bar(
            name=f"{side} wall",
            x=[f"{strike:,.2f}".rstrip("0").rstrip(".") for strike in values["Wall strike"]],
            y=latest / 1e6, marker_color=color,
            marker_opacity=[1.0 if status == "Latest wall" else 0.4 for status in statuses],
            marker_line=dict(
                color="#ffffff",
                width=[2 if status.startswith("Latest wall;") or status == "Latest wall" else 0 for status in statuses],
            ),
            text=[
                "LATEST" if status == "Latest wall" else
                "LATEST / old strength" if status.startswith("Latest wall;") else
                "FORMER" if status == "Former wall" else "STATUS UNKNOWN"
                for status in statuses
            ],
            textposition="outside", cliponaxis=False,
            error_y=dict(
                type="data", symmetric=False,
                array=(values["High strength"] - latest) / 1e6,
                arrayminus=(latest - values["Low strength"]) / 1e6,
                color="#e5e7eb", thickness=1.5, width=5,
            ),
            customdata=values[[
                "Low strength", "High strength", "Latest valid capture (ET)", "Valid snapshots",
                "Wall status", "Latest snapshot (ET)",
            ]].values,
            hovertemplate=(
                "Strike %{x}<br>Latest valid: $%{y:,.3f}M / 1% move"
                "<br>Recorded low: $%{customdata[0]:,.2f}"
                "<br>Recorded high: $%{customdata[1]:,.2f}"
                "<br>Latest valid capture: %{customdata[2]}"
                "<br>Status: %{customdata[4]}<br>Latest selected snapshot: %{customdata[5]}"
                "<br>Valid snapshots: %{customdata[3]}<extra>%{fullData.name}</extra>"
            ),
        ))
    strikes = sorted(summary["Wall strike"].unique())
    figure.update_layout(
        title="Recorded call / put wall strength by strike",
        barmode="group", height=420, template="plotly_dark",
        paper_bgcolor="#101217", plot_bgcolor="#101217", font=dict(color="#e5e7eb"),
        xaxis=dict(
            title="Wall strike (not time) - LATEST / FORMER labels identify status", type="category", categoryorder="array",
            categoryarray=[f"{strike:,.2f}".rstrip("0").rstrip(".") for strike in strikes],
        ),
        yaxis=dict(title="GEX ($ millions per 1% spot move)", rangemode="tozero"),
        legend=dict(orientation="h"), margin=dict(l=65, r=20, t=75, b=65),
    )
    return figure


def render_wall_strength_history(rows):
    st.subheader("Wall strength histogram & watermarks")
    if rows:
        latest = max(rows, key=lambda row: row["SLNO"])
        st.caption(f"Latest retained snapshot in this selection: {latest['TIMESTAMP']} (ET). Not a live quote.")
        for column, side in zip(st.columns(2), ("CALL", "PUT")):
            strike = flow_store.finite_number(latest.get(f"{side} WALL STRIKE"))
            strength = flow_store.finite_number(latest.get(f"{side} WALL STRENGTH"))
            column.metric(
                f"Latest recorded {side.lower()} wall",
                f"{strike:,.2f}" if strike is not None and strike > 0 else "Unavailable",
            )
            column.caption(
                f"Latest snapshot strength: ${format_heatmap_value(strength)} / 1% move"
                if strike is not None and strike > 0 and strength is not None and strength >= 0
                else "Latest snapshot strength unavailable; older bars are not a substitute."
            )
    summary = wall_strength_history(rows)
    if summary.empty:
        st.info("No valid retained wall strengths for this history selection. Missing values are not zero.")
        return
    strikes = st.multiselect(
        "Wall strikes (empty = all)", sorted(summary["Wall strike"].unique()),
        format_func=lambda strike: f"{strike:,.2f}", key="wall_strength_strikes",
        help="Select the call and/or put wall strikes to compare. Uses the history session selected above.",
    )
    if strikes:
        summary = summary.loc[summary["Wall strike"].isin(strikes)]
    st.plotly_chart(
        build_wall_strength_history_chart(summary), width="stretch",
        key="wall_strength_history", theme=None, config={"displayModeBar": False},
    )
    st.dataframe(
        summary.style.set_properties(**{"background-color": "#101217"}).apply(
            lambda row: [f"color: {'#36cba5' if row['Side'] == 'Call' else '#f45b76'}"] * len(row),
            axis=1,
        ).format({
            "Wall strike": "{:,.2f}", "Low strength": format_heatmap_value,
            "High strength": format_heatmap_value, "Latest valid strength": format_heatmap_value,
        }, na_rep="Unavailable"),
        hide_index=True, width="stretch", height=min(400, 38 + 35 * len(summary)),
    )
    st.caption(
        "Green = call wall; red = put wall. Bars show latest valid retained strength at each strike; "
        "LATEST = latest retained wall in the selection; FORMER = an earlier wall. "
        "Former bars are muted; filtering strikes never changes latest status. "
        "LATEST / old strength means the latest row lacks strength, so the bar is an older observation. "
        "whiskers show its recorded low/high, with capture timestamps in the table. "
        "Strength is side-specific OI GEX, USD per 1% spot move, not net GEX or trade direction. "
        "Uses all retained rows in the selected symbol/root/expiry/session, not just the latest 500 shown above. "
        "Only snapshots when that strike was the selected side's wall contribute; missing inputs are excluded. "
        "These are sampled watermarks, not continuous intraminute extremes or a measure of time spent at a strike."
    )


def render_snapshot_history(symbol, option_root, expiration, strikes_up, strikes_down, increment, automatic_expiry):
    st.subheader("Market summary history")
    try:
        with HistoryStore() as store:
            config = store.get("config", DEFAULT_CONFIG)
            status = store.get("status", {})
            expiry_display = datetime.strptime(expiration, "%y%m%d").strftime("%m/%d/%Y")
            all_rows = store.rows()
            rows = [
                row for row in all_rows
                if row["SYMBOL"] == symbol and row["OPTION ROOT"] == option_root and row["EXPIRY"] == expiry_display
            ]
            with st.expander("Background history settings"):
                enabled = st.checkbox("Enable background history", value=config["enabled"], key="history_enabled")
                st.caption(
                    f"Collector configuration: {config['symbol']} / {config['root']}; "
                    f"expiry {config['expiry'] or 'active/next PM session'}; "
                    f"{config['strikes_down']} below + center + {config['strikes_up']} above. "
                    "Apply to follow the dashboard selection. One configuration is collected at a time."
                )
                if st.button("Apply background history settings", key="apply_history"):
                    store.set("config", validate_config(dict(
                        enabled=enabled, symbol=symbol, root=option_root,
                        expiry=None if automatic_expiry else expiration,
                        strikes_up=int(strikes_up), strikes_down=int(strikes_down), increment=increment,
                    )))
                    st.success("Saved background history settings. The running collector checks them on its next cycle.")
            heartbeat = status.get("heartbeat_ms", 0)
            fresh = 0 <= time.time() * 1000 - heartbeat < 90000
            if not fresh:
                st.warning("History collector is not reporting a fresh heartbeat. Start start_history_recorder.bat.")
            elif status.get("state") == "paused":
                st.info(f"Background history paused: {status.get('pause_reason', 'outside scheduled hours')}")
            elif status.get("state") in {"retrying", "failed", "stopped"}:
                st.warning(f"Background history: {status['state']}. {status.get('error') or ''}")
            else:
                st.caption(
                    f"Collector self-reported state: {status.get('state')}; "
                    f"{status.get('symbol', '?')} / expiry {status.get('expiry', '?')}; "
                    f"OI {status.get('oi_coverage', 'not yet observed')}, "
                    f"gamma {status.get('gamma_coverage', 'not yet observed')}."
                )
        st.caption(
            "Browser-independent collector polls once per minute during scheduled symbol sessions. "
            "Retains the first snapshot, every 15 minutes (including updated wall strengths), and "
            "whenever either side-specific call-wall or put-wall strike changes. Wall-strike changes "
            "do not reset the 15-minute schedule. "
            "Changes between polls can be missed. No pre-start history or missed intervals are backfilled."
        )
        if rows:
            latest = rows[-1]
            if "Underlying last trade is" in latest["REMARKS"]:
                st.warning(
                    f"The latest retained row uses an older underlying trade: {latest['PRICE TIMESTAMP']}. "
                    "Capture time is not price-update time. See REMARKS for missing/unconfirmed inputs."
                )
            sessions = list(dict.fromkeys(row["SESSION DATE"] for row in reversed(rows)))
            selection = st.selectbox("History session (ET)", ["All saved sessions", *sessions], index=1, key="history_session")
            selected = rows if selection == "All saved sessions" else [row for row in rows if row["SESSION DATE"] == selection]
            frame = pd.DataFrame(selected, columns=HISTORY_COLUMNS)
            display = frame.iloc[::-1].head(500)
            formats = {column: "{:,.2f}" for column in (
                "CURRENT PRICE", "CALL WALL STRIKE", "PUT WALL STRIKE", "DAY OPEN",
                "DAY HIGH", "DAY LOW", "CLOSE PRICE", "ABS GEX STRIKE", "NET GEX STRIKE",
                "POSITIVE NET GEX STRIKE", "NEGATIVE NET GEX STRIKE",
            )}
            formats.update({column: format_heatmap_value for column in (
                "CALL WALL STRENGTH", "PUT WALL STRENGTH", "ABS GEX VALUE", "NET GEX VALUE",
                "POSITIVE NET GEX VALUE", "NEGATIVE NET GEX VALUE",
            )})
            formats.update({column: "{:,.0f}" for column in ("CALL WALL OI", "PUT WALL OI")})
            formats.update({column: "{:.4f}" for column in ("P/C RATIO FOR OI", "P/C RATIO FOR VOLUME")})
            compact_columns = HISTORY_COLUMNS[:11]
            compact = display[compact_columns].copy()
            compact["CAPTURE REASON"] = display["REMARKS"].map(capture_reason)
            st.dataframe(
                compact.style.apply(
                    lambda _: history_style(display).reindex(columns=compact.columns, fill_value=""), axis=None,
                ).format({key: value for key, value in formats.items() if key in compact}, na_rep="Unavailable"),
                hide_index=True, width="stretch", height=min(650, 38 + 35 * len(display)),
            )
            st.caption(
                f"Showing latest {len(display)} of {len(selected)} matching retained rows, newest first. "
                "CLOSE PRICE is the previous official close. Diagnostics and original remarks remain in full details. "
                "Display filters do not limit the master CSV."
            )
            with st.expander("Full snapshot details (all original columns)"):
                st.dataframe(
                    display.style.apply(history_style, axis=None).format(formats, na_rep="Unavailable"),
                    hide_index=True, width="stretch",
                )
            with st.expander("Sampled crossings (unique events)"):
                events = crossing_history(selected)
                st.caption("Each scope/side/strike/event appears once here. Session-wide annotations may postdate an individual snapshot.")
                if events.empty:
                    st.info("No sampled crossing events in this selection.")
                else:
                    st.dataframe(events, hide_index=True, width="stretch")
            render_wall_strength_history(selected)
        else:
            st.info("No retained snapshots for this symbol/root/expiry yet. Earlier snapshots cannot be reconstructed.")
        st.download_button(
            "Download master history CSV", data=csv_text(all_rows).encode("utf-8-sig"),
            file_name="snapshot_history.csv", mime="text/csv",
            key="download_snapshot_history",
        )
        st.caption(
            f"One master CSV: {len(all_rows)} saved rows across all symbols, expiries and sessions, oldest first. "
            "The collector continuously updates flow_data\\snapshot_history.csv; it does not rotate files each day. "
            "A downloaded copy is a snapshot and does not update automatically."
        )
        st.caption(
            "Call walls are green; put walls are red. Bold shaded strikes were crossed between sampled "
            "underlying prices after that wall was first observed. Crossings are marked throughout that "
            "session's history, not proof that every intraday crossing was captured. A moving wall alone "
            "does not count. CSV uses explicit crossing labels, not colors."
        )
        st.caption(
            "Strength and GEX values are USD per 1% spot move. ABS GEX is call + put GEX, "
            "not the older chart's |net GEX|. Net peak retains its sign. Positive/negative net GEX columns "
            "separately select the largest positive and most negative call-minus-put OI GEX in the fetched "
            "range, regardless of price side; these are not verified Unusual Whales walls. "
            "Existing call/put wall columns keep their side-specific definition. New columns are blank "
            "for older rows; past values are not reconstructed. P/C ratios require complete "
            "selected-range inputs and a nonzero call denominator. CLOSE PRICE is the provider's previous "
            "FINAL close only; unconfirmed closes stay blank. PRICE TIMESTAMP exposes stale underlying trades. "
            "All-symbol durable CSV: flow_data\\snapshot_history.csv. Keep the machine awake and collector "
            "running; reboot autostart is not configured."
        )
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        st.error(f"History could not be read or updated ({type(error).__name__}). No history was fabricated.")


@st.fragment(run_every="15s")
def render_intraday_gex(config, expiration, session_date):
    st.subheader(f"{config['symbol']} daily GEX")
    st.caption(
        f"{config['root']} expiry {datetime.strptime(expiration, '%y%m%d'):%m/%d/%Y} | "
        "Tastytrade/dxFeed inputs; call-positive / put-negative OI gamma estimate. "
        "Not Tradytics data or verified dealer positions."
    )
    try:
        with HistoryStore() as store:
            sessions = sorted(set([session_date.isoformat(), *store.gex_sessions(config, expiration)]), reverse=True)
            controls = st.columns(2)
            view = controls[0].selectbox("GEX display", INTRADAY_GEX_VIEWS, key="daily_gex_view")
            scope = gex_scope(config, expiration)
            session = controls[1].selectbox("GEX session (ET)", sessions, key=f"daily_gex_session:{scope}")
            captures = store.gex_rows(config, expiration, session)
            recorder = store.get("config", DEFAULT_CONFIG)
            status = store.get("status", {})
        recorded_expiry = recorder["expiry"] or status.get("expiry") or session_date.strftime("%y%m%d")
        matching = recorder["enabled"] and gex_scope(recorder, recorded_expiry) == scope
        if not matching:
            st.info(
                "Background recording is configured for a different selection or is disabled. "
                "Use Background history settings below to apply this symbol, expiry and strike range. "
                "Successful dashboard fetches also record this selection during its scheduled session."
            )
        elif not (0 <= time.time() * 1000 - status.get("heartbeat_ms", 0) < 90000):
            st.warning("Background GEX recording has no fresh heartbeat. Intraday coverage may have gaps.")
        elif status.get("state") in {"retrying", "failed", "stopped"}:
            st.warning(f"Background GEX recorder reports {status['state']}; new captures are not assured.")
        elif status.get("state") == "paused":
            st.caption("Background recorder reports paused; saved captures remain available.")
        st.caption(
            "Records each successful background poll (about one minute) and dashboard fetch. "
            "Filters follow the selected symbol, option root, expiry and strike-range settings. "
            "No pre-start history or missing intervals are reconstructed. Panel checks saved data every 15 seconds."
        )
        if not captures:
            st.info("No per-strike GEX captures for this selection/session yet. Earlier wall-summary rows cannot reconstruct them.")
            return
        latest = captures[-1]
        frame = capture_frame(latest)
        available = int(frame["net_oi_gex"].notna().sum())
        st.caption(
            f"Latest saved capture: {time_label(latest['at_ms'])} | Source: {latest['source']} | "
            f"Price timestamp: {time_label(latest['price_ms']) or 'Unavailable'} | "
            f"{available}/{len(frame)} strikes with net GEX | {len(captures)} captures."
        )
        if latest["spot"] is None:
            st.warning(latest["quality"])
        if available < len(frame):
            st.warning("Incomplete latest inputs: missing strikes stay blank; displayed rankings are provisional within observed strikes.")
        if time.time() * 1000 - latest["at_ms"] > 120000:
            st.warning("Saved snapshot, not live: latest capture is more than two minutes old.")
        baseline = None
        if view == "Changes":
            if len(captures) < 2:
                st.info("Changes require two captures in the same selection/session; no baseline exists yet.")
                return
            earlier = {capture["at_ms"]: capture for capture in captures[:-1]}
            comparison = st.selectbox(
                "Compare latest against", ["Previous recorded capture", "First recorded capture", "Choose capture"],
                key="daily_gex_comparison",
            )
            if comparison == "Choose capture":
                chosen = st.selectbox(
                    "Baseline capture (ET)", list(reversed(earlier)), format_func=time_label,
                    key=f"daily_gex_baseline:{scope}:{session}",
                )
                baseline = earlier[chosen]
            else:
                baseline = captures[-2] if comparison == "Previous recorded capture" else captures[0]
            st.caption(f"Baseline: {time_label(baseline['at_ms'])}. Latest minus baseline; missing/new/absent strikes are not treated as zero.")
        st.plotly_chart(
            build_intraday_gex_chart(captures, view, baseline), width="stretch",
            key="daily_gex_chart", theme=None, config={"displayModeBar": False},
        )
        if view == "Levels":
            st.caption(
                "Dashed lines = the latest capture's three largest positive and three most negative net-GEX strikes "
                "within the fetched range, not side-specific call/put walls. They are overlaid on sampled prices "
                "for reference, not a reconstruction of past levels. Gaps over two minutes break the price line."
            )
        elif view == "Throughout day":
            st.caption("Green = positive net GEX; red = negative. Blank cells = unavailable observations. No forward-filling; this is sampled history, not a continuous feed.")
        elif view == "Changes":
            st.caption("Green = increased net GEX; red = decreased net GEX. This is not a buy/sell flow signal; changes can reflect gamma, spot or OI inputs.")
        st.caption(f"All GEX values: {INTRADAY_GEX_UNITS}. OI is a provider snapshot, not a live count of intraday positions.")
        with st.expander("GEX values and capture details"):
            details = gex_changes(latest, baseline) if baseline else frame.reset_index()
            st.dataframe(details, hide_index=True, width="stretch")
            st.caption(latest["quality"])
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        st.error(f"Daily GEX could not be read or rendered ({type(error).__name__}). No data was fabricated.")


def main():
    st.session_state.next_ui_tick = time.time() + 1
    st.title("📊 Options Gamma Exposure Dashboard")

    # Initialize session state
    if 'data_fetched' not in st.session_state:
        st.session_state.data_fetched = False
    if 'gex_calculator' not in st.session_state:
        st.session_state.gex_calculator = None
    if 'auto_refresh' not in st.session_state:
        st.session_state.auto_refresh = True
    if 'last_fetch_time' not in st.session_state:
        st.session_state.last_fetch_time = 0
    if 'option_data' not in st.session_state:
        st.session_state.option_data = {}
    if 'gex_view' not in st.session_state:
        st.session_state.gex_view = "Calls vs Puts"
    if 'volume_view' not in st.session_state:
        st.session_state.volume_view = "Calls vs Puts"

    # Sidebar controls
    with st.sidebar:
        st.header("⚙️ Settings")

        # Symbol selection: Preset or Custom
        symbol_mode = st.radio("Symbol Mode", ["Preset", "Custom"], horizontal=True)

        if symbol_mode == "Preset":
            symbol = st.selectbox("Underlying", list(PRESET_SYMBOLS.keys()))
            config = PRESET_SYMBOLS[symbol]
            option_prefix = config["option_prefix"]
            increment = config["increment"]
            default_price = config["default_price"]
        else:
            st.caption("Enter any symbol and its option parameters")
            symbol = st.text_input("Underlying Symbol", value="AAPL", max_chars=10).upper()
            option_prefix = st.text_input("Option Prefix", value="AAPL", max_chars=10,
                                         help="Usually same as underlying (e.g., AAPL, TSLA)").upper()
            increment = st.number_input("Strike Increment", min_value=0.5, max_value=100.0, value=2.5, step=0.5,
                                       help="SPY/QQQ: 1, AAPL: 2.5, TSLA: 5, SPX: 5, NDX: 25")
            default_price = st.number_input("Fallback Price", min_value=1.0, max_value=100000.0, value=100.0,
                                          help="Used if live price unavailable")

        try:
            scheduled = market_state(symbol, option_prefix)
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as error:
            scheduled = MarketState(False, f"Exchange calendar unavailable ({type(error).__name__}); automatic fetching paused.")
        default_exp = scheduled.session_date or datetime.now(ZoneInfo("America/New_York")).date()
        if "selected_expiry" not in st.session_state or st.session_state.selected_expiry == st.session_state.get("scheduled_default_expiry"):
            st.session_state.selected_expiry = default_exp
        st.session_state.scheduled_default_expiry = default_exp

        expiration_date = st.date_input(
            "Expiration (MM/DD/YYYY)",
            key="selected_expiry",
            format="MM/DD/YYYY",
            help="Defaults to the active/next scheduled PM expiry, including overnight rollover. Manual selections are preserved."
        )
        expiration = expiration_date.strftime("%y%m%d")
        try:
            schedule = market_state(symbol, option_prefix, expiration)
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as error:
            schedule = MarketState(False, f"Exchange calendar unavailable ({type(error).__name__}); automatic fetching paused.")

        with st.expander("Strike Range"):
            strikes_up = st.number_input("Strikes above center", min_value=5, max_value=50, value=50)
            strikes_down = st.number_input("Strikes below center", min_value=5, max_value=50, value=50)
            total_strikes = strikes_up + strikes_down + 1
            st.caption(
                f"{strikes_down} below + center + {strikes_up} above = {total_strikes} strikes. "
                f"{total_strikes * 2} call/put contracts."
            )

        st.divider()

        # Auto-refresh controls
        st.subheader("🔄 Auto-Refresh")
        st.session_state.auto_refresh = st.checkbox(
            "Enable auto-refresh",
            value=st.session_state.auto_refresh,
            help="Automatically refetch data every X seconds"
        )

        if st.session_state.auto_refresh:
            refresh_interval = st.slider(
                "Refresh interval (seconds)",
                min_value=30,
                max_value=300,
                value=60,
                step=10,
                help="How often to refresh data"
            )
        else:
            refresh_interval = 60

        st.divider()

        # Manual fetch button
        fetch_triggered = st.button("🔄 Fetch Data", type="primary", width='stretch')
        manual_fetch = fetch_triggered
        if schedule.allowed:
            st.caption(f"Scheduled session: {schedule.label} (ET). Feed availability is checked on connection.")
        else:
            st.info(f"Auto-fetch paused: {schedule.label}")
            if schedule.next_open:
                st.caption(f"Next scheduled open: {schedule.next_open.strftime('%m/%d/%Y %I:%M %p ET')}")
            st.caption("Existing data stays visible. Fetch Data is an explicit manual snapshot request.")

        # Auto-fetch logic
        if st.session_state.auto_refresh and schedule.allowed:
            current_time = time.time()
            if current_time - st.session_state.last_fetch_time >= refresh_interval:
                fetch_triggered = True

        if fetch_triggered:
            with st.spinner(f"Fetching {symbol} data..."):
                try:
                    guard = None if manual_fetch else lambda: require_fetch_session(symbol, option_prefix, expiration)
                    if guard:
                        guard()
                    # Get token and connect
                    token = ensure_streamer_token()
                    if guard:
                        guard()
                    st.info("Loading the full selected-expiry chain for max pain...")
                    chain_error = None
                    try:
                        full_chain = fetch_expiry_chain(symbol, option_prefix, expiration)
                    except (requests.RequestException, ValueError) as error:
                        full_chain = {}
                        chain_error = str(error)
                    if guard:
                        guard()
                    ws = connect_websocket(token)
                    try:
                        st.info(f"📊 Getting {symbol} price...")
                        market_prices = get_underlying_snapshot(ws, symbol, session_guard=guard)
                        price = market_prices["price"]
                        price_is_fallback = not price
                        if price_is_fallback:
                            price = default_price
                            st.warning(f"⚠️ Using fallback price: ${price}")
                        else:
                            st.success(f"✅ {symbol} Price: ${price:,.2f}")

                        option_symbols = generate_option_symbols(
                            price, option_prefix, expiration,
                            strikes_up, strikes_down, increment,
                        )
                        st.info(
                            f"Fetching {len(option_symbols)} display options and OI for "
                            f"{len(full_chain)} full-expiry contracts..."
                        )
                        snapshot = fetch_option_data(
                            ws, option_symbols, wait_seconds=20, oi_symbols=full_chain,
                            session_guard=guard,
                        )
                    finally:
                        ws.close()
                    max_pain = (
                        {"reason": chain_error} if chain_error
                        else calculate_max_pain(full_chain, snapshot)
                    )
                    option_data = {
                        option: snapshot[option] for option in option_symbols if option in snapshot
                    }

                    # Calculate GEX
                    calc = GEXCalculator()
                    calc.update_spot_price(price)

                    for symbol_name, data in option_data.items():
                        if "gamma" in data and "oi" in data:
                            gamma = data["gamma"]
                            oi = data["oi"]
                            if gamma is not None and oi is not None:
                                calc.update_gamma(symbol_name, gamma, oi)

                    capture_session = market_state(symbol, option_prefix, expiration)
                    if capture_session.allowed:
                        capture = make_gex_capture(
                            dict(symbol=symbol, root=option_prefix, strikes_up=strikes_up,
                                 strikes_down=strikes_down, increment=increment),
                            expiration, capture_session.session_date, market_prices, option_data,
                            option_symbols, int(time.time() * 1000), "Dashboard fetch",
                        )
                        with HistoryStore() as store:
                            store.save_gex_capture(capture)

                    # Store in session state
                    st.session_state.gex_calculator = calc
                    st.session_state.option_data = option_data
                    st.session_state.data_fetched = True
                    st.session_state.underlying_price = price
                    st.session_state.price_is_fallback = price_is_fallback
                    st.session_state.market_prices = market_prices
                    st.session_state.max_pain = max_pain
                    st.session_state.symbol = symbol
                    st.session_state.expiration = expiration
                    st.session_state.option_count = len(option_data)
                    st.session_state.last_fetch_time = time.time()

                    greeks_count = sum(1 for d in option_data.values() if "gamma" in d)
                    oi_count = sum(1 for d in option_data.values() if "oi" in d)
                    volume_count = sum(1 for d in option_data.values() if "volume" in d)

                    st.success(f"✅ Data fetched! Greeks: {greeks_count}, OI: {oi_count}")
                    st.rerun()

                except Exception as e:
                    st.error(f"❌ Error: {e}")
                    import traceback
                    st.code(traceback.format_exc())

        st.divider()

        if st.session_state.data_fetched:
            st.metric(f"{st.session_state.symbol} Price", f"${st.session_state.underlying_price:,.2f}")
            st.caption(f"Options: {st.session_state.option_count}")

            # Show last fetch time
            if st.session_state.last_fetch_time > 0:
                elapsed = time.time() - st.session_state.last_fetch_time
                st.caption(f"⏱️ Last fetch: {int(elapsed)}s ago")

                if st.session_state.auto_refresh and schedule.allowed:
                    next_refresh = max(0, refresh_interval - elapsed)

                    # Simple countdown display with color coding
                    if next_refresh <= 5:
                        # About to refresh - red warning
                        st.error(f"🔄 **REFRESHING IN {int(next_refresh)}s**")
                    elif next_refresh <= 15:
                        # Getting close - yellow warning
                        st.warning(f"🔄 Next refresh: **{int(next_refresh)}s**")
                    else:
                        # Plenty of time - green info
                        st.success(f"🔄 Next refresh: **{int(next_refresh)}s**")

                    # Progress bar showing time remaining
                    progress = (refresh_interval - elapsed) / refresh_interval
                    st.progress(max(0.0, min(1.0, progress)))

    # Main display
    matching_symbol = st.session_state.get("symbol") == symbol
    render_market_sentiment(
        symbol, st.session_state.get("market_prices", {}) if matching_symbol else {},
        st.session_state.get("price_is_fallback", False),
    )
    render_intraday_gex(
        dict(symbol=symbol, root=option_prefix, strikes_up=strikes_up,
             strikes_down=strikes_down, increment=increment),
        expiration, schedule.session_date or datetime.now(ZoneInfo("America/New_York")).date(),
    )
    if not st.session_state.data_fetched:
        st.info("👈 Configure settings and click 'Fetch Data' to load GEX data")
        render_snapshot_history(
            symbol, option_prefix, expiration, strikes_up, strikes_down, increment,
            expiration_date == default_exp,
        )
        auto_refresh_tick()
        return

    # Get GEX data
    calc = st.session_state.gex_calculator
    gex_df = calc.get_gex_by_strike()
    metrics = calc.get_total_gex_metrics()

    render_snapshot_history(
        symbol, option_prefix, expiration, strikes_up, strikes_down, increment,
        expiration_date == default_exp,
    )
    render_wall_heatmap(
        st.session_state.option_data,
        st.session_state.underlying_price,
        st.session_state.symbol,
        st.session_state.expiration,
        st.session_state.get("max_pain"),
        st.session_state.get("price_is_fallback", False),
        st.session_state.get("market_prices"),
    )
    if gex_df.empty:
        st.warning("⚠️ No GEX data available. Try fetching again or check different expiration.")
        auto_refresh_tick()
        return

    # Display
    col1, col2 = st.columns([2, 1])

    with col1:
        # GEX View Selector
        gex_view = st.radio(
            "GEX View",
            ["Calls vs Puts", "Net GEX", "Absolute GEX"],
            index=["Calls vs Puts", "Net GEX", "Absolute GEX"].index(st.session_state.gex_view),
            key="gex_view_radio",
            horizontal=True,
            help="Calls vs Puts: Separate bars | Net GEX: Call-Put | Absolute GEX: |Net| magnitude"
        )
        st.session_state.gex_view = gex_view

        # Create chart based on selected view
        fig = go.Figure()

        if gex_view == "Calls vs Puts":
            # Original view: Calls (green) vs Puts (red)
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=gex_df['call_gex'],
                name='Call GEX',
                marker_color='green'
            ))
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=-gex_df['put_gex'],
                name='Put GEX',
                marker_color='red'
            ))
            barmode = 'relative'
            yaxis_title = 'Gamma Exposure ($)'

        elif gex_view == "Net GEX":
            # Net GEX: Call - Put (can be positive or negative)
            colors = ['green' if x >= 0 else 'red' for x in gex_df['net_gex']]
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=gex_df['net_gex'],
                name='Net GEX',
                marker_color=colors
            ))
            barmode = 'group'
            yaxis_title = 'Net GEX ($) - Green=Call Heavy, Red=Put Heavy'

        else:  # Absolute GEX
            # Absolute Net GEX: |Call - Put| (always positive)
            abs_gex = abs(gex_df['net_gex'])
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=abs_gex,
                name='|Net GEX|',
                marker_color='blue'
            ))
            barmode = 'group'
            yaxis_title = 'Absolute Net GEX ($)'

        # Add vertical line at underlying price
        fig.add_vline(
            x=st.session_state.underlying_price,
            line_dash="dash",
            line_color="orange",
            line_width=2,
            annotation_text=f"${st.session_state.underlying_price:,.2f}",
            annotation_position="top"
        )

        # Add vertical line at Zero Gamma level (Gamma Flip)
        if metrics.get('zero_gamma'):
            zero_gamma = metrics['zero_gamma']
            fig.add_vline(
                x=zero_gamma,
                line_dash="dot",
                line_color="purple",
                line_width=2,
                annotation_text=f"Zero Γ: ${zero_gamma:,.2f}",
                annotation_position="bottom"
            )

        # Format expiration for display
        exp_display = st.session_state.expiration
        try:
            exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
            exp_display = exp_date.strftime("%m/%d/%Y")
        except:
            pass

        fig.update_layout(
            title=f'{st.session_state.symbol} Gamma Exposure by Strike - {gex_view} (Exp: {exp_display})',
            xaxis_title='Strike Price',
            yaxis_title=yaxis_title,
            barmode=barmode,
            template='plotly_white',
            height=500
        )

        st.plotly_chart(fig, width='stretch')

    with col2:
        st.subheader("📈 Total GEX")

        st.metric("Total Call GEX", f"${metrics['total_call_gex']:,.0f}")
        st.metric("Total Put GEX", f"${metrics['total_put_gex']:,.0f}")
        st.metric("Net GEX", f"${metrics['net_gex']:,.0f}")

        if metrics['max_gex_strike']:
            st.divider()
            st.metric("Max GEX Strike", f"${metrics['max_gex_strike']:,.0f}")

        if metrics.get('zero_gamma'):
            st.divider()
            zero_gamma = metrics['zero_gamma']
            st.metric(
                "Zero Gamma (Flip)",
                f"${zero_gamma:,.2f}",
                help="Strike where Net GEX crosses zero. Dealers long gamma above, short gamma below."
            )

    # Volume and Open Interest Section
    # Aggregate data by strike (used for IV Skew and Volume/OI)
    strike_df = aggregate_by_strike(st.session_state.option_data)

    # IV Skew Section
    if not strike_df.empty and (strike_df['call_iv'].notna().any() or strike_df['put_iv'].notna().any()):
        st.divider()
        st.header("📈 Implied Volatility Skew")

        fig_iv = go.Figure()

        # Plot Call IV
        call_iv_data = strike_df[strike_df['call_iv'].notna()]
        if not call_iv_data.empty:
            fig_iv.add_trace(go.Scatter(
                x=call_iv_data['strike'],
                y=call_iv_data['call_iv'] * 100,  # Convert to percentage
                mode='lines+markers',
                name='Call IV',
                line=dict(color='green', width=2),
                marker=dict(size=6)
            ))

        # Plot Put IV
        put_iv_data = strike_df[strike_df['put_iv'].notna()]
        if not put_iv_data.empty:
            fig_iv.add_trace(go.Scatter(
                x=put_iv_data['strike'],
                y=put_iv_data['put_iv'] * 100,  # Convert to percentage
                mode='lines+markers',
                name='Put IV',
                line=dict(color='red', width=2),
                marker=dict(size=6)
            ))

        # Add vertical line at underlying price
        fig_iv.add_vline(
            x=st.session_state.underlying_price,
            line_dash="dash",
            line_color="orange",
            line_width=2,
            annotation_text=f"${st.session_state.underlying_price:,.2f}",
            annotation_position="top"
        )

        # Format expiration date for display (YYMMDD -> MM/DD/YYYY)
        exp_display = st.session_state.expiration
        try:
            exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
            exp_display = exp_date.strftime("%m/%d/%Y")
        except:
            pass

        fig_iv.update_layout(
            title=f'{st.session_state.symbol} Implied Volatility Skew - Exp: {exp_display}',
            xaxis_title='Strike Price',
            yaxis_title='Implied Volatility (%)',
            template='plotly_white',
            height=400,
            hovermode='x unified'
        )

        st.plotly_chart(fig_iv, width='stretch')

    st.divider()
    st.header("📊 Volume & Open Interest Analysis")

    if not strike_df.empty:
        # Two columns for OI and Volume charts
        col3, col4 = st.columns(2)

        with col3:
            # Open Interest Chart
            fig_oi = go.Figure()
            fig_oi.add_trace(go.Bar(
                x=strike_df['strike'],
                y=strike_df['call_oi'],
                name='Call OI',
                marker_color='green'
            ))
            fig_oi.add_trace(go.Bar(
                x=strike_df['strike'],
                y=-strike_df['put_oi'],
                name='Put OI',
                marker_color='red'
            ))

            # Add vertical line at underlying price
            fig_oi.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            fig_oi.update_layout(
                title='Open Interest by Strike',
                xaxis_title='Strike',
                yaxis_title='Open Interest',
                barmode='relative',
                template='plotly_white',
                height=400
            )
            st.plotly_chart(fig_oi, width='stretch')

        with col4:
            # Volume Chart with toggle
            volume_view = st.radio(
                "Volume View",
                ["Calls vs Puts", "Total Volume"],
                index=["Calls vs Puts", "Total Volume"].index(st.session_state.volume_view),
                key="volume_view_radio",
                horizontal=True,
                help="Switch between separate call/put volume or total volume by strike"
            )
            st.session_state.volume_view = volume_view

            fig_vol = go.Figure()

            if volume_view == "Calls vs Puts":
                # Separate calls and puts
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=strike_df['call_volume'],
                    name='Call Volume',
                    marker_color='lightgreen'
                ))
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=-strike_df['put_volume'],
                    name='Put Volume',
                    marker_color='lightcoral'
                ))
                barmode = 'relative'
            else:  # Total Volume
                # Total volume (calls + puts)
                total_volume = strike_df['call_volume'] + strike_df['put_volume']
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=total_volume,
                    name='Total Volume',
                    marker_color='purple'
                ))
                barmode = 'group'

            # Add vertical line at underlying price
            fig_vol.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            fig_vol.update_layout(
                title=f'Volume by Strike - {volume_view}',
                xaxis_title='Strike',
                yaxis_title='Volume',
                barmode=barmode,
                template='plotly_white',
                height=400
            )
            st.plotly_chart(fig_vol, width='stretch')

        # Top Strikes Table
        st.subheader("🔝 Top Strikes")

        # Create tabs for different views
        tab1, tab2, tab3 = st.tabs(["By Total OI", "By Total Volume", "By Put/Call Ratio"])

        with tab1:
            top_oi = strike_df.nlargest(10, 'total_oi')[['strike', 'call_oi', 'put_oi', 'total_oi']]
            top_oi['strike'] = top_oi['strike'].apply(lambda x: f"${x:,.0f}")
            top_oi.columns = ['Strike', 'Call OI', 'Put OI', 'Total OI']
            st.dataframe(top_oi, hide_index=True, width='stretch')

        with tab2:
            top_vol = strike_df.nlargest(10, 'total_volume')[['strike', 'call_volume', 'put_volume', 'total_volume']]
            top_vol['strike'] = top_vol['strike'].apply(lambda x: f"${x:,.0f}")
            top_vol.columns = ['Strike', 'Call Vol', 'Put Vol', 'Total Vol']
            st.dataframe(top_vol, hide_index=True, width='stretch')

        with tab3:
            # Calculate put/call ratio
            pc_ratio_df = strike_df.copy()
            pc_ratio_df['pc_ratio_oi'] = pc_ratio_df['put_oi'] / pc_ratio_df['call_oi'].replace(0, 1)
            pc_ratio_df['pc_ratio_vol'] = pc_ratio_df['put_volume'] / pc_ratio_df['call_volume'].replace(0, 1)
            top_pc = pc_ratio_df.nlargest(10, 'pc_ratio_oi')[['strike', 'pc_ratio_oi', 'pc_ratio_vol', 'total_oi']]
            top_pc['strike'] = top_pc['strike'].apply(lambda x: f"${x:,.0f}")
            top_pc['pc_ratio_oi'] = top_pc['pc_ratio_oi'].apply(lambda x: f"{x:.2f}")
            top_pc['pc_ratio_vol'] = top_pc['pc_ratio_vol'].apply(lambda x: f"{x:.2f}")
            top_pc.columns = ['Strike', 'P/C Ratio (OI)', 'P/C Ratio (Vol)', 'Total OI']
            st.dataframe(top_pc, hide_index=True, width='stretch')

    auto_refresh_tick()


if __name__ == "__main__":
    main()
