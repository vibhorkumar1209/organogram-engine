"""
Organogram NLP Classifier — v2
================================
Primary signals  : job_title, linkedin_headline
Guidance signal  : job_function (soft hint, never authoritative)
Ignored          : department field
Reference        : Global_Org_Hierarchy.xlsx   (16 canonical L0 departments)
                   Global_Designation_Hierarchy.xlsx (G0–G11 grade scale)

Department classification uses a keyword-scoring approach:
  - Every department accumulates a score from keyword matches in the title
  - The dept with the highest score wins
  - job_function is used as a tiebreaker only when scores are close (gap < 30)
  - linkedin_headline adds additional evidence to the scoring text

Layer classification uses ordered regex rules (first match wins, G0 → G9).
Default layer is G10 (IC / analyst / specialist) when no rule matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# CANONICAL DEPARTMENT NAMES  (Global_Org_Hierarchy.xlsx)
# ─────────────────────────────────────────────────────────────────────────────

DEPT_BOARD = "Board of Management"
DEPT_EXEC  = "Executive Management"
DEPT_FIN   = "Finance & Accounting"
DEPT_HR    = "Human Resources"
DEPT_LRC   = "Legal, Risk & Compliance"
DEPT_IT    = "Information Technology"
DEPT_ENG   = "Engineering"
DEPT_RD    = "Research & Development"
DEPT_PM    = "Product Management"
DEPT_MKT   = "Marketing"
DEPT_SALES = "Sales & Business Development"
DEPT_CS    = "Customer Success & Service"
DEPT_OPS   = "Operations"
DEPT_STR   = "Strategy & Corporate Development"
DEPT_FAC   = "Facilities, Real Estate & Workplace"
DEPT_COMM  = "Corporate Communications & Public Affairs"
DEPT_SUS   = "Sustainability"

# Industry-specific canonical departments
DEPT_IB    = "Investment Banking"
DEPT_ST    = "Sales & Trading"
DEPT_WM    = "Wealth Management"
DEPT_IM    = "Investment Management"
DEPT_ACT   = "Actuarial"
DEPT_UW    = "Underwriting"
DEPT_CLM   = "Claims"
DEPT_SC    = "Supply Chain"
DEPT_MFG   = "Manufacturing"
DEPT_PRC   = "Procurement"


@dataclass
class TitleClassification:
    dept_primary:   str
    dept_secondary: str
    layer:          int
    confidence:     float   # 0.0–1.0
    method:         str     # evidence trail


# ─────────────────────────────────────────────────────────────────────────────
# LAYER CLASSIFICATION  (G0–G10, Global_Designation_Hierarchy.xlsx)
# ─────────────────────────────────────────────────────────────────────────────
# Rules checked in order; first match wins.
# All patterns are applied to the lowercased job_title.
_LAYER_RULES: list[tuple[int, list[str]]] = [

    # G0 — Board / Non-Executive
    (0, [
        r"non[- ]?executive\s+director",
        r"independent\s+(?:non[- ]?executive\s+)?director",
        r"independent\s+non[- ]?executive",
        r"supervisory\s+board",
        r"board\s+of\s+(?:directors?|trustees?)",
        r"board\s+(?:member|director|trustee)",
        r"non[- ]?executive\s+chair(?:man|woman|person)?",
        r"lead\s+independent\s+director",
        r"outside\s+director",
        r"external\s+director",
        r"\bned\b",
        r"^chairman$", r"^chairwoman$", r"^chairperson$", r"^chair$",
    ]),

    # G1 — C-Suite (CEO, CFO, CTO, COO, CMO, CHRO, CIO, CRO, CPO, CDO, CLO …)
    (1, [
        r"\bchief\s+executive\b",               r"\bceo\b",
        r"\bchief\s+financial\b",               r"\bcfo\b",
        r"\bchief\s+(?:technology|technical)\b",r"\bcto\b",
        r"\bchief\s+operating\b",               r"\bcoo\b",
        r"\bchief\s+marketing\b",               r"\bcmo\b",
        r"\bchief\s+(?:human\s+resources?|people|talent)\b", r"\bchro\b",
        r"\bchief\s+information\b",             r"\bcio\b",
        r"\bchief\s+revenue\b",
        r"\bchief\s+product\b",                 r"\bcpo\b",
        r"\bchief\s+data\b",                    r"\bcdo\b",
        r"\bchief\s+(?:legal|law)\b",           r"\bclo\b",
        r"\bchief\s+risk\b",                    r"\bcro\b",
        r"\bchief\s+commercial\b",
        r"\bchief\s+strategy\b",                r"\bcso\b",
        r"\bchief\s+(?:digital|transformation)\b",
        r"\bchief\s+analytics\b",
        r"\bchief\s+compliance\b",
        r"\bchief\s+(?:information\s+)?security\b", r"\bciso\b",
        r"\bchief\s+(?:growth|innovation|sustainability|diversity)\b",
        r"\bchief\s+(?:supply\s+chain|procurement|purchasing)\b",
        r"\bchief\s+(?:communications?|public\s+affairs)\b",
        r"\bchief\s+customer\b",
        r"\bchief\s+science\b",                 r"\bcso\b",
        # Founder / President / Managing Partner as standalone titles
        r"^(?:co-?)?founder(?:\s*&\s*(?:co-?)?(?:ceo|cto|coo))?$",
        r"^(?:group\s+)?president$",
        r"^owner$",
        r"^managing\s+partner$",
        r"^group\s+managing\s+director$",
    ]),

    # G2 — EVP / Executive Director
    (2, [
        r"executive\s+vice\s+president",
        r"\bevp\b",
        r"\bexecutive\s+director\b",
        r"\bgroup\s+(?:president|executive)\b",
        r"\bdivisional\s+managing\s+director\b",
        r"^managing\s+director$",   # standalone MD → EVP-equivalent (G2)
        r"^md$",
        r"regional\s+president",
    ]),

    # G3 — SVP / Senior VP
    (3, [
        r"senior\s+vice\s+president",
        r"\bsvp\b",
        r"\bgroup\s+vice\s+president\b",
    ]),

    # G4 — VP / Head of Function
    (4, [
        r"\bvice\s+president\b",
        r"\bvp\b",
        r"\bhead\s+of\b",
        r"^head,?\s",
    ]),

    # G5 — Senior Director / Associate VP
    (5, [
        r"senior\s+director",
        r"\bsr\.?\s+director\b",
        r"associate\s+vice\s+president",
        r"\bavp\b",
        r"associate\s+vp\b",
    ]),

    # G6 — Director
    (6, [
        r"\bdirector\b",
    ]),

    # G7 — Senior Manager / Associate Director / Team Lead
    (7, [
        r"senior\s+manager",
        r"\bsr\.?\s+manager\b",
        r"associate\s+director",
        r"\bteam\s+lead(?:er)?\b",
        r"\bchapter\s+lead\b",
        r"\bgroup\s+manager\b",
        r"\bprincipal\s+(?:consultant|advisor)\b",
    ]),

    # G8 — Manager / Supervisor
    (8, [
        r"\bmanager\b",
        r"\bsupervisor\b",
    ]),

    # G9 — Senior IC / Lead / Staff / Principal
    (9, [
        r"\bsenior\b",
        r"\bsr\.?\b",
        r"\blead\b",
        r"\bstaff\b",
        r"\bprincipal\b",
        r"\bsolution\s+architect\b",
        r"\benterprise\s+architect\b",
        r"\bsystem\s+architect\b",
        r"\btechnical\s+architect\b",
    ]),
    # G10 — IC / Analyst / Specialist — default (no rule needed)
]


# ─────────────────────────────────────────────────────────────────────────────
# INDUSTRY-SPECIFIC LAYER RULES
# ─────────────────────────────────────────────────────────────────────────────
# In Financial Markets / Banking the seniority ladder is inverted relative to
# most corporate functions: VP is mid-level, MD is a senior leader.
# These rules REPLACE the default _LAYER_RULES when the relevant industry
# is detected.
#
# Standard banking hierarchy (bottom → top):
#   Analyst → Associate → Vice President → Executive Director →
#   Director → Managing Director → Senior MD / Partner → C-Suite / CEO

_FINANCIAL_LAYER_RULES: list[tuple[int, list[str]]] = [
    # G0 — Board
    (0, [
        r"non[- ]?executive\s+director", r"independent\s+director",
        r"supervisory\s+board", r"board\s+(?:member|director|trustee)",
        r"\bned\b", r"^chair(?:man|woman|person)?$",
    ]),
    # G1 — CEO / President / Group Head
    (1, [
        r"\bchief\s+executive\b", r"\bceo\b", r"^(?:group\s+)?president$",
        r"\bchief\s+financial\b", r"\bcfo\b",
        r"\bchief\s+(?:operating|technology|risk|legal|compliance|commercial)\b",
        r"\bcoo\b", r"\bcto\b", r"\bcro\b",
        r"^(?:co-?)?founder$",
    ]),
    # G2 — Global Head / Senior Managing Director / Senior Partner
    (2, [
        r"global\s+head\s+of",
        r"senior\s+managing\s+director",
        r"\bgroup\s+managing\s+director\b",
        r"\bsenior\s+partner\b",
    ]),
    # G3 — Managing Director (MD) — most senior operating title in IB
    (3, [
        r"managing\s+director",
        r"^md$",
        r"\bhead\s+of\b",
        r"^head,?\s",
    ]),
    # G4 — Executive Director (ED) / Principal
    (4, [
        r"executive\s+director",
        r"\bed\b",
        r"\bprincipal\b",
    ]),
    # G5 — Director (Dir) — below ED in banking
    (5, [
        r"\bdirector\b",
        r"senior\s+vice\s+president",
        r"\bsvp\b",
    ]),
    # G6 — Vice President (VP) — mid-level in banking, NOT equivalent to corporate VP
    (6, [
        r"\bvice\s+president\b",
        r"\bvp\b",
    ]),
    # G7 — Senior Associate
    (7, [
        r"senior\s+associate",
        r"\bsenior\b",
        r"\bsr\.?\b",
    ]),
    # G8 — Associate (post-MBA entry in IB)
    (8, [
        r"\bassociate\b",
    ]),
    # G9 — Senior Analyst / Lead
    (9, [
        r"senior\s+analyst",
        r"\blead\b",
        r"\bstaff\b",
    ]),
    # G10 — Analyst (default)
]

# Industries where the financial-markets layer rules apply
_FINANCIAL_INDUSTRIES: frozenset[str] = frozenset({
    "Financial Markets / Capital Markets / Investments",
    "Retail Banking / Commercial Banking",
    "Life Insurance",
    "P&C Insurance",
    "Reinsurance",
    "Healthcare Insurance (Payers)",
})


# ─────────────────────────────────────────────────────────────────────────────
# INDUSTRY → DEPARTMENT SCORING BOOSTS
# ─────────────────────────────────────────────────────────────────────────────
# When the company's industry is known, add these extra points to specific
# departments. Used to resolve ties and push industry-primary depts to the top.

_INDUSTRY_DEPT_BOOSTS: dict[str, list[tuple[int, str]]] = {
    # Insurance industries → boost Underwriting, Claims, Actuarial
    "P&C Insurance": [
        (60, DEPT_UW), (60, DEPT_CLM), (50, DEPT_ACT), (40, DEPT_LRC),
    ],
    "Life Insurance": [
        (60, DEPT_UW), (60, DEPT_CLM), (50, DEPT_ACT),
    ],
    "Reinsurance": [
        (60, DEPT_UW), (60, DEPT_CLM), (50, DEPT_ACT),
    ],
    "Healthcare Insurance (Payers)": [
        (50, DEPT_UW), (50, DEPT_CLM), (40, DEPT_ACT), (30, DEPT_OPS),
    ],
    # Financial markets → boost IB, S&T, WM, IM
    "Financial Markets / Capital Markets / Investments": [
        (60, DEPT_IB), (60, DEPT_ST), (50, DEPT_WM), (50, DEPT_IM),
        (40, DEPT_LRC),
    ],
    "Retail Banking / Commercial Banking": [
        (50, DEPT_FIN), (40, DEPT_LRC), (30, DEPT_CS),
    ],
    # Technology industries → boost Engineering, IT, Product
    "Software": [
        (50, DEPT_ENG), (40, DEPT_PM), (30, DEPT_IT),
    ],
    "High Tech / Technology": [
        (40, DEPT_ENG), (40, DEPT_IT), (30, DEPT_PM),
    ],
    "IT Services": [
        (50, DEPT_IT), (30, DEPT_ENG), (20, DEPT_OPS),
    ],
    "IT Hardware": [
        (40, DEPT_ENG), (30, DEPT_IT), (20, DEPT_MFG),
    ],
    # Pharma / Life Sciences → boost R&D, Medical Affairs
    "Pharmaceuticals / Life Sciences": [
        (60, DEPT_RD), (30, DEPT_MFG), (20, DEPT_LRC),
    ],
    "Medical Devices": [
        (50, DEPT_RD), (40, DEPT_MFG), (30, DEPT_SALES),
    ],
    # Manufacturing → boost Manufacturing, Operations, Supply Chain
    "Automotive": [
        (50, DEPT_MFG), (40, DEPT_SC), (30, DEPT_ENG),
    ],
    "Industrial Manufacturing – Discrete": [
        (60, DEPT_MFG), (40, DEPT_ENG), (30, DEPT_SC),
    ],
    "Industrial Manufacturing – Process": [
        (60, DEPT_MFG), (50, DEPT_RD), (30, DEPT_SC),
    ],
    "Aerospace & Defence": [
        (50, DEPT_ENG), (40, DEPT_MFG), (30, DEPT_RD),
    ],
    # Logistics → boost Supply Chain
    "Supply Chain / Logistics": [
        (70, DEPT_SC), (30, DEPT_OPS),
    ],
    "Transportation": [
        (50, DEPT_OPS), (40, DEPT_SC),
    ],
    # Retail / Ecommerce → boost Sales, Marketing, Ops
    "Retail": [
        (40, DEPT_SALES), (40, DEPT_OPS), (30, DEPT_MKT),
    ],
    "Ecommerce": [
        (50, DEPT_SALES), (40, DEPT_MKT), (30, DEPT_IT),
    ],
    "Wholesale / Distribution": [
        (50, DEPT_SC), (40, DEPT_SALES), (30, DEPT_OPS),
    ],
    # Energy / Mining
    "Energy (Oil & Gas)": [
        (40, DEPT_ENG), (40, DEPT_OPS), (30, DEPT_RD),
    ],
    "Utilities": [
        (40, DEPT_OPS), (30, DEPT_ENG),
    ],
    "Mineral / Mining / Natural Resources": [
        (50, DEPT_OPS), (30, DEPT_ENG),
    ],
    # Healthcare providers → boost Operations, HR
    "Healthcare Providers": [
        (50, DEPT_OPS), (30, DEPT_HR),
    ],
    # Public sector
    "Public Sector & Government": [
        (40, DEPT_OPS), (30, DEPT_COMM),
    ],
    # Real estate
    "Real Estate": [
        (70, DEPT_FAC), (30, DEPT_FIN),
    ],
    # Media
    "Media & Entertainment": [
        (60, DEPT_MKT), (40, DEPT_COMM), (30, DEPT_ENG),
    ],
    # Telecom
    "Telecommunications": [
        (50, DEPT_ENG), (40, DEPT_IT), (30, DEPT_SALES),
    ],
    # Professional services
    "Business Services / Professional Services": [
        (40, DEPT_OPS), (30, DEPT_STR),
    ],
    # Construction
    "Construction": [
        (50, DEPT_OPS), (30, DEPT_ENG),
    ],
}


def _classify_layer(title: str, job_level: str = "", industry: str = "") -> int:
    """Return layer 0–10 for a job title (G0–G10 per reference).

    When *industry* is a financial-markets type, uses the banking-specific
    layer rules where VP = G6 (mid-level), MD = G3 (senior).
    """
    t = title.strip().lower()

    rules = (
        _FINANCIAL_LAYER_RULES
        if industry in _FINANCIAL_INDUSTRIES
        else _LAYER_RULES
    )

    for layer, patterns in rules:
        for pat in patterns:
            if re.search(pat, t):
                return layer

    # LinkedIn job_level as fallback when title is ambiguous
    _JL: dict[str, int] = {
        "entry level": 10, "entry-level": 10, "internship": 10,
        "associate":   9,
        "mid-senior level": 8, "mid-senior": 8,
        "director":    6,
        "vice president": 4,
        "c-suite":     1, "executive": 2,
    }
    if job_level:
        mapped = _JL.get(job_level.strip().lower())
        if mapped is not None:
            return mapped

    return 10  # default: IC / specialist


# ─────────────────────────────────────────────────────────────────────────────
# DEPARTMENT SCORING RULES
# ─────────────────────────────────────────────────────────────────────────────
# Each entry: (dept_name, [(score, keyword_phrase), ...])
# Keywords are matched case-insensitively against: job_title + " " + linkedin_headline
# The dept with the highest total score wins.
#
# Score calibration guide:
#   100   C-suite title (unambiguous dept)
#    90   Named compound title (2+ words, very specific)
#    85   Strong compound title or named functional role
#    80   Moderately specific role
#    75   Named function (one clear dept word)
#    60   Functional role word (moderate specificity)
#    50   Keyword that strongly implies this dept
#    40   Keyword that suggests this dept
#    30   Keyword with moderate specificity
#    20   Weak indicator
#
_DEPT_SCORE_RULES: list[tuple[str, list[tuple[int, str]]]] = [

    # ── Finance & Accounting ─────────────────────────────────────────────────
    (DEPT_FIN, [
        (100, "cfo"), (100, "chief financial officer"),
        (90,  "financial controller"), (90, "finance director"),
        (90,  "treasurer"), (90, "head of finance"),
        (90,  "vp finance"), (90, "vp of finance"),
        (90,  "fp&a"), (90, "financial planning and analysis"),
        (90,  "financial planning & analysis"),
        (85,  "group treasurer"), (85, "treasury director"),
        (85,  "treasury manager"), (85, "internal audit"),
        (85,  "internal auditor"), (85, "audit manager"),
        (85,  "tax director"), (85, "tax manager"),
        (85,  "investor relations"), (85, "financial reporting"),
        (85,  "financial analyst"), (85, "financial accountant"),
        (85,  "management accountant"), (85, "chief accountant"),
        (85,  "accounting manager"), (85, "payroll manager"),
        (85,  "payroll director"), (85, "group finance"),
        (85,  "corporate finance"), (85, "budget analyst"),
        (80,  "accountant"), (80, "auditor"), (80, "bookkeeper"),
        (80,  "tax consultant"), (80, "finance manager"),
        (80,  "finance analyst"), (80, "credit analyst"),
        (75,  "finance"), (65, "financial"), (55, "accounting"),
        (45,  "audit"), (40, "treasury"), (35, "tax"), (25, "payroll"),
    ]),

    # ── Human Resources ──────────────────────────────────────────────────────
    (DEPT_HR, [
        (100, "chro"), (100, "chief human resources officer"),
        (100, "chief people officer"),
        (90,  "head of hr"), (90, "hr director"),
        (90,  "head of people"), (90, "people director"),
        (90,  "human resources director"), (90, "human resources manager"),
        (90,  "talent acquisition director"), (90, "talent acquisition manager"),
        (90,  "head of talent"), (90, "head of recruitment"),
        (85,  "learning & development manager"), (85, "l&d manager"),
        (85,  "l&d director"), (85, "compensation & benefits manager"),
        (85,  "total rewards manager"), (85, "total rewards director"),
        (85,  "hrbp"), (85, "hr business partner"),
        (85,  "people partner"), (85, "people operations"),
        (85,  "workforce planning"), (85, "recruitment manager"),
        (85,  "recruitment director"), (85, "diversity & inclusion"),
        (85,  "dei manager"), (85, "employee relations"),
        (85,  "hr operations"), (85, "hr generalist"),
        (80,  "recruiter"), (80, "talent manager"), (80, "hr manager"),
        (75,  "hr analyst"), (75, "people analyst"),
        (70,  "human resources"), (60, "talent"),
        (50,  "hr"), (40, "people"), (30, "workforce"), (25, "recruitment"),
    ]),

    # ── Legal, Risk & Compliance ─────────────────────────────────────────────
    (DEPT_LRC, [
        (100, "general counsel"), (100, "chief legal officer"),
        (100, "clo"), (100, "chief compliance officer"),
        (90,  "legal director"), (90, "head of legal"),
        (90,  "legal counsel"), (90, "in-house counsel"),
        (90,  "solicitor"), (90, "attorney"),
        (90,  "compliance director"), (90, "head of compliance"),
        (90,  "compliance manager"), (90, "risk director"),
        (90,  "head of risk"), (90, "risk manager"),
        (90,  "chief risk officer"),
        (90,  "regulatory affairs director"), (90, "regulatory affairs manager"),
        (90,  "company secretary"), (90, "corporate secretary"),
        (85,  "credit risk"), (85, "market risk"),
        (85,  "operational risk"), (85, "enterprise risk manager"),
        (85,  "legal manager"), (85, "legal analyst"),
        (85,  "grc manager"), (85, "governance manager"),
        (85,  "data protection officer"), (85, "dpo"),
        (80,  "paralegal"), (80, "legal advisor"),
        (80,  "compliance analyst"), (80, "risk analyst"),
        (80,  "regulatory analyst"), (80, "governance analyst"),
        (75,  "legal"), (70, "compliance"), (65, "risk management"),
        (55,  "regulatory"), (45, "governance"), (35, "risk"),
    ]),

    # ── Information Technology ───────────────────────────────────────────────
    (DEPT_IT, [
        (100, "cio"), (100, "chief information officer"),
        (100, "ciso"), (100, "chief information security officer"),
        (90,  "it director"), (90, "head of it"),
        (90,  "it manager"), (90, "systems administrator"),
        (90,  "network administrator"), (90, "network engineer"),
        (90,  "infrastructure manager"), (90, "infrastructure director"),
        (90,  "cloud architect"), (90, "enterprise architect"),
        (90,  "information security manager"), (90, "cybersecurity manager"),
        (90,  "cybersecurity director"), (90, "security operations centre"),
        (90,  "soc manager"), (90, "it operations"),
        (90,  "it service delivery"), (90, "it support manager"),
        (90,  "data warehouse manager"), (90, "business intelligence manager"),
        (90,  "bi analyst"), (90, "bi developer"),
        (90,  "data architect"), (90, "database administrator"),
        (90,  "dba"), (90, "erp manager"), (90, "erp director"),
        (90,  "it project manager"), (90, "it programme manager"),
        (90,  "digital analytics manager"), (90, "analytics manager"),
        (85,  "systems analyst"), (85, "it analyst"),
        (85,  "network analyst"), (85, "security analyst"),
        (85,  "data analyst"), (85, "data engineer"),
        (85,  "data scientist"), (85, "machine learning engineer"),
        (85,  "ai engineer"), (85, "cloud engineer"),
        (80,  "helpdesk manager"), (80, "service desk manager"),
        (80,  "desktop support manager"), (80, "it support"),
        (75,  "information technology"), (70, "it infrastructure"),
        (60,  "cybersecurity"), (60, "information security"),
        (50,  "data science"), (50, "analytics"),
        (45,  "data"), (40, "technology"), (30, "digital"), (20, "it"),
    ]),

    # ── Engineering ──────────────────────────────────────────────────────────
    (DEPT_ENG, [
        (100, "cto"), (100, "chief technology officer"),
        (100, "chief technical officer"),
        (90,  "vp engineering"), (90, "vp of engineering"),
        (90,  "head of engineering"), (90, "engineering director"),
        (90,  "director of engineering"),
        (90,  "software engineer"), (90, "software developer"),
        (90,  "full stack developer"), (90, "frontend developer"),
        (90,  "backend developer"), (90,  "mobile developer"),
        (90,  "ios developer"), (90, "android developer"),
        (90,  "devops engineer"), (90, "site reliability engineer"),
        (90,  "sre"), (90, "platform engineer"),
        (90,  "qa engineer"), (90, "quality assurance engineer"),
        (90,  "test engineer"), (90, "automation engineer"),
        (90,  "firmware engineer"), (90, "embedded engineer"),
        (90,  "hardware engineer"), (90, "systems engineer"),
        (85,  "software architect"), (85, "technical architect"),
        (85,  "solutions architect"), (85, "full stack engineer"),
        (85,  "frontend engineer"), (85, "backend engineer"),
        (85,  "principal engineer"), (85, "staff engineer"),
        (85,  "engineering manager"), (85, "developer"),
        (85,  "programmer"), (85, "build engineer"),
        (85,  "release engineer"), (85, "web developer"),
        (80,  "technical lead"), (80, "tech lead"),
        (75,  "software development"), (70, "engineering"),
        (55,  "engineer"), (40, "developer"),
    ]),

    # ── Research & Development ───────────────────────────────────────────────
    (DEPT_RD, [
        (100, "chief science officer"), (100, "chief research officer"),
        (90,  "research director"), (90, "head of research"),
        (90,  "r&d director"), (90, "r&d manager"), (90, "head of r&d"),
        (90,  "head of innovation"), (90, "innovation director"),
        (90,  "research scientist"), (90, "principal researcher"),
        (90,  "research fellow"), (90, "laboratory director"),
        (90,  "lab director"), (90, "clinical research manager"),
        (85,  "research engineer"), (85, "r&d engineer"),
        (85,  "innovation manager"), (85, "product scientist"),
        (80,  "scientist"), (80, "researcher"),
        (75,  "r&d"), (70, "research"), (60, "innovation"),
        (50,  "laboratory"), (45, "lab"), (35, "clinical"),
    ]),

    # ── Product Management ───────────────────────────────────────────────────
    (DEPT_PM, [
        (100, "chief product officer"), (100, "cpo"),
        (90,  "vp product"), (90, "vp of product"),
        (90,  "head of product"), (90, "product director"),
        (90,  "director of product"), (90, "product manager"),
        (90,  "product owner"), (90, "senior product manager"),
        (90,  "principal product manager"), (90, "group product manager"),
        (90,  "ux director"), (90, "ui director"),
        (90,  "head of design"), (90, "design director"),
        (90,  "ux manager"), (90, "product design director"),
        (85,  "product analyst"), (85, "product operations"),
        (85,  "ux researcher"), (85, "user researcher"),
        (85,  "ux designer"), (85, "ui designer"),
        (85,  "product designer"), (85, "design manager"),
        (80,  "interaction designer"), (80, "service designer"),
        (75,  "product management"), (65, "product owner"),
        (55,  "product"), (45, "ux"), (35, "ui"), (30, "design"),
    ]),

    # ── Marketing ────────────────────────────────────────────────────────────
    (DEPT_MKT, [
        (100, "cmo"), (100, "chief marketing officer"),
        (90,  "marketing director"), (90, "head of marketing"),
        (90,  "vp marketing"), (90, "vp of marketing"),
        (90,  "brand director"), (90, "head of brand"),
        (90,  "digital marketing director"), (90, "digital marketing manager"),
        (90,  "performance marketing director"),
        (90,  "performance marketing manager"),
        (90,  "content director"), (90, "head of content"),
        (90,  "seo manager"), (90, "sem manager"),
        (90,  "social media manager"), (90, "social media director"),
        (90,  "market research manager"), (90, "consumer insights manager"),
        (90,  "trade marketing manager"), (90, "events manager"),
        (85,  "brand manager"), (85, "marketing manager"),
        (85,  "digital marketer"), (85, "growth marketer"),
        (85,  "email marketing manager"), (85, "crm manager"),
        (85,  "demand generation manager"), (85, "lead generation manager"),
        (80,  "copywriter"), (80, "content creator"), (80, "content manager"),
        (80,  "creative director"), (80, "art director"),
        (80,  "marketing analyst"), (80, "brand analyst"),
        (75,  "marketing"), (65, "brand"),
        (50,  "digital marketing"), (40, "content"),
        (30,  "growth"),
    ]),

    # ── Sales & Business Development ────────────────────────────────────────
    (DEPT_SALES, [
        (100, "chief revenue officer"),
        (90,  "vp sales"), (90, "vp of sales"),
        (90,  "sales director"), (90, "head of sales"),
        (90,  "business development director"), (90, "bd director"),
        (90,  "business development manager"), (90, "bdm"),
        (90,  "account executive"), (90, "account manager"),
        (90,  "enterprise account executive"),
        (90,  "regional sales manager"), (90, "territory manager"),
        (90,  "inside sales manager"), (90, "field sales manager"),
        (90,  "sales operations manager"), (90, "revenue operations manager"),
        (90,  "channel manager"), (90, "partner manager"),
        (90,  "alliances manager"), (90, "partnerships director"),
        (90,  "bdr"), (90, "sdr"),
        (90,  "business development representative"),
        (90,  "sales development representative"),
        (85,  "commercial director"), (85, "commercial manager"),
        (85,  "key account manager"), (85, "strategic account manager"),
        (85,  "new business manager"), (85, "sales engineer"),
        (85,  "pre-sales manager"), (85, "presales manager"),
        (80,  "sales manager"), (80, "sales analyst"),
        (75,  "sales"), (65, "business development"),
        (55,  "commercial"), (45, "revenue"),
        (35,  "partnerships"), (30, "channel"),
    ]),

    # ── Customer Success & Service ───────────────────────────────────────────
    (DEPT_CS, [
        (100, "chief customer officer"),
        (90,  "head of customer success"), (90, "vp customer success"),
        (90,  "customer success director"), (90, "head of customer support"),
        (90,  "customer support director"), (90, "customer experience director"),
        (90,  "head of cx"), (90, "customer success manager"),
        (90,  "csm"), (90, "client success manager"),
        (90,  "client success director"),
        (85,  "customer support manager"), (85, "customer service manager"),
        (85,  "technical support manager"), (85, "service delivery manager"),
        (85,  "customer onboarding manager"), (85, "implementation manager"),
        (80,  "customer success specialist"), (80, "support engineer"),
        (80,  "technical support engineer"), (80, "helpdesk manager"),
        (75,  "customer success"), (75, "client success"),
        (70,  "customer support"), (65, "customer service"),
        (55,  "customer experience"), (50, "cx"),
        (45,  "technical support"), (35, "support"),
        (25,  "onboarding"),
    ]),

    # ── Operations ───────────────────────────────────────────────────────────
    (DEPT_OPS, [
        (100, "coo"), (100, "chief operating officer"),
        (90,  "operations director"), (90, "head of operations"),
        (90,  "vp operations"), (90, "vp of operations"),
        (90,  "supply chain director"), (90, "head of supply chain"),
        (90,  "logistics director"), (90, "head of logistics"),
        (90,  "manufacturing director"), (90, "head of manufacturing"),
        (90,  "plant manager"), (90, "factory manager"),
        (90,  "production manager"), (90, "production director"),
        (90,  "quality assurance director"), (90, "head of quality"),
        (90,  "ehs director"), (90, "hse director"),
        (90,  "health & safety director"), (90, "head of procurement"),
        (90,  "procurement director"),
        (85,  "operations manager"), (85, "supply chain manager"),
        (85,  "logistics manager"), (85, "warehouse manager"),
        (85,  "distribution manager"), (85, "manufacturing manager"),
        (85,  "quality assurance manager"), (85, "lean manager"),
        (85,  "continuous improvement manager"), (85, "procurement manager"),
        (80,  "supply chain analyst"), (80, "logistics coordinator"),
        (80,  "operations analyst"), (80, "purchasing manager"),
        (75,  "operations"), (65, "supply chain"),
        (55,  "logistics"), (50, "manufacturing"),
        (45,  "procurement"), (40, "production"),
        (35,  "quality assurance"), (30, "warehouse"),
        (25,  "distribution"),
    ]),

    # ── Strategy & Corporate Development ────────────────────────────────────
    (DEPT_STR, [
        (100, "chief strategy officer"),
        (90,  "strategy director"), (90, "head of strategy"),
        (90,  "vp strategy"), (90, "vp of strategy"),
        (90,  "corporate development director"),
        (90,  "head of corporate development"),
        (90,  "m&a director"), (90, "head of m&a"),
        (90,  "transformation director"), (90, "head of transformation"),
        (90,  "pmo director"), (90, "head of pmo"),
        (90,  "programme director"), (90, "program director"),
        (85,  "strategy manager"), (85, "corporate development manager"),
        (85,  "m&a manager"), (85, "strategic planning manager"),
        (85,  "transformation manager"), (85, "change director"),
        (85,  "change management director"),
        (80,  "strategy analyst"), (80, "corporate development analyst"),
        (80,  "m&a analyst"), (80, "strategic analyst"),
        (75,  "corporate strategy"), (70, "group strategy"),
        (65,  "strategy"), (60, "strategic"),
        (55,  "corporate development"), (50, "transformation"),
        (45,  "change management"), (40, "pmo"),
        (35,  "programme management"), (30, "m&a"),
    ]),

    # ── Facilities, Real Estate & Workplace ─────────────────────────────────
    (DEPT_FAC, [
        (100, "head of facilities"), (100, "facilities director"),
        (90,  "real estate director"), (90, "head of real estate"),
        (90,  "workplace director"), (90, "head of workplace"),
        (90,  "facilities manager"), (90, "property manager"),
        (90,  "real estate manager"), (90, "workplace manager"),
        (90,  "corporate real estate manager"), (90, "building manager"),
        (90,  "estate manager"), (90, "office services manager"),
        (85,  "facilities coordinator"), (85, "facilities officer"),
        (85,  "workplace services manager"), (85, "workplace coordinator"),
        (80,  "property director"), (75, "facilities management"),
        (70,  "real estate"), (65, "facilities"),
        (60,  "workplace"), (50, "building services"),
        (40,  "office services"),
    ]),

    # ── Corporate Communications & Public Affairs ────────────────────────────
    (DEPT_COMM, [
        (100, "chief communications officer"),
        (90,  "communications director"), (90, "head of communications"),
        (90,  "vp communications"), (90, "public affairs director"),
        (90,  "head of public affairs"), (90, "government relations director"),
        (90,  "head of government relations"),
        (90,  "media relations director"), (90, "head of media relations"),
        (90,  "pr director"), (90, "head of pr"),
        (90,  "corporate communications director"),
        (90,  "corporate communications manager"),
        (85,  "public affairs manager"), (85, "government relations manager"),
        (85,  "external affairs manager"), (85, "external affairs director"),
        (85,  "press officer"), (85, "press secretary"),
        (85,  "internal communications manager"),
        (80,  "pr manager"), (80, "public relations manager"),
        (75,  "public relations"), (70, "corporate communications"),
        (65,  "communications"), (60, "public affairs"),
        (55,  "government relations"), (50, "external affairs"),
        (40,  "media relations"), (35, "pr"),
    ]),

    # ── Sustainability / ESG ─────────────────────────────────────────────────
    (DEPT_SUS, [
        (100, "chief sustainability officer"),
        (90,  "sustainability director"), (90, "head of sustainability"),
        (90,  "esg director"), (90, "head of esg"),
        (90,  "csr director"), (90, "head of csr"),
        (85,  "sustainability manager"), (85, "esg manager"),
        (80,  "sustainability analyst"), (80, "esg analyst"),
        (75,  "sustainability"), (70, "esg"),
        (60,  "csr"), (50, "environmental"),
        (40,  "net zero"), (35, "climate"),
        (30,  "carbon"),
    ]),

    # ── Financial Services: Investment Banking ───────────────────────────────
    (DEPT_IB, [
        (100, "investment banker"), (100, "managing director investment banking"),
        (90,  "head of investment banking"), (90, "m&a advisor"),
        (90,  "capital markets director"), (90, "head of capital markets"),
        (90,  "dcm"), (90, "ecm"), (90, "equity capital markets"),
        (90,  "debt capital markets"), (90, "leveraged finance"),
        (85,  "investment banking analyst"), (85, "investment banking associate"),
        (80,  "capital markets analyst"), (80, "m&a analyst"),
        (75,  "investment banking"), (70, "capital markets"),
        (65,  "m&a advisory"),
    ]),

    # ── Financial Services: Sales & Trading ─────────────────────────────────
    (DEPT_ST, [
        (100, "trader"), (100, "quantitative trader"), (100, "quant trader"),
        (90,  "head of trading"), (90, "trading director"),
        (90,  "fixed income trader"), (90, "equity trader"),
        (90,  "fx trader"), (90, "derivatives trader"),
        (90,  "prime brokerage manager"), (90, "market maker"),
        (90,  "quantitative analyst"), (90, "quant"),
        (85,  "trading analyst"), (85, "fixed income analyst"),
        (80,  "equities analyst"), (80, "equity research analyst"),
        (75,  "trading"), (70, "fixed income"),
        (65,  "equities"), (60, "derivatives"),
        (55,  "prime brokerage"), (50, "institutional securities"),
    ]),

    # ── Financial Services: Wealth Management ───────────────────────────────
    (DEPT_WM, [
        (100, "wealth manager"), (100, "private banker"),
        (90,  "wealth advisor"), (90, "head of wealth management"),
        (90,  "private banking director"), (90, "private wealth manager"),
        (90,  "family office director"), (90, "private client manager"),
        (85,  "wealth management associate"), (85, "wealth analyst"),
        (75,  "wealth management"), (70, "private banking"),
        (65,  "private wealth"), (60, "private client"),
        (50,  "family office"),
    ]),

    # ── Financial Services: Investment Management ────────────────────────────
    (DEPT_IM, [
        (100, "portfolio manager"), (100, "fund manager"),
        (100, "chief investment officer"),
        (90,  "head of asset management"), (90, "investment manager"),
        (90,  "portfolio director"),
        (85,  "investment analyst"), (85, "portfolio analyst"),
        (80,  "asset management analyst"), (80, "fund analyst"),
        (75,  "asset management"), (70, "investment management"),
        (65,  "portfolio management"), (60, "fund management"),
    ]),

    # ── Insurance: Actuarial ─────────────────────────────────────────────────
    (DEPT_ACT, [
        (100, "chief actuary"), (100, "actuarial director"),
        (90,  "head of actuarial"), (90, "actuary"),
        (85,  "actuarial manager"), (85, "actuarial analyst"),
        (80,  "pricing actuary"), (80, "reserving actuary"),
        (80,  "catastrophe modeler"), (80, "cat modeler"),
        (75,  "actuarial"), (60, "actuary"),
    ]),

    # ── Insurance: Underwriting ──────────────────────────────────────────────
    (DEPT_UW, [
        (100, "chief underwriting officer"),
        (90,  "underwriting director"), (90, "head of underwriting"),
        (85,  "underwriting manager"), (85, "underwriter"),
        (80,  "treaty underwriter"), (80, "facultative underwriter"),
        (75,  "underwriting"), (60, "underwriter"),
    ]),

    # ── Insurance: Claims ────────────────────────────────────────────────────
    (DEPT_CLM, [
        (100, "head of claims"), (100, "claims director"),
        (90,  "claims manager"), (90, "claims adjuster"),
        (85,  "claims handler"), (85, "claims analyst"),
        (80,  "claims"), (65, "claim"),
    ]),
]


# ─────────────────────────────────────────────────────────────────────────────
# SUB-DEPARTMENT KEYWORDS
# ─────────────────────────────────────────────────────────────────────────────
# {dept_primary: [(sub_dept_label, [phrase_in_title_or_headline])]}
# Checked after primary dept is determined; first matching sub-dept wins.
_SUB_DEPT_KEYWORDS: dict[str, list[tuple[str, list[str]]]] = {

    DEPT_FIN: [
        ("FP&A",                          ["fp&a", "financial planning", "budgeting", "forecasting"]),
        ("Treasury",                      ["treasury", "treasurer", "cash management"]),
        ("Internal Audit",                ["internal audit", "auditor", "sox"]),
        ("Tax",                           ["tax"]),
        ("Control & Financial Reporting", ["financial reporting", "controller", "controlling", "general ledger"]),
        ("Investor Relations",            ["investor relations"]),
        ("Financial Operations",          ["payroll", "accounts payable", "accounts receivable"]),
    ],

    DEPT_HR: [
        ("Talent Acquisition",            ["talent acquisition", "recruiter", "recruitment", "sourcing"]),
        ("Learning & Development",        ["learning & development", "l&d", "training", "learning"]),
        ("Total Rewards",                 ["compensation", "benefits", "total rewards", "remuneration"]),
        ("HR Business Partners",          ["hrbp", "hr business partner", "people partner"]),
        ("People Operations & HR Systems",["people operations", "hr operations", "hr systems", "hris", "people analytics"]),
        ("Employee Relations & DE&I",     ["employee relations", "diversity", "inclusion", "dei", "wellbeing"]),
    ],

    DEPT_LRC: [
        ("Corporate Legal",               ["general counsel", "legal counsel", "in-house", "litigation", "contract"]),
        ("Regulatory & Compliance",       ["compliance", "regulatory", "regulation", "kyc", "aml", "gdpr"]),
        ("Risk Management",               ["risk", "credit risk", "market risk", "operational risk", "enterprise risk"]),
        ("Ethics & Governance",           ["governance", "ethics", "grc", "company secretary"]),
        ("Intellectual Property",         ["intellectual property", "ip", "patent", "trademark"]),
    ],

    DEPT_IT: [
        ("Infrastructure & Cloud",        ["infrastructure", "cloud", "network", "server", "aws", "azure", "gcp"]),
        ("Cybersecurity",                 ["cybersecurity", "cyber security", "information security", "infosec", "soc", "iam"]),
        ("Enterprise Applications",       ["erp", "sap", "oracle", "enterprise application", "crm"]),
        ("Data & Enterprise Analytics",   ["data analyst", "data engineer", "business intelligence", "bi", "data warehouse", "analytics", "data science", "machine learning"]),
        ("IT Support & Service Delivery", ["it support", "helpdesk", "service desk", "it service", "desktop support"]),
    ],

    DEPT_ENG: [
        ("Software Engineering",              ["software engineer", "software developer", "full stack", "frontend", "backend", "mobile", "ios", "android", "web developer"]),
        ("Platform & Infrastructure Engineering", ["devops", "sre", "site reliability", "platform engineer", "release engineer", "build engineer"]),
        ("Hardware & Systems Engineering",    ["hardware engineer", "firmware", "embedded", "systems engineer"]),
        ("Quality Engineering",               ["qa engineer", "quality engineer", "test engineer", "automation engineer"]),
    ],

    DEPT_RD: [
        ("Research",                      ["research scientist", "researcher", "research fellow", "principal scientist"]),
        ("Product Development",           ["product development", "product engineer"]),
        ("Innovation & IP Management",    ["innovation", "intellectual property", "patent manager"]),
    ],

    DEPT_PM: [
        ("Product Strategy",              ["product strategy", "product manager", "product owner", "product operations"]),
        ("Product Design (UX/UI)",        ["ux", "ui", "user experience", "user interface", "product design", "design"]),
        ("Technical Product Management",  ["technical product", "platform product"]),
    ],

    DEPT_MKT: [
        ("Brand & Creative",              ["brand", "creative", "content", "copywriter", "art director"]),
        ("Performance & Digital Marketing",["performance marketing", "digital marketing", "seo", "sem", "paid media", "social media", "email marketing"]),
        ("Product Marketing",             ["product marketing", "gtm", "go-to-market", "market research", "competitive"]),
        ("Field & Event Marketing",       ["events", "field marketing", "trade marketing"]),
    ],

    DEPT_SALES: [
        ("Direct Sales",                  ["account executive", "account manager", "enterprise sales", "field sales", "regional sales", "territory"]),
        ("Sales Operations",              ["sales operations", "revenue operations", "revops", "crm", "sales enablement"]),
        ("Partnerships & Alliances",      ["partnerships", "alliances", "channel", "partner manager", "business development"]),
    ],

    DEPT_CS: [
        ("Customer Support",              ["customer support", "customer service", "help desk", "helpdesk", "service desk", "technical support"]),
        ("Customer Success Management",   ["customer success", "client success", "csm", "onboarding", "renewals"]),
        ("Customer Experience",           ["customer experience", "cx", "customer journey"]),
    ],

    DEPT_OPS: [
        ("Supply Chain Management",       ["supply chain", "sourcing", "vendor management", "category management"]),
        ("Logistics & Distribution",      ["logistics", "warehouse", "distribution", "shipping", "fleet"]),
        ("Manufacturing & Production",    ["manufacturing", "production", "plant", "factory", "assembly"]),
        ("Quality Assurance & EHS",       ["quality assurance", "quality control", "qa", "ehs", "hse", "health & safety", "lean", "six sigma"]),
        ("Procurement",                   ["procurement", "purchasing", "buyer"]),
    ],

    DEPT_STR: [
        ("Corporate Strategy",            ["corporate strategy", "group strategy", "strategic planning", "business strategy"]),
        ("M&A & Corporate Development",   ["m&a", "corporate development", "mergers", "acquisitions", "deal"]),
        ("Digital Transformation & Innovation", ["transformation", "change management", "innovation", "digital transformation"]),
    ],

    DEPT_FAC: [
        ("Facilities Management",         ["facilities management", "facilities manager", "building", "building services"]),
        ("Real Estate & Workplace Strategy", ["real estate", "workplace", "property", "corporate real estate"]),
    ],

    DEPT_COMM: [
        ("Corporate Communications",      ["corporate communications", "internal communications", "pr", "public relations", "media relations"]),
        ("Public Affairs & Government Relations", ["public affairs", "government relations", "external affairs", "policy"]),
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# JOB_FUNCTION → CANONICAL DEPT  (soft hint map — used as tiebreaker only)
# ─────────────────────────────────────────────────────────────────────────────
_FUNCTION_HINT_MAP: dict[str, str] = {
    "finance": DEPT_FIN, "accounting": DEPT_FIN, "treasury": DEPT_FIN,
    "tax": DEPT_FIN, "audit": DEPT_FIN, "investor relations": DEPT_FIN,
    "human resources": DEPT_HR, "hr": DEPT_HR, "people": DEPT_HR,
    "people & culture": DEPT_HR, "talent": DEPT_HR,
    "legal": DEPT_LRC, "compliance": DEPT_LRC, "regulatory": DEPT_LRC,
    "regulatory affairs": DEPT_LRC, "risk": DEPT_LRC,
    "information technology": DEPT_IT, "it": DEPT_IT,
    "technology": DEPT_IT, "data": DEPT_IT, "analytics": DEPT_IT,
    "cybersecurity": DEPT_IT, "information security": DEPT_IT,
    "engineering": DEPT_ENG, "software": DEPT_ENG,
    "software engineering": DEPT_ENG, "devops": DEPT_ENG,
    "research": DEPT_RD, "r&d": DEPT_RD, "innovation": DEPT_RD,
    "product": DEPT_PM, "product management": DEPT_PM, "design": DEPT_PM,
    "marketing": DEPT_MKT, "brand": DEPT_MKT, "content": DEPT_MKT,
    "communications": DEPT_COMM, "public relations": DEPT_COMM,
    "public affairs": DEPT_COMM, "government relations": DEPT_COMM,
    "sales": DEPT_SALES, "business development": DEPT_SALES,
    "partnerships": DEPT_SALES, "revenue": DEPT_SALES,
    "customer service": DEPT_CS, "customer success": DEPT_CS,
    "customer support": DEPT_CS, "customer experience": DEPT_CS,
    "operations": DEPT_OPS, "supply chain": DEPT_OPS,
    "logistics": DEPT_OPS, "manufacturing": DEPT_MFG,
    "production": DEPT_MFG, "procurement": DEPT_PRC,
    "strategy": DEPT_STR, "consulting": DEPT_STR,
    "corporate development": DEPT_STR, "transformation": DEPT_STR,
    "facilities": DEPT_FAC, "real estate": DEPT_FAC, "workplace": DEPT_FAC,
    "sustainability": DEPT_SUS, "esg": DEPT_SUS, "csr": DEPT_SUS,
    "investment banking": DEPT_IB, "capital markets": DEPT_IB,
    "trading": DEPT_ST, "fixed income": DEPT_ST, "equities": DEPT_ST,
    "wealth management": DEPT_WM, "private banking": DEPT_WM,
    "asset management": DEPT_IM, "investment management": DEPT_IM,
    "portfolio management": DEPT_IM,
    "actuarial": DEPT_ACT, "underwriting": DEPT_UW, "claims": DEPT_CLM,
}


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _score(text: str, rules: list[tuple[int, str]]) -> int:
    """Sum scores for all keyword matches in text."""
    total = 0
    for weight, kw in rules:
        if kw in text:
            total += weight
    return total


def _classify_dept(
    title: str,
    headline: str,
    job_function: str,
    industry: str = "",
) -> tuple[str, float, str]:
    """
    Score every department against the combined text of title + headline.
    Returns (dept_primary, confidence, method).

    When *industry* is known, industry-specific boosts are added to the
    department scores before ranking.  job_function is used as a tiebreaker
    when the gap between first and second place is < 30 points.
    """
    combined = (title + " " + headline).lower()

    scores: dict[str, int] = {
        dept: _score(combined, rules)
        for dept, rules in _DEPT_SCORE_RULES
    }

    # Apply industry-specific boosts (only when title scores are ambiguous
    # or the boosted dept is already competitive — prevents overriding
    # clear title signals)
    if industry:
        for boost, dept in _INDUSTRY_DEPT_BOOSTS.get(industry, []):
            if dept in scores:
                scores[dept] = scores[dept] + boost

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_dept,  top_score   = ranked[0]
    sec_dept,  sec_score   = ranked[1] if len(ranked) > 1 else ("", 0)

    if top_score == 0:
        hint = _FUNCTION_HINT_MAP.get((job_function or "").strip().lower())
        return (hint or DEPT_OPS, 0.3, "job_function_fallback")

    gap = top_score - sec_score
    if gap >= 60:
        conf, method = 0.95, "title_keyword_high"
    elif gap >= 30:
        conf, method = 0.80, "title_keyword"
    elif gap >= 10:
        conf, method = 0.65, "title_keyword_moderate"
    else:
        hint = _FUNCTION_HINT_MAP.get((job_function or "").strip().lower())
        if hint == top_dept:
            conf, method = 0.72, "title_keyword+job_function"
        elif hint == sec_dept and sec_score > 0:
            top_dept = sec_dept
            conf, method = 0.60, "job_function_tiebreak"
        else:
            conf, method = 0.55, "title_keyword_weak"

    return top_dept, conf, method


def _classify_sub_dept(dept_primary: str, title: str, headline: str) -> str:
    """Return the best matching sub-department label, or '' if none."""
    combined = (title + " " + headline).lower()
    for sub_dept, keywords in _SUB_DEPT_KEYWORDS.get(dept_primary, []):
        if any(kw in combined for kw in keywords):
            return sub_dept
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def classify(
    job_title: str,
    linkedin_headline: str = "",
    job_function:      str = "",
    job_level:         str = "",
    industry:          str = "",
) -> TitleClassification:
    """
    Classify a person record into a canonical department + layer.

    Parameters
    ----------
    job_title         Primary NLP signal — the person's current role title.
    linkedin_headline Secondary NLP signal — LinkedIn profile headline.
    job_function      LinkedIn job_function field — soft tiebreaker only.
    job_level         LinkedIn job_level string — layer fallback.
    industry          One of the 37 canonical industries from
                      Global_Org_Hierarchy.xlsx.  When provided:
                        - Selects industry-specific layer rules (e.g. banking:
                          VP = G6 not G4, MD = G3)
                        - Applies industry department-scoring boosts so that
                          industry-primary depts rank higher on ambiguous titles

    Returns
    -------
    TitleClassification with dept_primary, dept_secondary, layer, confidence.
    """
    title    = (job_title        or "").strip()
    headline = (linkedin_headline or "").strip()
    jf       = (job_function     or "").strip()
    jl       = (job_level        or "").strip()
    ind      = (industry         or "").strip()

    # Strip "at [Company]" from headline to avoid company name polluting scoring
    headline = re.sub(r"\s+at\s+.+$", "", headline, flags=re.IGNORECASE).strip()

    if not title and not headline:
        return TitleClassification(
            dept_primary=DEPT_OPS, dept_secondary="",
            layer=10, confidence=0.1, method="no_input",
        )

    effective_title = title or headline

    # ── Layer classification (industry-aware) ────────────────────────────
    layer = _classify_layer(effective_title, jl, ind)

    # Board members → always DEPT_BOARD regardless of industry
    if layer == 0:
        return TitleClassification(
            dept_primary=DEPT_BOARD, dept_secondary="",
            layer=0, confidence=1.0, method="board_pattern",
        )

    # ── Department classification (industry-aware) ───────────────────────
    dept, conf, method = _classify_dept(effective_title, headline, jf, ind)
    sub  = _classify_sub_dept(dept, effective_title, headline)

    return TitleClassification(
        dept_primary=dept,
        dept_secondary=sub,
        layer=layer,
        confidence=conf,
        method=method,
    )
