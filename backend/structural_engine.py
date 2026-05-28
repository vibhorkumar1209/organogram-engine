"""
Universal Organogram Engine - Structural Engine
Builds a Directed Acyclic Graph (DAG) from classified records.
Inserts Ghost Nodes to maintain 10-layer depth continuity.
Supports recursive CTE-style drill-down queries.
"""

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)

from inference_logic import ClassifiedRecord, InferenceEngine

# ─────────────────────────────────────────────────────────────────────────────
# EXCEL RULE TABLES  (loaded from backend/rules/ at import time)
# ─────────────────────────────────────────────────────────────────────────────
# CANONICAL_L0_DEPTS — 16 authoritative top-level department names from
#                      Global_Org_Hierarchy.xlsx (Master Hierarchy sheet).
# L0_DEPT_SUBS       — {l0_dept: [l1_sub_dept, ...]} sub-department index.
# Used to enrich _CANONICAL_PRIMARY so any new L0 dept added to the Excel
# file is automatically recognised without touching code.
try:
    from rules.loader import CANONICAL_L0_DEPTS as _EXCEL_L0_DEPTS
    from rules.loader import L0_DEPT_SUBS as _EXCEL_L0_SUBS
    from rules.loader import TITLE_TO_GRADE as _EXCEL_TITLE_GRADES
except ImportError:
    _EXCEL_L0_DEPTS: list[str] = []
    _EXCEL_L0_SUBS: dict[str, list[str]] = {}
    _EXCEL_TITLE_GRADES: dict[str, int] = {}


# ─────────────────────────────────────────────
# NODE TYPES
# ─────────────────────────────────────────────
NODE_GLOBAL    = "global"
NODE_REGION    = "region"
NODE_SECTOR    = "sector"
NODE_DEPT_P    = "dept_primary"
NODE_DEPT_S    = "dept_secondary"
NODE_DEPT_T    = "dept_tertiary"
NODE_PERSON    = "person"
NODE_GHOST     = "ghost"

# ─────────────────────────────────────────────
# CANONICAL DEPARTMENT NAME NORMALIZATION
# ─────────────────────────────────────────────

# Accepted top-level (primary) department names.
# Any name NOT in this set is treated as either a secondary/team name
# ── Fix 3: Sub-department (dept_secondary) canonical hierarchy ───────────────
# Maps raw/vendor dept_secondary values → rational sub-department names
# that fit within the expected parent department hierarchy.
#
# Sales parent should produce: Account Management | New Business | Pre-Sales |
#   Sales Operations | Channel & Partners | Sales & Account Management |
#   Sales & Commercial | Inside Sales
# Marketing parent should produce: Brand | Digital Marketing | Content |
#   Performance Marketing | Product Marketing | Events | Market Research
# Finance parent: FP&A | Accounting | Treasury | Tax | Internal Audit |
#   Investor Relations | Financial Reporting
# etc.
#
# Any sub-dept that doesn't fit a rational model is merged into its parent.
_SUBDEPT_REMAP: dict[str, str] = {
    # Programme / Project / PMO — never a sub-dept name
    "programme":                    "",   # merge into parent
    "programme management":         "",
    "programme delivery":           "",
    "project management":           "",
    "project office":               "",
    "pmo":                          "",
    "delivery":                     "",
    # Generic / non-rational
    "general":                      "",
    "admin":                        "",
    "administration":               "",
    "support":                      "",
    "general management":           "",
    # Sales sub-depts
    "account management":           "Account Management",
    "key accounts":                 "Account Management",
    "strategic accounts":           "Account Management",
    "enterprise accounts":          "Account Management",
    "named accounts":               "Account Management",
    "new business":                 "New Business Development",
    "new business development":     "New Business Development",
    "business development":         "New Business Development",
    "pre-sales":                    "Pre-Sales & Solutioning",
    "presales":                     "Pre-Sales & Solutioning",
    "solution engineering":         "Pre-Sales & Solutioning",
    "sales operations":             "Sales Operations",
    "sales ops":                    "Sales Operations",
    "revenue operations":           "Sales Operations",
    "revops":                       "Sales Operations",
    "sales enablement":             "Sales Operations",
    "inside sales":                 "Inside Sales",
    "telesales":                    "Inside Sales",
    "channel":                      "Channel & Partners",
    "channel management":           "Channel & Partners",
    "channel sales":                "Channel & Partners",
    "partner management":           "Channel & Partners",
    "partnerships":                 "Channel & Partners",
    "commercial":                   "Sales & Commercial",
    "sales & commercial":           "Sales & Commercial",
    "sales & account management":   "Sales & Account Management",
    # Marketing sub-depts
    "brand":                        "Brand & Communications",
    "brand management":             "Brand & Communications",
    "brand & marketing":            "Brand & Communications",
    "digital marketing":            "Digital & Performance Marketing",
    "performance marketing":        "Digital & Performance Marketing",
    "paid media":                   "Digital & Performance Marketing",
    "seo":                          "Digital & Performance Marketing",
    "content":                      "Content & Creative",
    "content marketing":            "Content & Creative",
    "creative":                     "Content & Creative",
    "design":                       "Content & Creative",
    "product marketing":            "Product Marketing",
    "events":                       "Events & Sponsorship",
    "market research":              "Market Research & Insights",
    "consumer insights":            "Market Research & Insights",
    "trade marketing":              "Trade Marketing",
    # Finance sub-depts
    "fp&a":                         "FP&A",
    "financial planning":           "FP&A",
    "budgeting":                    "FP&A",
    "forecasting":                  "FP&A",
    "accounting":                   "Accounting & Reporting",
    "financial reporting":          "Accounting & Reporting",
    "general ledger":               "Accounting & Reporting",
    "treasury":                     "Treasury",
    "cash management":              "Treasury",
    "tax":                          "Tax",
    "direct tax":                   "Tax",
    "indirect tax":                 "Tax",
    "internal audit":               "Internal Audit",
    "audit":                        "Internal Audit",
    "investor relations":           "Investor Relations",
    # HR sub-depts
    "talent acquisition":           "Talent Acquisition",
    "recruitment":                  "Talent Acquisition",
    "learning & development":       "Learning & Development",
    "l&d":                          "Learning & Development",
    "training":                     "Learning & Development",
    "compensation & benefits":      "Compensation & Benefits",
    "c&b":                          "Compensation & Benefits",
    "hrbp":                         "HR Business Partnering",
    "hr business partner":          "HR Business Partnering",
    "people analytics":             "People Analytics",
    "workforce planning":           "People Analytics",
    # Technology sub-depts
    "infrastructure":               "IT Infrastructure",
    "it infrastructure":            "IT Infrastructure",
    "cybersecurity":                "Information Security",
    "security":                     "Information Security",
    "information security":         "Information Security",
    "application development":      "Application Development",
    "software development":         "Application Development",
    "enterprise architecture":      "Enterprise Architecture",
    "data engineering":             "Data Engineering",
    "data science":                 "Data & Analytics",
    "analytics":                    "Data & Analytics",
    # Operations sub-depts
    "supply chain":                 "Supply Chain",
    "logistics":                    "Logistics",
    "warehousing":                  "Logistics",
    "quality":                      "Quality & Compliance",
    "quality assurance":            "Quality & Compliance",
    "hse":                          "Health, Safety & Environment",
    "health & safety":              "Health, Safety & Environment",
    "maintenance":                  "Asset & Maintenance",
    "facilities":                   "Facilities Management",
}

# or an industry-specific label and gets remapped via _DEPT_REMAP.
_CANONICAL_PRIMARY: frozenset[str] = frozenset({
    # ── Leadership ─────────────────────────────────────────────────────
    "Board of Management",
    "Executive Management",
    # ── Support functions ───────────────────────────────────────────────
    "Finance & Accounting",
    "Human Resources",
    "Legal, Risk & Compliance",          # merged per Global_Org_Hierarchy.xlsx
    # ── Technology (split per reference) ───────────────────────────────
    "Information Technology",            # infra, apps, cybersecurity, data
    "Engineering",                       # software / platform / hardware eng.
    "Product Management",
    # ── Operations ──────────────────────────────────────────────────────
    "Operations",
    "Supply Chain",
    "Manufacturing",
    "Procurement",
    "Facilities, Real Estate & Workplace",  # standalone per reference
    # ── Revenue ─────────────────────────────────────────────────────────
    "Sales & Business Development",
    "Marketing",
    "Customer Success & Service",
    "Corporate Communications & Public Affairs",  # standalone per reference
    # ── Strategy ────────────────────────────────────────────────────────
    "Strategy & Corporate Development",
    "Sustainability",
    # ── R&D ─────────────────────────────────────────────────────────────
    "Research & Development",
    # ── Financial-services industry-specific ────────────────────────────
    "Investment Management",
    # Note: Actuarial, Underwriting, Claims are now Operations sub-depts
    # ── Investment bank / markets business divisions ─────────────────────
    "Investment Banking",
    "Sales & Trading",
    "Wealth Management",
})

# Map Excel L0 UPPERCASE dept names → canonical Python strings.
# Used to auto-extend _CANONICAL_PRIMARY when new L0 depts are added to the Excel file.
# Entries already in _CANONICAL_PRIMARY are skipped (no-op for known depts).
_EXCEL_L0_CANONICAL_MAP: dict[str, str] = {
    "BOARD OF DIRECTORS":                     "Board of Management",
    "EXECUTIVE MANAGEMENT (C-SUITE)":          "Executive Management",
    "FINANCE & ACCOUNTING":                    "Finance & Accounting",
    "HUMAN RESOURCES (PEOPLE & CULTURE)":      "Human Resources",
    "LEGAL, RISK & COMPLIANCE":               "Legal, Risk & Compliance",
    "INFORMATION TECHNOLOGY (IT)":             "Information Technology",
    "ENGINEERING":                             "Engineering",
    "RESEARCH & DEVELOPMENT (R&D)":            "Research & Development",
    "PRODUCT MANAGEMENT":                      "Product Management",
    "MARKETING":                               "Marketing",
    "SALES & BUSINESS DEVELOPMENT":            "Sales & Business Development",
    "CUSTOMER SUCCESS & SERVICE":              "Customer Success & Service",
    "OPERATIONS":                              "Operations",
    "STRATEGY & CORPORATE DEVELOPMENT":        "Strategy & Corporate Development",
    "FACILITIES, REAL ESTATE & WORKPLACE":     "Facilities, Real Estate & Workplace",
    "CORPORATE COMMUNICATIONS & PUBLIC AFFAIRS": "Corporate Communications & Public Affairs",
}
if _EXCEL_L0_DEPTS:
    extra = frozenset(
        _EXCEL_L0_CANONICAL_MAP.get(name, name)
        for name in _EXCEL_L0_DEPTS
        if name
    ) - _CANONICAL_PRIMARY
    if extra:
        _CANONICAL_PRIMARY = _CANONICAL_PRIMARY | extra
        logger.debug(
            "Added %d new L0 depts from Excel to _CANONICAL_PRIMARY: %s",
            len(extra), extra,
        )

