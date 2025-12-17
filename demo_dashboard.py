"""
Demo GEX Dashboard - Uses public dxFeed demo endpoint
NO API CREDENTIALS NEEDED - Data is delayed
SPX only - Perfect for testing and demonstrations
"""
import streamlit as st
import json
import time
from datetime import datetime, timedelta
from websocket import create_connection
import pandas as pd
import plotly.graph_objects as go
from utils.gex_calculator import GEXCalculator

st.set_page_config(page_title="GEX Demo Dashboard", page_icon="🧪", layout="wide")

# Preset symbol configuration
PRESET_SYMBOLS = {
    "SPX": {"option_prefix": "SPXW", "default_price": 6000, "increment": 5},
    "NDX": {"option_prefix": "NDXP", "default_price": 20000, "increment": 25},
    "SPY": {"option_prefix": "SPY", "default_price": 680, "increment": 1},
    "QQQ": {"option_prefix": "QQQ", "default_price": 612, "increment": 1},
    "IWM": {"option_prefix": "IWM", "default_price": 240, "increment": 1},
    "DIA": {"option_prefix": "DIA", "default_price": 450, "increment": 1},
}

DXFEED_URL = "wss://demo.dxfeed.com/dxlink-ws"


def connect_websocket():
    """Connect to dxFeed Demo WebSocket (no authentication needed)"""
    ws = create_connection(DXFEED_URL, timeout=10)

    # SETUP
    ws.send(json.dumps({
        "type": "SETUP",
        "channel": 0,
        "keepaliveTimeout": 60,
        "acceptKeepaliveTimeout": 60,
        "version": "0.1-js/1.0.0"
    }))
    ws.recv()

    # FEED channel (no authentication required for demo)
    ws.send(json.dumps({
        "type": "CHANNEL_REQUEST",
        "channel": 1,
        "service": "FEED",
        "parameters": {"contract": "AUTO"}
    }))
    msg = json.loads(ws.recv())

    return ws


def get_underlying_price(ws, symbol):
    """Get underlying price - tries Trade first (most accurate), falls back to Quote midpoint"""
    ws.send(json.dumps({
        "type": "FEED_SUBSCRIPTION",
        "channel": 1,
        "add": [
            {"symbol": symbol, "type": "Trade"},
            {"symbol": symbol, "type": "Quote"}
        ]
    }))

    trade_price = None
    quote_mid = None
    start = time.time()

    while time.time() - start < 5:
        try:
            ws.settimeout(1)
            msg = json.loads(ws.recv())
            if msg.get("type") == "FEED_DATA":
                for data in msg.get("data", []):
                    if data.get("eventSymbol") == symbol:
                        event_type = data.get("eventType")

                        # Prefer Trade price (last trade)
                        if event_type == "Trade":
                            price = data.get("price")
                            if price:
                                trade_price = float(price)

                        # Fallback: Quote midpoint
                        elif event_type == "Quote":
                            bid = data.get("bidPrice")
                            ask = data.get("askPrice")
                            if bid and ask:
                                try:
                                    quote_mid = (float(bid) + float(ask)) / 2
                                except (ValueError, TypeError):
                                    pass

            # Return Trade price if we have it, otherwise Quote mid
            if trade_price:
                return trade_price
            elif quote_mid:
                return quote_mid

        except:
            continue

    # Return whichever we got
    return trade_price or quote_mid


def generate_option_symbols(center_price, option_prefix, expiration, strikes_up, strikes_down, increment):
    """Generate option symbols around center price"""
    center_strike = round(center_price / increment) * increment
    strikes = []

    for i in range(-strikes_down, strikes_up + 1):
        strike = center_strike + (i * increment)
        strikes.append(strike)

    options = []
    for strike in strikes:
        # Format strike: use int if whole number, else keep decimal
        if strike == int(strike):
            strike_str = str(int(strike))
        else:
            strike_str = str(strike)

        options.append(f".{option_prefix}{expiration}C{strike_str}")
        options.append(f".{option_prefix}{expiration}P{strike_str}")

    return options


