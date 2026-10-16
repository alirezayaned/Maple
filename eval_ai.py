"""
AI Briefing Evaluation — scores an AI-generated briefing against ground truth
from the deterministic scoring engine.

Metrics:
  - Faithfulness:   Do all factual claims match the ground truth data?
  - Completeness:   Does the briefing mention all critical items?
  - Hallucination:  Does it invent entities or facts not in the data?
  - Numerical accuracy: Are numbers within tolerance of actual values?
  - Relevance:      Does it prioritise the right things?

Usage:
  from eval_ai import evaluate_briefing
  report = evaluate_briefing(text, ground_truth)
  # report["overall_score"]  -> 0.0 to 1.0
  # report["metrics"]        -> per-dimension breakdown
  # report["issues"]         -> list of specific problems found
"""

import re


def evaluate_briefing(text, ground_truth):
    """
    Score an AI briefing against ground truth.

    ground_truth dict should contain:
      blockers:       list of {"vehicle_id", "depot", "reason"}
      post_repair:    list of {"vehicle_id", "dtc_code", "wo_id"}
      top10:          list of {"vehicle_id", "driver_id", "depot"}
      stats:          {"total_raw", "duplicates", "scored", "my_high", ...}
      driver_top5:    list of (driver_id, {"vehicle_id", "firmware", ...})
      valid_vehicles: set of valid vehicle IDs
      valid_drivers:  set of valid driver IDs
    """
    if not text or len(text.strip()) < 20:
        return _empty_report("Briefing is empty or too short")

    metrics = {}
    issues = []

    metrics["faithfulness"] = _score_faithfulness(text, ground_truth, issues)
    metrics["completeness"] = _score_completeness(text, ground_truth, issues)
    metrics["hallucination"] = _score_hallucination(text, ground_truth, issues)
    metrics["numerical_accuracy"] = _score_numerical(text, ground_truth, issues)
    metrics["relevance"] = _score_relevance(text, ground_truth, issues)

    weights = {
        "faithfulness": 0.30,
        "completeness": 0.20,
        "hallucination": 0.25,
        "numerical_accuracy": 0.15,
        "relevance": 0.10,
    }
    overall = sum(metrics[k]["score"] * weights[k] for k in weights)

    return {
        "overall_score": round(overall, 3),
        "grade": _grade(overall),
        "metrics": metrics,
        "issues": issues,
        "weights": weights,
    }


def _empty_report(reason):
    empty_metric = {"score": 0.0, "detail": reason, "checks": []}
    return {
        "overall_score": 0.0,
        "grade": "F",
        "metrics": {k: empty_metric for k in
                    ["faithfulness", "completeness", "hallucination", "numerical_accuracy", "relevance"]},
        "issues": [{"severity": "critical", "message": reason}],
        "weights": {},
    }


def _grade(score):
    if score >= 0.9: return "A"
    if score >= 0.8: return "B"
    if score >= 0.65: return "C"
    if score >= 0.5: return "D"
    return "F"


# ---------------------------------------------------------------------------
#  Faithfulness — do factual claims match ground truth?
# ---------------------------------------------------------------------------

