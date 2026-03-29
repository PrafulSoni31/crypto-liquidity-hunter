/**
 * MeanRev v2 — Backend Trading Engine
 * Runs 24/7 independent of browser. Handles:
 *   - Paper positions: stored in SQLite, monitored via Binance WebSocket prices
 *   - Live positions:  synced from Binance Futures API every 15s
 *   - SL/TP checking: closes positions when thresholds hit
 *   - PnL tracking:   real-time unrealised + realised
 *   - REST API:       /api/state, /api/positions, /api/trades, /api/open, /api/close
 */
const crypto  = require('crypto');
const https   = require('https');
const sqlite3 = require('better-sqlite3');
const path    = require('path');
const EventEmitter = require('events');

// ─── DB Setup ───────────────────────────────────────────────────────────────
const DB_PATH = path.join(__dirname, 'engine.db');
let db;
try {
  db = sqlite3(DB_PATH);
} catch(e) {
  // better-sqlite3 not installed yet — use in-memory fallback, install below
  db = null;
}

function initDB() {
  if (!db) return;
  // Run schema migrations safely
  const migrations = [
    `ALTER TABLE positions ADD COLUMN reentry_price REAL`,
    `ALTER TABLE positions ADD COLUMN reentry_step  REAL`,
    `ALTER TABLE positions ADD COLUMN lowest_price  REAL`,
    `ALTER TABLE positions ADD COLUMN highest_price REAL`,
  ];
  for (const m of migrations) {
    try { db.exec(m); } catch(e) { /* column already exists */ }
  }
  db.exec(`
    CREATE TABLE IF NOT EXISTS positions (
      id            TEXT PRIMARY KEY,
      symbol        TEXT NOT NULL,
      side          TEXT NOT NULL DEFAULT 'SHORT',
      avg_entry     REAL NOT NULL,
      total_size    REAL NOT NULL,
      entries_json  TEXT NOT NULL DEFAULT '[]',
      tp_price      REAL,
      sl_price      REAL,
      reentry_price REAL,
      reentry_step  REAL,
      trail_pct     REAL DEFAULT 0,
      trail_price   REAL,
      lowest_price  REAL,
      highest_price REAL,
      open_time     TEXT NOT NULL,
      mode          TEXT NOT NULL DEFAULT 'paper',
      broker        TEXT DEFAULT 'binance',
      order_id      TEXT,
      status        TEXT NOT NULL DEFAULT 'open'
    );
    -- Add new columns to existing DB without breaking old installs
    CREATE TABLE IF NOT EXISTS _migrations (migration TEXT PRIMARY KEY);
    
    CREATE TABLE IF NOT EXISTS trades (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol      TEXT NOT NULL,
      side        TEXT NOT NULL,
      entry_price REAL NOT NULL,
      exit_price  REAL,
      total_size  REAL NOT NULL,
      pnl_usd     REAL,
      pnl_pct     REAL,
      reason      TEXT,
      open_time   TEXT,
      close_time  TEXT,
      mode        TEXT NOT NULL DEFAULT 'paper',
      broker      TEXT DEFAULT 'binance'
    );
    CREATE TABLE IF NOT EXISTS engine_state (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
  `);
}

// ─── State ──────────────────────────────────────────────────────────────────
class TradingEngine extends EventEmitter {
  constructor() {
    super();
    this.mode        = 'paper';
    this.broker      = 'binance';
    this.brokerCreds = { key: '', secret: '', network: 'mainnet' };
    this.cfg = {
      positionSize:   200,
      takeProfitPct:  4,
      stopLossPct:    12,
      trailingStopPct: 0,
      maxPositions:   3,
      maxAveraging:   3,
      dailyLossLimit: 500,
    };
    this.balance      = 10000;  // paper balance
    this.peakBalance  = 10000;
    this.realised     = 0;
    this.dailyLoss    = 0;
    this.wins         = 0;
    this.losses       = 0;
    this.prices       = {};     // symbol → last price
    this.positions    = [];     // in-memory, synced from DB
    this.closedTrades = [];
    this._monitorTimer = null;
    this._liveSync     = null;

    if (db) {
      initDB();
      this._loadState();   // load mode FIRST so _loadFromDB uses correct mode
      this._loadFromDB();  // then load positions for that mode
    }
  }

