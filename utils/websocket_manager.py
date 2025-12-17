"""
WebSocket Manager for Tastytrade dxFeed real-time data streaming
Handles connection, authentication, subscription, and data callbacks
"""
import json
import time
import threading
from datetime import datetime
from websocket import create_connection, WebSocketConnectionClosedException

DXFEED_URL = "wss://tasty-openapi-ws.dxfeed.com/realtime"


def get_todays_expiration():
    """Generate today's expiration date in YYMMDD format (e.g., 251027 for Oct 27, 2025)"""
    return datetime.now().strftime("%y%m%d")


def generate_option_symbols(center_price, option_prefix="SPXW", strikes_up=25, strikes_down=25,
                           increment=5, expiration=None):
    """
    Generate option symbols around a center price for any underlying.

    Args:
        center_price (int): Center strike price (e.g., 6000 for SPX, 20000 for NDX)
        option_prefix (str): Option symbol prefix (e.g., "SPXW" for SPX, "NDXP" for NDX)
        strikes_up (int): Number of strikes above center
        strikes_down (int): Number of strikes below center
        increment (int): Strike increment (default 5)
        expiration (str): Expiration date in YYMMDD format. If None, uses today.

    Returns:
        list: List of option symbols (e.g., [".SPXW251214C6000", ".NDXP251214P20000", ...])
    """
    if expiration is None:
        expiration = get_todays_expiration()

    start = (center_price - strikes_down * increment)
    end = (center_price + strikes_up * increment)
    start = (start // increment) * increment
    end = ((end // increment) + 1) * increment

    strikes = []
    current_strike = start
    while current_strike <= end:
        strikes.append(int(current_strike))
        current_strike += increment

    options = []
    for strike in strikes:
        options.append(f".{option_prefix}{expiration}C{strike}")
        options.append(f".{option_prefix}{expiration}P{strike}")

    return options


class OptionsWebSocket:
    """
    Manages WebSocket connection to Tastytrade dxFeed for options data.
    Supports multiple underlyings (SPX, NDX, etc.)
    Runs in background thread and uses callbacks to push data to main application.
    """

    def __init__(self, token, on_data_callback, underlying="SPX", option_prefix="SPXW",
                 expiration=None, strikes_up=25, strikes_down=25, increment=5):
        """
        Initialize WebSocket manager.

        Args:
            token (str): dxFeed streamer token
            on_data_callback (callable): Function called with (msg) when data is received
                                        msg is the parsed JSON message
            underlying (str): Underlying symbol (e.g., "SPX", "NDX")
            option_prefix (str): Option symbol prefix (e.g., "SPXW", "NDXP")
            expiration (str, optional): Expiration date in YYMMDD format
            strikes_up (int): Number of strikes above center (default 25)
            strikes_down (int): Number of strikes below center (default 25)
            increment (int): Strike increment (default 5)
        """
        self.token = token
        self.on_data_callback = on_data_callback
        self.underlying = underlying
        self.option_prefix = option_prefix
        self.expiration = expiration
        self.strikes_up = strikes_up
        self.strikes_down = strikes_down
        self.increment = increment
        self.ws = None
        self.thread = None
        self.running = False
        self.connected = False
        self.underlying_price = None

    def connect(self):
        """
        Establish WebSocket connection and authenticate.

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            print("🔌 Connecting to TastyTrade dxFeed...")
            self.ws = create_connection(DXFEED_URL)

            # SETUP message
            self.ws.send(json.dumps({
                "type": "SETUP",
                "channel": 0,
                "keepaliveTimeout": 60,
                "acceptKeepaliveTimeout": 60,
                "version": "1.0.0"
            }))
            print("→ Sent SETUP")

            # AUTH message
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("type") == "AUTH_STATE":
                    if msg["state"] == "UNAUTHORIZED":
                        self.ws.send(json.dumps({
                            "type": "AUTH",
                            "channel": 0,
                            "token": self.token
                        }))
                        print("→ Sent AUTH")
                    elif msg["state"] == "AUTHORIZED":
                        print("✅ Authorized")
                        break

            # FEED CHANNEL request
            self.ws.send(json.dumps({
                "type": "CHANNEL_REQUEST",
                "channel": 1,
                "service": "FEED",
                "parameters": {"contract": "AUTO"}
            }))
            print("→ Sent CHANNEL_REQUEST")

            # Wait for channel open
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("type") == "CHANNEL_OPENED" and msg.get("channel") == 1:
                    print("✅ Channel opened")
                    break

            self.connected = True
            return True

        except Exception as e:
            print(f"❌ Connection error: {e}")
            self.connected = False
            return False

    def get_underlying_price(self, default_price=6000):
        """
        Fetch current underlying midpoint price.

        Args:
            default_price (int): Default price if fetch fails

        Returns:
            int: Underlying price rounded to nearest 5, or default_price
        """
        if not self.ws:
            return default_price

        try:
            print(f"\n📊 Fetching {self.underlying} midpoint...")
            self.ws.send(json.dumps({
                "type": "FEED_SUBSCRIPTION",
                "channel": 1,
                "add": [{"symbol": self.underlying, "type": "Quote"}]
            }))

            underlying_price = None
            for _ in range(20):
                msg = json.loads(self.ws.recv())
                if msg.get("type") == "FEED_DATA":
                    for data in msg.get("data", []):
                        if data.get("eventSymbol") == self.underlying and data.get("eventType") == "Quote":
                            bid = data.get("bidPrice")
                            ask = data.get("askPrice")
                            if bid and ask:
                                underlying_price = round(((bid + ask) / 2) / 5) * 5
                                print(f"✅ {self.underlying} midpoint ≈ {underlying_price}")
                                break
                if underlying_price:
                    break

            if not underlying_price:
                underlying_price = default_price
                print(f"⚠️ Defaulting to {default_price}")

            self.underlying_price = underlying_price
            return underlying_price

        except Exception as e:
            print(f"❌ Error fetching {self.underlying} price: {e}")
            self.underlying_price = default_price
            return default_price

    def subscribe_to_options(self, center_price=None, expiration=None, strikes_up=25,
                           strikes_down=25, increment=5):
        """
        Subscribe to options data (Quotes, Trades, Greeks, Summary).

        Args:
            center_price (int, optional): Price to center strikes around. If None, fetches current price.
            expiration (str, optional): Expiration date in YYMMDD format. If None, uses today.
            strikes_up (int): Number of strikes above center (default 25)
            strikes_down (int): Number of strikes below center (default 25)
            increment (int): Strike increment (default 5)
        """
        if not self.ws:
            print("❌ Not connected. Cannot subscribe.")
            return

        if center_price is None:
            # Use default based on underlying
            default_prices = {"SPX": 6000, "NDX": 20000, "SPY": 600, "QQQ": 500}
            default_price = default_prices.get(self.underlying, 1000)
            center_price = self.get_underlying_price(default_price)

        try:
            if expiration is None:
                expiration = get_todays_expiration()
            print(f"📅 Using expiration date: {expiration}")

            # Generate option symbols
            options = generate_option_symbols(
                center_price,
                option_prefix=self.option_prefix,
                strikes_up=strikes_up,
                strikes_down=strikes_down,
                increment=increment,
                expiration=expiration
            )
            print(f"Subscribing to {len(options)} {self.option_prefix} options...")

            # Build subscription list
            add_list = []
            for sym in options:
                add_list.append({"symbol": sym, "type": "Quote"})
                add_list.append({"symbol": sym, "type": "Trade"})
                add_list.append({"symbol": sym, "type": "Greeks"})
                add_list.append({"symbol": sym, "type": "Summary"})

            # Also subscribe to underlying quotes for price updates
            add_list.append({"symbol": self.underlying, "type": "Quote"})

            # Send subscription
            self.ws.send(json.dumps({
                "type": "FEED_SUBSCRIPTION",
                "channel": 1,
                "add": add_list
            }))
            print(f"✅ Subscribed to {len(options)} {self.option_prefix} options + {self.underlying} (Quotes, Trades, Greeks, Summary)")

        except Exception as e:
            print(f"❌ Error subscribing to options: {e}")

    def _message_loop(self):
        """
        Internal method: Main message processing loop (runs in background thread).
        """
        reconnect_delay = 5
        max_reconnect_delay = 60

        while self.running:
            try:
                # Connect and subscribe
                if not self.connect():
                    print(f"⏳ Reconnecting in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                    continue

                # Get underlying price and subscribe
                self.subscribe_to_options(
                    expiration=self.expiration,
                    strikes_up=self.strikes_up,
                    strikes_down=self.strikes_down,
                    increment=self.increment
                )

                # Reset reconnect delay on successful connection
                reconnect_delay = 5

                # Message processing loop
                while self.running and self.connected:
                    msg = json.loads(self.ws.recv())
                    msg_type = msg.get("type")

                    if msg_type == "FEED_DATA":
                        # Push data to callback
                        if self.on_data_callback:
                            self.on_data_callback(msg)

                    elif msg_type == "KEEPALIVE":
                        # Respond to keepalive
                        self.ws.send(json.dumps({
                            "type": "KEEPALIVE",
                            "channel": 0
                        }))

            except WebSocketConnectionClosedException:
                print("❌ WebSocket connection closed")
                self.connected = False
                if self.running:
                    print(f"⏳ Reconnecting in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

            except Exception as e:
                print(f"❌ Error in message loop: {e}")
                self.connected = False
                if self.running:
                    print(f"⏳ Reconnecting in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        print("✅ Message loop stopped")

    def start_listening(self):
        """
        Start listening for messages in a background thread.
        Non-blocking - returns immediately.
        """
        if self.running:
            print("⚠️ Already listening")
            return

        self.running = True
        self.thread = threading.Thread(target=self._message_loop, daemon=True)
        self.thread.start()
        print("✅ Started listening in background thread")

    def stop(self):
        """
        Stop listening and close WebSocket connection gracefully.
        """
        print("🛑 Stopping WebSocket...")
        self.running = False
        self.connected = False

        if self.ws:
            try:
                self.ws.close()
            except:
                pass

        if self.thread:
            self.thread.join(timeout=5)

        print("✅ WebSocket stopped")

    def is_connected(self):
        """
        Check if WebSocket is currently connected.

        Returns:
            bool: True if connected, False otherwise
        """
        return self.connected


if __name__ == "__main__":
    """Test WebSocket connection"""
    from utils.auth import ensure_streamer_token

    def on_data(msg):
        """Simple callback that prints received data"""
        for data in msg.get("data", []):
            symbol = data.get("eventSymbol")
            event_type = data.get("eventType")
            if event_type == "Greeks":
                gamma = data.get("gamma")
                print(f"  {symbol}: gamma={gamma}")

    print("Testing WebSocket connection...\n")

    # Get streamer token
    token = ensure_streamer_token()

    # Create WebSocket manager (for SPX)
    ws_manager = OptionsWebSocket(token, on_data, underlying="SPX", option_prefix="SPXW")

    # Start listening
    ws_manager.start_listening()

    # Run for 30 seconds
    try:
        print("\nListening for 30 seconds... Press Ctrl+C to stop")
        time.sleep(30)
    except KeyboardInterrupt:
        print("\n⏸️ Interrupted by user")
    finally:
        ws_manager.stop()

    print("\n✅ Test complete!")
