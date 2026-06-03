"""
PPTX export for the Organogram Engine.

One slide per department, hierarchical person cards arranged in layer bands.
"""
from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

# ── Constants ─────────────────────────────────────────────────────────────────

SLIDE_W = Inches(13.333)   # 16:9 widescreen
SLIDE_H = Inches(7.5)

# Seniority label per layer
LAYER_LABELS: dict[int, str] = {
    0:  "Board",
    1:  "C-Suite",
    2:  "Executive VP",
    3:  "SVP / Managing Director",
    4:  "VP / Head of",
    5:  "Senior Director / AVP",
    6:  "Director",
    7:  "Senior Manager",
    8:  "Manager",
    9:  "Senior / Lead / Staff",
    10: "Analyst / Specialist",
}

# Fill colour per layer: dark navy → light slate
_LAYER_RGB: list[tuple[int, int, int]] = [
    (0x08, 0x15, 0x22),   # L0
    (0x0c, 0x28, 0x3f),   # L1
    (0x0f, 0x3e, 0x6e),   # L2
    (0x17, 0x53, 0x88),   # L3
    (0x21, 0x6b, 0xa8),   # L4
    (0x2e, 0x86, 0xc1),   # L5
    (0x44, 0x9b, 0xd5),   # L6
    (0x5b, 0xb0, 0xe6),   # L7
    (0x85, 0xc1, 0xed),   # L8
    (0xa8, 0xd5, 0xf5),   # L9
    (0xcc, 0xe8, 0xfa),   # L10
]

def _layer_rgb(layer: int) -> tuple[int, int, int]:
    return _LAYER_RGB[min(layer, len(_LAYER_RGB) - 1)]

