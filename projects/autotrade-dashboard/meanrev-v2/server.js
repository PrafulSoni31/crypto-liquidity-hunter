/**
 * MeanRev AutoTrader v2 — Node.js Server
 * Proxies: Binance, Delta Exchange India, Coinbase, CoinDCX
 */
require('dotenv').config();
const express     = require('express');
const helmet      = require('helmet');
const compression = require('compression');
const cors        = require('cors');
const morgan      = require('morgan');
const https       = require('https');
const path        = require('path');
const crypto      = require('crypto');
const engine      = require('./engine');

const app  = express();
const PORT = process.env.PORT || 3000;
const HOST = process.env.HOST || '0.0.0.0';

// ── MIDDLEWARE ──
// Relaxed CSP — allow all connections so the dashboard works from any browser
app.use(helmet({
  contentSecurityPolicy: false,  // disable CSP — app uses proxy routes + WS streams
  crossOriginEmbedderPolicy: false,
}));
app.use(compression());
app.use(cors());
app.use(morgan('combined'));
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// ── STATIC ──
app.use(express.static(path.join(__dirname, 'public'), { maxAge:'1h', etag:true }));

// ─────────────────────────────────────
//  GENERIC PROXY HELPER
// ─────────────────────────────────────
function proxyReq(method, url, headers, body, res) {
  const urlObj = new URL(url);
  const opts = {
    hostname: urlObj.hostname,
    path:     urlObj.pathname + urlObj.search,
    method,
    headers: { 'User-Agent':'MeanRevTrader/2.0', ...headers },
  };
  if (body) opts.headers['Content-Length'] = Buffer.byteLength(body);

  const req = https.request(opts, r => {
    res.status(r.statusCode);
    Object.entries(r.headers).forEach(([k,v]) => { try { res.setHeader(k,v); } catch(e){} });
    r.on('data', c => res.write(c));
    r.on('end',  ()  => res.end());
  });
  req.on('error', e => res.status(502).json({ error: e.message }));
  if (body) req.write(body);
  req.end();
}

// ─────────────────────────────────────
//  BINANCE SPOT (public market data)
// ─────────────────────────────────────
app.get('/proxy/binance/spot', (req, res) => {
  const { path: bPath, ...qs } = req.query;
  if (!bPath?.startsWith('/api/')) return res.status(400).json({ error:'invalid path' });
  const q = new URLSearchParams(qs).toString();
  proxyReq('GET', `https://api.binance.com${bPath}${q?'?'+q:''}`, {}, null, res);
});

// ─────────────────────────────────────
//  BINANCE FUTURES (signed)
// ─────────────────────────────────────
app.get('/proxy/binance/futures', (req, res) => {
  const { path: bPath, _net, ...qs } = req.query;
  if (!bPath?.startsWith('/fapi/')) return res.status(400).json({ error:'invalid path' });
  const base = _net==='mainnet'?'https://fapi.binance.com':'https://testnet.binancefuture.com';
  const q = new URLSearchParams(qs).toString();
  proxyReq('GET', `${base}${bPath}${q?'?'+q:''}`, { 'X-MBX-APIKEY': req.headers['x-mbx-apikey']||'' }, null, res);
});

app.post('/proxy/binance/futures', express.text({ type:'*/*' }), (req, res) => {
  const { path: bPath, _net } = req.query;
  if (!bPath?.startsWith('/fapi/')) return res.status(400).json({ error:'invalid path' });
  const base = _net==='mainnet'?'https://fapi.binance.com':'https://testnet.binancefuture.com';
  proxyReq('POST', `${base}${bPath}`, {
    'X-MBX-APIKEY': req.headers['x-mbx-apikey']||'',
    'Content-Type': 'application/x-www-form-urlencoded',
  }, req.body||'', res);
});

// ─────────────────────────────────────
//  DELTA EXCHANGE INDIA
// ─────────────────────────────────────
app.use('/proxy/delta', (req, res) => {
  const p = req.path;
  if (!p.startsWith('/v2/')) return res.status(400).json({ error:'invalid path' });
  const base = 'https://api.india.delta.exchange';
  const q = req.method==='GET' ? '?'+new URLSearchParams(req.query).toString() : '';
  const body = req.method!=='GET' ? JSON.stringify(req.body) : null;
  proxyReq(req.method, `${base}${p}${q}`, {
    'api-key':   req.headers['api-key']||'',
    'signature': req.headers['signature']||'',
    'timestamp': req.headers['timestamp']||'',
    'Content-Type': 'application/json',
  }, body, res);
});

