"""
Smoke tests and validation tests for Maple Freight Alert Priority Engine.
Run: python tests.py
"""

import sys
import os
import datetime
import re

sys.path.insert(0, os.path.dirname(__file__))
import analyze
from eval_ai import evaluate_briefing

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def test_data_loading():
    print("\n--- Data Loading ---")
    alerts = analyze.load_csv("alerts.csv")
    vehicles = analyze.load_csv("vehicles.csv")
    work_orders = analyze.load_csv("work_orders.csv")

    check("alerts.csv loads", len(alerts) > 2000, f"got {len(alerts)}")
    check("vehicles.csv has 60 vehicles", len(vehicles) == 60)
    check("work_orders.csv loads", len(work_orders) == 9)

    required_cols = {"alert_id", "vehicle_id", "driver_id", "alert_type",
                     "vendor_severity", "event_time_utc", "dtc_code", "value"}
    actual = set(alerts[0].keys())
    check("alerts has required columns", required_cols.issubset(actual),
          f"missing: {required_cols - actual}")

    vids = {v["vehicle_id"] for v in vehicles}
    check("vehicle IDs are V-101..V-160", vids == {f"V-{i}" for i in range(101, 161)})

    return alerts, vehicles, work_orders


def test_deduplication(alerts):
    print("\n--- Deduplication ---")
    unique, dup_count = analyze.deduplicate(alerts)
    check("dedup removes duplicates", dup_count > 0, f"found {dup_count}")
    check("dedup count ~134", 100 <= dup_count <= 200, f"got {dup_count}")
    check("no remaining dups", len(unique) == len(set(
        (r["vehicle_id"], r["alert_type"], r["event_time_utc"], r["value"]) for r in unique)))
    return unique


def test_ota_noise_filter(alerts):
    print("\n--- OTA Noise Filter ---")
    ota_count = sum(1 for a in alerts if analyze.is_ota_noise(a))
    check("OTA filter catches alerts", ota_count > 0, f"filtered {ota_count}")
    check("OTA filter in range 15-35", 15 <= ota_count <= 35, f"got {ota_count}")

    for a in alerts:
        if analyze.is_ota_noise(a):
            t = analyze.parse_utc(a["event_time_utc"])
            check(f"OTA {a['alert_id']} in 03-04 UTC window",
                  t is not None and 3 <= t.hour < 4)
            break


def test_scoring():
    print("\n--- Scoring Engine ---")
    firmware_map = {"V-101": "4.1.8", "V-142": "4.2.1"}
    open_wo_vids = set()

    # P0524 should score highest (100 base)
    row_p0524 = {"alert_type": "engine_fault", "vendor_severity": "HIGH",
                 "dtc_code": "P0524", "vehicle_id": "V-101", "value": "85",
                 "event_time_utc": "2026-09-21T18:32:00+00:00"}
    score, fw = analyze.score_alert(row_p0524, open_wo_vids, firmware_map)
    check("P0524 scores >= 100", score >= 100, f"got {score}")
    check("P0524 classified HIGH", analyze.classify(score) == "HIGH")

    # P0420 should score low (10 base)
    row_p0420 = {"alert_type": "engine_fault", "vendor_severity": "HIGH",
                 "dtc_code": "P0420", "vehicle_id": "V-101", "value": "",
                 "event_time_utc": "2026-09-21T10:00:00+00:00"}
    score420, _ = analyze.score_alert(row_p0420, open_wo_vids, firmware_map)
    check("P0420 scores LOW despite vendor HIGH", analyze.classify(score420) == "LOW",
          f"score={score420}, class={analyze.classify(score420)}")

    # Firmware 4.2.1 harsh braking flags inflated
    row_harsh = {"alert_type": "harsh_braking", "vendor_severity": "HIGH",
                 "dtc_code": "", "vehicle_id": "V-142", "value": "0.68",
                 "event_time_utc": "2026-09-21T12:00:00+00:00"}
    _, fw_inflated = analyze.score_alert(row_harsh, open_wo_vids, firmware_map)
    check("4.2.1 harsh braking flagged as fw_inflated", fw_inflated)

    row_harsh_old = {**row_harsh, "vehicle_id": "V-101"}
    _, fw_old = analyze.score_alert(row_harsh_old, open_wo_vids, firmware_map)
    check("4.1.8 harsh braking NOT flagged", not fw_old)

    # Recency boost: recent alert scores higher
    row_old = {**row_p0524, "event_time_utc": "2026-09-10T10:00:00+00:00"}
    score_old, _ = analyze.score_alert(row_old, open_wo_vids, firmware_map)
    check("Recent alert scores higher than old", score > score_old,
          f"recent={score}, old={score_old}")


