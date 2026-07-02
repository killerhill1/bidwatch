"""
BidWatch — Courtman Enterprises LLC
Full-stack web app: dashboard + scraper in one Railway deployment
"""

from flask import Flask, jsonify, render_template_string, request
import requests
from bs4 import BeautifulSoup
import json
import os
import re
import smtplib
import threading
import schedule
import time
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
SMTP_USER   = os.environ.get("SMTP_USER", "")
SMTP_PASS   = os.environ.get("SMTP_PASS", "")
DATA_FILE   = Path("/app/data/bids.json")
SEEN_FILE   = Path("/app/data/seen.json")

ROOFING_KEYWORDS = [
    "roof", "roofing", "slate", "shingle", "membrane", "flashing",
    "gutter", "copper", "waterproof", "tpo", "epdm", "flat roof",
    "sheet metal", "soffit", "fascia", "historic roof", "re-roof"
]

HEADERS = {"User-Agent": "Mozilla/5.0 (BidWatchBot/1.0 roofing bid monitor)"}

TOWNS = [
    ("East Hartford",  "https://www.easthartfordct.gov/bids",                                          "Town of East Hartford"),
    ("Manchester",     "http://generalservices1.townofmanchester.org/index.cfm/bid-requests/",          "Town of Manchester"),
    ("Meriden",        "https://www.meridenct.gov/business/bids-rfps/",                                "City of Meriden"),
    ("Berlin",         "https://www.berlinct.gov/topic/subtopic.php?topicid=412&structureid=123",       "Town of Berlin"),
    ("Glastonbury",    "https://www.glastonburyct.gov/departments/department-directory-i-z/purchasing/bids-rfps", "Town of Glastonbury"),
    ("Enfield",        "https://www.enfield-ct.gov/Bids.aspx",                                         "Town of Enfield"),
    ("Wethersfield",   "https://wethersfieldct.gov/content/398/410/559.aspx",                          "Town of Wethersfield"),
    ("Newington",      "https://www.newingtonct.gov/bids",                                             "Town of Newington"),
    ("Windsor",        "https://www.townofwindsor.com/Bids.aspx",                                      "Town of Windsor"),
    ("Bloomfield",     "https://www.bloomfieldct.gov/Bids.aspx",                                       "Town of Bloomfield"),
    ("Avon",           "https://www.avon-ct.gov/Bids.aspx",                                            "Town of Avon"),
    ("Farmington",     "https://www.farmington-ct.org/departments/finance-purchasing/purchasing/bids",  "Town of Farmington"),
    ("Windsor Locks",  "https://www.windsorlocksct.org/Bids.aspx",                                     "Town of Windsor Locks"),
    ("Southington",    "https://www.southingtonct.gov/Bids.aspx",                                      "Town of Southington"),
    ("Vernon",         "https://www.vernon-ct.gov/government/bids-and-contracts",                      "Town of Vernon"),
    ("Tolland",        "https://www.tolland.org/Bids.aspx",                                            "Town of Tolland"),
    ("Middletown",     "https://www.middletownct.gov/Bids.aspx",                                       "City of Middletown"),
    ("Bristol",        "https://www.bristolct.gov/Bids.aspx",                                          "City of Bristol"),
    ("New Britain",    "https://www.newbritainct.gov/Bids.aspx",                                       "City of New Britain"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_roofing(text):
    t = text.lower()
    return any(kw in t for kw in ROOFING_KEYWORDS)

def clean(text):
    return re.sub(r'\s+', ' ', text or "").strip()

def load_bids():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except:
            return []
    return []

def save_bids(bids):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(bids, indent=2))

def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except:
            return set()
    return set()

def save_seen(seen):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ── Scrapers ──────────────────────────────────────────────────────────────────
def scrape_ctsource():
    bids = []
    seen_ids = set()
    keywords = ["roofing", "roof replacement", "slate roof", "membrane roof", "historic roof"]

    for kw in keywords:
        try:
            r = requests.get(
                "https://www.biznet.ct.gov/SCP_Search/BidResults.aspx",
                params={"TN": kw, "CT": "B"},
                headers=HEADERS, timeout=20
            )
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("table tr")[1:]:
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
                    "id":       bid_id,
                    "title":    title,
                    "org":      clean(cols[2].get_text()) if len(cols) > 2 else "CT Gov Entity",
                    "source":   "CT Source",
                    "deadline": clean(cols[3].get_text()) if len(cols) > 3 else "",
                    "value":    None,
                    "link":     link,
                    "status":   "new",
                    "found":    datetime.now().isoformat()
                })
        except Exception as e:
            print(f"CT Source error ({kw}): {e}")

    # DAS Construction board
    try:
        r = requests.get("https://portal.ct.gov/das/construction-services/bidboard", headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text())
            if len(text) < 15 or not is_roofing(text):
                continue
            bid_id = f"das_{abs(hash(text))}"
            if bid_id not in seen_ids:
                seen_ids.add(bid_id)
                href = a["href"]
                if not href.startswith("http"):
                    href = "https://portal.ct.gov" + href
                bids.append({
                    "id":       bid_id,
                    "title":    text[:200],
                    "org":      "CT Dept of Administrative Services",
                    "source":   "CT Source",
                    "deadline": "",
                    "value":    None,
                    "link":     href,
                    "status":   "new",
                    "found":    datetime.now().isoformat()
                })
    except Exception as e:
        print(f"DAS board error: {e}")

    return bids

