"""
BidWatch — Courtman Enterprises LLC
v8: clean rewrite — universal row search, stealth headers, verified URLs, expanded keywords
"""

import logging, sys
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("bidwatch")
log.info("BidWatch v8 starting")

from flask import Flask, jsonify, render_template_string, request
import requests, json, os, re, threading, schedule, time
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from pathlib import Path
import random

log.info("Imports OK")

# ── Storage ───────────────────────────────────────────────────────────────────
DATA_DIR  = Path("/tmp/bidwatch")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "bids.json"
SEEN_FILE = DATA_DIR / "seen.json"
lock      = threading.Lock()

# ── Keywords — expanded ───────────────────────────────────────────────────────
ROOFING_KEYWORDS = [
    "roof", "roofing", "re-roof", "reroof",
    "slate", "shingle", "shingles",
    "membrane", "tpo", "epdm", "modified bitumen",
    "flashing", "sheet metal",
    "gutter", "gutters", "fascia", "soffit",
    "copper", "waterproof", "waterproofing",
    "flat roof", "low slope", "standing seam",
    "historic roof", "restoration"
]

# ── User-Agent rotation ───────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def get_headers(referer=None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        h["Referer"] = referer
    return h

# ── Verified town URLs ────────────────────────────────────────────────────────
TOWNS = [
    # Confirmed working
    ("Meriden",       "https://www.meridenct.gov/business/bids-rfps/",                       "City of Meriden"),
    ("Enfield",       "https://www.enfield-ct.gov/Bids.aspx",                                "Town of Enfield"),
    ("Vernon",        "https://www.vernon-ct.gov/government/bids-and-contracts",             "Town of Vernon"),
    ("Bloomfield",    "https://www.bloomfieldct.gov/Bids.aspx",                              "Town of Bloomfield"),
    ("Middletown",    "https://www.middletownct.gov/Bids.aspx",                              "City of Middletown"),
    ("Bristol",       "https://www.bristolct.gov/bids.aspx",                                 "City of Bristol"),
    ("Newington",     "https://www.newingtonct.gov/Bids.aspx",                               "Town of Newington"),
    ("Windsor",       "https://www.windsorct.gov/bids.aspx",                                 "Town of Windsor"),
    ("Wethersfield",  "https://www.wethersfieldct.gov/332/Current-Open-Bids",               "Town of Wethersfield"),
    ("New Britain",   "https://www.newbritainct.gov/services/purchasing/bidshtm",            "City of New Britain"),
    ("Southington",   "https://www.southingtonct.gov/departments/engineering_department/bid_invitations.php", "Town of Southington"),
    ("Granby",        "https://www.granby-ct.gov/Bids.aspx",                                 "Town of Granby"),
    ("Berlin",        "https://www.berlinct.gov/topic/subtopic.php?topicid=412&structureid=123", "Town of Berlin"),
    ("Windsor Locks", "https://windsorlocksct.org/bidding-opportunities/",                   "Town of Windsor Locks"),
    ("Cromwell",      "https://www.cromwellct.com/bids",                                     "Town of Cromwell"),
    ("Canton",        "https://www.townofcantonct.org/active-bids",                          "Town of Canton"),
    # User-verified URLs
    ("Farmington",    "https://www.farmington-ct.org/departments/finance-purchasing/purchasing/bids", "Town of Farmington"),
    ("Manchester",    "https://www.manchesterct.gov/Government/Departments/Purchasing/BIDS", "Town of Manchester"),
    # Massachusetts cities — high roofing bid volume
    ("Springfield MA",  "https://www.springfield-ma.gov/finance/procurement-bids/open_bids.php", "City of Springfield MA"),
    ("Worcester MA",    "https://www.worcesterma.gov/bids",                                       "City of Worcester MA"),
    ("Providence RI",   "https://www.providenceri.gov/purchasing/bid-invitations/",               "City of Providence RI"),
]