def test_work_order_maps():
    print("\n--- Work Order Maps ---")
    work_orders = analyze.load_csv("work_orders.csv")
    open_wos, closed_wos = analyze.build_work_order_maps(work_orders)

    check("Open WOs found", len(open_wos) > 0, f"found {len(open_wos)}")
    check("V-102 has open WO", "V-102" in open_wos)
    check("V-158 has open WO", "V-158" in open_wos)
    check("Closed WOs found", len(closed_wos) > 0)
    check("V-137 has closed WO (WO-5540)", "V-137" in closed_wos)
    check("V-108 has closed WO (WO-5512)", "V-108" in closed_wos)


def test_post_repair_detection():
    print("\n--- Post-Repair Fault Detection ---")
    work_orders = analyze.load_csv("work_orders.csv")
    _, closed_wos = analyze.build_work_order_maps(work_orders)

    # Simulate V-137 P0524 after repair
    fake_scored = [{
        "alert_type": "engine_fault", "dtc_code": "P0524",
        "vehicle_id": "V-137", "already_fixed": False,
        "event_time_utc": "2026-09-21T18:32:00+00:00",
    }]
    faults = analyze.find_post_repair_faults(fake_scored, closed_wos)
    check("V-137 P0524 detected as post-repair fault", len(faults) >= 1)
    if faults:
        check("V-137 fault references WO-5540", faults[0]["wo_id"] == "WO-5540")


def test_ai_validation():
    print("\n--- AI Output Validation ---")
    valid_vids = {f"V-{i}" for i in range(101, 161)}
    blockers = [{"vehicle_id": "V-102"}, {"vehicle_id": "V-137"},
                {"vehicle_id": "V-158"}, {"vehicle_id": "V-108"}]
    stats = {"duplicates": 134}

    # Good output passes
    good = ("Dispatch Briefing for Monday Sep 21. V-137 has a critical oil pressure fault. "
            "V-108 shows overheating after repair. V-158 is in the shop for misfire inspection. "
            "134 duplicate alerts were removed from the dataset.")
    ok, reason = analyze.validate_ai_output(good, valid_vids, blockers, stats)
    check("Good output passes validation", ok, reason)

    # Too short fails
    ok, reason = analyze.validate_ai_output("Hi", valid_vids)
    check("Too-short output rejected", not ok, reason)

    # Hallucinated vehicle fails
    bad_vid = "V-137 is blocked. V-270 also needs inspection. V-108 is critical."
    ok, reason = analyze.validate_ai_output(bad_vid, valid_vids)
    check("Hallucinated V-270 rejected", not ok, reason)

    # Hallucinated driver fails
    bad_driver = "D-17 has the most alerts. D-53 on V-270 also needs attention." * 1
    ok, reason = analyze.validate_ai_output(bad_driver, valid_vids)
    check("Hallucinated D-53 rejected", not ok, reason)

    # Repetition loop
    repeated = ("157 alerts already resolved by work orders.\n" * 15)
    ok, reason = analyze.validate_ai_output(repeated, valid_vids)
    check("Repetition loop rejected", not ok, reason)

    # False "no duplicates" claim
    no_dup_claim = ("Dispatch Briefing for Monday Sep 21. V-137 needs attention. "
                    "No issues with duplicate alerts were found in the data. All clean.")
    ok, reason = analyze.validate_ai_output(no_dup_claim, valid_vids, blockers, stats)
    check("False 'no duplicates' claim rejected", not ok, reason)

    # Non-blocker called blocker
    wrong_blocker = ("V-145 is a blocker and should not be dispatched. Stop V-145 immediately. "
                     "Also check V-137 which has oil pressure issues.")
    ok, reason = analyze.validate_ai_output(wrong_blocker, valid_vids, blockers, stats)
    check("Non-blocker V-145 called blocker rejected", not ok, reason)


