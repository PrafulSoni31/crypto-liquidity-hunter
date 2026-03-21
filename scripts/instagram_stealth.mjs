#!/usr/bin/env node
/**
 * Instagram Automated Login - Stealth Version
 * Uses puppeteer-extra with stealth plugin
 */
import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Paths
const SESSION_FILE = path.join(process.env.HOME || '/root', '.clawdbot', 'browser-sessions', 'instagram.json');
const CREDS_FILE = path.join(process.env.HOME || '/root', '.clawdbot', 'secrets', 'instagram.json');

// Enable stealth plugin
puppeteer.use(StealthPlugin());

function loadCredentials() {
    const data = JSON.parse(fs.readFileSync(CREDS_FILE, 'utf8'));
    return data.instagram;
}

function saveSession(context) {
    const sessionData = {
        storage_state: context.storageState(),
        timestamp: Date.now()
    };
    const dir = path.dirname(SESSION_FILE);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    fs.writeFileSync(SESSION_FILE, JSON.stringify(sessionData, null, 2));
    console.log(`Session saved to ${SESSION_FILE}`);
}

function loadSession() {
    if (fs.existsSync(SESSION_FILE)) {
        try {
            const data = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
            console.log('Session loaded successfully');
            return data.storage_state;
        } catch (e) {
            console.log(`Failed to load session: ${e.message}`);
        }
    }
    return null;
}

async function loginToInstagram(page, username, password) {
    console.log(`Logging in as ${username}...`);
    
    // Navigate to Instagram
    await page.goto('https://www.instagram.com/accounts/login/', {
        waitUntil: 'networkidle2',
        timeout: 30000
    });
    
    // Wait for page to settle
    await new Promise(r => setTimeout(r, 3000));
    
    // Check page title
    const title = await page.title();
    console.log(`Page title: ${title}`);
    
    // Try to find username field with multiple selectors
    const usernameSelectors = [
        'input[name="username"]',
        'input[aria-label="Phone number, username, or email"]',
        'input[type="text"]'
    ];
    
    let usernameField = null;
    for (const selector of usernameSelectors) {
        try {
            usernameField = await page.waitForSelector(selector, { timeout: 5000 });
            if (usernameField) {
                console.log(`Found username field: ${selector}`);
                break;
            }
        } catch (e) {
            continue;
        }
    }
    
    if (!usernameField) {
        console.log('Could not find username field, taking screenshot...');
        await page.screenshot({ path: '/root/.openclaw/workspace/debug.png' });
        return false;
    }
    
    // Type username with human-like delay
    await usernameField.type(username, { delay: 100 + Math.random() * 100 });
    await new Promise(r => setTimeout(r, 500 + Math.random() * 500));
    
    // Find password field
    const passwordSelectors = [
        'input[name="password"]',
        'input[type="password"]'
    ];
    
    let passwordField = null;
    for (const selector of passwordSelectors) {
        try {
            passwordField = await page.waitForSelector(selector, { timeout: 5000 });
            if (passwordField) {
                console.log(`Found password field: ${selector}`);
                break;
            }
        } catch (e) {
            continue;
        }
    }
    
    if (!passwordField) {
        console.log('Could not find password field');
        return false;
    }
    
    // Type password with human-like delay
    await passwordField.type(password, { delay: 100 + Math.random() * 100 });
    await new Promise(r => setTimeout(r, 500 + Math.random() * 500));
    
    // Click login button
    const loginButtonSelectors = [
        'button[type="submit"]',
        'button:has-text("Log in")'
    ];
    
    for (const selector of loginButtonSelectors) {
        try {
            const button = await page.waitForSelector(selector, { timeout: 5000 });
            if (button) {
                await button.click();
                console.log('Clicked login button');
                break;
            }
        } catch (e) {
            continue;
        }
    }
    
    // Wait for login to complete
    console.log('Waiting for login...');
    await new Promise(r => setTimeout(r, 8000));
    
    // Check current URL
    const url = page.url();
    console.log(`Current URL: ${url}`);
    
    // Check if logged in
    try {
        await page.waitForSelector('a[href="/"]', { timeout: 10000 });
        console.log('✓ Login successful!');
        return true;
    } catch (e) {
        // Check for errors
        const content = await page.content();
        if (content.includes('Incorrect')) {
            console.log('✗ Login failed - incorrect credentials');
            return false;
        } else if (content.toLowerCase().includes('two-factor') || content.toLowerCase().includes('verification')) {
            console.log('⚠ 2FA required');
            await page.screenshot({ path: '/root/.openclaw/workspace/2fa.png' });
            return false;
        } else {
            console.log('⚠ Login status unclear');
            await page.screenshot({ path: '/root/.openclaw/workspace/status.png' });
            return true;
        }
    }
}

async function main() {
    console.log('='.repeat(50));
    console.log('Instagram Automation - Stealth Mode');
    console.log('='.repeat(50));
    
    const creds = loadCredentials();
    console.log(`Username: ${creds.username}`);
    
    // Try to load existing session
    const storageState = loadSession();
    
    const browser = await puppeteer.launch({
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--window-size=1920,1080'
        ]
    });
    
    if (storageState) {
        console.log('\n[1/4] Verifying saved session...');
        const context = await browser.createIncognitoBrowserContext();
        await context.setStorageState(storageState);
        const page = await context.newPage();
        
        await page.goto('https://www.instagram.com/');
        await new Promise(r => setTimeout(r, 3000));
        
        if (await page.$('a[href="/"]')) {
            console.log('✓ Session is valid!');
            await page.screenshot({ path: '/root/.openclaw/workspace/instagram_logged_in.png' });
            await browser.close();
            return;
        }
        console.log('Session expired, logging in again...');
        await context.close();
    }
    
    console.log('\n[2/4] Launching browser...');
    const page = await browser.newPage();
    
    // Set realistic viewport
    await page.setViewport({ width: 1920, height: 1080 });
    
    console.log('\n[3/4] Logging in...');
    const success = await loginToInstagram(page, creds.username, creds.password);
    
    if (success) {
        console.log('\n[4/4] Saving session...');
        const context = page.browser().defaultBrowserContext();
        saveSession(context);
        
        await page.screenshot({ path: '/root/.openclaw/workspace/instagram_logged_in.png' });
        console.log('Screenshot saved!');
    } else {
        console.log('\nLogin failed - check credentials or 2FA');
    }
    
    await browser.close();
    console.log('\nDone!');
}

main().catch(console.error);
