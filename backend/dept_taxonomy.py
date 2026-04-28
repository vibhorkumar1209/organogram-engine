"""
dept_taxonomy.py — Master 4-Level Department Reference Library
==============================================================
Sources:
  1. GTM Title Library  — 1,100 titles · 6 functions · 22 sub-functions · 12 seniority levels
  2. ProTrail OrgData   — 354,221 rows across JPMorgan, HSBC, Microsoft, Mastercard, …
  3. Enterprise hierarchy framework — 14 CXO-level functions, L1–L4 depth

Structure
---------
Each entry in TAXONOMY:
    {
        "l1":           str,           # CXO-level function name
        "l1_keywords":  list[str],     # keywords that signal this function
        "departments":  list[dict],    # L2 departments
    }

Each L2 department:
    {
        "name":         str,
        "keywords":     list[str],     # keywords → dept_primary
        "sub_depts":    list[dict],    # L3 sub-departments
    }

Each L3 sub-department:
    {
        "name":         str,
        "keywords":     list[str],     # keywords → dept_secondary
        "teams":        list[dict],    # L4 micro-teams (optional)
    }

Each L4 team:
    {
        "name":         str,
        "keywords":     list[str],     # keywords → dept_tertiary
    }

Seniority → Layer mapping (from GTM Title Library):
    Chief / Group CEO      → L1
    Global Head / EVP / MD → L2
    SVP / VP               → L3
    Senior Director / Head → L4
    Director               → L5
    Principal / Sr Manager → L6
    Manager / Lead         → L7
    Senior IC / Staff      → L8
    Analyst / Associate    → L9
    Graduate / Intern      → L10
"""

from __future__ import annotations

# ── Seniority → Layer map (GTM Title Library) ────────────────────────────────
GTM_SENIORITY_LAYERS: dict[str, int] = {
    "chief":          1,
    "global head":    2,
    "evp":            2,
    "executive vp":   2,
    "managing director": 2,
    "svp":            3,
    "vp":             3,
    "vice president": 3,
    "senior director":4,
    "head":           4,
    "director":       5,
    "principal":      6,
    "senior manager": 6,
    "manager":        7,
    "lead":           8,
    "senior analyst": 8,
    "analyst":        9,
    "associate":      9,
    "graduate":       10,
    "intern":         10,
    "trainee":        10,
}