  // ── Persistence ─────────────────────────────────────────────────────────
  _loadFromDB() {
    if (!db) return;
    // Live positions are NEVER stored in DB — always fetched from Binance
    // Only load paper positions from DB
    const rows = db.prepare("SELECT * FROM positions WHERE status='open' AND mode='paper'").all();
    this.positions = rows.map(r => ({
      id:           r.id,
      symbol:       r.symbol,
      side:         r.side,
      avgEntry:     r.avg_entry,
      totalSize:    r.total_size,
      entries:      JSON.parse(r.entries_json || '[]'),
      tpPrice:      r.tp_price,
      slPrice:      r.sl_price,
      reentryPrice: r.reentry_price,
      reentryStep:  r.reentry_step,
      trailPct:     r.trail_pct,
      trailPrice:   r.trail_price,
      lowestPrice:  r.lowest_price,
      highestPrice: r.highest_price,
      openTime:     new Date(r.open_time),
      mode:         r.mode,
      broker:       r.broker,
      orderId:      r.order_id,
      unrealisedPnl: 0,
    }));
    const trades = db.prepare("SELECT * FROM trades ORDER BY id DESC LIMIT 200").all();
    this.closedTrades = trades;
  }

  _savePosition(pos) {
    if (!db) return;
    db.prepare(`INSERT OR REPLACE INTO positions
      (id,symbol,side,avg_entry,total_size,entries_json,tp_price,sl_price,reentry_price,reentry_step,trail_pct,trail_price,lowest_price,highest_price,open_time,mode,broker,order_id,status)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')
    `).run(
      pos.id, pos.symbol, pos.side, pos.avgEntry, pos.totalSize,
      JSON.stringify(pos.entries||[]),
      pos.tpPrice||null, pos.slPrice||null,
      pos.reentryPrice||null, pos.reentryStep||null,
      pos.trailPct||0, pos.trailPrice||null,
      pos.lowestPrice||null, pos.highestPrice||null,
      pos.openTime instanceof Date ? pos.openTime.toISOString() : pos.openTime,
      pos.mode||this.mode, pos.broker||this.broker, pos.orderId||null
    );
  }

  _closePositionDB(posId, exitPrice, pnlUsd, pnlPct, reason, mode) {
    if (!db) return;
    db.prepare("UPDATE positions SET status='closed' WHERE id=?").run(posId);
    const pos = this.positions.find(p=>p.id===posId);
    if (!pos) return;
    db.prepare(`INSERT INTO trades (symbol,side,entry_price,exit_price,total_size,pnl_usd,pnl_pct,reason,open_time,close_time,mode,broker)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    `).run(pos.symbol, pos.side, pos.avgEntry, exitPrice, pos.totalSize,
           pnlUsd, pnlPct, reason, pos.openTime.toISOString(),
           new Date().toISOString(), mode||this.mode, this.broker);
  }

  _saveState() {
    if (!db) return;
    const s = { balance: this.balance, peakBalance: this.peakBalance,
                realised: this.realised, dailyLoss: this.dailyLoss,
                wins: this.wins, losses: this.losses,
                mode: this.mode, cfg: this.cfg,
                brokerCreds: { ...this.brokerCreds, secret: '****' } };
    db.prepare("INSERT OR REPLACE INTO engine_state (key,value) VALUES ('state',?)")
      .run(JSON.stringify(s));
  }

