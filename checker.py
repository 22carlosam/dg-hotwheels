#!/usr/bin/env python3
"""
Dollar General Hot Wheels Inventory Checker
Tracks stock levels at nearby DG stores and highlights inventory increases.
"""

import base64
import json
import os
import platform
import subprocess
import sys
import time
import requests
from datetime import datetime
from pathlib import Path

import token_fetcher

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"
REPORT_FILE = BASE_DIR / "report.html"
SESSION_CACHE_FILE = DATA_DIR / "session_cache.json"

INVENTORY_URL = "https://dggo.dollargeneral.com/omni/api/store/search/inventory"
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"

DEFAULT_CONFIG = {
    "zip_code": "",
    "radius_miles": 10,
    "upcs": [
        {"upc": "27084412086", "name": "Hot Wheels Basic Play Car"},
        {"upc": "27084480665", "name": "Hot Wheels Basic Car"},
        {"upc": "887961761382", "name": "Hot Wheels 5-Pack"},
        {"upc": "887961928105", "name": "Hot Wheels Monster Truck"},
    ]
}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"Created config.json — please edit it to set your ZIP code, then re-run.")
        sys.exit(0)
    cfg = json.loads(CONFIG_FILE.read_text())
    # Env var wins (used by GitHub Actions so the ZIP stays out of the repo).
    env_zip = os.environ.get("DG_ZIP", "").strip()
    if env_zip:
        cfg["zip_code"] = env_zip
    elif not cfg.get("zip_code"):
        if os.environ.get("CI"):
            print("ERROR: no ZIP. Set the DG_ZIP secret in GitHub.")
            sys.exit(1)
        cfg["zip_code"] = input("Enter your ZIP code: ").strip()
    return cfg


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode_zip(zip_code):
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"q": zip_code, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "DG-HotWheels-Checker/1.0"},
            timeout=10,
        )
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"Geocoding error: {e}")
    return None, None


# ── Auth ──────────────────────────────────────────────────────────────────────

def _parse_jwt_expiry(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.b64decode(payload))
        return claims.get("exp", time.time() + 3600)
    except Exception:
        return time.time() + 3600


def get_session():
    """
    Return a fresh DG guest session (idToken + x-dg-* values).
    Uses a cached session while its token is still valid, otherwise
    launches a headless browser to mint a new one.
    """
    if SESSION_CACHE_FILE.exists():
        try:
            cache = json.loads(SESSION_CACHE_FILE.read_text())
            if cache.get("idToken") and time.time() < cache.get("expires_at", 0) - 120:
                return cache
        except Exception:
            pass

    print("Minting a fresh DG session (headless browser)...")
    session = token_fetcher.fetch_session()
    session["expires_at"] = _parse_jwt_expiry(session["idToken"])
    DATA_DIR.mkdir(exist_ok=True)
    SESSION_CACHE_FILE.write_text(json.dumps(session))
    return session


# ── API ───────────────────────────────────────────────────────────────────────