// ─────────────────────────────────────
//  COINBASE ADVANCED TRADE
// ─────────────────────────────────────
app.use('/proxy/coinbase', (req, res) => {
  const p = req.path;
  if (!p.startsWith('/api/v3/')) return res.status(400).json({ error:'invalid path' });
  const body = req.method!=='GET' ? JSON.stringify(req.body) : null;
  proxyReq(req.method, `https://api.coinbase.com${p}`, {
    'CB-ACCESS-KEY':       req.headers['cb-access-key']||'',
    'CB-ACCESS-SIGN':      req.headers['cb-access-sign']||'',
    'CB-ACCESS-TIMESTAMP': req.headers['cb-access-timestamp']||'',
    'CB-ACCESS-PASSPHRASE':req.headers['cb-access-passphrase']||'',
    'Content-Type': 'application/json',
  }, body, res);
});

// ─────────────────────────────────────
//  COINDCX
// ─────────────────────────────────────
app.use('/proxy/coindcx', (req, res) => {
  const p = req.path;
  if (!p.startsWith('/exchange/')) return res.status(400).json({ error:'invalid path' });
  const body = JSON.stringify(req.body);
  proxyReq('POST', `https://api.coindcx.com${p}`, {
    'X-AUTH-APIKEY':    req.headers['x-auth-apikey']||'',
    'X-AUTH-SIGNATURE': req.headers['x-auth-signature']||'',
    'Content-Type': 'application/json',
  }, body, res);
});