  _loadState() {
    if (!db) return;
    const row = db.prepare("SELECT value FROM engine_state WHERE key='state'").get();
    if (!row) return;
    try {
      const s = JSON.parse(row.value);
      this.mode        = s.mode         ?? 'paper';
      // Only restore paper balance — live balance always comes from Binance API
      if (this.mode === 'paper') {
        this.balance   = s.balance      ?? this.balance;
        this.peakBalance = s.peakBalance ?? this.peakBalance;
      }
      this.realised    = s.realised     ?? 0;
      this.dailyLoss   = s.dailyLoss    ?? 0;
      this.wins        = s.wins         ?? 0;
      this.losses      = s.losses       ?? 0;
      if (s.cfg) Object.assign(this.cfg, s.cfg);
    } catch(e) {}
  }

  // ── Price Fetch ─────────────────────────────────────────────────────────
  async fetchPrice(symbol) {
    return new Promise((resolve) => {
      const sym = symbol.replace('/','');
      const opts = {
        hostname: 'api.binance.com',
        path:     `/api/v3/ticker/price?symbol=${sym}`,
        method:   'GET',
        headers:  { 'User-Agent': 'MeanRevTrader/2.0' },
      };
      const req = https.request(opts, r => {
        let data = '';
        r.on('data', c => data += c);
        r.on('end', () => {
          try { const d = JSON.parse(data); resolve(parseFloat(d.price||0)); }
          catch(e) { resolve(0); }
        });
      });
      req.on('error', () => resolve(0));
      req.setTimeout(5000, () => { req.destroy(); resolve(0); });
      req.end();
    });
  }

  async fetchPrices(symbols) {
    const unique = [...new Set(symbols)];
    await Promise.all(unique.map(async sym => {
      const p = await this.fetchPrice(sym);
      if (p > 0) this.prices[sym] = p;
    }));
  }

  // ── Binance Signed Request ───────────────────────────────────────────────
  async binanceRequest(method, fpath, params = {}) {
    const { key, secret, network } = this.brokerCreds;
    if (!key || !secret) throw new Error('No broker credentials');
    const base = network === 'testnet'
      ? 'https://testnet.binancefuture.com'
      : 'https://fapi.binance.com';
    const p = { ...params, timestamp: Date.now(), recvWindow: 5000 };
    const qs = Object.entries(p).map(([k,v]) => `${k}=${encodeURIComponent(v)}`).join('&');
    const sig = crypto.createHmac('sha256', secret).update(qs).digest('hex');

    // batchOrders must go in POST body (not URL) due to length limits
    const isBatch  = method === 'POST' && fpath.includes('batchOrders');
    const fullQs   = `${qs}&signature=${sig}`;
    const url      = isBatch
      ? `${base}${fpath}`                     // params go in body
      : `${base}${fpath}?${fullQs}`;          // params in URL (GET/POST)
    const body     = isBatch ? fullQs : null;

    return new Promise((resolve, reject) => {
      const urlObj = new URL(url);
      const opts = {
        hostname: urlObj.hostname,
        path:     urlObj.pathname + urlObj.search,
        method,
        headers:  { 'X-MBX-APIKEY': key, 'User-Agent': 'MeanRevTrader/2.0',
                    'Content-Type': 'application/x-www-form-urlencoded' },
      };
      if (body) opts.headers['Content-Length'] = Buffer.byteLength(body);
      const req = https.request(opts, r => {
        let data = '';
        r.on('data', c => data += c);
        r.on('end', () => {
          try { resolve(JSON.parse(data)); }
          catch(e) { resolve({}); }
        });
      });
      req.on('error', reject);
      req.setTimeout(10000, () => { req.destroy(); reject(new Error('Timeout')); });
      if (body) req.write(body);
      req.end();
    });
  }

