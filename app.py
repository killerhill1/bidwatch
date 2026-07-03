"""
BidWatch — Courtman Enterprises LLC
v5: proper Flask startup, safe lxml fallback, verbose logging
"""

# ── Logging first — before anything else so we catch all errors ───────────────
import logging
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("bidwatch")
log.info("BidWatch v5 starting — logging active")

# ── Safe lxml fallback ────────────────────────────────────────────────────────
try:
    import lxml
    HTML_PARSER = "lxml"
    log.info("lxml available — using lxml parser")
except ImportError:
    HTML_PARSER = "html.parser"
    log.info("lxml not available — using html.parser (this is fine)")

# ── Standard imports ──────────────────────────────────────────────────────────
try:
    from flask import Flask, jsonify, render_template_string, request
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    import json, os, re, threading, schedule, time
    from datetime import datetime
    from pathlib import Path
    log.info("All imports successful")
except Exception as e:
    log.critical(f"Import failed: {e}")
    raise

# ── Storage ───────────────────────────────────────────────────────────────────
DATA_DIR = Path("/tmp/bidwatch")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Data directory ready: {DATA_DIR}")
except Exception as e:
    log.warning(f"Could not create {DATA_DIR}: {e} — using /tmp")
    DATA_DIR = Path("/tmp")

DATA_FILE = DATA_DIR / "bids.json"
SEEN_FILE = DATA_DIR / "seen.json"
lock      = threading.Lock()

# ── Config ────────────────────────────────────────────────────────────────────
ROOFING_KEYWORDS = [
    "roof", "roofing", "slate", "shingle", "membrane", "flashing",
    "gutter", "copper", "waterproof", "tpo", "epdm", "flat roof",
    "sheet metal", "soffit", "fascia", "historic roof", "re-roof"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
}

TOWNS = [
    ("East Hartford",  "https://www.easthartfordct.gov/bids",                          "Town of East Hartford"),
    ("Manchester",     "https://www.manchesterct.gov/government/departments/general-services/purchasing", "Town of Manchester"),
    ("Meriden",        "https://www.meridenct.gov/business/bids-rfps/",                "City of Meriden"),
    ("Berlin",         "https://www.berlinct.gov/bids",                                "Town of Berlin"),
    ("Glastonbury",    "https://www.glastonburyct.gov/bids-rfps",                      "Town of Glastonbury"),
    ("Enfield",        "https://www.enfield-ct.gov/Bids.aspx",                         "Town of Enfield"),
    ("Wethersfield",   "https://www.wethersfieldct.gov/bids",                          "Town of Wethersfield"),
    ("Newington",      "https://www.newingtonct.gov/bids",                             "Town of Newington"),
    ("Windsor",        "https://www.townofwindsor.com/Bids.aspx",                      "Town of Windsor"),
    ("Bloomfield",     "https://www.bloomfieldct.gov/Bids.aspx",                       "Town of Bloomfield"),
    ("Avon",           "https://www.avon-ct.gov/bids",                                 "Town of Avon"),
    ("Farmington",     "https://www.farmington-ct.org/bids",                           "Town of Farmington"),
    ("Windsor Locks",  "https://www.windsorlocksct.org/Bids.aspx",                     "Town of Windsor Locks"),
    ("Southington",    "https://www.southingtonct.gov/Bids.aspx",                      "Town of Southington"),
    ("Vernon",         "https://www.vernon-ct.gov/government/bids-and-contracts",      "Town of Vernon"),
    ("Tolland",        "https://www.tolland.org/Bids.aspx",                            "Town of Tolland"),
    ("Middletown",     "https://www.middletownct.gov/Bids.aspx",                       "City of Middletown"),
    ("Bristol",        "https://www.bristolct.gov/Bids.aspx",                          "City of Bristol"),
    ("New Britain",    "https://www.newbritainct.gov/Bids.aspx",                       "City of New Britain"),
]

# ── HTTP session with retries ─────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# ── Data helpers ──────────────────────────────────────────────────────────────
def is_roofing(text):
    return any(kw in text.lower() for kw in ROOFING_KEYWORDS)

def clean(text):
    return re.sub(r'\s+', ' ', text or "").strip()