def check_inventory(lat, lon, radius, upc, session):
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {session['idToken']}",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.dollargeneral.com",
        "referer": "https://www.dollargeneral.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-dg-apptoken": session["appToken"],
        "x-dg-appsessiontoken": session["appSessionToken"],
        "x-dg-cloud-service": "true",
        "x-dg-customerguid": session["customerGuid"],
        "x-dg-deviceuniqueid": session["deviceId"],
        "x-dg-partnerapitoken": session["partnerApiToken"],
    }
    resp = requests.post(
        INVENTORY_URL,
        headers=headers,
        data={"latitude": lat, "longitude": lon, "radius": radius, "upc": upc, "offerSourceType": 0},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("stores", [])


# ── History ───────────────────────────────────────────────────────────────────

def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return {}


def save_snapshot(history, snapshot):
    DATA_DIR.mkdir(exist_ok=True)
    history[snapshot["timestamp"]] = snapshot
    keys = sorted(history.keys())
    for old in keys[:-30]:
        del history[old]
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def get_previous_snapshot(history, current_ts):
    keys = [k for k in sorted(history.keys()) if k != current_ts]
    return history[keys[-1]] if keys else None


# ── Report ────────────────────────────────────────────────────────────────────

def _inventory_status_label(code):
    return {1: "In Stock", 2: "Low", 3: "In Stock", 0: "Out"}.get(code, f"Status {code}")


def _restocks_vs_previous(snapshot, previous, upcs):
    """Find quantity increases in the current snapshot vs the previous one."""
    if not previous:
        return []
    upc_names = {u["upc"]: u["name"] for u in upcs}
    prev_qty = {}
    for u in previous.get("upcs", []):
        for s in u.get("stores", []):
            prev_qty[(s["storeNumber"], u["upc"])] = s["productQTY"]
    events = []
    for u in snapshot.get("upcs", []):
        if u["upc"] not in upc_names:
            continue
        for s in u.get("stores", []):
            key = (s["storeNumber"], u["upc"])
            p = prev_qty.get(key)
            if p is not None and s["productQTY"] > p:
                events.append({
                    "store": s["storeNumber"],
                    "addr": f"{s['address']}, {s['city']}, {s['state']}",
                    "distance": s["distance"],
                    "name": upc_names[u["upc"]],
                    "prev": p,
                    "new": s["productQTY"],
                    "delta": s["productQTY"] - p,
                })
    return events


def notify_discord(events, report_url=None):
    """POST a Discord webhook for each new restock event, if DISCORD_WEBHOOK is set."""
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook:
        return
    if not events:
        return
    embeds = []
    for ev in events[:10]:  # Discord caps embeds per message at 10
        embeds.append({
            "title": f"🚗 Restock: {ev['name']}",
            "description": (
                f"**Store #{ev['store']}** · {ev['distance']:.1f} mi\n"
                f"{ev['addr']}\n"
                f"Quantity: **{ev['prev']} → {ev['new']}**  (+{ev['delta']})"
            ),
            "color": 0x2E7D32,
        })
    payload = {"embeds": embeds}
    if report_url:
        payload["content"] = f"📦 {len(events)} restock(s) detected — [open report]({report_url})"
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        r.raise_for_status()
        print(f"Discord: sent {len(events)} restock notification(s)")
    except Exception as e:
        print(f"Discord notification failed: {e}")


def _walk_restock_history(history, upcs):
    """
    Walk every consecutive pair of snapshots and find quantity increases.
    Returns:
      events:  list of {ts, store, addr, distance, upc, name, prev, new, delta},
               sorted newest first.
      last_by: dict keyed by (storeNumber, upc) -> the most recent event for that pair.
    """
    upc_names = {u["upc"]: u["name"] for u in upcs}
    sorted_ts = sorted(history.keys())
    events = []
    last_by = {}
    for i in range(1, len(sorted_ts)):
        a, b = sorted_ts[i - 1], sorted_ts[i]
        prev_qty = {}
        for u in history[a].get("upcs", []):
            for s in u.get("stores", []):
                prev_qty[(s["storeNumber"], u["upc"])] = s["productQTY"]
        for u in history[b].get("upcs", []):
            if u["upc"] not in upc_names:
                continue
            for s in u.get("stores", []):
                key = (s["storeNumber"], u["upc"])
                p = prev_qty.get(key)
                if p is not None and s["productQTY"] > p:
                    ev = {
                        "ts": b,
                        "store": s["storeNumber"],
                        "addr": f"{s['address']}, {s['city']}, {s['state']}",
                        "distance": s["distance"],
                        "upc": u["upc"],
                        "name": upc_names[u["upc"]],
                        "prev": p,
                        "new": s["productQTY"],
                        "delta": s["productQTY"] - p,
                    }
                    events.append(ev)
                    last_by[key] = ev
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events, last_by


def _short_date(ts_str):
    """'2026-05-15 06:17:32' -> 'May 15'."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").strftime("%b %d")
    except Exception:
        return ts_str[:10]


def _short_datetime(ts_str):
    """'2026-05-15 06:17:32' -> 'May 15 06:17'."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").strftime("%b %d %H:%M")
    except Exception:
        return ts_str