  // ── Build position object from Binance positionRisk row ──────────────────
  _buildLivePos(p, existingPos = null) {
    const amt        = parseFloat(p.positionAmt);
    const entryPrice = parseFloat(p.entryPrice);
    const markPrice  = parseFloat(p.markPrice);
    const notional   = Math.abs(amt) * entryPrice;
    const unPnl      = parseFloat(p.unRealizedProfit || 0);
    const side       = amt > 0 ? 'LONG' : 'SHORT';
    this.prices[p.symbol] = markPrice;

    // Restore TP/SL from existing in-memory position if available
    // Otherwise calculate from cfg
    const tpPrice   = existingPos?.tpPrice  || entryPrice * (1 - (this.cfg.takeProfitPct / 100));
    const slPrice   = existingPos?.slPrice  || entryPrice * (1 + (this.cfg.stopLossPct   / 100));
    const reentryStep  = this.cfg.stopLossPct / Math.max(this.cfg.maxAveraging, 1);
    const reentryPrice = existingPos?.reentryPrice || entryPrice * (1 + (reentryStep / 100));

    return {
      id:            `live_${p.symbol}`,
      symbol:        p.symbol,
      side,
      avgEntry:      entryPrice,
      markPrice,
      totalSize:     notional,
      contracts:     Math.abs(amt),
      entries:       existingPos?.entries || [{ price: entryPrice, size: notional, time: new Date().toISOString() }],
      tpPrice,
      slPrice,
      reentryPrice,
      reentryStep,
      trailPct:      existingPos?.trailPct   ?? this.cfg.trailingStopPct ?? 0,
      trailPrice:    existingPos?.trailPrice  ?? null,
      lowestPrice:   existingPos?.lowestPrice ?? markPrice,
      highestPrice:  existingPos?.highestPrice ?? markPrice,
      openTime:      existingPos?.openTime ?? new Date(),
      mode:          'live',
      broker:        'binance',
      unrealisedPnl: unPnl,
      roiPct:        notional > 0 ? (unPnl / (notional / parseFloat(p.leverage || 1))) * 100 : 0,
      leverage:      parseFloat(p.leverage || 1),
      liquidPrice:   parseFloat(p.liquidationPrice || 0),
    };
  }

  // ── Live Position Sync ───────────────────────────────────────────────────
  // In live mode: Binance is the ONLY source of truth for position data.
  // TP/SL/re-entry levels are maintained in memory and enforced by monitorLoop.
  async syncLivePositions() {
    if (this.mode !== 'live' || !this.brokerCreds.key) return;
    try {
      const data = await this.binanceRequest('GET', '/fapi/v2/positionRisk');
      if (!Array.isArray(data)) return;

      const open = data.filter(p => Math.abs(parseFloat(p.positionAmt)) > 0);
      const livePositions = open.map(p => {
        const existing = this.positions.find(pos => pos.id === `live_${p.symbol}`);
        return this._buildLivePos(p, existing);
      });

      // Detect live positions that disappeared (closed on exchange)
      const prevLive = this.positions.filter(p => p.mode === 'live');
      const newSymbols = new Set(livePositions.map(p => p.symbol));
      for (const old of prevLive) {
        if (!newSymbols.has(old.symbol)) {
          console.log(`[Engine] Live position closed on exchange: ${old.symbol}`);
          this.closedTrades.unshift({
            symbol: old.symbol, side: old.side,
            entryPrice: old.avgEntry, exitPrice: this.prices[old.symbol] || old.avgEntry,
            pnl: old.unrealisedPnl || 0, reason: 'Closed on exchange',
            time: new Date(), mode: 'live',
          });
        }
      }

      this.positions = [
        ...this.positions.filter(p => p.mode !== 'live'),
        ...livePositions,
      ];
      this.emit('stateUpdate');
    } catch(e) {
      console.error('[Engine] syncLivePositions error:', e.message);
    }
  }

