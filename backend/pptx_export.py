"""
PPTX export — org-chart style matching the reference screenshot.

Per dept-primary slide:
  • Full-width coloured header bar
  • Dept-head card centred at top (CEO / Head of dept)
  • Sub-department columns below, connected by L-shaped lines
  • Each column: sub-dept head card + member cards stacked below
  • If no sub-depts: direct members arranged in implied columns
  • Paginate when more than MAX_COLS columns
  • Card style: dark rounded header (name + title + photo circle) + white body
    (email / phone / location / LinkedIn)
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
HEADER_H = Inches(1.05)

# ── Card geometry ─────────────────────────────────────────────────────────────

CARD_W    = Inches(1.88)    # column-card width
CARD_HDR  = Inches(0.54)    # dark header height
CARD_BODY = Inches(0.74)    # white body height
CARD_H    = CARD_HDR + CARD_BODY

HEAD_W    = Inches(2.30)    # dept-head card slightly wider
PHOTO_D   = Inches(0.40)    # photo-circle diameter
COL_GAP   = Inches(0.30)    # horizontal gap between columns
CARD_V_GAP= Inches(0.10)    # vertical gap between stacked cards
CONN_H    = Inches(0.30)    # vertical space reserved for connector lines

SIDE_PAD  = Inches(0.45)
TREE_TOP  = int(HEADER_H) + int(Inches(0.22))   # y where dept-head card starts
MAX_COLS  = 6               # columns per slide before paginating

# ── Colour helpers ─────────────────────────────────────────────────────────────

def _luminance(r: int, g: int, b: int) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b

def _lighten(rgb: tuple[int, int, int], amt: float) -> tuple[int, int, int]:
    r, g, b = rgb
    return (min(255, r + int((255-r)*amt)),
            min(255, g + int((255-g)*amt)),
            min(255, b + int((255-b)*amt)))

def _darken(rgb: tuple[int, int, int], amt: float) -> tuple[int, int, int]:
    r, g, b = rgb
    return (max(0, int(r*(1-amt))), max(0, int(g*(1-amt))), max(0, int(b*(1-amt))))

def _on_dark(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(0xff, 0xff, 0xff) if _luminance(r,g,b) < 140 else RGBColor(0x0c,0x16,0x22)

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) != 6:
        return (0x3d, 0x51, 0x68)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _rgb(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(r, g, b)

# Cycling accent palette (used when sub-dept has same colour as parent)
_PALETTE: list[tuple[int, int, int]] = [
    (0x2c, 0x5f, 0x8a),
    (0x1b, 0x7a, 0x53),
    (0x7a, 0x1f, 0x96),
    (0xb0, 0x26, 0x26),
    (0xc9, 0x70, 0x1e),
    (0x10, 0x6b, 0x84),
]

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
                 align=PP_ALIGN.LEFT, wrap: bool = True):
    tb = slide.shapes.add_textbox(int(left), int(top), int(width), int(height))
    tf = tb.text_frame
    tf.word_wrap = wrap
    p  = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(font_size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color
    return tb


def _add_hyperlink_box(slide, left, top, width, height,
                       display: str, url: str, font_size: float):
    from pptx.oxml.ns import qn
    from lxml import etree
    tb = slide.shapes.add_textbox(int(left), int(top), int(width), int(height))
    tf = tb.text_frame
    tf.word_wrap = False
    p  = tf.paragraphs[0]
    run = p.add_run()
    run.text = display
    run.font.size = Pt(font_size)
    run.font.color.rgb = RGBColor(0x26, 0x7b, 0xd6)
    try:
        rId = slide.part.relate_to(
            url,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True)
        rPr = run._r.get_or_add_rPr()
        hl  = etree.SubElement(rPr, qn("a:hlinkClick"))
        hl.set(qn("r:id"), rId)
    except Exception:
        pass


_LP = 9525   # 1-pt line width in EMU

def _add_line(slide, x1, y1, x2, y2, color: RGBColor):
    if abs(x2 - x1) < _LP:   # vertical
        _add_rect(slide, x1 - _LP//2, min(y1,y2), _LP, max(abs(y2-y1), _LP), fill_rgb=color)
    else:                      # horizontal
        _add_rect(slide, min(x1,x2), y1 - _LP//2, abs(x2-x1), max(abs(y2-y1), _LP), fill_rgb=color)


# ── Cover slide ───────────────────────────────────────────────────────────────

def _make_cover(prs, company, total_people, dept_count, industry):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(slide, 0, 0, SLIDE_W, SLIDE_H, fill_rgb=RGBColor(0x08,0x15,0x22))
    _add_rect(slide, 0, 0, Inches(0.22), SLIDE_H, fill_rgb=RGBColor(0x34,0x91,0xE8))
    _add_textbox(slide, Inches(0.55), Inches(2.10), Inches(11.5), Inches(1.6),
                 company, font_size=44, bold=True, color=RGBColor(0xff,0xff,0xff))
    sub = f"Organisational Chart  ·  {total_people:,} people  ·  {dept_count} departments"
    if industry:
        sub += f"  ·  {industry}"
    _add_textbox(slide, Inches(0.55), Inches(3.80), Inches(11.5), Inches(0.55),
                 sub, font_size=13, color=RGBColor(0x7e,0xc8,0xf8))
    _add_textbox(slide, Inches(0.55), Inches(6.65), Inches(6.0), Inches(0.4),
                 f"Generated {datetime.now().strftime('%d %B %Y')}",
                 font_size=9, color=RGBColor(0x44,0x6e,0x88))


# ── Slide header ──────────────────────────────────────────────────────────────

def _draw_header(slide, dept_label, company, dept_rgb, total_hc, page_sfx=""):
    r, g, b = dept_rgb
    fill = _rgb(r, g, b)
    txt  = _on_dark(r, g, b)
    _add_rect(slide, 0, 0, SLIDE_W, HEADER_H, fill_rgb=fill)
    _add_rect(slide, 0, HEADER_H - int(Inches(0.04)), SLIDE_W, int(Inches(0.04)),
              fill_rgb=RGBColor(0xff,0xff,0xff))
    label = dept_label.upper() + (f"  {page_sfx}" if page_sfx else "")
    _add_textbox(slide, Inches(0.45), Inches(0.14), Inches(9.0), Inches(0.65),
                 label, font_size=22, bold=True, color=txt)
    _add_textbox(slide, Inches(10.0), Inches(0.20), Inches(3.1), Inches(0.55),
                 f"{company}\n{total_hc:,} people",
                 font_size=9, color=txt, align=PP_ALIGN.RIGHT)


# ── Org-chart card ────────────────────────────────────────────────────────────
#
#  ┌──────────────────────────────────┐
#  │ ● Name (bold, white)             │  ← dark rounded header
#  │   Title (light white, smaller)   │
#  ├──────────────────────────────────┤
#  │ email@company.com                │  ← white body
#  │ Work Phone: xxx-xxx-xxxx         │
#  │ Location: Florida                │
#  │ linkedin.com/in/...              │
#  └──────────────────────────────────┘
#
def _draw_card(slide, left: int, top: int, person: dict,
               hdr_rgb: tuple[int, int, int],
               card_w: int | None = None) -> None:

    cw = card_w or int(CARD_W)
    ch = int(CARD_H)
    hh = int(CARD_HDR)
    bh = int(CARD_BODY)
    cr = 5000  # corner_radius

    r, g, b = hdr_rgb
    hdr_fill = _rgb(r, g, b)

    # 1 — Full card as dark rounded rect (gives rounded top corners)
    _add_rect(slide, left, top, cw, ch, fill_rgb=hdr_fill, corner_radius=cr)

    # 2 — White body painted over the bottom part (square top, rounded bottom illusion via border)
    _add_rect(slide, left, top + hh, cw, bh + 1, fill_rgb=RGBColor(0xff,0xff,0xff))

    # 3 — Card border on top of everything (transparent fill, rounded)
    _add_rect(slide, left, top, cw, ch,
              line_rgb=RGBColor(0xb8,0xca,0xdc), line_width=0.8, corner_radius=cr)

    # 4 — Photo circle (centred vertically in header)
    pd = int(PHOTO_D)
    ph_left = left + int(Inches(0.10))
    ph_top  = top + (hh - pd) // 2
    dr, dg, db = _darken(hdr_rgb, 0.18)
    _add_rect(slide, ph_left, ph_top, pd, pd,
              fill_rgb=RGBColor(dr, dg, db), corner_radius=50000)
    _add_rect(slide, ph_left, ph_top, pd, pd,
              line_rgb=RGBColor(0xff,0xff,0xff), line_width=1.5, corner_radius=50000)

    # 5 — Name + title in header
    tx = ph_left + pd + int(Inches(0.07))
    tw = left + cw - tx - int(Inches(0.06))

    name  = str(person.get("label") or "").strip()[:28]
    title = str((person.get("metadata") or {}).get("designation") or "").strip()[:34]

    _add_textbox(slide, tx, top + int(Inches(0.07)), tw, int(Inches(0.20)),
                 name, font_size=8.5, bold=True, color=RGBColor(0xff,0xff,0xff), wrap=False)
    if title:
        _add_textbox(slide, tx, top + int(Inches(0.28)), tw, int(Inches(0.18)),
                     title, font_size=7.0, color=RGBColor(0xcc,0xd8,0xe5), wrap=False)

    # 6 — Contact details in white body
    meta   = person.get("metadata") or {}
    bx     = left + int(Inches(0.10))
    bw     = cw - int(Inches(0.15))
    by     = top + hh + int(Inches(0.06))
    line_h = int(Inches(0.175))
    lgap   = int(Inches(0.175))
    dark_c = RGBColor(0x3a, 0x52, 0x66)

    def _body_line(text: str, y: int, url: str | None = None) -> None:
        if url:
            _add_hyperlink_box(slide, bx, y, bw, line_h, text, url, 6.5)
        else:
            _add_textbox(slide, bx, y, bw, line_h, text, 6.5, color=dark_c, wrap=False)

    cy = by
    email = str(meta.get("email") or "").strip()
    if email:
        _body_line(email, cy)
        cy += lgap

    phone = str(meta.get("phone") or meta.get("work_phone") or "").strip()
    if phone:
        _body_line(f"Work Phone: {phone}", cy)
        cy += lgap

    loc = str(meta.get("city") or meta.get("region") or meta.get("location") or "").strip()[:28]
    if loc:
        _body_line(f"Location: {loc}", cy)
        cy += lgap

    li = str(meta.get("linkedin_url") or meta.get("linkedin") or
             meta.get("LinkedInURL") or "").strip()
    if li and cy + line_h < top + ch:
        disp = li.replace("https://www.", "").replace("https://", "").replace("http://","")[:36]
        _body_line(disp, cy, url=li)


# ── Connectors ────────────────────────────────────────────────────────────────

_CONN_CLR = RGBColor(0x8e, 0xa3, 0xb8)   # mid-gray connector lines

def _draw_connectors(slide, parent_cx: int, parent_bottom: int,
                     child_cxs: list[int], child_top: int) -> None:
    """T-bar connectors: vertical stem → horizontal bar → drops to each child."""
    if not child_cxs:
        return
    mid_y = parent_bottom + (child_top - parent_bottom) // 2
    if len(child_cxs) == 1:
        _add_line(slide, parent_cx, parent_bottom, parent_cx, child_top, _CONN_CLR)
    else:
        _add_line(slide, parent_cx, parent_bottom, parent_cx, mid_y, _CONN_CLR)
        _add_line(slide, min(child_cxs), mid_y, max(child_cxs), mid_y, _CONN_CLR)
        for cx in child_cxs:
            _add_line(slide, cx, mid_y, cx, child_top, _CONN_CLR)


# ── Department slide(s) ───────────────────────────────────────────────────────

def _make_dept_slides(prs: Presentation, company: str,
                      dept_label: str, dept_color_hex: str,
                      total_hc: int,
                      dept_head: dict | None,
                      columns: list[dict]) -> None:
    """
    Renders one or more slides for a department.

    ``columns`` is a list of dicts:
        { "head": person | None,
          "members": [person, …],
          "accent_rgb": (r,g,b) }

    The dept_head is shown at the top-center; columns are arranged
    horizontally below it, each with its own head + stacked members.
    """
    dept_rgb = _hex_to_rgb(dept_color_hex)
    blank    = prs.slide_layouts[6]

    # ── Scale column width to fit slide ──────────────────────────────────
    avail_w = int(SLIDE_W) - 2 * int(SIDE_PAD)

    def _col_w_for_n(n: int) -> int:
        if n == 0:
            return int(CARD_W)
        total = n * int(CARD_W) + (n - 1) * int(COL_GAP)
        if total <= avail_w:
            return int(CARD_W)
        # scale down
        return max(int(Inches(1.2)), (avail_w - (n-1) * int(COL_GAP)) // n)

    # ── Split columns across slides ───────────────────────────────────────
    def _make_one_slide(slide_cols: list[dict], page_sfx: str) -> None:
        slide = prs.slides.add_slide(blank)
        _draw_header(slide, dept_label, company, dept_rgb, total_hc, page_sfx)

        n  = len(slide_cols)
        cw = _col_w_for_n(n)
        ch = int(CARD_H)
        gap = int(COL_GAP) if cw == int(CARD_W) else int(Inches(0.20))

        total_w    = n * cw + (n-1) * gap
        col_start  = (int(SLIDE_W) - total_w) // 2
        col_xs     = [col_start + i * (cw + gap) for i in range(n)]
        col_cxs    = [x + cw // 2 for x in col_xs]

        # Dept-head card — centred, slightly wider
        head_y     = TREE_TOP
        head_cw    = min(int(HEAD_W), cw)
        head_x     = int(SLIDE_W // 2) - head_cw // 2
        head_btm   = head_y + ch

        if dept_head:
            _draw_card(slide, head_x, head_y, dept_head, dept_rgb, card_w=head_cw)

        # Connector gap below head
        conn_top  = head_btm + int(CONN_H)
        conn_btm  = conn_top + int(CONN_H)   # col-head card top

        # Connectors: dept_head → column heads
        if dept_head and slide_cols:
            head_cx = int(SLIDE_W // 2)
            _draw_connectors(slide, head_cx, head_btm, col_cxs, conn_btm)

        # Columns
        for i, col in enumerate(slide_cols):
            cx   = col_xs[i]
            ccx  = col_cxs[i]
            acc  = col["accent_rgb"]

            # Sub-dept head card
            col_head_y = conn_btm
            if col.get("head"):
                _draw_card(slide, cx, col_head_y, col["head"], acc, card_w=cw)
            member_start = col_head_y + ch + int(CARD_V_GAP)

            # Stacked member cards
            for j, member in enumerate(col.get("members") or []):
                my = member_start + j * (ch + int(CARD_V_GAP))
                if my + ch > int(SLIDE_H) - int(Inches(0.12)):
                    break
                _draw_card(slide, cx, my, member, _lighten(acc, 0.25), card_w=cw)

    # Chunk columns across slides
    chunks = [columns[i:i+MAX_COLS] for i in range(0, max(len(columns), 1), MAX_COLS)]
    if not chunks:
        chunks = [[]]
    total_pages = len(chunks)
    for idx, chunk in enumerate(chunks):
        sfx = f"({idx+1}/{total_pages})" if total_pages > 1 else ""
        _make_one_slide(chunk, sfx)


# ── Public API ────────────────────────────────────────────────────────────────

def build_pptx(company: str, industry: str,
               depts: list[dict[str, Any]]) -> bytes:
    """
    Build and return PPTX bytes.

    Each entry in ``depts``:
      {
        "label":     str,
        "color":     str,          # hex
        "headcount": int,
        "head":      dict | None,  # dept-head person node
        "columns": [               # each = one vertical column on the slide
          { "head":       dict | None,
            "members":    [dict, …],
            "accent_rgb": (r,g,b) },
          …
        ],
      }
    """
    prs = Presentation()
    prs.slide_width  = int(SLIDE_W)
    prs.slide_height = int(SLIDE_H)

    total_people = sum(d.get("headcount", 0) for d in depts)
    _make_cover(prs, company, total_people, len(depts), industry)

    for dept in depts:
        cols = dept.get("columns") or []
        hc   = dept.get("headcount", 0)
        if hc == 0 and not dept.get("head") and not cols:
            continue
        _make_dept_slides(
            prs,
            company        = company,
            dept_label     = dept.get("label", "Department"),
            dept_color_hex = dept.get("color", "#3491E8"),
            total_hc       = hc,
            dept_head      = dept.get("head"),
            columns        = cols,
        )

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
