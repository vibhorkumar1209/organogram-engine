"""
PPTX export for the Organogram Engine.

One slide per department: top-down tree org chart (CEO → VPs → Directors…)
with L-shaped connectors and branch-colour coding.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

# ── Slide geometry ─────────────────────────────────────────────────────────────

SLIDE_W  = Inches(13.333)   # 16:9 widescreen
SLIDE_H  = Inches(7.5)
HEADER_H = Inches(1.10)

# ── Layer labels (reference, not used in tree layout) ─────────────────────────

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

# ── Tree card defaults ─────────────────────────────────────────────────────────
# These are scaled down at runtime when the tree is wider/taller than the slide.

_TREE_CARD_W   = Inches(2.0)
_TREE_CARD_H   = Inches(0.72)
_TREE_GAP_X    = Inches(0.22)   # horizontal gap between sibling subtrees
_TREE_GAP_Y    = Inches(0.72)   # vertical gap between layers (for connectors)
_TREE_SIDE_PAD = Inches(0.5)    # left / right slide padding
_TREE_TOP_Y    = HEADER_H + Inches(0.22)   # y-start of tree area

_TREE_AVAIL_W  = SLIDE_W - 2 * _TREE_SIDE_PAD
_TREE_AVAIL_H  = SLIDE_H - _TREE_TOP_Y - Inches(0.2)

# ── Branch colour palette ──────────────────────────────────────────────────────

_BRANCH_COLORS: list[tuple[int, int, int]] = [
    (0x21, 0x6b, 0xa8),   # steel blue
    (0x1e, 0x88, 0x5e),   # forest green
    (0x8e, 0x24, 0xaa),   # purple
    (0xc6, 0x28, 0x28),   # deep red
    (0xe6, 0x81, 0x2e),   # burnt orange
    (0x00, 0x7b, 0x91),   # teal
]

# ── Colour helpers ─────────────────────────────────────────────────────────────

def _lighten(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    r, g, b = rgb
    return (
        min(255, r + int((255 - r) * amount)),
        min(255, g + int((255 - g) * amount)),
        min(255, b + int((255 - b) * amount)),
    )

def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b

def _hex_to_rgb_tuple(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0x34, 0x91, 0xE8)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _rgb(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(r, g, b)


# ── Low-level drawing helpers ─────────────────────────────────────────────────

def _add_rect(
    slide, left, top, width, height,
    fill_rgb: RGBColor | None = None,
    line_rgb: RGBColor | None = None,
    line_width: float = 0,
    corner_radius: int = 0,
):
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
        sp = shape._element
        spPr = sp.find(qn("p:spPr"))
        prstGeom = spPr.find(qn("a:prstGeom"))
        if prstGeom is not None:
            spPr.remove(prstGeom)
        new_geom = etree.SubElement(spPr, qn("a:prstGeom"), attrib={"prst": "roundRect"})
        avLst = etree.SubElement(new_geom, qn("a:avLst"))
        adj_val = min(50000, int(corner_radius * 100000 // min(width, height)))
        etree.SubElement(avLst, qn("a:gd"), attrib={"name": "adj", "fmla": f"val {adj_val}"})

    return shape


def _add_textbox(
    slide, left, top, width, height, text: str,
    font_size: float, bold: bool = False,
    color: RGBColor | None = None,
    align=PP_ALIGN.LEFT,
    wrap: bool = True,
):
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


_LINE_PX = 9525   # ~0.75 pt in EMU (1 pt = 12700 EMU)

def _add_line(slide, x1: int, y1: int, x2: int, y2: int, color: RGBColor) -> None:
    """Draw a horizontal or vertical line as a thin filled rectangle."""
    if abs(x2 - x1) < _LINE_PX:   # vertical
        _add_rect(slide, x1 - _LINE_PX // 2, min(y1, y2),
                  _LINE_PX, max(abs(y2 - y1), _LINE_PX), fill_rgb=color)
    else:                           # horizontal
        _add_rect(slide, min(x1, x2), y1 - _LINE_PX // 2,
                  abs(x2 - x1), max(abs(y2 - y1), _LINE_PX), fill_rgb=color)


# ── Cover slide ───────────────────────────────────────────────────────────────

def _make_cover(prs: Presentation, company: str, total_people: int,
                dept_count: int, industry: str):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    _add_rect(slide, 0, 0, SLIDE_W, SLIDE_H,
              fill_rgb=RGBColor(0x08, 0x15, 0x22))
    _add_rect(slide, 0, 0, Inches(0.22), SLIDE_H,
              fill_rgb=RGBColor(0x34, 0x91, 0xE8))

    _add_textbox(slide,
                 Inches(0.55), Inches(2.2),
                 Inches(11.0), Inches(1.6),
                 company,
                 font_size=44, bold=True,
                 color=RGBColor(0xFF, 0xFF, 0xFF))

    sub = f"Organisational Chart  ·  {total_people:,} people  ·  {dept_count} departments"
    if industry:
        sub += f"  ·  {industry}"
    _add_textbox(slide,
                 Inches(0.55), Inches(3.85),
                 Inches(11.0), Inches(0.55),
                 sub, font_size=13,
                 color=RGBColor(0x7e, 0xc8, 0xf8))

    _add_textbox(slide,
                 Inches(0.55), Inches(6.6),
                 Inches(6.0), Inches(0.4),
                 f"Generated {datetime.now().strftime('%d %B %Y')}",
                 font_size=9,
                 color=RGBColor(0x44, 0x6e, 0x88))


# ── Dept slide header ─────────────────────────────────────────────────────────

def _draw_header(slide, dept_label: str, company: str,
                 dept_color: RGBColor,
                 total_headcount: int,
                 displayed_count: int | None = None):
    """Full-width coloured header bar."""
    _add_rect(slide, 0, 0, SLIDE_W, HEADER_H, fill_rgb=dept_color)
    _add_rect(slide, 0, HEADER_H - Inches(0.04), SLIDE_W, Inches(0.04),
              fill_rgb=RGBColor(0xFF, 0xFF, 0xFF))

    _add_textbox(slide,
                 Inches(0.45), Inches(0.18),
                 Inches(9.5), Inches(0.65),
                 dept_label.upper(),
                 font_size=24, bold=True,
                 color=RGBColor(0xFF, 0xFF, 0xFF))

    if displayed_count is not None and displayed_count < total_headcount:
        count_str = f"Top {displayed_count} of {total_headcount:,} people"
    else:
        count_str = f"{total_headcount:,} people"

    _add_textbox(slide,
                 Inches(10.2), Inches(0.30),
                 Inches(3.0), Inches(0.55),
                 f"{company}\n{count_str}",
                 font_size=9,
                 color=RGBColor(0xFF, 0xFF, 0xFF),
                 align=PP_ALIGN.RIGHT)


# ── Tree data structure ───────────────────────────────────────────────────────

@dataclass
class TreeNode:
    person:      dict
    children:    list["TreeNode"]        = field(default_factory=list)
    depth:       int                     = 0
    branch_rgb:  tuple[int, int, int]    = (0x34, 0x91, 0xE8)
    cx:          int                     = 0   # centre-x EMU (assigned later)
    ty:          int                     = 0   # top-y EMU (assigned later)
    subtree_w:   int                     = 0   # total subtree width EMU (assigned later)


# ── Tree building ─────────────────────────────────────────────────────────────

def _build_person_tree(people: list[dict]) -> list[TreeNode]:
    """
    Build an implied tree from a flat people list.
    Uses the `layer` field as hierarchy depth; distributes children
    across parents in the layer above as evenly as possible.
    """
    if not people:
        return []

    sorted_ppl = sorted(people, key=lambda p: (p.get("layer", 9), p.get("label", "")))

    layer_groups: dict[int, list[dict]] = {}
    for p in sorted_ppl:
        layer_groups.setdefault(p.get("layer", 9), []).append(p)

    layers    = sorted(layer_groups.keys())
    depth_map = {L: i for i, L in enumerate(layers)}

    nodes_by_layer: dict[int, list[TreeNode]] = {
        L: [TreeNode(person=p, depth=depth_map[L]) for p in layer_groups[L]]
        for L in layers
    }

    for i in range(len(layers) - 1):
        parents  = nodes_by_layer[layers[i]]
        children = nodes_by_layer[layers[i + 1]]
        np_, nc  = len(parents), len(children)
        for j, child in enumerate(children):
            parent_idx = (j * np_) // nc
            parents[parent_idx].children.append(child)

    return nodes_by_layer[layers[0]]


def _assign_branch_colors(roots: list[TreeNode],
                           root_rgb: tuple[int, int, int]) -> None:
    """
    Assign branch colours:
    - Root nodes              → root_rgb (dept colour)
    - Root's direct children  → cycling _BRANCH_COLORS (one per branch)
    - Deeper descendants      → progressively lighter shade of branch colour
    """
    def _subtree(node: TreeNode, color: tuple[int, int, int]) -> None:
        node.branch_rgb = color
        lighter = _lighten(color, 0.30)
        for child in node.children:
            _subtree(child, lighter)

    for root in roots:
        root.branch_rgb = root_rgb
        for i, child in enumerate(root.children):
            branch_color = _BRANCH_COLORS[i % len(_BRANCH_COLORS)]
            _subtree(child, branch_color)


# ── Layout helpers ────────────────────────────────────────────────────────────

def _calc_subtree_width(node: TreeNode, card_w: int, gap_x: int) -> int:
    """Post-order pass: set node.subtree_w for every node."""
    if not node.children:
        node.subtree_w = card_w
        return card_w
    children_w = sum(_calc_subtree_width(c, card_w, gap_x) for c in node.children)
    children_w += gap_x * (len(node.children) - 1)
    node.subtree_w = max(card_w, children_w)
    return node.subtree_w


def _assign_positions(node: TreeNode, cx: int, ty: int,
                       card_h: int, gap_y: int, gap_x: int) -> None:
    """Pre-order pass: set node.cx and node.ty for every node."""
    node.cx = cx
    node.ty = ty
    if not node.children:
        return
    total_w = sum(c.subtree_w for c in node.children) + gap_x * (len(node.children) - 1)
    child_x = cx - total_w // 2
    child_ty = ty + card_h + gap_y
    for child in node.children:
        child_cx = child_x + child.subtree_w // 2
        _assign_positions(child, child_cx, child_ty, card_h, gap_y, gap_x)
        child_x += child.subtree_w + gap_x


def _max_depth_of(nodes: list[TreeNode]) -> int:
    def _depth(n: TreeNode) -> int:
        if not n.children:
            return n.depth
        return max(_depth(c) for c in n.children)
    return max((_depth(r) for r in nodes), default=0)


# ── Drawing ───────────────────────────────────────────────────────────────────

_CONN_COLOR = RGBColor(0xb0, 0xb8, 0xc4)   # neutral grey for connector lines


def _draw_connectors(slide, node: TreeNode, card_h: int, gap_y: int) -> None:
    """Draw L-shaped connectors from node to each child, then recurse."""
    if not node.children:
        return

    parent_cx = node.cx
    parent_by = node.ty + card_h   # bottom of parent box

    if len(node.children) == 1:
        child = node.children[0]
        _add_line(slide, parent_cx, parent_by, parent_cx, child.ty, _CONN_COLOR)
    else:
        mid_y = parent_by + gap_y // 2
        _add_line(slide, parent_cx, parent_by, parent_cx, mid_y, _CONN_COLOR)
        left_cx  = node.children[0].cx
        right_cx = node.children[-1].cx
        _add_line(slide, left_cx, mid_y, right_cx, mid_y, _CONN_COLOR)
        for child in node.children:
            _add_line(slide, child.cx, mid_y, child.cx, child.ty, _CONN_COLOR)

    for child in node.children:
        _draw_connectors(slide, child, card_h, gap_y)


def _draw_box(slide, node: TreeNode, card_w: int, card_h: int, scale: float) -> None:
    """Draw a single person box with name, title, and branch colour fill."""
    r, g, b = node.branch_rgb
    fill = RGBColor(r, g, b)
    txt  = (RGBColor(0xFF, 0xFF, 0xFF) if _luminance((r, g, b)) < 155
            else RGBColor(0x08, 0x15, 0x22))

    left = node.cx - card_w // 2
    top  = node.ty

    _add_rect(slide, left, top, card_w, card_h, fill_rgb=fill, corner_radius=8000)

    person = node.person
    name   = str(person.get("label", "—"))[:36]
    title  = str(person.get("metadata", {}).get("designation", "") or "")[:48]

    name_pt  = max(5.5, 8.5 * scale)
    title_pt = max(4.5, 6.5 * scale)
    pad_x    = max(_LINE_PX, int(Inches(0.09) * scale))
    name_top = top + int(Inches(0.06))
    name_h   = int(Inches(0.28))

    _add_textbox(slide,
                 left + pad_x, name_top,
                 card_w - 2 * pad_x, name_h,
                 name, font_size=name_pt, bold=True, color=txt, wrap=False)

    if title and card_h > int(Inches(0.48)):
        title_color = (
            RGBColor(min(r + 70, 255), min(g + 70, 255), min(b + 70, 255))
            if _luminance((r, g, b)) < 155
            else RGBColor(0x55, 0x75, 0x88)
        )
        title_top = top + int(Inches(0.34))
        _add_textbox(slide,
                     left + pad_x, title_top,
                     card_w - 2 * pad_x, int(Inches(0.28)),
                     title, font_size=title_pt, color=title_color)


def _draw_tree(slide, roots: list[TreeNode],
               card_w: int, card_h: int, gap_y: int, scale: float) -> None:
    """Draw connectors (back) then boxes (front) for the whole tree."""
    for root in roots:
        _draw_connectors(slide, root, card_h, gap_y)

    def _boxes(node: TreeNode) -> None:
        _draw_box(slide, node, card_w, card_h, scale)
        for child in node.children:
            _boxes(child)

    for root in roots:
        _boxes(root)


# ── Department slide ──────────────────────────────────────────────────────────

def _make_dept_tree_slide(
    prs: Presentation,
    company: str,
    dept_label: str,
    dept_color_hex: str,
    executives: list[dict],
    headcount: int,
) -> None:
    """Create one tree-layout org chart slide for a department."""
    slide      = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    dept_rgb   = _hex_to_rgb_tuple(dept_color_hex)
    dept_color = _rgb(*dept_rgb)

    displayed = len(executives)
    _draw_header(
        slide, dept_label, company, dept_color,
        total_headcount=headcount,
        displayed_count=(displayed if displayed < headcount else None),
    )

    if not executives:
        return

    # Build implied hierarchy from layer numbers
    roots = _build_person_tree(executives)
    if not roots:
        return

    # Colour-code branches
    _assign_branch_colors(roots, dept_rgb)

    # ── Scale-to-fit ──────────────────────────────────────────────────────
    card_w = int(_TREE_CARD_W)
    card_h = int(_TREE_CARD_H)
    gap_x  = int(_TREE_GAP_X)
    gap_y  = int(_TREE_GAP_Y)

    # Phase 1: calculate nominal tree dimensions
    for root in roots:
        _calc_subtree_width(root, card_w, gap_x)

    total_tree_w = sum(r.subtree_w for r in roots) + gap_x * (len(roots) - 1)

    num_layers   = _max_depth_of(roots) + 1   # 0-indexed → +1 for actual count
    total_tree_h = num_layers * card_h + (num_layers - 1) * gap_y

    avail_w = int(_TREE_AVAIL_W)
    avail_h = int(_TREE_AVAIL_H)

    scale = min(avail_w / total_tree_w, avail_h / total_tree_h, 1.0)
    scale = max(scale, 0.25)   # floor: never below 25% (still renders, just small)

    # Phase 2: apply scale if needed
    if scale < 0.98:
        card_w = int(card_w * scale)
        card_h = int(card_h * scale)
        gap_x  = int(gap_x  * scale)
        gap_y  = int(gap_y  * scale)
        for root in roots:
            _calc_subtree_width(root, card_w, gap_x)
        total_tree_w = sum(r.subtree_w for r in roots) + gap_x * (len(roots) - 1)

    # ── Assign positions ──────────────────────────────────────────────────
    slide_cx  = int(SLIDE_W // 2)
    tree_top  = int(_TREE_TOP_Y)

    if len(roots) == 1:
        _assign_positions(roots[0], slide_cx, tree_top, card_h, gap_y, gap_x)
    else:
        # Multiple roots: lay them side by side, centred on the slide
        start_cx = slide_cx - total_tree_w // 2
        for root in roots:
            root_cx = start_cx + root.subtree_w // 2
            _assign_positions(root, root_cx, tree_top, card_h, gap_y, gap_x)
            start_cx += root.subtree_w + gap_x

    # ── Draw ──────────────────────────────────────────────────────────────
    _draw_tree(slide, roots, card_w, card_h, gap_y, scale)


# ── Public API ────────────────────────────────────────────────────────────────

def build_pptx(
    company: str,
    industry: str,
    depts: list[dict[str, Any]],
) -> bytes:
    """
    Build and return PPTX bytes.

    ``depts`` entries:
      {
        "label":      str,
        "color":      str,           # hex like "#3491E8"
        "executives": list[dict],    # person node dicts (already capped by caller)
        "headcount":  int,           # total before capping (for header display)
      }
    """
    prs = Presentation()
    prs.slide_width  = int(SLIDE_W)
    prs.slide_height = int(SLIDE_H)

    total_people = sum(d.get("headcount", len(d.get("executives", []))) for d in depts)
    dept_count   = len(depts)

    _make_cover(prs, company, total_people, dept_count, industry)

    for dept in depts:
        execs     = dept.get("executives", [])
        headcount = dept.get("headcount", len(execs))
        if not execs:
            continue
        _make_dept_tree_slide(
            prs,
            company        = company,
            dept_label     = dept.get("label", "Department"),
            dept_color_hex = dept.get("color", "#3491E8"),
            executives     = execs,
            headcount      = headcount,
        )

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
