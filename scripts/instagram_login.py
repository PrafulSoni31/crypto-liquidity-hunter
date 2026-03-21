#!/usr/bin/env python3
"""
Instagram Automated Login Script using Playwright (Async API)
"""
import json
import time
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SESSION_FILE = Path.home() / ".clawdbot" / "browser-sessions" / "instagram.json"
CREDS_FILE = Path.home() / ".clawdbot" / "secrets" / "instagram.json"

def load_credentials():
    """Load Instagram credentials"""
    with open(CREDS_FILE) as f:
        return json.load(f)["instagram"]

def save_session(context):
    """Save browser session (cookies and storage state)"""
    storage_state = context.storage_state()
    
    session_data = {
        "storage_state": storage_state,
        "timestamp": time.time()
    }
    
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_FILE, 'w') as f:
        json.dump(session_data, f)
    
    print(f"Session saved to {SESSION_FILE}")

def load_session():
    """Load saved session if exists"""
    if not SESSION_FILE.exists():
        return None
    
    try:
        with open(SESSION_FILE) as f:
            session_data = json.load(f)
        
        print("Session loaded successfully")
        return session_data.get("storage_state")
    except Exception as e:
        print(f"Failed to load session: {e}")
        return None

async def login_to_instagram(page, username, password):
    """Automatically login to Instagram"""
    print(f"Logging in as {username}...")
    
    # Navigate to Instagram login page
    await page.goto('https://www.instagram.com/accounts/login/')
    await page.wait_for_load_state('networkidle')
    await asyncio.sleep(3)
    
    # Wait for login form
    await page.wait_for_selector('input[name="username"]', timeout=10000)
    
    # Enter username
    await page.fill('input[name="username"]', username)
    await asyncio.sleep(0.5)
    
    # Enter password
    await page.fill('input[name="password"]', password)
    await asyncio.sleep(0.5)
    
    # Click login button
    await page.click('button[type="submit"]')
    
    # Wait for login to complete
    print("Waiting for login to complete...")
    await asyncio.sleep(8)
    
    # Check if login was successful
    try:
        await page.wait_for_selector('a[href="/"]', timeout=15000)
        print("✓ Login successful!")
        return True
    except:
        # Check for errors
        page_content = await page.content()
        if "Incorrect" in page_content:
            print("✗ Login failed - incorrect credentials")
            return False
        elif "two-factor" in page_content.lower() or "verification" in page_content.lower():
            print("⚠ 2FA required - manual intervention needed")
            return False
        else:
            print("⚠ Login status unclear, saving session anyway")
            return True

async def main():
    """Main automation flow"""
    print("=" * 50)
    print("Instagram Automation Script (Playwright)")
    print("=" * 50)
    
    # Load credentials
    creds = load_credentials()
    print(f"Username: {creds['username']}")
    
    # Try to load existing session
    storage_state = load_session()
    
    async with async_playwright() as p:
        # Launch browser
        print("\n[1/4] Launching browser...")
        
        if storage_state:
            # Use saved session
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()
            
            # Verify session
            print("[2/4] Verifying saved session...")
            await page.goto('https://www.instagram.com/')
            await asyncio.sleep(3)
            
            if await page.query_selector('a[href="/"]'):
                print("✓ Session is valid!")
                await page.screenshot(path='/root/.openclaw/workspace/instagram_logged_in.png')
                print("Screenshot saved to instagram_logged_in.png")
                await browser.close()
                return
            else:
                print("Session expired, logging in again...")
                await browser.close()
        
        # Login fresh
        print("[2/4] No valid session, launching fresh browser...")
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()
        
        # Login
        print("[3/4] Performing automated login...")
        success = await login_to_instagram(page, creds['username'], creds['password'])
        
        if success:
            # Save session
            print("[4/4] Saving session...")
            save_session(context)
            
            # Take screenshot
            await page.screenshot(path='/root/.openclaw/workspace/instagram_logged_in.png')
            print("Screenshot saved!")
        else:
            print("Login failed - check credentials or 2FA")
        
        await browser.close()
    
    print("\nDone!")

if __name__ == "__main__":
    asyncio.run(main())