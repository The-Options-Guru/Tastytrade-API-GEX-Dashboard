"""Sampled intraday OI gamma, using the dashboard's existing exposure convention."""
import json
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go

from utils.exposure_profile import build_exposure_profile
from utils.flow_store import NEW_YORK, finite_number
from utils.gex_calculator import parse_option_symbol


VIEWS = ("Levels", "Strikes", "Throughout day", "Changes")
GREEN, RED = "#98e51b", "#ff606b"
UNITS = "USD per 1% spot move"


def gex_scope(config, expiry):
    return json.dumps([
        config["symbol"], config["root"], expiry,
        int(config["strikes_up"]), int(config["strikes_down"]), float(config["increment"]),
    ])


def make_gex_capture(config, expiry, session_date, prices, options, symbols, at_ms, source):
    selected = {}
    for symbol in symbols:
        contract = parse_option_symbol(symbol)
        if not contract or contract["prefix"] != config["root"] or contract["expiration"] != expiry:
            raise ValueError("GEX capture contains a contract outside the selected root/expiry.")
        selected[symbol] = options.get(symbol, {})
    if not selected:
        raise ValueError("GEX capture requires an expected contract range.")
    spot, price_ms = finite_number(prices.get("last")), finite_number(prices.get("last_time"))
    capture_day = datetime.fromtimestamp(at_ms / 1000, NEW_YORK).date()
    price_day = datetime.fromtimestamp(price_ms / 1000, NEW_YORK).date() if price_ms and price_ms > 0 else None
    valid_price = bool(
        spot and spot > 0 and price_ms and 0 <= at_ms - price_ms <= 120000 and price_day == capture_day
    )
    # Preserve missing-input captures as gaps, never carry a previous profile forward.
    if valid_price:
        profile = build_exposure_profile(selected, spot)
        rows = [
            {key: finite_number(row[key]) for key in ("strike", "call_oi_gex", "put_oi_gex", "net_oi_gex")}
            for row in profile.to_dict("records")
        ]
    else:
        strikes = sorted({parse_option_symbol(symbol)["strike"] for symbol in selected})
        rows = [dict(strike=strike, call_oi_gex=None, put_oi_gex=None, net_oi_gex=None) for strike in strikes]
    return dict(
        scope=gex_scope(config, expiry), session=session_date.isoformat(), at_ms=at_ms,
        symbol=config["symbol"], root=config["root"], expiry=expiry,
        spot=spot if valid_price else None, price_ms=price_ms, source=source, rows=rows,
        quality="Observed trade within 120s at capture" if valid_price else "Underlying trade stale, missing or future; GEX unavailable",
    )


def capture_frame(capture):
    return pd.DataFrame(capture["rows"]).set_index("strike").sort_index()


def gex_changes(latest, baseline):
    if latest["scope"] != baseline["scope"] or latest["session"] != baseline["session"]:
        raise ValueError("GEX changes require the same symbol, root, expiry, range settings and session.")
    if baseline["at_ms"] >= latest["at_ms"]:
        raise ValueError("GEX baseline must precede the latest capture.")
    current = capture_frame(latest)["net_oi_gex"].rename("Latest")
    previous = capture_frame(baseline)["net_oi_gex"].rename("Baseline")
    result = pd.concat([previous, current], axis=1)
    result["Change"] = result["Latest"] - result["Baseline"]
    result["Status"] = result["Change"].map(lambda value: "Comparable" if pd.notna(value) else "Unavailable at one or both captures")
    return result.reset_index()


def top_gex_levels(capture):
    frame = capture_frame(capture).dropna(subset=["net_oi_gex"]).reset_index()
    frame["distance"] = (frame["strike"] - (capture["spot"] or 0)).abs()
    positive = frame[frame.net_oi_gex > 0].sort_values(
        ["net_oi_gex", "distance", "strike"], ascending=[False, True, True],
    ).head(3)
    negative = frame[frame.net_oi_gex < 0].sort_values(
        ["net_oi_gex", "distance", "strike"], ascending=[True, True, True],
    ).head(3)
    return positive, negative


def plot_time(at_ms):
    # Plotly's axis is explicitly ET rather than the browser's local timezone.
    return datetime.fromtimestamp(at_ms / 1000, NEW_YORK).replace(tzinfo=None)


