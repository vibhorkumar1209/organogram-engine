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
    "Actuarial",
    "Underwriting",
    "Claims",
    "Investment Management",
    # ── Investment bank / markets business divisions ─────────────────────
    "Investment Banking",
    "Sales & Trading",
    "Wealth Management",
})

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
    # ── Corporate Communications & Public Affairs (standalone) ────────────
    "communications":                   "Corporate Communications & Public Affairs",
    "public relations":                 "Corporate Communications & Public Affairs",
    "pr":                               "Corporate Communications & Public Affairs",
    "corporate communications":         "Corporate Communications & Public Affairs",
    "internal communications":          "Corporate Communications & Public Affairs",
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
    # ── Corporate Communications & Public Affairs sub-depts ──────────────
    "corporate communications":         ("Corporate Communications & Public Affairs", "Corporate Communications"),
    "public relations":                 ("Corporate Communications & Public Affairs", "Corporate Communications"),
    "pr":                               ("Corporate Communications & Public Affairs", "Corporate Communications"),
    "external affairs":                 ("Corporate Communications & Public Affairs", "Public Affairs & Government Relations"),
    "public affairs":                   ("Corporate Communications & Public Affairs", "Public Affairs & Government Relations"),
    "government relations":             ("Corporate Communications & Public Affairs", "Public Affairs & Government Relations"),
    "communications":                   ("Corporate Communications & Public Affairs", "Corporate Communications"),
    "internal communications":          ("Corporate Communications & Public Affairs", "Corporate Communications"),
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
    if layer == 0:
        return "Board of Management"
    if layer == 1:
        # True C-Suite (CEO, CFO, COO, CTO, CMO …) → always Executive Management
        return "Executive Management"
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
        # Enforce canonical department names and layer-based overrides
        # (Board L0 → "Board of Management", C-Suite L1-2 → "Executive Management")
        dept_p = _canonical_dept(rec.dept_primary, rec.layer)
        dept_s = _canonical_subdept(rec.dept_secondary if rec.layer > 2 else "")
        dept_t = rec.dept_tertiary  if rec.layer > 2 else ""
        # If sub-dept remapped to "" (merge into parent) treat as no sub-dept
        if not dept_s:
            dept_t = ""

        # ── Elevate compound dept names to correct (primary, secondary) ──
        # e.g. dept_p="Customer Experience" → dept_p="Marketing", dept_s="Customer Experience"
        #      dept_p="Sales & Account Management" → dept_p="Sales", dept_s="Account Management"
        if rec.layer > 2 and not dept_s:
            elevate_key = dept_p.lower()
            if elevate_key in _DEPT_ELEVATE:
                dept_p, dept_s = _DEPT_ELEVATE[elevate_key]

        leaf_dept_id = self.ensure_department(
            rec.region, rec.sector,
            dept_p, dept_s, dept_t
        )

        person_id = rec.id
        self._ensure_node(person_id, **{
            "node_id":    person_id,
            "node_type":  NODE_PERSON,
            "label":      rec.full_name,
            "layer":      rec.layer,
            "sector":     rec.sector,
            "color":      SECTOR_COLORS.get(rec.sector, "#64748B"),
            "is_ghost":   False,
            "expanded":   False,
            "metadata": {
                "designation":    rec.designation,
                "company":        rec.company,
                "linkedin_url":   rec.linkedin_url,
                "location":       rec.location,
                "region":         getattr(rec, "region", "") or "",
                "dept_primary":   dept_p,
                "dept_secondary": dept_s,
                "nlp_confidence": round(getattr(rec, "nlp_confidence", 0.0), 2),
                "nlp_industry":   getattr(rec, "nlp_industry", "generic"),
                "nlp_method":     getattr(rec, "nlp_method", "fallback"),
            },
        })

        # Build ghost chain from layer 4 → rec.layer
        self._insert_with_ghosts(leaf_dept_id, person_id, rec)

    def _real_person_at_layer(self, parent_id: str, target_layer: int) -> Optional[str]:
        """
        Return the node_id of the first real (non-ghost) person node that is a
        direct child of *parent_id* at *target_layer*, or None if not found.

        Used so ghost chains route through actual senior people rather than
        creating parallel phantom chains alongside them.
        """
        for child in self.G.successors(parent_id):
            attrs = self.G.nodes.get(child, {})
            if (attrs.get("node_type") == NODE_PERSON
                    and attrs.get("layer") == target_layer
                    and not attrs.get("is_ghost", False)):
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

    # Classify company industry before running inference engine
    from industry_classifier import classify_industry as _classify_industry
    detected_industry = _classify_industry(company_name, primary_domain_pre)
    logger.info("Detected industry for '%s': %s", company_name, detected_industry or "unknown")

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
    injections: list[tuple[int, str, str, str]] = []
    for p in leadership.get("board", []):
        injections.append((0, p["name"], p["title"], "Board of Management"))
    for p in leadership.get("executives", []):
        injections.append((1, p["name"], p["title"], "Executive Management"))

    for layer, name, title, dept_primary in injections:
        key = _name_key(name)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        dag.insert_person(ClassifiedRecord(
            id=f"llm_{uuid.uuid4().hex[:12]}",
            full_name=name,
            designation=title,
            company=company_name,
            linkedin_url="",
            location="",
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

        injections: list[tuple[int, str, str, str]] = []
        for person in leadership.get("board", []):
            injections.append((0, person["name"], person["title"], "Board of Management"))
        for person in leadership.get("executives", []):
            injections.append((1, person["name"], person["title"], "Executive Management"))

        from inference_logic import ClassifiedRecord
        for layer, name, title, dept_primary in injections:
            key = _name_key(name)

            # Web source can upgrade an AI-only entry: update the existing node's
            # nlp_method in-place instead of creating a duplicate.
            if key in existing_keys and source == "web" and key in ai_only_keys:
                for nid in dag.G.nodes:
                    attrs = dag.G.nodes[nid]
                    if (attrs.get("node_type") == "person"
                            and _name_key(attrs.get("label", "")) == key):
                        meta = dict(attrs.get("metadata", {}))
                        meta["nlp_method"] = "llm_leadership_web"
                        dag.G.nodes[nid]["metadata"] = meta
                        ai_only_keys.discard(key)
                        break
                continue

            if key in existing_keys:
                continue
            existing_keys.add(key)

            rec = ClassifiedRecord(
                id=f"llm_{uuid.uuid4().hex[:12]}",
                full_name=name,
                designation=title,
                company=co,
                linkedin_url="",
                location="",
                sector=sector,
                region=region,
                layer=layer,
                dept_primary=dept_primary,
                dept_secondary="",
                dept_tertiary="",
                nlp_confidence=0.9,
                nlp_industry="llm",
                nlp_method=nlp_meth,
            )
            dag.insert_person(rec)

    # ── Repair governance edges ───────────────────────────────────────────
    # Uploaded records often lack L0/L1 people, so EM and functional depts
    # fall back to root_global as their parent.  After injecting BOD/EM here,
    # fix those stale edges so the hierarchy is correct.
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