def test_classify():
    print("\n--- Classification ---")
    check("score 100 = HIGH", analyze.classify(100) == "HIGH")
    check("score 70 = HIGH", analyze.classify(70) == "HIGH")
    check("score 69 = MEDIUM", analyze.classify(69) == "MEDIUM")
    check("score 30 = MEDIUM", analyze.classify(30) == "MEDIUM")
    check("score 29 = LOW", analyze.classify(29) == "LOW")
    check("score 0 = LOW", analyze.classify(0) == "LOW")


def test_template_briefing():
    print("\n--- Template Briefing (fallback) ---")
    stats = {"scored": 2199, "duplicates": 134, "ota_filtered": 23,
             "vendor_high": 313, "my_high": 42, "already_fixed": 157}
    blockers = [
        {"vehicle_id": "V-137", "reason": "Critical P0524", "source": "engine_fault"},
        {"vehicle_id": "V-102", "reason": "Telematics unit removed", "detail": "Parked", "source": "work_order"},
    ]
    post_repair = [{
        "vehicle_id": "V-137", "dtc_label": "Oil pressure critical",
        "dtc_code": "P0524", "alert_time_et": "Sep 21 14:32 ET",
        "wo_id": "WO-5540", "wo_work": "Replaced coolant temperature sensor",
    }]
    result = analyze.generate_template_briefing(stats, blockers, [], post_repair)
    check("Template mentions V-137", "V-137" in result)
    check("Template mentions P0524", "P0524" in result)
    check("Template mentions duplicates", "134" in result)
    check("Template mentions vendor overcount", "313" in result)
    check("Template mentions firmware", "4.2.1" in result)
    check("Template is non-trivial length", len(result) > 200, f"len={len(result)}")


def test_full_pipeline():
    print("\n--- Full Pipeline Smoke Test ---")
    alerts_raw = analyze.load_csv("alerts.csv")

    backfill_dir = analyze.DATA.parent / "Maple_Freight_New_Information"
    backfill_file = backfill_dir / "hamilton_backfill.csv"
    if backfill_file.exists():
        backfill = analyze.load_csv_path(backfill_file)
        alerts_raw.extend(backfill)
        check("Backfill loaded", len(backfill) == 10)

    vehicles = analyze.load_csv("vehicles.csv")
    work_orders = analyze.load_csv("work_orders.csv")

    vehicle_map = {v["vehicle_id"]: v for v in vehicles}
    firmware_map = {v["vehicle_id"]: v["device_firmware"] for v in vehicles}
    km_map = {v["vehicle_id"]: int(v["km_driven_14d"]) for v in vehicles}
    open_wos, closed_wos = analyze.build_work_order_maps(work_orders)
    open_wo_vids = set(open_wos.keys())

    alerts, dup_count = analyze.deduplicate(alerts_raw)
    check("Pipeline: dedup count", 130 <= dup_count <= 140, f"got {dup_count}")

    scored = []
    ota_filtered = 0
    for row in alerts:
        if analyze.is_ota_noise(row):
            ota_filtered += 1
            continue
        if row["alert_type"] == "maintenance_due":
            try:
                val = float(row["value"])
            except (ValueError, TypeError):
                val = 0
            if val <= 0:
                continue
        score, fw_inflated = analyze.score_alert(row, open_wo_vids, firmware_map)
        vid = row["vehicle_id"]
        alert_dt = analyze.parse_utc(row["event_time_utc"])
        already_fixed = analyze.check_already_fixed(vid, alert_dt, closed_wos)
        v = vehicle_map.get(vid, {})
        scored.append({
            "alert_type": row["alert_type"], "dtc_code": row["dtc_code"],
            "vehicle_id": vid, "already_fixed": already_fixed,
            "event_time_utc": row["event_time_utc"],
            "my_severity": analyze.classify(score), "score": score,
        })

    high = sum(1 for a in scored if a["my_severity"] == "HIGH")
    med = sum(1 for a in scored if a["my_severity"] == "MEDIUM")
    low = sum(1 for a in scored if a["my_severity"] == "LOW")

    check("Pipeline: HIGH count 30-60", 30 <= high <= 60, f"got {high}")
    check("Pipeline: MEDIUM count 600-800", 600 <= med <= 800, f"got {med}")
    check("Pipeline: LOW count 1300-1600", 1300 <= low <= 1600, f"got {low}")
    check("Pipeline: total scored ~2199", 2100 <= len(scored) <= 2300, f"got {len(scored)}")
    check("Pipeline: OTA filtered ~23", 20 <= ota_filtered <= 30, f"got {ota_filtered}")

    # Post-repair faults should find V-137 and V-108
    post_repair = analyze.find_post_repair_faults(scored, closed_wos)
    pr_vids = {f["vehicle_id"] for f in post_repair}
    check("Pipeline: V-137 post-repair detected", "V-137" in pr_vids)
    check("Pipeline: V-108 post-repair detected", "V-108" in pr_vids)