# Map non-canonical / industry-specific / generic dept names → canonical.
# Keys are lowercase stripped strings.
_DEPT_REMAP: dict[str, str] = {
    # ── Executive / leadership catch-alls ────────────────────────────────
    "general":                          "Operations",
    "general management":               "Operations",
    "administration":                   "Operations",
    "corporate":                        "Executive Management",
    "corporate & executive":            "Executive Management",
    "executive":                        "Executive Management",
    "ceo office":                       "Executive Management",
    "c-suite":                          "Executive Management",
    "managing directors":               "Executive Management",
    "president / evp":                  "Executive Management",
    # ── Finance & Accounting (all variants) ──────────────────────────────
    "finance":                          "Finance & Accounting",
    "financial planning & analysis":    "Finance & Accounting",
    "fp&a":                             "Finance & Accounting",
    "corporate finance":                "Finance & Accounting",
    "financial services":               "Finance & Accounting",
    "accounting":                       "Finance & Accounting",
    "financial advisory":               "Finance & Accounting",
    "deal advisory":                    "Strategy & Corporate Development",
    "m&a":                              "Strategy & Corporate Development",
    "investor relations":               "Finance & Accounting",
    # ── Human Resources ──────────────────────────────────────────────────
    "hr":                               "Human Resources",
    "people":                           "Human Resources",
    "people & culture":                 "Human Resources",
    "talent":                           "Human Resources",
    "talent management":                "Human Resources",
    "talent & culture":                 "Human Resources",
    "people operations":                "Human Resources",
    "workforce":                        "Human Resources",
    "learning & development":           "Human Resources",
    # ── Legal, Risk & Compliance (merged per reference) ──────────────────
    "legal":                            "Legal, Risk & Compliance",
    "legal & compliance":               "Legal, Risk & Compliance",
    "compliance & legal":               "Legal, Risk & Compliance",
    "legal & regulatory":               "Legal, Risk & Compliance",
    "legal, compliance & regulatory":   "Legal, Risk & Compliance",
    "legal and compliance":             "Legal, Risk & Compliance",
    "legal, risk & compliance":         "Legal, Risk & Compliance",
    "regulatory":                       "Legal, Risk & Compliance",
    "regulatory affairs":               "Legal, Risk & Compliance",
    "compliance":                       "Legal, Risk & Compliance",
    "governance":                       "Legal, Risk & Compliance",
    "secretariat":                      "Legal, Risk & Compliance",
    "company secretary":                "Legal, Risk & Compliance",
    "risk & compliance":                "Legal, Risk & Compliance",
    "risk and compliance":              "Legal, Risk & Compliance",
    "governance risk & compliance":     "Legal, Risk & Compliance",
    "governance, risk & compliance":    "Legal, Risk & Compliance",
    "grc":                              "Legal, Risk & Compliance",
    "risk":                             "Legal, Risk & Compliance",
    "credit risk":                      "Legal, Risk & Compliance",
    "market risk":                      "Legal, Risk & Compliance",
    "operational risk":                 "Legal, Risk & Compliance",
    "enterprise risk":                  "Legal, Risk & Compliance",
    "risk advisory":                    "Legal, Risk & Compliance",
    # ── Information Technology ────────────────────────────────────────────
    "it":                               "Information Technology",
    "information technology":           "Information Technology",
    "technology":                       "Information Technology",
    "data":                             "Information Technology",
    "analytics":                        "Information Technology",
    "data science":                     "Information Technology",
    "data & analytics":                 "Information Technology",
    "digital":                          "Information Technology",
    "cybersecurity":                    "Information Technology",
    "information security":             "Information Technology",
    "it infrastructure":                "Information Technology",
    "enterprise architecture":          "Information Technology",
    "it operations":                    "Information Technology",
    "it service delivery":              "Information Technology",
    # ── Engineering ───────────────────────────────────────────────────────
    "software":                         "Engineering",
    "engineering":                      "Engineering",
    "product & engineering":            "Engineering",
    "software engineering":             "Engineering",
    "platform engineering":             "Engineering",
    "hardware engineering":             "Engineering",
    "quality engineering":              "Engineering",
    "devops":                           "Engineering",
    "application development":          "Engineering",
    "software development":             "Engineering",
    # ── Sales & Business Development ────────────────────────────────────
    "sales":                            "Sales & Business Development",
    "key accounts":                     "Sales & Business Development",
    "key account management":           "Sales & Business Development",
    "enterprise sales":                 "Sales & Business Development",
    "inside sales":                     "Sales & Business Development",
    "field sales":                      "Sales & Business Development",
    "retail sales":                     "Sales & Business Development",
    "channel sales":                    "Sales & Business Development",
    "business development":             "Sales & Business Development",
    "revenue":                          "Sales & Business Development",
    "commercial & sales":               "Sales & Business Development",
    "sales & distribution":             "Sales & Business Development",
    "bancassurance":                    "Sales & Business Development",
    "vehicle sales":                    "Sales & Business Development",
    "commercial":                       "Sales & Business Development",
    "sales & commercial":               "Sales & Business Development",
    "partnerships":                     "Sales & Business Development",
    "alliances":                        "Sales & Business Development",
    # ── Marketing ────────────────────────────────────────────────────────
    "brand management":                 "Marketing",
    "brand & marketing":                "Marketing",
    "trade marketing":                  "Marketing",
    "shopper marketing":                "Marketing",
    "consumer insights":                "Marketing",
    "digital marketing":                "Marketing",
    "performance marketing":            "Marketing",
    "product marketing":                "Marketing",
    "marketing & brand":                "Marketing",
    # ── Corporate Communications → sub-dept of Marketing ─────────────────
    # Plain comms/internal comms sit under Marketing.
    # External-facing public affairs/government relations remain standalone.
    "corporate communications":         "Marketing",
    "internal communications":          "Marketing",
    "communications":                   "Marketing",
    # ── Corporate Communications & Public Affairs (standalone) ────────────
    "public relations":                 "Corporate Communications & Public Affairs",
    "pr":                               "Corporate Communications & Public Affairs",
    "external affairs":                 "Corporate Communications & Public Affairs",
    "public affairs":                   "Corporate Communications & Public Affairs",
    "government relations":             "Corporate Communications & Public Affairs",
    "media relations":                  "Corporate Communications & Public Affairs",
    "investor communications":          "Corporate Communications & Public Affairs",
    # ── Customer Success & Service ────────────────────────────────────────
    "customer service":                 "Customer Success & Service",
    "customer support":                 "Customer Success & Service",
    "client services":                  "Customer Success & Service",
    "client success":                   "Customer Success & Service",
    "after sales":                      "Customer Success & Service",
    "after-sales":                      "Customer Success & Service",
    "post sales":                       "Customer Success & Service",
    "customer experience":              "Customer Success & Service",
    "customer success":                 "Customer Success & Service",
    # ── Insurance / financial-services ops (sub-depts of Operations) ─────
    "actuarial":                        "Operations",
    "underwriting":                     "Operations",
    "claims":                           "Operations",
    "claims management":                "Operations",
    "claims & operations":              "Operations",
    "reinsurance":                      "Operations",
    # ── Operations / industry-specific ───────────────────────────────────
    "upstream":                         "Operations",
    "downstream":                       "Operations",
    "e&p":                              "Operations",
    "exploration":                      "Operations",
    "drilling":                         "Operations",
    "refining":                         "Operations",
    "field development":                "Operations",
    "mining operations":                "Operations",
    "plant operations":                 "Operations",
    "service delivery":                 "Operations",
    "quality":                          "Operations",
    "quality assurance":                "Operations",
    "quality control":                  "Operations",
    "health & safety":                  "Operations",
    "hse":                              "Operations",
    "ehs":                              "Operations",
    "maintenance":                      "Operations",
    "technical services":               "Operations",
    "field services":                   "Operations",
    "service engineering":              "Operations",
    "admin":                            "Operations",
    "support":                          "Operations",
    "shared services":                  "Operations",
    "business support":                 "Operations",
    "back office":                      "Operations",
    "office management":                "Operations",
    "delivery":                         "Operations",
    "delivery management":              "Operations",
    "project delivery":                 "Operations",
    # ── Facilities, Real Estate & Workplace (standalone per reference) ────
    "facilities":                       "Facilities, Real Estate & Workplace",
    "real estate":                      "Facilities, Real Estate & Workplace",
    "facilities & real estate":         "Facilities, Real Estate & Workplace",
    "real estate & facilities":         "Facilities, Real Estate & Workplace",
    "facilities management":            "Facilities, Real Estate & Workplace",
    "corporate real estate":            "Facilities, Real Estate & Workplace",
    "workplace":                        "Facilities, Real Estate & Workplace",
    "workplace & facilities":           "Facilities, Real Estate & Workplace",
    "workplace services":               "Facilities, Real Estate & Workplace",
    "facilities, real estate & workplace": "Facilities, Real Estate & Workplace",
    # ── Programme / PMO → Strategy & Corporate Development ───────────────
    "programme":                        "Strategy & Corporate Development",
    "programme management":             "Strategy & Corporate Development",
    "programme management office":      "Strategy & Corporate Development",
    "programme delivery":               "Strategy & Corporate Development",
    "project management":               "Strategy & Corporate Development",
    "project management office":        "Strategy & Corporate Development",
    "pmo":                              "Strategy & Corporate Development",
    "epmo":                             "Strategy & Corporate Development",
    "project office":                   "Strategy & Corporate Development",
    # ── Supply chain ─────────────────────────────────────────────────────
    "logistics":                        "Supply Chain",
    "warehousing":                      "Supply Chain",
    "distribution":                     "Supply Chain",
    "sourcing":                         "Procurement",
    "indirect procurement":             "Procurement",
    "direct procurement":               "Procurement",
    # ── Public Sector / Government → Sales & Business Development ─────────
    "public sector":                    "Sales & Business Development",
    "government":                       "Sales & Business Development",
    "government & public sector":       "Sales & Business Development",
    "public sector & government":       "Sales & Business Development",
    "defence":                          "Sales & Business Development",
    "defence & public sector":          "Sales & Business Development",
    "healthcare sector":                "Sales & Business Development",
    "financial services sector":        "Sales & Business Development",
    # ── R&D ──────────────────────────────────────────────────────────────
    "r&d":                              "Research & Development",
    "innovation":                       "Research & Development",
    "research & development":           "Research & Development",
    # ── Financial-services business divisions ────────────────────────────
    "investment banking":               "Investment Banking",
    "investment banking division":      "Investment Banking",
    "m&a advisory":                     "Investment Banking",
    "mergers & acquisitions":           "Investment Banking",
    "capital markets":                  "Investment Banking",
    "sales & trading":                  "Sales & Trading",
    "fixed income":                     "Sales & Trading",
    "fixed income, currencies & commodities": "Sales & Trading",
    "equities":                         "Sales & Trading",
    "equity sales":                     "Sales & Trading",
    "institutional securities":         "Sales & Trading",
    "wealth management":                "Wealth Management",
    "global wealth management":         "Wealth Management",
    "private banking":                  "Wealth Management",
    "private banking & wealth management": "Wealth Management",
    "private wealth management":        "Wealth Management",
    # ── Strategy & Corporate Development ────────────────────────────────
    "corporate strategy":               "Strategy & Corporate Development",
    "group strategy":                   "Strategy & Corporate Development",
    "strategy & corporate development": "Strategy & Corporate Development",
    "strategy":                         "Strategy & Corporate Development",
    "corporate development":            "Strategy & Corporate Development",
    "business strategy":                "Strategy & Corporate Development",
    "policy & strategy":                "Strategy & Corporate Development",
    "strategy & policy":                "Strategy & Corporate Development",
    "policy":                           "Strategy & Corporate Development",
    "public policy":                    "Strategy & Corporate Development",
    "strategy & planning":              "Strategy & Corporate Development",
    "planning & strategy":              "Strategy & Corporate Development",
    "transformation":                   "Strategy & Corporate Development",
    "business transformation":          "Strategy & Corporate Development",
    "digital transformation":           "Strategy & Corporate Development",
    "change management":                "Strategy & Corporate Development",
    # ── Sustainability / ESG ─────────────────────────────────────────────
    "esg":                              "Sustainability",
    "sustainability & esg":             "Sustainability",
    "environment":                      "Sustainability",
    "csr":                              "Sustainability",
    # ── NLP engine legacy output remaps ─────────────────────────────────
    # NLP engine returns old canonical names; these remap them to new ones.
    "risk management":                  "Legal, Risk & Compliance",
    "legal & compliance":               "Legal, Risk & Compliance",
    "internal audit":                   "Finance & Accounting",
    "customer experience":              "Customer Success & Service",
    "customer success":                 "Customer Success & Service",
    "customer service":                 "Customer Success & Service",
    "engineering / it":                 "Information Technology",
    "people & culture":                 "Human Resources",
    "strategy":                         "Strategy & Corporate Development",
    "corporate development":            "Strategy & Corporate Development",
    "sales":                            "Sales & Business Development",
    "medical affairs":                  "Research & Development",
}


