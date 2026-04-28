"""
MSA Trading Bot - Professional Dashboard v3
Optimized DB reads + Notifications + Market Status
"""
import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse, unquote

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, jsonify, request, session, redirect, Response

DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'MSA2025')
SECRET_KEY = os.getenv('SECRET_KEY', 'msa-bot-secret-2025')
CACHE_CHECK_INTERVAL = 45
BINANCE_API = "https://api.binance.com/api/v3"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
app.secret_key = SECRET_KEY


class DashboardDB:
    def __init__(self):
        self.db_params = None
        self._setup()

    def _setup(self):
        url = os.getenv('DATABASE_URL')
        if not url:
            logger.error("DATABASE_URL not found!")
            return
        p = urlparse(url)
        self.db_params = {
            'host': p.hostname, 'port': p.port or 5432,
            'database': p.path[1:], 'user': p.username,
            'password': unquote(p.password),
            'sslmode': 'require', 'connect_timeout': 10,
        }
        logger.info("Dashboard DB configured")

    def get_fingerprint(self):
        try:
            conn = psycopg2.connect(**self.db_params)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MAX(buy_time) FROM positions;")
            r = cur.fetchone()
            conn.close()
            return r
        except:
            return None

    def load_positions(self):
        try:
            conn = psycopg2.connect(**self.db_params)
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM positions ORDER BY buy_time DESC;")
            rows = cur.fetchall()
            conn.close()
            positions = []
            for row in rows:
                pos = dict(row)
                if pos.get('data'):
                    try:
                        extra = json.loads(pos['data']) if isinstance(pos['data'], str) else pos['data']
                        pos['buy_confidence'] = extra.get('buy_confidence') or 0
                        pos['stop_loss_threshold'] = extra.get('stop_loss_threshold') or 0
                    except:
                        pass
                positions.append(pos)
            return positions
        except Exception as e:
            logger.error(f"Load error: {e}")
            return []

    def load_bot_status(self):
        try:
            conn = psycopg2.connect(**self.db_params)
            cur = conn.cursor()
            cur.execute("SELECT value FROM bot_settings WHERE key = 'bot_status';")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                logger.info(f"bot_status from DB: macro={data.get('macro_status','?')} time={data.get('time','?')}")
                return data
        except Exception as e:
            logger.error(f"Status error: {e}")
        return {}

db = DashboardDB()


class SmartCache:
    def __init__(self):
        self.positions = []
        self.fp = None
        self.last_check = 0
        self.prices = {}
        self.last_price = 0
        self.lock = threading.Lock()
        self.bot_status = {}
        self.last_status = 0
        self.notifications = []
        self.notif_lock = threading.Lock()
        self.initialized = False

    def get_positions(self):
        now = time.time()
        if now - self.last_check > CACHE_CHECK_INTERVAL:
            self.last_check = now
            fp = db.get_fingerprint()
            if fp != self.fp:
                self.fp = fp
                with self.lock:
                    old_positions = self.positions.copy()
                    self.positions = db.load_positions()
                    self._detect_changes(old_positions, self.positions)
        return self.positions

    def get_bot_status(self):
        now = time.time()
        if now - self.last_status > 45:
            status = db.load_bot_status()
            if status:
                with self.lock:
                    self.bot_status = status
                self.last_status = now
            else:
                self.last_status = now - 20
        return self.bot_status

    def _detect_changes(self, old_list, new_list):
        if not self.initialized:
            self.initialized = True
            return
        old_syms = {p['symbol'] for p in old_list}
        new_syms = {p['symbol'] for p in new_list}
        bought = new_syms - old_syms
        sold = old_syms - new_syms
        # Build set of recent notification keys to prevent duplicates
        recent_keys = set()
        for n in self.notifications[-20:]:
            recent_keys.add(f"{n['type']}_{n['symbol']}")
        with self.notif_lock:
            for sym in bought:
                if f"BUY_{sym}" in recent_keys:
                    continue
                pos = next((p for p in new_list if p['symbol'] == sym), None)
                price = float(pos.get('buy_price', 0)) if pos else 0
                self.notifications.append({
                    'type': 'BUY', 'symbol': sym, 'price': price,
                    'time': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                    'id': int(time.time() * 1000)
                })
            for sym in sold:
                pos = next((p for p in old_list if p['symbol'] == sym), None)
                buy_price = float(pos.get('buy_price', 0)) if pos else 0
                slt = float(pos.get('stop_loss_threshold', 0) or 0) if pos else 0
                last_cp = self.prices.get(sym, 0)
                if last_cp and buy_price and last_cp > buy_price:
                    sell_type = 'TAKE PROFIT'
                elif slt > 0:
                    sell_type = 'STOP LOSS'
                else:
                    sell_type = 'SELL'
                if f"{sell_type}_{sym}" in recent_keys:
                    continue
                self.notifications.append({
                    'type': sell_type, 'symbol': sym, 'price': buy_price,
                    'time': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                    'id': int(time.time() * 1000)
                })
            self.notifications = self.notifications[-50:]

    def get_notifications(self, after_id=0):
        with self.notif_lock:
            return [n for n in self.notifications if n['id'] > after_id]

    def get_prices(self, symbols):
        now = time.time()
        if now - self.last_price > 8:
            self.last_price = now
            try:
                resp = requests.get(f"{BINANCE_API}/ticker/price", timeout=5)
                if resp.status_code == 200:
                    all_p = {p['symbol']: float(p['price']) for p in resp.json()}
                    with self.lock:
                        for s in symbols:
                            bs = s.replace('/', '')
                            if bs in all_p:
                                self.prices[s] = all_p[bs]
            except:
                pass
        return self.prices