# ── Junk phrases to skip ──────────────────────────────────────────────────────
JUNK = [
    "sign in", "create account", "printer friendly", "email page",
    "site map", "translate", "my account", "facebook", "twitter",
    "pinterest", "linkedin", "instagram", "subscribe", "copyright",
    "powered by", "skip to", "delicious", "blogger", "youtube",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_roofing(text):
    t = text.lower()
    return any(kw in t for kw in ROOFING_KEYWORDS)

def is_junk(text):
    t = text.lower()
    if any(j in t for j in JUNK):
        return True
    words = t.split()
    if len(words) > 8 and len(set(words)) < len(words) * 0.5:
        return True
    return False

def clean(text):
    return re.sub(r'\s+', ' ', text or "").strip()

def make_session():
    s = requests.Session()
    r = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://",  HTTPAdapter(max_retries=r))
    s.mount("https://", HTTPAdapter(max_retries=r))
    return s

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

# ── SAM.gov scraper ───────────────────────────────────────────────────────────
def scrape_samgov():
    api_key = os.environ.get("SAM_API_KEY", "")
    if not api_key:
        log.warning("SAM_API_KEY not set — skipping federal bids")
        return []

    bids = []
    seen_ids = set()
    session = make_session()

    from datetime import timedelta
    today     = datetime.now()
    from_date = (today - timedelta(days=90)).strftime("%m/%d/%Y")
    to_date   = today.strftime("%m/%d/%Y")

    # SAM.gov uses 'title' for keyword search, not 'keyword'
    for term in ["roofing", "roof replacement", "slate roof", "roof repair",
                 "historic roof", "historic roofing", "slate roofing", "copper roofing"]:
        try:
            params = {
                "api_key":    api_key,
                "limit":      25,
                "postedFrom": from_date,
                "postedTo":   to_date,
                "title":      term,
                "active":     "true",
                "state":      "CT,MA,RI,NH,NY",
            }
            r = session.get(
                "https://api.sam.gov/prod/opportunities/v2/search",
                params=params,
                timeout=25
            )
            log.info(f"SAM.gov '{term}': HTTP {r.status_code}")
            if not r.ok:
                log.warning(f"SAM.gov error: {r.text[:200]}")
                continue
            data  = r.json()
            total = data.get("totalRecords", 0)
            opps  = data.get("opportunitiesData") or []
            log.info(f"SAM.gov '{term}': {total} total, {len(opps)} returned")

            for o in opps:
                bid_id = f"sam_{o.get('noticeId', abs(hash(o.get('title',''))))}"
                if bid_id in seen_ids:
                    continue
                seen_ids.add(bid_id)
                bids.append({
                    "id":       bid_id,
                    "title":    o.get("title", "Untitled")[:200],
                    "org":      o.get("department") or o.get("subTier") or "Federal Agency",
                    "source":   "Federal",
                    "deadline": (o.get("responseDeadLine") or "")[:10],
                    "value":    None,
                    "link":     o.get("uiLink") or f"https://sam.gov/opp/{o.get('noticeId')}/view",
                    "status":   "new",
                    "found":    datetime.now().isoformat()
                })
        except Exception as e:
            log.warning(f"SAM.gov '{term}': {e}")

    log.info(f"SAM.gov total: {len(bids)} federal bids")
    return bids
def scrape_town(name, url, org):
    bids     = []
    seen_ids = set()
    session  = make_session()
    from urllib.parse import urlparse
    parsed   = urlparse(url)
    homepage = f"{parsed.scheme}://{parsed.netloc}"

    try:
        r = session.get(url, headers=get_headers(referer=homepage), timeout=15)
        r.raise_for_status()
        # Force UTF-8 decoding and handle compressed responses
        r.encoding = r.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(r.text, "html.parser")

        # Strip chrome
        for tag in soup.find_all(["nav","footer","header","aside","script","style","noscript"]):
            tag.decompose()
        for tag in soup.find_all(True, class_=re.compile(r"nav|menu|footer|header|sidebar|social|breadcrumb|banner|cookie|alert", re.I)):
            tag.decompose()

        # Universal row search — check every <a> tag for roofing keywords
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text())
            if len(text) < 8 or len(text) > 250:
                continue
            if not is_roofing(text):
                continue
            if is_junk(text):
                continue

            href = a["href"]
            if href.startswith("http"):
                link = href
            elif href.startswith("/"):
                link = f"{homepage}{href}"
            else:
                link = url

            bid_id = f"{name.lower().replace(' ','_')}_{abs(hash(text))}"
            if bid_id in seen_ids:
                continue
            seen_ids.add(bid_id)
            bids.append({
                "id": bid_id, "title": text[:200], "org": org,
                "source": "Town", "deadline": "", "value": None,
                "link": link, "status": "new",
                "found": datetime.now().isoformat()
            })

        log.info(f"{name}: {len(bids)} bids" if bids else f"{name}: no roofing bids found")
        return bids[:8]

    except Exception as e:
        log.warning(f"{name}: {e}")
        return []

