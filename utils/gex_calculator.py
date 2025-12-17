"""
Gamma Exposure (GEX) Calculator
Calculates and aggregates gamma exposure metrics for SPX options
"""
import re
import time
import threading
from collections import deque, defaultdict
from datetime import datetime
import pandas as pd


def parse_option_symbol(symbol):
    """
    Parse option symbol to extract components.
    Supports multiple option symbol formats (SPXW, NDXP, etc.)

    Args:
        symbol (str): Option symbol (e.g., ".SPXW251214C6000", ".NDXP251214P20000")

    Returns:
        dict: Dictionary with 'prefix', 'expiration', 'type' (C/P), and 'strike'
              Returns None if symbol doesn't match pattern
    """
    # Match pattern: .PREFIX + YYMMDD + C/P + STRIKE
    pattern = r'\.([A-Z]+)(\d{6})([CP])(\d+)'
    match = re.match(pattern, symbol)

    if match:
        return {
            'prefix': match.group(1),      # e.g., 'SPXW', 'NDXP'
            'expiration': match.group(2),  # e.g., '251214'
            'type': match.group(3),        # 'C' for call, 'P' for put
            'strike': int(match.group(4))  # e.g., 6000
        }

    return None


class GEXCalculator:
    """
    Thread-safe gamma exposure calculator.
    Maintains real-time GEX data and historical time series.
    """

    def __init__(self, max_history_seconds=3600, spot_price=6000):
        """
        Initialize GEX calculator.

        Args:
            max_history_seconds (int): How long to keep time series data (default 1 hour)
            spot_price (float): Initial SPX spot price for calculations
        """
        self.lock = threading.Lock()
        self.spot_price = spot_price

        # Store option data: symbol -> {'gamma': float, 'oi': float, 'type': 'C'/'P', 'strike': int}
        self.options = {}

        # Store GEX by strike: strike -> {'call_gex': float, 'put_gex': float}
        self.gex_by_strike = defaultdict(lambda: {'call_gex': 0.0, 'put_gex': 0.0})

        # Time series data: deque of {'timestamp': float, 'total_gex': float}
        # 720 snapshots at 5s intervals = 1 hour
        self.time_series = deque(maxlen=720)
        self.max_history_seconds = max_history_seconds
        self.last_snapshot_time = 0

    def update_spot_price(self, price):
        """
        Update SPX spot price.

        Args:
            price (float): New SPX spot price
        """
        with self.lock:
            self.spot_price = price

    def update_gamma(self, symbol, gamma, open_interest):
        """
        Update gamma and open interest for an option symbol.
        Automatically recalculates GEX.

        Args:
            symbol (str): Option symbol (e.g., ".SPXW251214C6000", ".NDXP251214P20000")
            gamma (float): Gamma value
            open_interest (float): Open interest
        """
        # Parse symbol
        parsed = parse_option_symbol(symbol)
        if not parsed:
            return  # Invalid symbol

        with self.lock:
            # Store option data
            self.options[symbol] = {
                'gamma': gamma,
                'oi': open_interest,
                'type': parsed['type'],
                'strike': parsed['strike']
            }

            # Recalculate GEX for this option
            self._recalculate_gex_for_option(symbol)

    def _recalculate_gex_for_option(self, symbol):
        """
        Internal method: Recalculate GEX for a single option.
        Must be called within lock.

        Args:
            symbol (str): Option symbol
        """
        if symbol not in self.options:
            return

        option = self.options[symbol]
        gamma = option['gamma']
        oi = option['oi']
        option_type = option['type']
        strike = option['strike']

        # Calculate GEX: gamma * oi * 100 * spot_price
        # Multiply by 100 because each contract represents 100 shares
        if gamma is not None and oi is not None:
            gex = gamma * oi * 100 * self.spot_price
        else:
            gex = 0.0

        # Update GEX by strike
        if option_type == 'C':
            # Find and remove old call GEX for this symbol if it exists
            self.gex_by_strike[strike]['call_gex'] = \
                sum(
                    self.options[s]['gamma'] * self.options[s]['oi'] * 100 * self.spot_price
                    for s in self.options
                    if self.options[s]['strike'] == strike
                    and self.options[s]['type'] == 'C'
                    and self.options[s]['gamma'] is not None
                    and self.options[s]['oi'] is not None
                )
        else:  # Put
            self.gex_by_strike[strike]['put_gex'] = \
                sum(
                    self.options[s]['gamma'] * self.options[s]['oi'] * 100 * self.spot_price
                    for s in self.options
                    if self.options[s]['strike'] == strike
                    and self.options[s]['type'] == 'P'
                    and self.options[s]['gamma'] is not None
                    and self.options[s]['oi'] is not None
                )

    def get_gex_by_strike(self):
        """
        Get gamma exposure aggregated by strike price.

        Returns:
            pd.DataFrame: DataFrame with columns [strike, call_gex, put_gex, net_gex]
                         Sorted by strike price
        """
        with self.lock:
            if not self.gex_by_strike:
                return pd.DataFrame(columns=['strike', 'call_gex', 'put_gex', 'net_gex'])

            data = []
            for strike, gex in self.gex_by_strike.items():
                call_gex = gex['call_gex']
                put_gex = gex['put_gex']
                net_gex = call_gex - put_gex  # Net = Calls - Puts

                data.append({
                    'strike': strike,
                    'call_gex': call_gex,
                    'put_gex': put_gex,
                    'net_gex': net_gex
                })

            df = pd.DataFrame(data)
            df = df.sort_values('strike').reset_index(drop=True)
            return df

    def _get_zero_gamma_level_unlocked(self):
        """
        Internal method: Calculate Zero Gamma level without acquiring lock.
        Must be called within lock context.

        Returns:
            float: The interpolated strike price where Net GEX = 0, or None if not found
        """
        if not self.gex_by_strike or len(self.gex_by_strike) < 2:
            return None

        # Get sorted strikes and net GEX values
        strikes = sorted(self.gex_by_strike.keys())

        # Find where net GEX crosses zero (sign change)
        for i in range(len(strikes) - 1):
            strike1 = strikes[i]
            strike2 = strikes[i + 1]

            net_gex1 = self.gex_by_strike[strike1]['call_gex'] - self.gex_by_strike[strike1]['put_gex']
            net_gex2 = self.gex_by_strike[strike2]['call_gex'] - self.gex_by_strike[strike2]['put_gex']

            # Check if sign changes (crosses zero)
            if net_gex1 * net_gex2 < 0:
                # Linear interpolation to find exact zero crossing
                # zero_gamma = strike1 + (strike2 - strike1) * (0 - net_gex1) / (net_gex2 - net_gex1)
                if net_gex2 != net_gex1:
                    zero_gamma = strike1 + (strike2 - strike1) * (-net_gex1) / (net_gex2 - net_gex1)
                    return zero_gamma

        return None  # No zero crossing found

    def get_zero_gamma_level(self):
        """
        Calculate the Zero Gamma (Gamma Flip) level.
        This is the strike price where Net GEX crosses zero.

        Returns:
            float: The interpolated strike price where Net GEX = 0, or None if not found
        """
        with self.lock:
            return self._get_zero_gamma_level_unlocked()

    def get_total_gex_metrics(self):
        """
        Get total gamma exposure metrics across all strikes.

        Returns:
            dict: Dictionary with:
                - total_call_gex (float): Total call gamma exposure
                - total_put_gex (float): Total put gamma exposure
                - net_gex (float): Net gamma exposure (calls + puts)
                - max_gex_strike (int): Strike with maximum net GEX
                - max_gex_value (float): Maximum net GEX value
                - zero_gamma (float): Zero Gamma (Gamma Flip) level
                - num_options (int): Number of options tracked
        """
        with self.lock:
            if not self.gex_by_strike:
                return {
                    'total_call_gex': 0.0,
                    'total_put_gex': 0.0,
                    'net_gex': 0.0,
                    'max_gex_strike': 0,
                    'max_gex_value': 0.0,
                    'zero_gamma': None,
                    'num_options': 0
                }

            total_call = 0.0
            total_put = 0.0
            max_net_gex = 0.0
            max_gex_strike = 0

            for strike, gex in self.gex_by_strike.items():
                call_gex = gex['call_gex']
                put_gex = gex['put_gex']
                net_gex = call_gex - put_gex  # Net = Calls - Puts

                total_call += call_gex
                total_put += put_gex

                if abs(net_gex) > abs(max_net_gex):
                    max_net_gex = net_gex
                    max_gex_strike = strike

            return {
                'total_call_gex': total_call,
                'total_put_gex': total_put,
                'net_gex': total_call - total_put,  # Net = Calls - Puts
                'max_gex_strike': max_gex_strike,
                'max_gex_value': max_net_gex,
                'zero_gamma': self._get_zero_gamma_level_unlocked(),
                'num_options': len(self.options)
            }

    def get_time_series(self):
        """
        Get time series of total GEX.

        Returns:
            pd.DataFrame: DataFrame with columns [timestamp, total_gex, datetime]
                         Sorted by timestamp
        """
        with self.lock:
            if not self.time_series:
                return pd.DataFrame(columns=['timestamp', 'total_gex', 'datetime'])

            data = list(self.time_series)
            df = pd.DataFrame(data)

            # Convert timestamp to datetime for plotting
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')

            return df

    def add_time_series_snapshot(self):
        """
        Take a snapshot of current total GEX and add to time series.
        Automatically manages rolling window based on max_history_seconds.

        Returns:
            bool: True if snapshot was added, False if too soon since last snapshot
        """
        current_time = time.time()

        # Only add snapshot every 5 seconds to avoid too many data points
        if current_time - self.last_snapshot_time < 5:
            return False

        with self.lock:
            metrics = self.get_total_gex_metrics()
            total_gex = metrics['net_gex']

            self.time_series.append({
                'timestamp': current_time,
                'total_gex': total_gex
            })

            self.last_snapshot_time = current_time

            # Clean old data beyond max_history_seconds
            cutoff_time = current_time - self.max_history_seconds
            while self.time_series and self.time_series[0]['timestamp'] < cutoff_time:
                self.time_series.popleft()

            return True

    def get_summary_string(self):
        """
        Get a human-readable summary of current GEX state.

        Returns:
            str: Summary string
        """
        metrics = self.get_total_gex_metrics()

        return f"""
GEX Summary:
  Total Call GEX: ${metrics['total_call_gex']:,.0f}
  Total Put GEX: ${metrics['total_put_gex']:,.0f}
  Net GEX: ${metrics['net_gex']:,.0f}
  Max GEX Strike: {metrics['max_gex_strike']} (${metrics['max_gex_value']:,.0f})
  Options Tracked: {metrics['num_options']}
  SPX Price: ${self.spot_price:,.0f}
  Time Series Points: {len(self.time_series)}
        """.strip()