# Maps sub-department concepts that sometimes appear as dept_primary → the
# correct (parent_primary, sub_dept_label) pair.  Applied in insert_person()
# only when dept_secondary is otherwise empty, and only for L3+ people.
_DEPT_ELEVATE: dict[str, tuple[str, str]] = {
    # ── Legal, Risk & Compliance sub-depts (merged) ─────────────────────
    "legal & compliance":               ("Legal, Risk & Compliance", "Legal Counsel"),
    "compliance & legal":               ("Legal, Risk & Compliance", "Legal Counsel"),
    "legal and compliance":             ("Legal, Risk & Compliance", "Legal Counsel"),
    "compliance":                       ("Legal, Risk & Compliance", "Regulatory & Compliance"),
    "legal & regulatory":               ("Legal, Risk & Compliance", "Regulatory & Compliance"),
    "legal, compliance & regulatory":   ("Legal, Risk & Compliance", "Regulatory & Compliance"),
    "regulatory affairs":               ("Legal, Risk & Compliance", "Regulatory & Compliance"),
    "regulatory":                       ("Legal, Risk & Compliance", "Regulatory & Compliance"),
    "governance":                       ("Legal, Risk & Compliance", "Ethics & Governance"),
    "secretariat":                      ("Legal, Risk & Compliance", "Corporate Secretary"),
    "company secretary":                ("Legal, Risk & Compliance", "Corporate Secretary"),
    "risk & compliance":                ("Legal, Risk & Compliance", "Risk Management"),
    "risk and compliance":              ("Legal, Risk & Compliance", "Risk Management"),
    "governance risk & compliance":     ("Legal, Risk & Compliance", "Ethics & Governance"),
    "governance, risk & compliance":    ("Legal, Risk & Compliance", "Ethics & Governance"),
    "grc":                              ("Legal, Risk & Compliance", "Ethics & Governance"),
    "credit risk":                      ("Legal, Risk & Compliance", "Risk Management"),
    "market risk":                      ("Legal, Risk & Compliance", "Risk Management"),
    "operational risk":                 ("Legal, Risk & Compliance", "Risk Management"),
    "enterprise risk":                  ("Legal, Risk & Compliance", "Risk Management"),
    "risk advisory":                    ("Legal, Risk & Compliance", "Risk Management"),
    "ethics":                           ("Legal, Risk & Compliance", "Ethics & Governance"),
    "ip":                               ("Legal, Risk & Compliance", "Intellectual Property"),
    "intellectual property":            ("Legal, Risk & Compliance", "Intellectual Property"),
    # ── Strategy & Corporate Development sub-depts ───────────────────────
    "policy & strategy":                ("Strategy & Corporate Development", "Corporate Strategy"),
    "strategy & policy":                ("Strategy & Corporate Development", "Corporate Strategy"),
    "public policy":                    ("Strategy & Corporate Development", "Corporate Strategy"),
    "policy":                           ("Strategy & Corporate Development", "Corporate Strategy"),
    "corporate strategy":               ("Strategy & Corporate Development", "Corporate Strategy"),
    "group strategy":                   ("Strategy & Corporate Development", "Corporate Strategy"),
    "business strategy":                ("Strategy & Corporate Development", "Corporate Strategy"),
    "strategy & planning":              ("Strategy & Corporate Development", "Corporate Strategy"),
    "planning & strategy":              ("Strategy & Corporate Development", "Corporate Strategy"),
    "transformation":                   ("Strategy & Corporate Development", "Digital Transformation & Innovation"),
    "business transformation":          ("Strategy & Corporate Development", "Digital Transformation & Innovation"),
    "digital transformation":           ("Strategy & Corporate Development", "Digital Transformation & Innovation"),
    "change management":                ("Strategy & Corporate Development", "Digital Transformation & Innovation"),
    "programme management":             ("Strategy & Corporate Development", "Corporate Strategy"),
    "programme management office":      ("Strategy & Corporate Development", "Corporate Strategy"),
    "pmo":                              ("Strategy & Corporate Development", "Corporate Strategy"),
    "epmo":                             ("Strategy & Corporate Development", "Corporate Strategy"),
    "project management":               ("Strategy & Corporate Development", "Corporate Strategy"),
    "deal advisory":                    ("Strategy & Corporate Development", "M&A & Corporate Development"),
    "m&a":                              ("Strategy & Corporate Development", "M&A & Corporate Development"),
    "corporate development":            ("Strategy & Corporate Development", "M&A & Corporate Development"),
    # ── Sales & Business Development sub-depts ──────────────────────────
    "sales & account management":       ("Sales & Business Development", "Direct Sales"),
    "sales & commercial":               ("Sales & Business Development", "Sales Operations"),
    "account management":               ("Sales & Business Development", "Direct Sales"),
    "key account management":           ("Sales & Business Development", "Direct Sales"),
    "inside sales":                     ("Sales & Business Development", "Direct Sales"),
    "new business development":         ("Sales & Business Development", "Partnerships & Alliances"),
    "new business":                     ("Sales & Business Development", "Partnerships & Alliances"),
    "business development":             ("Sales & Business Development", "Partnerships & Alliances"),
    "pre-sales":                        ("Sales & Business Development", "Sales Operations"),
    "channel & partners":               ("Sales & Business Development", "Partnerships & Alliances"),
    "sales operations":                 ("Sales & Business Development", "Sales Operations"),
    "commercial":                       ("Sales & Business Development", "Sales Operations"),
    "public sector":                    ("Sales & Business Development", "Direct Sales"),
    "government":                       ("Sales & Business Development", "Direct Sales"),
    "government & public sector":       ("Sales & Business Development", "Direct Sales"),
    "public sector & government":       ("Sales & Business Development", "Direct Sales"),
    "defence":                          ("Sales & Business Development", "Direct Sales"),
    "defence & public sector":          ("Sales & Business Development", "Direct Sales"),
    "healthcare sector":                ("Sales & Business Development", "Direct Sales"),
    "financial services sector":        ("Sales & Business Development", "Direct Sales"),
    "enterprise accounts":              ("Sales & Business Development", "Direct Sales"),
    "mid-market":                       ("Sales & Business Development", "Direct Sales"),
    "alliances":                        ("Sales & Business Development", "Partnerships & Alliances"),
    # ── Marketing sub-depts ─────────────────────────────────────────────
    "brand":                            ("Marketing", "Brand & Creative"),
    "brand & communications":           ("Marketing", "Brand & Creative"),
    "brand management":                 ("Marketing", "Brand & Creative"),
    "digital marketing":                ("Marketing", "Performance & Digital Marketing"),
    "digital & performance marketing":  ("Marketing", "Performance & Digital Marketing"),
    "performance marketing":            ("Marketing", "Performance & Digital Marketing"),
    "social media":                     ("Marketing", "Performance & Digital Marketing"),
    "market research":                  ("Marketing", "Market Research & Insights"),
    "consumer insights":                ("Marketing", "Market Research & Insights"),
    "product marketing":                ("Marketing", "Product Marketing"),
    "content":                          ("Marketing", "Brand & Creative"),
    "creative":                         ("Marketing", "Brand & Creative"),
    "events":                           ("Marketing", "Field & Event Marketing"),
    "trade marketing":                  ("Marketing", "Field & Event Marketing"),
    # ── Corporate Communications → Marketing sub-dept ───────────────────
    "corporate communications":         ("Marketing", "Corporate Communications"),
    "communications":                   ("Marketing", "Corporate Communications"),
    "internal communications":          ("Marketing", "Corporate Communications"),
    # ── Corporate Communications & Public Affairs sub-depts ──────────────
    "public relations":                 ("Corporate Communications & Public Affairs", "Corporate Communications"),
    "pr":                               ("Corporate Communications & Public Affairs", "Corporate Communications"),
    "external affairs":                 ("Corporate Communications & Public Affairs", "Public Affairs & Government Relations"),
    "public affairs":                   ("Corporate Communications & Public Affairs", "Public Affairs & Government Relations"),
    "government relations":             ("Corporate Communications & Public Affairs", "Public Affairs & Government Relations"),
    "media relations":                  ("Corporate Communications & Public Affairs", "Corporate Communications"),
    # ── Customer Success & Service sub-depts ─────────────────────────────
    "customer experience":              ("Customer Success & Service", "Customer Experience"),
    "customer success":                 ("Customer Success & Service", "Customer Success Management"),
    "customer support":                 ("Customer Success & Service", "Customer Support"),
    "client relations":                 ("Customer Success & Service", "Customer Success Management"),
    "client success":                   ("Customer Success & Service", "Customer Success Management"),
    "onboarding":                       ("Customer Success & Service", "Customer Success Management"),
    "renewals":                         ("Customer Success & Service", "Customer Success Management"),
    "technical support":                ("Customer Success & Service", "Customer Support"),
    # ── Finance & Accounting sub-depts ──────────────────────────────────
    "treasury":                         ("Finance & Accounting", "Treasury"),
    "internal audit":                   ("Finance & Accounting", "Internal Audit"),
    "audit & assurance":                ("Finance & Accounting", "Internal Audit"),
    "audit":                            ("Finance & Accounting", "Internal Audit"),
    "fp&a":                             ("Finance & Accounting", "FP&A"),
    "financial planning & analysis":    ("Finance & Accounting", "FP&A"),
    "tax":                              ("Finance & Accounting", "Financial Operations"),
    "accounting":                       ("Finance & Accounting", "Financial Operations"),
    "investor relations":               ("Finance & Accounting", "Financial Operations"),
    "corporate finance":                ("Finance & Accounting", "FP&A"),
    "payroll":                          ("Finance & Accounting", "Financial Operations"),
    "financial operations":             ("Finance & Accounting", "Financial Operations"),
    "control & reporting":              ("Finance & Accounting", "Control & Financial Reporting"),
    "financial reporting":              ("Finance & Accounting", "Control & Financial Reporting"),
    # ── Human Resources sub-depts (aligned with reference) ──────────────
    "talent acquisition":               ("Human Resources", "Talent Acquisition"),
    "recruitment":                      ("Human Resources", "Talent Acquisition"),
    "talent management":                ("Human Resources", "Talent Acquisition"),
    "learning & development":           ("Human Resources", "Learning & Development"),
    "l&d":                              ("Human Resources", "Learning & Development"),
    "training":                         ("Human Resources", "Learning & Development"),
    "compensation & benefits":          ("Human Resources", "Total Rewards"),
    "total rewards":                    ("Human Resources", "Total Rewards"),
    "hr business partnering":           ("Human Resources", "HR Business Partners"),
    "people analytics":                 ("Human Resources", "People Operations & HR Systems"),
    "diversity & inclusion":            ("Human Resources", "Employee Relations & DE&I"),
    "dei":                              ("Human Resources", "Employee Relations & DE&I"),
    "people operations":                ("Human Resources", "People Operations & HR Systems"),
    "employee relations":               ("Human Resources", "Employee Relations & DE&I"),
    # ── Information Technology sub-depts (per reference L1) ─────────────
    "data & analytics":                 ("Information Technology", "Data & Enterprise Analytics"),
    "data engineering":                 ("Information Technology", "Data & Enterprise Analytics"),
    "data science":                     ("Information Technology", "Data & Enterprise Analytics"),
    "analytics":                        ("Information Technology", "Data & Enterprise Analytics"),
    "cybersecurity":                    ("Information Technology", "Cybersecurity"),
    "information security":             ("Information Technology", "Cybersecurity"),
    "cyber security":                   ("Information Technology", "Cybersecurity"),
    "it infrastructure":                ("Information Technology", "Infrastructure & Cloud"),
    "infrastructure":                   ("Information Technology", "Infrastructure & Cloud"),
    "cloud":                            ("Information Technology", "Infrastructure & Cloud"),
    "enterprise architecture":          ("Information Technology", "Infrastructure & Cloud"),
    "enterprise applications":          ("Information Technology", "Enterprise Applications"),
    "erp":                              ("Information Technology", "Enterprise Applications"),
    "it support":                       ("Information Technology", "IT Support & Service Delivery"),
    "it service delivery":              ("Information Technology", "IT Support & Service Delivery"),
    "support services":                 ("Information Technology", "IT Support & Service Delivery"),
    "digital":                          ("Information Technology", "Data & Enterprise Analytics"),
    # ── Engineering sub-depts (per reference L1) ────────────────────────
    "software engineering":             ("Engineering", "Software Engineering"),
    "software development":             ("Engineering", "Software Engineering"),
    "application development":          ("Engineering", "Software Engineering"),
    "devops":                           ("Engineering", "Platform & Infrastructure Engineering"),
    "platform engineering":             ("Engineering", "Platform & Infrastructure Engineering"),
    "site reliability":                 ("Engineering", "Platform & Infrastructure Engineering"),
    "hardware engineering":             ("Engineering", "Hardware & Systems Engineering"),
    "systems engineering":              ("Engineering", "Hardware & Systems Engineering"),
    "quality engineering":              ("Engineering", "Quality Engineering"),
    "qa":                               ("Engineering", "Quality Engineering"),
    # ── R&D sub-depts ───────────────────────────────────────────────────
    "research":                         ("Research & Development", "Research"),
    "innovation":                       ("Research & Development", "Innovation & IP Management"),
    "r&d":                              ("Research & Development", "Research"),
    "product development":              ("Research & Development", "Product Development"),
    # ── Operations sub-depts ────────────────────────────────────────────
    # Insurance / financial-services operations
    "actuarial":                        ("Operations", "Actuarial"),
    "underwriting":                     ("Operations", "Underwriting"),
    "claims":                           ("Operations", "Claims Management"),
    "claims management":                ("Operations", "Claims Management"),
    "claims & operations":              ("Operations", "Claims Management"),
    "reinsurance":                      ("Operations", "Reinsurance"),
    "health, safety & environment":     ("Operations", "Quality Assurance & EHS"),
    "hse":                              ("Operations", "Quality Assurance & EHS"),
    "ehs":                              ("Operations", "Quality Assurance & EHS"),
    "health & safety":                  ("Operations", "Quality Assurance & EHS"),
    "quality & compliance":             ("Operations", "Quality Assurance & EHS"),
    "quality assurance":                ("Operations", "Quality Assurance & EHS"),
    "quality":                          ("Operations", "Quality Assurance & EHS"),
    "shared services":                  ("Operations", "Supply Chain Management"),
    "service delivery":                 ("Operations", "Supply Chain Management"),
    "production":                       ("Operations", "Manufacturing & Production"),
    "manufacturing eng.":               ("Operations", "Manufacturing & Production"),
    "maintenance":                      ("Operations", "Manufacturing & Production"),
    "reliability engineering":          ("Operations", "Manufacturing & Production"),
    # ── Facilities, Real Estate & Workplace sub-depts ────────────────────
    "facilities management":            ("Facilities, Real Estate & Workplace", "Facilities Management"),
    "facilities":                       ("Facilities, Real Estate & Workplace", "Facilities Management"),
    "real estate":                      ("Facilities, Real Estate & Workplace", "Real Estate & Workplace Strategy"),
    "facilities & real estate":         ("Facilities, Real Estate & Workplace", "Facilities Management"),
    "real estate & facilities":         ("Facilities, Real Estate & Workplace", "Facilities Management"),
    "corporate real estate":            ("Facilities, Real Estate & Workplace", "Real Estate & Workplace Strategy"),
    "workplace":                        ("Facilities, Real Estate & Workplace", "Real Estate & Workplace Strategy"),
    "workplace & facilities":           ("Facilities, Real Estate & Workplace", "Facilities Management"),
    # ── Investment Banking sub-depts ────────────────────────────────────
    "m&a advisory":                     ("Investment Banking", "M&A Advisory"),
    "mergers & acquisitions":           ("Investment Banking", "M&A Advisory"),
    "capital markets":                  ("Investment Banking", "Capital Markets"),
    "debt capital markets":             ("Investment Banking", "Capital Markets"),
    "equity capital markets":           ("Investment Banking", "Capital Markets"),
    "leveraged finance":                ("Investment Banking", "Capital Markets"),
    # ── Sales & Trading sub-depts ───────────────────────────────────────
    "fixed income":                     ("Sales & Trading", "Fixed Income"),
    "fixed income, currencies & commodities": ("Sales & Trading", "Fixed Income"),
    "equities":                         ("Sales & Trading", "Equities"),
    "equity sales":                     ("Sales & Trading", "Equities"),
    "equity trading":                   ("Sales & Trading", "Equities"),
    "prime brokerage":                  ("Sales & Trading", "Prime Brokerage"),
    "institutional securities":         ("Sales & Trading", "Institutional Securities"),
    # ── Wealth Management sub-depts ─────────────────────────────────────
    "private banking":                  ("Wealth Management", "Private Banking"),
    "private banking & wealth management": ("Wealth Management", "Private Banking"),
    "private wealth management":        ("Wealth Management", "Private Wealth"),
    "global wealth management":         ("Wealth Management", "Client Advisory"),
    # ── Investment Management sub-depts ─────────────────────────────────
    "asset management":                 ("Investment Management", "Asset Management"),
    "global investment management":     ("Investment Management", "Asset Management"),
}


