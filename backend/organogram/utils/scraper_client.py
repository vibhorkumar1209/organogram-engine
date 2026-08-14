"""
Thin client for the scraper-api service.

Used by web_filings_agent to fetch LinkedIn company pages
with Playwright stealth (bypasses login wall) and general web pages.
Falls back gracefully if SCRAPER_API_URL is not set.
"""
from __future__ import annotations
import os
from typing import Optional
import httpx

_BASE = os.getenv("SCRAPER_API_URL", "").rstrip("/")
_KEY  = os.getenv("SCRAPER_API_KEY", "")
_TIMEOUT = 30.0


def _get(path: str) -> Optional[dict]:
    if not _BASE:
        return None
    try:
        headers = {"x-api-key": _KEY} if _KEY else {}
        resp = httpx.get(f"{_BASE}{path}", headers=headers, timeout=_TIMEOUT, follow_redirects=True)
        data = resp.json()
        return data.get("data") if data.get("success") else None
    except Exception:
        return None


def scrape_linkedin_company(url: str) -> Optional[dict]:
    """
    Returns dict with: name, tagline, about, industry, companySize,
    headquarters, founded, website, followerCount, specialties.
    None if scraper unavailable or request fails.
    """
    if "linkedin.com/company/" not in url:
        return None
    return _get(f"/scrape/linkedin/company?url={httpx.URL(url)}")


def scrape_web(url: str) -> Optional[dict]:
    """
    Returns dict with: title, bodyText, headings, links, metaTags, wordCount.
    None if scraper unavailable or request fails.
    """
    return _get(f"/scrape/web?url={httpx.URL(url)}")
