"""
AI Briefing Evaluation — scores a data-to-text briefing against ground truth
from the deterministic scoring engine.

This is NOT RAG (no retrieval step). The pipeline is:
  structured data (scoring engine output) -> AI prompt -> narrative briefing

Metrics (data-to-text generation standard):
  - Groundedness:  Is every claim traceable to the input data? No fabricated entities?
  - Coverage:      Does the briefing mention all critical items from the data?
  - Correctness:   Are numbers, percentages, and factual associations accurate?
  - Actionability: Does the output help the ops manager make tonight's dispatch decisions?

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
    Score an AI briefing against ground truth from the deterministic engine.

    ground_truth dict:
      blockers:       list of {"vehicle_id", "depot", "reason"}
      post_repair:    list of {"vehicle_id", "dtc_code", "wo_id"}
      top10:          list of {"vehicle_id", "driver_id", "depot"}
      stats:          {"total_raw", "duplicates", "scored", "my_high", ...}
      driver_top5:    list of (driver_id, {"vehicle_id", "firmware", ...})
      valid_vehicles: set of valid vehicle IDs
      valid_drivers:  set of valid driver IDs
      all_wo_ids:     set of all work order IDs in the data
    """
    if not text or len(text.strip()) < 20:
        return _empty_report("Briefing is empty or too short")

    metrics = {}
    issues = []

    metrics["groundedness"] = _score_groundedness(text, ground_truth, issues)
    metrics["coverage"] = _score_coverage(text, ground_truth, issues)
    metrics["correctness"] = _score_correctness(text, ground_truth, issues)
    metrics["actionability"] = _score_actionability(text, ground_truth, issues)

    weights = {
        "groundedness": 0.35,
        "coverage": 0.25,
        "correctness": 0.25,
        "actionability": 0.15,
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
                    ["groundedness", "coverage", "correctness", "actionability"]},
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
#  Groundedness — is every claim traceable to input data? No fabrication?
#
#  This replaces the old "faithfulness" (a RAG term) and "hallucination"
#  metrics. In data-to-text generation, groundedness = the output only
#  contains information present in the structured input.
#  Two sub-dimensions:
#    (a) Entity grounding: every vehicle, driver, WO, DTC exists in the data
#    (b) Relation grounding: associations (vehicle-depot, vehicle-blocker
#        status) match the data
# ---------------------------------------------------------------------------

