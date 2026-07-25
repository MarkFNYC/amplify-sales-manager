"""Seven-stage call classifier — file-replay path.

Reads a saved stream of MeetStream live-transcription webhook payloads
(fields: speakerName, timestamp, new_text, end_of_turn, word_is_final, ...),
reconstructs committed segments, classifies each into one of the seven
stages defined in stages.json, evaluates every per-stage check
independently, and detects the headline sequence violation:
provide_solution occurring before assess_need is complete.

No APIs, no keys, no network. Rule-based only.

Per stages.json constraints, the classifier's per-segment output is
constrained to a stage label plus a span reference (timestamp + quote).
It performs no inference beyond phrase matching — nothing here reads or
infers familial status, race, religion, national origin, or disability,
and no check cue may reference them.

Usage:
    python src/classifier.py [transcripts/sample_call_001.json]
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGES_PATH = REPO_ROOT / "stages.json"
DEFAULT_TRANSCRIPT = REPO_ROOT / "transcripts" / "sample_call_001.json"

# Phrase cues per stage id. Ids must match stages.json — that file is
# authoritative for the stage list; this table only supplies the lexicon.
STAGE_CUES = {
    "engage": [
        r"how did you hear", r"\breferr(ed|al)\b", r"thanks for making the time",
        r"glad we could connect", r"congratulations", r"great to meet",
    ],
    "qualify": [
        r"driving the move", r"start date", r"\btimeline\b", r"need to be settled",
        r"\brenting\b", r"nothing to sell", r"home to sell", r"pre-?approved",
        r"\blender\b", r"\bcash\b", r"decide together", r"part of the process",
        r"who else is involved",
    ],
    "assess_need": [
        r"hoping for", r"must-?haves?", r"nice-?to-?have", r"what (you|we) (actually )?need",
        r"walk me through", r"what i'?m hearing", r"did i get that right",
        r"sounds like you", r"the big thing", r"\bcommute\b", r"wants, not needs",
    ],
    "capabilities_trust": [
        r"track record", r"my process", r"what happens next", r"buyer representation",
        r"\bcompensation\b", r"\bcommission\b", r"i'?ve helped", r"clients like you",
        r"how i work",
    ],
    "provide_solution": [
        r"perfect place", r"a listing on", r"i'?d recommend", r"recommend we",
        r"focus the search", r"filtered search", r"get us in", r"just dropped to",
        r"i can show you",
    ],
    "confirm_commitment": [
        r"lock in", r"\bschedule\b", r"book (a|the)", r"next meeting",
        r"(that|ten|saturday) works", r"can we (do|meet|lock)", r"first tour",
    ],
    "serve": [
        r"i'?ll send", r"check in every", r"follow up (with|by)", r"by tomorrow",
        r"you always know", r"keep you posted",
    ],
}

# assess_need completion, per its checks in stages.json: the need is
# reflected back, then a different speaker gives an explicit yes.
REFLECT_BACK = re.compile(
    r"what i'?m hearing|did i get that right|so to confirm|"
    r"just to make sure i have (this|that) right|sounds like you need",
    re.I,
)
EXPLICIT_YES = re.compile(
    r"^\s*(yes|yeah|yep|exactly|that'?s right|correct)\b", re.I,
)

# "may" is omitted on purpose — as a modal verb it false-positives.
MONTHS = (
    r"\b(january|february|march|april|june|july|august|september|october|"
    r"november|december)\b"
)
MAPS_TO_NEED = r"must-?haves?|maps to|what you (said|need|asked for)|those needs|exactly those"

# Timeline contradiction: a soft no-rush cue followed later by a hard
# deadline (lease end, job start, school year, closing date) that was
# never reconciled on the call.
NO_RUSH = re.compile(
    r"no rush|not in a (rush|hurry)|no hurry|just (looking|browsing|starting to look)",
    re.I,
)
HARD_DEADLINE = re.compile(
    r"lease (ends?|is up|expires|runs out)|(job|position|new role) (starts?|begins)|"
    r"school year|before school starts|closing date|close on",
    re.I,
)

# One rule per check, aligned by index with stages.json — that file remains
# authoritative for the check text; this table only supplies detection.
#   {"any": [...]}            met if any segment matches any pattern
#   {"all": [[...], [...]]}   met if every group matches somewhere;
#                             evidence is the segment completing the last group
#   {"scope": stage_id}       restrict matching to segments of that stage
#   {"special": name}         structural rule handled in eval_check()
CHECK_RULES = {
    "engage": [
        {"special": "rapport_first"},
        {"any": [r"how did you hear", r"who referred you", r"how did you find (me|us)"]},
        {"any": [r"congratulations", r"new (role|job)", r"\brelocation\b",
                 r"(she|he|they) mentioned"]},
    ],
    "qualify": [
        {"any": [r"driving the move", r"why are you (moving|looking)",
                 r"what'?s prompting", r"\bforced\b"]},
        # The check demands an actual date or trigger, so a rep merely asking
        # ("is the start date fixed?") doesn't satisfy it — only date content.
        {"any": [MONTHS, r"need to be settled",
                 r"by the end of (the )?(year|month|summer|spring|fall|winter)"]},
        {"any": [r"pre-?approved", r"\blender\b", r"\bmortgage\b", r"financing",
                 r"cash (buyer|offer)", r"paying cash"]},
        {"any": [r"decide together", r"who else", r"part of the process",
                 r"decision.?makers?"]},
        {"any": [r"\brenting\b", r"month to month", r"own (a|our|your) (home|place)",
                 r"(nothing|home) to sell", r"sell first"]},
    ],
    "assess_need": [
        {"all": [[r"must-?haves?"], [r"nice-?to-?haves?"]]},
        {"any": [r"what i'?m hearing", r"did i get that right", r"so to confirm",
                 r"sounds like you need"]},
        {"special": "explicit_yes"},
    ],
    "capabilities_trust": [
        {"any": [r"track record", r"i'?ve helped", r"clients like you", r"closed \d+"]},
        {"any": [r"my process", r"what happens next", r"here'?s how (it|this) works"]},
        # promote: when missed, this is always a top-level finding, never a
        # sub-bullet of a missed stage — it's a compliance conversation.
        {"any": [r"buyer representation", r"\bcompensation\b", r"\bcommission\b",
                 r"how i (get|am) paid"],
         "promote": True},
    ],
    "provide_solution": [
        {"special": "solution_maps_to_need"},
        {"any": [r"\btimeline\b", r"by (early |mid |late )?(september|october)",
                 r"before (the|your) start", r"move-?in", r"\blease\b", r"renting"],
         "scope": "provide_solution"},
    ],
    "confirm_commitment": [
        {"any": [r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow) at",
                 r"at (nine|ten|eleven|noon|\d)"],
         "scope": "confirm_commitment"},
        {"special": "both_committed"},
        {"any": [r"\bworks\b", r"see you (then|on)", r"\bconfirmed\b", r"locked in"],
         "scope": "confirm_commitment"},
    ],
    "serve": [
        {"any": [r"by (tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|the end of)",
                 r"within (a|an|\d)", r"follow up (by|on|within)"],
         "scope": "serve"},
        {"any": [r"every (monday|tuesday|wednesday|thursday|friday|week|other)",
                 r"\bweekly\b", r"check in every"],
         "scope": "serve"},
    ],
}


def mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def load_events(path):
    with open(path) as f:
        data = json.load(f)
    events = data["events"] if isinstance(data, dict) else data
    return sorted(events, key=lambda e: e["timestamp"])


def committed_segments(events):
    """Rebuild committed turns: append final fragments, commit on end_of_turn.

    word_is_final: false events are interim and dropped — a final version of
    the same words always follows.
    """
    segments = []
    buf_text, buf_speaker, buf_start = "", None, None

    def flush():
        nonlocal buf_text, buf_speaker, buf_start
        if buf_text:
            segments.append({"speaker": buf_speaker, "t": buf_start, "text": buf_text.strip()})
        buf_text, buf_speaker, buf_start = "", None, None

    for ev in events:
        if not ev.get("word_is_final", True):
            continue
        if buf_speaker is not None and ev["speakerName"] != buf_speaker:
            flush()  # defensive: speaker changed without an end_of_turn
        if buf_speaker is None:
            buf_speaker, buf_start = ev["speakerName"], ev["timestamp"]
        buf_text = (buf_text + " " + ev["new_text"]).strip()
        if ev.get("end_of_turn"):
            flush()
    flush()
    return segments


def classify(segments, stage_ids):
    """Assign a stage id to each segment: highest cue-match count wins,
    ties break toward the earlier stage in canonical order, and segments
    with no matches inherit the previous segment's stage (continuations
    like a bare 'Yes, exactly right.')."""
    compiled = {
        sid: [re.compile(p, re.I) for p in STAGE_CUES.get(sid, [])]
        for sid in stage_ids
    }
    prev = None
    for seg in segments:
        scores = {
            sid: sum(1 for rx in pats if rx.search(seg["text"]))
            for sid, pats in compiled.items()
        }
        best = max(stage_ids, key=lambda sid: scores[sid])
        seg["stage"] = best if scores[best] > 0 else prev
        prev = seg["stage"]
    return segments


def assess_completed_at(segments):
    """Segment at which assess_need became complete: a reflect-back is
    answered with an explicit yes by a different speaker. None if never."""
    pending = None
    for seg in segments:
        if REFLECT_BACK.search(seg["text"]):
            pending = seg
        elif pending and seg["speaker"] != pending["speaker"] and EXPLICIT_YES.match(seg["text"]):
            return seg
    return None


def first_match(segments, patterns):
    compiled = [re.compile(p, re.I) for p in patterns]
    for seg in segments:
        if any(rx.search(seg["text"]) for rx in compiled):
            return seg
    return None


def eval_check(rule, segments, order, confirm_seg):
    """Evaluate one check rule → (met, evidence_segment_or_None)."""
    pool = [s for s in segments if s["stage"] == rule["scope"]] if "scope" in rule else segments

    if "any" in rule:
        seg = first_match(pool, rule["any"])
        return (seg is not None), seg

    if "all" in rule:
        evidence = None
        for group in rule["all"]:
            seg = first_match(pool, group)
            if seg is None:
                return False, None
            if evidence is None or seg["t"] > evidence["t"]:
                evidence = seg
        return True, evidence

    special = rule["special"]
    if special == "rapport_first":
        engage = [s for s in segments if s["stage"] == "engage"]
        transactional = [s for s in segments if s["stage"] and order[s["stage"]] > 0]
        met = bool(engage) and (not transactional or engage[0]["t"] < transactional[0]["t"])
        return met, engage[0] if met else None

    if special == "explicit_yes":
        return (confirm_seg is not None), confirm_seg

    if special == "solution_maps_to_need":
        if confirm_seg is None:
            return False, None
        rx = re.compile(MAPS_TO_NEED, re.I)
        for seg in segments:
            if seg["stage"] == "provide_solution" and seg["t"] >= confirm_seg["t"] and rx.search(seg["text"]):
                return True, seg
        return False, None

    if special == "both_committed":
        rx = re.compile(r"\bi'?ll\b", re.I)
        seen = set()
        for seg in segments:
            if seg["stage"] == "confirm_commitment" and rx.search(seg["text"]):
                seen.add(seg["speaker"])
                if len(seen) >= 2:
                    return True, seg
        return False, None

    raise SystemExit(f"unknown special check rule: {special}")


def evaluate_checks(segments, stages, confirm_seg):
    """Evaluate every check of every stage independently.

    Returns {stage_id: [{check, met, evidence}, ...]} aligned with
    stages.json order."""
    order = {s["id"]: i for i, s in enumerate(stages)}
    results = {}
    for stage in stages:
        rules = CHECK_RULES.get(stage["id"], [])
        if len(rules) != len(stage["checks"]):
            raise SystemExit(
                f"CHECK_RULES for '{stage['id']}' has {len(rules)} rules but "
                f"stages.json defines {len(stage['checks'])} checks — realign them."
            )
        results[stage["id"]] = [
            dict(zip(("met", "evidence"), eval_check(rule, segments, order, confirm_seg)),
                 check=check, promoted=rule.get("promote", False))
            for check, rule in zip(stage["checks"], rules)
        ]
    return results


def moved_past(segments, stages, stage_id):
    """First segment where the conversation moved beyond stage_id in
    canonical order — the evidence moment for anything missed there."""
    order = {s["id"]: i for i, s in enumerate(stages)}
    return next(
        (s for s in segments if s["stage"] and order[s["stage"]] > order[stage_id]),
        None,
    )


def build_findings(segments, stages, sequence_rule, check_results, confirm_seg):
    findings = []
    names = {s["id"]: s["name"] for s in stages}
    confirmed_t = confirm_seg["t"] if confirm_seg else None

    # Headline: provide_solution before assess_need is complete.
    for seg in segments:
        if seg["stage"] != "provide_solution":
            continue
        if confirmed_t is None or seg["t"] < confirmed_t:
            headline = sequence_rule["output"].replace("{timestamp}", mmss(seg["t"]))
            note = (
                f'The need was only confirmed later, at {mmss(confirmed_t)}: '
                f'"{confirm_seg["text"]}"'
                if confirmed_t is not None
                else "The need was never explicitly confirmed on this call."
            )
            findings.append({
                "kind": "SEQUENCE", "t": seg["t"], "headline": headline,
                "quote": seg, "note": note, "subnotes": [],
            })

    # Timeline contradiction: a no-rush cue followed later by a hard deadline.
    no_rush = next((s for s in segments if NO_RUSH.search(s["text"])), None)
    if no_rush:
        deadline = next(
            (s for s in segments
             if s["t"] >= no_rush["t"] and HARD_DEADLINE.search(s["text"])),
            None,
        )
        if deadline:
            findings.append({
                "kind": "CONTRADICTION", "t": deadline["t"],
                "headline": (
                    f"They said no rush at {mmss(no_rush['t'])}, then a hard deadline "
                    f"surfaced at {mmss(deadline['t'])} — a missed opportunity to "
                    "reconcile the timeline."
                ),
                "quote": no_rush,
                "quote2": deadline if deadline is not no_rush else None,
                "note": "Worth pinning down which timeline is real — the deadline usually wins.",
                "subnotes": [],
            })

    def check_finding(sid, result, later):
        quote = later or (segments[-1] if segments else None)
        return {
            "kind": "CHECK", "t": quote["t"] if quote else None,
            "headline": f'{names[sid]} — "{result["check"]}" didn\'t happen. '
                        "A missed opportunity.",
            "quote": quote,
            "note": (
                f'The conversation moved on to "{names[later["stage"]]}" at '
                f"{mmss(later['t'])} without it."
                if later else "The call ended without it."
            ),
            "subnotes": [],
        }

    covered = {seg["stage"] for seg in segments}
    for stage in stages:
        sid = stage["id"]
        later = moved_past(segments, stages, sid)

        # Whole stage missed: one stage-level finding subsumes its missed
        # checks — except promoted ones, which always stand on their own.
        if sid not in covered:
            missed = [r for r in check_results[sid] if not r["met"]]
            findings.append({
                "kind": "COVERAGE", "t": later["t"] if later else None,
                "headline": f'"{names[sid]}" never came up — a missed opportunity.',
                "quote": later,
                "note": (
                    f'The conversation reached "{names[later["stage"]]}" at '
                    f"{mmss(later['t'])} without it."
                    if later else "The call ended before this stage."
                ),
                "subnotes": [f"Missed with it: {r['check']}"
                             for r in missed if not r["promoted"]],
            })
            findings.extend(check_finding(sid, r, later)
                            for r in missed if r["promoted"])
            continue

        # Stage covered: each missed check is its own finding.
        findings.extend(check_finding(sid, r, later)
                        for r in check_results[sid] if not r["met"])

    # Headline sequence findings first, then chronological.
    findings.sort(key=lambda f: (f["kind"] != "SEQUENCE", f["t"] is None, f["t"] or 0))
    return findings


def render_report(path, events, segments, stages, check_results, findings):
    names = {s["id"]: s["name"] for s in stages}
    attrs = next((e.get("custom_attributes") for e in events if e.get("custom_attributes")), {}) or {}
    lines = []
    w = lines.append

    w("=" * 72)
    w("AMPLIFY SALES MANAGER — CALL DEBRIEF (file replay)")
    w(f"  transcript : {path}")
    w(f"  rep        : {attrs.get('rep_identifier', 'unknown')}"
      f"    session: {attrs.get('session', 'unknown')}")
    w(f"  segments   : {len(segments)} committed"
      f"    length: {mmss(segments[-1]['t'])}" if segments else "  segments   : 0")
    w("=" * 72)

    w("")
    w("STAGE COVERAGE")
    for i, stage in enumerate(stages, 1):
        segs = [s for s in segments if s["stage"] == stage["id"]]
        checks = check_results[stage["id"]]
        met = sum(1 for r in checks if r["met"])
        tally = f"checks {met}/{len(checks)}"
        if segs:
            span = f"{mmss(segs[0]['t'])}–{mmss(segs[-1]['t'])}"
            w(f"  {i}. {stage['name']:<42} covered   {len(segs):>2} segments   {span}   {tally}")
        else:
            w(f"  {i}. {stage['name']:<42} MISSED{'':>26}{tally}")

    w("")
    w("CHECK COVERAGE")
    for i, stage in enumerate(stages, 1):
        w(f"  {i}. {stage['name']}")
        for r in check_results[stage["id"]]:
            mark = "met   " if r["met"] else "MISSED"
            ref = f"   ({mmss(r['evidence']['t'])})" if r["evidence"] else ""
            w(f"       {mark}  {r['check']}{ref}")

    w("")
    w("SEGMENTS (stage label + span reference)")
    for seg in segments:
        label = names.get(seg["stage"], "unclassified")
        w(f"  {mmss(seg['t']):>5}  {seg['speaker']:<14} {label}")

    w("")
    w(f"FINDINGS — {len(findings)} missed opportunit"
      f"{'y' if len(findings) == 1 else 'ies'} (each with timestamp + verbatim quote)")
    if not findings:
        w("  Every stage was covered, in order, with every check met. Clean call.")
    for f in findings:
        w("")
        w(f"  [{f['kind']}] {f['headline']}")
        if f["quote"]:
            w(f'      {mmss(f["quote"]["t"])}  {f["quote"]["speaker"]}: "{f["quote"]["text"]}"')
        if f.get("quote2"):
            w(f'      {mmss(f["quote2"]["t"])}  {f["quote2"]["speaker"]}: "{f["quote2"]["text"]}"')
        w(f"      {f['note']}")
        for sub in f["subnotes"]:
            w(f"        · {sub}")
    w("")
    return "\n".join(lines)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRANSCRIPT
    with open(STAGES_PATH) as f:
        config = json.load(f)
    stages = config["stages"]

    events = load_events(path)
    segments = committed_segments(events)
    classify(segments, [s["id"] for s in stages])
    confirm_seg = assess_completed_at(segments)
    check_results = evaluate_checks(segments, stages, confirm_seg)
    findings = build_findings(segments, stages, config["sequence_rule"], check_results, confirm_seg)
    print(render_report(path, events, segments, stages, check_results, findings))


if __name__ == "__main__":
    main()