def _canonical_dept(dept_primary: str, layer: int) -> str:
    """
    Return a canonical primary department name.

    Layer overrides take priority:
      L0  → Board of Management (always)
      L1  → Executive Management (C-Suite)

    For L2+, only applies EXACT-match lookups (case-insensitive):
      1. Explicit remap in _DEPT_REMAP  (e.g. "it" → "Technology")
      2. Already a canonical primary name → return as-is
      3. Non-empty unknown name → pass through unchanged
      4. Empty → "Operations"

    ⚠  No partial/substring matching — that caused "Professional Services"
    to wrongly match "service" → "Customer Experience", and many similar
    false positives.  The NLP engine (classify_dept_from_text) handles
    compound or unfamiliar names before this function is called.
    """
    # Layer-based hard overrides (most important — independent of NLP dept)
    # Per Global_Designation_Hierarchy.xlsx:
    #   G0 = Board Chairman (apex)   → Board of Management
    #   G1 = Vice Chair / Comm Chair → Board of Management  (if dept says so)
    #        OR C-Suite              → Executive Management
    #   G2 = Regular NEDs / INEDs   → Board of Management  (if dept says so)
    #        OR EVP-level execs      → their functional dept
    _BOD_DEPT_NAMES = frozenset({"board of management", "board of directors", "board"})
    if layer == 0:
        return "Board of Management"
    if layer == 1:
        if dept_primary.strip().lower() in _BOD_DEPT_NAMES:
            return "Board of Management"
        # True C-Suite (CEO, CFO, COO, CTO, CMO …) → Executive Management
        return "Executive Management"
    if layer == 2:
        # Regular NEDs / INEDs (board members at layer 2) stay in BOD;
        # EVP-level functional execs fall through to functional dept logic below.
        if dept_primary.strip().lower() in _BOD_DEPT_NAMES:
            return "Board of Management"
    # L2 (EVP / MD) and below: use actual functional department.
    # An "EVP of Finance" belongs in Finance, not Executive Management.

    # ── Exact lookup in the remap table (case-insensitive) ───────────────
    key = dept_primary.strip().lower()
    if key in _DEPT_REMAP:
        return _DEPT_REMAP[key]

    # ── Already a recognised canonical primary name ───────────────────────
    key_lower = key
    for canon in _CANONICAL_PRIMARY:
        if key_lower == canon.lower():
            return canon

    # ── Unknown but non-empty — preserve it (the analyst wrote something real)
    if dept_primary.strip():
        return dept_primary.strip()

    # ── Truly empty dept → last resort
    return "Operations"


def _canonical_subdept(dept_secondary: str) -> str:
    """
    Normalize a dept_secondary value using _SUBDEPT_REMAP.

    Returns:
      - The remapped canonical sub-dept name (e.g. "Account Management")
      - "" if the sub-dept should be merged into the parent (e.g. "Programme")
      - The original value stripped if it's not in the remap (pass-through)
    """
    if not dept_secondary or not dept_secondary.strip():
        return ""
    key = dept_secondary.strip().lower()
    if key in _SUBDEPT_REMAP:
        return _SUBDEPT_REMAP[key]   # "" means merge into parent
    # No partial/substring matching — pass unknown sub-dept names through unchanged.
    # Partial matching caused "Professional Services" → "" (merge), etc.
    return dept_secondary.strip()


# Department display order — lower number = shown first
DEPT_PRIMARY_ORDER: dict[str, int] = {
    # ── Leadership (always first two levels) ─────────────────────────
    "board of management":      0,
    "board of directors":       0,
    "board":                    0,
    "executive management":     1,
    "c-suite":                  1,
    # ── Governance / Support functions ───────────────────────────────
    "finance & accounting":    10,
    "human resources":         11,
    "people & culture":        11,
    "legal & compliance":      12,
    "risk management":         13,
    # ── Technology / Data ────────────────────────────────────────────
    "engineering / it":        14,
    "product management":      17,
    # ── Strategy / Transformation ────────────────────────────────────
    "strategy":                18,
    "corporate development":   19,
    # ── Operations ───────────────────────────────────────────────────
    "operations":              20,
    "manufacturing":           21,
    "supply chain":            22,
    "procurement":             23,
    # ── Revenue / Customer-facing ────────────────────────────────────
    "sales":                   24,
    "marketing":               25,
    "customer success":        26,
    # ── ESG ──────────────────────────────────────────────────────────
    "sustainability":          30,
    # ── R&D / Specialist ─────────────────────────────────────────────
    "research & development":  32,
    "actuarial":               35,
    "underwriting":            36,
    "claims":                  37,
    "investment management":   38,
}


SECTOR_COLORS = {
    "Automotive": "#F59E0B",
    "Govt":       "#3B82F6",
    "NGO":        "#10B981",
    "Startup":    "#8B5CF6",
    "Public":     "#06B6D4",
    "Private":    "#64748B",
}