def load_bids():
    with lock:
        try:
            if DATA_FILE.exists():
                return json.loads(DATA_FILE.read_text())
        except Exception as e:
            log.error(f"load_bids: {e}")
        return []

def save_bids(bids):
    with lock:
        try:
            tmp = DATA_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(bids, indent=2))
            tmp.replace(DATA_FILE)
        except Exception as e:
            log.error(f"save_bids: {e}")

def load_seen():
    with lock:
        try:
            if SEEN_FILE.exists():
                return set(json.loads(SEEN_FILE.read_text()))
        except Exception as e:
            log.error(f"load_seen: {e}")
        return set()

def save_seen(seen):
    with lock:
        try:
            tmp = SEEN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(seen)))
            tmp.replace(SEEN_FILE)
        except Exception as e:
            log.error(f"save_seen: {e}")

# ── Scrapers ──────────────────────────────────────────────────────────────────
def scrape_ctsource():
    bids = []
    seen_ids = set()
    session = make_session()

    for kw in ["roofing", "roof replacement", "slate", "membrane roof"]:
        try:
            r = session.get(
                "https://www.biznet.ct.gov/SCP_Search/BidResults.aspx",
                params={"TN": kw, "CT": "B"},
                headers=HEADERS, timeout=25
            )
            r.raise_for_status()
            soup = BeautifulSoup(r.text, HTML_PARSER)
            rows = soup.select("table tr")[1:]
            log.debug(f"CT Source '{kw}': {len(rows)} rows returned")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue
                title = clean(cols[1].get_text())
                if not title or not is_roofing(title):
                    continue
                bid_id = f"ct_{clean(cols[0].get_text())}_{abs(hash(title))}"
                if bid_id in seen_ids:
                    continue
                seen_ids.add(bid_id)
                link_tag = cols[1].find("a")
                link = ("https://www.biznet.ct.gov" + link_tag["href"]) if link_tag and link_tag.get("href") else "https://portal.ct.gov/DAS/CTSource/BidBoard"
                bids.append({
                    "id": bid_id, "title": title,
                    "org": clean(cols[2].get_text()) if len(cols) > 2 else "CT Agency",
                    "source": "CT Source",
                    "deadline": clean(cols[3].get_text()) if len(cols) > 3 else "",
                    "value": None, "link": link, "status": "new",
                    "found": datetime.now().isoformat()
                })
        except Exception as e:
            log.warning(f"BizNet ({kw}): {e}")

    try:
        r = session.get("https://portal.ct.gov/das/construction-services/bidboard", headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, HTML_PARSER)
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text())
            if len(text) < 15 or not is_roofing(text):
                continue
            bid_id = f"das_{abs(hash(text))}"
            if bid_id in seen_ids:
                continue
            seen_ids.add(bid_id)
            href = a["href"]
            if not href.startswith("http"):
                href = "https://portal.ct.gov" + href
            bids.append({
                "id": bid_id, "title": text[:200],
                "org": "CT Dept of Administrative Services",
                "source": "CT Source", "deadline": "", "value": None,
                "link": href, "status": "new", "found": datetime.now().isoformat()
            })
    except Exception as e:
        log.warning(f"DAS board: {e}")

    log.info(f"CT Source total: {len(bids)} bids")
    return bids

def scrape_town(name, url, org):
    bids = []
    seen_ids = set()
    session = make_session()
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, HTML_PARSER)
        for tag in soup.find_all(["a", "li", "tr", "p", "div"]):
            text = clean(tag.get_text())
            if len(text) < 15 or len(text) > 300 or not is_roofing(text):
                continue
            bid_id = f"{name.lower().replace(' ','_')}_{abs(hash(text))}"
            if bid_id in seen_ids:
                continue
            seen_ids.add(bid_id)
            a = tag if tag.name == "a" else tag.find("a")
            link = url
            if a and a.get("href"):
                href = a["href"]
                if href.startswith("http"):
                    link = href
                elif href.startswith("/"):
                    from urllib.parse import urlparse
                    base = urlparse(url)
                    link = f"{base.scheme}://{base.netloc}{href}"
            bids.append({
                "id": bid_id, "title": text[:200], "org": org,
                "source": "Town", "deadline": "", "value": None,
                "link": link, "status": "new", "found": datetime.now().isoformat()
            })
        if bids:
            log.info(f"{name}: {len(bids[:8])} bids")
        return bids[:8]
    except Exception as e:
        log.warning(f"{name}: {e}")
        return []

