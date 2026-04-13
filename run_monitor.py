#!/usr/bin/env python3
"""
Standalone Position Monitor Daemon
Runs independently of gunicorn — survives dashboard restarts.
Only ONE instance runs at a time (controlled by systemd).
"""
import sys, os, time, logging, sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MonitorDaemon] %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/monitor_daemon.log')
    ]
)
logger = logging.getLogger(__name__)

def main():
    from core.config_manager import ConfigManager
    from data.store import DataStore
    from core.binance_connector import BinanceConnector
    from core.position_monitor import PositionMonitor

    logger.info("=== Position Monitor Daemon Starting ===")

    cfg = ConfigManager()
    cfg.reload()

    active_id = cfg.get('active_account_id')
    if not active_id:
        logger.error("No active_account_id in config")
        sys.exit(1)

    store = DataStore()

    conn = sqlite3.connect('data/store.db')
    row = conn.execute(
        "SELECT api_key, api_secret, mode FROM accounts WHERE id=? AND enabled=1",
        (int(active_id),)
    ).fetchone()
    conn.close()

    if not row:
        logger.error(f"Account {active_id} not found or disabled")
        sys.exit(1)

    api_key, api_secret, mode = row
    logger.info(f"Account {active_id} (mode={mode}) — starting monitor")

    connector = BinanceConnector(api_key=api_key, api_secret=api_secret, mode=mode)

    # Detect hedge mode (dualSidePosition) without a full CCXT connect()
    try:
        import hmac as _hm, hashlib as _hs, requests as _rq2
        _ts = int(time.time() * 1000)
        _par = f"timestamp={_ts}&recvWindow=15000"
        _sig = _hm.new(api_secret.encode(), _par.encode(), _hs.sha256).hexdigest()
        _r = _rq2.get(
            f"https://fapi.binance.com/fapi/v1/positionSide/dual?{_par}&signature={_sig}",
            headers={"X-MBX-APIKEY": api_key}, timeout=5
        )
        connector.hedge_mode = _r.json().get('dualSidePosition', False)
        logger.info(f"Position mode: {'HEDGE (dualSidePosition=True)' if connector.hedge_mode else 'ONE-WAY'}")
    except Exception as _he:
        logger.warning(f"Hedge mode detection failed: {_he} — assuming ONE-WAY")
        connector.hedge_mode = False

    interval = int(cfg.get('signal_execution.monitor_interval_sec') or 10)
    monitor = PositionMonitor(connector, store, account_id=int(active_id), interval=interval)
    monitor.start()
    logger.info(f"✅ Monitor running — checking every {interval}s")

    try:
        while True:
            time.sleep(30)
            cfg.reload()
    except KeyboardInterrupt:
        logger.info("Monitor daemon stopping.")
        monitor.stop()

if __name__ == '__main__':
    main()
