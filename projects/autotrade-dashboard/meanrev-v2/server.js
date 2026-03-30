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
//  Browser sends {key?, secret?, network, method, fpath, params}.
//  If key/secret omitted → uses saved credentials from broker_creds.json.
//  Browser NEVER needs to hold the secret.
// ─────────────────────────────────────
app.post('/sign/binance-request', express.json(), async (req, res) => {
  try {
    let { key, secret, network, method='GET', fpath, params={} } = req.body;
    if (!fpath) return res.status(400).json({ error: 'fpath required' });
    if (!fpath.startsWith('/fapi/')) return res.status(400).json({ error: 'invalid path' });

    // Fall back to stored credentials if browser doesn't send them
    if (!key || !secret) {
      const fs = require('fs');
      const credsPath = path.join(__dirname, 'broker_creds.json');
      if (fs.existsSync(credsPath)) {
        const saved = JSON.parse(fs.readFileSync(credsPath, 'utf8'));
        key     = key    || saved.binance?.key    || engine.brokerCreds.key;
        secret  = secret || saved.binance?.secret || engine.brokerCreds.secret;
        network = network || saved.binance?.network || 'mainnet';
      }
      // Also try engine in-memory
      if (!secret) secret = engine.brokerCreds.secret;
      if (!key)    key    = engine.brokerCreds.key;
    }
    if (!key || !secret) return res.status(400).json({ error: 'No API credentials available. Save credentials first.' });

    const base = (network || 'mainnet') === 'mainnet'
      ? 'https://fapi.binance.com'
      : 'https://testnet.binancefuture.com';

    const p   = { ...params, timestamp: Date.now(), recvWindow: 5000 };
    const qs  = Object.entries(p).map(([k,v]) => `${k}=${encodeURIComponent(v)}`).join('&');
    const sig = crypto.createHmac('sha256', secret).update(qs).digest('hex');
    const url = `${base}${fpath}?${qs}&signature=${sig}`;
    const headers = { 'X-MBX-APIKEY': key, 'Content-Type': 'application/x-www-form-urlencoded' };
    proxyReq(method, url, headers, method === 'POST' ? qs : null, res);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/test-creds — test stored Binance credentials server-side
// Browser calls this instead of exposing secret in JS
app.get('/api/test-creds', async (req, res) => {
  try {
    if (!engine.brokerCreds.key || !engine.brokerCreds.secret) {
      // Try loading from disk
      const fs = require('fs');
      const credsPath = path.join(__dirname, 'broker_creds.json');
      if (!fs.existsSync(credsPath)) return res.status(400).json({ status: 'error', message: 'No credentials saved' });
      const saved = JSON.parse(fs.readFileSync(credsPath, 'utf8'));
      if (!saved.binance?.key || !saved.binance?.secret)
        return res.status(400).json({ status: 'error', message: 'Credentials incomplete' });
      engine.setCreds(saved.binance.key, saved.binance.secret, saved.binance.network || 'mainnet');
    }
    const bal = await engine.binanceRequest('GET', '/fapi/v2/balance');
    if (!Array.isArray(bal)) return res.status(400).json({ status: 'error', message: bal.msg || 'Auth failed' });
    const usdt    = bal.find(b => b.asset === 'USDT');
    const balance = parseFloat(usdt?.balance || 0);
    res.json({ status: 'connected', balance, network: engine.brokerCreds.network });
  } catch(e) {
    res.status(400).json({ status: 'error', message: e.message });
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

  // If live mode — place entry + SL atomically via Binance batchOrders
  if (engine.mode === 'live' && engine.brokerCreds.key) {
    try {
      const sym       = symbol.replace('/USDT','USDT').replace('/','');
      const entrySide = side === 'SHORT' ? 'SELL' : 'BUY';
      const exitSide  = side === 'SHORT' ? 'BUY'  : 'SELL';

      // Use symbol-specific precision from exchangeInfo (prevents "Precision is over maximum" error)
      const prec     = _symbolPrecision[sym] || {};
      const qtyPrec  = prec.qtyPrecision  ?? 3;   // integer qty for some coins (e.g. ALPACA = 0)
      const stepSize = prec.stepSize      ?? 0.001;
      // Round qty DOWN to stepSize boundary
      const rawQty   = entrySize / entryPrice;
      const qty      = (Math.floor(rawQty / stepSize) * stepSize).toFixed(qtyPrec);

      // Calculate SL price from cfg
      const slPrice = side === 'SHORT'
        ? entryPrice * (1 + engine.cfg.stopLossPct   / 100)   // SHORT: SL above entry
        : entryPrice * (1 - engine.cfg.stopLossPct   / 100);  // LONG:  SL below entry
      const tpPrice = side === 'SHORT'
        ? entryPrice * (1 - engine.cfg.takeProfitPct / 100)
        : entryPrice * (1 + engine.cfg.takeProfitPct / 100);

      // Round to 5 significant figures (handles both BTC $60k and SHIB $0.00001)
      const roundPrice = p => {
        if (p <= 0) return 0;
        const mag = Math.floor(Math.log10(p));
        const factor = Math.pow(10, 5 - mag);
        return Math.round(p * factor) / factor;
      };
      const sl4 = roundPrice(slPrice);
      const tp4 = roundPrice(tpPrice);

      // Build batch: [0] entry MARKET + [1] SL LIMIT reduceOnly + [2] TP LIMIT reduceOnly
      // Using LIMIT orders (not STOP_MARKET) — works on all account types including
      // multi-assets margin where STOP_MARKET is blocked (-4120).
      // Binance processes batchOrders sequentially: entry fills first, then SL/TP
      // reduceOnly orders are valid because the position already exists.
      const batchOrders = [
        { symbol: sym, side: entrySide, type: 'MARKET', quantity: qty },
        { symbol: sym, side: exitSide, type: 'LIMIT',
          quantity: qty, price: String(sl4), reduceOnly: 'true', timeInForce: 'GTC' },
        { symbol: sym, side: exitSide, type: 'LIMIT',
          quantity: qty, price: String(tp4), reduceOnly: 'true', timeInForce: 'GTC' },
      ];

      let batchResult = null;
      try {
        batchResult = await engine.binanceRequest('POST', '/fapi/v1/batchOrders', {
          batchOrders: JSON.stringify(batchOrders)
        });
      } catch(batchErr) {
        console.warn('[Entry] batchOrders failed:', batchErr.message, '— falling back to sequential');
      }

      if (Array.isArray(batchResult) && batchResult[0]?.orderId) {
        // Batch succeeded — atomic entry + SL
        const entryOrder = batchResult[0];
        const slOrder    = batchResult[1] || {};
        const tpOrder    = batchResult[2] || {};
        opts.orderId     = entryOrder.orderId;
        opts.slOrderId   = slOrder.orderId;
        opts.tpOrderId   = tpOrder.orderId;
        entryPrice       = parseFloat(entryOrder.avgPrice || entryOrder.price || entryPrice);
        opts.tpPrice     = tp4;
        opts.slPrice     = sl4;
        console.log(`[Entry] Atomic entry+SL: entry=${entryOrder.orderId} sl=${slOrder.orderId} tp=${tpOrder.orderId} fill=${entryPrice}`);
      } else {
        // Fallback: place entry, then immediately SL
        console.log('[Entry] Placing entry then SL sequentially (batchOrders not supported)');
        const order = await engine.binanceRequest('POST', '/fapi/v1/order', {
          symbol: sym, side: entrySide, type: 'MARKET', quantity: qty
        });
        if (order.code) return res.status(400).json({ error: order.msg || 'Entry order failed' });
        opts.orderId = order.orderId;
        entryPrice   = parseFloat(order.avgPrice || order.price || entryPrice);

        // Place SL immediately after fill — as quickly as possible
        const actualSl = side === 'SHORT'
          ? entryPrice * (1 + engine.cfg.stopLossPct   / 100)
          : entryPrice * (1 - engine.cfg.stopLossPct   / 100);
        const actualTp = side === 'SHORT'
          ? entryPrice * (1 - engine.cfg.takeProfitPct / 100)
          : entryPrice * (1 + engine.cfg.takeProfitPct / 100);

        // Place SL (LIMIT reduceOnly as fallback for accounts that block STOP_MARKET)
        try {
          const slOrder = await engine.binanceRequest('POST', '/fapi/v1/order', {
            symbol: sym, side: exitSide, type: 'LIMIT',
            quantity: qty, price: roundPrice(actualSl).toFixed(4),
            reduceOnly: 'true', timeInForce: 'GTC'
          });
          opts.slOrderId = slOrder.orderId;
          console.log(`[SL] Limit SL placed: ${slOrder.orderId} @ ${roundPrice(actualSl)}`);
        } catch(slErr) {
          console.error('[SL] SL placement failed:', slErr.message);
        }

        // Place TP
        try {
          const tpOrder = await engine.binanceRequest('POST', '/fapi/v1/order', {
            symbol: sym, side: exitSide, type: 'LIMIT',
            quantity: qty, price: roundPrice(actualTp).toFixed(4),
            reduceOnly: 'true', timeInForce: 'GTC'
          });
          opts.tpOrderId = tpOrder.orderId;
          console.log(`[TP] Limit TP placed: ${tpOrder.orderId} @ ${roundPrice(actualTp)}`);
        } catch(tpErr) {
          console.error('[TP] TP placement failed:', tpErr.message);
        }

        opts.slPrice = roundPrice(actualSl);
        opts.tpPrice = roundPrice(actualTp);
      }
    } catch(e) {
      return res.status(500).json({ error: 'Live entry failed: ' + e.message });
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
      const closeSide = pos.side === 'SHORT' ? 'BUY' : 'SELL';
      const cPrec    = _symbolPrecision[sym] || {};
      const cQtyPrec = cPrec.qtyPrecision  ?? 3;
      const cStep    = cPrec.stepSize      ?? 0.001;
      const rawCQty  = pos.contracts || (pos.totalSize / exitPrice);
      const qty      = (Math.floor(rawCQty / cStep) * cStep).toFixed(cQtyPrec);
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

// ── Server-side scan cache (prevents browser from doing heavy Binance fetches) ──
let _scanCache    = null;   // last scan result
let _scanRunning  = false;  // deduplicate concurrent scans
let _scanAutoTimer = null;

// Cache of TRADING perpetuals with their precision info (refreshed every 6h)
let _tradingSymbols = null;   // Set of symbol strings (TRADING perpetuals only)
let _symbolPrecision = {};    // symbol → {qtyPrecision, pricePrecision, stepSize, tickSize}
let _tradingSymsFetchedAt = 0;

async function _refreshTradingSymbols() {
  if (_tradingSymbols && Date.now() - _tradingSymsFetchedAt < 6 * 3600 * 1000) return;
  return new Promise((resolve) => {
    const req2 = https.request({
      hostname: 'fapi.binance.com', path: '/fapi/v1/exchangeInfo',
      method: 'GET', headers: { 'User-Agent': 'MeanRevTrader/2.0' },
    }, r => {
      let data = '';
      r.on('data', c => data += c);
      r.on('end', () => {
        try {
          const d = JSON.parse(data);
          _tradingSymbols = new Set();
          _symbolPrecision = {};
          for (const s of d.symbols) {
            // Only TRADING perpetuals — exclude SETTLING, DELIVERING, BREAK, etc.
            if (s.quoteAsset !== 'USDT') continue;
            if (s.contractType !== 'PERPETUAL') continue;
            if (s.status !== 'TRADING') continue;
            _tradingSymbols.add(s.symbol);
            // Extract precision from filters
            let stepSize = 1, tickSize = 0.01;
            for (const f of s.filters) {
              if (f.filterType === 'LOT_SIZE')    stepSize = parseFloat(f.stepSize);
              if (f.filterType === 'PRICE_FILTER') tickSize = parseFloat(f.tickSize);
            }
            _symbolPrecision[s.symbol] = {
              qtyPrecision:   s.quantityPrecision,
              pricePrecision: s.pricePrecision,
              stepSize,
              tickSize,
            };
          }
          _tradingSymsFetchedAt = Date.now();
          console.log(`[Scan] Trading perpetuals: ${_tradingSymbols.size} USDT-M`);
        } catch(e) { console.error('[Scan] exchangeInfo parse error:', e.message); }
        resolve();
      });
    });
    req2.on('error', () => resolve());
    req2.setTimeout(10000, () => { req2.destroy(); resolve(); });
    req2.end();
  });
}

async function _runScanInternal(pumpThreshold, minVolume, revThreshold) {
  if (_scanRunning) return _scanCache;
  _scanRunning = true;
  const STABLES = new Set(['BUSDUSDT','USDCUSDT','TUSDUSDT','USDTUSDT','DAIUSDT','FDUSDUSDT','USDPUSDT']);
  try {
    // Refresh symbol whitelist (TRADING only, no SETTLING contracts)
    await _refreshTradingSymbols();

    const tickerData = await new Promise((resolve, reject) => {
      const req2 = https.request({
        hostname: 'fapi.binance.com', path: '/fapi/v1/ticker/24hr',
        method: 'GET', headers: { 'User-Agent': 'MeanRevTrader/2.0' },
      }, r => {
        let data = '';
        r.on('data', c => data += c);
        r.on('end', () => { try { resolve(JSON.parse(data)); } catch(e) { reject(e); } });
      });
      req2.on('error', reject);
      req2.setTimeout(10000, () => { req2.destroy(); reject(new Error('Timeout')); });
      req2.end();
    });
    if (!Array.isArray(tickerData)) throw new Error('Bad Binance response');

    // Only count TRADING symbols
    const tradingSet = _tradingSymbols || new Set(tickerData.map(t => t.symbol));
    const totalPairs = tickerData.filter(t =>
      t.symbol.endsWith('USDT') && !STABLES.has(t.symbol) && tradingSet.has(t.symbol)
    ).length;

    const found = tickerData
      .filter(t => t.symbol.endsWith('USDT') && !STABLES.has(t.symbol)
        && tradingSet.has(t.symbol)          // ← exclude SETTLING / non-TRADING
        && parseFloat(t.lastPrice) > 0
        && parseFloat(t.priceChangePercent) >= pumpThreshold
        && parseFloat(t.quoteVolume) >= minVolume)
      .sort((a, b) => parseFloat(b.priceChangePercent) - parseFloat(a.priceChangePercent));

    const results = found.map(t => {
      const pump = parseFloat(t.priceChangePercent);
      const high = parseFloat(t.highPrice), last = parseFloat(t.lastPrice);
      const fromHigh  = high > 0 ? ((last - high) / high) * 100 : 0;
      const hasSignal = fromHigh <= -revThreshold;
      const strength  = !hasSignal ? null : Math.abs(fromHigh) >= revThreshold * 2 ? 'STRONG' : 'WEAK';
      const confidence = Math.round(
        Math.min(40, pump / 2) +
        (hasSignal ? Math.min(40, Math.abs(fromHigh) * 4) : 0) +
        Math.min(20, Math.log10(Math.max(parseFloat(t.quoteVolume), 1e5) / 1e5) * 5)
      );
      const prec = _symbolPrecision[t.symbol] || {};
      return { symbol: t.symbol, pump, high, low: parseFloat(t.lowPrice), last,
               vol: parseFloat(t.quoteVolume), fromHigh, hasSignal, signalStrength: strength, confidence,
               qtyPrecision: prec.qtyPrecision ?? 3,
               pricePrecision: prec.pricePrecision ?? 4,
               stepSize: prec.stepSize ?? 0.001,
               tickSize: prec.tickSize ?? 0.0001 };
    });

    _scanCache = {
      scannedAt: new Date().toISOString(), totalPairs,
      pumped: results.length, signals: results.filter(r => r.hasSignal).length,
      pumpThreshold, minVolume, revThreshold,
      results, signalList: results.filter(r => r.hasSignal),
    };
    console.log(`[Scan] Complete: ${totalPairs} pairs, ${results.length} pumped, ${_scanCache.signals} signals`);
    return _scanCache;
  } finally {
    _scanRunning = false;
  }
}

// Start background auto-scan on server (every N seconds matching engine cfg)
function startAutoScan() {
  if (_scanAutoTimer) clearInterval(_scanAutoTimer);
  const interval = Math.max(60, (engine.cfg.autoInterval || 300)) * 1000;
  _scanAutoTimer = setInterval(async () => {
    try {
      const cfg = engine.cfg;
      await _runScanInternal(cfg.pumpThreshold||20, cfg.minVolume||5e5, cfg.reversalThreshold||5);
    } catch(e) { console.error('[AutoScan] Error:', e.message); }
  }, interval);
  // Run immediately on start
  setTimeout(async () => {
    try {
      const cfg = engine.cfg;
      await _runScanInternal(cfg.pumpThreshold||20, cfg.minVolume||5e5, cfg.reversalThreshold||5);
    } catch(e) { console.error('[AutoScan] Initial scan error:', e.message); }
  }, 5000);
  console.log(`[AutoScan] Background scan every ${interval/1000}s`);
}

// GET /api/scan/precision/:symbol — return lot size + price precision for a symbol
app.get('/api/scan/precision/:symbol', (req, res) => {
  const sym  = req.params.symbol.toUpperCase().replace('/USDT','USDT').replace('/','');
  const prec = _symbolPrecision[sym];
  if (!prec) return res.status(404).json({ error: 'Symbol not found or not TRADING', symbol: sym });
  res.json({ symbol: sym, ...prec, tradable: _tradingSymbols?.has(sym) || false });
});

// GET /api/scan — returns cached scan result instantly; triggers fresh scan if stale
// Scans TRADING USDT-M perpetuals only (excludes SETTLING contracts)
app.get('/api/scan', async (req, res) => {
  const force = req.query.force === '1';
  try {
    const cfg = engine.cfg;
    const pumpThreshold = parseFloat(req.query.pump || cfg.pumpThreshold || 20);
    const minVolume     = parseFloat(req.query.vol  || cfg.minVolume     || 5e5);
    const revThreshold  = parseFloat(req.query.rev  || cfg.reversalThreshold || 5);

    // Return cached result immediately if fresh enough (< 2 min) and not forced
    const cacheAge = _scanCache ? (Date.now() - new Date(_scanCache.scannedAt).getTime()) : Infinity;
    if (!force && _scanCache && cacheAge < 120000) {
      return res.json({ ..._scanCache, cached: true, cacheAgeMs: cacheAge });
    }
    // If scan already running, return old cache with running flag
    if (_scanRunning) {
      return res.json({ ...(_scanCache || {}), running: true, cached: true });
    }
    // Run scan (non-blocking: returns immediately if cache available while scan runs)
    if (_scanCache && !force) {
      // Return stale cache, trigger background refresh
      _runScanInternal(pumpThreshold, minVolume, revThreshold).catch(e => console.error('[Scan]', e.message));
      return res.json({ ..._scanCache, cached: true, refreshing: true });
    }
    const result = await _runScanInternal(pumpThreshold, minVolume, revThreshold);
    res.json(result);
  } catch(e) {
    console.error('[Scan] Error:', e.message);
    res.status(500).json({ error: e.message });
  }
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

  // Start background scan (server fetches Binance every 5 min, browser just polls cache)
  startAutoScan();
  console.log(`[Server] Background scan started — dashboard loads instantly from cache`);
});

// Graceful shutdown
process.on('SIGTERM', () => { engine.stop(); process.exit(0); });
process.on('SIGINT',  () => { engine.stop(); process.exit(0); });

module.exports = app;