  // ── Fetch live positions on demand (for API responses) ──────────────────
  async getLivePositions() {
    if (!this.brokerCreds.key) return [];
    try {
      const data = await this.binanceRequest('GET', '/fapi/v2/positionRisk');
      if (!Array.isArray(data)) return [];
      return data
        .filter(p => Math.abs(parseFloat(p.positionAmt)) > 0)
        .map(p => {
          const existing = this.positions.find(pos => pos.id === `live_${p.symbol}`);
          return this._buildLivePos(p, existing);
        });
    } catch(e) {
      console.error('[Engine] getLivePositions error:', e.message);
      return [];
    }
  }

  // ── Recalculate TP/SL/reentry from current avgEntry ─────────────────────
  recalcLevels(pos) {
    const tp = pos.avgEntry * (1 - (this.cfg.takeProfitPct  / 100));
    const sl = pos.avgEntry * (1 + (this.cfg.stopLossPct    / 100));
    // Re-entry: trigger when price rises X% above avgEntry (SHORT only)
    // Use stopLossPct/maxAveraging as the re-entry step spacing
    const reentryStep = this.cfg.stopLossPct / Math.max(this.cfg.maxAveraging, 1);
    const reentryPrice = pos.avgEntry * (1 + (reentryStep / 100));
    pos.tpPrice      = tp;
    pos.slPrice      = sl;
    pos.reentryPrice = reentryPrice;
    pos.reentryStep  = reentryStep;
    this._savePosition(pos);
    return { tp, sl, reentryPrice };
  }

  // ── Monitor Loop ─────────────────────────────────────────────────────────
  async monitorLoop() {
    if (!this.positions.length) return;
    const symbols = [...new Set(this.positions.map(p => p.symbol))];
    await this.fetchPrices(symbols);

    for (const pos of [...this.positions]) {
      const price = this.prices[pos.symbol];
      if (!price || price <= 0) continue;

      // ── Live positions: enforce SL/TP via price watch ──────────────────
      if (pos.mode === 'live') {
        // Ensure TP/SL are set (calculated from cfg if missing)
        if (!pos.tpPrice || !pos.slPrice) this.recalcLevels(pos);
        const tp = pos.tpPrice;
        const sl = pos.slPrice;
        let closeReason = null;
        if (pos.side === 'SHORT') {
          if (price <= tp) closeReason = 'Take-Profit ✓';
          else if (price >= sl) closeReason = 'Stop-Loss ✗';
        } else {
          if (price >= tp) closeReason = 'Take-Profit ✓';
          else if (price <= sl) closeReason = 'Stop-Loss ✗';
        }
        if (closeReason) {
          // Place real closing order on Binance
          try {
            const sym   = pos.symbol.replace('/USDT','USDT').replace('/','');
            const qty   = pos.contracts
              ? pos.contracts.toFixed(3)
              : (pos.totalSize / price).toFixed(3);
            const closeSide = pos.side === 'SHORT' ? 'BUY' : 'SELL';
            const order = await this.binanceRequest('POST', '/fapi/v1/order', {
              symbol: sym, side: closeSide, type: 'MARKET',
              quantity: qty, reduceOnly: 'true'
            });
            const exitPrice = parseFloat(order.avgPrice || order.price || price);
            console.log(`[Engine] Live ${closeReason} ${sym} @ ${exitPrice}`);
            // Remove from in-memory positions
            const idx = this.positions.findIndex(p => p.id === pos.id);
            if (idx !== -1) this.positions.splice(idx, 1);
            // Record closed trade
            const pnlPct = pos.side === 'SHORT'
              ? ((pos.avgEntry - exitPrice) / pos.avgEntry) * 100
              : ((exitPrice - pos.avgEntry) / pos.avgEntry) * 100;
            const pnlUsd = (pnlPct / 100) * pos.totalSize;
            if (pnlUsd >= 0) this.wins++; else this.losses++;
            this.realised += pnlUsd;
            this.closedTrades.unshift({
              symbol: pos.symbol, side: pos.side,
              entryPrice: pos.avgEntry, exitPrice,
              pnl: pnlUsd, pnlPct, reason: closeReason,
              time: new Date(), mode: 'live',
            });
            this.emit('positionClosed', pos, exitPrice, closeReason);
          } catch(e) {
            console.error(`[Engine] Live close order failed ${pos.symbol}:`, e.message);
          }
        } else {
          // Update live unrealised PnL from price
          if (pos.side === 'SHORT') {
            pos.unrealisedPnl = ((pos.avgEntry - price) / pos.avgEntry) * pos.totalSize;
          } else {
            pos.unrealisedPnl = ((price - pos.avgEntry) / pos.avgEntry) * pos.totalSize;
          }
        }
        continue;
      }

      // ── Paper positions ────────────────────────────────────────────────
      // Ensure TP/SL always reflect current avgEntry (recalc if stale)
      if (!pos.tpPrice || !pos.slPrice) this.recalcLevels(pos);
      const tp = pos.tpPrice;
      const sl = pos.slPrice;

      // Update trailing stop (SHORT: track lowest price)
      if (pos.trailPct > 0) {
        if (pos.side === 'SHORT') {
          if (!pos.lowestPrice || price < pos.lowestPrice) pos.lowestPrice = price;
          const newTrail = pos.lowestPrice * (1 + pos.trailPct / 100);
          // Trail only tightens, never loosens
          if (!pos.trailPrice || newTrail < pos.trailPrice) pos.trailPrice = newTrail;
        } else {
          if (!pos.highestPrice || price > pos.highestPrice) pos.highestPrice = price;
          const newTrail = pos.highestPrice * (1 - pos.trailPct / 100);
          if (!pos.trailPrice || newTrail > pos.trailPrice) pos.trailPrice = newTrail;
        }
      }

      let closeReason = null;
      if (pos.side === 'SHORT') {
        if  (price <= tp)                                  closeReason = 'Take-Profit ✓';
        else if (price >= sl)                              closeReason = 'Stop-Loss ✗';
        else if (pos.trailPrice && price >= pos.trailPrice) closeReason = 'Trailing Stop 📈';
      } else {
        if  (price >= tp)                                  closeReason = 'Take-Profit ✓';
        else if (price <= sl)                              closeReason = 'Stop-Loss ✗';
        else if (pos.trailPrice && price <= pos.trailPrice) closeReason = 'Trailing Stop 📈';
      }

      if (closeReason) {
        this.closePosition(pos.id, price, closeReason);
      } else {
        // Update unrealised PnL
        pos.unrealisedPnl = pos.side === 'SHORT'
          ? ((pos.avgEntry - price) / pos.avgEntry) * pos.totalSize
          : ((price - pos.avgEntry) / pos.avgEntry) * pos.totalSize;
      }
    }
    this.emit('stateUpdate');
  }

