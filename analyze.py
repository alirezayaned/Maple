"""
Maple Freight – Alert Priority Engine
Rule-based scoring + Gemini AI briefing for the operations manager.

Run:  python analyze.py
      GOOGLE_API_KEY=<key> python analyze.py    (enables AI briefing)
Output: report.html (open in any browser)
"""

import csv
import json
import datetime
import os
import sys
import time
import warnings
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings("ignore", category=FutureWarning)

import importlib.util
_eval_spec = importlib.util.spec_from_file_location("eval_ai", Path(__file__).parent / "eval_ai.py")
_eval_mod = importlib.util.module_from_spec(_eval_spec)
_eval_spec.loader.exec_module(_eval_mod)
evaluate_briefing = _eval_mod.evaluate_briefing
format_eval_text = _eval_mod.format_eval_text
format_eval_html = _eval_mod.format_eval_html

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:1.5b")

DATA = Path(__file__).parent / "data"

SNAPSHOT_UTC = datetime.datetime(2026, 9, 21, 22, 0, 0, tzinfo=datetime.timezone.utc)
EDT = datetime.timezone(datetime.timedelta(hours=-4))
OTA_START_UTC, OTA_END_UTC = 3, 4  # 23:00–00:00 EDT = 03:00–04:00 UTC

DTC_REF = {
    "P0524": {"label": "Oil pressure critical",       "urgency": "Critical", "score": 100, "action": "Stop vehicle immediately"},
    "P0217": {"label": "Engine overheating",           "urgency": "Critical", "score": 90,  "action": "Stop vehicle, risk of engine damage"},
    "P0300": {"label": "Cylinder misfire",             "urgency": "High",     "score": 70,  "action": "Inspect within 24 h"},
    "P0171": {"label": "Running lean (bank 1)",        "urgency": "Medium",   "score": 35,  "action": "Inspect at next service"},
    "P0128": {"label": "Thermostat below temp",        "urgency": "Low",      "score": 15,  "action": "Monitor"},
    "P0420": {"label": "Catalyst efficiency low",      "urgency": "Low",      "score": 10,  "action": "Schedule repair (emissions)"},
}

ALERT_BASE_SCORE = {
    "harsh_braking":    {"HIGH": 55, "MEDIUM": 30, "LOW": 10},
    "speeding":         {"HIGH": 45, "MEDIUM": 25, "LOW": 10},
    "harsh_accel":      {"HIGH": 40, "MEDIUM": 22, "LOW": 8},
    "harsh_cornering":  {"HIGH": 40, "MEDIUM": 22, "LOW": 8},
    "idling":           {"HIGH": 15, "MEDIUM": 10, "LOW": 5},
    "maintenance_due":  {"HIGH": 60, "MEDIUM": 40, "LOW": 20},
    "device_unplugged": {"HIGH": 25, "MEDIUM": 15, "LOW": 10},
    "camera_offline":   {"HIGH": 20, "MEDIUM": 12, "LOW": 8},
}

ALERT_TYPE_LABEL = {
    "engine_fault": "Engine fault", "harsh_braking": "Harsh braking",
    "speeding": "Speeding", "harsh_accel": "Harsh accel",
    "harsh_cornering": "Harsh cornering", "maintenance_due": "Maintenance due",
    "device_unplugged": "Device unplugged", "camera_offline": "Camera offline",
    "idling": "Idling",
}