class OrganogramDAG:
    """Directed Acyclic Graph representing the full organogram."""

    def __init__(self, company_name: str = "Organization"):
        self.G = nx.DiGraph()
        self.company_name = company_name
        self._ensure_root()

    def _ensure_root(self):
        root_id = "root_global"
        if root_id not in self.G:
            self.G.add_node(root_id, **{
                "node_id":   root_id,
                "node_type": NODE_GLOBAL,
                "label":     self.company_name,
                "layer":     -1,
                "sector":    "All",
                "color":     "#1E293B",
                "is_ghost":  False,
                "expanded":  False,
                "metadata":  {},
            })
        return root_id

    def _node_id(self, *parts: str) -> str:
        clean = [re.sub(r"[^a-z0-9_]", "_", p.lower().strip())
                 for p in parts if p]
        return "__".join(clean)

    def _ensure_node(self, _nid: str, **attrs) -> str:
        if _nid not in self.G:
            self.G.add_node(_nid, **attrs)
        return _nid

    def _ensure_edge(self, parent: str, child: str):
        if not self.G.has_edge(parent, child):
            self.G.add_edge(parent, child)

    # Governance hierarchy constants (checked in ensure_department)
    _BOD_NAMES: frozenset = frozenset({
        "board of management", "board of directors", "board",
    })
    _EM_NAMES: frozenset = frozenset({
        "executive management", "c-suite", "ceo office",
    })

    # ─── Build department layers ──────────────
    def ensure_department(self, region: str, sector: str,
                           dept_p: str, dept_s: str, dept_t: str
                           ) -> str:
        """
        Create department hierarchy nodes (1–3 levels) and return the
        deepest created node ID (leaf). Redundant nodes are skipped:
        - Secondary is skipped when empty or identical to primary.
        - Tertiary is skipped when empty or identical to secondary/primary.

        Governance chain (enforced via parent selection):
          root_global  [invisible anchor]
            └── Board of Management   (BOD)
                  └── Executive Management  (EM, if BOD exists)
                        └── Finance / HR / Tech / …  (functional depts)

        Region is stored only as metadata on person cards — not as a
        structural node — so regional orgs merge into one dept branch.
        """
        color = SECTOR_COLORS.get(sector, "#64748B")

        # ── Choose correct parent for primary dept ───────────────────────
        dp_lower = dept_p.lower()
        if dp_lower in self._BOD_NAMES:
            # BOD is a direct child of root
            parent_id = "root_global"
        elif dp_lower in self._EM_NAMES:
            # EM sits under BOD when BOD dept already exists
            bod_id    = self._node_id("dept", "Board of Management")
            parent_id = bod_id if bod_id in self.G else "root_global"
        else:
            # All functional depts sit under EM → BOD → root (whichever exists)
            em_id     = self._node_id("dept", "Executive Management")
            bod_id    = self._node_id("dept", "Board of Management")
            if em_id in self.G:
                parent_id = em_id
            elif bod_id in self.G:
                parent_id = bod_id
            else:
                parent_id = "root_global"

        # ── Primary dept (always created) ───────────────────────────────
        dp_id = self._node_id("dept", dept_p)
        self._ensure_node(dp_id, **{
            "node_id":   dp_id,
            "node_type": NODE_DEPT_P,
            "label":     dept_p,
            "layer":     1,
            "sector":    sector,
            "color":     color,
            "is_ghost":  False,
            "expanded":  False,
            "metadata":  {"dept_primary": dept_p},
        })
        self._ensure_edge(parent_id, dp_id)
        leaf = dp_id

        # ── Secondary dept (skip if empty or same as primary) ───────────
        effective_s = dept_s if (dept_s and dept_s.lower() != dept_p.lower()) else ""
        if effective_s:
            ds_id = self._node_id("dept", dept_p, dept_s)
            self._ensure_node(ds_id, **{
                "node_id":   ds_id,
                "node_type": NODE_DEPT_S,
                "label":     dept_s,
                "layer":     2,
                "sector":    sector,
                "color":     color,
                "is_ghost":  False,
                "expanded":  False,
                "metadata":  {"dept_primary": dept_p, "dept_secondary": dept_s},
            })
            self._ensure_edge(dp_id, ds_id)
            leaf = ds_id

            # ── Tertiary dept (skip if empty or mirrors secondary/primary) ──
            effective_t = (
                dept_t
                if (dept_t
                    and dept_t.lower() != dept_s.lower()
                    and dept_t.lower() != dept_p.lower())
                else ""
            )
            if effective_t:
                dt_id = self._node_id("dept", dept_p, dept_s, dept_t)
                self._ensure_node(dt_id, **{
                    "node_id":   dt_id,
                    "node_type": NODE_DEPT_T,
                    "label":     dept_t,
                    "layer":     3,
                    "sector":    sector,
                    "color":     color,
                    "is_ghost":  False,
                    "expanded":  False,
                    "metadata":  {"dept_tertiary": dept_t},
                })
                self._ensure_edge(ds_id, dt_id)
                leaf = dt_id

        return leaf

    # ─── Department sort key (for ordered get_subtree output) ────────
    def _dept_sort_key(self, nid: str) -> tuple:
        attrs = self.G.nodes.get(nid, {})
        node_type = attrs.get("node_type", "")
        label = attrs.get("label", "").lower().rstrip(" ✦")

        if attrs.get("is_ghost"):
            return (900, label)

        if node_type == NODE_DEPT_P:
            return (DEPT_PRIMARY_ORDER.get(label, 50), label)

        if node_type in (NODE_DEPT_S, NODE_DEPT_T):
            return (50, label)

        if node_type == NODE_PERSON:
            return (100 + attrs.get("layer", 99), label)

        return (500, label)

    # ─── Insert person with ghost-node bridging ─
    def insert_person(self, rec: ClassifiedRecord):
        # ── Step 1: Initial canonical dept using NLP layer ───────────────
        dept_p = _canonical_dept(rec.dept_primary, rec.layer)

        # ── Step 2: BOD 3-layer re-classification ────────────────────────
        # When CSV data is uploaded, the NLP classifier assigns G0 (layer 0)
        # to ALL board members — regular NEDs, committee chairs, and the
        # chairman alike.  Apply the 3-tier BOD hierarchy from
        # Global_Designation_Hierarchy.xlsx so ExecPanel renders correctly:
        #   layer 0 → Chairman (board apex)
        #   layer 1 → Vice Chair / Committee Chairs (senior board roles)
        #   layer 2 → Regular NEDs / INEDs
        # Also: an executive who is also a board member (executive director)
        # should remain in Executive Management, not Board of Management.
        layer = rec.layer
        board_sub = ""
        designation = rec.designation or ""

        # ── CSV employee guard — only LLM-injected people in BOD/EM panels ──
        # Uploaded employees whose titles happen to look like board/exec roles
        # (e.g. "Independent Non-executive Director" at a bank, "Chief of Staff")
        # should NOT appear in the Board of Directors or Executive Management
        # panels.  Only people injected via llm_fetch_leadership belong there.
        # True C-suite from the CSV (CEO, CFO, COO …) are allowed through so
        # they still appear in the EM panel.
        _is_llm_injected = rec.nlp_method in ("llm_leadership_web", "llm_leadership_ai")
        if not _is_llm_injected:
            _fallback_dept = (
                rec.dept_primary.strip()
                if rec.dept_primary and rec.dept_primary.strip().lower()
                   not in ("board of management", "board of directors",
                           "board", "executive management")
                else "Strategy & Corporate Development"
            )
            if dept_p == "Board of Management":
                # Redirect board-titled CSV employee to their functional dept
                dept_p = _canonical_dept(_fallback_dept, 3)
                layer  = 3
            elif dept_p == "Executive Management" and layer == 1:
                # Only genuine C-suite stays in EM; others go to functional dept
                if not _is_csuite(designation):
                    dept_p = _canonical_dept(_fallback_dept, 3)
                    layer  = 3

        # ── Regional exec demotion (CSV path) ────────────────────────────
        # When NLP classifies "Australia CEO" as G1 (layer 1), keep them in
        # Executive Management but at layer 2 so they don't hijack the apex.
        if dept_p == "Executive Management" and layer == 1 and _is_regional_exec(designation):
            layer = 2

        if dept_p == "Board of Management":
            if _is_ceo(designation) and not _is_board_chairman(designation):
                # Executive director who is also CEO → Executive Management
                dept_p = "Executive Management"
                layer  = 2 if _is_regional_exec(designation) else 1
            elif _is_board_chairman(designation):
                layer     = 0
                board_sub = _board_sub_role(designation)
            elif _is_vice_chair(designation) or _is_committee_chair(designation):
                layer     = 1
                board_sub = _board_sub_role(designation)
            else:
                layer     = 2   # Regular NED / INED
                board_sub = _board_sub_role(designation)

        dept_s = _canonical_subdept(rec.dept_secondary if layer > 2 else "")
        dept_t = rec.dept_tertiary  if layer > 2 else ""
        # If sub-dept remapped to "" (merge into parent) treat as no sub-dept
        if not dept_s:
            dept_t = ""

        # ── Elevate compound dept names to correct (primary, secondary) ──
        # e.g. dept_p="Customer Experience" → dept_p="Marketing", dept_s="Customer Experience"
        #      dept_p="Sales & Account Management" → dept_p="Sales", dept_s="Account Management"
        if layer > 2 and not dept_s:
            elevate_key = dept_p.lower()
            if elevate_key in _DEPT_ELEVATE:
                dept_p, dept_s = _DEPT_ELEVATE[elevate_key]

        leaf_dept_id = self.ensure_department(
            rec.region, rec.sector,
            dept_p, dept_s, dept_t
        )

        person_id = rec.id
        metadata: dict = {
            "designation":    rec.designation,
            "company":        rec.company,
            "linkedin_url":   rec.linkedin_url,
            "location":       rec.location,
            "country":        getattr(rec, "country", "") or "",
            "region":         getattr(rec, "region", "") or "",
            "dept_primary":   dept_p,
            "dept_secondary": dept_s,
            "nlp_confidence": round(getattr(rec, "nlp_confidence", 0.0), 2),
            "nlp_industry":   getattr(rec, "nlp_industry", "generic"),
            "nlp_method":     getattr(rec, "nlp_method", "fallback"),
        }
        if board_sub:
            metadata["board_sub_role"] = board_sub

        self._ensure_node(person_id, **{
            "node_id":    person_id,
            "node_type":  NODE_PERSON,
            "label":      rec.full_name,
            "layer":      layer,          # ← re-classified BOD layer
            "sector":     rec.sector,
            "color":      SECTOR_COLORS.get(rec.sector, "#64748B"),
            "is_ghost":   False,
            "expanded":   False,
            "metadata":   metadata,
        })

        # Build ghost chain using the re-classified layer.
        # Temporarily override rec.layer so _insert_with_ghosts uses the
        # correct layer for ghost chain depth calculation.
        _orig_layer = rec.layer
        rec.layer   = layer
        self._insert_with_ghosts(leaf_dept_id, person_id, rec)
        rec.layer   = _orig_layer

    # Titles that should NEVER be used as pass-through parents in ghost chains.
    # A Chief of Staff, for example, is a coordinator role — other C-Suite execs
    # do not report through the CoS even if they share the same layer.
    _EXCLUDED_CHAIN_TITLES = re.compile(
        r"\b(chief\s+of\s+staff|cos\b|executive\s+assistant\s+to|"
        r"pa\s+to\s+|personal\s+assistant\s+to|office\s+of\s+the\s+ceo|"
        r"deputy\s+chief\s+of\s+staff)\b",
        re.IGNORECASE,
    )

    def _real_person_at_layer(self, parent_id: str, target_layer: int) -> Optional[str]:
        """
        Return the node_id of a real (non-ghost) person node that is a direct
        child of *parent_id* at *target_layer*, or None if not found.

        Excludes coordinator/staff roles (Chief of Staff, EA to CEO, etc.) that
        should not appear as reporting managers for peers in the hierarchy.

        Used so ghost chains route through actual senior people rather than
        creating parallel phantom chains alongside them.
        """
        for child in self.G.successors(parent_id):
            attrs = self.G.nodes.get(child, {})
            if (attrs.get("node_type") == NODE_PERSON
                    and attrs.get("layer") == target_layer
                    and not attrs.get("is_ghost", False)):
                # Skip coordinator/staff roles — they're not line managers
                designation = str(attrs.get("metadata", {}).get("designation", ""))
                if designation and self._EXCLUDED_CHAIN_TITLES.search(designation):
                    continue
                return child
        return None

    def _insert_with_ghosts(self, dept_node: str,
                             person_node: str,
                             rec: ClassifiedRecord):
        """
        Bridge the department node (depth 3) to the person node by inserting
        Ghost Nodes for any missing intermediate layers.

        Key rule: if a real person already occupies an intermediate layer in
        the chain (e.g. a Director at L5 when inserting a Manager at L7),
        route THROUGH that person rather than creating a parallel ghost at
        the same layer.  This ensures Manager reports to Director, not to
        a phantom "Director / Head ✦" node alongside the real Director.
        """
        person_layer = rec.layer
        start_layer  = 4   # first employee layer after dept tree

        if person_layer <= start_layer:
            self._ensure_edge(dept_node, person_node)
            return

        GHOST_LABELS = {
            0:  "Board of Directors",
            1:  "Executive Management",
            2:  "SVP / EVP",
            3:  "VP / Divisional Head",
            4:  "Senior Director",
            5:  "Director / Head",
            6:  "Senior Manager",
            7:  "Manager / Lead",
            8:  "Senior Contributor",
            9:  "Contributor",
            10: "Entry Level",
        }

        prev = dept_node
        for ghost_layer in range(start_layer, person_layer):
            # ── Prefer a real senior person at this layer over a ghost ───
            real = self._real_person_at_layer(prev, ghost_layer)
            if real:
                prev = real
                continue   # no ghost needed — chain through the real person

            ghost_id = self._node_id(
                "ghost", dept_node,
                rec.dept_primary, rec.dept_secondary,
                str(ghost_layer)
            )
            if ghost_id not in self.G:
                label = GHOST_LABELS.get(ghost_layer,
                                         f"Layer {ghost_layer} — Pending Data")
                self._ensure_node(ghost_id, **{
                    "node_id":   ghost_id,
                    "node_type": NODE_GHOST,
                    "label":     f"{label} ✦",
                    "layer":     ghost_layer,
                    "sector":    rec.sector,
                    "color":     "#374151",
                    "is_ghost":  True,
                    "expanded":  False,
                    "metadata": {
                        "reason": "Auto-generated placeholder — data pending",
                        "dept_primary": rec.dept_primary,
                    },
                })
                self._ensure_edge(prev, ghost_id)
            prev = ghost_id

        self._ensure_edge(prev, person_node)

    # ─── Governance edge repair ───────────────────────────────────────
    def repair_governance_edges(self) -> None:
        """
        Re-enforce the canonical governance hierarchy:
            root_global → BOD → EM → functional depts

        This is needed because the LLM enrichment thread creates BOD/EM
        AFTER the uploaded records were already inserted.  At upload time,
        EM and functional depts fall back to root_global as their parent
        (since BOD/EM don't exist yet).  Once enrichment finishes, stale
        root_global edges must be removed and correct parent edges added.

        Safe to call multiple times — every operation is idempotent.
        """
        G   = self.G
        bod_id = self._node_id("dept", "Board of Management")
        em_id  = self._node_id("dept", "Executive Management")
        bod_exists = bod_id in G
        em_exists  = em_id  in G

        _BOD_LABELS = frozenset({
            "board of management", "board of directors", "board",
        })
        _EM_LABELS = frozenset({
            "executive management", "c-suite", "ceo office",
        })

        # ── Step 1: EM should sit under BOD when both exist ──────────────
        if bod_exists and em_exists:
            if G.has_edge("root_global", em_id):
                G.remove_edge("root_global", em_id)
            if not G.has_edge(bod_id, em_id):
                G.add_edge(bod_id, em_id)

        # ── Step 2: Functional depts should sit under EM ─────────────────
        # Only re-parent when EM actually exists.  If only BOD is present
        # (no EM scraped yet), leave functional depts under root_global —
        # pulling them under BOD would mix board members with dept cards.
        if em_exists:
            _reserved = _BOD_LABELS | _EM_LABELS

            # Walk root_global children → move functional depts to EM
            for child_id in list(G.successors("root_global")):
                if child_id == bod_id:
                    continue   # BOD stays directly under root_global
                attrs = G.nodes.get(child_id, {})
                label = str(attrs.get("label", "")).lower()
                if (attrs.get("node_type") == NODE_DEPT_P
                        and label not in _reserved):
                    G.remove_edge("root_global", child_id)
                    if not G.has_edge(em_id, child_id):
                        G.add_edge(em_id, child_id)

            # Walk BOD children — functional depts that landed under BOD
            # (happens when BOD existed but EM didn't yet) must move to EM
            if bod_exists:
                for child_id in list(G.successors(bod_id)):
                    if child_id == em_id:
                        continue   # EM stays under BOD
                    attrs = G.nodes.get(child_id, {})
                    label = str(attrs.get("label", "")).lower()
                    if (attrs.get("node_type") == NODE_DEPT_P
                            and label not in _reserved):
                        G.remove_edge(bod_id, child_id)
                        if not G.has_edge(em_id, child_id):
                            G.add_edge(em_id, child_id)

    # ─── Recursive CTE-style drill-down ──────
    def get_subtree(self, node_id: str, max_depth: int = 20) -> dict:
        """
        Returns a nested dict representing the subtree rooted at node_id,
        up to max_depth levels deep — like a recursive CTE.
        Children at each level are sorted:
          Board of Directors → Executive Management → CEO Office →
          functional depts (Finance, HR, IT …) → people (by layer) → ghosts.
        """
        if node_id not in self.G:
            return {}

        def recurse(nid: str, depth: int) -> dict:
            attrs = dict(self.G.nodes[nid])
            raw_children = list(self.G.successors(nid))
            if depth >= max_depth:
                return {**attrs, "children": [], "has_more": len(raw_children) > 0}
            children = sorted(raw_children, key=self._dept_sort_key)
            child_nodes = [recurse(c, depth + 1) for c in children]
            return {**attrs, "children": child_nodes}

        return recurse(node_id, 0)

    def get_flat_nodes(self) -> list[dict]:
        return [dict(self.G.nodes[n]) for n in self.G.nodes]

    def get_edges(self) -> list[dict]:
        return [{"source": u, "target": v}
                for u, v in self.G.edges]

    def stats(self) -> dict:
        return {
            "total_nodes": self.G.number_of_nodes(),
            "total_edges": self.G.number_of_edges(),
            "people_nodes": sum(
                1 for n, d in self.G.nodes(data=True)
                if d.get("node_type") == NODE_PERSON
            ),
            "ghost_nodes": sum(
                1 for n, d in self.G.nodes(data=True)
                if d.get("is_ghost")
            ),
            "max_depth": self._max_depth(),
        }

    def _max_depth(self) -> int:
        try:
            return nx.dag_longest_path_length(self.G)
        except Exception:
            return -1