  // ── Open Position / Re-entry ─────────────────────────────────────────────
  openPosition(symbol, side, price, size, opts = {}) {
    const existing = this.positions.find(p => p.symbol === symbol && p.mode === this.mode);

    if (existing) {
      // ── RE-ENTRY / AVERAGING ────────────────────────────────────────────
      if (existing.entries.length >= this.cfg.maxAveraging)
        return { error: `Max averaging (${this.cfg.maxAveraging}) reached` };

      // Validate re-entry price: for SHORT, only average UP (price must be above avgEntry)
      // Re-entry is valid if price >= avgEntry * (1 + reentryStep/2) i.e. price moved against us
      const reentryStep = this.cfg.stopLossPct / Math.max(this.cfg.maxAveraging, 1);
      const minReentryPrice = existing.avgEntry * (1 + (reentryStep * 0.5 / 100));
      if (existing.side === 'SHORT' && price < minReentryPrice) {
        return { error: `Re-entry too early: price ${price.toFixed(6)} must be ≥ ${minReentryPrice.toFixed(6)} (${(reentryStep*0.5).toFixed(1)}% above avg entry)` };
      }

      // Add re-entry
      existing.entries.push({ price, size, time: new Date().toISOString() });
      existing.totalSize += size;
      // Recalculate VWAP average entry
      existing.avgEntry = existing.entries.reduce((s, e) => s + e.price * e.size, 0) / existing.totalSize;
      // Recalculate TP/SL/re-entry from NEW avgEntry
      this.recalcLevels(existing);
      if (this.mode === 'paper') this.balance -= size;
      this._savePosition(existing);
      console.log(`[Engine] Re-entry #${existing.entries.length} ${symbol} @ ${price} | avgEntry=${existing.avgEntry.toFixed(6)} TP=${existing.tpPrice?.toFixed(6)} SL=${existing.slPrice?.toFixed(6)}`);
      this.emit('positionUpdated', existing);
      return {
        id:           existing.id,
        averaged:     true,
        entryNum:     existing.entries.length,
        avgEntry:     existing.avgEntry,
        tpPrice:      existing.tpPrice,
        slPrice:      existing.slPrice,
        reentryPrice: existing.reentryPrice,
      };
    }

    if (this.positions.filter(p => p.mode === this.mode).length >= this.cfg.maxPositions)
      return { error: `Max positions (${this.cfg.maxPositions}) reached` };

    // ── NEW POSITION ────────────────────────────────────────────────────────
    // Always calculate TP/SL from cfg (ignore any passed-in opts to keep consistency)
    const tpPrice   = price * (1 - (this.cfg.takeProfitPct / 100));
    const slPrice   = price * (1 + (this.cfg.stopLossPct   / 100));
    const reentryStep  = this.cfg.stopLossPct / Math.max(this.cfg.maxAveraging, 1);
    const reentryPrice = price * (1 + (reentryStep / 100));

    const pos = {
      id:            opts.orderId ? `live_${symbol}_${Date.now()}` : `paper_${symbol}_${Date.now()}`,
      symbol,
      side:          side || 'SHORT',
      avgEntry:      price,
      totalSize:     size,
      entries:       [{ price, size, time: new Date().toISOString() }],
      tpPrice,
      slPrice,
      reentryPrice,
      reentryStep,
      trailPct:      this.cfg.trailingStopPct || 0,
      trailPrice:    null,
      lowestPrice:   price,   // for SHORT trailing stop
      highestPrice:  price,   // for LONG trailing stop
      openTime:      new Date(),
      mode:          this.mode,
      broker:        this.broker,
      orderId:       opts.orderId || null,
      unrealisedPnl: 0,
    };
    this.positions.push(pos);
    if (this.mode === 'paper') {
      this.balance -= size;
      this._savePosition(pos);
    }
    console.log(`[Engine] Position opened: ${side} ${symbol} @ ${price} | TP=${tpPrice.toFixed(6)} SL=${slPrice.toFixed(6)} reentry=${reentryPrice.toFixed(6)}`);
    this.emit('positionOpened', pos);
    this.emit('stateUpdate');
    return {
      id:           pos.id,
      opened:       true,
      avgEntry:     price,
      tpPrice,
      slPrice,
      reentryPrice,
    };
  }