def scrape_town(name, url, org):
    bids = []
    seen_ids = set()
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["a", "li", "tr", "p"]):
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
                "id":       bid_id,
                "title":    text[:200],
                "org":      org,
                "source":   "Town",
                "deadline": "",
                "value":    None,
                "link":     link,
                "status":   "new",
                "found":    datetime.now().isoformat()
            })
        return bids[:8]
    except Exception as e:
        print(f"{name} scrape error: {e}")
        return []

def send_alert(new_bids):
    if not all([ALERT_EMAIL, SMTP_USER, SMTP_PASS]):
        return
    rows = "".join(f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #e5e7eb">
            <a href="{b['link']}" style="color:#1d4ed8;font-weight:600;text-decoration:none">{b['title'][:100]}</a><br>
            <span style="color:#6b7280;font-size:12px">{b['org']} &middot; {b['source']}</span>
          </td>
          <td style="padding:12px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#6b7280;white-space:nowrap">{b.get('deadline') or '—'}</td>
        </tr>""" for b in new_bids)

    html = f"""<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto">
      <div style="background:#111827;padding:24px;border-radius:8px 8px 0 0">
        <div style="font-size:22px;font-weight:700;color:#f59e0b">🏗 BidWatch</div>
        <div style="color:#9ca3af;margin-top:4px">Courtman Enterprises LLC — {len(new_bids)} new roofing bid{'s' if len(new_bids)>1 else ''} found</div>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:20px">
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="background:#f9fafb">
            <th style="padding:10px;text-align:left;font-size:11px;color:#6b7280;border-bottom:2px solid #e5e7eb">BID / ORGANIZATION</th>
            <th style="padding:10px;text-align:left;font-size:11px;color:#6b7280;border-bottom:2px solid #e5e7eb">DEADLINE</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="margin-top:16px;font-size:11px;color:#9ca3af">BidWatch runs 6am &amp; 6pm daily · CT Source · SAM.gov · 19 town pages</p>
      </div>
    </div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🏗 BidWatch: {len(new_bids)} New Roofing Bid{'s' if len(new_bids)>1 else ''}"
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_EMAIL
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, ALERT_EMAIL, msg.as_string())
        print(f"Alert sent → {ALERT_EMAIL}")
    except Exception as e:
        print(f"Email error: {e}")

