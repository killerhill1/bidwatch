"""
BidWatch — Courtman Enterprises LLC
Fixed version: persistent storage + corrected town URLs + working CT Source
"""

from flask import Flask, jsonify, render_template_string, request
import requests
from bs4 import BeautifulSoup
import json, os, re, threading, schedule, time
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

DATA_DIR  = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/tmp"))
DATA_FILE = DATA_DIR / "bids.json"
SEEN_FILE = DATA_DIR / "seen.json"

ROOFING_KEYWORDS = [
    "roof", "roofing", "slate", "shingle", "membrane", "flashing",
    "gutter", "copper", "waterproof", "tpo", "epdm", "flat roof",
    "sheet metal", "soffit", "fascia", "historic roof", "re-roof"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
}

TOWNS = [
    ("East Hartford",  "https://www.easthartfordct.gov/bids",                         "Town of East Hartford"),
    ("Manchester",     "https://www.manchesterct.gov/government/departments/general-services/purchasing", "Town of Manchester"),
    ("Meriden",        "https://www.meridenct.gov/business/bids-rfps/",               "City of Meriden"),
    ("Berlin",         "https://www.berlinct.gov/bids",                               "Town of Berlin"),
    ("Glastonbury",    "https://www.glastonburyct.gov/bids-rfps",                     "Town of Glastonbury"),
    ("Enfield",        "https://www.enfield-ct.gov/Bids.aspx",                        "Town of Enfield"),
    ("Wethersfield",   "https://www.wethersfieldct.gov/bids",                         "Town of Wethersfield"),
    ("Newington",      "https://www.newingtonct.gov/bids",                            "Town of Newington"),
    ("Windsor",        "https://www.townofwindsor.com/Bids.aspx",                     "Town of Windsor"),
    ("Bloomfield",     "https://www.bloomfieldct.gov/Bids.aspx",                      "Town of Bloomfield"),
    ("Avon",           "https://www.avon-ct.gov/bids",                                "Town of Avon"),
    ("Farmington",     "https://www.farmington-ct.org/bids",                          "Town of Farmington"),
    ("Windsor Locks",  "https://www.windsorlocksct.org/Bids.aspx",                    "Town of Windsor Locks"),
    ("Southington",    "https://www.southingtonct.gov/Bids.aspx",                     "Town of Southington"),
    ("Vernon",         "https://www.vernon-ct.gov/government/bids-and-contracts",     "Town of Vernon"),
    ("Tolland",        "https://www.tolland.org/Bids.aspx",                           "Town of Tolland"),
    ("Middletown",     "https://www.middletownct.gov/Bids.aspx",                      "City of Middletown"),
    ("Bristol",        "https://www.bristolct.gov/Bids.aspx",                         "City of Bristol"),
    ("New Britain",    "https://www.newbritainct.gov/Bids.aspx",                      "City of New Britain"),
]

def is_roofing(text):
    return any(kw in text.lower() for kw in ROOFING_KEYWORDS)

def clean(text):
    return re.sub(r'\s+', ' ', text or "").strip()

def load_bids():
    try:
        if DATA_FILE.exists():
            return json.loads(DATA_FILE.read_text())
    except:
        pass
    return []

def save_bids(bids):
    DATA_FILE.write_text(json.dumps(bids, indent=2))

def load_seen():
    try:
        if SEEN_FILE.exists():
            return set(json.loads(SEEN_FILE.read_text()))
    except:
        pass
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)))

def scrape_ctsource():
    bids = []
    seen_ids = set()
    for kw in ["roofing", "roof replacement", "slate", "membrane roof"]:
        try:
            r = requests.get(
                "https://www.biznet.ct.gov/SCP_Search/BidResults.aspx",
                params={"TN": kw, "CT": "B"},
                headers=HEADERS, timeout=25
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
                    "id": bid_id, "title": title,
                    "org": clean(cols[2].get_text()) if len(cols) > 2 else "CT Agency",
                    "source": "CT Source",
                    "deadline": clean(cols[3].get_text()) if len(cols) > 3 else "",
                    "value": None, "link": link, "status": "new",
                    "found": datetime.now().isoformat()
                })
        except Exception as e:
            print(f"  BizNet ({kw}): {e}")
    try:
        r = requests.get("https://portal.ct.gov/das/construction-services/bidboard", headers=HEADERS, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
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
        print(f"  DAS: {e}")
    print(f"  CT Source: {len(bids)} bids")
    return bids

def scrape_town(name, url, org):
    bids = []
    seen_ids = set()
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
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
            print(f"  {name}: {len(bids[:8])} bids")
        return bids[:8]
    except Exception as e:
        print(f"  {name}: {e}")
        return []

def run_scraper():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Scraper running...")
    seen = load_seen()
    current = {b["id"]: b for b in load_bids()}
    fresh = scrape_ctsource()
    for name, url, org in TOWNS:
        fresh += scrape_town(name, url, org)
    new_bids = [b for b in fresh if b["id"] not in seen]
    print(f"  Total: {len(fresh)} | New: {len(new_bids)}")
    for b in fresh:
        if b["id"] not in current:
            current[b["id"]] = b
    if new_bids:
        seen.update(b["id"] for b in new_bids)
        save_seen(seen)
    save_bids(list(current.values())[:500])

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
            b["notes"] = data.get("notes", b.get("notes", ""))
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
        "total": len(bids),
        "ct_source": sum(1 for b in bids if b["source"] == "CT Source"),
        "town": sum(1 for b in bids if b["source"] == "Town"),
        "last_run": datetime.now().strftime("%b %d, %I:%M %p")
    })

DASHBOARD = open(Path(__file__).parent / "dashboard.html").read() if (Path(__file__).parent / "dashboard.html").exists() else "<h1>Dashboard loading...</h1>"

@app.route("/")
def dashboard():
    return DASHBOARD

@app.route("/awards")
def awards():
    f = Path(__file__).parent / "awards.html"
    return f.read_text() if f.exists() else "<h1>Awards page coming soon</h1>"

def start_scheduler():
    schedule.every().day.at("06:00").do(run_scraper)
    schedule.every().day.at("18:00").do(run_scraper)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_scraper, daemon=True).start()
    threading.Thread(target=start_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