  // ── Close Position ───────────────────────────────────────────────────────
  closePosition(posId, exitPrice, reason = 'Manual') {
    const idx = this.positions.findIndex(p => p.id === posId);
    if (idx === -1) return { error: 'Position not found' };
    const pos = this.positions[idx];

    const pnlPct = pos.side === 'SHORT'
      ? ((pos.avgEntry - exitPrice) / pos.avgEntry) * 100
      : ((exitPrice - pos.avgEntry) / pos.avgEntry) * 100;
    const pnlUsd = (pnlPct / 100) * pos.totalSize;

    if (pos.mode === 'paper') {
      this.balance  += pos.totalSize + pnlUsd;
      this.realised += pnlUsd;
      if (this.balance > this.peakBalance) this.peakBalance = this.balance;
      if (pnlUsd >= 0) this.wins++; else { this.losses++; this.dailyLoss += Math.abs(pnlUsd); }
    }

    this._closePositionDB(posId, exitPrice, pnlUsd, pnlPct, reason, pos.mode);
    this.closedTrades.unshift({
      symbol: pos.symbol, side: pos.side,
      entryPrice: pos.avgEntry, exitPrice,
      pnl: pnlUsd, pnlPct, reason,
      time: new Date(), mode: pos.mode,
    });
    this.positions.splice(idx, 1);
    this._saveState();
    this.emit('positionClosed', pos, exitPrice, reason);
    this.emit('stateUpdate');
    return { closed: true, pnlUsd, pnlPct };
  }