def _steady_since(history, current_ts):
    """
    For each (storeNumber, upc) pair in the snapshot at current_ts, find the
    earliest consecutive prior snapshot where the quantity equaled the current
    value. Returns {(store, upc): earliest_ts_at_current_qty}.
    """
    sorted_ts = sorted(history.keys())
    if current_ts not in sorted_ts:
        return {}
    idx_cur = sorted_ts.index(current_ts)

    snaps = {}
    for ts in sorted_ts:
        snaps[ts] = {
            (s["storeNumber"], u["upc"]): s["productQTY"]
            for u in history[ts].get("upcs", [])
            for s in u.get("stores", [])
        }

    current = snaps[current_ts]
    result = {}
    for key, q in current.items():
        earliest = current_ts
        for j in range(idx_cur - 1, -1, -1):
            ts = sorted_ts[j]
            if snaps[ts].get(key) == q:
                earliest = ts
            else:
                break
        result[key] = earliest
    return result


def _format_steady(seconds):
    """Format a duration in seconds as '2d steady' / '12h steady'."""
    if seconds < 3600:
        return None  # less than an hour — no useful info
    days = seconds / 86400
    if days >= 1:
        return f"{int(round(days))}d steady"
    return f"{int(round(seconds / 3600))}h steady"


def build_report(snapshot, previous, config, history=None):
    upcs = config.get("upcs", [])
    ts = snapshot["timestamp"]
    prev_ts = previous["timestamp"] if previous else None

    # Walk all of history for restock events (last increase per store/item)
    history = history or {}
    restock_events, last_increase = _walk_restock_history(history, upcs)
    steady_at = _steady_since(history, ts)
    fmt = "%Y-%m-%d %H:%M:%S"

    # Index current stores: upc -> storeNumber -> store dict
    current_idx = {}
    for upc_data in snapshot.get("upcs", []):
        current_idx[upc_data["upc"]] = {s["storeNumber"]: s for s in upc_data.get("stores", [])}

    # Index previous: upc -> storeNumber -> qty
    prev_idx = {}
    if previous:
        for upc_data in previous.get("upcs", []):
            prev_idx[upc_data["upc"]] = {s["storeNumber"]: s["productQTY"] for s in upc_data.get("stores", [])}

    # Collect all stores
    all_stores = {}
    for upc_data in snapshot.get("upcs", []):
        for s in upc_data.get("stores", []):
            snum = s["storeNumber"]
            if snum not in all_stores:
                all_stores[snum] = s
    sorted_stores = sorted(all_stores.values(), key=lambda s: s["distance"])

    # Build table rows
    rows = []
    for s in sorted_stores:
        snum = s["storeNumber"]
        cells = []
        row_has_increase = False

        for upc_cfg in upcs:
            upc = upc_cfg["upc"]
            store_data = current_idx.get(upc, {}).get(snum)
            if store_data is None:
                cells.append('<td class="cell-none">—</td>')
                continue

            qty = store_data["productQTY"]
            prev_qty = prev_idx.get(upc, {}).get(snum)

            if prev_qty is None:
                badge = '<span class="badge new">NEW</span>'
                css = "cell-new"
            elif qty > prev_qty:
                badge = f'<span class="badge up">▲ {qty - prev_qty}</span>'
                css = "cell-up"
                row_has_increase = True
            elif qty < prev_qty:
                badge = f'<span class="badge dn">▼ {prev_qty - qty}</span>'
                css = "cell-dn"
            else:
                badge = '<span class="badge eq">=</span>'
                css = "cell-eq"

            # Build subtext: last restock date + how long current qty has been steady
            sub_parts = []
            last_ev = last_increase.get((snum, upc))
            if last_ev and last_ev["ts"] != ts:
                sub_parts.append(
                    f'<span class="last-up" title="{last_ev["prev"]} → {last_ev["new"]}">'
                    f'last ↑ {_short_date(last_ev["ts"])}</span>'
                )
            # Only show steady if qty didn't just change this run
            if qty == prev_qty:
                earliest = steady_at.get((snum, upc))
                if earliest and earliest != ts:
                    secs = (datetime.strptime(ts, fmt) - datetime.strptime(earliest, fmt)).total_seconds()
                    label = _format_steady(secs)
                    if label:
                        sub_parts.append(f'<span class="steady">{label}</span>')
            sub = ("<br>" + " · ".join(sub_parts)) if sub_parts else ""

            cells.append(f'<td class="{css}"><strong>{qty}</strong> {badge}{sub}</td>')

        row_class = "row-highlight" if row_has_increase else ""
        addr = f"{s['address']}, {s['city']}, {s['state']}"
        cells_html = "\n".join(cells)
        rows.append(f"""
      <tr class="{row_class}">
        <td class="store-num">#{snum}</td>
        <td class="store-addr">{addr}</td>
        <td class="store-dist">{s['distance']:.1f} mi</td>
        {cells_html}
      </tr>""")

    upc_headers = "\n".join(f"<th>{u['name']}<br><small>{u['upc']}</small></th>" for u in upcs)
    rows_html = "\n".join(rows)
    prev_line = f"Compared to: {prev_ts}" if prev_ts else "First run — no comparison yet"

    total_stores = len(sorted_stores)
    increases = sum(
        1 for s in sorted_stores
        for upc_cfg in upcs
        if (lambda upc=upc_cfg["upc"], snum=s["storeNumber"]: (
            current_idx.get(upc, {}).get(snum) is not None and
            prev_idx.get(upc, {}).get(snum) is not None and
            current_idx[upc][snum]["productQTY"] > prev_idx[upc][snum]
        ))()
    )

    # Recent restocks panel: top N events across all of history
    recent_window = len(history)
    if restock_events:
        items = []
        for ev in restock_events[:15]:
            items.append(
                f'<li>'
                f'<span class="when">{_short_datetime(ev["ts"])}</span>'
                f'<span><strong>#{ev["store"]}</strong> '
                f'({ev["addr"].split(",")[1].strip()}, {ev["distance"]:.1f} mi)</span>'
                f'<span>{ev["name"]}:</span>'
                f'<span class="delta">{ev["prev"]} → {ev["new"]} (+{ev["delta"]})</span>'
                f'</li>'
            )
        restocks_html = f'<ul>{"".join(items)}</ul>'
    else:
        restocks_html = '<div class="empty">No restocks recorded yet in stored history.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="600">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>DG Hot Wheels Inventory</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    margin: 0; padding: 24px; background: #f0f0f0; color: #222;
  }}
  header {{ margin-bottom: 20px; }}
  h1 {{ margin: 0 0 6px; color: #c41230; font-size: 1.6rem; }}
  .meta {{ font-size: 0.88rem; color: #555; line-height: 1.7; }}
  .summary {{
    display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap;
  }}
  .stat {{
    background: white; border-radius: 8px; padding: 12px 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,.1); text-align: center;
  }}
  .stat .num {{ font-size: 1.8rem; font-weight: 700; color: #c41230; }}
  .stat .lbl {{ font-size: 0.78rem; color: #777; }}
  .legend {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; font-size: 0.82rem; }}
  .tbl-wrap {{ overflow-x: auto; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.12); }}
  table {{ width: 100%; border-collapse: collapse; background: white; min-width: 600px; }}
  th {{
    background: #c41230; color: white; padding: 11px 12px;
    text-align: left; font-size: 0.82rem; white-space: nowrap;
  }}
  th small {{ font-weight: 400; opacity: .8; font-size: 0.75rem; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #f0f0f0; font-size: 0.88rem; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.row-highlight {{ background: #fffde7 !important; }}
  tr:hover td {{ background: rgba(0,0,0,.02); }}
  .store-num {{ color: #888; font-size: 0.8rem; white-space: nowrap; }}
  .store-addr {{ }}
  .store-dist {{ white-space: nowrap; color: #666; }}
  .badge {{
    display: inline-block; font-size: 0.72rem; font-weight: 600;
    padding: 1px 5px; border-radius: 4px; margin-left: 4px; vertical-align: middle;
  }}
  .badge.up  {{ background: #e8f5e9; color: #2e7d32; }}
  .badge.dn  {{ background: #ffebee; color: #c62828; }}
  .badge.eq  {{ background: #f5f5f5; color: #999; }}
  .badge.new {{ background: #e3f2fd; color: #1565c0; }}
  .cell-up   {{ background: #f1f8f2; }}
  .cell-dn   {{ background: #fff5f5; }}
  .cell-none {{ color: #ccc; }}
  .leg-item  {{ display: flex; align-items: center; gap: 5px; }}
  .last-up   {{ display: inline-block; margin-top: 2px; font-size: 0.72rem; color: #2e7d32; }}
  .steady    {{ display: inline-block; margin-top: 2px; font-size: 0.72rem; color: #777; }}
  .restocks  {{
    background: white; border-radius: 10px; padding: 14px 16px;
    margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.1);
  }}
  .restocks h2 {{ margin: 0 0 10px; font-size: 1rem; color: #2e7d32; }}
  .restocks ul {{ list-style: none; margin: 0; padding: 0; }}
  .restocks li {{
    padding: 6px 0; border-bottom: 1px solid #f3f3f3;
    font-size: 0.85rem; display: flex; gap: 8px; flex-wrap: wrap;
  }}
  .restocks li:last-child {{ border-bottom: none; }}
  .restocks .when {{ color: #555; font-variant-numeric: tabular-nums; min-width: 95px; }}
  .restocks .delta {{ color: #2e7d32; font-weight: 600; }}
  .restocks .empty {{ color: #999; font-size: 0.85rem; }}
</style>
</head>
<body>
<header>
  <h1>🚗 DG Hot Wheels Inventory</h1>
  <div class="meta">
    <strong>Checked:</strong> {ts} &nbsp;|&nbsp; <strong>ZIP:</strong> {config['zip_code']} &nbsp;|&nbsp; <strong>Radius:</strong> {config['radius_miles']} mi<br>
    {prev_line}
  </div>
  <button onclick="location.replace(location.pathname + '?t=' + Date.now())"
          style="margin-top:10px;padding:8px 16px;font-size:0.9rem;font-weight:600;
                 color:#fff;background:#c41230;border:none;border-radius:6px;cursor:pointer">
    ↻ Refresh
  </button>
</header>

<div class="summary">
  <div class="stat"><div class="num">{total_stores}</div><div class="lbl">Stores checked</div></div>
  <div class="stat"><div class="num" style="color:#2e7d32">{increases}</div><div class="lbl">Inventory increases</div></div>
</div>

<div class="restocks">
  <h2>📦 Recent restocks (last {recent_window} runs)</h2>
  {restocks_html}
</div>

<div class="legend">
  <span class="leg-item"><span class="badge up">▲ N</span> Increased vs last check</span>
  <span class="leg-item"><span class="badge dn">▼ N</span> Decreased</span>
  <span class="leg-item"><span class="badge eq">=</span> Unchanged</span>
  <span class="leg-item"><span class="badge new">NEW</span> New store or first check</span>
  <span class="leg-item" style="background:#fffde7;padding:2px 6px;border-radius:4px">Yellow row = at least one increase</span>
  <span class="leg-item"><span class="last-up">last ↑ May X</span> Most recent restock for that cell</span>
  <span class="leg-item"><span class="steady">Nd steady</span> How long current qty has held</span>
</div>

<div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>Store #</th>
        <th>Address</th>
        <th>Distance</th>
        {upc_headers}
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</div>
<p style="color:#aaa;font-size:0.78rem;margin-top:12px">
  Run checker.py again to update. History stored in data/history.json (last 30 checks).
</p>
</body>
</html>"""

    REPORT_FILE.write_text(html)
    print(f"\nReport: {REPORT_FILE}")


def open_report():
    if os.environ.get("CI"):
        return  # running in the cloud — nothing to open
    if platform.system() == "Darwin":
        subprocess.run(["open", str(REPORT_FILE)])
    elif platform.system() == "Windows":
        os.startfile(str(REPORT_FILE))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("DG Hot Wheels Inventory Checker")
    print("=" * 40)

    # Test-alert mode: fire a fake Discord notification then exit.
    if os.environ.get("TEST_ALERT", "").lower() in ("true", "1", "yes"):
        print("TEST_ALERT mode — sending a fake restock notification...")
        fake = [{
            "store": 9999, "addr": "123 Test Street, Test City, TS", "distance": 0.0,
            "name": "🧪 Test Hot Wheels Item",
            "prev": 1, "new": 99, "delta": 98,
        }]
        notify_discord(fake, report_url=os.environ.get("REPORT_URL"))
        print("Done — check Discord.")
        return

    config = load_config()
    zip_code = config["zip_code"]
    radius = config.get("radius_miles", 10)
    upcs = config.get("upcs", [])

    print(f"ZIP: {zip_code}  |  Radius: {radius} miles  |  Tracking {len(upcs)} UPC(s)")

    print("Geocoding ZIP...")
    lat, lon = geocode_zip(zip_code)
    if not lat:
        print("ERROR: Could not geocode ZIP. Check your internet connection.")
        sys.exit(1)
    print(f"Location: {lat:.5f}, {lon:.5f}")

    session = get_session()

    history = load_history()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot = {"timestamp": ts, "zip_code": zip_code, "upcs": []}

    retried = False
    for upc_cfg in upcs:
        upc = upc_cfg["upc"]
        name = upc_cfg["name"]
        print(f"Checking {name} ({upc})...", end=" ", flush=True)
        try:
            stores = check_inventory(lat, lon, radius, upc, session)
            print(f"{len(stores)} stores")
            snapshot["upcs"].append({"upc": upc, "name": name, "stores": stores})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401 and not retried:
                print("token expired — re-minting")
                SESSION_CACHE_FILE.unlink(missing_ok=True)
                session = get_session()
                retried = True
                try:
                    stores = check_inventory(lat, lon, radius, upc, session)
                    print(f"  retry OK — {len(stores)} stores")
                    snapshot["upcs"].append({"upc": upc, "name": name, "stores": stores})
                    continue
                except Exception as e2:
                    print(f"  retry FAILED ({e2})")
            else:
                print(f"FAILED ({e})")
            snapshot["upcs"].append({"upc": upc, "name": name, "stores": []})
        except Exception as e:
            print(f"FAILED ({e})")
            snapshot["upcs"].append({"upc": upc, "name": name, "stores": []})
        time.sleep(0.8)

    save_snapshot(history, snapshot)
    history[ts] = snapshot
    previous = get_previous_snapshot(history, ts)

    new_restocks = _restocks_vs_previous(snapshot, previous, upcs)
    if new_restocks:
        print(f"Restocks this run: {len(new_restocks)}")
        notify_discord(new_restocks, report_url=os.environ.get("REPORT_URL"))

    build_report(snapshot, previous, config, history=history)
    open_report()
    print("Done!")


if __name__ == "__main__":
    main()