cache = SmartCache()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        if request.form.get('password') == DASHBOARD_PASSWORD:
            session['logged_in'] = True
            return redirect('/')
        error = "Wrong password"
    return Response(get_login_html(error), mimetype='text/html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')


@app.route('/')
@login_required
def dashboard():
    return Response(get_dashboard_html(), mimetype='text/html')


@app.route('/api/data')
@login_required
def api_data():
    positions = cache.get_positions()
    symbols = [p['symbol'] for p in positions]
    prices = cache.get_prices(symbols) if symbols else {}
    bot_status = cache.get_bot_status()

    after_id = int(request.args.get('after', 0))
    notifications = cache.get_notifications(after_id)

    total_invested = 0
    pos_list = []

    for p in positions:
        sym = p['symbol']
        bp = float(p.get('buy_price', 0) or 0)
        amt = float(p.get('amount', 0) or 0)
        hi = float(p.get('highest_price', bp) or bp)
        inv = float(p.get('invested', 0) or 0)
        cp = prices.get(sym, bp)
        slt = float(p.get('stop_loss_threshold', 0) or 0)
        conf = float(p.get('buy_confidence', 0) or 0)

        if not inv or inv == 0:
            inv = bp * amt

        pp = ((cp - bp) / bp * 100) if bp > 0 else 0
        dp = ((hi - bp) / bp * 100) if bp > 0 else 0
        pu = (cp - bp) * amt
        total_invested += inv

        sl_trigger = slt * 1.5 if slt > 0 else 0

        if pp >= 0.5:
            st, si = 'RIDING', 'green'
        elif pp < -1.0 and slt > 0:
            st, si = 'SL ZONE', 'red'
        elif pp < 0:
            st, si = 'WAITING', 'yellow'
        else:
            st, si = 'HOLDING', 'blue'

        pos_list.append({
            'symbol': sym, 'buy_price': bp, 'current_price': cp,
            'highest_price': hi, 'amount': amt, 'invested': round(inv, 2),
            'profit_pct': round(pp, 2), 'profit_usd': round(pu, 2),
            'peak_pct': round(dp, 2),
            'sl_threshold': round(slt, 2),
            'sl_trigger': round(sl_trigger, 2),
            'confidence': round(conf, 1),
            'tp_price': round(bp * (1 + 0.02 + (conf / 100.0) * 0.04), 6) if bp > 0 else 0,
            'sl_price': round(bp * (1 - slt/100), 6) if bp > 0 and slt > 0 else 0,
            'buy_time': str(p.get('buy_time', '')),
            'status': st, 'status_color': si,
        })

    pos_list.sort(key=lambda x: x['symbol'])
    w = sum(1 for p in pos_list if p['profit_pct'] > 0)
    l = sum(1 for p in pos_list if p['profit_pct'] < 0)
    tp = sum(p['profit_usd'] for p in pos_list)
    best = max((p['profit_pct'] for p in pos_list), default=0)
    worst = min((p['profit_pct'] for p in pos_list), default=0)

    return jsonify({
        'positions': pos_list,
        'summary': {
            'active': len(pos_list), 'max_positions': 20,
            'total_invested': round(total_invested, 2),
            'total_pnl': round(tp, 2), 'winners': w, 'losers': l,
            'best': round(best, 2), 'worst': round(worst, 2),
        },
        'bot_status': bot_status,
        'notifications': notifications,
        'last_update': datetime.now(timezone.utc).strftime('%H:%M:%S UTC'),
    })


@app.route('/api/chart/<path:symbol>')
@login_required
def api_chart(symbol):
    try:
        bs = symbol.replace('/', '')
        url = f"{BINANCE_API}/klines?symbol={bs}&interval=1h&limit=168"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            candles = []
            for k in resp.json():
                candles.append({
                    'time': int(k[0]) // 1000,
                    'open': float(k[1]), 'high': float(k[2]),
                    'low': float(k[3]), 'close': float(k[4]),
                    'volume': float(k[5]),
                })
            return jsonify({'candles': candles, 'symbol': symbol})
    except:
        pass
    return jsonify({'candles': [], 'symbol': symbol})


@app.route('/api/live_price/<path:symbol>')
@login_required
def api_live_price(symbol):
    try:
        bs = symbol.replace('/', '')
        url = f"{BINANCE_API}/ticker/price?symbol={bs}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return jsonify({'price': float(resp.json()['price'])})
    except:
        pass
    return jsonify({'price': 0})