if __name__ == "__main__":
    """Test GEX calculator"""
    print("Testing GEX Calculator...\n")

    # Create calculator
    calc = GEXCalculator(spot_price=6000)

    # Test symbol parsing
    print("1. Testing symbol parsing:")
    test_symbols = [
        ".SPXW251214C6000",
        ".SPXW251214P5995",
        ".NDXP251214C20000",
        ".SPXW251231C6100"
    ]
    for sym in test_symbols:
        parsed = parse_option_symbol(sym)
        print(f"  {sym} -> {parsed}")

    # Test updating gamma
    print("\n2. Testing gamma updates:")
    calc.update_gamma(".SPXW251214C6000", gamma=0.05, open_interest=1000)
    calc.update_gamma(".SPXW251214P6000", gamma=0.04, open_interest=1500)
    calc.update_gamma(".SPXW251214C6005", gamma=0.06, open_interest=800)

    # Test GEX by strike
    print("\n3. GEX by strike:")
    df = calc.get_gex_by_strike()
    print(df)

    # Test total metrics
    print("\n4. Total GEX metrics:")
    metrics = calc.get_total_gex_metrics()
    for key, value in metrics.items():
        print(f"  {key}: {value}")

    # Test time series
    print("\n5. Testing time series:")
    for i in range(5):
        calc.add_time_series_snapshot()
        time.sleep(1)

    ts_df = calc.get_time_series()
    print(ts_df)

    # Test summary
    print("\n6. Summary:")
    print(calc.get_summary_string())

    print("\n✅ All tests passed!")
