"""
pytest suite for analyze.py core logic.
Run:  pytest tests/
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import analyze as a

EDT = datetime.timezone(datetime.timedelta(hours=-4))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_alert(**kwargs):
    base = {
        "alert_id": "A-000001",
        "vehicle_id": "V-101",
        "driver_id": "D-01",
        "alert_type": "speeding",
        "vendor_severity": "MEDIUM",
        "event_time_utc": "2026-09-21T14:00:00Z",
        "received_time_utc": "2026-09-21T14:01:00Z",
        "value": "90",
        "unit": "km/h",
        "posted_limit_kmh": "80",
        "dtc_code": "",
    }
    base.update(kwargs)
    return base


def make_wo(vehicle_id, status, closed_local=None, work_done="routine"):
    return {
        "wo_id": "WO-0001",
        "vehicle_id": vehicle_id,
        "status": status,
        "opened_local": "2026-09-21T07:00:00",
        "closed_local": closed_local or "",
        "work_done": work_done,
        "technician_note": "",
    }


# ---------------------------------------------------------------------------
# parse_utc / to_eastern
# ---------------------------------------------------------------------------

def test_parse_utc_valid():
    dt = a.parse_utc("2026-09-21T18:32:00Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 9 and dt.day == 21

def test_parse_utc_none():
    assert a.parse_utc("") is None
    assert a.parse_utc(None) is None

def test_to_eastern_converts():
    dt = a.parse_utc("2026-09-21T18:32:00Z")   # UTC 18:32 = ET 14:32
    et = a.to_eastern(dt)
    assert et.hour == 14 and et.minute == 32


# ---------------------------------------------------------------------------
# is_ota_noise
# ---------------------------------------------------------------------------

def test_ota_noise_device_unplugged_in_window():
    row = make_alert(alert_type="device_unplugged", event_time_utc="2026-09-21T03:30:00Z")
    assert a.is_ota_noise(row) is True

def test_ota_noise_camera_offline_in_window():
    row = make_alert(alert_type="camera_offline", event_time_utc="2026-09-21T03:00:00Z")
    assert a.is_ota_noise(row) is True

def test_ota_noise_outside_window():
    row = make_alert(alert_type="device_unplugged", event_time_utc="2026-09-21T18:44:00Z")
    assert a.is_ota_noise(row) is False

def test_ota_noise_wrong_type():
    # speeding in the OTA window is NOT noise
    row = make_alert(alert_type="speeding", event_time_utc="2026-09-21T03:30:00Z")
    assert a.is_ota_noise(row) is False


# ---------------------------------------------------------------------------
# deduplicate
# ---------------------------------------------------------------------------

def test_deduplicate_removes_exact_duplicate():
    row = make_alert()
    unique, dup = a.deduplicate([row, row.copy()])
    assert len(unique) == 1
    assert dup == 1

def test_deduplicate_keeps_different_vehicle():
    r1 = make_alert(vehicle_id="V-101")
    r2 = make_alert(vehicle_id="V-102")
    unique, dup = a.deduplicate([r1, r2])
    assert len(unique) == 2 and dup == 0

def test_deduplicate_different_alert_id_same_event():
    r1 = make_alert(alert_id="A-000001")
    r2 = make_alert(alert_id="A-000002")   # different ID, same (vehicle, type, time, value)
    unique, dup = a.deduplicate([r1, r2])
    assert len(unique) == 1 and dup == 1


# ---------------------------------------------------------------------------
# build_work_order_maps
# ---------------------------------------------------------------------------

def test_open_wo_detected():
    wo = make_wo("V-102", "Open")
    open_wos, closed_wos = a.build_work_order_maps([wo])
    assert "V-102" in open_wos
    assert "V-102" not in closed_wos

def test_closed_wo_detected():
    wo = make_wo("V-137", "Closed", closed_local="2026-09-21T07:40:00")
    open_wos, closed_wos = a.build_work_order_maps([wo])
    assert "V-137" not in open_wos
    assert "V-137" in closed_wos

def test_most_recent_closed_wo_wins():
    wo1 = make_wo("V-137", "Closed", closed_local="2026-09-20T08:00:00")
    wo2 = make_wo("V-137", "Closed", closed_local="2026-09-21T07:40:00")
    wo2["wo_id"] = "WO-0002"
    _, closed_wos = a.build_work_order_maps([wo1, wo2])
    assert closed_wos["V-137"]["wo"]["wo_id"] == "WO-0002"


# ---------------------------------------------------------------------------
# score_alert — DTC overrides vendor severity
# ---------------------------------------------------------------------------

def test_p0524_scores_100():
    row = make_alert(alert_type="engine_fault", vendor_severity="MEDIUM", dtc_code="P0524",
                     event_time_utc="2026-09-21T19:41:00Z")
    score, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    assert score >= 100   # base 100 * recency

def test_p0217_scores_high():
    row = make_alert(alert_type="engine_fault", vendor_severity="MEDIUM", dtc_code="P0217",
                     event_time_utc="2026-09-21T16:05:00Z")
    score, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    assert score >= 90

def test_p0420_scores_low():
    row = make_alert(alert_type="engine_fault", vendor_severity="HIGH", dtc_code="P0420",
                     event_time_utc="2026-09-21T14:00:00Z")
    score, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    assert score <= 15   # reference low urgency

def test_open_wo_reduces_non_engine_score():
    row = make_alert(alert_type="speeding", vendor_severity="HIGH",
                     event_time_utc="2026-09-21T14:00:00Z")
    score_no_wo, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    score_with_wo, _ = a.score_alert(row, open_wo_vids={"V-101"}, firmware_map={})
    assert score_with_wo < score_no_wo

def test_open_wo_does_not_reduce_engine_fault():
    row = make_alert(alert_type="engine_fault", vendor_severity="MEDIUM", dtc_code="P0524",
                     event_time_utc="2026-09-21T19:41:00Z")
    score_no_wo, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    score_with_wo, _ = a.score_alert(row, open_wo_vids={"V-101"}, firmware_map={})
    assert score_no_wo == score_with_wo

def test_firmware_421_flagged():
    row = make_alert(alert_type="harsh_braking", vendor_severity="HIGH",
                     event_time_utc="2026-09-21T14:00:00Z")
    _, fw_inflated = a.score_alert(row, open_wo_vids=set(), firmware_map={"V-101": "4.2.1"})
    assert fw_inflated is True

def test_firmware_other_not_flagged():
    row = make_alert(alert_type="harsh_braking", vendor_severity="HIGH",
                     event_time_utc="2026-09-21T14:00:00Z")
    _, fw_inflated = a.score_alert(row, open_wo_vids=set(), firmware_map={"V-101": "4.1.8"})
    assert fw_inflated is False


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_classify_high():
    assert a.classify(70) == "HIGH"
    assert a.classify(150) == "HIGH"

def test_classify_medium():
    assert a.classify(30) == "MEDIUM"
    assert a.classify(69) == "MEDIUM"

def test_classify_low():
    assert a.classify(0) == "LOW"
    assert a.classify(29) == "LOW"


# ---------------------------------------------------------------------------
# check_already_fixed
# ---------------------------------------------------------------------------

def test_already_fixed_alert_before_wo_close():
    wo = make_wo("V-137", "Closed", closed_local="2026-09-21T07:40:00")
    _, closed_wos = a.build_work_order_maps([wo])
    # Alert at 06:00 ET = before 07:40 close → already fixed
    alert_dt = datetime.datetime(2026, 9, 21, 6, 0, tzinfo=EDT)
    assert a.check_already_fixed("V-137", alert_dt, closed_wos) is True

def test_not_fixed_alert_after_wo_close():
    wo = make_wo("V-137", "Closed", closed_local="2026-09-21T07:40:00")
    _, closed_wos = a.build_work_order_maps([wo])
    # Alert at 14:32 ET = after 07:40 close → NOT already fixed
    alert_dt = datetime.datetime(2026, 9, 21, 14, 32, tzinfo=EDT)
    assert a.check_already_fixed("V-137", alert_dt, closed_wos) is False

def test_not_fixed_no_closed_wo():
    assert a.check_already_fixed("V-999", datetime.datetime.now(tz=EDT), {}) is False


# ---------------------------------------------------------------------------
# find_post_repair_faults
# ---------------------------------------------------------------------------

def test_post_repair_fault_detected():
    wo = make_wo("V-108", "Closed", closed_local="2026-09-09T10:00:00", work_done="P0171 gasket")
    _, closed_wos = a.build_work_order_maps([wo])
    alert = {
        "vehicle_id": "V-108",
        "alert_type": "engine_fault",
        "dtc_code": "P0524",
        "event_time_utc": "2026-09-21T19:41:00Z",
        "already_fixed": False,
    }
    faults = a.find_post_repair_faults([alert], closed_wos)
    assert len(faults) == 1
    assert faults[0]["dtc_code"] == "P0524"

def test_post_repair_fault_not_detected_when_already_fixed():
    wo = make_wo("V-137", "Closed", closed_local="2026-09-21T15:00:00")
    _, closed_wos = a.build_work_order_maps([wo])
    alert = {
        "vehicle_id": "V-137",
        "alert_type": "engine_fault",
        "dtc_code": "P0524",
        "event_time_utc": "2026-09-21T14:32:00Z",
        "already_fixed": True,   # marked already fixed
    }
    faults = a.find_post_repair_faults([alert], closed_wos)
    assert faults == []

def test_post_repair_fault_only_for_critical_dtcs():
    wo = make_wo("V-101", "Closed", closed_local="2026-09-20T10:00:00")
    _, closed_wos = a.build_work_order_maps([wo])
    alert = {
        "vehicle_id": "V-101",
        "alert_type": "engine_fault",
        "dtc_code": "P0171",   # medium urgency — not in critical set
        "event_time_utc": "2026-09-21T14:00:00Z",
        "already_fixed": False,
    }
    faults = a.find_post_repair_faults([alert], closed_wos)
    assert faults == []


# ---------------------------------------------------------------------------
# validate_ai_output
# ---------------------------------------------------------------------------

VALID_VIDS = {f"V-{i}" for i in range(101, 161)}

def test_validate_rejects_short_text():
    ok, reason = a.validate_ai_output("Too short.", VALID_VIDS)
    assert not ok

def test_validate_rejects_hallucinated_vehicle():
    text = "V-270 has a critical fault and must not be dispatched tonight." * 3
    ok, reason = a.validate_ai_output(text, VALID_VIDS)
    assert not ok
    assert "hallucinated" in reason

def test_validate_rejects_repetition_loop():
    line = "157 alerts already resolved.\n"
    text = line * 10
    ok, reason = a.validate_ai_output(text, VALID_VIDS)
    assert not ok

def test_validate_accepts_clean_text():
    text = (
        "Dispatch briefing for Monday Sep 21. "
        "V-108 has two critical faults and must not be dispatched. "
        "V-137 has an oil pressure fault. "
        "V-158 has a misfire inspection booked for tomorrow morning. "
        "134 duplicate alerts were removed from the dataset."
    )
    ok, reason = a.validate_ai_output(text, VALID_VIDS)
    assert ok, reason

def test_validate_rejects_wrong_duplicate_pct():
    # actual duplicate rate is ~6% (134/2380); claiming 50% should fail (>10 pt gap)
    text = ("Fleet status: 50% duplicate alerts found in the export. " * 5 +
            "V-108 and V-137 need attention tonight. Check oil levels.")
    stats = {"total_raw": 2380, "duplicates": 134}
    ok, reason = a.validate_ai_output(text, VALID_VIDS, stats=stats)
    assert not ok


# ---------------------------------------------------------------------------
# strip_markdown
# ---------------------------------------------------------------------------

def test_strip_markdown_removes_headers():
    assert "##" not in a.strip_markdown("## Section\nBody text here.")

def test_strip_markdown_removes_bold():
    result = a.strip_markdown("**important** thing")
    assert "**" not in result
    assert "important" in result

def test_strip_markdown_collapses_blank_lines():
    result = a.strip_markdown("line1\n\n\n\nline2")
    assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# maintenance_due scoring (negative = not due)
# ---------------------------------------------------------------------------

def test_maintenance_negative_scores_low():
    # base = 5 for negative km; recency 1.5x applies for recent alerts (within 48 h of snapshot)
    row = make_alert(alert_type="maintenance_due", vendor_severity="HIGH", value="-500",
                     event_time_utc="2026-09-21T14:00:00Z")
    score, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    assert score == 7.5   # 5 * 1.5 recency multiplier (alert is ~8 h old)

def test_maintenance_positive_scores_higher():
    row = make_alert(alert_type="maintenance_due", vendor_severity="HIGH", value="2350",
                     event_time_utc="2026-09-21T14:00:00Z")
    score, _ = a.score_alert(row, open_wo_vids=set(), firmware_map={})
    assert score > 30