def _score_groundedness(text, gt, issues):
    checks = []
    text_lower = text.lower()

    valid_vids = gt.get("valid_vehicles", set())
    valid_dids = gt.get("valid_drivers", set())
    blocker_vids = {b["vehicle_id"] for b in gt.get("blockers", [])}

    # (a) Entity grounding — are mentioned entities real?

    mentioned_vids = set(re.findall(r"V-\d+", text))
    hallucinated_vids = mentioned_vids - valid_vids
    if hallucinated_vids:
        checks.append(("fail", f"Fabricated vehicles: {hallucinated_vids}"))
        issues.append({"severity": "critical", "metric": "groundedness",
                       "message": f"Invented vehicle IDs: {hallucinated_vids}"})
    else:
        checks.append(("pass", f"All {len(mentioned_vids)} vehicle IDs exist in fleet data"))

    mentioned_dids = set(re.findall(r"D-\d+", text))
    hallucinated_dids = mentioned_dids - valid_dids
    if hallucinated_dids:
        checks.append(("fail", f"Fabricated drivers: {hallucinated_dids}"))
        issues.append({"severity": "critical", "metric": "groundedness",
                       "message": f"Invented driver IDs: {hallucinated_dids}"})
    else:
        checks.append(("pass", f"All {len(mentioned_dids)} driver IDs exist in fleet data"))

    mentioned_wos = set(re.findall(r"WO-\d+", text))
    all_wo_ids = gt.get("all_wo_ids", set())
    hallucinated_wos = mentioned_wos - all_wo_ids
    if hallucinated_wos:
        checks.append(("fail", f"Fabricated work orders: {hallucinated_wos}"))
        issues.append({"severity": "high", "metric": "groundedness",
                       "message": f"Invented work order IDs: {hallucinated_wos}"})
    elif mentioned_wos:
        checks.append(("pass", f"All {len(mentioned_wos)} work order IDs exist in data"))

    mentioned_dtcs = set(re.findall(r"P\d{4}", text))
    valid_dtcs = {"P0524", "P0217", "P0300", "P0171", "P0128", "P0420"}
    hallucinated_dtcs = mentioned_dtcs - valid_dtcs
    if hallucinated_dtcs:
        checks.append(("fail", f"Fabricated DTC codes: {hallucinated_dtcs}"))
        issues.append({"severity": "high", "metric": "groundedness",
                       "message": f"Invented DTC codes: {hallucinated_dtcs}"})
    elif mentioned_dtcs:
        checks.append(("pass", f"All {len(mentioned_dtcs)} DTC codes exist in reference"))

    # (b) Relation grounding — are associations correct?

    # Vehicle-depot associations
    for b in gt.get("blockers", []):
        vid = b["vehicle_id"]
        depot = b.get("depot", "")
        if vid.lower() in text_lower and depot:
            vid_idx = text_lower.find(vid.lower())
            context = text_lower[max(0, vid_idx - 150):vid_idx + 150]
            if depot.lower() in context:
                checks.append(("pass", f"{vid} correctly placed at {depot}"))
            else:
                other_depots = [d for d in ["north yard", "airport hub", "hamilton depot"]
                                if d in context and d != depot.lower()]
                if other_depots:
                    checks.append(("fail", f"{vid} placed at {other_depots[0]}, actual: {depot}"))
                    issues.append({"severity": "high", "metric": "groundedness",
                                   "message": f"{vid} associated with wrong depot"})

    # Non-blockers falsely called blockers (fabricated status)
    for vid in mentioned_vids:
        if vid not in blocker_vids:
            idx = text_lower.find(vid.lower())
            if idx >= 0:
                context = text_lower[max(0, idx - 100):idx + 100]
                if any(w in context for w in ["do not dispatch", "blocker", "block",
                                               "must not dispatch", "stop vehicle", "grounded"]):
                    checks.append(("fail", f"{vid} falsely called a dispatch blocker"))
                    issues.append({"severity": "critical", "metric": "groundedness",
                                   "message": f"{vid} is not a blocker but AI says it is"})

    # Post-repair fault relationships
    for pr in gt.get("post_repair", []):
        vid = pr["vehicle_id"]
        wo = pr.get("wo_id", "")
        if vid.lower() in text_lower:
            vid_idx = text_lower.find(vid.lower())
            context = text_lower[max(0, vid_idx - 200):vid_idx + 200]
            if "after" in context and ("repair" in context or "work order" in context or wo.lower() in context):
                checks.append(("pass", f"{vid} post-repair relationship correctly described"))
            elif "repair" in context or wo.lower() in context:
                checks.append(("partial", f"{vid} mentions repair but not the temporal sequence"))

    # Score: heavily penalise fabrication
    failed = sum(1 for s, _ in checks if s == "fail")
    partial = sum(1 for s, _ in checks if s == "partial")
    passed = sum(1 for s, _ in checks if s == "pass")
    total = passed + partial + failed
    score = max(0, (passed + 0.5 * partial) / max(total, 1))
    if any(i["severity"] == "critical" for i in issues if i.get("metric") == "groundedness"):
        score = min(score, 0.3)

    return {"score": round(score, 3),
            "detail": f"{passed} grounded, {partial} partial, {failed} ungrounded",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Coverage — does it mention all critical data points?
#
#  Standard data-to-text metric: what fraction of important input facts
#  appear in the output?
# ---------------------------------------------------------------------------

def _score_coverage(text, gt, issues):
    checks = []
    text_lower = text.lower()

    # Must mention all dispatch blockers
    for b in gt.get("blockers", []):
        vid = b["vehicle_id"]
        if vid.lower() in text_lower:
            checks.append(("pass", f"Mentions blocker {vid}"))
        else:
            checks.append(("fail", f"Missing blocker {vid}"))
            issues.append({"severity": "high", "metric": "coverage",
                           "message": f"Dispatch blocker {vid} not mentioned"})

    # Must mention post-repair faults with DTC
    for pr in gt.get("post_repair", []):
        vid = pr["vehicle_id"]
        dtc = pr.get("dtc_code", "")
        if vid.lower() in text_lower and dtc.lower() in text_lower:
            checks.append(("pass", f"Mentions post-repair {vid}/{dtc}"))
        elif vid.lower() in text_lower:
            checks.append(("partial", f"Mentions {vid} but not fault code {dtc}"))
        else:
            checks.append(("fail", f"Missing post-repair fault {vid}/{dtc}"))
            issues.append({"severity": "medium", "metric": "coverage",
                           "message": f"Post-repair fault {vid}/{dtc} not mentioned"})

    # Should mention data quality facts
    if "duplicate" in text_lower:
        checks.append(("pass", "Mentions duplicate alerts"))
    else:
        checks.append(("fail", "No mention of duplicate alerts"))

    if "vendor" in text_lower or "over-report" in text_lower or "re-rank" in text_lower:
        checks.append(("pass", "Mentions vendor severity discrepancy"))
    else:
        checks.append(("partial", "No mention of vendor severity over-reporting"))

    passed = sum(1 for s, _ in checks if s == "pass")
    partial = sum(1 for s, _ in checks if s == "partial")
    failed = sum(1 for s, _ in checks if s == "fail")
    total = passed + partial + failed
    score = (passed + 0.5 * partial) / max(total, 1)

    return {"score": round(score, 3), "detail": f"{passed}/{total} items covered, {failed} missing",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Correctness — are numbers, percentages, and facts accurate?
#
#  Merges the old "numerical accuracy" into a broader correctness check.
#  In data-to-text generation this measures whether the output faithfully
#  reproduces quantitative facts from the structured input.
# ---------------------------------------------------------------------------

def _score_correctness(text, gt, issues):
    checks = []
    stats = gt.get("stats", {})

    # Check specific number claims against ground truth
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
                issues.append({"severity": "medium", "metric": "correctness",
                               "message": f"{label}: AI says {claimed}, actual is {actual}"})

    # Check percentage claims
    pct_match = re.search(r"(\d+)%\s*[Dd]uplicate", text)
    if pct_match and stats.get("total_raw"):
        claimed_pct = int(pct_match.group(1))
        actual_pct = round(stats["duplicates"] / stats["total_raw"] * 100)
        if abs(claimed_pct - actual_pct) <= 5:
            checks.append(("pass", f"Duplicate %: claimed {claimed_pct}%, actual ~{actual_pct}%"))
        else:
            checks.append(("fail", f"Duplicate %: claimed {claimed_pct}%, actual ~{actual_pct}%"))
            issues.append({"severity": "medium", "metric": "correctness",
                           "message": f"Duplicate percentage: AI says {claimed_pct}%, actual ~{actual_pct}%"})

    # Check for false "no duplicates" claim
    no_dup = re.search(r"no\s+(?:issues?\s+with\s+)?duplicate", text, re.IGNORECASE)
    if no_dup and stats.get("duplicates", 0) > 0:
        checks.append(("fail", f"Claims no duplicates, but {stats['duplicates']} found"))
        issues.append({"severity": "medium", "metric": "correctness",
                       "message": f"Claims no duplicates but we found {stats['duplicates']}"})

    passed = sum(1 for s, _ in checks if s == "pass")
    failed = sum(1 for s, _ in checks if s == "fail")
    total = passed + failed
    score = passed / max(total, 1)

    return {"score": round(score, 3), "detail": f"{passed}/{total} facts accurate",
            "checks": checks}


# ---------------------------------------------------------------------------
#  Actionability — does the output help the ops manager decide?
#
#  Domain-specific metric (not a standard NLG metric, but critical here).
#  A briefing can be grounded, complete, and correct but still useless
#  if it buries the dispatch-blocking information.
# ---------------------------------------------------------------------------

def _score_actionability(text, gt, issues):
    checks = []
    text_lower = text.lower()

    blockers = gt.get("blockers", [])
    top10 = gt.get("top10", [])

    # Blockers should appear before non-blocker action items
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
            checks.append(("pass", "Blockers appear before lower-priority items"))
        else:
            checks.append(("fail", f"{non_blocker_before} non-critical items appear before blockers"))
            issues.append({"severity": "medium", "metric": "actionability",
                           "message": "Lower-priority items mentioned before dispatch blockers"})

    # Post-repair faults should be emphasized (most surprising/dangerous)
    post_repair = gt.get("post_repair", [])
    if post_repair:
        pr_mentioned = sum(1 for pr in post_repair if pr["vehicle_id"].lower() in text_lower)
        if pr_mentioned == len(post_repair):
            checks.append(("pass", f"All {len(post_repair)} post-repair faults surfaced"))
        elif pr_mentioned > 0:
            checks.append(("partial", f"{pr_mentioned}/{len(post_repair)} post-repair faults surfaced"))
        else:
            checks.append(("fail", "Post-repair faults not surfaced"))
            issues.append({"severity": "high", "metric": "actionability",
                           "message": "Post-repair faults (most critical finding) not mentioned"})

    # Next-step language: does it tell the manager what to DO?
    action_words = ["inspect", "dispatch", "do not", "check", "open a work order",
                    "schedule", "re-inspect", "maintenance", "shop", "tonight", "tomorrow"]
    action_count = sum(1 for w in action_words if w in text_lower)
    if action_count >= 3:
        checks.append(("pass", f"Contains actionable language ({action_count} action phrases)"))
    elif action_count >= 1:
        checks.append(("partial", f"Some actionable language ({action_count} action phrases)"))
    else:
        checks.append(("fail", "No actionable language — reads as a status report, not a decision brief"))
        issues.append({"severity": "medium", "metric": "actionability",
                       "message": "Briefing lacks actionable next-step language"})

    # Firmware context when discussing drivers
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

    return {"score": round(score, 3),
            "detail": f"{passed} actionable, {partial} partial, {failed} not actionable",
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