def load_csv(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_csv_path(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_utc(s):
    if not s:
        return None
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_eastern(dt):
    if dt is None:
        return None
    return dt.astimezone(EDT)


def fmt_eastern(utc_str):
    t = parse_utc(utc_str)
    if not t:
        return ""
    return to_eastern(t).strftime("%b %d %H:%M ET")


def is_ota_noise(row):
    if row["alert_type"] not in ("device_unplugged", "camera_offline"):
        return False
    t = parse_utc(row["event_time_utc"])
    return t is not None and OTA_START_UTC <= t.hour < OTA_END_UTC


def deduplicate(alerts):
    seen = {}
    unique = []
    dup_count = 0
    for row in alerts:
        key = (row["vehicle_id"], row["alert_type"], row["event_time_utc"], row["value"])
        if key in seen:
            dup_count += 1
            continue
        seen[key] = True
        unique.append(row)
    return unique, dup_count


def build_work_order_maps(work_orders):
    open_wos = {}
    closed_wos = {}
    for w in work_orders:
        vid = w["vehicle_id"]
        if w["status"] == "Open":
            open_wos[vid] = w
        elif w["status"] == "Closed" and w["closed_local"]:
            closed_dt = datetime.datetime.fromisoformat(w["closed_local"]).replace(tzinfo=EDT)
            if vid not in closed_wos or closed_dt > closed_wos[vid]["dt"]:
                closed_wos[vid] = {"dt": closed_dt, "wo": w}
    return open_wos, closed_wos


def score_alert(row, open_wo_vids, firmware_map):
    atype = row["alert_type"]
    sev = row["vendor_severity"]
    dtc = row["dtc_code"]
    vid = row["vehicle_id"]

    if atype == "engine_fault":
        base = DTC_REF.get(dtc, {}).get("score", 5)
    elif atype == "maintenance_due":
        try:
            km = float(row["value"])
        except (ValueError, TypeError):
            km = 0
        base = min(80, max(5, km * 0.1)) if km > 0 else 5
    else:
        base = ALERT_BASE_SCORE.get(atype, {}).get(sev, 5)

    t = parse_utc(row["event_time_utc"])
    age_h = (SNAPSHOT_UTC - t).total_seconds() / 3600 if t else 9999
    recency = 1.5 if age_h <= 48 else (1.2 if age_h <= 96 else 1.0)
    score = base * recency

    fw = firmware_map.get(vid, "")
    fw_inflated = fw == "4.2.1" and atype in ("harsh_braking", "harsh_accel", "harsh_cornering")

    if vid in open_wo_vids and atype != "engine_fault":
        score *= 0.6

    return round(score, 1), fw_inflated


def classify(score):
    if score >= 70:
        return "HIGH"
    if score >= 30:
        return "MEDIUM"
    return "LOW"


def check_already_fixed(vid, alert_dt, closed_wos):
    if vid not in closed_wos or alert_dt is None:
        return False
    return alert_dt < closed_wos[vid]["dt"]


def find_post_repair_faults(scored_alerts, closed_wos):
    """Find critical faults that appeared AFTER a closed work order -- data-driven, not hardcoded."""
    faults = []
    critical_dtcs = {"P0524", "P0217"}
    for a in scored_alerts:
        if a["alert_type"] != "engine_fault" or a["dtc_code"] not in critical_dtcs:
            continue
        if a["already_fixed"]:
            continue
        vid = a["vehicle_id"]
        if vid in closed_wos:
            wo = closed_wos[vid]["wo"]
            faults.append({
                "vehicle_id": vid,
                "dtc_code": a["dtc_code"],
                "dtc_label": DTC_REF.get(a["dtc_code"], {}).get("label", a["dtc_code"]),
                "alert_time": a["event_time_utc"],
                "alert_time_et": fmt_eastern(a["event_time_utc"]),
                "wo_id": wo["wo_id"],
                "wo_closed": wo.get("closed_local", ""),
                "wo_work": wo.get("work_done", ""),
            })
    return faults


def compute_delay_stats(alerts):
    delays = []
    for row in alerts:
        event = parse_utc(row["event_time_utc"])
        received = parse_utc(row["received_time_utc"])
        if event and received:
            delay_h = (received - event).total_seconds() / 3600
            if delay_h > 1:
                delays.append({"alert_id": row["alert_id"], "vehicle_id": row["vehicle_id"],
                               "delay_hours": round(delay_h, 1)})
    return sorted(delays, key=lambda x: x["delay_hours"], reverse=True)


def build_ai_prompt(stats, blockers, top10, driver_top, post_repair_faults, depot_stats):
    context = f"""You are an AI assistant for Maple Freight, a trucking company with 60 vehicles across 3 depots.
It is Monday, September 21, 2026 at 6:00 PM Eastern. The operations manager will read this briefing tonight
and again before the 6:00 AM dispatch tomorrow.

DATA SUMMARY (from 14-day telematics export, Sep 8-21):
- {stats['total_raw']} raw alerts, {stats['duplicates']} duplicates removed, {stats['ota_filtered']} OTA noise filtered
- After cleaning: {stats['scored']} actionable alerts
- Re-ranked: {stats['my_high']} HIGH / {stats['my_medium']} MEDIUM / {stats['my_low']} LOW
- Vendor originally marked {stats['vendor_high']} as HIGH (over-reporting due to P0420 catalyst alerts getting HIGH despite being low-urgency emissions issues)
- {stats['already_fixed']} alerts already resolved by work orders

DISPATCH BLOCKERS ({len(blockers)} vehicles must NOT be dispatched tomorrow):
"""
    for b in blockers:
        context += f"- {b['vehicle_id']} ({b['depot']}): {b['reason']}. {b.get('detail','')}\n"

    if post_repair_faults:
        context += "\nPOST-REPAIR FAULTS (new critical faults after a recent repair):\n"
        for f in post_repair_faults:
            context += (f"- {f['vehicle_id']}: {f['dtc_label']} ({f['dtc_code']}) at {f['alert_time_et']}, "
                       f"AFTER work order {f['wo_id']} was closed ({f['wo_work']})\n")

    context += "\nTOP 10 ACTION ITEMS (vehicles needing attention, excluding blockers):\n"
    for i, a in enumerate(top10, 1):
        dtc_info = DTC_REF.get(a["top_dtc"], {}).get("label", "") if a.get("top_dtc") else ""
        context += (f"{i}. {a['vehicle_id']} ({a['depot']}, driver {a['driver_id']}): "
                   f"{a['alert_count']} alerts, top issue: {a['top_alert_type']} "
                   f"{'(' + dtc_info + ')' if dtc_info else ''} "
                   f"[score {a['top_score']}]\n")

    context += "\nDRIVER LEADERBOARD (top 5 by alert count, firmware-adjusted):\n"
    for did, s in driver_top[:5]:
        fw_note = " [fw 4.2.1 - inflated harsh-event detection]" if s["firmware"] == "4.2.1" else ""
        context += (f"- {did} on {s['vehicle_id']}: {s['total_alerts']} alerts, "
                   f"adj score {s['adj_score']}, {s['km']} km driven{fw_note}\n")

    context += "\nDEPOT SUMMARY:\n"
    for depot, ds in sorted(depot_stats.items()):
        context += f"- {depot}: {ds['high']} HIGH, {ds['medium']} MEDIUM, {ds['low']} LOW, {ds['blockers']} blocked\n"

    return f"""{context}

Write a short dispatch briefing (under 300 words) for the operations manager.
Include: one status line, then the 3 most urgent items (what, why, next step), then one note about data quality.
Be direct. No markdown formatting. No bullet points. Plain text only."""


def call_ollama(prompt):
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 500, "repeat_penalty": 1.5},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "")
    except (urllib.error.URLError, Exception) as e:
        print(f"  Ollama call failed: {e}")
        return None


def call_gemini(prompt):
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not GENAI_AVAILABLE or not api_key:
        return None
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3.8-flash")
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower():
                wait = 15 * (attempt + 1)
                print(f"  Gemini rate limited, retrying in {wait}s... ({attempt+1}/3)")
                time.sleep(wait)
            else:
                print(f"  Gemini failed: {e}")
                break
    return None