def run_scraper():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Scraper running...")
    seen    = load_seen()
    current = {b["id"]: b for b in load_bids()}

    fresh = scrape_ctsource()
    for name, url, org in TOWNS:
        fresh += scrape_town(name, url, org)

    new_bids = [b for b in fresh if b["id"] not in seen]

    if new_bids:
        for b in new_bids:
            current[b["id"]] = b
        save_bids(list(current.values())[:500])
        seen.update(b["id"] for b in new_bids)
        save_seen(seen)
        send_alert(new_bids)
        print(f"  {len(new_bids)} new bids found and saved")
    else:
        print("  No new bids")

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/api/bids")
def api_bids():
    bids = load_bids()
    return jsonify(bids)

@app.route("/api/status/<bid_id>", methods=["POST"])
def api_status(bid_id):
    data  = request.json
    bids  = load_bids()
    for b in bids:
        if b["id"] == bid_id:
            b["status"] = data.get("status", "new")
            b["notes"]  = data.get("notes", b.get("notes", ""))
    save_bids(bids)
    return jsonify({"ok": True})

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    threading.Thread(target=run_scraper, daemon=True).start()
    return jsonify({"ok": True, "message": "Scraper started"})

@app.route("/api/stats")
def api_stats():
    bids = load_bids()
    now  = datetime.now()
    def days(d):
        if not d:
            return None
        try:
            dt = datetime.strptime(d[:10], "%Y-%m-%d")
            return (dt - now).days
        except:
            return None
    urgent = sum(1 for b in bids if (days(b.get("deadline")) or 999) < 7 and (days(b.get("deadline")) or 999) >= 0)
    return jsonify({
        "total":    len(bids),
        "urgent":   urgent,
        "ct_source": sum(1 for b in bids if b["source"] == "CT Source"),
        "town":     sum(1 for b in bids if b["source"] == "Town"),
        "last_run": datetime.now().strftime("%b %d, %I:%M %p")
    })

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BidWatch — Courtman Enterprises</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;500;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--surface:#171b26;--surface2:#1e2332;--border:#2a3048;
  --accent:#f59e0b;--blue:#3b82f6;--green:#10b981;--red:#ef4444;--purple:#8b5cf6;
  --text:#e8eaf0;--text2:#8892a4;--text3:#4a5568;
  --mono:'IBM Plex Mono',monospace;--sans:'Inter',sans-serif;
}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;min-height:100vh}

/* HEADER */
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;
  display:flex;align-items:center;justify-content:space-between;height:56px;
  position:sticky;top:0;z-index:100}
.logo{display:flex;align-items:center;gap:10px;font-family:var(--mono);font-weight:600;font-size:15px}
.logo-icon{width:28px;height:28px;background:var(--accent);border-radius:6px;
  display:flex;align-items:center;justify-content:center;font-size:14px}
.hdr-right{display:flex;align-items:center;gap:12px}
.live{font-family:var(--mono);font-size:11px;color:var(--green);
  display:flex;align-items:center;gap:5px}
.live::before{content:'';width:6px;height:6px;background:var(--green);border-radius:50%;
  animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.btn-refresh{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text2);font-family:var(--mono);font-size:11px;padding:6px 12px;cursor:pointer}
.btn-refresh:hover{color:var(--text)}

/* LAYOUT */
.layout{display:grid;grid-template-columns:200px 1fr;min-height:calc(100vh - 56px)}

/* SIDEBAR */
.sidebar{background:var(--surface);border-right:1px solid var(--border);
  padding:16px 0;position:sticky;top:56px;height:calc(100vh - 56px);overflow-y:auto}
.sb-section{padding:0 14px 16px;border-bottom:1px solid var(--border);margin-bottom:16px}
.sb-label{font-family:var(--mono);font-size:10px;font-weight:600;color:var(--text3);
  letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}
.fb{display:flex;align-items:center;justify-content:space-between;width:100%;
  padding:6px 8px;border-radius:6px;border:none;background:transparent;
  color:var(--text2);font-size:12px;font-family:var(--sans);cursor:pointer;
  margin-bottom:2px;text-align:left;transition:all .15s}
