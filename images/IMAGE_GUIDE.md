# Image Creation Guide

This guide lists all the screenshots you need to create for the README.

## 📋 Required Images

### 1. Dashboard Screenshots (From Your App)

#### `dashboard-overview.png`
- **What:** Full dashboard screenshot showing the entire interface
- **How:** Run the dashboard, load SPX data, capture the full browser window
- **Recommended size:** 1920x1080 or similar widescreen
- **Shows:** Header, sidebar settings, GEX chart, metrics, everything visible

#### `gex-calls-vs-puts.png`
- **What:** GEX chart in "Calls vs Puts" view mode
- **How:** Select "Calls vs Puts" radio button, capture just the GEX chart section
- **Shows:** Green bars (calls) going up, red bars (puts) going down
- **Include:** Chart title, axes, legend, current price line

#### `gex-net-view.png`
- **What:** GEX chart in "Net GEX" view mode
- **How:** Select "Net GEX" radio button, capture the chart
- **Shows:** Single bars per strike (green where calls dominate, red where puts dominate)
- **Include:** Chart with color-coded bars showing net exposure

#### `gex-absolute-view.png`
- **What:** GEX chart in "Absolute GEX" view mode
- **How:** Select "Absolute GEX" radio button, capture the chart
- **Shows:** Blue bars showing magnitude only (all positive)
- **Include:** Chart showing |Net GEX| values

#### `iv-skew.png`
- **What:** Implied Volatility Skew chart
- **How:** Scroll down to the IV Skew section, capture that chart
- **Shows:** Line chart with call IV (green) and put IV (red) across strikes
- **Include:** Chart title, legend, current price vertical line

#### `volume-oi-analysis.png`
- **What:** Volume and Open Interest charts (side by side)
- **How:** Capture the two charts in the Volume & OI Analysis section
- **Shows:** Left chart (OI), right chart (Volume)
- **Include:** Both charts with their legends and data

#### `top-strikes-tables.png`
- **What:** The three tabs of top strikes tables
- **How:** Capture the tables section showing "By Total OI" tab (or all three)
- **Shows:** Tables with strike prices, call/put data, totals
- **Include:** Tab headers and at least one visible table

---

### 2. Tastytrade Setup Screenshots (From Tastytrade Website)

⚠️ **Important:** Use a test account or carefully crop out sensitive information!

#### `tastytrade-api-settings.png`
- **What:** Tastytrade API settings page
- **Where:** Manage → My Profile → API
- **Shows:** The API settings dashboard/landing page
- **Crop:** Just the relevant section showing where to find API settings
- **Blur/Hide:** Any account numbers, real Client IDs/Secrets

#### `api-credentials.png`
- **What:** The Client ID and Client Secret section
- **Where:** Same page as above
- **Shows:** Where users can see/copy their Client ID and Client Secret
- **Blur/Hide:** The actual credential values (replace with "your_client_id_here")
- **Highlight:** The "Show" button for Client Secret

#### `oauth-application.png`
- **What:** Creating a new OAuth application/grant
- **Where:** API settings → Create OAuth Application
- **Shows:** The dialog/form for creating a new application
- **Include:** Name field, any relevant options
- **Blur/Hide:** Existing application names if sensitive

#### `refresh-token.png`
- **What:** The refresh token being displayed after creation
- **Shows:** The token value (BLURRED/REDACTED) with warning that it's shown only once
- **Highlight:** The ⚠️ warning about copying it immediately
- **Blur/Hide:** The actual token value (replace with asterisks or placeholder)

---

## 🎨 Screenshot Tips

### Best Practices
- **Resolution:** Use high DPI/quality (at least 1920px wide)
- **Format:** PNG (supports transparency, better quality than JPG)
- **Cropping:** Crop to relevant content, remove unnecessary whitespace
- **Blur sensitive data:** Use image editor to blur account numbers, real API keys
- **Consistent size:** Try to keep similar screenshots at similar widths

### Tools You Can Use
- **Windows Snipping Tool** (Win + Shift + S)
- **Greenshot** (free, allows editing)
- **ShareX** (free, advanced)
- **Browser DevTools** (F12 → Device Toolbar for consistent sizes)

### For Dashboard Screenshots
1. Run: `start_simple_dashboard.bat`
2. Open: http://localhost:8501
3. Load data for SPX (or any symbol)
4. Switch between different view modes
5. Capture full window or specific sections
6. Save as PNG in the `images/` folder

### For Tastytrade Screenshots
1. Log into your Tastytrade account
2. Navigate to Manage → My Profile → API
3. Take screenshots of each step
4. **IMPORTANT:** Blur/redact all sensitive information:
   - Client ID (show format, not real value)
   - Client Secret (show it's hidden, show "Show" button)
   - Refresh Token (show placeholder/asterisks)
   - Account numbers
5. Save as PNG in the `images/` folder

---

## ✅ Checklist

Once you've created all images, verify:

- [ ] `dashboard-overview.png` - Full dashboard screenshot
- [ ] `gex-calls-vs-puts.png` - Calls vs Puts view
- [ ] `gex-net-view.png` - Net GEX view
- [ ] `gex-absolute-view.png` - Absolute GEX view
- [ ] `iv-skew.png` - IV skew chart
- [ ] `volume-oi-analysis.png` - Volume & OI charts
- [ ] `top-strikes-tables.png` - Top strikes tables
- [ ] `tastytrade-api-settings.png` - API settings page
- [ ] `api-credentials.png` - Client ID/Secret section
- [ ] `oauth-application.png` - OAuth app creation
- [ ] `refresh-token.png` - Refresh token (REDACTED)

**Total: 11 images**

---

## 📦 After Creating Images

1. Save all images in the `images/` folder
2. Verify images display in README:
   ```bash
   # View README locally (if you have a markdown viewer)
   # Or just push to GitHub and check there
   ```
3. Commit and push to GitHub:
   ```bash
   git add images/
   git commit -m "Add dashboard and setup screenshots"
   git push
   ```

---

## 🎯 Optional Enhancements

### Animated Demo (Optional)
Create `demo.gif` showing:
- Clicking "Fetch Data"
- Data loading
- Switching between view modes
- Auto-refresh countdown

**Tools:** ShareX, ScreenToGif, LICEcap

### Comparison Screenshots
Show different symbols side-by-side:
- `spx-vs-spy-comparison.png`
- `custom-symbol-example.png`

---

**Questions?** Just start with the dashboard screenshots - those are the most important!