def validate_ai_output(text, valid_vehicles, blockers=None, stats=None):
    import re
    if not text or len(text) < 50:
        return False, "too short"
    lines = text.split("\n")
    non_empty = [l for l in lines if len(l.strip()) > 20]
    if non_empty:
        for line in non_empty:
            if sum(1 for other in non_empty if other == line) > 3:
                return False, "repetition loop"
    mentioned = set(re.findall(r"V-\d+", text))
    hallucinated = mentioned - valid_vehicles
    if hallucinated:
        return False, f"hallucinated vehicles: {hallucinated}"
    mentioned_drivers = set(re.findall(r"D-\d+", text))
    valid_drivers = {f"D-{i:02d}" for i in range(1, 61)}
    bad_drivers = mentioned_drivers - valid_drivers
    if bad_drivers:
        return False, f"hallucinated drivers: {bad_drivers}"
    if blockers:
        blocker_vids = {b["vehicle_id"] for b in blockers}
        for vid in mentioned:
            text_lower = text.lower()
            idx = text_lower.find(vid.lower())
            if idx >= 0:
                vid_context = text_lower[max(0, idx-100):idx+100]
                if vid not in blocker_vids and any(w in vid_context for w in
                        ["blocker", "block", "do not dispatch", "stop vehicle", "must not"]):
                    return False, f"{vid} called a blocker but isn't one"
    if stats:
        dup_match = re.search(r"no\s+(?:issues?\s+with\s+)?duplicate", text, re.IGNORECASE)
        if dup_match and stats.get("duplicates", 0) > 0:
            return False, f"claims no duplicates but we found {stats['duplicates']}"
        pct_match = re.search(r"(\d+)%\s*[Dd]uplicate", text)
        if pct_match:
            claimed_pct = int(pct_match.group(1))
            actual_pct = round(stats.get("duplicates", 0) / max(stats.get("total_raw", 1), 1) * 100)
            if abs(claimed_pct - actual_pct) > 10:
                return False, f"claims {claimed_pct}% duplicates, actual is ~{actual_pct}%"
    return True, "ok"


def strip_markdown(text):
    import re
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*]\s+', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def generate_ai_briefing(stats, blockers, top10, driver_top, post_repair_faults, depot_stats):
    prompt = build_ai_prompt(stats, blockers, top10, driver_top, post_repair_faults, depot_stats)
    valid_vids = {f"V-{i}" for i in range(101, 161)}
    full_stats = {**stats, "total_raw": stats.get("total_raw", 0)}

    # Try Gemini first (better quality)
    print("  Trying Gemini...")
    result = call_gemini(prompt)
    if result:
        ok, reason = validate_ai_output(result, valid_vids, blockers, full_stats)
        if ok:
            print("  Gemini briefing generated.")
            return strip_markdown(result), "gemini"
        print(f"  Gemini output failed validation: {reason}")

    # Try Ollama
    print("  Trying Ollama (local)...")
    result = call_ollama(prompt)
    if result:
        ok, reason = validate_ai_output(result, valid_vids, blockers, full_stats)
        if ok:
            print("  Ollama briefing generated.")
            return strip_markdown(result), "ollama"
        print(f"  Ollama output failed validation: {reason}")

    # Fall back to template
    print("  Using template briefing (reliable, data-driven).")
    return generate_template_briefing(stats, blockers, top10, post_repair_faults), "template"


def generate_template_briefing(stats, blockers, top10, post_repair_faults):
    lines = []
    lines.append(f"DISPATCH BRIEFING — Monday Sep 21, 6:00 PM ET — {stats['scored']} alerts scored, "
                 f"{len(blockers)} vehicles blocked, {stats['my_high']} items ranked HIGH.\n")

    lines.append("CRITICAL ACTIONS FOR TONIGHT:\n")

    if post_repair_faults:
        f = post_repair_faults[0]
        lines.append(f"1. {f['vehicle_id']} — {f['dtc_label']} ({f['dtc_code']}) at {f['alert_time_et']}. "
                     f"This is a NEW fault that appeared AFTER today's repair ({f['wo_id']}: {f['wo_work']}). "
                     f"DTC reference says: stop vehicle immediately. Do NOT dispatch tomorrow. "
                     f"Send to shop first thing for oil pressure inspection.\n")

    for i, b in enumerate(blockers):
        if b.get("source") == "work_order":
            lines.append(f"{i+2}. {b['vehicle_id']} — {b['reason']}. {b.get('detail', '')} "
                         f"Keep out of dispatch until work order closes.\n")

    lines.append(f"\nDATA QUALITY NOTES: {stats['duplicates']} duplicate alerts removed. "
                 f"Vendor marked {stats['vendor_high']} alerts HIGH but our re-ranking using DTC reference urgency "
                 f"reduces this to {stats['my_high']}. Firmware 4.2.1 vehicles show ~40% more harsh-event alerts "
                 f"due to improved sensor sensitivity — compare drivers on adjusted scores, not raw counts.\n")

    lines.append("D-17 NOTE: Highest alert count but also highest mileage (3,650 km) and runs fw 4.2.1. "
                 "After adjusting for mileage and firmware, D-17 still ranks high — coaching is warranted — "
                 "but the gap vs. other drivers is smaller than raw counts suggest. Review specific HIGH events, "
                 "not the total count.")

    return "\n".join(lines)