def _rgb(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(r, g, b)

# White text for dark layers, dark text for light layers
def _text_rgb(layer: int) -> RGBColor:
    return RGBColor(0xFF, 0xFF, 0xFF) if layer <= 6 else RGBColor(0x08, 0x15, 0x22)

# Convert hex string like "#3491E8" or "3491E8" → RGBColor
def _hex_to_rgb(hex_color: str) -> RGBColor:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return RGBColor(0x34, 0x91, 0xE8)
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# ── Slide helpers ─────────────────────────────────────────────────────────────

def _add_rect(
    slide, left, top, width, height,
    fill_rgb: RGBColor | None = None,
    line_rgb: RGBColor | None = None,
    line_width: float = 0,
    corner_radius: int = 0,
):
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.oxml.ns import qn
    from lxml import etree

    shape = slide.shapes.add_shape(
        1,  # MSO_SHAPE.RECTANGLE
        int(left), int(top), int(width), int(height),
    )
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
        # Set rounded corners via XML
        sp = shape._element
        spPr = sp.find(qn("p:spPr"))
        prstGeom = spPr.find(qn("a:prstGeom"))
        if prstGeom is not None:
            spPr.remove(prstGeom)
        new_geom = etree.SubElement(spPr, qn("a:prstGeom"), attrib={"prst": "roundRect"})
        avLst = etree.SubElement(new_geom, qn("a:avLst"))
        # adj value: corner radius as a fraction of half the min dimension * 100000
        adj_val = min(50000, int(corner_radius * 100000 // min(width, height)))
        etree.SubElement(avLst, qn("a:gd"), attrib={"name": "adj", "fmla": f"val {adj_val}"})

    return shape


def _add_textbox(slide, left, top, width, height, text: str,
                 font_size: float, bold: bool = False,
                 color: RGBColor | None = None,
                 align=PP_ALIGN.LEFT,
                 wrap: bool = True):
    txBox = slide.shapes.add_textbox(int(left), int(top), int(width), int(height))
    tf    = txBox.text_frame
    tf.word_wrap = wrap
    p  = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size  = Pt(font_size)
    run.font.bold  = bold
    if color:
        run.font.color.rgb = color
    return txBox


def _set_shape_text(shape, text: str, font_size: float, bold: bool = False,
                    color: RGBColor | None = None, align=PP_ALIGN.LEFT,
                    word_wrap: bool = True):
    tf = shape.text_frame
    tf.word_wrap = word_wrap
    p  = tf.paragraphs[0]
    p.alignment = align
    # Clear existing runs
    for run in p.runs:
        run.text = ""
    if not p.runs:
        run = p.add_run()
    else:
        run = p.runs[0]
    run.text = text
    run.font.size = Pt(font_size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color


# ── Card dimensions ───────────────────────────────────────────────────────────

CARD_W    = Inches(2.55)
CARD_H    = Inches(0.85)
CARD_GAP  = Inches(0.12)
BAND_GAP  = Inches(0.28)   # vertical gap between layer bands
LEFT_PAD  = Inches(0.5)
TOP_START = Inches(1.40)   # below header
HEADER_H  = Inches(1.25)

# Max cards across one row
MAX_CARDS_ROW = 4


# ── Cover slide ───────────────────────────────────────────────────────────────

def _make_cover(prs: Presentation, company: str, total_people: int,
                dept_count: int, industry: str):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    # Full-bleed dark gradient via two rects
    _add_rect(slide, 0, 0, SLIDE_W, SLIDE_H,
              fill_rgb=RGBColor(0x08, 0x15, 0x22))
    # Accent bar on the left
    _add_rect(slide, 0, 0, Inches(0.22), SLIDE_H,
              fill_rgb=RGBColor(0x34, 0x91, 0xE8))

    # Company name
    _add_textbox(slide,
                 Inches(0.55), Inches(2.2),
                 Inches(11.0), Inches(1.6),
                 company,
                 font_size=44, bold=True,
                 color=RGBColor(0xFF, 0xFF, 0xFF))

    # Sub-line
    sub = f"Organisational Chart  ·  {total_people:,} people  ·  {dept_count} departments"
    if industry:
        sub += f"  ·  {industry}"
    _add_textbox(slide,
                 Inches(0.55), Inches(3.85),
                 Inches(11.0), Inches(0.55),
                 sub,
                 font_size=13,
                 color=RGBColor(0x7e, 0xc8, 0xf8))

    # Date
    _add_textbox(slide,
                 Inches(0.55), Inches(6.6),
                 Inches(6.0), Inches(0.4),
                 f"Generated {datetime.now().strftime('%d %B %Y')}",
                 font_size=9,
                 color=RGBColor(0x44, 0x6e, 0x88))


# ── Department slide(s) ───────────────────────────────────────────────────────

def _draw_header(slide, dept_label: str, company: str,
                 dept_color: RGBColor, people_count: int):
    # Full-width coloured header bar
    _add_rect(slide, 0, 0, SLIDE_W, HEADER_H, fill_rgb=dept_color)

    # White accent bottom border
    _add_rect(slide, 0, HEADER_H - Inches(0.04), SLIDE_W, Inches(0.04),
              fill_rgb=RGBColor(0xFF, 0xFF, 0xFF))

    # Department name
    _add_textbox(slide,
                 Inches(0.45), Inches(0.20),
                 Inches(9.5), Inches(0.72),
                 dept_label.upper(),
                 font_size=26, bold=True,
                 color=RGBColor(0xFF, 0xFF, 0xFF))

    # Company + count on the right
    _add_textbox(slide,
                 Inches(10.2), Inches(0.36),
                 Inches(3.0), Inches(0.45),
                 f"{company}\n{people_count} people",
                 font_size=9,
                 color=RGBColor(0xFF, 0xFF, 0xFF),
                 align=PP_ALIGN.RIGHT)


def _draw_person_card(slide, left, top, person: dict, layer: int):
    """Draw a single person card (rounded rect + name + title + badge)."""
    fill_r, fill_g, fill_b = _layer_rgb(layer)
    fill  = RGBColor(fill_r, fill_g, fill_b)
    txt   = _text_rgb(layer)

    # Card background
    card = _add_rect(slide, left, top, CARD_W, CARD_H,
                     fill_rgb=fill, corner_radius=8000)

    name  = str(person.get("label", "—"))[:38]
    title = str(person.get("metadata", {}).get("designation", "") or "")[:52]
    badge = f"L{layer}"

    # Name
    _add_textbox(slide,
                 left + Inches(0.12), top + Inches(0.06),
                 CARD_W - Inches(0.55), Inches(0.35),
                 name,
                 font_size=9.5, bold=True, color=txt, wrap=False)

    # Title (smaller, lighter)
    title_color = RGBColor(
        min(fill_r + 80, 255),
        min(fill_g + 80, 255),
        min(fill_b + 80, 255),
    ) if layer <= 6 else RGBColor(0x44, 0x6e, 0x88)

    if title:
        _add_textbox(slide,
                     left + Inches(0.12), top + Inches(0.38),
                     CARD_W - Inches(0.55), Inches(0.40),
                     title,
                     font_size=7.5, bold=False, color=title_color)

    # Region tag (small, bottom-left)
    region = str(person.get("metadata", {}).get("region", "") or "")
    if region and region != "Global HQ":
        _add_textbox(slide,
                     left + Inches(0.12), top + CARD_H - Inches(0.22),
                     Inches(1.4), Inches(0.20),
                     f"🌐 {region[:18]}",
                     font_size=6.5, color=title_color)

    # Layer badge pill (top-right)
    badge_fill = RGBColor(
        min(fill_r + 40, 255),
        min(fill_g + 40, 255),
        min(fill_b + 40, 255),
    ) if layer <= 6 else RGBColor(0xdd, 0xee, 0xf8)
    badge_txt = RGBColor(0xFF, 0xFF, 0xFF) if layer <= 6 else RGBColor(0x08, 0x15, 0x22)
    badge_left = left + CARD_W - Inches(0.44)
    badge_top  = top + Inches(0.06)
    _add_rect(slide, badge_left, badge_top,
              Inches(0.36), Inches(0.24),
              fill_rgb=badge_fill, corner_radius=20000)
    _add_textbox(slide,
                 badge_left + Inches(0.02), badge_top,
                 Inches(0.32), Inches(0.24),
                 badge,
                 font_size=7, bold=True, color=badge_txt, align=PP_ALIGN.CENTER)

    return card


def _draw_connector(slide, parent_center_x, parent_bottom_y,
                    child_center_x, child_top_y, color: RGBColor):
    """Draw an L-shaped connector from parent card bottom to child card top."""
    from pptx.oxml.ns import qn
    from lxml import etree

    mid_y = (parent_bottom_y + child_top_y) // 2

    def _add_line(x1, y1, x2, y2):
        """Add a thin solid line shape."""
        # Use a very thin rectangle as a line (1 px ≈ 9525 EMU)
        if abs(x2 - x1) < 9525:   # vertical
            _add_rect(slide, x1, min(y1, y2), 9525, abs(y2 - y1),
                      fill_rgb=color)
        else:                       # horizontal
            _add_rect(slide, min(x1, x2), y1, abs(x2 - x1), 9525,
                      fill_rgb=color)

    # Vertical stem down from parent
    _add_line(parent_center_x, parent_bottom_y, parent_center_x, mid_y)
    # Horizontal leg to child
    _add_line(parent_center_x, mid_y, child_center_x, mid_y)
    # Vertical drop to child
    _add_line(child_center_x, mid_y, child_center_x, child_top_y)


def _make_dept_slides(prs: Presentation, company: str,
                      dept_label: str, dept_color_hex: str,
                      executives: list[dict]):
    """Create one or more slides for a department."""
    blank = prs.slide_layouts[6]
    dept_color = _hex_to_rgb(dept_color_hex)

    # Sort executives: layer first, then name
    execs = sorted(executives, key=lambda p: (p.get("layer", 99), p.get("label", "")))

    # Group into layer bands
    bands: dict[int, list[dict]] = {}
    for p in execs:
        L = p.get("layer", 9)
        bands.setdefault(L, []).append(p)

    # ── Pagination ────────────────────────────────────────────────────────
    # Pre-calculate how many rows each band needs, then bin into slides
    def _rows_for_band(count: int) -> int:
        return math.ceil(count / MAX_CARDS_ROW)

    ROWS_PER_SLIDE = 6   # conservative: leaves headroom for band labels

    # Build a flat list of (layer, chunk_of_people) that fits per slide
    slides_content: list[list[tuple[int, list[dict]]]] = []
    current_page: list[tuple[int, list[dict]]] = []
    current_rows = 0

    for layer in sorted(bands.keys()):
        people = bands[layer]
        # Split band into per-slide chunks
        chunk_size = MAX_CARDS_ROW * ROWS_PER_SLIDE
        for chunk_start in range(0, len(people), chunk_size):
            chunk = people[chunk_start:chunk_start + chunk_size]
            rows_needed = _rows_for_band(len(chunk)) + 1  # +1 for band label
            if current_rows + rows_needed > ROWS_PER_SLIDE and current_page:
                slides_content.append(current_page)
                current_page = []
                current_rows = 0
            current_page.append((layer, chunk))
            current_rows += rows_needed

    if current_page:
        slides_content.append(current_page)

    total_pages = len(slides_content)

    for page_idx, page_bands in enumerate(slides_content):
        slide = prs.slides.add_slide(blank)

        page_label = dept_label
        if total_pages > 1:
            page_label += f"  ({page_idx + 1}/{total_pages})"

        _draw_header(slide, page_label, company, dept_color, len(execs))

        y = TOP_START
        prev_band_cards: list[tuple[int, int, int]] = []   # (center_x, bottom_y, layer)

        for (layer, people) in page_bands:
            # Band label strip
            band_label = LAYER_LABELS.get(layer, f"Layer {layer}")
            label_rect = _add_rect(slide,
                                   0, y, SLIDE_W, Inches(0.26),
                                   fill_rgb=RGBColor(0xf0, 0xf5, 0xf9))
            _add_textbox(slide,
                         Inches(0.45), y + Inches(0.03),
                         Inches(10.0), Inches(0.22),
                         band_label.upper(),
                         font_size=7.5, bold=True,
                         color=RGBColor(0x0c, 0x36, 0x49))
            # Count badge
            count_text = str(len(people))
            _add_textbox(slide,
                         Inches(11.5), y + Inches(0.03),
                         Inches(1.6), Inches(0.22),
                         count_text,
                         font_size=7.5, bold=True,
                         color=RGBColor(0x34, 0x91, 0xE8),
                         align=PP_ALIGN.RIGHT)
            y += Inches(0.29)

            # Cards in rows
            rows = math.ceil(len(people) / MAX_CARDS_ROW)
            this_band_cards: list[tuple[int, int, int]] = []

            for row_idx in range(rows):
                row_people = people[row_idx * MAX_CARDS_ROW:(row_idx + 1) * MAX_CARDS_ROW]
                n = len(row_people)

                # Centre the row
                total_row_w = n * CARD_W + (n - 1) * CARD_GAP
                row_left = (SLIDE_W - total_row_w) // 2

                for col_idx, person in enumerate(row_people):
                    card_left = row_left + col_idx * (CARD_W + CARD_GAP)
                    _draw_person_card(slide, card_left, y, person, layer)
                    center_x = int(card_left + CARD_W // 2)
                    this_band_cards.append((center_x, int(y), int(y + CARD_H)))

                y += CARD_H + CARD_GAP

            prev_band_cards = this_band_cards
            y += BAND_GAP

        # Overflow guard
        if y > SLIDE_H - Inches(0.2):
            pass   # pagination handled above; shouldn't overflow


# ── Public API ────────────────────────────────────────────────────────────────

def build_pptx(
    company: str,
    industry: str,
    depts: list[dict[str, Any]],   # list of {label, color, executives: [...]}
) -> bytes:
    """
    Build and return PPTX bytes.

    ``depts`` is ordered: BOD first, then EM, then primary depts, then sub-depts.
    Each entry:
      {
        "label":      str,
        "color":      str,   # hex like "#3491E8" or "3491E8"
        "executives": list[dict],   # raw person node dicts from the DAG
      }
    """
    prs = Presentation()
    prs.slide_width  = int(SLIDE_W)
    prs.slide_height = int(SLIDE_H)

    total_people = sum(len(d["executives"]) for d in depts)
    dept_count   = len(depts)

    _make_cover(prs, company, total_people, dept_count, industry)

    for dept in depts:
        execs = dept.get("executives", [])
        if not execs:
            continue
        _make_dept_slides(
            prs,
            company       = company,
            dept_label    = dept.get("label", "Department"),
            dept_color_hex= dept.get("color", "#3491E8"),
            executives    = execs,
        )

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
