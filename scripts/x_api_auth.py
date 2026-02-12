#!/usr/bin/env python3
"""
X API OAuth 2.0 PKCE Authentication Helper

One-time setup: runs a local server to handle the OAuth callback,
opens the browser for user authorization, and saves tokens to disk.

Usage:
    python3 x_api_auth.py --client-id YOUR_CLIENT_ID [--client-secret YOUR_SECRET]

Tokens are saved to ~/.config/x-bookmarks/tokens.json and auto-refreshed.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

TOKEN_DIR = Path.home() / ".config" / "x-bookmarks"
TOKEN_FILE = TOKEN_DIR / "tokens.json"
CONFIG_FILE = TOKEN_DIR / "config.json"

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
REDIRECT_PORT = 8739
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "tweet.read users.read bookmark.read bookmark.write offline.access"


def save_config(client_id: str, client_secret: str = ""):
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    config = {"client_id": client_id}
    if client_secret:
        config["client_secret"] = client_secret
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    print(f"Config saved to {CONFIG_FILE}")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def save_tokens(tokens: dict):
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    os.chmod(TOKEN_FILE, 0o600)
    print(f"Tokens saved to {TOKEN_FILE}")


def load_tokens() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text())


def generate_pkce():
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def exchange_code(code: str, verifier: str, client_id: str, client_secret: str = "") -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
        "client_id": client_id,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    if client_secret:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def refresh_access_token(refresh_token: str, client_id: str, client_secret: str = "") -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    if client_secret:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_valid_token() -> str | None:
    """Get a valid access token, refreshing if needed. Returns None if no tokens."""
    tokens = load_tokens()
    if not tokens:
        return None

    config = load_config()
    if not config.get("client_id"):
        return None

    # Try refreshing
    if tokens.get("refresh_token"):
        try:
            new_tokens = refresh_access_token(
                tokens["refresh_token"],
                config["client_id"],
                config.get("client_secret", ""),
            )
            new_tokens.setdefault("refresh_token", tokens["refresh_token"])
            save_tokens(new_tokens)
            return new_tokens["access_token"]
        except Exception as e:
            print(f"Token refresh failed: {e}", file=sys.stderr)
            return tokens.get("access_token")

    return tokens.get("access_token")


def authorize(client_id: str, client_secret: str = ""):
    """Run the full OAuth 2.0 PKCE authorization flow."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(32)

    auth_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    auth_url = f"{AUTHORIZE_URL}?{auth_params}"
    result = {"code": None, "error": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            if parsed.path == "/callback":
                if params.get("state", [None])[0] != state:
                    result["error"] = "State mismatch"
                elif "error" in params:
                    result["error"] = params["error"][0]
                else:
                    result["code"] = params.get("code", [None])[0]

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                msg = "✅ Authorization successful! You can close this tab." if result["code"] else f"❌ Error: {result['error']}"
                self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass  # Suppress logs

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 120

    print(f"\n🔐 Opening browser for X authorization...")
    print(f"   If it doesn't open, visit:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for callback
    server.handle_request()
    server.server_close()

    if result["error"]:
        print(f"❌ Authorization failed: {result['error']}")
        sys.exit(1)

    if not result["code"]:
        print("❌ No authorization code received (timeout?)")
        sys.exit(1)

    print("🔄 Exchanging code for tokens...")
    tokens = exchange_code(result["code"], verifier, client_id, client_secret)
    save_tokens(tokens)
    save_config(client_id, client_secret)

    print(f"✅ Authenticated! Access token expires in {tokens.get('expires_in', '?')}s")
    if tokens.get("refresh_token"):
        print("   Refresh token saved — will auto-refresh.")
    else:
        print("   ⚠️  No refresh token. Add 'offline.access' scope or re-auth when expired.")


def main():
    parser = argparse.ArgumentParser(description="X API OAuth 2.0 PKCE setup")
    parser.add_argument("--client-id", required=True, help="Your X API Client ID")
    parser.add_argument("--client-secret", default="", help="Client Secret (for confidential apps)")
    parser.add_argument("--refresh", action="store_true", help="Just refresh the existing token")
    parser.add_argument("--print-token", action="store_true", help="Print current access token")

    args = parser.parse_args()

    if args.print_token:
        token = get_valid_token()
        if token:
            print(token)
        else:
            print("No valid token found. Run without --print-token to authorize.", file=sys.stderr)
            sys.exit(1)
        return

    if args.refresh:
        save_config(args.client_id, args.client_secret)
        token = get_valid_token()
        if token:
            print(f"✅ Token refreshed successfully")
        else:
            print("❌ Refresh failed. Re-run without --refresh to re-authorize.")
            sys.exit(1)
        return

    authorize(args.client_id, args.client_secret)


if __name__ == "__main__":
    main()