def _score_faithfulness(text, gt, issues):
    checks = []
    text_lower = text.lower()

    # Check blocker vehicle-depot associations
    for b in gt.get("blockers", []):
        vid = b["vehicle_id"]
        depot = b.get("depot", "")
        if vid.lower() in text_lower and depot:
            vid_idx = text_lower.find(vid.lower())
            context = text_lower[max(0, vid_idx - 150):vid_idx + 150]
            if depot.lower() in context:
                checks.append(("pass", f"{vid} correctly associated with {depot}"))
            else:
                other_depots = [d for d in ["north yard", "airport hub", "hamilton depot"]
                                if d in context and d != depot.lower()]
                if other_depots:
                    checks.append(("fail", f"{vid} associated with wrong depot: {other_depots[0]} (actual: {depot})"))
                    issues.append({"severity": "high", "metric": "faithfulness",
                                   "message": f"{vid} placed at wrong depot"})
                else:
                    checks.append(("skip", f"{vid} mentioned but depot not specified nearby"))

    # Check post-repair fault claims
    for pr in gt.get("post_repair", []):
        vid = pr["vehicle_id"]
        wo = pr.get("wo_id", "")
        if vid.lower() in text_lower:
            vid_idx = text_lower.find(vid.lower())
            context = text_lower[max(0, vid_idx - 200):vid_idx + 200]
            if "after" in context and ("repair" in context or "work order" in context or wo.lower() in context):
                checks.append(("pass", f"{vid} correctly identified as post-repair fault"))
            elif "repair" in context or wo.lower() in context:
                checks.append(("partial", f"{vid} mentions repair but not the temporal relationship"))

    passed = sum(1 for s, _ in checks if s == "pass")
    partial = sum(1 for s, _ in checks if s == "partial")
    failed = sum(1 for s, _ in checks if s == "fail")
    total = passed + partial + failed
    score = (passed + 0.5 * partial) / max(total, 1)

    return {"score": round(score, 3), "detail": f"{passed} correct, {partial} partial, {failed} wrong",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Completeness — does it mention all critical items?
# ---------------------------------------------------------------------------

def _score_completeness(text, gt, issues):
    checks = []
    text_lower = text.lower()

    # Must mention all dispatch blockers
    for b in gt.get("blockers", []):
        vid = b["vehicle_id"]
        if vid.lower() in text_lower:
            checks.append(("pass", f"Mentions blocker {vid}"))
        else:
            checks.append(("fail", f"Missing blocker {vid}"))
            issues.append({"severity": "high", "metric": "completeness",
                           "message": f"Dispatch blocker {vid} not mentioned"})

    # Must mention post-repair faults
    for pr in gt.get("post_repair", []):
        vid = pr["vehicle_id"]
        dtc = pr.get("dtc_code", "")
        if vid.lower() in text_lower and dtc.lower() in text_lower:
            checks.append(("pass", f"Mentions post-repair {vid}/{dtc}"))
        elif vid.lower() in text_lower:
            checks.append(("partial", f"Mentions {vid} but not {dtc}"))
        else:
            checks.append(("fail", f"Missing post-repair fault {vid}/{dtc}"))
            issues.append({"severity": "medium", "metric": "completeness",
                           "message": f"Post-repair fault {vid}/{dtc} not mentioned"})

    # Should mention data quality (duplicates, vendor overcount)
    if "duplicate" in text_lower:
        checks.append(("pass", "Mentions duplicate alerts"))
    else:
        checks.append(("fail", "No mention of duplicate alerts"))

    if "vendor" in text_lower or "over-report" in text_lower or "re-rank" in text_lower:
        checks.append(("pass", "Mentions vendor severity issues"))
    else:
        checks.append(("partial", "No mention of vendor severity discrepancy"))

    passed = sum(1 for s, _ in checks if s == "pass")
    partial = sum(1 for s, _ in checks if s == "partial")
    failed = sum(1 for s, _ in checks if s == "fail")
    total = passed + partial + failed
    score = (passed + 0.5 * partial) / max(total, 1)

    return {"score": round(score, 3), "detail": f"{passed}/{total} items covered, {failed} missing",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Hallucination — does it invent things?
# ---------------------------------------------------------------------------

def _score_hallucination(text, gt, issues):
    checks = []

    valid_vids = gt.get("valid_vehicles", set())
    valid_dids = gt.get("valid_drivers", set())
    blocker_vids = {b["vehicle_id"] for b in gt.get("blockers", [])}

    # Check vehicle IDs
    mentioned_vids = set(re.findall(r"V-\d+", text))
    hallucinated_vids = mentioned_vids - valid_vids
    if hallucinated_vids:
        checks.append(("fail", f"Hallucinated vehicles: {hallucinated_vids}"))
        issues.append({"severity": "critical", "metric": "hallucination",
                       "message": f"Invented vehicle IDs: {hallucinated_vids}"})
    else:
        checks.append(("pass", f"All {len(mentioned_vids)} vehicle IDs are valid"))

    # Check driver IDs
    mentioned_dids = set(re.findall(r"D-\d+", text))
    hallucinated_dids = mentioned_dids - valid_dids
    if hallucinated_dids:
        checks.append(("fail", f"Hallucinated drivers: {hallucinated_dids}"))
        issues.append({"severity": "critical", "metric": "hallucination",
                       "message": f"Invented driver IDs: {hallucinated_dids}"})
    else:
        checks.append(("pass", f"All {len(mentioned_dids)} driver IDs are valid"))

    # Check work order IDs
    mentioned_wos = set(re.findall(r"WO-\d+", text))
    valid_wos = {pr.get("wo_id", "") for pr in gt.get("post_repair", [])}
    valid_wos |= {b.get("wo_id", "") for b in gt.get("blockers", []) if b.get("wo_id")}
    all_wo_ids = gt.get("all_wo_ids", valid_wos)
    hallucinated_wos = mentioned_wos - all_wo_ids
    if hallucinated_wos:
        checks.append(("fail", f"Hallucinated work orders: {hallucinated_wos}"))
        issues.append({"severity": "high", "metric": "hallucination",
                       "message": f"Invented work order IDs: {hallucinated_wos}"})
    elif mentioned_wos:
        checks.append(("pass", f"All {len(mentioned_wos)} work order IDs are valid"))

    # Check DTC codes
    mentioned_dtcs = set(re.findall(r"P\d{4}", text))
    valid_dtcs = {"P0524", "P0217", "P0300", "P0171", "P0128", "P0420"}
    hallucinated_dtcs = mentioned_dtcs - valid_dtcs
    if hallucinated_dtcs:
        checks.append(("fail", f"Hallucinated DTC codes: {hallucinated_dtcs}"))
        issues.append({"severity": "high", "metric": "hallucination",
                       "message": f"Invented DTC codes: {hallucinated_dtcs}"})
    elif mentioned_dtcs:
        checks.append(("pass", f"All {len(mentioned_dtcs)} DTC codes are valid"))

    # Check non-blockers called blockers
    text_lower = text.lower()
    for vid in mentioned_vids:
        if vid not in blocker_vids:
            idx = text_lower.find(vid.lower())
            if idx >= 0:
                context = text_lower[max(0, idx - 100):idx + 100]
                if any(w in context for w in ["do not dispatch", "blocker", "block", "must not dispatch",
                                               "stop vehicle", "grounded"]):
                    checks.append(("fail", f"{vid} falsely called a dispatch blocker"))
                    issues.append({"severity": "critical", "metric": "hallucination",
                                   "message": f"{vid} is not a blocker but AI says it is"})

    # Score: 1.0 = no hallucinations, deduct per failure
    failed = sum(1 for s, _ in checks if s == "fail")
    total = len(checks)
    score = max(0, (total - failed * 2)) / max(total, 1)
    score = min(score, 1.0)

    return {"score": round(score, 3), "detail": f"{failed} hallucinations found in {total} checks",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Numerical accuracy — are numbers close to actual?
# ---------------------------------------------------------------------------

def _score_numerical(text, gt, issues):
    checks = []
    stats = gt.get("stats", {})

    number_claims = {
        "total alerts": (r"(\d[\d,]*)\s*(?:raw\s+)?alerts?", "total_raw"),
        "duplicates": (r"(\d[\d,]*)\s*duplicate", "duplicates"),
        "scored/actionable": (r"(\d[\d,]*)\s*(?:actionable|scored|cleaned)", "scored"),
        "HIGH count": (r"(\d+)\s*(?:HIGH|high[\s-]priority|high[\s-]severity)", "my_high"),
        "blocker count": (r"(\d+)\s*(?:vehicles?\s+)?(?:blocked|blocker)", "blockers"),
    }

    for label, (pattern, key) in number_claims.items():
        actual = stats.get(key)
        if actual is None:
            continue
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            claimed = int(match.group(1).replace(",", ""))
            tolerance = max(actual * 0.15, 5)
            if abs(claimed - actual) <= tolerance:
                checks.append(("pass", f"{label}: claimed {claimed}, actual {actual}"))
            else:
                pct_off = abs(claimed - actual) / max(actual, 1) * 100
                checks.append(("fail", f"{label}: claimed {claimed}, actual {actual} ({pct_off:.0f}% off)"))
                issues.append({"severity": "medium", "metric": "numerical_accuracy",
                               "message": f"{label}: AI says {claimed}, actual is {actual}"})

    # Check duplicate percentage claims
    pct_match = re.search(r"(\d+)%\s*[Dd]uplicate", text)
    if pct_match and stats.get("total_raw"):
        claimed_pct = int(pct_match.group(1))
        actual_pct = round(stats["duplicates"] / stats["total_raw"] * 100)
        if abs(claimed_pct - actual_pct) <= 5:
            checks.append(("pass", f"Duplicate %: claimed {claimed_pct}%, actual ~{actual_pct}%"))
        else:
            checks.append(("fail", f"Duplicate %: claimed {claimed_pct}%, actual ~{actual_pct}%"))
            issues.append({"severity": "medium", "metric": "numerical_accuracy",
                           "message": f"Duplicate percentage: AI says {claimed_pct}%, actual is ~{actual_pct}%"})

    passed = sum(1 for s, _ in checks if s == "pass")
    failed = sum(1 for s, _ in checks if s == "fail")
    total = passed + failed
    score = passed / max(total, 1)

    return {"score": round(score, 3), "detail": f"{passed}/{total} numbers accurate",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Relevance — does it prioritise the right things?
# ---------------------------------------------------------------------------

def _score_relevance(text, gt, issues):
    checks = []
    text_lower = text.lower()

    blockers = gt.get("blockers", [])
    top10 = gt.get("top10", [])

    # Blockers should appear before or more prominently than non-blocker action items
    if blockers:
        first_blocker_pos = len(text)
        for b in blockers:
            idx = text_lower.find(b["vehicle_id"].lower())
            if idx >= 0:
                first_blocker_pos = min(first_blocker_pos, idx)

        non_blocker_before = 0
        blocker_vids = {b["vehicle_id"].lower() for b in blockers}
        for t in top10[:3]:
            vid = t["vehicle_id"].lower()
            if vid not in blocker_vids:
                idx = text_lower.find(vid)
                if 0 <= idx < first_blocker_pos:
                    non_blocker_before += 1

        if non_blocker_before == 0:
            checks.append(("pass", "Blockers mentioned before lower-priority items"))
        else:
            checks.append(("fail", f"{non_blocker_before} non-blocker items appear before blockers"))
            issues.append({"severity": "medium", "metric": "relevance",
                           "message": "Lower-priority items mentioned before dispatch blockers"})

    # Post-repair faults should be emphasized (these are the most surprising findings)
    post_repair = gt.get("post_repair", [])
    if post_repair:
        pr_mentioned = sum(1 for pr in post_repair if pr["vehicle_id"].lower() in text_lower)
        if pr_mentioned == len(post_repair):
            checks.append(("pass", f"All {len(post_repair)} post-repair faults mentioned"))
        elif pr_mentioned > 0:
            checks.append(("partial", f"{pr_mentioned}/{len(post_repair)} post-repair faults mentioned"))
        else:
            checks.append(("fail", "No post-repair faults mentioned"))
            issues.append({"severity": "high", "metric": "relevance",
                           "message": "Post-repair faults are the most critical finding but AI ignored them"})

    # Should mention firmware context for driver discussion
    if "d-17" in text_lower or "driver" in text_lower:
        if "firmware" in text_lower or "4.2.1" in text_lower or "fw" in text_lower:
            checks.append(("pass", "Driver discussion includes firmware context"))
        else:
            checks.append(("partial", "Discusses drivers without firmware context"))

    passed = sum(1 for s, _ in checks if s == "pass")
    partial = sum(1 for s, _ in checks if s == "partial")
    failed = sum(1 for s, _ in checks if s == "fail")
    total = passed + partial + failed
    score = (passed + 0.5 * partial) / max(total, 1)

    return {"score": round(score, 3), "detail": f"{passed} good, {partial} partial, {failed} poor priorities",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Formatting helpers
# ---------------------------------------------------------------------------

def format_eval_text(report):
    """One-line summary for console output."""
    grade = report["grade"]
    score = report["overall_score"]
    n_issues = len(report["issues"])
    critical = sum(1 for i in report["issues"] if i["severity"] == "critical")
    return f"Grade: {grade} ({score:.0%}) | {n_issues} issues ({critical} critical)"


def format_eval_html(report):
    """HTML block for the dispatch report showing eval results."""
    grade = report["grade"]
    score = report["overall_score"]
    grade_colors = {"A": "#16a34a", "B": "#65a30d", "C": "#d97706", "D": "#ea580c", "F": "#dc2626"}
    color = grade_colors.get(grade, "#6b7280")

    rows = ""
    for name, m in report["metrics"].items():
        bar_pct = int(m["score"] * 100)
        bar_color = "#16a34a" if m["score"] >= 0.8 else ("#d97706" if m["score"] >= 0.5 else "#dc2626")
        label = name.replace("_", " ").title()
        weight = report.get("weights", {}).get(name, 0)
        rows += f"""<tr>
          <td style="font-weight:600;font-size:13px">{label}</td>
          <td style="width:50%">
            <div style="background:#f3f4f6;border-radius:4px;height:18px;position:relative">
              <div style="background:{bar_color};border-radius:4px;height:100%;width:{bar_pct}%;min-width:2px"></div>
              <span style="position:absolute;right:6px;top:1px;font-size:11px;color:#374151">{m['score']:.0%}</span>
            </div>
          </td>
          <td style="font-size:12px;color:#6b7280">{m['detail']}</td>
          <td style="font-size:11px;color:#9ca3af;text-align:center">{weight:.0%}</td>
        </tr>\n"""

    issue_html = ""
    if report["issues"]:
        issue_items = ""
        sev_icon = {"critical": "X", "high": "!", "medium": "~"}
        sev_color = {"critical": "#dc2626", "high": "#ea580c", "medium": "#d97706"}
        for iss in report["issues"][:10]:
            ic = sev_icon.get(iss["severity"], "?")
            sc = sev_color.get(iss["severity"], "#6b7280")
            issue_items += (f'<div style="padding:4px 0;font-size:12px">'
                           f'<span style="color:{sc};font-weight:700;margin-right:6px">[{ic}]</span>'
                           f'{iss["message"]}</div>\n')
        issue_html = f"""<div style="margin-top:12px;padding:10px 16px;background:#fef2f2;
                         border-radius:6px;border:1px solid #fecaca">
          <div style="font-size:12px;font-weight:600;color:#991b1b;margin-bottom:6px">
            Issues Found ({len(report['issues'])})</div>
          {issue_items}</div>"""

    return f"""<div style="padding:16px 20px">
      <div style="display:flex;align-items:center;gap:16px;margin-bottom:14px">
        <div style="width:56px;height:56px;border-radius:50%;background:{color};color:#fff;
             display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800">{grade}</div>
        <div>
          <div style="font-size:18px;font-weight:700">AI Briefing Quality: {score:.0%}</div>
          <div style="font-size:12px;color:#6b7280">Evaluated against deterministic engine ground truth</div>
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr>
          <th style="text-align:left;font-size:11px;color:#9ca3af;padding:4px 8px">METRIC</th>
          <th style="text-align:left;font-size:11px;color:#9ca3af;padding:4px 8px">SCORE</th>
          <th style="text-align:left;font-size:11px;color:#9ca3af;padding:4px 8px">DETAIL</th>
          <th style="text-align:center;font-size:11px;color:#9ca3af;padding:4px 8px">WEIGHT</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {issue_html}
      <div style="margin-top:14px;text-align:right">
        <a href="eval_explained.html" target="_blank"
           style="display:inline-block;background:#1d4ed8;color:#fff;padding:8px 18px;
                  border-radius:6px;font-size:13px;font-weight:600;text-decoration:none">
          What do these metrics mean?</a>
      </div>
    </div>"""