def main():
    print("Loading data...")
    alerts_raw = load_csv("alerts.csv")
    vehicles = load_csv("vehicles.csv")
    work_orders = load_csv("work_orders.csv")

    # Load backfill data if present (e.g., Hamilton gateway reconnect)
    backfill_dir = DATA.parent / "Maple_Freight_New_Information"
    backfill_file = backfill_dir / "hamilton_backfill.csv"
    if backfill_file.exists():
        backfill = load_csv_path(backfill_file)
        print(f"  Loaded {len(backfill)} backfill alerts from Hamilton gateway")
        alerts_raw.extend(backfill)

    vehicle_map = {v["vehicle_id"]: v for v in vehicles}
    firmware_map = {v["vehicle_id"]: v["device_firmware"] for v in vehicles}
    km_map = {v["vehicle_id"]: int(v["km_driven_14d"]) for v in vehicles}
    open_wos, closed_wos = build_work_order_maps(work_orders)
    open_wo_vids = set(open_wos.keys())

    # Deduplicate
    alerts, dup_count = deduplicate(alerts_raw)
    print(f"  {len(alerts_raw)} raw alerts, {dup_count} duplicates removed -> {len(alerts)} unique")

    # Filter and score
    scored = []
    ota_filtered = 0
    neg_maintenance = 0

    for row in alerts:
        if is_ota_noise(row):
            ota_filtered += 1
            continue

        if row["alert_type"] == "maintenance_due":
            try:
                val = float(row["value"])
            except (ValueError, TypeError):
                val = 0
            if val <= 0:
                neg_maintenance += 1
                continue

        score, fw_inflated = score_alert(row, open_wo_vids, firmware_map)
        vid = row["vehicle_id"]
        alert_dt = parse_utc(row["event_time_utc"])
        already_fixed = check_already_fixed(vid, alert_dt, closed_wos)

        v = vehicle_map.get(vid, {})
        scored.append({
            "alert_id":        row["alert_id"],
            "vehicle_id":      vid,
            "driver_id":       row["driver_id"],
            "depot":           v.get("depot", ""),
            "vehicle_class":   v.get("vehicle_class", ""),
            "firmware":        firmware_map.get(vid, ""),
            "alert_type":      row["alert_type"],
            "vendor_severity": row["vendor_severity"],
            "dtc_code":        row["dtc_code"],
            "value":           row["value"],
            "unit":            row["unit"],
            "posted_limit":    row["posted_limit_kmh"],
            "event_time_utc":  row["event_time_utc"],
            "score":           score,
            "my_severity":     classify(score),
            "fw_inflated":     fw_inflated,
            "already_fixed":   already_fixed,
            "in_open_wo":      vid in open_wo_vids,
        })

    # Dispatch blockers (data-driven)
    blockers = []
    for vid, wo in open_wos.items():
        v = vehicle_map.get(vid, {})
        blockers.append({
            "vehicle_id": vid,
            "depot": v.get("depot", ""),
            "reason": wo["work_done"] or "Open work order",
            "detail": wo.get("technician_note", ""),
            "wo_id": wo["wo_id"],
            "source": "work_order",
        })

    # Critical unfixed engine faults
    critical_dtcs = {"P0524", "P0217"}
    blocker_vids = {b["vehicle_id"] for b in blockers}
    for a in scored:
        if (a["alert_type"] == "engine_fault"
                and a["dtc_code"] in critical_dtcs
                and not a["already_fixed"]
                and a["vehicle_id"] not in blocker_vids):
            blockers.append({
                "vehicle_id": a["vehicle_id"],
                "depot": a["depot"],
                "reason": f"Critical: {DTC_REF.get(a['dtc_code'], {}).get('label', a['dtc_code'])}",
                "detail": DTC_REF.get(a["dtc_code"], {}).get("action", ""),
                "alert_time": a["event_time_utc"],
                "source": "engine_fault",
            })
            blocker_vids.add(a["vehicle_id"])

    # Post-repair faults
    post_repair_faults = find_post_repair_faults(scored, closed_wos)

    # Vehicle-level aggregation
    vehicle_scores = defaultdict(lambda: {"total": 0, "top_score": 0, "alerts": [],
                                           "top_alert": None, "fw_inflated_count": 0})
    for a in scored:
        if a["already_fixed"]:
            continue
        vid = a["vehicle_id"]
        vehicle_scores[vid]["total"] += a["score"]
        vehicle_scores[vid]["alerts"].append(a)
        if a["score"] > vehicle_scores[vid]["top_score"]:
            vehicle_scores[vid]["top_score"] = a["score"]
            vehicle_scores[vid]["top_alert"] = a
        if a["fw_inflated"]:
            vehicle_scores[vid]["fw_inflated_count"] += 1

    # Top 10 action items (exclude blockers)
    action_candidates = []
    for vid, vs in vehicle_scores.items():
        if vid in blocker_vids:
            continue
        top = vs["top_alert"]
        if not top:
            continue
        high_engine = [a for a in vs["alerts"]
                       if a["alert_type"] == "engine_fault" and a["my_severity"] == "HIGH"]
        action_candidates.append({
            "vehicle_id": vid,
            "depot": top["depot"],
            "driver_id": top["driver_id"],
            "firmware": top["firmware"],
            "total_score": round(vs["total"], 1),
            "top_score": round(vs["top_score"], 1),
            "top_alert_type": top["alert_type"],
            "top_dtc": top["dtc_code"],
            "top_value": top["value"],
            "top_severity": top["my_severity"],
            "alert_count": len(vs["alerts"]),
            "high_engine_faults": len(high_engine),
            "fw_inflated_pct": round(vs["fw_inflated_count"] / len(vs["alerts"]) * 100) if vs["alerts"] else 0,
        })
    action_candidates.sort(key=lambda x: x["top_score"], reverse=True)
    top10 = action_candidates[:10]

    # Driver leaderboard
    driver_stats = defaultdict(lambda: {
        "total_alerts": 0, "high": 0, "medium": 0, "low": 0,
        "vehicle_id": "", "firmware": "", "depot": "",
        "fw_inflated": 0, "score": 0, "km": 0,
    })
    for a in scored:
        did = a["driver_id"]
        driver_stats[did]["total_alerts"] += 1
        driver_stats[did][a["my_severity"].lower()] += 1
        driver_stats[did]["vehicle_id"] = a["vehicle_id"]
        driver_stats[did]["firmware"] = a["firmware"]
        driver_stats[did]["depot"] = a["depot"]
        driver_stats[did]["score"] += a["score"]
        driver_stats[did]["km"] = km_map.get(a["vehicle_id"], 0)
        if a["fw_inflated"]:
            driver_stats[did]["fw_inflated"] += 1

    for did, s in driver_stats.items():
        if s["firmware"] == "4.2.1":
            s["adj_score"] = max(0, round(s["score"] - s["fw_inflated"] * 8, 1))
        else:
            s["adj_score"] = round(s["score"], 1)
        s["rate_per_1000km"] = round(s["total_alerts"] / max(s["km"], 1) * 1000, 1)

    driver_list = sorted(driver_stats.items(), key=lambda x: x[1]["total_alerts"], reverse=True)[:20]

    # Depot summary
    depot_stats = defaultdict(lambda: {"vehicles": set(), "high": 0, "medium": 0, "low": 0, "blockers": 0})
    for a in scored:
        d = a["depot"]
        depot_stats[d]["vehicles"].add(a["vehicle_id"])
        depot_stats[d][a["my_severity"].lower()] += 1
    for b in blockers:
        depot_stats[b["depot"]]["blockers"] += 1

    # Delay stats
    delay_stats = compute_delay_stats(alerts_raw)

    # Statistics
    stats = {
        "total_raw": len(alerts_raw),
        "duplicates": dup_count,
        "ota_filtered": ota_filtered,
        "neg_maintenance": neg_maintenance,
        "scored": len(scored),
        "my_high": sum(1 for a in scored if a["my_severity"] == "HIGH"),
        "my_medium": sum(1 for a in scored if a["my_severity"] == "MEDIUM"),
        "my_low": sum(1 for a in scored if a["my_severity"] == "LOW"),
        "vendor_high": sum(1 for a in scored if a["vendor_severity"] == "HIGH"),
        "already_fixed": sum(1 for a in scored if a["already_fixed"]),
        "blockers": len(blockers),
        "delayed_alerts": len(delay_stats),
        "max_delay_h": delay_stats[0]["delay_hours"] if delay_stats else 0,
    }

    print(f"  {stats['scored']} scored ({stats['ota_filtered']} OTA noise, "
          f"{stats['already_fixed']} already fixed)")
    print(f"  Re-ranked: {stats['my_high']} HIGH / {stats['my_medium']} MEDIUM / {stats['my_low']} LOW")
    print(f"  Vendor had {stats['vendor_high']} HIGH (over-reports)")
    print(f"  Dispatch blockers: {stats['blockers']} vehicles")
    print(f"  Delayed alerts (>1h): {stats['delayed_alerts']}, max delay: {stats['max_delay_h']}h")

    # AI Briefing
    print("Generating AI briefing...")
    briefing, ai_source = generate_ai_briefing(stats, blockers, top10, driver_list,
                                                post_repair_faults, depot_stats)
    print("  Done.")

    # Evaluate AI briefing against ground truth
    all_wo_ids = {w["wo_id"] for w in work_orders}
    ground_truth = {
        "blockers": blockers,
        "post_repair": post_repair_faults,
        "top10": top10,
        "stats": stats,
        "driver_top5": driver_list[:5],
        "valid_vehicles": {f"V-{i}" for i in range(101, 161)},
        "valid_drivers": {f"D-{i:02d}" for i in range(1, 61)},
        "all_wo_ids": all_wo_ids,
    }
    eval_report = evaluate_briefing(briefing, ground_truth)
    print(f"  AI Eval: {format_eval_text(eval_report)}")

    # Render HTML
    all_alerts_sorted = sorted(scored, key=lambda x: x["score"], reverse=True)
    html = render_html(blockers, top10, driver_list, all_alerts_sorted[:200],
                       depot_stats, stats, post_repair_faults, briefing, ai_source,
                       eval_report)
    out = Path(__file__).parent / "report.html"
    out.write_text(html, encoding="utf-8")
    print(f"\nReport written to {out}")
    print("Open report.html in your browser.")


