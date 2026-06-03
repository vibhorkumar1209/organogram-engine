"""
PPTX export for the Organogram Engine.

Layout per dept_primary slide:
  • Full-width coloured header (dept name + headcount)
  • Sub-department sections as labelled colour-coded bands
  • Person cards (white bg + coloured left-accent): Name · Designation · Location · LinkedIn
  • Paginate when > MAX_PER_SLIDE people on one slide
"""
from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

# ── Slide geometry ─────────────────────────────────────────────────────────────

SLIDE_W  = Inches(13.333)
SLIDE_H  = Inches(7.5)
HEADER_H = Inches(1.15)

# ── Card geometry ─────────────────────────────────────────────────────────────

CARD_W        = Inches(2.35)
CARD_H        = Inches(1.30)
CARD_GAP_X    = Inches(0.14)
CARD_GAP_Y    = Inches(0.10)
CARDS_PER_ROW = 5
MAX_PER_SLIDE = 15               # paginate after this many cards

LEFT_PAD      = Inches(0.50)
RIGHT_PAD     = Inches(0.50)
TOP_CONTENT   = HEADER_H + Inches(0.20)

SECTION_H     = Inches(0.32)    # sub-dept section-label strip height
SECTION_GAP   = Inches(0.14)    # gap above each section label
ACCENT_W      = int(Inches(0.07))  # left accent bar width on cards

# ── Colours ────────────────────────────────────────────────────────────────────

_CARD_BG      = RGBColor(0xff, 0xff, 0xff)
_CARD_BORDER  = RGBColor(0xd4, 0xdf, 0xed)
_TXT_NAME     = RGBColor(0x0c, 0x16, 0x22)
_TXT_DESIG    = RGBColor(0x3a, 0x52, 0x66)
_TXT_LOC      = RGBColor(0x55, 0x70, 0x84)
_TXT_LINK     = RGBColor(0x26, 0x7b, 0xd6)

# Cycling palette for sub-dept accent colours when the dept color is re-used
_PALETTE: list[tuple[int, int, int]] = [
    (0x21, 0x6b, 0xa8),
    (0x1e, 0x88, 0x5e),
    (0x8e, 0x24, 0xaa),
    (0xc6, 0x28, 0x28),
    (0xe6, 0x81, 0x2e),
    (0x00, 0x7b, 0x91),
]

# ── Colour helpers ─────────────────────────────────────────────────────────────

def _luminance(r: int, g: int, b: int) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b