# ── Main scraper ──────────────────────────────────────────────────────────────
def run_scraper():
    log.info("=== Scraper starting ===")
    try:
        seen    = load_seen()
        current = {b["id"]: b for b in load_bids()}
        fresh   = scrape_samgov()

        for name, url, org in TOWNS:
            fresh += scrape_town(name, url, org)
            time.sleep(0.5)

        new_bids = [b for b in fresh if b["id"] not in seen]
        log.info(f"Total: {len(fresh)} | New: {len(new_bids)}")

        for b in fresh:
            if b["id"] not in current:
                current[b["id"]] = b
        if new_bids:
            seen.update(b["id"] for b in new_bids)
            save_seen(seen)
        save_bids(list(current.values())[:500])
        log.info("=== Scraper complete ===")
    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
_started = False
_start_lock = threading.Lock()

@app.before_request
def start_once():
    global _started
    with _start_lock:
        if not _started:
            _started = True
            log.info("First request — starting background tasks")
            def scheduler():
                schedule.every().day.at("06:00").do(run_scraper)
                schedule.every().day.at("18:00").do(run_scraper)
                while True:
                    schedule.run_pending()
                    time.sleep(60)
            threading.Thread(target=run_scraper, daemon=True, name="scraper").start()
            threading.Thread(target=scheduler,   daemon=True, name="scheduler").start()

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
        "total":   len(bids),
        "federal": sum(1 for b in bids if b["source"] == "Federal"),
        "town":    sum(1 for b in bids if b["source"] == "Town"),
        "last_run": datetime.now().strftime("%b %d, %I:%M %p")
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
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px}
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
.bt{font-weight:500;color:var(--text);font-size:13px;max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bo{font-family:var(--mono);font-size:10px;color:var(--text2);margin-top:2px}
.badge{display:inline-flex;padding:2px 7px;border-radius:4px;font-family:var(--mono);font-size:10px;font-weight:600;background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3)}
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
      <div class="sb-lbl">Status Filter</div>
      <button class="fb active" onclick="setSt('all',this)">All Bids <span class="cnt" id="ca">0</span></button>
      <button class="fb" onclick="setSt('new',this)">New <span class="cnt" id="cn">0</span></button>
      <button class="fb" onclick="setSt('reviewing',this)">Reviewing <span class="cnt" id="cr">0</span></button>
      <button class="fb" onclick="setSt('bidding',this)">Bidding <span class="cnt" id="cb">0</span></button>
      <button class="fb" onclick="setSt('submitted',this)">Submitted <span class="cnt" id="cs">0</span></button>
    </div>
    <div class="sb-sec">
      <div class="sb-lbl">Monitored Towns</div>
      <div id="tlist" style="margin-top:4px"></div>
    </div>
  </aside>
  <main class="main">
    <div class="stats">
      <div class="stat"><div class="slbl">Open Bids</div><div class="sval" id="st">-</div><div class="ssub">all sources</div></div>
      <div class="stat"><div class="slbl">Federal (SAM.gov)</div><div class="sval" style="color:var(--blue)" id="sf">-</div><div class="ssub">CT / MA / RI</div></div>
      <div class="stat"><div class="slbl">Town Pages</div><div class="sval" style="color:var(--green)" id="stw">-</div><div class="ssub">Hartford region</div></div>
      <div class="stat"><div class="slbl">Last Updated</div><div class="sval" style="font-size:14px;padding-top:4px" id="slr">-</div><div class="ssub">runs 6am &amp; 6pm</div></div>
    </div>
    <div class="toolbar">
      <div class="sw"><span class="swi">🔍</span><input id="search" placeholder="Search bids, towns, keywords..." oninput="render()"></div>
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
const TOWNS=['Meriden','Enfield','Vernon','Bloomfield','Middletown','Bristol','Newington','Windsor','Wethersfield','New Britain','Southington','Tolland','Glastonbury','Farmington','Windsor Locks','Berlin','East Hartford','Manchester','Wallingford','Granby','Cromwell','Canton'];
let bids=[],status='all';
document.getElementById('tlist').innerHTML=TOWNS.map(t=>`<div class="tc"><span class="dot"></span><span>${t}</span></div>`).join('');
function ff(i){if(!i)return'-';try{return new Date(i).toLocaleDateString('en-US',{month:'short',day:'numeric'})}catch{return'-'}}
function counts(){
  const tots={all:bids.length,new:0,reviewing:0,bidding:0,submitted:0};
  bids.forEach(b=>{const s=b.status||'new';if(tots[s]!==undefined)tots[s]++;});
  document.getElementById('ca').textContent=tots.all;
  document.getElementById('cn').textContent=tots.new;
  document.getElementById('cr').textContent=tots.reviewing;
  document.getElementById('cb').textContent=tots.bidding;
  document.getElementById('cs').textContent=tots.submitted;
  document.getElementById('st').textContent=bids.length;
  document.getElementById('sf').textContent=bids.filter(b=>b.source==='Federal').length;
  document.getElementById('stw').textContent=bids.filter(b=>b.source==='Town').length;
}
function render(){
  const q=document.getElementById('search').value.toLowerCase();
  let b=[...bids];
  if(status!=='all')b=b.filter(x=>(x.status||'new')===status);
  if(q)b=b.filter(x=>(x.title||'').toLowerCase().includes(q)||(x.org||'').toLowerCase().includes(q));
  b.sort((a,x)=>new Date(x.found)-new Date(a.found));
  const tb=document.getElementById('tb');
  if(!b.length){tb.innerHTML='<tr><td colspan="5"><div class="empty">No bids match your filters</div></td></tr>';return}
  tb.innerHTML=b.map(x=>{const s=x.status||'new';return`<tr onclick="openP('${x.id}')">
    <td><div class="bt" title="${x.title}">${x.title}</div><div class="bo">${x.org}</div></td>
    <td><span class="badge" style="${x.source==='Federal'?'background:rgba(59,130,246,.15);color:var(--blue);border-color:rgba(59,130,246,.3)':''}">${x.source==='Federal'?'⚑ Federal':'◆ Town'}</span></td>
    <td style="color:var(--text3);font-family:var(--mono);font-size:11px">-</td>
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
function setSt(s,b){status=s;document.querySelectorAll('.sb-sec .fb').forEach(x=>x.classList.remove('active'));b.classList.add('active');render()}
function saveSt(id,val){const b=bids.find(x=>x.id===id);if(b)b.status=val;fetch(`/api/status/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:val,notes:b?.notes||''})});render()}
function openP(id){
  const b=bids.find(x=>x.id===id);if(!b)return;
  document.getElementById('pc').innerHTML=`
    <span class="badge">Town</span>
    <div class="pt" style="margin-top:10px">${b.title}</div>
    <div class="po">${b.org}</div>
    <div class="dg">
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
    <textarea class="nb" id="nb" placeholder="Add notes about this bid...">${b.notes||''}</textarea>
    <div class="pa">
      <a href="${b.link}" target="_blank" style="flex:1;text-decoration:none"><button class="bp" style="width:100%">View Official Bid →</button></a>
      <button class="bs" onclick="saveN('${b.id}')">Save Notes</button>
      <button class="bs" onclick="closeP()">Close</button>
    </div>`;
  document.getElementById('ov').classList.add('open');
}
function saveN(id){const n=document.getElementById('nb').value;const b=bids.find(x=>x.id===id);if(b)b.notes=n;fetch(`/api/status/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:b?.status||'new',notes:n})});toast('Notes saved ✓')}
function closeP(e){if(e&&e.target!==document.getElementById('ov'))return;document.getElementById('ov').classList.remove('open')}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
async function go(){toast('Scraper started...');await fetch('/api/scrape',{method:'POST'});setTimeout(load,30000)}
async function load(){
  try{
    const[a,b]=await Promise.all([fetch('/api/bids'),fetch('/api/stats')]);
    bids=await a.json();
    const s=await b.json();
    document.getElementById('slr').textContent=s.last_run;
    counts();render();
  }catch(e){
    document.getElementById('tb').innerHTML='<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text2)">Could not load bids — check Railway logs</td></tr>';
  }
}
load();setInterval(load,5*60*1000);
</script>
</body>
</html>"""

log.info("App ready")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