.fb:hover{background:var(--surface2);color:var(--text)}
.fb.active{background:var(--surface2);color:var(--text)}
.fb .cnt{font-family:var(--mono);font-size:10px;background:var(--surface2);
  padding:1px 6px;border-radius:10px;color:var(--text2)}
.fb.active .cnt{background:var(--accent);color:#000}
.town-chip{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text2);
  padding:3px 0;border-bottom:1px solid var(--border)}
.town-chip:last-child{border:none}
.dot{width:5px;height:5px;border-radius:50%;background:var(--green);flex-shrink:0}

/* MAIN */
.main{padding:20px;overflow:hidden}

/* STATS */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-lbl{font-family:var(--mono);font-size:10px;color:var(--text2);text-transform:uppercase;
  letter-spacing:.8px;margin-bottom:6px}
.stat-val{font-family:var(--mono);font-size:22px;font-weight:600}
.stat-sub{font-size:11px;color:var(--text3);margin-top:3px}
.c-green{color:var(--green)}.c-red{color:var(--red)}.c-blue{color:var(--blue)}.c-yellow{color:var(--accent)}

/* TOOLBAR */
.toolbar{display:flex;gap:10px;margin-bottom:14px;align-items:center}
.sw{position:relative;flex:1}
.sw input{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:8px 12px 8px 34px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none}
.sw input:focus{border-color:var(--accent)}
.sw-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--text3);font-size:13px}
.tabs{display:flex;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:3px;gap:2px}
.tab{padding:5px 12px;border-radius:5px;border:none;background:transparent;
  color:var(--text2);font-size:12px;font-family:var(--sans);cursor:pointer;white-space:nowrap}
