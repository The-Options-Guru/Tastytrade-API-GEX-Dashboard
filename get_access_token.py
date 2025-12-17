"""
Get Tastytrade access token using OAuth refresh token flow.
"""
from utils.auth import get_access_token

if __name__ == "__main__":
    try:
        # Get access token (will use .env credentials)
        access_token = get_access_token(force_refresh=True)
        print(f"\n✅ Access token obtained and saved!")
        print(f"📄 Token file: tasty_token.json")
        print(f"⏱️  Token is valid and ready to use")
    except Exception as e:
        print(f"❌ Error: {e}")