def _on_bg(r: int, g: int, b: int) -> RGBColor:
    """White text on dark bg, dark text on light bg."""
    return RGBColor(0xff, 0xff, 0xff) if _luminance(r, g, b) < 130 else RGBColor(0x0c, 0x16, 0x22)

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0x34, 0x91, 0xE8)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _rgb(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(r, g, b)


# ── Low-level drawing ─────────────────────────────────────────────────────────

def _add_rect(slide, left, top, width, height,
              fill_rgb: RGBColor | None = None,
              line_rgb: RGBColor | None = None,
              line_width: float = 0,
              corner_radius: int = 0):
    from pptx.oxml.ns import qn
    from lxml import etree

    shape = slide.shapes.add_shape(1, int(left), int(top), int(width), int(height))
    shape.line.width = int(Pt(line_width)) if line_width else 0

    if fill_rgb:
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill_rgb
    else:
        shape.fill.background()

    if line_rgb:
        shape.line.color.rgb = line_rgb
    else:
        shape.line.fill.background()

    if corner_radius:
        sp    = shape._element
        spPr  = sp.find(qn("p:spPr"))
        prstG = spPr.find(qn("a:prstGeom"))
        if prstG is not None:
            spPr.remove(prstG)
        ng = etree.SubElement(spPr, qn("a:prstGeom"), attrib={"prst": "roundRect"})
        av = etree.SubElement(ng, qn("a:avLst"))
        adj = min(50000, int(corner_radius * 100000 // min(width, height)))
        etree.SubElement(av, qn("a:gd"), attrib={"name": "adj", "fmla": f"val {adj}"})

    return shape


def _add_textbox(slide, left, top, width, height, text: str,
                 font_size: float, bold: bool = False,
                 color: RGBColor | None = None,
                 align=PP_ALIGN.LEFT,
                 wrap: bool = True) -> Any:
    txBox = slide.shapes.add_textbox(int(left), int(top), int(width), int(height))
    tf    = txBox.text_frame
    tf.word_wrap = wrap
    p  = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(font_size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color
    return txBox


def _add_hyperlink_textbox(slide, left, top, width, height, display: str,
                            url: str, font_size: float) -> None:
    """Textbox whose single run carries a hyperlink."""
    from pptx.oxml.ns import qn
    from lxml import etree

    txBox = slide.shapes.add_textbox(int(left), int(top), int(width), int(height))
    tf    = txBox.text_frame
    tf.word_wrap = False
    p  = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    run = p.add_run()
    run.text = display
    run.font.size = Pt(font_size)
    run.font.color.rgb = _TXT_LINK

    # Add hyperlink via XML
    try:
        rId = slide.part.relate_to(
            url,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True,
        )
        rPr = run._r.get_or_add_rPr()
        hl  = etree.SubElement(rPr, qn("a:hlinkClick"))
        hl.set(qn("r:id"), rId)
    except Exception:
        pass   # URL linking is best-effort


# ── Cover slide ───────────────────────────────────────────────────────────────

def _make_cover(prs: Presentation, company: str, total_people: int,
                dept_count: int, industry: str) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(slide, 0, 0, SLIDE_W, SLIDE_H, fill_rgb=RGBColor(0x08, 0x15, 0x22))
    _add_rect(slide, 0, 0, Inches(0.22), SLIDE_H, fill_rgb=RGBColor(0x34, 0x91, 0xE8))

    _add_textbox(slide, Inches(0.55), Inches(2.1), Inches(11.5), Inches(1.6),
                 company, font_size=44, bold=True, color=RGBColor(0xff, 0xff, 0xff))

    sub = f"Organisational Chart  ·  {total_people:,} people  ·  {dept_count} departments"
    if industry:
        sub += f"  ·  {industry}"
    _add_textbox(slide, Inches(0.55), Inches(3.80), Inches(11.5), Inches(0.55),
                 sub, font_size=13, color=RGBColor(0x7e, 0xc8, 0xf8))

    _add_textbox(slide, Inches(0.55), Inches(6.65), Inches(6.0), Inches(0.4),
                 f"Generated {datetime.now().strftime('%d %B %Y')}",
                 font_size=9, color=RGBColor(0x44, 0x6e, 0x88))


# ── Header bar ────────────────────────────────────────────────────────────────

def _draw_header(slide, dept_label: str, company: str,
                 dept_rgb: tuple[int, int, int],
                 total_headcount: int,
                 page_label: str = "") -> None:
    r, g, b = dept_rgb
    fill    = _rgb(r, g, b)
    txt     = _on_bg(r, g, b)

    _add_rect(slide, 0, 0, SLIDE_W, HEADER_H, fill_rgb=fill)

    # Thin white accent strip at bottom of header
    _add_rect(slide, 0, HEADER_H - int(Inches(0.05)), SLIDE_W, int(Inches(0.05)),
              fill_rgb=RGBColor(0xff, 0xff, 0xff))

    label_text = dept_label.upper()
    if page_label:
        label_text += f"  {page_label}"

    _add_textbox(slide, Inches(0.45), Inches(0.15), Inches(9.0), Inches(0.70),
                 label_text, font_size=24, bold=True, color=txt)

    count_str = f"{total_headcount:,} people"
    _add_textbox(slide, Inches(10.0), Inches(0.22), Inches(3.1), Inches(0.55),
                 f"{company}\n{count_str}", font_size=9, color=txt, align=PP_ALIGN.RIGHT)


# ── Section label (sub-department separator) ──────────────────────────────────

def _draw_section_label(slide, y: int, label: str,
                        headcount: int, accent_rgb: tuple[int, int, int]) -> None:
    r, g, b = accent_rgb
    fill    = _rgb(r, g, b)
    txt     = _on_bg(r, g, b)

    # Full-width coloured strip
    _add_rect(slide, int(LEFT_PAD), y, int(SLIDE_W - LEFT_PAD - RIGHT_PAD),
              int(SECTION_H), fill_rgb=fill, corner_radius=4000)

    count_txt = f"{headcount:,} people" if headcount else ""
    label_txt = f"{label}   {count_txt}".strip()
    _add_textbox(slide,
                 int(LEFT_PAD) + int(Inches(0.12)), y + int(Inches(0.05)),
                 int(SLIDE_W - LEFT_PAD - RIGHT_PAD) - int(Inches(0.24)), int(Inches(0.24)),
                 label_txt, font_size=9.5, bold=True, color=txt, wrap=False)


# ── Person card ───────────────────────────────────────────────────────────────

def _draw_card(slide, left: int, top: int,
               person: dict, accent_rgb: tuple[int, int, int]) -> None:
    """White card with coloured left-accent bar; Name / Designation / Location / LinkedIn."""
    r, g, b = accent_rgb
    acc = _rgb(r, g, b)

    # Card shell (white bg, subtle border)
    _add_rect(slide, left, top, int(CARD_W), int(CARD_H),
              fill_rgb=_CARD_BG,
              line_rgb=_CARD_BORDER,
              line_width=0.6,
              corner_radius=4000)

    # Left accent bar
    _add_rect(slide, left, top, ACCENT_W, int(CARD_H), fill_rgb=acc, corner_radius=4000)

    meta   = person.get("metadata") or {}
    ix     = left + ACCENT_W + int(Inches(0.09))   # text inner-left
    iw     = int(CARD_W) - ACCENT_W - int(Inches(0.14))

    # ── Name ────────────────────────────────────────────────────────────
    name = str(person.get("label") or "").strip()[:36]
    if name:
        _add_textbox(slide, ix, top + int(Inches(0.08)), iw, int(Inches(0.22)),
                     name, font_size=9.0, bold=True, color=_TXT_NAME, wrap=False)

    # ── Designation ─────────────────────────────────────────────────────
    desig = str(meta.get("designation") or "").strip()[:48]
    if desig:
        _add_textbox(slide, ix, top + int(Inches(0.31)), iw, int(Inches(0.20)),
                     desig, font_size=7.5, color=_TXT_DESIG)

    # ── Location ────────────────────────────────────────────────────────
    loc = str(
        meta.get("city") or meta.get("region") or meta.get("location") or ""
    ).strip()[:30]
    if loc:
        _add_textbox(slide, ix, top + int(Inches(0.53)), iw, int(Inches(0.18)),
                     f"▪ {loc}", font_size=7.0, color=_TXT_LOC, wrap=False)

    # ── LinkedIn ─────────────────────────────────────────────────────────
    li = str(
        meta.get("linkedin_url") or meta.get("linkedin") or
        meta.get("LinkedInURL") or ""
    ).strip()
    if li:
        display = li.replace("https://www.", "").replace("https://", "").replace("http://", "")
        display = display[:36]
        _add_hyperlink_textbox(
            slide,
            ix, top + int(Inches(0.74)),
            iw, int(Inches(0.18)),
            display, li, font_size=7.0,
        )


# ── Slide builder ─────────────────────────────────────────────────────────────

class _SlideBuilder:
    """Incrementally places section labels and card rows across slides."""

    def __init__(self, prs: Presentation, company: str,
                 dept_label: str, dept_rgb: tuple[int, int, int],
                 total_headcount: int):
        self._prs           = prs
        self._company       = company
        self._dept_label    = dept_label
        self._dept_rgb      = dept_rgb
        self._total_hc      = total_headcount
        self._slide         = None
        self._page          = 0
        self._cards_on_page = 0
        self._y             = 0

    def _new_slide(self, page_label: str = "") -> None:
        self._page         += 1
        self._slide         = self._prs.slides.add_slide(self._prs.slide_layouts[6])
        self._cards_on_page = 0
        self._y             = int(TOP_CONTENT)
        _draw_header(self._slide, self._dept_label, self._company,
                     self._dept_rgb, self._total_hc, page_label)

    def _ensure_slide(self) -> None:
        if self._slide is None:
            self._new_slide()

    def _remaining_h(self) -> int:
        return int(SLIDE_H) - self._y - int(Inches(0.15))

    def _fits_section_plus_row(self) -> bool:
        needed = int(SECTION_GAP) + int(SECTION_H) + int(CARD_H) + int(CARD_GAP_Y)
        return self._remaining_h() >= needed

    def _fits_card_row(self) -> bool:
        return self._remaining_h() >= int(CARD_H) + int(CARD_GAP_Y)

    def add_section(self, label: str, headcount: int,
                    accent_rgb: tuple[int, int, int]) -> None:
        self._ensure_slide()
        # If we can't fit section label + at least one card row → new slide
        if self._cards_on_page > 0 and not self._fits_section_plus_row():
            self._new_slide("(cont.)")
        self._y += int(SECTION_GAP)
        _draw_section_label(self._slide, self._y, label, headcount, accent_rgb)
        self._y += int(SECTION_H) + int(Inches(0.08))

    def add_people(self, people: list[dict],
                   accent_rgb: tuple[int, int, int]) -> None:
        """Place people cards in rows of CARDS_PER_ROW, paginating as needed."""
        for row_start in range(0, len(people), CARDS_PER_ROW):
            row = people[row_start: row_start + CARDS_PER_ROW]

            self._ensure_slide()

            # Page full (hard card limit) → new slide
            if self._cards_on_page >= MAX_PER_SLIDE:
                self._new_slide("(cont.)")

            # No vertical room for another row → new slide
            if not self._fits_card_row():
                self._new_slide("(cont.)")

            # Draw the row
            total_row_w = (len(row) * int(CARD_W) +
                           (len(row) - 1) * int(CARD_GAP_X))
            row_left = (int(SLIDE_W) - total_row_w) // 2

            for i, person in enumerate(row):
                cx = row_left + i * (int(CARD_W) + int(CARD_GAP_X))
                _draw_card(self._slide, cx, self._y, person, accent_rgb)

            self._y             += int(CARD_H) + int(CARD_GAP_Y)
            self._cards_on_page += len(row)


# ── Build dept slides ─────────────────────────────────────────────────────────

def _make_dept_slides(
    prs:           Presentation,
    company:       str,
    dept_label:    str,
    dept_color_hex: str,
    executives:    list[dict],   # people directly under dept (no sub-dept)
    headcount:     int,
    subdepts:      list[dict],   # [{label, color, headcount, executives}]
) -> None:
    dept_rgb = _hex_to_rgb(dept_color_hex)
    builder  = _SlideBuilder(prs, company, dept_label, dept_rgb, headcount)

    # ── Direct people (no sub-dept label) ────────────────────────────────
    if executives:
        builder.add_section("Direct Reports", len(executives), dept_rgb)
        builder.add_people(executives, dept_rgb)

    # ── Sub-department sections ───────────────────────────────────────────
    for idx, sub in enumerate(subdepts):
        sub_rgb = _hex_to_rgb(sub.get("color") or dept_color_hex)
        # If sub-dept has same hex as parent, cycle the palette
        if sub_rgb == dept_rgb:
            sub_rgb = _PALETTE[idx % len(_PALETTE)]
        builder.add_section(sub["label"], sub["headcount"], sub_rgb)
        builder.add_people(sub.get("executives", []), sub_rgb)


# ── Public API ────────────────────────────────────────────────────────────────

def build_pptx(
    company:  str,
    industry: str,
    depts:    list[dict[str, Any]],
) -> bytes:
    """
    Build and return PPTX bytes.

    Each entry in ``depts``:
      {
        "label":      str,
        "color":      str,       # hex
        "headcount":  int,       # total people in dept
        "executives": list[dict],  # direct people (no sub-dept)
        "subdepts":   list[{label, color, headcount, executives}],
      }
    """
    prs = Presentation()
    prs.slide_width  = int(SLIDE_W)
    prs.slide_height = int(SLIDE_H)

    total_people = sum(d.get("headcount", 0) for d in depts)
    dept_count   = len(depts)

    _make_cover(prs, company, total_people, dept_count, industry)

    for dept in depts:
        execs    = dept.get("executives") or []
        subdepts = dept.get("subdepts") or []
        hc       = dept.get("headcount", len(execs))
        if hc == 0:
            continue
        _make_dept_slides(
            prs,
            company        = company,
            dept_label     = dept.get("label", "Department"),
            dept_color_hex = dept.get("color", "#3491E8"),
            executives     = execs,
            headcount      = hc,
            subdepts       = subdepts,
        )

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
