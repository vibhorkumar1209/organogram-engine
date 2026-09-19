"""
Department routing — keywords must match whole words, not substrings.

_score matched keywords as bare substrings, so short ones fired inside
ordinary words. "cto" is inside "dire(cto)r", which scored Engineering 100 on
EVERY title containing "Director": "Director of Sales" classified as
Engineering, and a finance roster ingested against the CFO's department
scattered. Four more collided the same way — "coo" in "coordinator", "it" in
"security"/"quality"/"architect", "pr" in "president", "ui" in "recruiter".

Run: python3 backend/test_classifier_routing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

import classifier as C
from inference_logic import InferenceEngine

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


_engine = InferenceEngine(industry="")


def dept_of(title: str) -> str:
    rec = {"Full Name": "Test Person", "Designation": title, "Company": "Acme"}
    return _engine.classify_all([rec])[0].dept_primary


# ── The substring collisions, asserted directly on the scorer ───────────────
for word, kw, dept in [("director", "cto", "Engineering"),
                       ("coordinator", "coo", "Operations"),
                       ("recruiter", "ui", "Product Management"),
                       ("president", "pr", "Corporate Communications & Public Affairs"),
                       ("security", "it", "Information Technology")]:
    scored = C._score(word, [(100, kw)])
    ok(scored == 0, f"boundary: '{kw}' no longer matches inside '{word}'")

ok(C._score("cto of engineering", [(100, "cto")]) == 100,
   "boundary: a real standalone keyword still scores")
ok(C._score("chief technology officer", [(90, "chief technology officer")]) == 90,
   "boundary: multi-word keyword still scores")
ok(C._score("head of r&d", [(90, "r&d")]) == 90,
   "boundary: punctuated keyword (r&d) still scores")

# "Director" is a seniority, not a function — on its own it should not pick
# any department.
scores = {d: C._score("director", r) for d, r in C._DEPT_SCORE_RULES}
ok(max(scores.values()) == 0, "boundary: bare 'Director' scores no department at all")

# ── Routing: "Director of X" must follow X ──────────────────────────────────
for title, expect in [
    ("Director of Sales", "Sales"),
    ("Director of Financial Planning", "Finance"),
    ("Director of Marketing", "Marketing"),
    ("Director of Human Resources", "Human Resources"),
    ("Director of Legal Affairs", "Legal"),
    ("Director of Engineering", "Engineering"),
    ("Director of Operations", "Operations"),
]:
    got = dept_of(title)
    ok(expect.lower() in got.lower(), f"routing: {title} -> {got}")

# The equivalent "X Director" spelling already worked and must keep working.
for title, expect in [("Sales Director", "Sales"), ("Finance Director", "Finance"),
                      ("Marketing Director", "Marketing")]:
    got = dept_of(title)
    ok(expect.lower() in got.lower(), f"routing: {title} -> {got} (unchanged spelling)")

# ── Titles that previously collided on coo/it/pr/ui ─────────────────────────
for title, expect in [
    ("Logistics Coordinator", "Operations"),
    ("Project Coordinator", ""),            # any dept, just not via "coo"
    ("Recruiter", "Human Resources"),
    ("Talent Acquisition Coordinator", "Human Resources"),
    ("Security Analyst", "Information Technology"),
    ("Quality Assurance Manager", ""),
]:
    got = dept_of(title)
    if expect:
        ok(expect.lower() in got.lower(), f"routing: {title} -> {got}")
    else:
        ok(bool(got), f"routing: {title} -> {got} (classified, no longer a collision)")

# ── Broad regression set: the ordinary titles a roster actually contains ────
ROSTER = [
    ("Senior Accountant", "Finance"), ("VP Finance", "Finance"),
    ("Financial Controller", "Finance"), ("Treasury Manager", "Finance"),
    ("Sales Manager", "Sales"), ("Account Executive", "Sales"),
    ("Head of Business Development", "Sales"),
    ("Brand Manager", "Marketing"), ("Head of Digital Marketing", "Marketing"),
    ("Software Engineer", "Engineering"), ("VP Engineering", "Engineering"),
    ("HR Business Partner", "Human Resources"),
    ("Head of People Operations", "Human Resources"),
    ("Legal Counsel", "Legal"), ("Compliance Officer", "Legal"), ("Paralegal", "Legal"),
    ("Product Manager", "Product"), ("Customer Success Manager", "Customer"),
    ("IT Support Specialist", "Information Technology"),
    ("Systems Administrator", "Information Technology"),
]
wrong = [(t, dept_of(t)) for t, exp in ROSTER if exp.lower() not in dept_of(t).lower()]
ok(not wrong, f"roster: all {len(ROSTER)} ordinary titles route sensibly"
              + (f" (off: {wrong})" if wrong else ""))

# Supply chain has no department of its own in this taxonomy; Operations is
# its canonical home, so these are correct rather than misrouted.
for title in ["Supply Chain Manager", "Procurement Lead"]:
    ok(dept_of(title) == "Operations",
       f"roster: {title} -> Operations (no Supply Chain dept exists)")

# ── Speed: this runs per record per department on a 0.5 CPU instance ────────
import time
_recs = [{"Full Name": f"P{i}", "Designation": ROSTER[i % len(ROSTER)][0], "Company": "X"}
         for i in range(500)]
_t = time.time()
InferenceEngine(industry="").classify_all(_recs)
_elapsed = time.time() - _t
ok(_elapsed < 5.0, f"speed: 500 records classified in {_elapsed:.2f}s")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