def get_login_html(error=""):
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MSA Trading Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#080b12;color:#e2e8f0;font-family:'Inter',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;overflow:hidden}}
body::before{{content:'';position:fixed;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(ellipse at 30% 20%,rgba(59,130,246,0.08) 0%,transparent 50%),radial-gradient(ellipse at 70% 80%,rgba(139,92,246,0.06) 0%,transparent 50%);animation:bgMove 20s ease infinite;z-index:-1}}
@keyframes bgMove{{0%,100%{{transform:translate(0,0)}}50%{{transform:translate(-2%,-2%)}}}}
.box{{background:rgba(17,24,39,0.8);backdrop-filter:blur(20px);padding:48px;border-radius:24px;border:1px solid rgba(255,255,255,0.06);width:400px;text-align:center;box-shadow:0 25px 50px rgba(0,0,0,0.5)}}
.logo{{width:80px;height:80px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:20px;display:flex;align-items:center;justify-content:center;font-size:36px;margin:0 auto 24px;box-shadow:0 10px 30px rgba(59,130,246,0.3)}}
h1{{font-size:22px;font-weight:700;margin-bottom:6px;background:linear-gradient(135deg,#e2e8f0,#94a3b8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.sub{{color:#64748b;margin-bottom:32px;font-size:13px}}
input{{width:100%;padding:14px 18px;background:rgba(15,23,42,0.8);border:1px solid rgba(255,255,255,0.08);border-radius:12px;color:#e2e8f0;font-size:15px;margin-bottom:18px;text-align:center;font-family:'Inter',sans-serif;transition:all 0.3s}}
input:focus{{outline:none;border-color:rgba(59,130,246,0.5);box-shadow:0 0 20px rgba(59,130,246,0.15)}}
button{{width:100%;padding:14px;background:linear-gradient(135deg,#3b82f6,#6366f1);color:white;border:none;border-radius:12px;font-size:15px;cursor:pointer;font-weight:600;font-family:'Inter',sans-serif;transition:all 0.3s;box-shadow:0 4px 15px rgba(59,130,246,0.3)}}
button:hover{{transform:translateY(-2px);box-shadow:0 8px 25px rgba(59,130,246,0.4)}}
.err{{color:#f87171;margin-bottom:16px;font-size:13px;padding:10px;background:rgba(248,113,113,0.1);border-radius:8px}}
</style></head><body>
<form class="box" method="POST">
<div class="logo"><img src="/static/logo.png" style="width:70px;height:70px;border-radius:15px;object-fit:contain"></div>
<h1>MSA Trading Bot</h1>
<p class="sub">Professional Trading Dashboard</p>
{"<div class='err'>"+error+"</div>" if error else ""}
<input type="password" name="password" placeholder="Enter Password" autofocus>
<button type="submit">Access Dashboard</button>
</form></body></html>"""


def get_dashboard_html():
    return """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MSA Trading Bot</title>


<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
--bg:#060910;--card:rgba(13,17,28,0.85);--card2:rgba(17,24,39,0.6);
--border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.1);
--green:#10b981;--green-bg:rgba(16,185,129,0.12);--green-glow:rgba(16,185,129,0.4);
--red:#ef4444;--red-bg:rgba(239,68,68,0.12);--red-glow:rgba(239,68,68,0.4);
--blue:#3b82f6;--blue-bg:rgba(59,130,246,0.12);--blue-glow:rgba(59,130,246,0.3);
--purple:#8b5cf6;--purple-bg:rgba(139,92,246,0.12);
--yellow:#f59e0b;--yellow-bg:rgba(245,158,11,0.12);
--text:#e2e8f0;--text2:#94a3b8;--text3:#64748b;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;padding:16px;min-height:100vh}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background:
radial-gradient(ellipse at 15% 0%,rgba(59,130,246,0.05) 0%,transparent 50%),
radial-gradient(ellipse at 85% 100%,rgba(139,92,246,0.03) 0%,transparent 50%);pointer-events:none;z-index:-1}

/* Header */
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px}
.header-left{display:flex;align-items:center;gap:12px}
.logo-sm{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 4px 15px rgba(59,130,246,0.3)}
.header h1{font-size:16px;font-weight:700;background:linear-gradient(135deg,#e2e8f0,#94a3b8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header-right{display:flex;gap:12px;align-items:center}
.header-right span{color:var(--text3);font-size:11px;font-family:'JetBrains Mono',monospace}
.live-dot{width:7px;height:7px;background:var(--green);border-radius:50%;display:inline-block;animation:pulse 2s infinite;margin-right:3px}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 var(--green-glow)}50%{opacity:0.7;box-shadow:0 0 0 6px transparent}}
.btn-logout{color:var(--text3);text-decoration:none;font-size:11px;padding:5px 12px;border:1px solid var(--border);border-radius:6px;transition:all 0.3s}
.btn-logout:hover{border-color:var(--red);color:var(--red)}

/* Market Status Bar */
.market-bar{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.mb-item{background:var(--card);backdrop-filter:blur(10px);padding:12px 16px;border-radius:12px;border:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:12px;font-family:'JetBrains Mono',monospace;transition:all 0.3s}
.mb-item:hover{border-color:var(--border2)}
.mb-label{color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:0.5px;font-family:'Inter',sans-serif}
.mb-val{font-weight:600;font-size:13px}
.mb-green{color:var(--green)}.mb-red{color:var(--red)}.mb-yellow{color:var(--yellow)}.mb-blue{color:var(--blue)}.mb-purple{color:var(--purple)}

/* Top Row - Mini Stats + Recent Activity */
.top-row{display:grid;grid-template-columns:auto 1fr;gap:16px;margin-bottom:16px;align-items:start}
.mini-stats{display:flex;gap:10px;flex-wrap:wrap}
.ms{background:var(--card);backdrop-filter:blur(10px);padding:10px 14px;border-radius:10px;border:1px solid var(--border);display:flex;align-items:center;gap:6px;white-space:nowrap;transition:all 0.3s}
.ms:hover{transform:translateY(-1px);border-color:var(--border2)}
.ms-icon{font-size:14px}
.ms-val{font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace}
.ms-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px}
.val-green{color:var(--green)}.val-red{color:var(--red)}.val-blue{color:var(--blue)}.val-yellow{color:var(--yellow)}.val-purple{color:var(--purple)}

.recent-box{background:var(--card);backdrop-filter:blur(10px);border-radius:12px;border:1px solid var(--border);padding:12px;max-height:130px;overflow-y:auto}
.recent-box::-webkit-scrollbar{width:3px}
.recent-box::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.rb-list{display:flex;flex-direction:column;gap:4px}
.rb-item{display:flex;align-items:center;gap:8px;font-size:11px;font-family:'JetBrains Mono',monospace;padding:5px 8px;border-radius:6px;background:rgba(255,255,255,0.02);transition:all 0.2s}
.rb-item:hover{background:rgba(255,255,255,0.04)}
.rb-buy{border-left:3px solid var(--green)}
.rb-sell{border-left:3px solid var(--red)}
.rb-takeprofit{border-left:3px solid #10b981}
.rb-type-takeprofit{background:rgba(16,185,129,0.1);color:#10b981}
.rb-stoploss{border-left:3px solid var(--yellow)}
.rb-type{font-size:9px;font-weight:700;font-family:'Inter',sans-serif;padding:2px 6px;border-radius:4px;min-width:32px;text-align:center}
.rb-type-buy{background:var(--green-bg);color:var(--green)}
.rb-type-sell{background:var(--red-bg);color:var(--red)}
.rb-type-stoploss{background:var(--yellow-bg);color:var(--yellow)}
.rb-coin{font-weight:600;font-family:'Inter',sans-serif;min-width:45px;color:var(--text)}
.rb-price{color:var(--text2);font-size:10px}
.rb-time{color:var(--text3);font-size:9px;margin-left:auto}
.rb-empty{color:var(--text3);font-size:11px;text-align:center;padding:12px 8px}

/* Cards */
.card{background:var(--card);backdrop-filter:blur(10px);border-radius:14px;border:1px solid var(--border);padding:16px;margin-bottom:16px;transition:all 0.3s}
.card:hover{border-color:var(--border2)}
.card-title{font-size:13px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px;color:var(--text2)}

/* Charts Row */



#chartCanvas{width:100%;height:320px}

.perf-legend{display:flex;gap:12px;margin-bottom:8px;flex-wrap:wrap}
.perf-leg-item{display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text2);font-family:'Inter',sans-serif}
.perf-dot{width:8px;height:8px;border-radius:50%}

/* Tabs */
.tabs{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:12px}
.tab{padding:6px 12px;border-radius:7px;border:1px solid var(--border);background:transparent;color:var(--text3);cursor:pointer;font-size:10px;font-weight:500;font-family:'Inter',sans-serif;transition:all 0.3s}
.tab:hover{border-color:var(--border2);color:var(--text)}
.tab.act{background:linear-gradient(135deg,var(--blue),var(--purple));color:white;border-color:transparent;box-shadow:0 2px 10px rgba(59,130,246,0.3)}

/* Table */
.tbl-wrap{overflow-x:auto;border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:10px 8px;color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:1px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.04);position:sticky;top:0;background:rgba(6,9,16,0.95);backdrop-filter:blur(10px)}
td{padding:12px 8px;border-bottom:1px solid rgba(255,255,255,0.02);font-family:'JetBrains Mono',monospace;font-size:11px}
tr{transition:all 0.2s}
tr:hover{background:rgba(255,255,255,0.02)}

.coin-name{display:flex;align-items:center;gap:8px}
.coin-icon{width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;font-family:'Inter',sans-serif}
.coin-sym{font-weight:600;font-family:'Inter',sans-serif;font-size:12px;color:var(--text)}
.coin-pair{font-size:9px;color:var(--text3);font-family:'Inter',sans-serif}

.pnl-bar{height:3px;border-radius:2px;background:rgba(255,255,255,0.04);overflow:hidden;width:70px;display:inline-block;vertical-align:middle;margin-left:6px}
.pnl-fill{height:100%;border-radius:2px;transition:width 0.5s}

.pos{color:var(--green);font-weight:600}
.neg{color:var(--red);font-weight:600}
.zer{color:var(--text3)}

.badge{padding:3px 8px;border-radius:5px;font-size:9px;font-weight:600;font-family:'Inter',sans-serif;letter-spacing:0.5px}
.b-green{background:var(--green-bg);color:var(--green)}
.b-red{background:var(--red-bg);color:var(--red)}
.b-yellow{background:var(--yellow-bg);color:var(--yellow)}
.b-blue{background:var(--blue-bg);color:var(--blue)}

.chart-btn{padding:5px 8px;border-radius:5px;border:1px solid var(--border);background:transparent;color:var(--text3);cursor:pointer;font-size:10px;transition:all 0.3s}
.chart-btn:hover{border-color:var(--blue);color:var(--blue)}

.sl-info{font-size:10px;color:var(--text3);line-height:1.4}
.sl-val{color:var(--red);font-weight:600}
.sl-trigger{color:var(--yellow);font-size:9px}

/* PnL Summary */
.pnl-summary{display:flex;gap:12px;margin-bottom:12px;align-items:center}
.pnl-bar-big{flex:1;height:6px;border-radius:3px;background:rgba(255,255,255,0.04);overflow:hidden;display:flex}
.pnl-bar-big .w{background:linear-gradient(90deg,var(--green),#34d399);transition:width 0.5s}
.pnl-bar-big .lo{background:linear-gradient(90deg,#f87171,var(--red));transition:width 0.5s}
.pnl-label{font-size:10px;color:var(--text3);white-space:nowrap}

.footer{text-align:center;color:var(--text3);font-size:10px;margin-top:16px;padding:12px;opacity:0.5}

@keyframes headGlow{0%,100%{filter:drop-shadow(0 0 4px rgba(59,130,246,0.6))}50%{filter:drop-shadow(0 0 12px rgba(59,130,246,0.9))}}

@media(max-width:700px){
.top-row{grid-template-columns:1fr}
.mini-stats{justify-content:center}
}

@media(max-width:900px){

}

@media(max-width:600px){
body{padding:10px}
.mini-stats{gap:6px}
.ms{padding:8px 10px}
.ms-val{font-size:14px}
.market-bar{flex-direction:column;gap:8px}
table{font-size:10px}
td,th{padding:6px 4px}
#chart{height:220px}
#chart2{height:220px}
}
</style></head><body>

<div class="header">
<div class="header-left">
<div class="logo-sm"><img src="/static/logo.png" style="width:34px;height:34px;border-radius:8px;object-fit:contain"></div>
<h1>MSA Trading Bot</h1>
</div>
<div class="header-right">
<span><span class="live-dot"></span>LIVE</span>
<span id="clk"></span>
<span id="upd"></span>
<a href="/logout" class="btn-logout">Logout</a>
</div>
</div>

<!-- Market Status Bar -->
<div class="market-bar" id="mBar">
<div class="mb-item"><span>🌐</span><div><div class="mb-label">Macro Trend</div><div class="mb-val" id="mbMacro">-</div></div></div>
<div class="mb-item"><span>💰</span><div><div class="mb-label">Balance</div><div class="mb-val mb-blue" id="mbBal">-</div></div></div>
<div class="mb-item"><span>📊</span><div><div class="mb-label">Invested</div><div class="mb-val mb-yellow" id="mbInv">-</div></div></div>
<div class="mb-item"><span>🔒</span><div><div class="mb-label">Locked Profit</div><div class="mb-val mb-green" id="mbLock">-</div></div></div>
<div class="mb-item"><span>💵</span><div><div class="mb-label">Tradable</div><div class="mb-val mb-yellow" id="mbTrad">-</div></div></div>
<div class="mb-item"><span>📊</span><div><div class="mb-label">Active</div><div class="mb-val mb-blue" id="sA">-</div></div></div>
<div class="mb-item"><span>📈</span><div><div class="mb-label">P&L</div><div class="mb-val" id="sP">-</div></div></div>
<div class="mb-item"><span>🏆</span><div><div class="mb-label">Win</div><div class="mb-val mb-green" id="sW">-</div></div></div>
<div class="mb-item"><span>📉</span><div><div class="mb-label">Loss</div><div class="mb-val mb-red" id="sL">-</div></div></div>
</div>









<div class="card" style="margin-bottom:16px;padding:10px 14px">
<div style="font-size:11px;color:var(--text3);margin-bottom:6px">🔔 Recent Trades</div>
<div id="recentList" class="rb-list"></div>
</div>

<!-- Coin Tabs -->
<div class="tabs" id="cTabs" style="margin-bottom:12px"></div>

<!-- Two Charts Side by Side -->
<div class="charts-row">
<div class="chart-card">


</div>
<div class="chart-card">
<div class="card-title">📈 Live Price — <span id="cSym2" style="color:var(--blue)">-</span></div>
<div class="perf-legend">


<div class="perf-leg-item"><div class="perf-dot" style="background:#3b82f6"></div>Live: <span id="liveP" style="color:#3b82f6">-</span></div>
</div>
<canvas id="chart2canvas" style="width:100%;height:350px;display:block"></canvas>
</div>
</div>

<div class="card">
<div class="card-title">📋 Open Positions</div>
<div class="pnl-summary">
<span class="pnl-label" id="wLabel">0W</span>
<div class="pnl-bar-big" id="pBar"><div class="w"></div><div class="lo"></div></div>
<span class="pnl-label" id="lLabel">0L</span>
</div>
<div class="tbl-wrap">
<table><thead><tr>
<th>Coin</th><th>Profit</th><th>P&L</th><th>Buy</th><th>Current</th><th>Highest</th><th>Invested</th><th>Stop Loss</th><th>Status</th><th></th>
</tr></thead><tbody id="tBody"></tbody></table>
</div>
</div>

<div class="footer">MSA Trading Bot © 2025 • Dashboard v3 • Auto-refresh 10s</div>

<script>
var ch=null,cs=null,lineSeries=null,volSeries=null,bl=null,curSym=null;
var ch2=null,ch2js=null,bl2=null,bl3=null,bl4=null,liveTimer=null;
var lastNotifId=parseInt(localStorage.getItem('lastNId')||'0');
var coinColors={BTC:'#f7931a',ETH:'#627eea',SOL:'#9945ff',ADA:'#0033ad',XLM:'#14b6e7',DOT:'#e6007a',AVAX:'#e84142',LTC:'#bfbbbb',UNI:'#ff007a',LINK:'#2a5ada',FIL:'#0090ff',VET:'#15bdff',ETC:'#328332',ICP:'#29abe2',THETA:'#2ab8e6',HBAR:'#8a8a8a',DOGE:'#c3a634',XRP:'#00aae4',BNB:'#f3ba2f',ALGO:'#000',TRX:'#ff0013'};

var recentTrades=JSON.parse(localStorage.getItem('recentTrades')||'[]');
renderRecent();

setInterval(function(){document.getElementById('clk').textContent=new Date().toLocaleTimeString()},1000);

function addRecentTrade(type,symbol,price,time){
var coin=symbol.replace('/USDT','');
var isDup=recentTrades.some(function(t){return t.type===type&&t.coin===coin;});
if(isDup)return;
recentTrades.unshift({type:type,coin:coin,price:price,time:time||new Date().toLocaleTimeString()});
recentTrades=recentTrades.slice(0,5);
localStorage.setItem('recentTrades',JSON.stringify(recentTrades));
renderRecent();
}

function renderRecent(){
var rl=document.getElementById('recentList');
if(!rl)return;
if(recentTrades.length===0){
rl.innerHTML='<div class="rb-empty">Waiting for trades...</div>';
return;
}
var html='';
recentTrades.forEach(function(t){
var cls,typeCls,icon;
if(t.type==='BUY'){cls='rb-buy';typeCls='rb-type-buy';icon='🟢';}
else if(t.type==='TAKE PROFIT'){cls='rb-takeprofit';typeCls='rb-type-takeprofit';icon='💰';}
else if(t.type==='STOP LOSS'){cls='rb-stoploss';typeCls='rb-type-stoploss';icon='🛡️';}
else{cls='rb-sell';typeCls='rb-type-sell';icon='🔴';}
var priceStr=typeof t.price==='number'?'$'+t.price.toFixed(4):t.price;
html+='<div class="rb-item '+cls+'">';
html+='<span>'+icon+'</span>';
html+='<span class="rb-type '+typeCls+'">'+t.type+'</span>';
html+='<span class="rb-coin">'+t.coin+'</span>';
html+='<span class="rb-price">'+priceStr+'</span>';
html+='<span class="rb-time">'+t.time+'</span>';
html+='</div>';
});
rl.innerHTML=html;
}

var livePrices=[];
var maxPoints=120;
var candleData=[];
var curBuyPrice=0;

function initC(){
// Canvas charts - no init needed
}

function drawCandleChart(){
var canvas=document.getElementById('chartCanvas');
if(!canvas||candleData.length<2)return;
var rect=canvas.parentElement.getBoundingClientRect();
canvas.width=rect.width;
canvas.height=320;
var ctx=canvas.getContext('2d');
ctx.clearRect(0,0,canvas.width,canvas.height);

var raw=candleData.slice(-50);
var closes=raw.map(function(c){return c.close});
var median=closes.sort(function(a,b){return a-b})[Math.floor(closes.length/2)];
var candles=raw.filter(function(c){return c.low>median*0.5&&c.high<median*2;});
if(candles.length<2)candles=raw.slice(-20);
var allHigh=candles.map(function(c){return c.high});
var allLow=candles.map(function(c){return c.low});
var min=Math.min.apply(null,allLow);
var max=Math.max.apply(null,allHigh);
var range=max-min;
if(range===0)range=1;
var pad=25;
var W=canvas.width;
var H=canvas.height;
var drawW=(W-pad*2)*0.75;
var candleW=drawW/candles.length;
var bodyW=Math.max(candleW*0.7,2);

// Grid lines
ctx.strokeStyle='rgba(255,255,255,0.03)';
ctx.lineWidth=1;
for(var g=0;g<5;g++){
var gy=pad+(H-pad*2)*g/4;
ctx.beginPath();ctx.moveTo(pad,gy);ctx.lineTo(pad+drawW,gy);ctx.stroke();
}

// Draw candles
for(var i=0;i<candles.length;i++){
var c=candles[i];
var x=pad+i*candleW+candleW/2;
var isGreen=c.close>=c.open;
var color=isGreen?'#10b981':'#ef4444';

// Wick
var wickTop=H-pad-((c.high-min)/range)*(H-pad*2);
var wickBot=H-pad-((c.low-min)/range)*(H-pad*2);
ctx.beginPath();
ctx.strokeStyle=isGreen?'rgba(16,185,129,0.5)':'rgba(239,68,68,0.5)';
ctx.lineWidth=1;
ctx.moveTo(x,wickTop);
ctx.lineTo(x,wickBot);
ctx.stroke();

// Body
var bodyTop=H-pad-((Math.max(c.open,c.close)-min)/range)*(H-pad*2);
var bodyBot=H-pad-((Math.min(c.open,c.close)-min)/range)*(H-pad*2);
var bodyH=Math.max(bodyBot-bodyTop,1);
ctx.fillStyle=color;
ctx.fillRect(x-bodyW/2,bodyTop,bodyW,bodyH);
}

// Buy price line
if(curBuyPrice>0&&curBuyPrice>=min&&curBuyPrice<=max){
var buyY=H-pad-((curBuyPrice-min)/range)*(H-pad*2);
ctx.beginPath();
ctx.strokeStyle='#f59e0b';
ctx.lineWidth=1.5;
ctx.setLineDash([5,3]);
ctx.moveTo(pad,buyY);
ctx.lineTo(pad+drawW,buyY);
ctx.stroke();
ctx.setLineDash([]);

// Buy label
ctx.fillStyle='#f59e0b';
ctx.font='bold 10px JetBrains Mono';
ctx.textAlign='left';
ctx.fillText('Buy: $'+curBuyPrice.toFixed(2),pad+drawW+5,buyY+3);
}

// Price labels
ctx.fillStyle='rgba(255,255,255,0.4)';
ctx.font='10px JetBrains Mono';
ctx.textAlign='left';
ctx.fillText(max.toFixed(2),pad+drawW+5,pad+10);
ctx.fillText(min.toFixed(2),pad+drawW+5,H-pad-2);
}

function drawLiveChart(){
var canvas=document.getElementById('chart2canvas');
if(!canvas||livePrices.length<2)return;
var rect=canvas.parentElement.getBoundingClientRect();
canvas.width=rect.width;
canvas.height=350;
var ctx=canvas.getContext('2d');
ctx.clearRect(0,0,canvas.width,canvas.height);
var prices=livePrices;
var min=Math.min.apply(null,prices);
var max=Math.max.apply(null,prices);
var range=max-min||1;
var pad=25;
var W=canvas.width;
var H=canvas.height;
var drawW=(W-pad*2)*0.75;
ctx.strokeStyle='rgba(255,255,255,0.03)';
ctx.lineWidth=1;
for(var g=0;g<5;g++){var gy=pad+(H-pad*2)*g/4;ctx.beginPath();ctx.moveTo(pad,gy);ctx.lineTo(pad+drawW,gy);ctx.stroke();}
ctx.beginPath();
ctx.strokeStyle='#3b82f6';
ctx.lineWidth=2.5;
ctx.lineJoin='round';
ctx.lineCap='round';
var lastX=0,lastY=0;
for(var i=0;i<prices.length;i++){
var x=pad+i*(drawW/(prices.length-1||1));
var y=H-pad-((prices[i]-min)/range)*(H-pad*2);
if(i===0){ctx.moveTo(x,y);}
else{
var step=drawW/(prices.length-1||1);
var prevX=pad+(i-1)*step;
var prevY=H-pad-((prices[i-1]-min)/range)*(H-pad*2);
var t=0.4;
var dx=x-prevX;
var cp1x=prevX+dx*t;
var cp2x=x-dx*t;
var prevPrevY=prevY;
var nextY=y;
if(i>=2){prevPrevY=H-pad-((prices[i-2]-min)/range)*(H-pad*2);}
if(i<prices.length-1){nextY=H-pad-((prices[i+1]-min)/range)*(H-pad*2);}
var cp1y=prevY+(y-prevPrevY)*0.15;
var cp2y=y-(nextY-prevY)*0.15;
ctx.bezierCurveTo(cp1x,cp1y,cp2x,cp2y,x,y);
}
lastX=x;lastY=y;
}
ctx.stroke();
var grad=ctx.createLinearGradient(0,lastY,0,H);
grad.addColorStop(0,'rgba(59,130,246,0.15)');
grad.addColorStop(1,'rgba(59,130,246,0)');
ctx.lineTo(lastX,H-pad);
ctx.lineTo(pad,H-pad);
ctx.closePath();
ctx.fillStyle=grad;
ctx.fill();
ctx.beginPath();ctx.arc(lastX,lastY,10,0,Math.PI*2);ctx.fillStyle='rgba(59,130,246,0.2)';ctx.fill();
ctx.beginPath();ctx.arc(lastX,lastY,6,0,Math.PI*2);ctx.fillStyle='rgba(59,130,246,0.5)';ctx.fill();
ctx.beginPath();ctx.arc(lastX,lastY,3.5,0,Math.PI*2);ctx.fillStyle='#ffffff';ctx.fill();
ctx.fillStyle='rgba(255,255,255,0.4)';
ctx.font='10px JetBrains Mono';
ctx.textAlign='left';
ctx.fillText(max.toFixed(2),pad+drawW+5,pad+10);
ctx.fillText(min.toFixed(2),pad+drawW+5,H-pad-2);
}

function loadC(sym,bp,slP,tpP){
curSym=sym;
curBuyPrice=bp;
var coin=sym.replace('/USDT','');
var cc=coinColors[coin]||'#3b82f6';


document.getElementById('cSym2').textContent=coin+' Live';
document.getElementById('cSym2').style.color=cc;
document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('act',t.getAttribute('data-s')===sym)});
livePrices=[];candleData=[];var cvs=document.getElementById('chartCanvas');if(cvs){var ct2=cvs.getContext('2d');ct2.clearRect(0,0,cvs.width,cvs.height);}var cvs=document.getElementById('chart2canvas');if(cvs){var ct=cvs.getContext('2d');ct.clearRect(0,0,cvs.width,cvs.height);}
fetch('/api/chart/'+encodeURIComponent(sym))
.then(function(r){return r.json()})
.then(function(d){
if(d.candles&&d.candles.length>0){
candleData=d.candles;

if(liveTimer){clearInterval(liveTimer);}
liveTimer=setInterval(function(){
fetch('/api/live_price/'+encodeURIComponent(sym))
.then(function(r){return r.json()})
.then(function(ld){
if(ld.price>0){
// Update last candle
if(candleData.length>0){
var last=candleData[candleData.length-1];
last.close=ld.price;
if(ld.price>last.high)last.high=ld.price;
if(ld.price<last.low)last.low=ld.price;

}
// Update live line chart
livePrices.push(ld.price);
if(livePrices.length>maxPoints)livePrices.shift();
drawLiveChart();
document.getElementById('liveP').textContent='$'+ld.price.toFixed(4);
document.getElementById('liveP').style.color=ld.price>=bp?'#10b981':'#ef4444';
}
});
},1000);
}
});
}
function getMacroColor(s){
if(!s)return'var(--text3)';
s=s.toUpperCase();
if(s.indexOf('BULL')>=0)return'var(--green)';
if(s.indexOf('BEAR')>=0)return'var(--red)';
return'var(--yellow)';
}

function getMacroEmoji(s){
if(!s)return'⚪';
s=s.toUpperCase();
if(s.indexOf('BULL')>=0)return'🟢';
if(s.indexOf('BEAR')>=0)return'🔴';
return'🟡';
}

function fetchD(){
fetch('/api/data?after='+lastNotifId)
.then(function(r){return r.json()})
.then(function(d){
var s=d.summary;
var bs=d.bot_status||{};
var macro=bs.macro_status||'UNKNOWN';
var el=document.getElementById('mbMacro');
el.textContent=macro; // ✅ استخدام الماكرو مباشرة لأنه يحتوي بالفعل على الإيموجي
el.style.color=getMacroColor(macro);
document.getElementById('mbBal').textContent='$'+(bs.balance||0).toLocaleString(undefined,{maximumFractionDigits:2});
document.getElementById('mbInv').textContent='$'+(bs.invested||s.total_invested||0).toLocaleString(undefined,{maximumFractionDigits:2});
document.getElementById('mbLock').textContent='$'+(bs.locked_profit||0).toLocaleString(undefined,{maximumFractionDigits:2});
document.getElementById('mbTrad').textContent='$'+(bs.tradable||0).toLocaleString(undefined,{maximumFractionDigits:2});
document.getElementById('sA').textContent=s.active+'/'+s.max_positions;
var pnl=s.total_pnl;
var pEl=document.getElementById('sP');
pEl.textContent=(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
pEl.className='ms-val '+(pnl>=0?'val-green':'val-red');
document.getElementById('sW').textContent=s.winners;
document.getElementById('sL').textContent=s.losers;
document.getElementById('upd').textContent=d.last_update;
document.getElementById('wLabel').textContent=s.winners+'W';
document.getElementById('lLabel').textContent=s.losers+'L';
var tot=s.winners+s.losers||1;
document.getElementById('pBar').innerHTML='<div class="w" style="width:'+(s.winners/tot*100)+'%"></div><div class="lo" style="width:'+(s.losers/tot*100)+'%"></div>';
if(d.notifications&&d.notifications.length>0){
d.notifications.forEach(function(n){
addRecentTrade(n.type,n.symbol,n.price,n.time||new Date().toLocaleTimeString());
if(n.id>lastNotifId){lastNotifId=n.id;localStorage.setItem('lastNId',lastNotifId);}
});
}
var tb=document.getElementById('tBody');
tb.innerHTML='';
var tabs=document.getElementById('cTabs');
tabs.innerHTML='';
d.positions.forEach(function(p){
var coin=p.symbol.replace('/USDT','');
var cc=coinColors[coin]||'#3b82f6';
var pc=p.profit_pct>0?'pos':(p.profit_pct<0?'neg':'zer');
var uc=p.profit_usd>0?'pos':(p.profit_usd<0?'neg':'zer');
var bc='b-'+p.status_color;
var barW=Math.min(Math.abs(p.profit_pct)*20,100);
var barC=p.profit_pct>=0?'var(--green)':'var(--red)';
var slHtml='-';
if(p.sl_threshold>0){
slHtml='<div class="sl-info"><span class="sl-val">-'+p.sl_threshold.toFixed(1)+'%</span>';
if(p.sl_trigger>0){slHtml+='<br><span class="sl-trigger">Trigger: -'+p.sl_trigger.toFixed(1)+'%</span>';}
slHtml+='</div>';
}
var row=document.createElement('tr');
row.innerHTML='<td><div class="coin-name"><div class="coin-icon" style="background:'+cc+'15;color:'+cc+'">'+coin.substring(0,3)+'</div><div><div class="coin-sym">'+coin+'</div><div class="coin-pair">USDT</div></div></div></td>'+'<td class="'+pc+'">'+(p.profit_pct>0?'+':'')+p.profit_pct.toFixed(2)+'%<div class="pnl-bar"><div class="pnl-fill" style="width:'+barW+'%;background:'+barC+'"></div></div></td>'+'<td class="'+uc+'">'+(p.profit_usd>=0?'+$':'-$')+Math.abs(p.profit_usd).toFixed(2)+'</td>'+'<td>$'+fmtP(p.buy_price)+'</td>'+'<td>$'+fmtP(p.current_price)+'</td>'+'<td>$'+fmtP(p.highest_price)+'</td>'+'<td>$'+p.invested.toFixed(0)+'</td>'+'<td>'+slHtml+'</td>'+'<td><span class="badge '+bc+'">'+p.status+'</span></td>'+'<td></td>';
var btn=document.createElement('button');
btn.className='chart-btn';btn.textContent='📈';
btn.onclick=function(){loadC(p.symbol,p.buy_price,p.sl_price||0,p.tp_price||0)};
row.lastChild.appendChild(btn);
tb.appendChild(row);
var tabBtn=document.createElement('button');
tabBtn.className='tab'+(p.symbol===curSym?' act':'');
tabBtn.setAttribute('data-s',p.symbol);
tabBtn.textContent=coin;
tabBtn.style.borderColor=cc+'30';
tabBtn.onclick=function(){loadC(p.symbol,p.buy_price,p.sl_price||0,p.tp_price||0)};
tabs.appendChild(tabBtn);
});
if(!curSym&&d.positions.length>0){var fp=d.positions[0];loadC(fp.symbol,fp.buy_price,fp.sl_price||0,fp.tp_price||0)}
});
}

function fmtP(p){
if(p>=1000)return p.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
if(p>=1)return p.toFixed(4);
if(p>=0.01)return p.toFixed(6);
return p.toFixed(8);
}

document.addEventListener('DOMContentLoaded',function(){initC();fetchD();setInterval(fetchD,10000);});
</script>
</body></html>"""


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