def test_eval_metrics():
    print("\n--- AI Evaluation Metrics ---")
    gt = {
        "blockers": [
            {"vehicle_id": "V-102", "depot": "Airport Hub", "reason": "Telematics removed"},
            {"vehicle_id": "V-158", "depot": "North Yard", "reason": "Misfire inspection"},
            {"vehicle_id": "V-137", "depot": "North Yard", "reason": "Oil pressure critical"},
            {"vehicle_id": "V-108", "depot": "Hamilton Depot", "reason": "Engine overheating"},
        ],
        "post_repair": [
            {"vehicle_id": "V-137", "dtc_code": "P0524", "wo_id": "WO-5540"},
            {"vehicle_id": "V-108", "dtc_code": "P0217", "wo_id": "WO-5512"},
        ],
        "top10": [{"vehicle_id": "V-151", "driver_id": "D-11", "depot": "North Yard"}],
        "stats": {"total_raw": 2380, "duplicates": 134, "scored": 2199,
                  "my_high": 42, "blockers": 4},
        "valid_vehicles": {f"V-{i}" for i in range(101, 161)},
        "valid_drivers": {f"D-{i:02d}" for i in range(1, 61)},
        "all_wo_ids": {"WO-5512", "WO-5519", "WO-5527", "WO-5531", "WO-5534",
                       "WO-5536", "WO-5540", "WO-5541", "WO-5543"},
    }

    # Perfect briefing should score high
    perfect = (
        "Dispatch Briefing Monday Sep 21. 4 vehicles blocked from dispatch. "
        "V-137 (North Yard): Oil pressure critical P0524 appeared AFTER work order WO-5540 repair. "
        "V-108 (Hamilton Depot): Engine overheating P0217 appeared AFTER work order WO-5512 closed. "
        "V-102 (Airport Hub): telematics removed, open work order. "
        "V-158 (North Yard): misfire inspection pending. "
        "134 duplicate alerts removed from 2380 raw alerts. 2199 scored, 42 HIGH. "
        "Vendor over-reported HIGH severity. Re-ranked using DTC reference."
    )
    r = evaluate_briefing(perfect, gt)
    check("Perfect briefing >= A grade", r["grade"] == "A", f"got {r['grade']} ({r['overall_score']:.0%})")
    check("Perfect: grounded", r["metrics"]["groundedness"]["score"] >= 0.8)
    check("Perfect: correct", r["metrics"]["correctness"]["score"] >= 0.8)
    check("Perfect: good coverage", r["metrics"]["coverage"]["score"] >= 0.8)

    # Hallucinated briefing should score low on hallucination
    hallucinated = (
        "V-270 is the worst vehicle. D-53 drives V-270 at Hamilton. "
        "WO-9999 was opened for V-270. P9999 fault code detected. "
        "V-145 must not dispatch, it is a blocker."
    )
    r2 = evaluate_briefing(hallucinated, gt)
    check("Hallucinated: low groundedness", r2["metrics"]["groundedness"]["score"] <= 0.3,
          f"got {r2['metrics']['groundedness']['score']:.0%}")
    check("Hallucinated: has critical issues",
          any(i["severity"] == "critical" for i in r2["issues"]))
    check("Hallucinated: overall grade D or F", r2["grade"] in ("D", "F"),
          f"got {r2['grade']}")

    # Incomplete briefing should lose completeness points
    incomplete = "Status: Monday Sep 21. V-137 has oil pressure issues. 134 duplicates. Vendor over-reports."
    r3 = evaluate_briefing(incomplete, gt)
    check("Incomplete: coverage < 70%", r3["metrics"]["coverage"]["score"] < 0.7,
          f"got {r3['metrics']['coverage']['score']:.0%}")

    # Wrong numbers should fail numerical accuracy
    wrong_nums = (
        "V-137 is critical. V-108 needs attention. V-102 in shop. V-158 pending. "
        "There are 5000 raw alerts total with 800 duplicates and 100 HIGH ranked items. "
        "2199 scored alerts."
    )
    r4 = evaluate_briefing(wrong_nums, gt)
    check("Wrong numbers: correctness < 60%",
          r4["metrics"]["correctness"]["score"] < 0.6,
          f"got {r4['metrics']['correctness']['score']:.0%}")

    # Empty should get F
    r5 = evaluate_briefing("", gt)
    check("Empty briefing: grade F", r5["grade"] == "F")

    # Template briefing (the actual fallback)
    stats = {"scored": 2199, "duplicates": 134, "ota_filtered": 23,
             "vendor_high": 313, "my_high": 42, "already_fixed": 157}
    blockers = gt["blockers"][:2]
    blockers[0]["source"] = "work_order"
    blockers[0]["detail"] = "test"
    blockers[1]["source"] = "work_order"
    blockers[1]["detail"] = "test"
    post_repair = [{"vehicle_id": "V-137", "dtc_label": "Oil pressure critical",
                    "dtc_code": "P0524", "alert_time_et": "Sep 21 14:32 ET",
                    "wo_id": "WO-5540", "wo_work": "Replaced coolant temperature sensor"}]
    template = analyze.generate_template_briefing(stats, blockers, [], post_repair)
    r6 = evaluate_briefing(template, gt)
    check("Template briefing >= B grade", r6["grade"] in ("A", "B"),
          f"got {r6['grade']} ({r6['overall_score']:.0%})")