.tab.active{background:var(--accent);color:#000;font-weight:600}

/* TABLE */
.tbl-wrap{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse}
thead tr{border-bottom:1px solid var(--border);background:var(--surface2)}
th{padding:9px 14px;font-family:var(--mono);font-size:10px;font-weight:600;color:var(--text3);
  text-transform:uppercase;letter-spacing:.8px;text-align:left;white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
tbody tr:last-child{border:none}
tbody tr:hover{background:var(--surface2)}
td{padding:11px 14px;vertical-align:middle}
.bid-title{font-weight:500;color:var(--text);font-size:13px;max-width:280px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bid-org{font-family:var(--mono);font-size:10px;color:var(--text2);margin-top:2px}
.badge{display:inline-flex;align-items:center;padding:2px 7px;border-radius:4px;
  font-family:var(--mono);font-size:10px;font-weight:600;white-space:nowrap}
.badge-ct{background:rgba(139,92,246,.15);color:var(--purple);border:1px solid rgba(139,92,246,.3)}
.badge-fed{background:rgba(59,130,246,.15);color:var(--blue);border:1px solid rgba(59,130,246,.3)}
.badge-town{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.dl{font-family:var(--mono);font-size:12px;white-space:nowrap}
.dl-urgent{color:var(--red)}.dl-soon{color:var(--accent)}.dl-ok{color:var(--green)}
.status-sel{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text);font-family:var(--mono);font-size:11px;padding:4px 7px;cursor:pointer;outline:none}
.status-sel:focus{border-color:var(--accent)}
.naics-tag{font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:4px;
  background:rgba(245,158,11,.1);color:var(--accent);border:1px solid rgba(245,158,11,.3)}
.empty{text-align:center;padding:60px 24px;color:var(--text2)}
.empty-icon{font-size:36px;margin-bottom:10px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

/* PANEL */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;backdrop-filter:blur(2px)}
.overlay.open{display:flex;align-items:flex-start;justify-content:flex-end}
.panel{background:var(--surface);border-left:1px solid var(--border);width:460px;
  height:100vh;overflow-y:auto;padding:22px;animation:slideIn .2s ease}
@keyframes slideIn{from{transform:translateX(30px);opacity:0}}
.panel-close{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text2);cursor:pointer;padding:5px 12px;font-size:12px;font-family:var(--mono);margin-bottom:18px}
.panel-title{font-size:16px;font-weight:600;line-height:1.4;margin-bottom:5px}
.panel-org{color:var(--text2);font-size:13px;margin-bottom:18px}
.dg{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.df{background:var(--surface2);border-radius:8px;padding:11px}
.df-lbl{font-family:var(--mono);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.df-val{font-size:13px;font-weight:500}
.panel-sec{font-family:var(--mono);font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.8px;margin:14px 0 7px}
.panel-desc{font-size:13px;color:var(--text2);line-height:1.6;background:var(--surface2);border-radius:8px;padding:13px}
.notes-box{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-family:var(--sans);font-size:13px;padding:10px;resize:vertical;
  min-height:80px;outline:none;margin-top:6px}
.notes-box:focus{border-color:var(--accent)}
.panel-actions{display:flex;gap:8px;margin-top:16px}
.btn-primary{flex:1;background:var(--accent);color:#000;border:none;border-radius:8px;
  padding:10px;font-weight:600;font-size:13px;cursor:pointer;font-family:var(--sans)}
.btn-sec{background:var(--surface2);color:var(--text);border:1px solid var(--border);
  border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer;font-family:var(--sans)}
.toast{position:fixed;bottom:24px;right:24px;background:var(--green);color:#fff;
  padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;
  opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
@media(max-width:768px){
  .layout{grid-template-columns:1fr}
  .sidebar{display:none}
  .stats{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>
<header class="hdr">
  <div class="logo">
    <div class="logo-icon">🏗</div>
    BidWatch
    <span style="font-size:11px;color:var(--text3);font-weight:400">· Courtman Enterprises</span>
  </div>
  <div class="hdr-right">
    <span class="live" id="live-status">LIVE</span>
    <span style="font-family:var(--mono);font-size:11px;color:var(--text2)" id="last-run">—</span>
    <button class="btn-refresh" onclick="triggerScrape()">↻ Refresh Now</button>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sb-section">
      <div class="sb-label">Source</div>
      <button class="fb active" onclick="setSource('all',this)">All Sources <span class="cnt" id="cnt-all">0</span></button>
      <button class="fb" onclick="setSource('CT Source',this)">CT Source <span class="cnt" id="cnt-ct">0</span></button>
      <button class="fb" onclick="setSource('Town',this)">Town Pages <span class="cnt" id="cnt-town">0</span></button>
    </div>
    <div class="sb-section">
      <div class="sb-label">Deadline</div>
      <button class="fb" onclick="setDeadline('urgent',this)">🔴 Under 7 days <span class="cnt" id="cnt-urgent">0</span></button>
      <button class="fb" onclick="setDeadline('soon',this)">🟡 7–14 days <span class="cnt" id="cnt-soon">0</span></button>
      <button class="fb" onclick="setDeadline('ok',this)">🟢 14+ days <span class="cnt" id="cnt-ok">0</span></button>
    </div>
    <div class="sb-section">
      <div class="sb-label">Monitored Towns</div>
      <div id="town-list" style="margin-top:4px"></div>
    </div>
  </aside>

  <main class="main">
    <div class="stats">
      <div class="stat">
        <div class="stat-lbl">Open Bids</div>
        <div class="stat-val" id="s-total">—</div>
        <div class="stat-sub">all sources</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Due This Week</div>
        <div class="stat-val c-red" id="s-urgent">—</div>
        <div class="stat-sub">act fast</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">CT Source</div>
        <div class="stat-val c-purple" id="s-ct">—</div>
        <div class="stat-sub">state + 262 entities</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Town Pages</div>
        <div class="stat-val c-green" id="s-town">—</div>
        <div class="stat-sub">Hartford region</div>
      </div>
    </div>

    <div class="toolbar">
      <div class="sw">
        <span class="sw-icon">🔍</span>
        <input id="search" placeholder="Search bids, towns, keywords..." oninput="render()">
      </div>
      <div class="tabs">
        <button class="tab active" onclick="setStatus('all',this)">All</button>
        <button class="tab" onclick="setStatus('new',this)">New</button>
        <button class="tab" onclick="setStatus('reviewing',this)">Reviewing</button>
        <button class="tab" onclick="setStatus('bidding',this)">Bidding</button>
        <button class="tab" onclick="setStatus('submitted',this)">Submitted</button>
      </div>
    </div>

    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Bid / Organization</th>
          <th>Source</th>
          <th>Deadline</th>
          <th>Found</th>
          <th>Your Status</th>
        </tr></thead>
        <tbody id="tbl-body">
          <tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text2)">
            <span class="spinner"></span>Loading bids...
          </td></tr>
        </tbody>
      </table>
    </div>
  </main>
</div>

<!-- DETAIL PANEL -->
<div class="overlay" id="overlay" onclick="closePanel(event)">
  <div class="panel" id="panel">
    <button class="panel-close" onclick="closePanel()">← Back</button>
    <div id="panel-content"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const TOWNS = [
  'Hartford','West Hartford','East Hartford','Manchester','Meriden',
  'Berlin','Glastonbury','Enfield','Wethersfield','Newington',
  'Windsor','Bloomfield','Avon','Farmington','Windsor Locks',
  'Southington','Vernon','Tolland','Middletown','Bristol','New Britain'
];

let allBids = [];
let activeSource = 'all';
let activeStatus = 'all';
let activeDeadline = null;
let currentBid = null;

// Render town list in sidebar
document.getElementById('town-list').innerHTML = TOWNS.map(t =>
  `<div class="town-chip"><span class="dot"></span><span>${t}</span></div>`
).join('');

function daysUntil(d) {
  if (!d) return null;
  try {
    const dt = new Date(d.slice(0,10));
    return Math.ceil((dt - new Date()) / 86400000);
  } catch { return null; }
}

function dlClass(days) {
  if (days === null) return '';
  if (days < 7) return 'dl-urgent';
  if (days <= 14) return 'dl-soon';
  return 'dl-ok';
}

function fmtDl(d) {
  const days = daysUntil(d);
  if (!d || days === null) return '<span style="color:var(--text3)">—</span>';
  const label = new Date(d.slice(0,10)).toLocaleDateString('en-US',{month:'short',day:'numeric'});
  const tag = days < 0 ? 'Closed' : days === 0 ? 'TODAY' : days+'d left';
  return `<span class="dl ${dlClass(days)}">${label}<br><span style="font-size:10px">${tag}</span></span>`;
}

function badge(source) {
  if (source === 'CT Source') return '<span class="badge badge-ct">★ CT Source</span>';
  if (source === 'Town') return '<span class="badge badge-town">◆ Town</span>';
  return '<span class="badge badge-fed">⚑ Federal</span>';
}

function fmtFound(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
  } catch { return '—'; }
}

function updateCounts() {
  const b = allBids;
  document.getElementById('cnt-all').textContent   = b.length;
  document.getElementById('cnt-ct').textContent    = b.filter(x=>x.source==='CT Source').length;
  document.getElementById('cnt-town').textContent  = b.filter(x=>x.source==='Town').length;
  document.getElementById('cnt-urgent').textContent= b.filter(x=>{ const d=daysUntil(x.deadline); return d!==null&&d<7&&d>=0; }).length;
  document.getElementById('cnt-soon').textContent  = b.filter(x=>{ const d=daysUntil(x.deadline); return d!==null&&d>=7&&d<=14; }).length;
  document.getElementById('cnt-ok').textContent    = b.filter(x=>{ const d=daysUntil(x.deadline); return d!==null&&d>14; }).length;
  document.getElementById('s-total').textContent   = b.length;
  document.getElementById('s-urgent').textContent  = b.filter(x=>{ const d=daysUntil(x.deadline); return d!==null&&d<7&&d>=0; }).length;
  document.getElementById('s-ct').textContent      = b.filter(x=>x.source==='CT Source').length;
  document.getElementById('s-town').textContent    = b.filter(x=>x.source==='Town').length;
}

function render() {
  const search = document.getElementById('search').value.toLowerCase();
  let bids = [...allBids];
  if (activeSource !== 'all') bids = bids.filter(b=>b.source===activeSource);
  if (activeStatus !== 'all') bids = bids.filter(b=>(b.status||'new')===activeStatus);
  if (activeDeadline==='urgent') bids = bids.filter(b=>{ const d=daysUntil(b.deadline); return d!==null&&d<7&&d>=0; });
  if (activeDeadline==='soon')   bids = bids.filter(b=>{ const d=daysUntil(b.deadline); return d!==null&&d>=7&&d<=14; });
  if (activeDeadline==='ok')     bids = bids.filter(b=>{ const d=daysUntil(b.deadline); return d!==null&&d>14; });
  if (search) bids = bids.filter(b=>
    (b.title||'').toLowerCase().includes(search) ||
    (b.org||'').toLowerCase().includes(search) ||
    (b.source||'').toLowerCase().includes(search)
  );
  bids.sort((a,b)=>{ const da=daysUntil(a.deadline)??9999, db=daysUntil(b.deadline)??9999; return da-db; });

  const tbody = document.getElementById('tbl-body');
  if (!bids.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty"><div class="empty-icon">📭</div><strong>No bids match</strong><br><span style="font-size:12px;color:var(--text3)">Try changing your filters</span></div></td></tr>`;
    return;
  }
  tbody.innerHTML = bids.map(b => {
    const s = b.status || 'new';
    return `<tr onclick="openPanel('${b.id}')">
      <td>
        <div class="bid-title" title="${b.title}">${b.title}</div>
        <div class="bid-org">${b.org}</div>
      </td>
      <td>${badge(b.source)}</td>
      <td>${fmtDl(b.deadline)}</td>
      <td style="font-family:var(--mono);font-size:11px;color:var(--text2)">${fmtFound(b.found)}</td>
      <td onclick="event.stopPropagation()">
        <select class="status-sel" onchange="saveStatus('${b.id}',this.value)">
          <option value="new"       ${s==='new'?'selected':''}>🆕 New</option>
          <option value="reviewing" ${s==='reviewing'?'selected':''}>👁 Reviewing</option>
          <option value="bidding"   ${s==='bidding'?'selected':''}>✏️ Bidding</option>
          <option value="submitted" ${s==='submitted'?'selected':''}>📤 Submitted</option>
          <option value="won"       ${s==='won'?'selected':''}>✅ Won</option>
          <option value="lost"      ${s==='lost'?'selected':''}>❌ Lost</option>
        </select>
      </td>
    </tr>`;
  }).join('');
}

function setSource(src, btn) {
  activeSource = src; activeDeadline = null;
  document.querySelectorAll('.sb-section .fb').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  render();
}

function setDeadline(dl, btn) {
  activeDeadline = activeDeadline === dl ? null : dl;
  document.querySelectorAll('.sb-section .fb').forEach(b=>b.classList.remove('active'));
  if (activeDeadline) btn.classList.add('active');
  else document.querySelector('.fb').classList.add('active');
  render();
}

function setStatus(s, btn) {
  activeStatus = s;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  render();
}

function saveStatus(id, status) {
  const bid = allBids.find(b=>b.id===id);
  if (bid) bid.status = status;
  fetch(`/api/status/${id}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({status, notes: bid?.notes||''})
  });
  render();
}

function openPanel(id) {
  const b = allBids.find(x=>x.id===id);
  if (!b) return;
  currentBid = b;
  const days = daysUntil(b.deadline);
  const cls  = dlClass(days);
  document.getElementById('panel-content').innerHTML = `
    <div style="margin-bottom:8px">${badge(b.source)}</div>
    <div class="panel-title">${b.title}</div>
    <div class="panel-org">${b.org}</div>
    <div class="dg">
      <div class="df"><div class="df-lbl">Deadline</div>
        <div class="df-val ${cls}">${b.deadline ? new Date(b.deadline.slice(0,10)).toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'}) : 'Not listed'}</div></div>
      <div class="df"><div class="df-lbl">Days Left</div>
        <div class="df-val ${cls}">${days===null?'—':days<0?'Closed':days===0?'TODAY':days+' days'}</div></div>
      <div class="df"><div class="df-lbl">Found</div>
        <div class="df-val">${fmtFound(b.found)}</div></div>
      <div class="df"><div class="df-lbl">Source</div>
        <div class="df-val">${b.source}</div></div>
    </div>
    <div class="panel-sec">Your Status</div>
    <select class="status-sel" style="width:100%;padding:8px 10px;font-size:12px" onchange="saveStatus('${b.id}',this.value)">
      <option value="new"       ${(b.status||'new')==='new'?'selected':''}>🆕 New</option>
      <option value="reviewing" ${b.status==='reviewing'?'selected':''}>👁 Reviewing</option>
      <option value="bidding"   ${b.status==='bidding'?'selected':''}>✏️ Bidding</option>
      <option value="submitted" ${b.status==='submitted'?'selected':''}>📤 Submitted</option>
      <option value="won"       ${b.status==='won'?'selected':''}>✅ Won</option>
      <option value="lost"      ${b.status==='lost'?'selected':''}>❌ Lost / Pass</option>
    </select>
    <div class="panel-sec">Notes</div>
    <textarea class="notes-box" id="notes-box" placeholder="Add notes about this bid...">${b.notes||''}</textarea>
    <div class="panel-actions">
      <a href="${b.link}" target="_blank" style="flex:1;text-decoration:none">
        <button class="btn-primary" style="width:100%">View Official Bid →</button>
      </a>
      <button class="btn-sec" onclick="saveNotes('${b.id}')">Save Notes</button>
      <button class="btn-sec" onclick="closePanel()">Close</button>
    </div>`;
  document.getElementById('overlay').classList.add('open');
}

function saveNotes(id) {
  const notes = document.getElementById('notes-box').value;
  const bid = allBids.find(b=>b.id===id);
  if (bid) bid.notes = notes;
  fetch(`/api/status/${id}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({status: bid?.status||'new', notes})
  });
  showToast('Notes saved');
}

function closePanel(e) {
  if (e && e.target !== document.getElementById('overlay')) return;
  document.getElementById('overlay').classList.remove('open');
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2500);
}

async function triggerScrape() {
  showToast('Scraper started — check back in a minute');
  await fetch('/api/scrape', {method:'POST'});
  setTimeout(loadBids, 15000);
}

async function loadBids() {
  try {
    const [bidsRes, statsRes] = await Promise.all([
      fetch('/api/bids'), fetch('/api/stats')
    ]);
    allBids = await bidsRes.json();
    const stats = await statsRes.json();
    document.getElementById('last-run').textContent = 'Last run: ' + stats.last_run;
    updateCounts();
    render();
  } catch(e) {
    document.getElementById('tbl-body').innerHTML =
      `<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text2)">
        Could not load bids. Make sure the server is running.
      </td></tr>`;
  }
}

loadBids();
setInterval(loadBids, 5 * 60 * 1000); // refresh every 5 min
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ── Scheduler ─────────────────────────────────────────────────────────────────
def start_scheduler():
    schedule.every().day.at("06:00").do(run_scraper)
    schedule.every().day.at("18:00").do(run_scraper)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Run scraper once on startup
    threading.Thread(target=run_scraper, daemon=True).start()
    # Start scheduler in background
    threading.Thread(target=start_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ── Awards Intelligence Page ──────────────────────────────────────────────────
@app.route("/awards")
def awards():
    awards_html = open("/app/awards.html").read() if os.path.exists("/app/awards.html") else ""
    if not awards_html:
        # fallback - read from same directory as app.py
        import pathlib
        f = pathlib.Path(__file__).parent / "awards.html"
        awards_html = f.read_text() if f.exists() else "<h1>Awards page not found</h1>"
    return awards_html