  // ── Start / Stop ─────────────────────────────────────────────────────────
  start() {
    console.log('[Engine] Starting monitoring loop...');
    // Paper monitoring: check SL/TP every 15s
    this._monitorTimer = setInterval(() => this.monitorLoop(), 15000);
    // Live sync: pull Binance positions every 15s
    this._liveSync = setInterval(() => this.syncLivePositions(), 15000);
    // Save state every minute
    this._stateSave = setInterval(() => this._saveState(), 60000);
    console.log('[Engine] Running. Paper monitor: 15s, Live sync: 15s');
  }

  stop() {
    clearInterval(this._monitorTimer);
    clearInterval(this._liveSync);
    clearInterval(this._stateSave);
    this._saveState();
    console.log('[Engine] Stopped.');
  }

  // ── State Snapshot ───────────────────────────────────────────────────────
  getState() {
    const uPnL = this.positions.reduce((s, p) => s + (p.unrealisedPnl || 0), 0);
    const maxDD = this.peakBalance > 0
      ? ((this.peakBalance - this.balance) / this.peakBalance) * 100
      : 0;
    return {
      mode:           this.mode,
      broker:         this.broker,
      balance:        Math.round(this.balance * 100) / 100,
      unrealisedPnl:  Math.round(uPnL * 100) / 100,
      realisedPnl:    Math.round(this.realised * 100) / 100,
      totalPnl:       Math.round((uPnL + this.realised) * 100) / 100,
      positions:      this.positions.length,
      wins:           this.wins,
      losses:         this.losses,
      winRate:        this.wins + this.losses > 0
                        ? Math.round(this.wins / (this.wins + this.losses) * 100)
                        : 0,
      maxDrawdownPct: Math.round(maxDD * 100) / 100,
      dailyLoss:      Math.round(this.dailyLoss * 100) / 100,
      cfg:            this.cfg,
      prices:         this.prices,
    };
  }

  setCreds(key, secret, network) {
    this.brokerCreds = { key, secret, network: network || 'mainnet' };
  }

  setMode(mode) {
    // Save current paper positions back to DB before switching
    if (db && this.mode === 'paper') {
      this.positions.filter(p => p.mode === 'paper').forEach(p => this._savePosition(p));
    }
    this.mode = mode;
    // Reload positions for new mode from DB
    if (db) {
      const rows = db.prepare("SELECT * FROM positions WHERE status='open' AND mode=?").all(mode);
      this.positions = rows.map(r => ({
        id: r.id, symbol: r.symbol, side: r.side,
        avgEntry: r.avg_entry, totalSize: r.total_size,
        entries: JSON.parse(r.entries_json || '[]'),
        tpPrice: r.tp_price, slPrice: r.sl_price,
        trailPct: r.trail_pct, trailPrice: r.trail_price,
        openTime: new Date(r.open_time),
        mode: r.mode, broker: r.broker, orderId: r.order_id,
      }));
    }
    // Reset paper balance display when switching
    if (mode === 'paper') {
      this._loadState(); // restore saved paper balance
    }
    this._saveState();
    console.log(`[Engine] Mode set to: ${mode} | positions loaded: ${this.positions.length}`);
  }

  updateCfg(cfg) {
    Object.assign(this.cfg, cfg);
    this._saveState();
  }
}

module.exports = new TradingEngine();
