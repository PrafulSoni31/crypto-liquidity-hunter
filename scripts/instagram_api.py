#!/usr/bin/env python3
"""
Instagram Login using Private API
"""
import json
import time
from pathlib import Path
from instagram_private_api import Client

CREDS_FILE = Path.home() / ".clawdbot" / "secrets" / "instagram.json"
SESSION_FILE = Path.home() / ".clawdbot" / "browser-sessions" / "instagram_api.json"

def load_credentials():
    with open(CREDS_FILE) as f:
        return json.load(f)["instagram"]

def save_session(api):
    """Save authenticated session"""
    session_data = {
        "cookies": api.get_cookie_dict(),
        "device_id": api.device_guid,
        "rank_token": api.rank_token,
        "timestamp": time.time()
    }
    
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_FILE, 'w') as f:
        json.dump(session_data, f, indent=2)
    
    print(f"Session saved to {SESSION_FILE}")

def load_session():
    """Load saved session"""
    if not SESSION_FILE.exists():
        return None
    
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except:
        return None

def main():
    print("=" * 50)
    print("Instagram Private API Login")
    print("=" * 50)
    
    creds = load_credentials()
    print(f"Username: {creds['username']}")
    
    # Try loading saved session
    saved_session = load_session()
    
    try:
        if saved_session:
            print("\n[1/2] Trying saved session...")
            # Try to login with cached session
            api = Client(creds['username'], creds['password'], 
                        cookie_dict=saved_session.get('cookies'))
            print("✓ Session valid!")
            user_info = api.user_info(api.user_id)
            print(f"Logged in as: {user_info['user']['username']}")
            return
        
        print("\n[1/2] Performing fresh login...")
        api = Client(creds['username'], creds['password'])
        
        # Save session
        save_session(api)
        
        user_info = api.user_info(api.user_id)
        print(f"✓ Logged in as: {user_info['user']['username']}")
        
    except ClientCompatibilityError as e:
        print(f"Compatibility error: {e}")
    except Exception as e:
        print(f"Login error: {e}")
        
        # Check if 2FA is needed
        if "two-factor" in str(e).lower() or "verification" in str(e).lower():
            print("\n2FA required. Please provide the code.")
            print("You'll need to login manually or disable 2FA.")

if __name__ == "__main__":
    main()
