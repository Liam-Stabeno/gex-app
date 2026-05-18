import os
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ['SCHWAB_CLIENT_ID']
CLIENT_SECRET = os.environ['SCHWAB_CLIENT_SECRET']
REDIRECT_URI = os.environ['SCHWAB_REDIRECT_URI']

def get_auth_url():
    return (
        f"https://api.schwabapi.com/v1/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
    )

def exchange_code_for_token(auth_code: str) -> dict:
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()

    response = requests.post(
        "https://api.schwabapi.com/v1/oauth/token",
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI
        }
    )
    if not response.ok:
        print(f"\nERROR {response.status_code}: {response.text}")
        response.raise_for_status()
    return response.json()

if __name__ == "__main__":
    print("\n--- Schwab OAuth ---")
    print(f"\nStep 1: Open this URL in your browser:\n\n{get_auth_url()}\n")
    print("Step 2: Log in, approve access, then paste the full redirect URL here:")
    redirect_url = input("> ").strip()

    # Extract code from URL
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(redirect_url)
    code = parse_qs(parsed.query).get('code', [None])[0]

    if not code:
        print("ERROR: Could not find 'code' in URL. Did you paste the full URL?")
        exit(1)

    print("\nExchanging code for access token...")
    tokens = exchange_code_for_token(code)

    print("\nSUCCESS! Tokens received:")
    print(f"  Access Token:  {tokens['access_token'][:40]}...")
    print(f"  Refresh Token: {tokens.get('refresh_token', 'N/A')[:40]}...")
    print(f"  Expires in:    {tokens.get('expires_in')} seconds")

    # Save tokens for next step
    import json
    with open('data/tokens.json', 'w') as f:
        json.dump(tokens, f, indent=2)
    os.makedirs('data', exist_ok=True)
    print("\nTokens saved to data/tokens.json")