# ─────────────────────────────────────────────
# SQLITE PERSISTENCE
# ─────────────────────────────────────────────

class OrganogramDB:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id   TEXT PRIMARY KEY,
            node_type TEXT,
            label     TEXT,
            layer     INTEGER,
            sector    TEXT,
            color     TEXT,
            is_ghost  INTEGER,
            metadata  TEXT
        );

        CREATE TABLE IF NOT EXISTS edges (
            parent_id TEXT,
            child_id  TEXT,
            PRIMARY KEY (parent_id, child_id),
            FOREIGN KEY (parent_id) REFERENCES nodes(node_id),
            FOREIGN KEY (child_id)  REFERENCES nodes(node_id)
        );

        CREATE INDEX IF NOT EXISTS idx_edges_parent ON edges(parent_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(node_type);
        CREATE INDEX IF NOT EXISTS idx_nodes_layer  ON nodes(layer);
        """)
        self.conn.commit()

    def upsert_dag(self, dag: OrganogramDAG):
        for node_id, attrs in dag.G.nodes(data=True):
            self.conn.execute("""
                INSERT OR REPLACE INTO nodes
                (node_id, node_type, label, layer, sector, color, is_ghost, metadata)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                node_id,
                attrs.get("node_type"),
                attrs.get("label"),
                attrs.get("layer"),
                attrs.get("sector"),
                attrs.get("color"),
                1 if attrs.get("is_ghost") else 0,
                json.dumps(attrs.get("metadata", {})),
            ))

        for u, v in dag.G.edges:
            self.conn.execute("""
                INSERT OR IGNORE INTO edges (parent_id, child_id)
                VALUES (?,?)
            """, (u, v))

        self.conn.commit()

    def recursive_subtree(self, root_id: str) -> list[dict]:
        """
        True recursive CTE — returns all descendants of root_id.
        """
        cur = self.conn.execute("""
            WITH RECURSIVE subtree(node_id, depth) AS (
                SELECT ?, 0
                UNION ALL
                SELECT e.child_id, subtree.depth + 1
                FROM edges e
                JOIN subtree ON subtree.node_id = e.parent_id
                WHERE subtree.depth < 20
            )
            SELECT n.*, s.depth
            FROM subtree s
            JOIN nodes n ON n.node_id = s.node_id
            ORDER BY s.depth, n.label
        """, (root_id,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            r["metadata"] = json.loads(r.get("metadata") or "{}")
        return rows

    def search(self, query: str) -> list[dict]:
        q = f"%{query.lower()}%"
        cur = self.conn.execute("""
            SELECT * FROM nodes
            WHERE lower(label) LIKE ?
               OR lower(sector) LIKE ?
               OR lower(node_type) LIKE ?
        """, (q, q, q))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─────────────────────────────────────────────
# BUILDER
# ─────────────────────────────────────────────

import re

def build_from_records(records: list[dict],
                       company_name: str = "Organization",
                       db_path: str = ":memory:"
                       ) -> tuple[OrganogramDAG, OrganogramDB]:
    # Determine primary domain early (needed for industry classification)
    from collections import Counter as _Counter
    _domain_counts_pre = _Counter(
        str(r.get("email_domain", "") or "").strip().lower()
        for r in records
        if r.get("email_domain")
        and str(r.get("email_domain", "")).strip().lower() not in ("nan", "none", "")
    )
    primary_domain_pre = _domain_counts_pre.most_common(1)[0][0] if _domain_counts_pre else ""
    if not primary_domain_pre and company_name:
        primary_domain_pre = _guess_domain(company_name)

    # Classify company industry before running inference engine.
    # quick=True → Claude knowledge-only, no DDG/homepage HTTP calls (fast, ~1s).
    # The background enrichment task runs full web-based classification and
    # updates the DAG root node metadata if a better result is found.
    from industry_classifier import classify_industry as _classify_industry
    detected_industry = _classify_industry(company_name, primary_domain_pre, quick=True)
    logger.info("Detected industry for '%s': %s (quick)", company_name, detected_industry or "unknown")

    engine = InferenceEngine(industry=detected_industry)
    classified = engine.classify_all(records)

    # Insert senior people first so ghost chains can route through them.
    # A Director (L5) must exist in the DAG before a Manager (L7) is inserted,
    # otherwise _insert_with_ghosts cannot find the real person at L5 and
    # creates a parallel ghost chain instead.
    classified.sort(key=lambda r: r.layer)

    dag = OrganogramDAG(company_name=company_name)
    for rec in classified:
        dag.insert_person(rec)

    # Repair any stale parent edges from the initial insertion pass
    # (happens when L0/L1 records appear after some L2+ records due to
    # edge cases in sort stability or data ordering)
    dag.repair_governance_edges()

    # Store industry on the root global node metadata
    if detected_industry and "root_global" in dag.G.nodes:
        meta = dict(dag.G.nodes["root_global"].get("metadata", {}))
        meta["industry"] = detected_industry
        dag.G.nodes["root_global"]["metadata"] = meta

    db = OrganogramDB(db_path=db_path)
    db.upsert_dag(dag)

    return dag, db, classified, primary_domain_pre, detected_industry


def _guess_domain(company_name: str) -> str:
    """
    Best-effort domain from a company name.
    'Morgan Stanley' → 'morganstanley.com'
    'JP Morgan'      → 'jpmorgan.com'
    Returns '' when the result looks too short to be reliable.
    """
    suffixes = (
        " & co", " and co", " inc", " ltd", " llc", " plc", " corp",
        " corporation", " limited", " group", " holdings", " sa", " ag",
        " gmbh", " bv", " nv", " se", " co",
    )
    name = company_name.lower().strip()
    for sfx in suffixes:
        if name.endswith(sfx):
            name = name[: -len(sfx)].strip()
            break
    slug = re.sub(r"[^a-z0-9]", "", name)
    return f"{slug}.com" if len(slug) >= 4 else ""


# ─────────────────────────────────────────────
# LLM LEADERSHIP ENRICHMENT
# ─────────────────────────────────────────────

def _name_key(name: str) -> str:
    """Normalised dedup key: first two words lowercase, letters only."""
    words = re.sub(r"[^a-z ]", "", name.lower()).split()
    return " ".join(words[:2])


# Regional/country chair qualifier — prevents "U.S. Chairman", "Asia Chairman",
# "Australia Chair" from being treated as THE board chairman.
_REGIONAL_CHAIR_RE = re.compile(
    r'\b(?:u\.?s\.?a?|uk|u\.k\.|emea|apac|apj|latam|mena|asean|gcc|'
    r'asia|europe|america|americas|africa|pacific|oceania|'
    r'australia|new\s+zealand|india|china|japan|south\s+korea|'
    r'canada|brazil|germany|france|italy|spain|mexico|'
    r'singapore|hong\s+kong|south\s+east\s+asia|anz|'
    r'regional|country|local|subsidiary|advisory)\s+'
    r'chair(?:man|woman|person)?',
    re.IGNORECASE,
)


def _is_board_chairman(title: str) -> bool:
    """
    Return True when *title* is the Chair/Chairman of the board (apex, layer 0).
    Per Global_Designation_Hierarchy.xlsx: Chairman, Non-Executive Chairman,
    Board Chair, Executive Chairman are all the board apex.
    Excludes: Vice-Chair, Deputy Chair, Committee Chairs, regional/country Chairs.
    """
    t = title.lower().strip()
    # Exclude vice/deputy/assistant chair and committee chairs
    if re.search(r'\b(vice|deputy|assistant)\b', t):
        return False
    if "committee" in t:
        return False   # "Audit Committee Chairman" → director, not THE chairman
    # Exclude regional/country chairman ("U.S. Chairman", "Asia Chair", etc.)
    if _REGIONAL_CHAIR_RE.search(t):
        return False
    # Board chairman — covers Executive Chairman, Non-Exec Chairman, etc.
    if re.search(r'\bchairman\b|\bchairwoman\b|\bchairperson\b', t):
        return True
    # "Board Chair", "Chair of the Board", "Executive Chair" (no -man/-woman suffix)
    if re.search(r"\bboard\s+chair\b", t):
        return True
    if re.search(r"\bchair\s+of\s+(?:the\s+)?board", t):
        return True
    if re.search(r"\bexecutive\s+chair\b", t):
        return True
    if t == "chair":
        return True
    return False


# Committee chair patterns — from Global_Designation_Hierarchy.xlsx §Board Committees
_COMMITTEE_CHAIR_RE = re.compile(
    r"(?:audit|compensation|remuneration|nomination|nominating|governance|"
    r"risk|technology|innovation|safety|environment|esg|sustainability|"
    r"public\s+policy|ethics|finance|investment|executive|strategy|scientific)"
    r"\s+committee",
    re.IGNORECASE,
)

_COMMITTEE_NAME_MAP: dict[str, str] = {
    "audit":        "audit_chair",
    "compensation": "comp_chair",
    "remuneration": "comp_chair",
    "nomination":   "nom_chair",
    "nominating":   "nom_chair",
    "governance":   "nom_chair",
    "risk":         "risk_chair",
    "technology":   "tech_chair",
    "innovation":   "tech_chair",
    "esg":          "esg_chair",
    "sustainability": "esg_chair",
    "environment":  "esg_chair",
    "finance":      "finance_chair",
    "investment":   "finance_chair",
}


def _is_vice_chair(title: str) -> bool:
    """
    Return True for Vice Chairman / Deputy Chairman — senior board role
    below THE Chairman, but senior to regular NEDs. Layer 1 in BOD.
    """
    t = title.lower().strip()
    return bool(re.search(r"\bvice[- ]?chair(?:man|woman|person)?\b", t)) or \
           bool(re.search(r"\bdeputy\s+chair(?:man|woman|person)?\b", t))


def _is_committee_chair(title: str) -> bool:
    """
    Return True when the title indicates the person chairs a specific
    board committee (Audit, Compensation, Nominations, Risk, Technology…).
    Layer 1 in BOD (senior director role per Excel).
    """
    t = title.lower().strip()
    if not _COMMITTEE_CHAIR_RE.search(t):
        return False
    return bool(re.search(r"\bchair(?:man|woman|person)?\b", t))


def _board_sub_role(title: str) -> str:
    """
    Classify a board member's sub-role for metadata storage.
    Used by ExecPanel.tsx to display committee badges.

    Returns one of: "chairman", "vice_chair", "lead_director",
    "<committee>_chair" (e.g. "audit_chair"), or "director".
    """
    t = title.lower().strip()
    if _is_board_chairman(title):
        return "chairman"
    if _is_vice_chair(title):
        return "vice_chair"
    if re.search(r"\b(?:lead|senior)\s+independent\s+director\b", t):
        return "lead_director"
    if re.search(r"\bpresiding\s+director\b", t):
        return "lead_director"
    if _is_committee_chair(title):
        for key, role in _COMMITTEE_NAME_MAP.items():
            if key in t:
                return role
        return "committee_chair"
    return "director"


_CSUITE_RE = re.compile(
    r"\bchief\s+(?:executive|financial|operating|technology|technical|"
    r"information|marketing|revenue|product|data|legal|risk|commercial|"
    r"strategy|digital|transformation|analytics|compliance|security|"
    r"administrative|investment|science|medical|nursing|pharmacy|actuarial|"
    r"underwriting|claims|accounting|people|talent|human\s+resources?|"
    r"customer|experience|client|growth|innovation|sustainability|"
    r"diversity|ai|artificial\s+intelligence|communications?|public\s+affairs|"
    r"supply\s+chain|procurement)\b"
    r"|\bce?[of][o]?\b"  # CEO, CFO, COO, CTO, CIO, CMO, CHRO, CRO, CPO, CDO, CLO, CSO …
    r"|\bcoo\b|\bcto\b|\bcfo\b|\bcmo\b|\bchro\b|\bcro\b|\bcpo\b|\bcdo\b|\bclo\b|\bcso\b"
    r"|\bcaio\b|\bcao\b|\bcxo\b"
    r"|\b(?:group\s+)?president$"
    r"|\bmanaging\s+partner$"
    r"|\bgroup\s+managing\s+director$"
    r"|\bchairman\s+(?:and|&)\s+managing\s+director\b|\bcmd\b",
    re.IGNORECASE,
)
_CSUITE_EXCLUDE_RE = re.compile(
    r"\b(?:deputy|vice|assistant|associate|regional|country|local|"
    r"office|team|department|dept|group|division|function|desk)\b",
    re.IGNORECASE,
)


def _is_csuite(title: str) -> bool:
    """
    Return True when *title* is a genuine C-suite / apex executive role.
    Excludes:  "Chief of Staff", titles with office/team suffixes (e.g. "CTO Office"),
    and regional/deputy variants.
    """
    t = title.lower().strip()
    # Explicit exclusions — support roles that look like C-suite
    if re.search(r"\bchief\s+of\s+staff\b", t):
        return False
    if not _CSUITE_RE.search(t):
        return False
    return True


def _is_ceo(title: str) -> bool:
    """
    Return True when *title* is the CEO / apex of Executive Management.
    Covers: CEO, Chief Executive Officer, (Group) President, Group MD.
    Excludes Deputy/Vice/Assistant CEO.
    """
    t = title.lower().strip()
    if any(exc in t for exc in ("deputy", "vice", "assistant", "associate")):
        return False
    return bool(re.search(r"\bchief\s+executive\b|\bceo\b", t)) or \
           bool(re.search(r"^(?:group\s+)?president$", t)) or \
           bool(re.search(r"^(?:group\s+)?managing\s+director$", t))


# Geographic terms that signal a regional/country scope in an exec title.
# Used by _is_regional_exec to distinguish "Australia CEO" (L2) from
# "CEO" (L1 global apex).
_REGIONAL_GEO_RE = re.compile(
    r"\b(?:u\.?s\.?a?|uk|u\.k\.|emea|apac|apj|latam|mena|asean|gcc|anz|"
    r"asia(?:[- ]pacific)?|europe(?:an)?|america[ns]?|africa[n]?|pacific|"
    r"oceania|australia[n]?|new\s+zealand|india[n]?|china|japan(?:ese)?|"
    r"south\s+korea[n]?|canada(?:ian)?|brazil(?:ian)?|"
    r"germany|german|france|french|italy|italian|spain|spanish|mexico|"
    r"singapore|hong\s+kong|greater\s+china|"
    r"south(?:east|ern)?\s+asia|middle\s+east(?:\s+and\s+africa)?|"
    r"regional|country|local)\b",
    re.IGNORECASE,
)


def _is_regional_exec(title: str) -> bool:
    """
    Return True when *title* is a regional/country leadership role —
    e.g. "CEO, Australia", "Head of EMEA", "Australia CEO",
    "Managing Director, Singapore", "President APAC".

    Regional executives sit at layer 2 in Executive Management so they do
    not compete with the global CEO for the apex position in the org chart.
    Layer 2 regional heads appear beneath the global C-Suite in the EM tree.
    """
    t = title.lower().strip()

    # Must have a geographic qualifier to be regional at all
    if not _REGIONAL_GEO_RE.search(t):
        return False

    # "Group CEO", "Global CEO", "Global Chief Executive" are the global apex —
    # never treat them as regional even though they may mention geographies
    if re.search(r"\b(group|global)\s+(?:ceo|chief\s+executive|president|managing)", t):
        return False

    # Require a senior role keyword alongside the geographic term
    if re.search(r"\b(ceo|chief\s+executive|president|managing\s+director)\b", t):
        return True

    # "Head of [region]" pattern — match when geographic term follows "head of"
    m = re.search(r"\bhead\s+of\s+(.+)", t)
    if m and _REGIONAL_GEO_RE.search(m.group(1)):
        return True

    return False


# Ordered from most-specific to least-specific so e.g. "Hong Kong" matches before "Asia".
_GEO_REGION_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bgreater\s+china\b',                  re.I), "Greater China"),
    (re.compile(r'\bhong\s+kong\b',                      re.I), "Hong Kong"),
    (re.compile(r'\bsouth(?:east|ern)?\s+asia\b|\basean\b', re.I), "Southeast Asia"),
    (re.compile(r'\bnorth(?:ern)?\s+america\b',          re.I), "North America"),
    (re.compile(r'\blatin\s+america\b|\blatam\b',        re.I), "Latin America"),
    (re.compile(r'\bmiddle\s+east(?:\s+and\s+africa)?\b|\bmena\b', re.I), "Middle East & Africa"),
    (re.compile(r'\bnew\s+zealand\b',                    re.I), "New Zealand"),
    (re.compile(r'\bsouth(?:\s+)?korea\b',               re.I), "South Korea"),
    (re.compile(r'\bgulf\b|\bgcc\b',                     re.I), "Gulf / GCC"),
    (re.compile(r'\bnordics?\b|\bscandinavia\b',         re.I), "Nordic"),
    (re.compile(r'\banz\b|\baustrali',                   re.I), "Australia / NZ"),
    (re.compile(r'\bindia\b|\bindian\b',                 re.I), "India"),
    (re.compile(r'\bchina\b|\bchinese\b',                re.I), "Greater China"),
    (re.compile(r'\bjapan\b|\bjapanese\b',               re.I), "Japan"),
    (re.compile(r'\bsingapore\b',                        re.I), "Singapore"),
    (re.compile(r'\bapac\b|\basia.?pacific\b|\bapj\b',  re.I), "Asia Pacific"),
    (re.compile(r'\bemea\b',                              re.I), "EMEA"),
    (re.compile(r'\beurope\b|\beuropean\b',              re.I), "Europe"),
    (re.compile(r'\bgermany\b|\bgerman\b',               re.I), "Germany"),
    (re.compile(r'\bfrance\b|\bfrench\b',                re.I), "France"),
    (re.compile(r'\bu\.?k\.?\b|\bunited\s+kingdom\b|\bbritish\b', re.I), "United Kingdom"),
    (re.compile(r'\bcanada\b|\bcanadian\b',              re.I), "Canada"),
    (re.compile(r'\bamericas\b',                         re.I), "Americas"),
    (re.compile(r'\bbrazil\b|\bbrazilian\b',             re.I), "Brazil"),
    (re.compile(r'\bmexic',                               re.I), "Mexico"),
    (re.compile(r'\bafrica\b|\bafrican\b',               re.I), "Africa"),
    (re.compile(r'\bmiddle\s+east\b',                    re.I), "Middle East"),
    (re.compile(r'\basia\b',                             re.I), "Asia Pacific"),
]


def _infer_exec_region(title: str, company_region: str = "Global HQ") -> str:
    """
    Infer the geographic sub-card region from an executive's title.

    Global/Group C-suite → company_region (stays in HQ card).
    Regional/country heads → their specific region card.
    Used to populate ExecPanel region sub-cards correctly.
    """
    t = title.lower().strip()
    # Global / group scope → HQ
    if re.search(r'\b(global|group)\s+(?:ceo|chief|president|managing|head)\b', t):
        return company_region or "Global HQ"
    # Match geographic indicators
    for pattern, region_name in _GEO_REGION_MAP:
        if pattern.search(t):
            return region_name
    return company_region or "Global HQ"


def _inject_knowledge_leadership(
    dag: OrganogramDAG,
    company_name: str,
    region: str = "Global HQ",
    sector: str = "Private",
    domain: str = "",
) -> None:
    """
    Synchronous BOD/EM injection called inline during upload.

    When *domain* is provided (inferred from email_domain or company_website),
    tries the company's own website first (web-sourced leadership); falls back
    to LLM training-data knowledge.  Without domain, uses knowledge only (fast).

    nlp_method is set to:
      "llm_leadership_web" — people found on the company's own website
      "llm_leadership_ai"  — people sourced from LLM training knowledge

    The background thread (_enrich_with_llm_leadership) runs after this and
    will upgrade any "llm_leadership_ai" entries to "llm_leadership_web" when
    the full web scrape yields results.
    """
    try:
        from llm_fallback import llm_fetch_leadership
    except ImportError:
        return

    try:
        leadership = llm_fetch_leadership(company_name, domain=domain)
    except Exception:
        return

    if not leadership.get("board") and not leadership.get("executives"):
        return

    source   = leadership.get("_source", "ai")
    nlp_meth = "llm_leadership_web" if source == "web" else "llm_leadership_ai"

    existing_keys: set[str] = {
        _name_key(dag.G.nodes[n].get("label", ""))
        for n in dag.G.nodes
        if dag.G.nodes[n].get("node_type") == "person"
    }

    from inference_logic import ClassifiedRecord

    # ── BOD: 3-layer hierarchy per Global_Designation_Hierarchy.xlsx ─────
    # layer 0: Chairman / Board Chair (sole apex)
    # layer 1: Vice Chair + Committee Chairs (senior board roles)
    # layer 2: Regular NEDs / INEDs
    # ── EM: CEO at layer 1 (frontend promotes to apex); global C-Suite at
    #        layer 1; regional/country heads at layer 2 (beneath C-Suite) ─
    injections: list[tuple[int, str, str, str, str]] = []  # (layer, name, title, dept, sub_role)
    for p in leadership.get("board", []):
        title = p["title"]
        if _is_board_chairman(title):
            layer = 0
        elif _is_vice_chair(title) or _is_committee_chair(title):
            layer = 1   # Senior board role
        else:
            layer = 2   # Regular NED / INED
        sub_role = _board_sub_role(title)
        injections.append((layer, p["name"], title, "Board of Management", sub_role))
    for p in leadership.get("executives", []):
        title = p["title"]
        # Regional/country heads (e.g. "CEO Australia", "Head of EMEA") sit at
        # layer 2 so they appear beneath global C-Suite, not at the same level.
        em_layer = 2 if _is_regional_exec(title) else 1
        injections.append((em_layer, p["name"], title, "Executive Management", ""))

    board_keys     = {_name_key(p["name"]) for p in leadership.get("board", [])}
    exec_keys      = {_name_key(p["name"]) for p in leadership.get("executives", [])}
    dual_role_keys = board_keys & exec_keys

    injected_dept_keys: set[tuple[str, str]] = set()

    for layer, name, title, dept_primary, sub_role in injections:
        key = _name_key(name)
        dept_key = (key, dept_primary)
        if dept_key in injected_dept_keys:
            continue
        if key in existing_keys and key not in dual_role_keys:
            continue
        injected_dept_keys.add(dept_key)
        person_uid = f"llm_{uuid.uuid4().hex[:12]}"
        dag.insert_person(ClassifiedRecord(
            id=person_uid,
            full_name=name,
            designation=title,
            company=company_name,
            linkedin_url="",
            location="",
            country="",
            sector=sector,
            region=region,
            layer=layer,
            dept_primary=dept_primary,
            dept_secondary="",
            dept_tertiary="",
            nlp_confidence=0.9,
            nlp_industry="llm",
            nlp_method=nlp_meth,
        ))
        # Attach board sub-role metadata for ExecPanel committee badge display
        if sub_role and person_uid in dag.G:
            dag.G.nodes[person_uid]["metadata"]["board_sub_role"] = sub_role

    dag.repair_governance_edges()


def _enrich_with_llm_leadership(
    dag: OrganogramDAG,
    classified: list,
    company_name: str,
    domain: str = "",
) -> None:
    """
    For every distinct company in the dataset, fetch Board of Directors and
    Executive Management via Claude and inject them into the DAG.

    - Runs unconditionally after every upload / demo load.
    - Deduplicates against names already present in the DAG.
    - Board members  → layer 0, dept_primary = "Board of Management"
    - C-Suite execs  → layer 1, dept_primary = "Executive Management"
    - Injected nodes carry nlp_method = "llm_leadership" for provenance.
    """
    try:
        from llm_fallback import llm_fetch_leadership
    except ImportError:
        return

    # ── Collect unique companies ──────────────────────────────────────
    companies: dict[str, dict] = {}   # company_name → {region, sector}

    # 1. Declared company name (always included)
    companies[company_name.strip()] = {"region": "Global HQ", "sector": "Private"}

    # 2. Companies found in the uploaded records
    for rec in classified:
        co = (getattr(rec, "company", "") or "").strip()
        if co and co not in companies:
            companies[co] = {
                "region": getattr(rec, "region", "Global HQ") or "Global HQ",
                "sector": getattr(rec, "sector", "Private")  or "Private",
            }

    # Fill region/sector for the declared company from existing classified records
    if company_name in companies and classified:
        # Use the most common region among all records
        from collections import Counter
        region_counts = Counter(
            getattr(r, "region", "Global HQ") or "Global HQ" for r in classified
        )
        sector_counts = Counter(
            getattr(r, "sector", "Private") or "Private" for r in classified
        )
        companies[company_name]["region"] = region_counts.most_common(1)[0][0]
        companies[company_name]["sector"] = sector_counts.most_common(1)[0][0]

    # ── Build existing-name index for deduplication ───────────────────
    existing_keys: set[str] = set()
    for nid in dag.G.nodes:
        attrs = dag.G.nodes[nid]
        if attrs.get("node_type") == "person":
            existing_keys.add(_name_key(attrs.get("label", "")))

    # ── Fetch and inject per company ─────────────────────────────────
    # Build index of AI-only nodes so web data can upgrade them.
    ai_only_keys: set[str] = {
        _name_key(dag.G.nodes[n].get("label", ""))
        for n in dag.G.nodes
        if (dag.G.nodes[n].get("node_type") == "person"
            and dag.G.nodes[n].get("metadata", {}).get("nlp_method") == "llm_leadership_ai")
    }

    for co, ctx in companies.items():
        leadership = llm_fetch_leadership(co, domain=domain if co == company_name else "")
        region = ctx["region"]
        sector = ctx["sector"]

        source   = leadership.get("_source", "ai")
        nlp_meth = "llm_leadership_web" if source == "web" else "llm_leadership_ai"

        # ── BOD: 3-layer hierarchy per Global_Designation_Hierarchy.xlsx ──
        # layer 0: Chairman (apex)
        # layer 1: Vice Chair + Committee Chairs (senior board roles)
        # layer 2: Regular NEDs / INEDs
        # ── EM: global C-Suite at layer 1 (CEO promoted by frontend);
        #        regional/country heads at layer 2, own region sub-card ──────
        # injections: (layer, name, title, dept_primary, sub_role, person_region)
        injections: list[tuple[int, str, str, str, str, str]] = []
        for person in leadership.get("board", []):
            title = person["title"]
            if _is_board_chairman(title):
                bod_layer = 0
            elif _is_vice_chair(title) or _is_committee_chair(title):
                bod_layer = 1
            else:
                bod_layer = 2   # Regular NED / INED
            sub_role = _board_sub_role(title)
            # BOD members always at HQ — board governs the whole company
            injections.append((bod_layer, person["name"], title, "Board of Management", sub_role, region))
        for person in leadership.get("executives", []):
            title = person["title"]
            em_layer    = 2 if _is_regional_exec(title) else 1
            exec_region = _infer_exec_region(title, region)
            injections.append((em_layer, person["name"], title, "Executive Management", "", exec_region))

        # Build set of name-keys that appear in BOTH board and executives lists.
        # These "dual-role" individuals (e.g. Executive Chairman who is also CEO)
        # must be injected into both panels, so deduplication is done per
        # (name, dept) pair rather than just per name.
        board_keys = {_name_key(p["name"]) for p in leadership.get("board", [])}
        exec_keys  = {_name_key(p["name"]) for p in leadership.get("executives", [])}
        dual_role_keys = board_keys & exec_keys

        from inference_logic import ClassifiedRecord
        # injected_dept_keys tracks (name_key, dept) to prevent true duplicates
        # while allowing the same person to appear in both BOD and EM.
        injected_dept_keys: set[tuple[str, str]] = set()

        for layer, name, title, dept_primary, sub_role, person_region in injections:
            key = _name_key(name)
            dept_key = (key, dept_primary)

            # Skip exact duplicate (same person, same dept) — handles repeated entries
            if dept_key in injected_dept_keys:
                continue

            # Web source can upgrade an AI-only entry: update the existing node's
            # nlp_method in-place instead of creating a duplicate.
            if key in existing_keys and source == "web" and key in ai_only_keys:
                for nid in dag.G.nodes:
                    attrs = dag.G.nodes[nid]
                    if (attrs.get("node_type") == "person"
                            and _name_key(attrs.get("label", "")) == key):
                        meta = dict(attrs.get("metadata", {}))
                        meta["nlp_method"] = "llm_leadership_web"
                        if sub_role:
                            meta["board_sub_role"] = sub_role
                        dag.G.nodes[nid]["metadata"] = meta
                        ai_only_keys.discard(key)
                        break
                injected_dept_keys.add(dept_key)
                continue

            # Skip persons already present in the uploaded employee DAG,
            # UNLESS they are dual-role (board + exec) — in that case allow
            # injection into both depts so they appear in both panels.
            if key in existing_keys and key not in dual_role_keys:
                continue

            injected_dept_keys.add(dept_key)

            person_uid = f"llm_{uuid.uuid4().hex[:12]}"
            try:
                rec = ClassifiedRecord(
                    id=person_uid,
                    full_name=name,
                    designation=title,
                    company=co,
                    linkedin_url="",
                    location="",
                    country="",
                    sector=sector,
                    region=person_region,   # geo-inferred for EM, HQ for BOD
                    layer=layer,
                    dept_primary=dept_primary,
                    dept_secondary="",
                    dept_tertiary="",
                    nlp_confidence=0.9,
                    nlp_industry="llm",
                    nlp_method=nlp_meth,
                )
                dag.insert_person(rec)
                # Attach board sub-role for ExecPanel committee badge display
                if sub_role and person_uid in dag.G:
                    dag.G.nodes[person_uid]["metadata"]["board_sub_role"] = sub_role
            except Exception as _exc:
                logger.warning("Failed to inject leadership person %s: %s", name, _exc)

    # ── Guarantee BOD node always exists ─────────────────────────────────
    # BOD is only created when board members are found.  If web + LLM returned
    # no board, the node never exists and the org chart shows no BOD card.
    # Always create a placeholder so the chart always shows BOD → EM → depts.
    bod_id = dag._node_id("dept", "Board of Management")
    if bod_id not in dag.G:
        ctx0   = list(companies.values())[0] if companies else {}
        dag._ensure_node(
            bod_id,
            node_type=NODE_DEPT_P,
            label="Board of Management",
            layer=0,
            sector=ctx0.get("sector", "Private"),
            color="#3491E8",
            is_ghost=False,
            has_more=False,
            metadata={"people_count": 0},
        )
        dag._ensure_edge("root_global", bod_id)
        logger.info("BOD placeholder created (no board members found via web/LLM)")

    # ── Repair governance edges ───────────────────────────────────────────
    dag.repair_governance_edges()


# ─────────────────────────────────────────────
# CLI TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    data_path = Path(__file__).parent / "test_data.json"
    with open(data_path) as f:
        records = json.load(f)

    dag, db = build_from_records(records, company_name="AutoPrime Motors")
    stats = dag.stats()
    print(f"\n{'='*60}")
    print("DAG Statistics")
    print(f"{'='*60}")
    for k, v in stats.items():
        print(f"  {k:<20}: {v}")

    subtree = db.recursive_subtree("root_global")
    print(f"\nRecursive CTE returned {len(subtree)} nodes from root.\n")

    results = db.search("engineering")
    print(f"Search 'engineering' → {len(results)} matches")
