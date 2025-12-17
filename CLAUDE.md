# Options Gamma Exposure Dashboard - Technical Documentation

## Project Overview
Real-time options gamma exposure (GEX) dashboard using Tastytrade API and dxFeed WebSocket for live market data.

**Created:** December 2025
**Status:** Production-ready
**Purpose:** Monitor gamma exposure, volume, and open interest for SPX, NDX, SPY, QQQ, and custom symbols

**Developer Documentation:** [Tastytrade API Getting Started Guide](https://developer.tastytrade.com/getting-started/)

---

## Setup Guide

### Prerequisites

#### 1. Install Python (3.8 or higher)

**Windows:**
1. Download Python from [python.org/downloads](https://www.python.org/downloads/)
2. Run the installer
3. ⚠️ **IMPORTANT**: Check "Add Python to PATH" during installation
4. Click "Install Now"
5. Verify installation:
   ```bash
   python --version
   ```
   Should show: `Python 3.x.x`

**Already have Python?** Verify version:
```bash
python --version
```
Must be 3.8 or higher.

#### 2. Get Tastytrade API Credentials

**Step-by-step process to get your API credentials:**

1. **Log into your Tastytrade account** at [tastytrade.com](https://tastytrade.com)

2. **Navigate to API Settings:**
   - Click your profile/account menu
   - Go to: **Manage → My Profile → API**

3. **Opt into API Access:**
   - Find the "API Access" section
   - Click to **enable/opt-in to API access**
   - Agree to terms if prompted

4. **Copy your credentials:**
   - **Client ID**: Copy and save this
   - **Client Secret**: Click "Show" and copy this
   - ⚠️ **Keep these secure** - treat them like passwords

5. **Create OAuth Application/Grant:**
   - Look for "Create OAuth Application" or "Generate Refresh Token"
   - Click to create a new application/grant
   - Give it a name (e.g., "GEX Dashboard")

6. **Get Refresh Token:**
   - After creating the application, a **Refresh Token** will be displayed
   - ⚠️ **CRITICAL**: This token is **shown only once**!
   - **Copy it immediately** and save securely
   - If you lose it, you'll need to create a new OAuth application

7. **You should now have:**
   - ✅ Client ID
   - ✅ Client Secret
   - ✅ Refresh Token

#### 3. Project Setup

**Clone or download this project**, then:

1. **Install dependencies:**
   ```bash
   cd "C:\Users\user\Desktop\tasty"
   pip install -r requirements.txt
   ```

2. **Create `.env` file** in the project root:
   ```bash
   # Copy the template
   copy .env.example .env
   ```

3. **Edit `.env` file** with your credentials:
   ```
   CLIENT_ID=your_client_id_here
   CLIENT_SECRET=your_client_secret_here
   REFRESH_TOKEN=your_refresh_token_here
   ```

   ⚠️ **Important notes:**
   - **NO quotes** around values
   - **NO spaces** around the `=` sign
   - Replace `your_client_id_here`, etc. with actual values from Tastytrade

   **Example:**
   ```
   CLIENT_ID=upfjfhdudjfudufuf.....
   CLIENT_SECRET=kfugucud.......
   REFRESH_TOKEN=dGFzdHljcududva2Vu...
   ```

4. **Test authentication:**
   ```bash
   python get_access_token.py
   ```

   If successful, you'll see:
   ```
   ✅ Access token obtained! (valid for 900s)
   💾 Token saved to tasty_token.json
   ```

5. **Run the dashboard:**
   ```bash
   start_simple_dashboard.bat
   ```

   Or manually:
   ```bash
   streamlit run simple_dashboard.py
   ```

6. **Open in browser:**
   - Automatically opens at: http://localhost:8501
   - If not, manually navigate to that URL

### Troubleshooting Setup

**"python is not recognized"**
- Python not added to PATH during installation
- Reinstall Python and check "Add Python to PATH"

**"ModuleNotFoundError"**
- Dependencies not installed
- Run: `pip install -r requirements.txt`

**"Missing required environment variables"**
- `.env` file not created or incorrect format
- Check: no quotes, no spaces around `=`
- Verify all three variables are present

**"Failed to get access token" (401)**
- Credentials are incorrect
- Verify you copied Client ID, Secret, and Refresh Token correctly
- Check for extra spaces or characters
- Refresh token may have expired - create new OAuth application in Tastytrade

**"Token expired"**
- Should auto-refresh automatically
- If persistent, delete `tasty_token.json` and `streamer_token.json`, then restart

---

## Active Files

### Core Application
- **`simple_dashboard.py`** - Main Streamlit dashboard (THE DASHBOARD IN USE)
- **`start_simple_dashboard.bat`** - Windows launcher script

### Authentication & Utilities
- **`utils/auth.py`** - Token management and authentication
- **`utils/gex_calculator.py`** - Thread-safe GEX calculations and aggregation
- **`get_access_token.py`** - Standalone OAuth token retrieval script
- **`get_streamer_token.py`** - Standalone streamer token retrieval script

### Configuration
- **`.env`** - API credentials (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
- **`requirements.txt`** - Python dependencies

### Testing
- **`test_live_symbols.py`** - Verify symbols work with live Tastytrade data
- **`test_live_symbols.bat`** - Windows launcher for test script

### Cached Data (auto-generated)
- `tasty_token.json` - Cached access token (15min expiry)
- `streamer_token.txt` - Cached streamer token (longer expiry)

## Architecture

### Data Flow
```
User clicks "Fetch Data"
    ↓
1. Get OAuth access token (from .env or cache)
    ↓
2. Get streamer token for dxFeed
    ↓
3. Connect to WebSocket: wss://tasty-openapi-ws.dxfeed.com/realtime
    ↓
4. Fetch underlying price (Trade or Quote events)
    ↓
5. Generate option symbols around current price
    ↓
6. Subscribe to Greeks, Summary (OI), Trade (Volume)
    ↓
7. Collect data for 15-20 seconds
    ↓
8. Calculate GEX and aggregate by strike
    ↓
9. Display charts and tables
    ↓
10. [Optional] Auto-refresh every 30-300 seconds
```

### Simple Dashboard Approach
- **No background threads** - Direct WebSocket connection per fetch
- **On-demand data** - Fetch when button clicked or auto-refresh triggers
- **Session state** - Maintains data between Streamlit reruns
- **Weekend compatible** - Shows Friday's closing data on weekends

## Dashboard Features

### 1. Symbol Configuration
**Preset Symbols:**
- SPX (SPXW options, $5 increment)
- NDX (NDXP options, $25 increment)
- SPY (SPY options, $1 increment)
- QQQ (QQQ options, $1 increment)
- IWM (IWM options, $1 increment)
- DIA (DIA options, $1 increment)

**Custom Symbol Mode:**
- Enter any underlying symbol (e.g., AAPL, TSLA)
- Specify option prefix (usually same as underlying)
- Set strike increment (0.5 - 100)
- Set fallback price if live price unavailable

### 2. GEX Visualizations

**Three View Modes:**
1. **Calls vs Puts** - Separate green/red bars (call up, put down)
2. **Net GEX** - Single bar per strike (green=calls dominate, red=puts dominate)
3. **Absolute GEX** - Blue bars showing |Net| magnitude only

**GEX Metrics:**
- Total Call GEX
- Total Put GEX
- Net GEX (Call - Put)
- Max GEX Strike (largest |Net GEX|)

### 3. Volume & Open Interest Analysis

**Charts:**
- Open Interest by Strike (calls vs puts)
- Volume by Strike (calls vs puts)

**Top Strikes Tables (3 tabs):**
- By Total OI - Top 10 strikes with most open interest
- By Total Volume - Top 10 strikes with most trading activity
- By Put/Call Ratio - Top 10 bearish sentiment strikes

### 4. Auto-Refresh
- Enable/disable checkbox
- Configurable interval: 30-300 seconds
- Countdown display to next refresh
- Persists view selection across refreshes

## GEX Calculation

### Formula
```
GEX = Gamma × Open Interest × 100 × Spot Price
```

### Max GEX Strike
The strike with the **largest absolute net GEX**:
```python
Net GEX = Call GEX - Put GEX
Max GEX Strike = strike where |Net GEX| is largest
```

**Can be positive or negative:**
- **Positive Net GEX**: Calls dominate (dealers long gamma)
- **Negative Net GEX**: Puts dominate (dealers short gamma)

**Meaning:**
- **Gamma Magnet** - Price level with most hedging activity
- **Support/Resistance** - Market makers concentrate hedging here
- **Price Attraction** - During low volatility, price gravitates toward Max GEX strike

### Example Calculation
```
Strike 6000:
  Gamma: 0.05
  Open Interest: 1000
  Spot Price: $6000

  Call GEX = 0.05 × 1000 × 100 × 6000 = $30,000,000
  Put GEX = 0.04 × 1500 × 100 × 6000 = $36,000,000
  Net GEX = $30M - $36M = -$6M (puts dominate)
  |Net GEX| = $6M
```

## dxFeed WebSocket Protocol

### Connection Sequence
```
1. WebSocket Connect
   → SETUP (keepalive: 60s)
   ← SETUP acknowledgment

2. Authentication
   ← AUTH_STATE (UNAUTHORIZED)
   → AUTH (token: streamer_token)
   ← AUTH_STATE (AUTHORIZED)

3. Channel Setup
   → CHANNEL_REQUEST (service: FEED, contract: AUTO)
   ← CHANNEL_OPENED (channel: 1)

4. Subscribe to Data
   → FEED_SUBSCRIPTION (add: [symbols with event types])
   ← FEED_DATA (continuous stream)
```

### Event Types Used

**Quote** - Bid/ask prices
```json
{
  "eventType": "Quote",
  "eventSymbol": "SPX",
  "bidPrice": 6050.25,
  "askPrice": 6050.50,
  "time": 1702569600000
}
```

**Trade** - Last trade price and volume
```json
{
  "eventType": "Trade",
  "eventSymbol": ".SPXW251219C6000",
  "price": 10.5,
  "dayVolume": 1234
}
```

**Greeks** - Option Greeks and IV
```json
{
  "eventType": "Greeks",
  "eventSymbol": ".SPXW251219C6000",
  "gamma": 0.05,
  "delta": 0.52,
  "theta": -0.35,
  "vega": 0.42,
  "volatility": 0.18
}
```

**Summary** - Open interest and prev close
```json
{
  "eventType": "Summary",
  "eventSymbol": ".SPXW251219C6000",
  "openInterest": 1234,
  "prevClose": 10.25
}
```

## Option Symbol Format

All option symbols use **dot prefix**:
```
.{PREFIX}{YYMMDD}{C|P}{STRIKE}
```

**Examples:**
- `.SPXW251219C6000` - SPX Weekly, Dec 19 2025, Call, $6000 strike
- `.NDXP251219P21000` - NDX PM-settled, Dec 19 2025, Put, $21000 strike
- `.SPY251219C680` - SPY, Dec 19 2025, Call, $680 strike
- `.QQQ251219P612` - QQQ, Dec 19 2025, Put, $612 strike

**Strike Formatting:**
- Integer strikes: `680` (not `680.0`)
- Decimal strikes: `2.5` (for stocks like AAPL)

## Authentication

**Getting API Credentials:** Visit the [Tastytrade Developer Portal](https://developer.tastytrade.com/getting-started/) to create an API application and obtain your CLIENT_ID, CLIENT_SECRET, and REFRESH_TOKEN.

### OAuth Flow (Refresh Token)
```python
# From .env file
CLIENT_ID = "your_client_id"
CLIENT_SECRET = "your_secret"
REFRESH_TOKEN = "your_refresh_token"

# Exchange for access token
POST https://api.tastytrade.com/oauth/token
  grant_type: refresh_token
  refresh_token: REFRESH_TOKEN

# Response
{
  "access_token": "eyklgtdjci...........",
  "expires_in": 900  // 15 minutes
}
```

### Streamer Token Flow
```python
# Get streamer token for dxFeed
GET https://api.tastyworks.com/api-quote-tokens
  Authorization: Bearer {access_token}

# Response
{
  "token": "dnfujfvx............",
  "websocket-url": "wss://tasty-openapi-ws.dxfeed.com/realtime"
}
```

## Key Configuration

### .env File Format
```
CLIENT_ID=your_client_id_here
CLIENT_SECRET=your_secret_here
REFRESH_TOKEN=your_refresh_token_here
```
**No quotes needed!**

### Automatic Token Management
- **Access tokens** (15min expiry) - Auto-refreshed when <60 seconds remaining
- **Streamer tokens** (~20h expiry) - Auto-refreshed when <5 minutes remaining
- **Token files**: `tasty_token.json` and `streamer_token.json` (auto-generated with expiration timestamps)
- **No manual refresh needed** - Tokens refresh automatically in the background

**How it works:**
1. First fetch creates token file with expiration timestamp
2. Subsequent requests check timestamp before using cached token
3. If expired or expiring soon, automatically fetches new token
4. Completely transparent - no user intervention required

### Expiration Date
- **Format**: YYMMDD (e.g., 251219 for December 19, 2025)
- **Default**: Today's date
- **Usage**: Manually change to any option expiration

### Strike Range
- **Strikes Above Center**: 5-50 (default: 25)
- **Strikes Below Center**: 5-50 (default: 25)
- Generates strikes around current underlying price

## Running the Dashboard

### Windows
```bash
start_simple_dashboard.bat
```

### Manual
```bash
cd "C:\Users\user\Desktop\tasty"
streamlit run simple_dashboard.py
```

Opens at: http://localhost:8501

## Troubleshooting

### Common Issues

**1. Token Errors (401)**
- **Automatic refresh** - Tokens now refresh automatically when expired
- If persistent: Check `.env` file has correct credentials (no quotes)
- Verify REFRESH_TOKEN is still valid in your Tastytrade account
- Manual refresh (if needed): `python get_access_token.py` or `python get_streamer_token.py`

**2. No Data on Weekends**
- Expected behavior - shows Friday's closing data
- Greeks/OI may be stale
- Real-time updates: Mon-Fri 9:30 AM - 4:00 PM ET

**3. Symbol Format Errors**
- All options must have dot prefix (`.SPY`, not `SPY`)
- Integer strikes for whole numbers (`680`, not `680.0`)
- Check expiration format (YYMMDD, exactly 6 digits)

**4. Volume = 0**
- Normal on weekends (no trading)
- During market hours, volume accumulates

**5. NaN or Missing Data**
- Some symbols may not have Greeks/OI immediately
- Wait 15-20 seconds for full data collection
- Check symbol exists and has options for that expiration

## Testing

### Verify Symbols Work
```bash
test_live_symbols.bat
```

Tests SPX, NDX, SPY, QQQ with live Tastytrade connection and shows:
- Underlying Quote (bid/ask/mid)
- Option Quotes (6/6 expected)
- Greeks (6/6 expected)
- Summary/OI (6/6 expected)

### Manual Token Test
```bash
python get_access_token.py
python get_streamer_token.py
```

## Performance Notes

- **Fetch time**: 15-20 seconds per refresh
- **Data points**: ~100 option symbols per fetch (50 calls + 50 puts)
- **Auto-refresh**: Recommended 60+ seconds to avoid rate limits
- **Weekend data**: Instant (cached), no fresh quotes
- **Market hours**: Real-time streaming data

## Version History

**December 14, 2024**
- Switched from background thread to simple fetch approach
- Added volume and open interest analysis
- Added three GEX view modes (Calls vs Puts, Net, Absolute)
- Added top strikes tables with P/C ratio
- Added custom symbol support
- Fixed Net GEX calculation (Call - Put, not Call + Put)
- Fixed strike formatting for integer strikes
- Added auto-refresh with countdown
- Default expiration changed to today's date
- Persisted GEX view selection across refreshes

**Key Improvements:**
- More reliable (no threading issues)
- Works on weekends
- Cleaner UI
- Better data visualization
- Support for any symbol, not just presets