def fetch_option_data(ws, symbols, wait_seconds=15):
    """Fetch Greeks, Summary (OI), and Trade (Volume) for options"""
    subscriptions = []
    for symbol in symbols:
        subscriptions.extend([
            {"symbol": symbol, "type": "Greeks"},
            {"symbol": symbol, "type": "Summary"},
            {"symbol": symbol, "type": "Trade"},
        ])

    ws.send(json.dumps({
        "type": "FEED_SUBSCRIPTION",
        "channel": 1,
        "add": subscriptions
    }))

    data = {}
    start = time.time()

    while time.time() - start < wait_seconds:
        try:
            ws.settimeout(0.5)
            msg = json.loads(ws.recv())

            if msg.get("type") == "FEED_DATA":
                for item in msg.get("data", []):
                    symbol = item.get("eventSymbol")
                    event_type = item.get("eventType")

                    if symbol not in data:
                        data[symbol] = {}

                    if event_type == "Greeks":
                        data[symbol]["gamma"] = item.get("gamma")
                        data[symbol]["delta"] = item.get("delta")
                        data[symbol]["iv"] = item.get("volatility")
                    elif event_type == "Summary":
                        data[symbol]["oi"] = item.get("openInterest")
                    elif event_type == "Trade":
                        # Cumulative volume from Trade events
                        data[symbol]["volume"] = item.get("dayVolume", 0)
        except:
            continue

    return data


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


def main():
    st.title("🧪 GEX Demo Dashboard")

    # Warning banner about demo mode
    st.warning("⚠️ **DEMO MODE** - No API required | Data delayed ~15-20min | **Working symbols: SPX, SPY**")

    # Initialize session state
    if 'data_fetched' not in st.session_state:
        st.session_state.data_fetched = False
    if 'gex_calculator' not in st.session_state:
        st.session_state.gex_calculator = None
    if 'option_data' not in st.session_state:
        st.session_state.option_data = {}
    if 'gex_view' not in st.session_state:
        st.session_state.gex_view = "Calls vs Puts"
    if 'volume_view' not in st.session_state:
        st.session_state.volume_view = "Calls vs Puts"

    # Sidebar controls
    with st.sidebar:
        st.header("⚙️ Settings")

        st.info("🧪 **Demo Mode**: SPX & SPY confirmed working. Others may not have data.")

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

        # Default expiration to today's date
        default_exp = datetime.now().strftime("%y%m%d")

        expiration = st.text_input(
            "Expiration (YYMMDD)",
            value=default_exp,
            max_chars=6,
            help="Today's date shown by default. Change to any option expiration (e.g., 251219 for Dec 19, 2025)"
        )

        with st.expander("Strike Range"):
            strikes_up = st.number_input("Strikes above center", min_value=5, max_value=50, value=25)
            strikes_down = st.number_input("Strikes below center", min_value=5, max_value=50, value=25)

        st.divider()

        # Manual fetch button
        fetch_triggered = st.button("🔄 Fetch Data", type="primary", width='stretch')

        if fetch_triggered:
            with st.spinner(f"Fetching {symbol} data..."):
                try:
                    # Connect to demo endpoint (no authentication needed)
                    ws = connect_websocket()

                    # Get underlying price
                    st.info(f"📊 Getting {symbol} price...")
                    price = get_underlying_price(ws, symbol)

                    if not price:
                        price = default_price
                        st.warning(f"⚠️ Using fallback price: ${price}")
                    else:
                        st.success(f"✅ {symbol} Price: ${price:,.2f}")

                    # Generate option symbols
                    option_symbols = generate_option_symbols(
                        price,
                        option_prefix,
                        expiration,
                        strikes_up,
                        strikes_down,
                        increment
                    )

                    st.info(f"📡 Fetching data for {len(option_symbols)} options...")

                    # Fetch option data
                    option_data = fetch_option_data(ws, option_symbols, wait_seconds=20)

                    ws.close()

                    # Calculate GEX
                    calc = GEXCalculator()
                    calc.update_spot_price(price)

                    for symbol_name, data in option_data.items():
                        if "gamma" in data and "oi" in data:
                            gamma = data["gamma"]
                            oi = data["oi"]
                            if gamma is not None and oi is not None:
                                calc.update_gamma(symbol_name, gamma, oi)

                    # Store in session state
                    st.session_state.gex_calculator = calc
                    st.session_state.option_data = option_data
                    st.session_state.data_fetched = True
                    st.session_state.underlying_price = price
                    st.session_state.symbol = symbol
                    st.session_state.expiration = expiration
                    st.session_state.option_count = len(option_data)

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

    # Main display
    if not st.session_state.data_fetched:
        st.info("👈 Click 'Fetch Data' to load demo data (SPX or SPY recommended)")
        return

    # Get GEX data
    calc = st.session_state.gex_calculator
    gex_df = calc.get_gex_by_strike()
    metrics = calc.get_total_gex_metrics()

    if gex_df.empty:
        st.warning("⚠️ No GEX data available. Try fetching again or check different expiration.")
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
            exp_display = exp_date.strftime("%b %d, %Y")
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

        # Format expiration date for display (YYMMDD -> Mon DD, YYYY)
        exp_display = st.session_state.expiration
        try:
            exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
            exp_display = exp_date.strftime("%b %d, %Y")
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


if __name__ == "__main__":
    main()