# ── Full 4-Level Taxonomy ─────────────────────────────────────────────────────
TAXONOMY: list[dict] = [

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 1. CORPORATE / EXECUTIVE LEADERSHIP
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Corporate & Executive",
        "l1_keywords": [
            "executive management", "executive leadership", "c-suite", "c suite",
            "senior leadership", "executive committee", "exco", "group executive",
            "corporate leadership", "ceo office", "office of the ceo",
            "management board", "leadership team", "corporate governance",
        ],
        "departments": [
            {
                "name": "Executive Management",
                "keywords": [
                    "executive management", "executive committee", "exco", "group executive",
                    "management board", "executive leadership team", "senior leadership team",
                    "managing director", "executive director", "group managing director",
                    "divisional managing director",
                ],
                "sub_depts": [
                    {
                        "name": "C-Suite",
                        "keywords": [
                            # Role-agnostic executive keywords only.
                            # Function-specific chiefs (CHRO → HR, CMO → Marketing, CTO → IT, etc.)
                            # are indexed in their own L1 so they land in the right department.
                            "chief executive officer", "ceo",
                            "chief operating officer", "coo",
                            "group chief executive", "group ceo",
                            "managing director", "group managing director",
                            "executive chairman",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "President / EVP",
                        "keywords": [
                            "president", "executive vice president", "evp",
                            "group president", "global president", "divisional president",
                            "co-founder", "founder",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Managing Directors",
                        "keywords": [
                            "managing director", "group managing director",
                            "regional managing director", "country managing director",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "CEO Office",
                "keywords": [
                    "ceo office", "office of the ceo", "chief of staff", "strategic initiatives",
                    "executive office", "group strategy", "enterprise strategy",
                ],
                "sub_depts": [
                    {
                        "name": "Corporate Strategy",
                        "keywords": [
                            "corporate strategy", "group strategy", "strategic planning",
                            "enterprise strategy", "business strategy", "strategy office",
                            "strategy and transformation", "strategic initiatives",
                        ],
                        "teams": [
                            {"name": "Strategic Planning", "keywords": ["strategic planning", "long-range planning", "annual planning", "strategic roadmap"]},
                            {"name": "OKR & Performance", "keywords": ["okr", "kpi", "performance management corporate", "business performance", "goal setting corporate"]},
                            {"name": "Market Intelligence", "keywords": ["market intelligence", "competitive intelligence", "market research corporate", "macro research"]},
                        ],
                    },
                    {
                        "name": "Enterprise PMO",
                        "keywords": [
                            "enterprise pmo", "pmo", "program management office",
                            "portfolio governance", "project portfolio", "transformation office",
                            "program management", "portfolio management office",
                        ],
                        "teams": [
                            {"name": "Program Management", "keywords": ["program management", "programme management", "project management", "delivery management"]},
                            {"name": "Portfolio Governance", "keywords": ["portfolio governance", "project portfolio", "governance office", "epmo"]},
                        ],
                    },
                    {
                        "name": "Corporate Development",
                        "keywords": [
                            "corporate development", "m&a", "mergers and acquisitions",
                            "mergers & acquisitions", "strategic acquisitions", "inorganic growth",
                            "deal management", "post-merger integration", "pmi",
                        ],
                        "teams": [
                            {"name": "M&A", "keywords": ["m&a", "mergers", "acquisitions", "deal", "transaction", "takeover"]},
                            {"name": "Post-Merger Integration", "keywords": ["post-merger integration", "pmi", "integration management", "synergy realisation"]},
                        ],
                    },
                ],
            },
            {
                "name": "Board of Directors",
                "keywords": [
                    "board of directors", "board of director", "non-executive", "independent director",
                    "supervisory board", "board member", "board director", "board governance",
                    "chairman", "chairwoman", "chairperson", "board of trustees",
                    "advisory board", "board relations", "corporate governance board",
                ],
                "sub_depts": [
                    {
                        "name": "Non-Executive Directors",
                        "keywords": [
                            "non-executive director", "ned", "independent director",
                            "outside director", "lead independent director",
                            "independent non-executive", "supervisory board member",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Board Committees",
                        "keywords": [
                            "audit committee", "remuneration committee", "compensation committee",
                            "nominations committee", "risk committee", "governance committee",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 2. FINANCE (CFO Org)
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Finance",
        "l1_keywords": [
            "finance", "financial", "cfo", "accounting", "treasury",
            "tax", "fp&a", "fpa", "financial planning", "controller",
            "corporate finance", "investor relations", "audit", "revenue cycle",
            "controlling", "fiscal", "budgeting", "forecasting",
            # CFO abbreviation variants from GTM library
            "cfo", "finance director", "financial director",
            "group finance", "head of finance", "vp finance", "svp finance",
            "chief financial", "finance operations", "financial operations",
        ],
        "departments": [
            {
                "name": "Financial Planning & Analysis",
                "keywords": [
                    "fp&a", "fpa", "financial planning", "financial planning and analysis",
                    "financial planning & analysis", "corporate fp&a",
                    "finance operations", "financial operations",
                    "planning and analysis", "management reporting", "business finance",
                    "commercial finance", "finance business partner", "embedded finance",
                    # GTM library seniority patterns
                    "head fp&a", "vp fp&a", "svp fp&a", "director fp&a",
                    "head of fp&a", "vp of fp&a", "chief fp&a",
                ],
                "sub_depts": [
                    {
                        "name": "Budgeting",
                        "keywords": [
                            "budgeting", "annual budget", "opex budget", "capex budget",
                            "cost management", "budget planning", "budget control",
                            "budget cycle", "budget process",
                        ],
                        "teams": [
                            {"name": "Opex Planning", "keywords": ["opex", "operating expenditure", "cost base", "opex management"]},
                            {"name": "Capex Planning", "keywords": ["capex", "capital expenditure", "capital planning", "investment budget"]},
                        ],
                    },
                    {
                        "name": "Forecasting",
                        "keywords": [
                            "forecasting", "financial forecast", "rolling forecast",
                            "revenue forecast", "demand forecast", "sales forecast",
                            "projection", "outlook",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Scenario Modeling",
                        "keywords": [
                            "scenario modeling", "scenario analysis", "financial modeling",
                            "stress testing", "sensitivity analysis", "what-if analysis",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Management Reporting",
                        "keywords": [
                            "management reporting", "management accounts", "board reporting",
                            "executive reporting", "financial performance reporting",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Finance Business Partnering",
                        "keywords": [
                            "finance business partner", "fbp", "commercial finance",
                            "business partnering finance", "embedded finance partner",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Accounting",
                "keywords": [
                    "accounting", "accounts", "general ledger", "accounts payable",
                    "accounts receivable", "financial accounting", "corporate accounting",
                    "statutory accounting", "controller", "controllership",
                    # GTM library variants
                    "head accounting", "vp accounting", "chief accounting",
                    "head of accounting", "director accounting",
                ],
                "sub_depts": [
                    {
                        "name": "General Ledger",
                        "keywords": [
                            "general ledger", "gl", "chart of accounts",
                            "period close", "month-end close", "year-end close",
                            "journal entries", "reconciliation",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Accounts Payable",
                        "keywords": [
                            "accounts payable", "ap", "vendor payments",
                            "invoice processing", "purchase-to-pay", "p2p",
                            "procure-to-pay", "payables",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Accounts Receivable",
                        "keywords": [
                            "accounts receivable", "ar", "collections",
                            "credit management", "order-to-cash", "o2c",
                            "billing", "receivables", "debtors",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Financial Reporting",
                        "keywords": [
                            "financial reporting", "statutory reporting", "ifrs", "gaap",
                            "external reporting", "disclosure", "financial statements",
                            "regulatory reporting finance", "annual accounts",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Revenue Accounting",
                        "keywords": [
                            "revenue accounting", "revenue recognition", "rev rec",
                            "asc 606", "revenue assurance", "billing revenue",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Treasury",
                "keywords": [
                    "treasury", "group treasury", "corporate treasury",
                    "cash management", "liquidity", "capital markets treasury",
                    "banking relations", "funding", "asset liability",
                    "forex", "fx treasury",
                    # GTM library variants
                    "head treasury", "vp treasury", "chief treasury",
                    "head of treasury", "director treasury",
                ],
                "sub_depts": [
                    {
                        "name": "Cash Management",
                        "keywords": [
                            "cash management", "cash flow", "working capital",
                            "liquidity management", "bank reconciliation", "cash pooling",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Capital Markets",
                        "keywords": [
                            "capital markets treasury", "debt capital", "equity capital",
                            "bond issuance", "fundraising", "debt management",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Banking Relations",
                        "keywords": [
                            "banking relations", "bank management", "credit facilities",
                            "revolving credit", "loan management", "bank covenants",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Forex & Hedging",
                        "keywords": [
                            "forex", "fx", "foreign exchange", "hedging", "currency risk",
                            "interest rate risk", "derivatives treasury", "swaps",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Tax",
                "keywords": [
                    "tax", "taxation", "direct tax", "indirect tax", "vat", "gst",
                    "transfer pricing", "tax compliance", "tax planning",
                    "tax advisory", "global tax", "corporate tax",
                ],
                "sub_depts": [
                    {
                        "name": "Direct Tax",
                        "keywords": [
                            "direct tax", "corporate tax", "income tax",
                            "deferred tax", "tax provision", "tax filings",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Indirect Tax",
                        "keywords": [
                            "indirect tax", "vat", "gst", "sales tax",
                            "customs duty", "excise tax", "consumption tax",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Transfer Pricing",
                        "keywords": [
                            "transfer pricing", "intercompany pricing",
                            "arm's length", "beps", "country-by-country reporting",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Tax Compliance",
                        "keywords": [
                            "tax compliance", "tax returns", "tax operations",
                            "tax reporting", "tax risk",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Controlling",
                "keywords": [
                    "controlling", "financial control", "cost control",
                    "management control", "financial controlling",
                    "business control", "finance control",
                    # GTM library variants
                    "head controlling", "vp controlling", "chief controlling",
                    "head of controlling", "director controlling",
                ],
                "sub_depts": [
                    {
                        "name": "Cost Controlling",
                        "keywords": [
                            "cost control", "cost management", "cost accounting",
                            "variance analysis", "standard costing", "cost base",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Profitability Analysis",
                        "keywords": [
                            "profitability analysis", "margin analysis",
                            "product profitability", "segment reporting",
                            "contribution margin",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Internal Audit",
                "keywords": [
                    "internal audit", "audit", "sox", "internal controls",
                    "assurance", "risk assurance", "compliance audit",
                    "control framework", "icfr",
                ],
                "sub_depts": [
                    {
                        "name": "SOX & Controls",
                        "keywords": [
                            "sox", "sarbanes-oxley", "internal controls", "icfr",
                            "control testing", "control framework", "control assurance",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Operational Audit",
                        "keywords": [
                            "operational audit", "process audit", "it audit",
                            "performance audit", "value for money audit",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Investor Relations",
                "keywords": [
                    "investor relations", "ir", "shareholders", "analyst relations",
                    "investor communications", "capital markets communications",
                    "equity story", "roadshow", "earnings",
                ],
                "sub_depts": [
                    {
                        "name": "Investor Communications",
                        "keywords": [
                            "investor communications", "earnings call", "annual report",
                            "investor day", "roadshow", "shareholder engagement",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "ESG Reporting",
                        "keywords": [
                            "esg reporting", "sustainability reporting",
                            "non-financial reporting", "integrated reporting",
                            "disclosure esg",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 3. HUMAN RESOURCES (CHRO Org)
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Human Resources",
        "l1_keywords": [
            "human resources", "hr", "people", "talent", "workforce",
            "chro", "people operations", "hr operations", "hr ops",
            "people & culture", "people and culture", "people ops",
            "talent management", "talent acquisition", "learning", "recruiting",
            "compensation", "benefits", "employee relations", "organisational development",
            # GTM library function keyword
            "people business",
        ],
        "departments": [
            {
                "name": "Talent Acquisition",
                "keywords": [
                    "talent acquisition", "recruiting", "recruitment", "talent sourcing",
                    "talent attraction", "global talent acquisition", "hiring",
                    "campus hiring", "lateral hiring", "executive search",
                    "resourcing", "talent resourcing",
                    # GTM library variants
                    "head talent acquisition", "vp talent acquisition", "chief talent acquisition",
                    "director talent acquisition",
                ],
                "sub_depts": [
                    {
                        "name": "Campus Hiring",
                        "keywords": [
                            "campus hiring", "campus recruitment", "graduate recruitment",
                            "university hiring", "early careers", "graduate scheme",
                            "apprentice recruitment", "intern recruitment",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Lateral Hiring",
                        "keywords": [
                            "lateral hiring", "lateral recruitment", "experienced hire",
                            "mid-career hiring", "professional hiring",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Executive Search",
                        "keywords": [
                            "executive search", "executive recruitment", "c-suite hiring",
                            "leadership hiring", "retained search", "headhunting",
                            "executive hiring",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Employer Branding",
                        "keywords": [
                            "employer branding", "talent brand", "employee value proposition",
                            "evp employer", "recruitment marketing", "careers site",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Learning & Development",
                "keywords": [
                    "learning and development", "l&d", "learning & development",
                    "training", "organisational learning", "capability building",
                    "skills development", "corporate academy", "learning operations",
                    "talent development", "people development",
                    # GTM library variants
                    "head l&d", "vp l&d", "chief l&d", "director l&d",
                    "head of l&d", "head of learning", "vp learning",
                ],
                "sub_depts": [
                    {
                        "name": "Technical Training",
                        "keywords": [
                            "technical training", "skills training", "product training",
                            "certification", "upskilling", "reskilling",
                            "functional training",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Leadership Development",
                        "keywords": [
                            "leadership development", "leadership program",
                            "management training", "leadership academy",
                            "executive education", "management development",
                            "high potential program", "hipo",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Learning Technology",
                        "keywords": [
                            "lms", "learning management system", "e-learning",
                            "digital learning", "learning platform", "learning technology",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Talent Management",
                        "keywords": [
                            "talent management", "performance management", "succession planning",
                            "career development", "9-box", "talent review", "hipo",
                        ],
                        "teams": [
                            {"name": "Performance Management", "keywords": ["performance management", "performance review", "appraisal", "performance cycle"]},
                            {"name": "Succession Planning", "keywords": ["succession planning", "succession management", "leadership pipeline", "bench strength"]},
                        ],
                    },
                ],
            },
            {
                "name": "Compensation & Benefits",
                "keywords": [
                    "compensation", "benefits", "total rewards", "payroll",
                    "rewards", "c&b", "compensation and benefits", "remuneration",
                    "reward strategy", "pay", "salary",
                    # GTM library variants
                    "head compensation", "vp compensation", "chief compensation",
                    "director compensation",
                ],
                "sub_depts": [
                    {
                        "name": "Payroll",
                        "keywords": [
                            "payroll", "payroll processing", "payroll operations",
                            "salary processing", "payroll compliance", "payroll tax",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Rewards Strategy",
                        "keywords": [
                            "rewards strategy", "total rewards", "compensation strategy",
                            "job architecture", "grading", "salary bands",
                            "pay equity", "benchmarking",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Benefits",
                        "keywords": [
                            "benefits", "employee benefits", "health benefits",
                            "retirement", "pension", "insurance benefits", "wellness",
                            "flexible benefits", "perks",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Equity & Long-term Incentives",
                        "keywords": [
                            "equity", "stock options", "rsu", "esop",
                            "long-term incentive", "ltip", "share plans",
                            "executive compensation", "deferred compensation",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "HR Business Partnering",
                "keywords": [
                    "hr business partner", "hrbp", "people business partner",
                    "strategic hr", "embedded hr", "hr generalist",
                ],
                "sub_depts": [],
            },
            {
                "name": "HR Operations",
                "keywords": [
                    "hr operations", "hr ops", "hris", "hr systems",
                    "people operations", "people ops", "workforce administration",
                    "hr services", "shared services hr", "employee lifecycle",
                    "hr service centre", "hr admin",
                ],
                "sub_depts": [
                    {
                        "name": "HRIS & Systems",
                        "keywords": [
                            "hris", "hr systems", "workday", "successfactors",
                            "peoplesoft", "hr technology", "hcm",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Employee Lifecycle",
                        "keywords": [
                            "employee lifecycle", "onboarding", "offboarding",
                            "employee administration", "hr admin", "employee records",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Employee Relations",
                "keywords": [
                    "employee relations", "labour relations", "labor relations",
                    "industrial relations", "employment relations",
                    "grievance", "disciplinary", "works council",
                    "diversity equity inclusion", "dei", "d&i", "wellbeing",
                ],
                "sub_depts": [
                    {
                        "name": "Labour Relations",
                        "keywords": [
                            "labour relations", "labor relations", "trade unions",
                            "collective bargaining", "industrial relations", "works council",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Wellbeing",
                        "keywords": [
                            "employee wellbeing", "mental health", "wellness",
                            "employee assistance", "eap", "occupational health",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "DEI",
                        "keywords": [
                            "diversity equity inclusion", "dei", "diversity and inclusion",
                            "d&i", "inclusion", "belonging", "edi",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 4. LEGAL, RISK & COMPLIANCE
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Legal, Risk & Compliance",
        "l1_keywords": [
            "legal", "compliance", "risk", "general counsel", "regulatory",
            "ethics", "governance legal", "company secretary", "corporate secretary",
            "grc", "risk management", "enterprise risk",
            "clo", "chief legal officer", "chief compliance officer",
            "chief risk officer", "general counsel",
        ],
        "departments": [
            {
                "name": "Legal",
                "keywords": [
                    "legal", "general counsel", "corporate legal", "legal counsel",
                    "legal affairs", "in-house legal", "legal operations", "legal advisory",
                    "company secretary", "corporate secretary", "secretariat",
                ],
                "sub_depts": [
                    {
                        "name": "Corporate Legal",
                        "keywords": [
                            "corporate legal", "corporate counsel", "corporate affairs legal",
                            "company secretary", "secretariat", "board secretariat",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Contracts Management",
                        "keywords": [
                            "contracts", "contract management", "contract negotiation",
                            "commercial contracts", "legal contracts", "agreements",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Litigation",
                        "keywords": [
                            "litigation", "disputes", "arbitration",
                            "dispute resolution", "court", "employment litigation",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Intellectual Property",
                        "keywords": [
                            "intellectual property", "ip", "patents", "trademarks",
                            "copyright", "trade secrets", "ip management",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Regulatory Affairs Legal",
                        "keywords": [
                            "regulatory affairs", "regulatory legal", "government affairs",
                            "public affairs legal", "policy legal",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Risk Management",
                "keywords": [
                    "risk management", "enterprise risk", "erm", "operational risk",
                    "risk framework", "credit risk", "market risk",
                    "liquidity risk", "risk governance", "chief risk",
                ],
                "sub_depts": [
                    {
                        "name": "Enterprise Risk",
                        "keywords": [
                            "enterprise risk", "erm", "risk governance",
                            "risk appetite", "strategic risk", "risk framework",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Operational Risk",
                        "keywords": [
                            "operational risk", "operational resilience",
                            "business continuity", "bcp", "disaster recovery", "bcm",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Credit Risk",
                        "keywords": [
                            "credit risk", "credit analysis", "credit underwriting",
                            "portfolio risk", "loan risk", "counterparty risk",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Market Risk",
                        "keywords": [
                            "market risk", "trading risk", "var",
                            "stress testing risk", "model risk",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Fraud & Financial Crime",
                        "keywords": [
                            "fraud", "financial crime", "anti-money laundering", "aml",
                            "kyc", "sanctions", "fraud prevention", "fraud detection",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Compliance",
                "keywords": [
                    "compliance", "regulatory compliance", "ethics",
                    "conduct", "grc", "financial crime compliance",
                    "compliance operations", "compliance officer",
                ],
                "sub_depts": [
                    {
                        "name": "Regulatory Compliance",
                        "keywords": [
                            "regulatory compliance", "regulatory management",
                            "regulatory change", "regulatory reporting compliance",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "AML & KYC",
                        "keywords": [
                            "aml", "kyc", "anti-money laundering", "know your customer",
                            "customer due diligence", "cdd", "sanctions screening",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Ethics & Conduct",
                        "keywords": [
                            "ethics", "conduct", "code of conduct",
                            "whistleblowing", "speak up", "investigations compliance",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Data Privacy",
                        "keywords": [
                            "data privacy", "gdpr", "data protection",
                            "privacy compliance", "dpo", "ccpa", "data governance privacy",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 5. OPERATIONS (COO Org)
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Operations",
        "l1_keywords": [
            "operations", "coo", "business operations", "global operations",
            "operational excellence", "process excellence", "lean", "six sigma",
            "service operations", "service delivery", "operational management",
        ],
        "departments": [
            {
                "name": "Service Operations",
                "keywords": [
                    "service operations", "service delivery", "operations management",
                    "business operations", "service management", "operational delivery",
                    "client operations", "global operations",
                ],
                "sub_depts": [
                    {
                        "name": "Service Delivery",
                        "keywords": [
                            "service delivery", "delivery management", "client delivery",
                            "service execution", "managed delivery",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Process Excellence",
                        "keywords": [
                            "process excellence", "lean", "six sigma",
                            "continuous improvement", "operational efficiency",
                            "bpm", "process improvement", "kaizen",
                        ],
                        "teams": [
                            {"name": "Lean / Six Sigma", "keywords": ["lean", "six sigma", "green belt", "black belt", "dmaic", "kaizen", "5s"]},
                            {"name": "Continuous Improvement", "keywords": ["continuous improvement", "ci", "operational efficiency", "process optimisation"]},
                        ],
                    },
                    {
                        "name": "Quality Assurance",
                        "keywords": [
                            "quality assurance", "qa", "quality management",
                            "quality control", "qc", "iso", "qms",
                            "quality operations",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Supply Chain",
                "keywords": [
                    "supply chain", "supply chain management", "scm",
                    "end-to-end supply chain", "supply chain operations",
                    "supply chain planning", "demand planning", "s&op",
                    # GTM library variants
                    "head supply chain", "vp supply chain", "chief supply chain",
                    "director supply chain", "head of supply chain",
                ],
                "sub_depts": [
                    {
                        "name": "Demand Planning",
                        "keywords": [
                            "demand planning", "s&op", "sales and operations planning",
                            "supply planning", "forecast accuracy", "ibp",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Inventory Management",
                        "keywords": [
                            "inventory management", "inventory control",
                            "stock management", "warehousing",
                            "inventory optimisation",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Logistics & Distribution",
                        "keywords": [
                            "logistics", "distribution", "transportation",
                            "freight", "last mile", "delivery logistics", "3pl",
                            # GTM library variants
                            "head logistics", "vp logistics", "chief logistics",
                            "director logistics",
                        ],
                        "teams": [
                            {"name": "Transportation", "keywords": ["transportation", "fleet", "freight", "carrier management", "shipping"]},
                            {"name": "Warehouse & Fulfillment", "keywords": ["warehouse", "warehousing", "fulfillment", "distribution centre", "dc management"]},
                        ],
                    },
                    {
                        "name": "Supplier Management",
                        "keywords": [
                            "supplier management", "vendor management supply",
                            "supplier relations", "supplier development",
                            "supply base management",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Procurement",
                "keywords": [
                    "procurement", "purchasing", "sourcing", "strategic sourcing",
                    "category management", "indirect procurement", "direct procurement",
                    "spend management", "procurement operations",
                    # GTM library variants
                    "head procurement", "vp procurement", "chief procurement",
                    "director procurement", "head of procurement",
                ],
                "sub_depts": [
                    {
                        "name": "Strategic Sourcing",
                        "keywords": [
                            "strategic sourcing", "category management",
                            "category strategy", "global sourcing",
                            "indirect procurement", "direct procurement",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Vendor Management Office",
                        "keywords": [
                            "vendor management", "vmo", "vendor office",
                            "supplier contracts", "third party management",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Spend Analytics",
                        "keywords": [
                            "spend analytics", "spend analysis", "procurement analytics",
                            "spend visibility", "cost reduction procurement",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Procure-to-Pay",
                        "keywords": [
                            "procure to pay", "p2p", "purchase to pay",
                            "purchase order", "invoice management",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Customer Service",
                "keywords": [
                    "customer service", "customer support", "customer care",
                    "customer operations", "client services",
                    "contact centre", "contact center", "call centre", "call center",
                    "customer experience", "cx",
                    "customer success",
                ],
                "sub_depts": [
                    {
                        "name": "Customer Support",
                        "keywords": [
                            "customer support", "tier 1 support", "technical support",
                            "help desk customer", "support operations",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Contact Centre",
                        "keywords": [
                            "contact centre", "contact center", "call centre", "call center",
                            "inbound", "outbound", "voice operations",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Customer Experience",
                        "keywords": [
                            "customer experience", "cx", "voice of customer", "voc",
                            "nps", "customer loyalty", "cx analytics",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Customer Success",
                        "keywords": [
                            "customer success", "csm", "client success",
                            "client onboarding", "customer onboarding",
                            "customer retention operations",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Manufacturing Operations",
                "keywords": [
                    "manufacturing", "plant operations", "production", "factory",
                    "assembly", "industrial operations", "lean manufacturing",
                    "shop floor", "plant management",
                ],
                "sub_depts": [
                    {
                        "name": "Plant Operations",
                        "keywords": [
                            "plant", "factory", "assembly line", "production line",
                            "shop floor", "plant management",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Maintenance",
                        "keywords": [
                            "maintenance", "reliability", "predictive maintenance",
                            "preventive maintenance", "asset care",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Industrial Engineering",
                        "keywords": [
                            "industrial engineering", "process engineering",
                            "manufacturing engineering", "methods engineering",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 6. INFORMATION TECHNOLOGY (CIO / CTO Org)
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Information Technology",
        "l1_keywords": [
            "information technology", "technology", "it", "cio", "cto",
            "digital", "software", "engineering it", "infrastructure", "cloud",
            "cybersecurity", "data", "enterprise applications", "platform",
            "erp", "sap", "devops", "ai", "digital transformation",
            "cdo", "chief digital officer", "chief technology officer",
            "chief information officer", "chief data officer",
            "ciso", "chief information security officer",
        ],
        "departments": [
            {
                "name": "Software Engineering",
                "keywords": [
                    "software engineering", "software development", "engineering",
                    "product engineering", "application development", "backend",
                    "frontend", "fullstack", "full stack", "platform engineering",
                    "devops", "sre", "site reliability",
                ],
                "sub_depts": [
                    {
                        "name": "Backend Engineering",
                        "keywords": [
                            "backend", "backend engineering", "server-side",
                            "api", "microservices", "distributed systems",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Frontend Engineering",
                        "keywords": [
                            "frontend", "frontend engineering", "ui engineering",
                            "web development", "react", "angular",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Platform & DevOps",
                        "keywords": [
                            "platform", "devops", "sre", "site reliability",
                            "ci/cd", "kubernetes", "docker",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Mobile Engineering",
                        "keywords": [
                            "mobile", "mobile engineering", "ios", "android",
                            "react native", "mobile app",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "QA Engineering",
                        "keywords": [
                            "qa engineering", "quality engineering", "test automation",
                            "testing", "sdet",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Cloud & Infrastructure",
                "keywords": [
                    "cloud", "infrastructure", "it infrastructure", "cloud infrastructure",
                    "data center", "networks", "networking", "server", "storage",
                    "cloud architecture", "cloud operations", "network operations",
                    # GTM library variants
                    "head cloud", "vp cloud", "chief cloud", "director cloud",
                ],
                "sub_depts": [
                    {
                        "name": "Cloud",
                        "keywords": [
                            "cloud", "aws", "azure", "gcp", "cloud architecture",
                            "cloud migration", "cloud operations", "saas ops",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Networks",
                        "keywords": [
                            "networks", "networking", "network infrastructure",
                            "wan", "lan", "network operations", "noc",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Data Centers",
                        "keywords": [
                            "data center", "data centre", "server", "storage",
                            "compute", "hardware infrastructure",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Cybersecurity",
                "keywords": [
                    "cybersecurity", "cyber security", "information security", "infosec",
                    "ciso", "security operations", "threat detection",
                    "identity access management", "iam security",
                    # GTM library variants
                    "head cybersecurity", "vp cybersecurity", "chief cybersecurity",
                    "director cybersecurity",
                ],
                "sub_depts": [
                    {
                        "name": "Security Operations",
                        "keywords": [
                            "soc", "security operations center", "threat detection",
                            "incident response", "cyber threat", "siem",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Identity & Access Management",
                        "keywords": [
                            "identity access management", "iam", "privileged access",
                            "pam", "sso", "zero trust", "mfa",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Vulnerability Management",
                        "keywords": [
                            "vulnerability management", "penetration testing",
                            "pen test", "red team", "patching",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Data Security",
                        "keywords": [
                            "data security", "dlp", "data loss prevention",
                            "encryption", "information protection",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Enterprise Applications",
                "keywords": [
                    "enterprise applications", "erp", "sap", "oracle", "crm",
                    "salesforce", "enterprise systems", "business applications",
                    "application management", "ebs", "dynamics",
                ],
                "sub_depts": [
                    {
                        "name": "ERP",
                        "keywords": [
                            "erp", "sap", "oracle erp", "dynamics", "s4hana",
                            "enterprise resource planning", "ebs",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "CRM Systems",
                        "keywords": [
                            "crm systems", "salesforce admin", "sfdc", "dynamics crm",
                            "crm implementation",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "HRIS",
                        "keywords": [
                            "hris", "workday", "successfactors", "hr technology hris",
                            "hcm platform",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "IT Operations",
                "keywords": [
                    "it operations", "it ops", "itsm", "service management",
                    "helpdesk", "service desk", "end user computing", "euc",
                    "deskside support", "it support",
                ],
                "sub_depts": [
                    {
                        "name": "Service Desk",
                        "keywords": [
                            "service desk", "helpdesk", "it helpdesk",
                            "1st line support", "tier 1 it",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "ITSM",
                        "keywords": [
                            "itsm", "itil", "service management it",
                            "change management it", "incident management it",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "End User Computing",
                        "keywords": [
                            "end user computing", "euc", "deskside",
                            "desktop support", "device management",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Data & Analytics",
                "keywords": [
                    "data", "analytics", "business intelligence", "data engineering",
                    "data science", "data platform", "data warehouse", "bi",
                    "insights", "data & analytics",
                    # GTM library variants
                    "head data science", "vp data science", "chief data science",
                    "head data engineering", "vp data engineering",
                ],
                "sub_depts": [
                    {
                        "name": "Data Engineering",
                        "keywords": [
                            "data engineering", "data platform", "data pipeline",
                            "etl", "data lake", "data warehouse", "data infrastructure",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Business Intelligence",
                        "keywords": [
                            "business intelligence", "bi", "reporting analytics",
                            "dashboards", "power bi", "tableau", "looker",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Data Science & ML",
                        "keywords": [
                            "data science", "machine learning", "ml",
                            "predictive analytics", "statistical modeling",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "AI & GenAI",
                        "keywords": [
                            "artificial intelligence", "ai", "generative ai", "genai",
                            "llm", "large language model", "applied ai", "nlp",
                            "computer vision",
                            # GTM library variants
                            "head ai", "vp ai", "chief ai", "director ai",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Digital Transformation",
                "keywords": [
                    "digital transformation", "digital", "digitisation",
                    "digitalisation", "digital strategy", "digital office",
                    "innovation digital",
                ],
                "sub_depts": [
                    {
                        "name": "Digital Strategy",
                        "keywords": [
                            "digital strategy", "digital roadmap",
                            "digitisation", "digital agenda",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Innovation",
                        "keywords": [
                            "innovation", "innovation lab", "emerging technology",
                            "incubation", "digital innovation",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Product Management",
                "keywords": [
                    "product management", "product", "product owner",
                    "product strategy", "product roadmap", "product development it",
                    "product operations", "ux", "user experience", "design product",
                ],
                "sub_depts": [
                    {
                        "name": "Product Strategy",
                        "keywords": [
                            "product strategy", "product vision",
                            "product roadmap", "product discovery",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "UX & Design",
                        "keywords": [
                            "ux", "user experience", "ui", "user interface",
                            "design", "product design", "ux design", "ui/ux",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 7. SALES
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Sales",
        "l1_keywords": [
            "sales", "revenue", "commercial", "cro", "go-to-market", "gtm",
            "business development", "account management", "enterprise sales",
            "inside sales", "field sales", "b2b sales",
            "sales operations", "sales ops", "revenue operations", "revops",
        ],
        "departments": [
            {
                "name": "Enterprise Sales",
                "keywords": [
                    "enterprise sales", "enterprise", "large enterprise",
                    "major accounts", "named accounts", "strategic accounts",
                    "global accounts", "b2b sales",
                    # GTM library variants
                    "head enterprise sales", "vp enterprise sales",
                    "chief enterprise sales", "director enterprise sales",
                ],
                "sub_depts": [
                    {
                        "name": "Account Management",
                        "keywords": [
                            "account management", "key accounts", "strategic account",
                            "account executive", "account director",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "New Business Development",
                        "keywords": [
                            "new business", "new logo", "hunting", "net new",
                            "business acquisition", "sales development",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Pre-Sales",
                        "keywords": [
                            "pre-sales", "solution engineering", "solution selling",
                            "solutioning", "technical sales",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "SMB & Mid-Market Sales",
                "keywords": [
                    "smb", "mid-market", "commercial sales", "inside sales",
                    "small business sales", "growth sales", "digital sales",
                ],
                "sub_depts": [
                    {
                        "name": "Inside Sales",
                        "keywords": [
                            "inside sales", "digital sales", "remote sales",
                            "inbound sales", "outbound sales", "telesales",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Channel & Partner Sales",
                "keywords": [
                    "channel", "partner", "indirect sales", "reseller",
                    "distribution sales", "alliances", "ecosystem",
                    "channel management", "partner management",
                    # GTM library variants
                    "head partnerships", "vp partnerships", "chief partnerships",
                    "director partnerships",
                ],
                "sub_depts": [
                    {
                        "name": "Partnerships",
                        "keywords": [
                            "partnerships", "strategic partnerships",
                            "alliance management", "partner success",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Channel Management",
                        "keywords": [
                            "channel management", "reseller", "distributor",
                            "value added reseller", "var", "distribution channel",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Sales Operations",
                "keywords": [
                    "sales operations", "sales ops", "revenue operations", "revops",
                    "sales enablement", "sales planning",
                    "go-to-market operations", "gtm operations",
                    # GTM library variants
                    "head gtm", "vp gtm", "chief gtm", "director gtm",
                ],
                "sub_depts": [
                    {
                        "name": "Revenue Operations",
                        "keywords": [
                            "revenue operations", "revops", "gtm operations",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Sales Enablement",
                        "keywords": [
                            "sales enablement", "sales training", "sales readiness",
                            "sales content", "playbooks sales",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Sales Analytics",
                        "keywords": [
                            "sales analytics", "sales reporting", "crm analytics",
                            "pipeline analytics", "quota management",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Key Accounts",
                "keywords": [
                    "key accounts", "national accounts", "global key accounts",
                    "named accounts", "strategic key accounts",
                    # GTM library variants
                    "head key accounts", "vp key accounts",
                    "chief key accounts", "director key accounts",
                ],
                "sub_depts": [
                    {
                        "name": "Global Account Management",
                        "keywords": [
                            "global account management", "gam", "strategic account management",
                            "key account manager",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 8. MARKETING
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Marketing",
        "l1_keywords": [
            "marketing", "brand", "communications", "cmo", "digital marketing",
            "growth marketing", "demand generation", "product marketing",
            "corporate communications", "marketing operations",
            "performance marketing", "content marketing",
        ],
        "departments": [
            {
                "name": "Brand & Communications",
                "keywords": [
                    "brand", "brand management", "corporate brand", "brand strategy",
                    "communications", "corporate communications", "public relations", "pr",
                    "media relations", "external communications",
                    # GTM library variants
                    "head brand", "vp brand", "chief brand", "director brand",
                ],
                "sub_depts": [
                    {
                        "name": "Brand",
                        "keywords": [
                            "brand management", "brand strategy", "brand equity",
                            "brand identity", "brand guidelines", "brand campaigns",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "PR & Media",
                        "keywords": [
                            "pr", "public relations", "media relations",
                            "press", "press office", "media management",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Internal Communications",
                        "keywords": [
                            "internal communications", "employee communications",
                            "town hall", "intranet", "change communications",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Corporate Affairs",
                        "keywords": [
                            "corporate affairs", "public affairs", "government relations",
                            "lobbying", "stakeholder management",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Digital Marketing",
                "keywords": [
                    "digital marketing", "performance marketing", "growth marketing",
                    "demand generation", "seo", "sem", "paid media",
                    "content marketing", "social media", "email marketing",
                    # GTM library variants
                    "head digital marketing", "vp digital marketing",
                    "chief digital marketing", "director digital marketing",
                ],
                "sub_depts": [
                    {
                        "name": "Performance Marketing",
                        "keywords": [
                            "performance marketing", "paid media", "ppc",
                            "paid search", "paid social", "programmatic",
                            # GTM library variants
                            "head performance marketing", "vp performance marketing",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "SEO & Content",
                        "keywords": [
                            "seo", "search engine optimisation", "content marketing",
                            "content strategy", "organic search",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Social Media",
                        "keywords": [
                            "social media", "social media marketing",
                            "instagram", "linkedin marketing", "tiktok",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Demand Generation",
                        "keywords": [
                            "demand generation", "demand gen", "lead generation",
                            "pipeline marketing", "abm", "account based marketing",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Email & Lifecycle Marketing",
                        "keywords": [
                            "email marketing", "crm marketing", "marketing automation",
                            "lifecycle marketing", "hubspot", "marketo",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Product Marketing",
                "keywords": [
                    "product marketing", "go-to-market marketing",
                    "product launch marketing", "positioning", "messaging",
                    "competitive intelligence marketing", "market intelligence",
                ],
                "sub_depts": [
                    {
                        "name": "Product Launch",
                        "keywords": [
                            "product launch", "launch planning", "release marketing",
                            "new product marketing",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Market Research & Insights",
                        "keywords": [
                            "market research", "market intelligence",
                            "competitive intelligence", "customer research",
                            "consumer insights", "market analysis",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Marketing Operations",
                "keywords": [
                    "marketing operations", "marketing ops", "martech",
                    "marketing technology", "marketing analytics",
                    "marketing data", "marketing infrastructure",
                ],
                "sub_depts": [
                    {
                        "name": "Martech",
                        "keywords": [
                            "martech", "marketing technology", "marketing stack",
                            "marketing platforms",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Marketing Analytics",
                        "keywords": [
                            "marketing analytics", "marketing data",
                            "attribution", "marketing roi", "marketing reporting",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Customer Marketing",
                "keywords": [
                    "customer marketing", "customer engagement marketing",
                    "lifecycle marketing", "retention marketing",
                    "upsell marketing", "loyalty marketing",
                ],
                "sub_depts": [
                    {
                        "name": "Retention & Loyalty",
                        "keywords": [
                            "retention", "customer retention", "loyalty",
                            "loyalty program", "churn reduction",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Customer Advocacy",
                        "keywords": [
                            "customer advocacy", "customer references",
                            "case studies", "testimonials", "community marketing",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 9. RESEARCH & DEVELOPMENT
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Research & Development",
        "l1_keywords": [
            "research and development", "r&d", "engineering r&d", "innovation rd",
            "product development rd", "advanced research", "er&d", "applied research",
            "clinical research", "drug discovery",
        ],
        "departments": [
            {
                "name": "Engineering & R&D",
                "keywords": [
                    "engineering", "research and development", "r&d",
                    "engineering services", "product development rd",
                    "advanced engineering", "manufacturing engineering",
                    "electrical engineering", "mechanical engineering",
                    "systems engineering", "process engineering rd",
                ],
                "sub_depts": [
                    {
                        "name": "Mechanical Engineering",
                        "keywords": [
                            "mechanical engineering", "mechanical design",
                            "structural engineering", "thermal engineering", "cad",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Electronics Engineering",
                        "keywords": [
                            "electronics", "electrical engineering",
                            "embedded systems", "firmware", "pcb",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Product Development",
                        "keywords": [
                            "product development", "new product development", "npd",
                            "product design", "product engineering rd",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Prototyping & Testing",
                        "keywords": [
                            "prototyping", "testing rd", "validation",
                            "verification", "dvt", "evt",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Innovation & Advanced Research",
                "keywords": [
                    "advanced research", "research", "fundamental research",
                    "applied research", "innovation labs", "emerging technology",
                    "technology research", "innovation centre",
                ],
                "sub_depts": [
                    {
                        "name": "Innovation Labs",
                        "keywords": [
                            "innovation lab", "innovation centre", "technology lab",
                            "research lab", "incubator", "emerging tech",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "AI & Emerging Tech Research",
                        "keywords": [
                            "ai research", "ml research", "emerging technology",
                            "quantum computing", "blockchain r&d",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Clinical & Scientific R&D",
                "keywords": [
                    "clinical research", "clinical trials", "drug development",
                    "r&d clinical", "preclinical", "clinical development",
                    "pharmacology", "biotech r&d",
                ],
                "sub_depts": [
                    {
                        "name": "Drug Discovery",
                        "keywords": [
                            "drug discovery", "target identification",
                            "lead optimisation", "medicinal chemistry",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Clinical Trials",
                        "keywords": [
                            "clinical trials", "clinical studies",
                            "phase 1", "phase 2", "phase 3",
                            "clinical operations",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Medical Affairs",
                        "keywords": [
                            "medical affairs", "medical science liaison",
                            "medical information", "pharmacovigilance",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Regulatory Affairs",
                        "keywords": [
                            "regulatory affairs", "regulatory submissions",
                            "fda", "ema", "regulatory strategy",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 10. SUSTAINABILITY & ESG
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Sustainability",
        "l1_keywords": [
            "sustainability", "esg", "environment", "climate", "corporate responsibility",
            "csr", "net zero", "carbon", "social responsibility", "responsible business",
        ],
        "departments": [
            {
                "name": "Sustainability",
                "keywords": [
                    "esg", "sustainability", "corporate sustainability",
                    "environment social governance", "responsible business",
                    "corporate responsibility", "sustainable development",
                    "csr", "social responsibility",
                ],
                "sub_depts": [
                    {
                        "name": "Climate & Environment",
                        "keywords": [
                            "climate", "environment", "carbon", "net zero",
                            "emissions", "decarbonisation", "carbon footprint",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Social Impact",
                        "keywords": [
                            "social impact", "csr", "community",
                            "corporate citizenship", "philanthropy",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "ESG Reporting",
                        "keywords": [
                            "esg reporting", "sustainability reporting",
                            "non-financial reporting", "gri", "tcfd", "sdg",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 11. ADMINISTRATION & FACILITIES
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Administration & Facilities",
        "l1_keywords": [
            "facilities", "administration", "real estate", "workplace",
            "office management", "corporate services", "security services", "fleet",
            "property management",
        ],
        "departments": [
            {
                "name": "Facilities & Real Estate",
                "keywords": [
                    "facilities", "real estate", "workplace", "office management",
                    "property", "facilities management", "corporate real estate",
                    "workspace",
                ],
                "sub_depts": [
                    {
                        "name": "Workplace Experience",
                        "keywords": [
                            "workplace experience", "workplace design",
                            "office design", "workplace strategy",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Corporate Real Estate",
                        "keywords": [
                            "real estate", "property management",
                            "lease management", "site selection",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Security Services",
                        "keywords": [
                            "security services", "physical security",
                            "access control", "security operations physical",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    # ════════════════════════════════════════════════════════════════════════════
    # L1 — 12. PUBLIC SECTOR / GOVERNMENT
    # ════════════════════════════════════════════════════════════════════════════
    {
        "l1": "Public Sector",
        "l1_keywords": [
            "policy", "government", "public sector", "public service",
            "citizen", "welfare", "legislative", "budget allocation",
            "grants management", "defence", "intelligence services",
            "ministry", "department of",
        ],
        "departments": [
            {
                "name": "Policy & Governance",
                "keywords": [
                    "policy", "policy design", "policy development", "legislation",
                    "regulatory policy", "governance public", "public policy",
                    "government affairs",
                ],
                "sub_depts": [
                    {
                        "name": "Policy Design",
                        "keywords": [
                            "policy design", "policy development",
                            "policy analysis", "policy research",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Legislative Affairs",
                        "keywords": [
                            "legislative affairs", "legislation",
                            "parliament", "congress", "regulatory reform",
                        ],
                        "teams": [],
                    },
                ],
            },
            {
                "name": "Citizen Services",
                "keywords": [
                    "citizen services", "public services", "welfare",
                    "public service delivery", "social services",
                    "government services", "e-government",
                ],
                "sub_depts": [
                    {
                        "name": "Public Service Delivery",
                        "keywords": [
                            "public service delivery", "citizen engagement",
                            "welfare programs", "social welfare",
                        ],
                        "teams": [],
                    },
                    {
                        "name": "Public Finance",
                        "keywords": [
                            "public finance", "budget allocation",
                            "grants management", "fiscal policy", "public expenditure",
                        ],
                        "teams": [],
                    },
                ],
            },
        ],
    },

    {
        "l1": "Customer Success",
        "l1_keywords": [
            "customer success", "customer experience", "customer support", "client success",
            "account management", "client relations", "renewals", "onboarding",
            "customer service", "cx", "csx", "csm", "customer operations",
            "voice of customer", "customer satisfaction", "nps",
        ],
        "departments": [
            {
                "name": "Customer Support",
                "keywords": ["customer support", "technical support", "help desk", "helpdesk",
                             "tier 1", "tier 2", "tier 3", "service desk", "support operations",
                             "customer care", "customer service"],
                "sub_depts": [
                    {"name": "Technical Support", "keywords": ["technical support", "product support", "engineering support", "l1 support", "l2 support", "l3 support"], "teams": []},
                    {"name": "Help Desk", "keywords": ["help desk", "helpdesk", "it helpdesk", "service desk", "desktop support"], "teams": []},
                ],
            },
            {
                "name": "Client Relations",
                "keywords": ["account management", "client relations", "customer success manager", "csm",
                             "renewals", "onboarding", "client onboarding", "client management",
                             "customer success", "client success", "strategic accounts"],
                "sub_depts": [
                    {"name": "Account Management", "keywords": ["account management", "key account", "named accounts", "strategic accounts", "account executive"], "teams": []},
                    {"name": "Customer Onboarding", "keywords": ["onboarding", "implementation", "professional services", "customer onboarding"], "teams": []},
                ],
            },
            {
                "name": "Customer Experience",
                "keywords": ["customer experience", "cx", "voice of customer", "voc",
                             "customer satisfaction", "csat", "nps", "net promoter",
                             "customer insights", "customer research"],
                "sub_depts": [],
            },
        ],
    },
    {
        "l1": "Product Management",
        "l1_keywords": [
            "product management", "product manager", "product owner", "cpo",
            "chief product officer", "head of product", "vp product", "product strategy",
            "product roadmap", "product operations", "product analytics",
            "ux research", "user research", "ux ui", "design", "product design",
        ],
        "departments": [
            {
                "name": "Product Strategy",
                "keywords": ["product strategy", "product roadmap", "product planning",
                             "product management", "product manager", "product owner",
                             "product operations", "product analytics", "product growth",
                             "go-to-market", "gtm strategy"],
                "sub_depts": [
                    {"name": "Product Analytics", "keywords": ["product analytics", "product metrics", "product data", "feature analytics"], "teams": []},
                    {"name": "Product Operations", "keywords": ["product operations", "product ops", "release management", "product programme"], "teams": []},
                ],
            },
            {
                "name": "UX & Design",
                "keywords": ["ux", "ui", "user experience", "user interface", "design",
                             "ux research", "user research", "ux ui", "visual design",
                             "product design", "interaction design", "information architecture",
                             "design system", "design operations"],
                "sub_depts": [
                    {"name": "UX Research", "keywords": ["ux research", "user research", "usability", "user testing", "ethnographic research"], "teams": []},
                    {"name": "UI Design", "keywords": ["ui design", "visual design", "interface design", "design system", "brand design"], "teams": []},
                ],
            },
        ],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Helper: convert TAXONOMY → UNIVERSAL_DEPTS format used by DepartmentExtractor
# (primary, [p_kws], [(secondary, [s_kws]), ...])
#
# Mapping strategy:
#   - "Corporate & Executive" L1: each L2 dept becomes its own primary entry
#     (Board of Directors, Executive Management, CEO Office stay as top-level).
#   - All other L1s: L1 name = primary; all L2 keywords merged into primary pool;
#     each L2 dept becomes a secondary (with L2 + L3 keywords combined).
#
# This gives the organogram tree the canonical shape the user requested:
#   Finance → FP&A → Budgeting
#   Human Resources → Talent Acquisition → Sourcing
#   Executive Management → C-Suite / President / Managing Directors
#   Board of Directors → Independent Directors / Committees
# ─────────────────────────────────────────────────────────────────────────────
def build_universal_depts() -> list[tuple[str, list[str], list[tuple[str, list[str]]]]]:
    result: list[tuple[str, list[str], list[tuple[str, list[str]]]]] = []

    # L1s whose L2 departments are promoted to top-level primaries
    PROMOTE_L2 = {"Corporate & Executive"}

    for l1 in TAXONOMY:
        l1_name = l1["l1"]
        l1_kws  = list(l1["l1_keywords"])

        if l1_name in PROMOTE_L2:
            # Each L2 department becomes its own primary entry
            for dept in l1["departments"]:
                p_kws: list[str] = list(dept["keywords"])
                secondaries: list[tuple[str, list[str]]] = []
                for sub in dept.get("sub_depts", []):
                    s_kws: list[str] = list(sub["keywords"])
                    for team in sub.get("teams", []):
                        s_kws.extend(team["keywords"])
                    secondaries.append((sub["name"], s_kws))
                result.append((dept["name"], p_kws, secondaries))
        else:
            # L1 becomes primary; aggregate all L2 keywords into primary pool
            primary_kws: list[str] = list(l1_kws)
            secondaries_l1: list[tuple[str, list[str]]] = []
            for dept in l1["departments"]:
                # L2 keywords contribute to primary matching
                primary_kws.extend(dept["keywords"])
                # L2 + its L3 keywords form the secondary keyword set
                sec_kws: list[str] = list(dept["keywords"])
                for sub in dept.get("sub_depts", []):
                    sec_kws.extend(sub["keywords"])
                    for team in sub.get("teams", []):
                        sec_kws.extend(team["keywords"])
                secondaries_l1.append((dept["name"], sec_kws))
            result.append((l1_name, primary_kws, secondaries_l1))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helper: flat list of all L2 → L3 → L4 keyword entries for search
# ─────────────────────────────────────────────────────────────────────────────
def flatten_keywords() -> list[tuple[str, str, str, str]]:
    """
    Returns list of (keyword, l1_name, l2_name, l3_name) tuples.
    Sorted longest keyword first for greedy matching.
    """
    entries: list[tuple[str, str, str, str]] = []
    for l1 in TAXONOMY:
        l1_name = l1["l1"]
        for dept in l1["departments"]:
            l2_name = dept["name"]
            for kw in dept["keywords"]:
                entries.append((kw.lower(), l1_name, l2_name, ""))
            for sub in dept.get("sub_depts", []):
                l3_name = sub["name"]
                for kw in sub["keywords"]:
                    entries.append((kw.lower(), l1_name, l2_name, l3_name))
                for team in sub.get("teams", []):
                    for kw in team["keywords"]:
                        entries.append((kw.lower(), l1_name, l2_name, l3_name))
    entries.sort(key=lambda x: -len(x[0]))
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# L1 keyword lookup → function name
# ─────────────────────────────────────────────────────────────────────────────
def build_l1_index() -> list[tuple[str, str]]:
    """Returns [(keyword, l1_name), ...] sorted longest first."""
    entries: list[tuple[str, str]] = []
    for l1 in TAXONOMY:
        for kw in l1["l1_keywords"]:
            entries.append((kw.lower(), l1["l1"]))
    entries.sort(key=lambda x: -len(x[0]))
    return entries
