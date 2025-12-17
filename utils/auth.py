"""
Authentication utilities for Tastytrade API
Handles OAuth token exchange and dxFeed streamer token retrieval
"""
import os
import sys
import json
import time
import requests
from dotenv import load_dotenv

# Token file paths
TOKEN_FILE = "tasty_token.json"
STREAMER_TOKEN_FILE = "streamer_token.json"

# Load environment variables from .env file
load_dotenv()


def load_credentials_from_env():
    """
    Load Tastytrade API credentials from environment variables.

    Returns:
        dict: Dictionary containing client_id, client_secret, and refresh_token

    Raises:
        ValueError: If any required environment variable is missing
    """
    client_id = os.getenv('CLIENT_ID')
    client_secret = os.getenv('CLIENT_SECRET')
    refresh_token = os.getenv('REFRESH_TOKEN')

    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            "Missing required environment variables. "
            "Please ensure CLIENT_ID, CLIENT_SECRET, and REFRESH_TOKEN are set in .env file"
        )

    return {
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token
    }


def get_access_token(force_refresh=False):
    """
    Get Tastytrade access token using OAuth refresh token flow.
    Caches the token with expiration timestamp for automatic refresh.

    Args:
        force_refresh (bool): If True, always fetch a new token.
                             If False, return cached token if valid and not expiring soon.

    Returns:
        str: Access token

    Raises:
        Exception: If token exchange fails
    """
    # Try to load cached token first
    if not force_refresh and os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                token_data = json.load(f)

                # Check if we have both token and expiration
                if 'access_token' in token_data and 'expires_at' in token_data:
                    expires_at = token_data['expires_at']
                    current_time = time.time()

                    # Refresh if expired or expiring within 60 seconds
                    if expires_at > current_time + 60:
                        time_remaining = int(expires_at - current_time)
                        print(f"✅ Using cached access token (expires in {time_remaining}s)")
                        return token_data['access_token']
                    else:
                        print(f"⚠️ Access token expired or expiring soon, refreshing...")
        except Exception as e:
            print(f"⚠️ Could not load cached token: {e}")

    # Fetch new token
    print("🔄 Fetching new access token from Tastytrade...")
    credentials = load_credentials_from_env()

    data = {
        "grant_type": "refresh_token",
        "refresh_token": credentials['refresh_token'],
        "client_id": credentials['client_id'],
        "client_secret": credentials['client_secret']
    }

    response = requests.post("https://api.tastytrade.com/oauth/token", data=data)

    if response.status_code == 200:
        token_response = response.json()
        access_token = token_response["access_token"]
        expires_in = token_response.get("expires_in", 900)  # Default 15 minutes

        # Calculate expiration timestamp
        expires_at = time.time() + expires_in

        # Save token with expiration
        token_data = {
            "access_token": access_token,
            "expires_in": expires_in,
            "expires_at": expires_at,
            "fetched_at": time.time()
        }

        with open(TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)

        print(f"✅ Access token obtained! (valid for {expires_in}s)")
        print(f"💾 Token saved to {TOKEN_FILE}")

        return access_token
    else:
        raise Exception(
            f"Failed to get access token. Status code: {response.status_code}\n"
            f"Response: {response.text}"
        )


def get_streamer_token(access_token=None, force_refresh=False):
    """
    Get dxFeed streamer token from Tastytrade API.
    Caches the token with expiration timestamp for automatic refresh.

    Args:
        access_token (str, optional): Access token. If not provided, will fetch one.
        force_refresh (bool): If True, always fetch a new token.

    Returns:
        str: dxFeed streamer token

    Raises:
        Exception: If streamer token retrieval fails
    """
    # Try to load cached token first
    if not force_refresh and os.path.exists(STREAMER_TOKEN_FILE):
        try:
            with open(STREAMER_TOKEN_FILE, 'r') as f:
                token_data = json.load(f)

                # Check if we have both token and expiration
                if 'token' in token_data and 'expires_at' in token_data:
                    expires_at = token_data['expires_at']
                    current_time = time.time()

                    # Refresh if expired or expiring within 5 minutes
                    if expires_at > current_time + 300:
                        time_remaining = int((expires_at - current_time) / 3600)
                        print(f"✅ Using cached streamer token (expires in ~{time_remaining}h)")
                        return token_data['token']
                    else:
                        print(f"⚠️ Streamer token expired or expiring soon, refreshing...")
        except Exception as e:
            print(f"⚠️ Could not load cached streamer token: {e}")

    # Get fresh access token if not provided
    if access_token is None:
        access_token = get_access_token()

    print("🔄 Fetching dxFeed streamer token...")
    url = "https://api.tastyworks.com/api-quote-tokens"

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        result = response.json()

        if 'data' in result and 'token' in result['data']:
            streamer_token = result['data']['token']

            # Streamer tokens typically last ~24 hours
            # Using conservative 20 hours to ensure refresh before expiry
            expires_in = 20 * 3600  # 20 hours in seconds
            expires_at = time.time() + expires_in

            # Save token with expiration
            token_data = {
                "token": streamer_token,
                "expires_in": expires_in,
                "expires_at": expires_at,
                "fetched_at": time.time()
            }

            with open(STREAMER_TOKEN_FILE, 'w') as f:
                json.dump(token_data, f, indent=2)

            print(f"✅ Streamer token obtained! (valid for ~{expires_in/3600:.0f}h)")
            print(f"💾 Token saved to '{STREAMER_TOKEN_FILE}'")

            return streamer_token
        else:
            raise Exception(f"Unexpected response format: {result}")
    else:
        raise Exception(
            f"Failed to get streamer token. Status code: {response.status_code}\n"
            f"Response: {response.text}"
        )


def ensure_streamer_token():
    """
    Ensure we have a valid streamer token with automatic expiration checking.
    This is the main function used by the dashboard.

    Returns:
        str: dxFeed streamer token (always valid)
    """
    # get_streamer_token now handles caching and expiration checking automatically
    return get_streamer_token()


if __name__ == "__main__":
    """Test authentication flow"""
    print("Testing authentication flow...\n")

    # Test loading credentials
    try:
        creds = load_credentials_from_env()
        print(f"✅ Credentials loaded successfully\n")
    except Exception as e:
        print(f"❌ Error loading credentials: {e}\n")
        exit(1)

    # Test getting access token
    try:
        access_token = get_access_token()
        print(f"✅ Access token obtained successfully\n")
    except Exception as e:
        print(f"❌ Error getting access token: {e}\n")
        exit(1)

    # Test getting streamer token
    try:
        streamer_token = get_streamer_token(access_token)
        print(f"✅ Streamer token obtained successfully\n")
    except Exception as e:
        print(f"❌ Error getting streamer token: {e}\n")
        exit(1)

    print("✅ All authentication tests passed!")