def test_html_output():
    print("\n--- HTML Output ---")
    report_path = analyze.Path(__file__).parent / "report.html"
    if not report_path.exists():
        check("report.html exists", False, "file not found")
        return

    html = report_path.read_text(encoding="utf-8")
    check("HTML has doctype", html.strip().startswith("<!DOCTYPE"))
    check("HTML has title", "Dispatch Decision Board" in html)
    check("HTML has blockers section", "Dispatch Blockers" in html)
    check("HTML has V-137 blocker", "V-137" in html)
    check("HTML has V-108 blocker", "V-108" in html)
    check("HTML has V-102 blocker", "V-102" in html)
    check("HTML has V-158 blocker", "V-158" in html)
    check("HTML has AI briefing section", "AI Morning Briefing" in html)
    check("HTML has confidence badge", "confidence:" in html)
    check("HTML has disclaimer", "trust the tables" in html)
    check("HTML has print button", "Print / PDF" in html)
    check("HTML has eval scorecard", "AI Briefing Quality" in html)
    check("HTML has eval metrics", "Groundedness" in html and "Coverage" in html)
    check("HTML has tabs", "switchTab" in html)
    check("HTML < 500KB", len(html) < 500_000, f"size={len(html)}")


if __name__ == "__main__":
    print("=" * 60)
    print("Maple Freight Alert Priority Engine — Test Suite")
    print("=" * 60)

    alerts, vehicles, work_orders = test_data_loading()
    unique = test_deduplication(alerts)
    test_ota_noise_filter(unique)
    test_scoring()
    test_classify()
    test_work_order_maps()
    test_post_repair_detection()
    test_ai_validation()
    test_template_briefing()
    test_full_pipeline()
    test_eval_metrics()
    test_html_output()

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL} tests")
    print("=" * 60)
    sys.exit(1 if FAIL > 0 else 0)