def run_scraper():
    log.info("=== Scraper run starting ===")
    try:
        seen    = load_seen()
        current = {b["id"]: b for b in load_bids()}
        fresh   = scrape_ctsource()
        for name, url, org in TOWNS:
            fresh += scrape_town(name, url, org)
        new_bids = [b for b in fresh if b["id"] not in seen]
        log.info(f"Total scraped: {len(fresh)} | New this run: {len(new_bids)}")
        for b in fresh:
            if b["id"] not in current:
                current[b["id"]] = b
        if new_bids:
            seen.update(b["id"] for b in new_bids)
            save_seen(seen)
        save_bids(list(current.values())[:500])
        log.info("=== Scraper run complete ===")
    except Exception as e:
        log.error(f"Scraper run failed: {e}", exc_info=True)

# ── Flask app ─────────────────────────────────────────────────────────────────
log.info("Creating Flask app...")
app = Flask(__name__)

# ── Gunicorn + python-safe startup using app context ─────────────────────────
_started = False

def start_background_tasks():
    global _started
    if _started:
        return
    _started = True
    log.info("Starting background tasks (scraper + scheduler)...")

    def scheduler_loop():
        schedule.every().day.at("06:00").do(run_scraper)
        schedule.every().day.at("18:00").do(run_scraper)
        log.info("Scheduler ready — runs at 06:00 and 18:00 daily")
        while True:
            schedule.run_pending()
            time.sleep(60)

    threading.Thread(target=run_scraper,    daemon=True, name="initial-scrape").start()
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()

# Works with both gunicorn and python app.py
with app.app_context():
    start_background_tasks()

# ── API routes ────────────────────────────────────────────────────────────────
@app.route("/api/bids")
def api_bids():
    return jsonify(load_bids())

@app.route("/api/status/<bid_id>", methods=["POST"])
def api_status(bid_id):
    data = request.json
    bids = load_bids()
    for b in bids:
        if b["id"] == bid_id:
            b["status"] = data.get("status", "new")
            b["notes"]  = data.get("notes", b.get("notes", ""))
    save_bids(bids)
    return jsonify({"ok": True})

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    threading.Thread(target=run_scraper, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stats")
def api_stats():
    bids = load_bids()
    return jsonify({
        "total":     len(bids),
        "ct_source": sum(1 for b in bids if b["source"] == "CT Source"),
        "town":      sum(1 for b in bids if b["source"] == "Town"),
        "last_run":  datetime.now().strftime("%b %d, %I:%M %p")
    })

@app.route("/awards")
def awards():
    f = Path(__file__).parent / "awards.html"
    return f.read_text() if f.exists() else "<h1>Awards page coming soon</h1>"

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD)

DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BidWatch - Courtman Enterprises</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;500;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1117;--surface:#171b26;--surface2:#1e2332;--border:#2a3048;--accent:#f59e0b;--blue:#3b82f6;--green:#10b981;--red:#ef4444;--purple:#8b5cf6;--text:#e8eaf0;--text2:#8892a4;--text3:#4a5568;--mono:'IBM Plex Mono',monospace;--sans:'Inter',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;display:flex;align-items:center;justify-content:space-between;height:56px;position:sticky;top:0;z-index:100}
.logo{display:flex;align-items:center;gap:10px;font-family:var(--mono);font-weight:600;font-size:15px}
.logo-icon{width:28px;height:28px;background:var(--accent);border-radius:6px;display:flex;align-items:center;justify-content:center}
.hdr-right{display:flex;align-items:center;gap:10px}
.live{font-family:var(--mono);font-size:11px;color:var(--green);display:flex;align-items:center;gap:5px}
.live::before{content:'';width:6px;height:6px;background:var(--green);border-radius:50%;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.btn{background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text2);font-family:var(--mono);font-size:11px;padding:6px 12px;cursor:pointer;text-decoration:none;display:inline-block}
.btn:hover{color:var(--text)}
.btn-blue{background:rgba(59,130,246,.15);color:var(--blue);border-color:rgba(59,130,246,.3)}
.layout{display:grid;grid-template-columns:200px 1fr;min-height:calc(100vh - 56px)}
.sidebar{background:var(--surface);border-right:1px solid var(--border);padding:16px 0;position:sticky;top:56px;height:calc(100vh - 56px);overflow-y:auto}
.sb-sec{padding:0 14px 16px;border-bottom:1px solid var(--border);margin-bottom:16px}
.sb-lbl{font-family:var(--mono);font-size:10px;font-weight:600;color:var(--text3);letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}
.fb{display:flex;align-items:center;justify-content:space-between;width:100%;padding:6px 8px;border-radius:6px;border:none;background:transparent;color:var(--text2);font-size:12px;font-family:var(--sans);cursor:pointer;margin-bottom:2px;text-align:left;transition:all .15s}
.fb:hover,.fb.active{background:var(--surface2);color:var(--text)}
.fb .cnt{font-family:var(--mono);font-size:10px;background:var(--surface2);padding:1px 6px;border-radius:10px}
.fb.active .cnt{background:var(--accent);color:#000}
.tc{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text2);padding:3px 0;border-bottom:1px solid var(--border)}
.tc:last-child{border:none}
.dot{width:5px;height:5px;border-radius:50%;background:var(--green);flex-shrink:0}
.main{padding:20px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.slbl{font-family:var(--mono);font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.sval{font-family:var(--mono);font-size:22px;font-weight:600}
.ssub{font-size:11px;color:var(--text3);margin-top:3px}
.toolbar{display:flex;gap:10px;margin-bottom:14px;align-items:center}
.sw{position:relative;flex:1}
.sw input{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px 8px 34px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none}
.sw input:focus{border-color:var(--accent)}
.swi{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--text3)}
.tabs{display:flex;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:3px;gap:2px}
.tab{padding:5px 12px;border-radius:5px;border:none;background:transparent;color:var(--text2);font-size:12px;font-family:var(--sans);cursor:pointer;white-space:nowrap}
.tab.active{background:var(--accent);color:#000;font-weight:600}
.tbl-wrap{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse}
thead tr{border-bottom:1px solid var(--border);background:var(--surface2)}
th{padding:9px 14px;font-family:var(--mono);font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;text-align:left;white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
tbody tr:last-child{border:none}
tbody tr:hover{background:var(--surface2)}
td{padding:11px 14px;vertical-align:middle}
.bt{font-weight:500;color:var(--text);font-size:13px;max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bo{font-family:var(--mono);font-size:10px;color:var(--text2);margin-top:2px}
.badge{display:inline-flex;padding:2px 7px;border-radius:4px;font-family:var(--mono);font-size:10px;font-weight:600}
.bct{background:rgba(139,92,246,.15);color:var(--purple);border:1px solid rgba(139,92,246,.3)}
.btown{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.dl{font-family:var(--mono);font-size:12px;white-space:nowrap}
.dlu{color:var(--red)}.dls{color:var(--accent)}.dlo{color:var(--green)}
.ssel{background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:var(--mono);font-size:11px;padding:4px 7px;cursor:pointer;outline:none}
.empty{text-align:center;padding:60px;color:var(--text2)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;backdrop-filter:blur(2px)}
.overlay.open{display:flex;align-items:flex-start;justify-content:flex-end}
.panel{background:var(--surface);border-left:1px solid var(--border);width:460px;height:100vh;overflow-y:auto;padding:22px;animation:si .2s ease}
@keyframes si{from{transform:translateX(30px);opacity:0}}
.pc{background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text2);cursor:pointer;padding:5px 12px;font-size:12px;font-family:var(--mono);margin-bottom:18px}
.pt{font-size:16px;font-weight:600;line-height:1.4;margin-bottom:5px}
.po{color:var(--text2);font-size:13px;margin-bottom:18px}
.dg{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.df{background:var(--surface2);border-radius:8px;padding:11px}
.dfl{font-family:var(--mono);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.dfv{font-size:13px;font-weight:500}
.psec{font-family:var(--mono);font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.8px;margin:14px 0 7px}
.nb{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:var(--sans);font-size:13px;padding:10px;resize:vertical;min-height:80px;outline:none;margin-top:6px}
.nb:focus{border-color:var(--accent)}
.pa{display:flex;gap:8px;margin-top:16px}
.bp{flex:1;background:var(--accent);color:#000;border:none;border-radius:8px;padding:10px;font-weight:600;font-size:13px;cursor:pointer;font-family:var(--sans)}
.bs{background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer;font-family:var(--sans)}
.toast{position:fixed;bottom:24px;right:24px;background:var(--green);color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
@media(max-width:768px){.layout{grid-template-columns:1fr}.sidebar{display:none}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header class="hdr">
  <div class="logo"><div class="logo-icon">🏗</div>BidWatch<span style="font-size:11px;color:var(--text3);font-weight:400;margin-left:4px">· Courtman Enterprises</span></div>
  <div class="hdr-right">
    <span class="live">LIVE</span>
    <span style="font-family:var(--mono);font-size:11px;color:var(--text2)" id="last-run">-</span>
    <a href="/awards" class="btn btn-blue">🏆 Award Intel</a>
    <button class="btn" onclick="go()">↻ Refresh Now</button>
  </div>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="sb-sec">
      <div class="sb-lbl">Source</div>
      <button class="fb active" onclick="setSrc('all',this)">All Sources <span class="cnt" id="ca">0</span></button>
      <button class="fb" onclick="setSrc('CT Source',this)">CT Source <span class="cnt" id="cct">0</span></button>
      <button class="fb" onclick="setSrc('Town',this)">Town Pages <span class="cnt" id="ctw">0</span></button>
    </div>
    <div class="sb-sec">
      <div class="sb-lbl">Deadline</div>
      <button class="fb" onclick="setDl('urgent',this)">🔴 Under 7 days <span class="cnt" id="cu">0</span></button>
      <button class="fb" onclick="setDl('soon',this)">🟡 7-14 days <span class="cnt" id="cs">0</span></button>
      <button class="fb" onclick="setDl('ok',this)">🟢 14+ days <span class="cnt" id="co">0</span></button>
    </div>
    <div class="sb-sec">
      <div class="sb-lbl">Monitored Towns</div>
      <div id="tlist" style="margin-top:4px"></div>
    </div>
  </aside>
  <main class="main">
    <div class="stats">
      <div class="stat"><div class="slbl">Open Bids</div><div class="sval" id="st">-</div><div class="ssub">all sources</div></div>
      <div class="stat"><div class="slbl">Due This Week</div><div class="sval" style="color:var(--red)" id="su">-</div><div class="ssub">act fast</div></div>
      <div class="stat"><div class="slbl">CT Source</div><div class="sval" style="color:var(--purple)" id="sct">-</div><div class="ssub">262 CT entities</div></div>
      <div class="stat"><div class="slbl">Town Pages</div><div class="sval" style="color:var(--green)" id="stw">-</div><div class="ssub">Hartford region</div></div>
    </div>
    <div class="toolbar">
      <div class="sw"><span class="swi">🔍</span><input id="search" placeholder="Search bids, towns, keywords..." oninput="render()"></div>
      <div class="tabs">
        <button class="tab active" onclick="setSt('all',this)">All</button>
        <button class="tab" onclick="setSt('new',this)">New</button>
        <button class="tab" onclick="setSt('reviewing',this)">Reviewing</button>
        <button class="tab" onclick="setSt('bidding',this)">Bidding</button>
        <button class="tab" onclick="setSt('submitted',this)">Submitted</button>
      </div>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>Bid / Organization</th><th>Source</th><th>Deadline</th><th>Found</th><th>Status</th></tr></thead>
        <tbody id="tb"><tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text2)"><span class="spin"></span>Loading bids...</td></tr></tbody>
      </table>
    </div>
  </main>
</div>
<div class="overlay" id="ov" onclick="closeP(event)">
  <div class="panel"><button class="pc" onclick="closeP()">← Back</button><div id="pc"></div></div>
</div>
<div class="toast" id="toast"></div>
<script>
const TOWNS=['Hartford','West Hartford','East Hartford','Manchester','Meriden','Berlin','Glastonbury','Enfield','Wethersfield','Newington','Windsor','Bloomfield','Avon','Farmington','Windsor Locks','Southington','Vernon','Tolland','Middletown','Bristol','New Britain'];
let bids=[],src='all',status='all',dl=null;
document.getElementById('tlist').innerHTML=TOWNS.map(t=>`<div class="tc"><span class="dot"></span><span>${t}</span></div>`).join('');
function du(d){if(!d)return null;try{return Math.ceil((new Date(d.slice(0,10))-new Date())/86400000)}catch{return null}}
function dc(d){if(d===null)return'';if(d<7)return'dlu';if(d<=14)return'dls';return'dlo'}
function fd(d){const v=du(d);if(!d||v===null)return'<span style="color:var(--text3)">-</span>';const l=new Date(d.slice(0,10)).toLocaleDateString('en-US',{month:'short',day:'numeric'});const t=v<0?'Closed':v===0?'TODAY':v+'d left';return`<span class="dl ${dc(v)}">${l}<br><span style="font-size:10px">${t}</span></span>`}
function badge(s){return s==='CT Source'?'<span class="badge bct">CT Source</span>':'<span class="badge btown">Town</span>'}
function ff(i){if(!i)return'-';try{return new Date(i).toLocaleDateString('en-US',{month:'short',day:'numeric'})}catch{return'-'}}
function counts(){
  document.getElementById('ca').textContent=bids.length;
  document.getElementById('cct').textContent=bids.filter(b=>b.source==='CT Source').length;
  document.getElementById('ctw').textContent=bids.filter(b=>b.source==='Town').length;
  const u=bids.filter(b=>{const d=du(b.deadline);return d!==null&&d<7&&d>=0}).length;
  document.getElementById('cu').textContent=u;
  document.getElementById('cs').textContent=bids.filter(b=>{const d=du(b.deadline);return d!==null&&d>=7&&d<=14}).length;
  document.getElementById('co').textContent=bids.filter(b=>{const d=du(b.deadline);return d!==null&&d>14}).length;
  document.getElementById('st').textContent=bids.length;
  document.getElementById('su').textContent=u;
  document.getElementById('sct').textContent=bids.filter(b=>b.source==='CT Source').length;
  document.getElementById('stw').textContent=bids.filter(b=>b.source==='Town').length;
}
function render(){
  const q=document.getElementById('search').value.toLowerCase();
  let b=[...bids];
  if(src!=='all')b=b.filter(x=>x.source===src);
  if(status!=='all')b=b.filter(x=>(x.status||'new')===status);
  if(dl==='urgent')b=b.filter(x=>{const d=du(x.deadline);return d!==null&&d<7&&d>=0});
  if(dl==='soon')b=b.filter(x=>{const d=du(x.deadline);return d!==null&&d>=7&&d<=14});
  if(dl==='ok')b=b.filter(x=>{const d=du(x.deadline);return d!==null&&d>14});
  if(q)b=b.filter(x=>(x.title||'').toLowerCase().includes(q)||(x.org||'').toLowerCase().includes(q));
  b.sort((a,x)=>(du(a.deadline)??9999)-(du(x.deadline)??9999));
  const tb=document.getElementById('tb');
  if(!b.length){tb.innerHTML='<tr><td colspan="5"><div class="empty">No bids match your filters</div></td></tr>';return}
  tb.innerHTML=b.map(x=>{const s=x.status||'new';return`<tr onclick="openP('${x.id}')">
    <td><div class="bt" title="${x.title}">${x.title}</div><div class="bo">${x.org}</div></td>
    <td>${badge(x.source)}</td><td>${fd(x.deadline)}</td>
    <td style="font-family:var(--mono);font-size:11px;color:var(--text2)">${ff(x.found)}</td>
    <td onclick="event.stopPropagation()">
      <select class="ssel" onchange="saveSt('${x.id}',this.value)">
        <option value="new" ${s==='new'?'selected':''}>New</option>
        <option value="reviewing" ${s==='reviewing'?'selected':''}>Reviewing</option>
        <option value="bidding" ${s==='bidding'?'selected':''}>Bidding</option>
        <option value="submitted" ${s==='submitted'?'selected':''}>Submitted</option>
        <option value="won" ${s==='won'?'selected':''}>Won</option>
        <option value="lost" ${s==='lost'?'selected':''}>Lost</option>
      </select></td></tr>`}).join('');
}
function setSrc(s,b){src=s;dl=null;document.querySelectorAll('.sb-sec .fb').forEach(x=>x.classList.remove('active'));b.classList.add('active');render()}
function setDl(d,b){dl=dl===d?null:d;document.querySelectorAll('.sb-sec .fb').forEach(x=>x.classList.remove('active'));if(dl)b.classList.add('active');else document.querySelector('.fb').classList.add('active');render()}
function setSt(s,b){status=s;document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');render()}
function saveSt(id,val){const b=bids.find(x=>x.id===id);if(b)b.status=val;fetch(`/api/status/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:val,notes:b?.notes||''})});render()}
function openP(id){
  const b=bids.find(x=>x.id===id);if(!b)return;
  const v=du(b.deadline),c=dc(v);
  document.getElementById('pc').innerHTML=`
    <div style="margin-bottom:8px">${badge(b.source)}</div>
    <div class="pt">${b.title}</div><div class="po">${b.org}</div>
    <div class="dg">
      <div class="df"><div class="dfl">Deadline</div><div class="dfv ${c}">${b.deadline?new Date(b.deadline.slice(0,10)).toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'}):'Not listed'}</div></div>
      <div class="df"><div class="dfl">Days Left</div><div class="dfv ${c}">${v===null?'-':v<0?'Closed':v===0?'TODAY':v+' days'}</div></div>
      <div class="df"><div class="dfl">Found</div><div class="dfv">${ff(b.found)}</div></div>
      <div class="df"><div class="dfl">Source</div><div class="dfv">${b.source}</div></div>
    </div>
    <div class="psec">Your Status</div>
    <select class="ssel" style="width:100%;padding:8px 10px;font-size:12px" onchange="saveSt('${b.id}',this.value)">
      <option value="new" ${(b.status||'new')==='new'?'selected':''}>New</option>
      <option value="reviewing" ${b.status==='reviewing'?'selected':''}>Reviewing</option>
      <option value="bidding" ${b.status==='bidding'?'selected':''}>Bidding</option>
      <option value="submitted" ${b.status==='submitted'?'selected':''}>Submitted</option>
      <option value="won" ${b.status==='won'?'selected':''}>Won</option>
      <option value="lost" ${b.status==='lost'?'selected':''}>Lost</option>
    </select>
    <div class="psec">Notes</div>
    <textarea class="nb" id="nb" placeholder="Add notes...">${b.notes||''}</textarea>
    <div class="pa">
      <a href="${b.link}" target="_blank" style="flex:1;text-decoration:none"><button class="bp" style="width:100%">View Official Bid</button></a>
      <button class="bs" onclick="saveN('${b.id}')">Save Notes</button>
      <button class="bs" onclick="closeP()">Close</button>
    </div>`;
  document.getElementById('ov').classList.add('open');
}
function saveN(id){const n=document.getElementById('nb').value;const b=bids.find(x=>x.id===id);if(b)b.notes=n;fetch(`/api/status/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:b?.status||'new',notes:n})});toast('Notes saved')}
function closeP(e){if(e&&e.target!==document.getElementById('ov'))return;document.getElementById('ov').classList.remove('open')}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
async function go(){toast('Scraper started...');await fetch('/api/scrape',{method:'POST'});setTimeout(load,25000)}
async function load(){
  try{
    const[a,b]=await Promise.all([fetch('/api/bids'),fetch('/api/stats')]);
    bids=await a.json();const s=await b.json();
    document.getElementById('last-run').textContent='Updated '+s.last_run;
    counts();render();
  }catch(e){document.getElementById('tb').innerHTML='<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text2)">Could not load bids</td></tr>'}
}
load();setInterval(load,5*60*1000);
</script>
</body>
</html>"""

log.info("App ready")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