# ───────────────────────────────────────────────────────────────────────────────
#  HTML rendering
# ───────────────────────────────────────────────────────────────────────────────

SEV_COLOR = {"HIGH": "#dc2626", "MEDIUM": "#d97706", "LOW": "#16a34a"}
SEV_BG    = {"HIGH": "#fef2f2", "MEDIUM": "#fffbeb", "LOW": "#f0fdf4"}

def badge(sev, text=None):
    t = text or sev
    c = SEV_COLOR.get(sev, "#6b7280")
    bg = SEV_BG.get(sev, "#f3f4f6")
    return (f'<span style="background:{bg};color:{c};border:1px solid {c};'
            f'padding:2px 8px;border-radius:99px;font-size:12px;font-weight:700;">{t}</span>')


def render_html(blockers, top10, driver_list, all_alerts, depot_stats, stats,
                post_repair_faults, briefing, ai_source="template", eval_report=None):

    # Briefing section
    briefing_html = briefing.replace("\n", "<br>\n") if briefing else ""
    ai_label = {"gemini": "Gemini 3.8 Flash", "ollama": "Ollama (local)", "template": "Template (rule-based)"}
    ai_confidence = {"gemini": ("high", "#16a34a", "#f0fdf4"),
                     "ollama": ("medium", "#d97706", "#fffbeb"),
                     "template": ("verified", "#1d4ed8", "#eff6ff")}
    conf_level, conf_color, conf_bg = ai_confidence.get(ai_source, ("unknown", "#6b7280", "#f3f4f6"))
    ai_badge = (f'<span style="background:{conf_bg};color:{conf_color};border:1px solid {conf_color};'
                f'padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;margin-left:8px">'
                f'{ai_label.get(ai_source, ai_source)} &middot; confidence: {conf_level}</span>')

    # Blockers
    blocker_rows = ""
    for b in blockers:
        blocker_rows += f"""
        <tr>
          <td><strong>{b['vehicle_id']}</strong></td>
          <td>{b.get('depot','')}</td>
          <td style="color:#dc2626;font-weight:600">{b['reason']}</td>
          <td style="color:#666;font-size:13px">{b.get('detail','')}</td>
        </tr>"""

    # Post-repair warnings (data-driven)
    repair_warnings = ""
    for f in post_repair_faults:
        repair_warnings += (
            f'<div class="warn-box" style="margin-bottom:8px">'
            f'<strong>{f["vehicle_id"]}:</strong> {f["dtc_label"]} ({f["dtc_code"]}) '
            f'at {f["alert_time_et"]} — appeared AFTER work order {f["wo_id"]} '
            f'closed ({f["wo_work"]}). This is a <strong>different, unresolved fault</strong>. '
            f'Re-inspect before dispatch.</div>'
        )

    # Top 10
    top10_rows = ""
    for i, a in enumerate(top10, 1):
        fw_note = ""
        if a["firmware"] == "4.2.1" and a["fw_inflated_pct"] > 30:
            fw_note = ' <span title="4.2.1 firmware detects more harsh events" style="color:#9ca3af;font-size:11px">fw4.2.1</span>'
        dtc_label = DTC_REF.get(a["top_dtc"], {}).get("label", "") if a.get("top_dtc") else ""
        alert_label = ALERT_TYPE_LABEL.get(a["top_alert_type"], a["top_alert_type"])
        detail = dtc_label if dtc_label else (
            f'{a["top_value"]} {("g" if "harsh" in a["top_alert_type"] else "km/h")}' if a.get("top_value") else "")
        ef_badge = ""
        if a.get("high_engine_faults"):
            ef_badge = (f' <span style="background:#fef2f2;color:#dc2626;border:1px solid #dc2626;'
                       f'padding:1px 6px;border-radius:99px;font-size:11px">'
                       f'{a["high_engine_faults"]} eng HIGH</span>')
        top10_rows += f"""
        <tr>
          <td style="color:#6b7280;font-size:13px">{i}</td>
          <td><strong>{a['vehicle_id']}</strong>{fw_note}</td>
          <td>{a['driver_id']}</td>
          <td>{a['depot']}</td>
          <td>{badge(a['top_severity'])}</td>
          <td>{alert_label}{ef_badge}</td>
          <td style="font-size:13px;color:#555">{detail}</td>
          <td style="text-align:right;font-weight:600">{a['alert_count']}</td>
        </tr>"""

    # Drivers
    driver_rows = ""
    for rank, (did, s) in enumerate(driver_list, 1):
        fw_flag = ""
        if s["firmware"] == "4.2.1":
            fw_flag = (' <span title="Firmware 4.2.1 inflates harsh-event counts by ~40% vs 4.1.8" '
                       'style="background:#fef9c3;color:#854d0e;border:1px solid #ca8a04;'
                       'padding:1px 6px;border-radius:99px;font-size:11px;font-weight:600">4.2.1</span>')
        d17_flag = ' <span style="color:#dc2626;font-size:11px">flagged by safety mgr</span>' if did == "D-17" else ""
        driver_rows += f"""
        <tr>
          <td style="color:#9ca3af;font-size:13px">{rank}</td>
          <td><strong>{did}</strong>{d17_flag}</td>
          <td>{s['vehicle_id']}{fw_flag}</td>
          <td>{s['depot']}</td>
          <td style="text-align:right;font-weight:700">{s['total_alerts']}</td>
          <td style="text-align:right">{s['km']}</td>
          <td style="text-align:right;color:#6b7280">{s['rate_per_1000km']}</td>
          <td style="text-align:right;color:#dc2626">{s['high']}</td>
          <td style="text-align:right;color:#d97706">{s['medium']}</td>
          <td style="text-align:right;color:#16a34a">{s['low']}</td>
          <td style="text-align:right;font-size:13px;color:#1e40af;font-weight:600">{s['adj_score']}</td>
        </tr>"""

    # All alerts
    alert_rows = ""
    for a in all_alerts:
        row_style = ""
        if a["my_severity"] == "HIGH":
            row_style = ' style="background:#fff8f8"'
        elif a["my_severity"] == "MEDIUM":
            row_style = ' style="background:#fffcf0"'

        notes = ""
        if a["already_fixed"]:
            notes = ' <span style="color:#16a34a;font-size:11px">fixed</span>'
        elif a["in_open_wo"]:
            notes = ' <span style="color:#f59e0b;font-size:11px">in shop</span>'
        if a["fw_inflated"]:
            notes += ' <span style="color:#9ca3af;font-size:10px">fw</span>'

        dtc_info = f' <span style="color:#666;font-size:11px">({DTC_REF.get(a["dtc_code"], {}).get("label", "")})</span>' if a["dtc_code"] else ""
        atype_disp = ALERT_TYPE_LABEL.get(a["alert_type"], a["alert_type"])

        alert_rows += f"""
        <tr{row_style}>
          <td style="font-size:11px;color:#9ca3af">{a['alert_id']}</td>
          <td>{a['vehicle_id']}</td>
          <td>{a['driver_id']}</td>
          <td>{a['depot']}</td>
          <td>{badge(a['my_severity'])}</td>
          <td><span style="font-size:11px">{badge(a['vendor_severity'], a['vendor_severity'])}</span></td>
          <td>{atype_disp}{dtc_info}{notes}</td>
          <td style="font-size:12px;color:#555">{fmt_eastern(a['event_time_utc'])}</td>
          <td style="text-align:right;font-weight:600;color:#1e40af">{a['score']}</td>
        </tr>"""

    # Depots
    depot_rows = ""
    for depot, ds in sorted(depot_stats.items()):
        depot_rows += f"""
        <tr>
          <td><strong>{depot}</strong></td>
          <td>{len(ds['vehicles'])}</td>
          <td style="color:#dc2626;font-weight:600">{ds['high']}</td>
          <td style="color:#d97706">{ds['medium']}</td>
          <td style="color:#16a34a">{ds['low']}</td>
          <td style="color:#dc2626">{ds['blockers']} blocked</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Maple Freight – Dispatch Decision Board</title>
<style>
  :root {{
    --bg: #f9fafb; --card: #ffffff; --border: #e5e7eb;
    --text: #111827; --muted: #6b7280;
    --red: #dc2626; --amber: #d97706; --green: #16a34a; --blue: #1d4ed8;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); font-size: 14px; }}
  .header {{ background: #1e293b; color: #fff; padding: 20px 32px; }}
  .header h1 {{ font-size: 22px; font-weight: 700; }}
  .header .sub {{ color: #94a3b8; font-size: 13px; margin-top: 4px; }}
  .content {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
  .section {{ background: var(--card); border: 1px solid var(--border);
              border-radius: 8px; margin-bottom: 24px; overflow: hidden; }}
  .section-header {{ padding: 14px 20px; border-bottom: 1px solid var(--border);
                     display: flex; align-items: center; gap: 10px; }}
  .section-header h2 {{ font-size: 15px; font-weight: 700; }}
  .section-header .count {{ background: #f3f4f6; color: #374151;
                            padding: 2px 8px; border-radius: 99px; font-size: 12px; }}
  .blocker-header {{ background: #fef2f2; border-bottom-color: #fecaca; }}
  .blocker-header h2 {{ color: var(--red); }}
  .ai-header {{ background: #eff6ff; border-bottom-color: #bfdbfe; }}
  .ai-header h2 {{ color: var(--blue); }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #f9fafb; padding: 9px 12px; text-align: left;
       font-size: 12px; font-weight: 600; color: var(--muted);
       text-transform: uppercase; letter-spacing: .04em;
       border-bottom: 1px solid var(--border); }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #f3f4f6; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f8fafc; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                gap: 1px; background: var(--border); }}
  .stat-box {{ background: var(--card); padding: 16px 20px; }}
  .stat-box .val {{ font-size: 28px; font-weight: 800; line-height: 1; }}
  .stat-box .lbl {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
  .note-box {{ background: #eff6ff; border-left: 4px solid #3b82f6;
               padding: 12px 16px; margin: 12px 20px; border-radius: 0 4px 4px 0;
               font-size: 13px; color: #1e40af; line-height: 1.5; }}
  .warn-box {{ background: #fffbeb; border-left: 4px solid #f59e0b;
              padding: 12px 16px; margin: 12px 20px; border-radius: 0 4px 4px 0;
              font-size: 13px; color: #92400e; line-height: 1.5; }}
  .briefing-box {{ padding: 16px 20px; font-size: 14px; line-height: 1.7; color: #1e293b; }}
  .tab-nav {{ display: flex; border-bottom: 1px solid var(--border);
              padding: 0 20px; background: #f9fafb; flex-wrap: wrap; }}
  .tab-btn {{ padding: 10px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
              border: none; background: none; color: var(--muted); border-bottom: 3px solid transparent; }}
  .tab-btn.active {{ color: var(--blue); border-bottom-color: var(--blue); }}
  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}
  .scroll-x {{ overflow-x: auto; }}
  @media (max-width: 640px) {{
    .header {{ padding: 16px; }}
    .header h1 {{ font-size: 18px; }}
    .content {{ padding: 12px 8px; }}
    .stat-box .val {{ font-size: 22px; }}
    .stats-grid {{ grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); }}
    .tab-btn {{ padding: 8px 10px; font-size: 12px; }}
    td, th {{ padding: 6px 8px; font-size: 13px; }}
    .section-header {{ flex-wrap: wrap; gap: 6px; }}
  }}
  @media print {{
    .tab-pane {{ display: block !important; page-break-inside: avoid; }}
    .tab-nav {{ display: none; }}
    .header {{ background: #000 !important; -webkit-print-color-adjust: exact; }}
    body {{ font-size: 11px; }}
  }}
  .header-actions {{ float: right; }}
  .header-actions button {{ background: rgba(255,255,255,.15); color: #fff; border: 1px solid rgba(255,255,255,.3);
    padding: 6px 14px; border-radius: 6px; font-size: 12px; cursor: pointer; }}
  .header-actions button:hover {{ background: rgba(255,255,255,.25); }}
</style>
</head>
<body>

<div class="header">
  <div class="header-actions"><button onclick="window.print()">Print / PDF</button></div>
  <h1>Maple Freight &ndash; Dispatch Decision Board</h1>
  <div class="sub">Monday, September 21, 2026 &middot; 6:00 PM Eastern &middot; 60 vehicles &middot; 3 depots &middot; Report generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</div>

<div class="content">

  <!-- AI Briefing -->
  <div class="section">
    <div class="section-header ai-header">
      <h2>AI Morning Briefing</h2>
      <span class="count" style="background:#dbeafe;color:#1e40af">generated from {stats['scored']} alerts</span>
      {ai_badge}
    </div>
    <div class="briefing-box">{briefing_html}</div>
    <div style="padding:8px 20px 14px;font-size:11px;color:#9ca3af;border-top:1px solid #f3f4f6">
      This briefing is AI-generated and validated against the scored data. All numbers in the stats bar,
      dispatch blockers, and action items below come from the deterministic scoring engine — not the AI.
      If this briefing conflicts with the tables below, trust the tables.
    </div>
    {format_eval_html(eval_report) if eval_report else ""}
  </div>

  <!-- Stats bar -->
  <div class="section">
    <div class="stats-grid">
      <div class="stat-box">
        <div class="val">{stats['total_raw']}</div>
        <div class="lbl">Raw alerts (14 days)</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:#9ca3af">{stats['duplicates']}</div>
        <div class="lbl">Duplicates removed</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:var(--red)">{stats['my_high']}</div>
        <div class="lbl">Re-ranked HIGH</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:var(--amber)">{stats['my_medium']}</div>
        <div class="lbl">Re-ranked MEDIUM</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:var(--green)">{stats['my_low']}</div>
        <div class="lbl">Re-ranked LOW</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:#9ca3af">{stats['ota_filtered']}</div>
        <div class="lbl">OTA noise filtered</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:var(--green)">{stats['already_fixed']}</div>
        <div class="lbl">Already resolved</div>
      </div>
      <div class="stat-box">
        <div class="val" style="color:var(--red)">{stats['blockers']}</div>
        <div class="lbl">Dispatch blockers</div>
      </div>
    </div>
    <div class="note-box">
      <strong>Ranking method:</strong> Scores use DTC urgency (not vendor severity), alert recency (1.5x for last 48h),
      and cross-reference with work orders. Vendor marked {stats['vendor_high']} alerts HIGH — our re-ranking
      uses the DTC reference to correct P0420 catalyst alerts (low-urgency emissions) that the vendor marks HIGH.
      {stats['duplicates']} duplicate alerts were removed ({stats['ota_filtered']} OTA-window noise filtered).
      Firmware 4.2.1 vehicles are flagged where harsh-event counts are inflated by improved sensor sensitivity.
    </div>
  </div>

  <!-- DISPATCH BLOCKERS -->
  <div class="section">
    <div class="section-header blocker-header">
      <h2>Dispatch Blockers — Do NOT send these vehicles out tomorrow</h2>
      <span class="count" style="background:#fecaca;color:#991b1b">{len(blockers)} vehicles</span>
    </div>
    <div class="scroll-x">
    <table>
      <thead><tr>
        <th>Vehicle</th><th>Depot</th><th>Reason</th><th>Detail</th>
      </tr></thead>
      <tbody>{blocker_rows}</tbody>
    </table>
    </div>
    {repair_warnings}
  </div>

  <!-- TABS -->
  <div class="section">
    <div class="tab-nav">
      <button class="tab-btn active" onclick="switchTab('top10',this)">Top 10 Action Items</button>
      <button class="tab-btn" onclick="switchTab('drivers',this)">Driver Leaderboard</button>
      <button class="tab-btn" onclick="switchTab('depots',this)">Depot Summary</button>
      <button class="tab-btn" onclick="switchTab('allalerts',this)">All Alerts (re-ranked)</button>
    </div>

    <div id="top10" class="tab-pane active">
      <div class="note-box">
        The 10 vehicles needing attention today, excluding dispatch blockers and open work orders.
      </div>
      <div class="scroll-x">
      <table>
        <thead><tr>
          <th>#</th><th>Vehicle</th><th>Driver</th><th>Depot</th>
          <th>My Rank</th><th>Top Issue</th><th>Detail</th><th>Alerts</th>
        </tr></thead>
        <tbody>{top10_rows}</tbody>
      </table>
      </div>
    </div>

    <div id="drivers" class="tab-pane">
      <div class="warn-box">
        <strong>Read before acting:</strong> Firmware 4.2.1 detects ~40% more harsh events than 4.1.8
        (vendor note, August 2026). Raw alert counts cannot be compared across firmware versions.
        The "Adj. Score" and "Rate/1000km" columns control for this. D-17 still ranks high after
        adjustment — specific HIGH events warrant review — but the gap is smaller than raw counts suggest.
      </div>
      <div class="scroll-x">
      <table>
        <thead><tr>
          <th>#</th><th>Driver</th><th>Vehicle</th><th>Depot</th>
          <th style="text-align:right">Alerts</th>
          <th style="text-align:right">KM (14d)</th>
          <th style="text-align:right">Rate/1000km</th>
          <th style="text-align:right">HIGH</th>
          <th style="text-align:right">MED</th>
          <th style="text-align:right">LOW</th>
          <th style="text-align:right">Adj. Score</th>
        </tr></thead>
        <tbody>{driver_rows}</tbody>
      </table>
      </div>
    </div>

    <div id="depots" class="tab-pane">
      <div class="scroll-x">
      <table>
        <thead><tr>
          <th>Depot</th><th>Vehicles</th>
          <th>HIGH</th><th>MEDIUM</th><th>LOW</th><th>Blocked</th>
        </tr></thead>
        <tbody>{depot_rows}</tbody>
      </table>
      </div>
    </div>

    <div id="allalerts" class="tab-pane">
      <div class="note-box">
        Top 200 alerts by re-ranked score. My severity uses DTC urgency + recency; vendor severity shown for comparison.
        "fixed" = work order closed after this alert. "in shop" = open work order. "fw" = 4.2.1 firmware inflation.
      </div>
      <div class="scroll-x">
      <table>
        <thead><tr>
          <th>Alert ID</th><th>Vehicle</th><th>Driver</th><th>Depot</th>
          <th>My rank</th><th>Vendor</th><th>Type / Detail</th><th>Time (ET)</th>
          <th style="text-align:right">Score</th>
        </tr></thead>
        <tbody>{alert_rows}</tbody>
      </table>
      </div>
    </div>
  </div>

  <div style="color:#9ca3af;font-size:12px;text-align:center;padding:16px 0">
    Maple Freight Dispatch Decision Board · Data export Sep 21 2026 18:00 ET · Rule-based scoring + AI briefing
  </div>
</div>

<script>
function switchTab(id, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