// ─────────────────────────────────────
//  SERVER-SIDE HMAC SIGNING
//  Fixes: crypto.subtle unavailable on HTTP (browser restriction)
//  The browser sends {secret, message} → server signs → returns hex signature
// ─────────────────────────────────────
app.post('/sign/hmac', (req, res) => {
  try {
    const { secret, message } = req.body;
    if (!secret || message === undefined)
      return res.status(400).json({ error: 'secret and message required' });
    const sig = crypto.createHmac('sha256', secret).update(message).digest('hex');
    res.json({ signature: sig });
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────────────────────
//  SIGNED BROKER REQUEST (server-side)
//  POST /sign/binance-request
//  Body: { key, secret, network, method, path, params }
//  Server signs and proxies the full request — browser never uses crypto.subtle
// ─────────────────────────────────────
app.post('/sign/binance-request', express.json(), async (req, res) => {
  try {
    const { key, secret, network, method='GET', fpath, params={} } = req.body;
    if (!key || !secret || !fpath)
      return res.status(400).json({ error: 'key, secret, fpath required' });
    if (!fpath.startsWith('/fapi/'))
      return res.status(400).json({ error: 'invalid path' });
    const base = network === 'mainnet'
      ? 'https://fapi.binance.com'
      : 'https://testnet.binancefuture.com';

    const p = { ...params, timestamp: Date.now(), recvWindow: 5000 };
    const qs = Object.entries(p).map(([k,v]) => `${k}=${encodeURIComponent(v)}`).join('&');
    const sig = crypto.createHmac('sha256', secret).update(qs).digest('hex');
    const url = `${base}${fpath}?${qs}&signature=${sig}`;
    const headers = { 'X-MBX-APIKEY': key, 'Content-Type': 'application/x-www-form-urlencoded' };

    // Proxy request
    proxyReq(method, url, headers, method === 'POST' ? qs : null, res);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// ═════════════════════════════════════════
//  ENGINE API — positions persist in backend
//  Works even when browser is closed
// ═════════════════════════════════════════

// GET /api/state — full engine state
// Live mode: balance + PnL from Binance API directly
app.get('/api/state', async (req, res) => {
  const state = engine.getState();

  if (engine.mode === 'live' && engine.brokerCreds.key) {
    try {
      const bal = await engine.binanceRequest('GET', '/fapi/v2/balance');
      if (Array.isArray(bal)) {
        const usdt = bal.find(b => b.asset === 'USDT');
        if (usdt) {
          state.balance          = parseFloat(usdt.balance);
          state.availableBalance = parseFloat(usdt.availableBalance || usdt.balance);
          state.unrealisedPnl    = parseFloat(usdt.crossUnPnl || 0);
          state.realisedPnl      = parseFloat(usdt.realizedProfit || 0);
          state.liveBalance      = true;
        }
      }
      // Get live position count directly from Binance
      const posRisk = await engine.binanceRequest('GET', '/fapi/v2/positionRisk');
      if (Array.isArray(posRisk)) {
        const openCount = posRisk.filter(p => Math.abs(parseFloat(p.positionAmt)) > 0).length;
        state.positions = openCount;  // override with real count
      }
    } catch(e) { /* fallback to engine state */ }
  }
  res.json(state);
});

// GET /api/positions — open positions for current mode
// Live mode: always fetched FRESH from Binance (never from DB)
// Paper mode: from engine memory (persisted in DB)
app.get('/api/positions', async (req, res) => {
  const currentMode = engine.mode;
  let enginePositions;

  if (currentMode === 'live') {
    // Always fetch fresh from Binance — this IS the source of truth
    enginePositions = await engine.getLivePositions();
    // Also update engine memory cache
    engine.positions = [
      ...engine.positions.filter(p => p.mode !== 'live'),
      ...enginePositions,
    ];
  } else {
    enginePositions = engine.positions.filter(p => p.mode === 'paper');
  }

  const positions = enginePositions.map(p => ({
    id:            p.id,
    symbol:        p.symbol,
    side:          p.side,
    avgEntry:      p.avgEntry,
    markPrice:     p.markPrice || engine.prices[p.symbol] || p.avgEntry,
    totalSize:     p.totalSize,
    contracts:     p.contracts,
    entries:       p.entries,
    tpPrice:       p.tpPrice,
    slPrice:       p.slPrice,
    reentryPrice:  p.reentryPrice,
    reentryStep:   p.reentryStep,
    trailPct:      p.trailPct,
    trailPrice:    p.trailPrice,
    openTime:      p.openTime,
    mode:          p.mode,
    unrealisedPnl: p.unrealisedPnl || 0,
    roiPct:        p.roiPct || 0,
    leverage:      p.leverage || 1,
    liquidPrice:   p.liquidPrice || 0,
  }));
  res.json(positions);
});

// GET /api/trades — closed trade history
app.get('/api/trades', (req, res) => {
  const limit = parseInt(req.query.limit) || 100;
  res.json(engine.closedTrades.slice(0, limit));
});

// POST /api/open — open a position from browser or signal
app.post('/api/open', express.json(), async (req, res) => {
  const { symbol, side='SHORT', price, size, opts={} } = req.body;
  if (!symbol) return res.status(400).json({ error: 'symbol required' });
  let entryPrice = price;
  if (!entryPrice) {
    entryPrice = await engine.fetchPrice(symbol);
    if (!entryPrice) return res.status(400).json({ error: 'Could not fetch price' });
  }
  const entrySize = size || engine.cfg.positionSize;

  // If live mode — place real order on Binance first
  if (engine.mode === 'live' && engine.brokerCreds.key) {
    try {
      const sym = symbol.replace('/USDT','USDT').replace('/','');
      const qty = (entrySize / entryPrice).toFixed(3);
      const orderSide = side === 'SHORT' ? 'SELL' : 'BUY';
      const order = await engine.binanceRequest('POST', '/fapi/v1/order', {
        symbol: sym, side: orderSide, type: 'MARKET', quantity: qty
      });
      if (order.code) return res.status(400).json({ error: order.msg || 'Order failed' });
      opts.orderId = order.orderId;
      entryPrice   = parseFloat(order.avgPrice || order.price || entryPrice);
    } catch(e) {
      return res.status(500).json({ error: 'Live order failed: ' + e.message });
    }
  }

  const result = engine.openPosition(symbol, side, entryPrice, entrySize, opts);
  res.json(result);
});

// POST /api/close — close a position
app.post('/api/close', express.json(), async (req, res) => {
  const { id, price, reason='Manual' } = req.body;
  if (!id) return res.status(400).json({ error: 'id required' });
  const pos = engine.positions.find(p => p.id === id);
  if (!pos) return res.status(404).json({ error: 'Position not found' });

  let exitPrice = price;
  if (!exitPrice) exitPrice = await engine.fetchPrice(pos.symbol) || pos.avgEntry;

  // If live mode — place closing order on Binance
  if (engine.mode === 'live' && engine.brokerCreds.key) {
    try {
      const sym = pos.symbol.replace('/USDT','USDT').replace('/','');
      const qty = (pos.totalSize / exitPrice).toFixed(3);
      const closeSide = pos.side === 'SHORT' ? 'BUY' : 'SELL';
      const order = await engine.binanceRequest('POST', '/fapi/v1/order', {
        symbol: sym, side: closeSide, type: 'MARKET',
        quantity: qty, reduceOnly: 'true'
      });
      exitPrice = parseFloat(order.avgPrice || order.price || exitPrice);
    } catch(e) {
      console.error('[Engine] Close order failed:', e.message);
      // Continue to close in DB even if exchange call fails
    }
  }

  const result = engine.closePosition(id, exitPrice, reason);
  res.json(result);
});

// POST /api/set-mode — switch paper/live
app.post('/api/set-mode', express.json(), (req, res) => {
  const { mode } = req.body;
  if (!['paper','live'].includes(mode)) return res.status(400).json({ error: 'mode must be paper or live' });
  engine.setMode(mode);
  res.json({ mode: engine.mode });
});

// POST /api/set-creds — save broker credentials to engine + persist to disk
app.post('/api/set-creds', express.json(), async (req, res) => {
  const { key, secret, network='mainnet', broker='binance', save=true } = req.body;
  if (!key || !secret) return res.status(400).json({ error: 'key and secret required' });
  engine.setCreds(key, secret, network);

  // Persist credentials to disk so they survive restart
  if (save) {
    try {
      const fs = require('fs');
      const credsPath = path.join(__dirname, 'broker_creds.json');
      let allCreds = {};
      if (fs.existsSync(credsPath)) {
        allCreds = JSON.parse(fs.readFileSync(credsPath, 'utf8'));
      }
      allCreds[broker] = { key, secret, network, savedAt: new Date().toISOString() };
      fs.writeFileSync(credsPath, JSON.stringify(allCreds, null, 2), 'utf8');
      fs.chmodSync(credsPath, 0o600); // owner read/write only
    } catch(e) {
      console.error('[Server] Failed to save creds:', e.message);
    }
  }

  // Test connection
  try {
    const bal = await engine.binanceRequest('GET', '/fapi/v2/balance');
    if (Array.isArray(bal)) {
      const usdt = bal.find(b => b.asset === 'USDT');
      const balance = parseFloat(usdt?.balance || 0);
      const unPnl   = parseFloat(usdt?.crossUnPnl || 0);
      res.json({ status: 'connected', balance, unPnl, network });
    } else {
      res.status(400).json({ status: 'error', message: bal.msg || 'Connection failed' });
    }
  } catch(e) {
    res.status(400).json({ status: 'error', message: e.message });
  }
});

// DELETE /api/creds/:broker — remove saved credentials
app.delete('/api/creds/:broker', (req, res) => {
  try {
    const fs = require('fs');
    const credsPath = path.join(__dirname, 'broker_creds.json');
    if (!fs.existsSync(credsPath)) return res.json({ status: 'ok' });
    const allCreds = JSON.parse(fs.readFileSync(credsPath, 'utf8'));
    delete allCreds[req.params.broker];
    fs.writeFileSync(credsPath, JSON.stringify(allCreds, null, 2));
    // Clear from engine too
    if (req.params.broker === 'binance') engine.setCreds('', '', 'mainnet');
    res.json({ status: 'ok', message: 'Credentials deleted' });
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/creds — return saved broker info (keys masked, no secrets)
app.get('/api/creds', (req, res) => {
  try {
    const fs = require('fs');
    const credsPath = path.join(__dirname, 'broker_creds.json');
    if (!fs.existsSync(credsPath)) return res.json({});
    const allCreds = JSON.parse(fs.readFileSync(credsPath, 'utf8'));
    // Mask secrets before sending to browser
    const masked = {};
    for (const [broker, c] of Object.entries(allCreds)) {
      masked[broker] = {
        key:     c.key ? c.key.slice(0,6) + '…' + c.key.slice(-4) : '',
        keyFull: c.key,   // full key needed to pre-fill input
        network: c.network,
        savedAt: c.savedAt,
        hasCreds: !!(c.key && c.secret),
      };
    }
    res.json(masked);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /api/update-cfg — update engine config + persist to disk
app.post('/api/update-cfg', express.json(), (req, res) => {
  const { autoEnabled, autoInterval, autoMaxPos, autoSignal, ...cfgFields } = req.body;
  engine.updateCfg(cfgFields);

  // Also save full settings to disk
  try {
    const fs = require('fs');
    const settingsPath = path.join(__dirname, 'user_settings.json');
    let saved = {};
    if (fs.existsSync(settingsPath)) {
      saved = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    }
    saved.cfg         = engine.cfg;
    saved.autoEnabled = autoEnabled ?? saved.autoEnabled ?? false;
    saved.autoInterval= autoInterval ?? saved.autoInterval ?? 300;
    saved.autoMaxPos  = autoMaxPos   ?? saved.autoMaxPos   ?? 5;
    saved.autoSignal  = autoSignal   ?? saved.autoSignal   ?? 'STRONG';
    saved.savedAt     = new Date().toISOString();
    fs.writeFileSync(settingsPath, JSON.stringify(saved, null, 2));
  } catch(e) {
    console.warn('[Server] Could not save user settings:', e.message);
  }

  res.json({ status: 'ok', cfg: engine.cfg });
});

// GET /api/settings — return persisted user settings
app.get('/api/settings', (req, res) => {
  try {
    const fs = require('fs');
    const settingsPath = path.join(__dirname, 'user_settings.json');
    if (!fs.existsSync(settingsPath)) return res.json({});
    res.json(JSON.parse(fs.readFileSync(settingsPath, 'utf8')));
  } catch(e) {
    res.json({});
  }
});

// GET /api/params — flat list of ALL configurable parameters (Telegram bot + dashboard sync)
app.get('/api/params', (req, res) => {
  const fs = require('fs');
  const settingsPath = path.join(__dirname, 'user_settings.json');
  let settings = {};
  try {
    if (fs.existsSync(settingsPath)) settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  } catch(e) {}
  const cfg = Object.assign({}, engine.cfg, settings.cfg || {});
  res.json({
    // ── Strategy Detection ──
    pumpThreshold:     cfg.pumpThreshold     ?? 20,
    reversalThreshold: cfg.reversalThreshold ?? 5,
    minVolume:         cfg.minVolume         ?? 5e5,
    // ── Position Management ──
    positionSize:      cfg.positionSize      ?? 200,
    takeProfitPct:     cfg.takeProfitPct     ?? 4,
    stopLossPct:       cfg.stopLossPct       ?? 12,
    trailingStopPct:   cfg.trailingStopPct   ?? 0,
    maxAveraging:      cfg.maxAveraging      ?? 3,
    maxPositions:      cfg.maxPositions      ?? 3,
    // ── Risk / DD Protection ──
    maxDailyLoss:      cfg.maxDailyLoss      ?? 300,
    maxAccountDD:      cfg.maxAccountDD      ?? 20,
    // ── Auto-Trading ──
    autoEnabled:       settings.autoEnabled  ?? false,
    autoInterval:      settings.autoInterval ?? 300,
    autoMaxPos:        settings.autoMaxPos   ?? 5,
    autoSignal:        settings.autoSignal   ?? 'STRONG',
    // ── Meta ──
    savedAt:           settings.savedAt      ?? null,
  });
});

// POST /api/params/:param — set a single parameter by name (Telegram bot → dashboard sync)
app.post('/api/params/:param', express.json(), (req, res) => {
  const { param } = req.params;
  const { value }  = req.body;
  if (value === undefined || value === null)
    return res.status(400).json({ error: 'value required' });

  const numericParams = ['pumpThreshold','reversalThreshold','positionSize','takeProfitPct',
    'stopLossPct','trailingStopPct','maxAveraging','maxPositions','maxDailyLoss','maxAccountDD',
    'minVolume','autoInterval','autoMaxPos'];
  const boolParams   = ['autoEnabled'];
  const stringParams = ['autoSignal'];

  let parsed;
  if (numericParams.includes(param)) {
    parsed = parseFloat(value);
    if (isNaN(parsed)) return res.status(400).json({ error: 'numeric value required' });
  } else if (boolParams.includes(param)) {
    parsed = (value === true || value === 'true' || value === '1' || value === 'on');
  } else if (stringParams.includes(param)) {
    parsed = String(value).toUpperCase();
    if (param === 'autoSignal' && !['STRONG','ANY'].includes(parsed))
      return res.status(400).json({ error: 'autoSignal must be STRONG or ANY' });
  } else {
    return res.status(400).json({ error: `unknown param: ${param}` });
  }

  // Split into cfg fields vs auto-trading meta fields
  const cfgKeys = ['pumpThreshold','reversalThreshold','positionSize','takeProfitPct',
    'stopLossPct','trailingStopPct','maxAveraging','maxPositions','maxDailyLoss','maxAccountDD','minVolume'];
  const cfgFields  = {};
  const metaFields = {};
  if (cfgKeys.includes(param)) cfgFields[param]  = parsed;
  else                          metaFields[param] = parsed;

  // Update engine cfg (auto fields are meta-only, engine doesn't hold them)
  if (Object.keys(cfgFields).length) engine.updateCfg(cfgFields);

  // Persist to user_settings.json
  try {
    const fs = require('fs');
    const settingsPath = path.join(__dirname, 'user_settings.json');
    let saved = {};
    if (fs.existsSync(settingsPath)) saved = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    if (!saved.cfg) saved.cfg = {};
    if (Object.keys(cfgFields).length)  Object.assign(saved.cfg, cfgFields);
    if (Object.keys(metaFields).length) Object.assign(saved, metaFields);
    saved.savedAt = new Date().toISOString();
    fs.writeFileSync(settingsPath, JSON.stringify(saved, null, 2));
  } catch(e) {
    console.warn('[Server] Could not persist param:', e.message);
  }

  console.log(`[Params] ${param} = ${parsed}`);
  res.json({ status: 'ok', param, value: parsed });
});

// POST /api/scan_trigger — request immediate scan (Telegram bot → dashboard)
// The browser client polls this and runs a scan when flag is set
let _scanTriggerAt = 0;
app.post('/api/scan_trigger', (req, res) => {
  _scanTriggerAt = Date.now();
  console.log('[Server] Scan triggered via API at', new Date().toISOString());
  res.json({ status: 'ok', message: 'Scan trigger set — browser will scan on next poll', triggeredAt: _scanTriggerAt });
});
app.get('/api/scan_trigger', (req, res) => {
  const since = parseInt(req.query.since || '0');
  res.json({ triggered: _scanTriggerAt > since, triggeredAt: _scanTriggerAt });
});

// GET /api/live-pnl — quick PnL check for all open positions
app.get('/api/live-pnl', async (req, res) => {
  if (!engine.positions.length) return res.json([]);
  const syms = [...new Set(engine.positions.map(p => p.symbol))];
  await engine.fetchPrices(syms);
  const result = engine.positions.map(p => ({
    id: p.id, symbol: p.symbol, side: p.side,
    avgEntry: p.avgEntry, markPrice: engine.prices[p.symbol] || p.avgEntry,
    unrealisedPnl: p.unrealisedPnl || 0,
    mode: p.mode,
  }));
  res.json(result);
});

// ── HEALTH ──
app.get('/health', (req, res) => res.json({
  status:'ok', version:'2.0.0', uptime:Math.floor(process.uptime()), time:new Date().toISOString(),
  brokers:['binance','delta','coinbase','coindcx'],
  engine: { mode: engine.mode, positions: engine.positions.length, running: true },
}));

// ── CATCH-ALL ──
app.get('*', (req, res) => res.sendFile(path.join(__dirname,'public','index.html')));

app.listen(PORT, HOST, () => {
  console.log(`
╔════════════════════════════════════════════╗
║  MeanRev AutoTrader  v2.0.0                ║
║  http://${HOST}:${PORT}                         ║
║  Brokers: Binance · Delta · Coinbase · DCX ║
╚════════════════════════════════════════════╝
`);
  // Auto-load saved broker credentials on startup
  try {
    const fs  = require('fs');
    const credsPath = path.join(__dirname, 'broker_creds.json');
    if (fs.existsSync(credsPath)) {
      const allCreds = JSON.parse(fs.readFileSync(credsPath, 'utf8'));
      if (allCreds.binance?.key && allCreds.binance?.secret) {
        engine.setCreds(allCreds.binance.key, allCreds.binance.secret, allCreds.binance.network || 'mainnet');
        console.log('[Server] Binance credentials auto-loaded from disk ✅');
      }
    }
  } catch(e) {
    console.warn('[Server] Could not auto-load credentials:', e.message);
  }

  // Start backend trading engine (runs 24/7, browser-independent)
  engine.start();
  console.log(`[Server] Engine running — paper/live positions monitored in background`);
});

// Graceful shutdown
process.on('SIGTERM', () => { engine.stop(); process.exit(0); });
process.on('SIGINT',  () => { engine.stop(); process.exit(0); });

module.exports = app;