def build_intraday_gex_chart(captures, view, baseline=None):
    if not captures or view not in VIEWS:
        raise ValueError("A known GEX view and at least one capture are required.")
    latest = captures[-1]
    frame = capture_frame(latest)
    figure = go.Figure()
    if view == "Levels":
        times, prices = [], []
        prior = None
        for capture in captures:
            if prior is not None and capture["at_ms"] - prior > 120000:
                times.append(plot_time(prior + 60000))
                prices.append(None)
            times.append(plot_time(capture["at_ms"]))
            prices.append(capture["spot"])
            prior = capture["at_ms"]
        figure.add_trace(go.Scatter(
            x=times, y=prices, mode="lines+markers", name=f"{latest['symbol']} sampled price",
            line=dict(color="#dddddf", width=1.5), marker=dict(size=4), connectgaps=False,
            hovertemplate="%{x|%H:%M:%S} ET<br>Sampled price %{y:,.2f}<extra></extra>",
        ))
        for levels, color, label in zip(top_gex_levels(latest), (GREEN, RED), ("Positive", "Negative")):
            for row in levels.to_dict("records"):
                figure.add_hline(
                    y=row["strike"], line_dash="dash", line_color=color, line_width=1,
                    annotation_text=f"{label} {row['strike']:g}", annotation_position="right",
                    annotation_font_color=color,
                )
        figure.update_layout(yaxis_title="Underlying price / latest net-GEX strikes", xaxis_title="Capture time (ET)")
    elif view == "Throughout day":
        strikes = sorted({row["strike"] for capture in captures for row in capture["rows"]})
        times, columns = [], []
        prior = None
        for capture in captures:
            if prior is not None and capture["at_ms"] - prior > 120000:
                for missing in range(prior + 60000, capture["at_ms"] - 30000, 60000):
                    times.append(plot_time(missing))
                    columns.append([None] * len(strikes))
            values = {row["strike"]: row["net_oi_gex"] for row in capture["rows"]}
            times.append(plot_time(capture["at_ms"]))
            columns.append([values.get(strike) for strike in strikes])
            prior = capture["at_ms"]
        magnitude = max((abs(value) for column in columns for value in column if value is not None), default=1) or 1
        figure.add_trace(go.Heatmap(
            x=times, y=strikes, z=list(map(list, zip(*columns))),
            zmin=-magnitude, zmax=magnitude, zmid=0,
            colorscale=[[0, RED], [0.5, "#22252c"], [1, GREEN]],
            colorbar=dict(title="Net GEX ($)"), hoverongaps=False, connectgaps=False,
            hovertemplate="%{x|%H:%M:%S} ET<br>Strike %{y}<br>Net GEX $%{z:,.0f}<extra></extra>",
        ))
        figure.update_layout(xaxis_title="Capture time (ET); blank = unavailable", yaxis_title="Strike")
    else:
        if view == "Changes":
            if baseline is None:
                raise ValueError("Changes require an earlier capture.")
            change = gex_changes(latest, baseline)
            strikes, values = change["strike"].tolist(), change["Change"].tolist()
        else:
            strikes, values = frame.index.tolist(), frame["net_oi_gex"].tolist()
        figure.add_trace(go.Bar(
            x=strikes, y=values, name="GEX change" if view == "Changes" else "Net GEX",
            marker_color=[GREEN if pd.notna(value) and value >= 0 else RED for value in values],
            hovertemplate="Strike %{x}<br>$%{y:,.0f}<extra></extra>",
        ))
        if latest["spot"] is not None:
            figure.add_vline(x=latest["spot"], line_color="#dddddf", line_dash="dot")
        figure.update_layout(xaxis_title="Strike", yaxis_title=f"{'Change in net GEX' if view == 'Changes' else 'Net GEX'} ({UNITS})")
    figure.update_layout(
        template="plotly_dark", paper_bgcolor="#17191e", plot_bgcolor="#17191e",
        font=dict(color="#dddddf"),
        height=430, margin=dict(l=20, r=110 if view == "Levels" else 30, t=25, b=35),
        legend=dict(orientation="h", y=-0.2), hovermode="closest",
    )
    if view in ("Levels", "Throughout day"):
        figure.update_xaxes(tickformat="%H:%M")
    return figure
