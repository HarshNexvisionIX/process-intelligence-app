"""Exports: process maps as HTML and PDF, and every readout / analysis page as one self-contained HTML file.

One layout, drawn twice:
    layout()        stages (or groups) as framed blocks in business order, the steps of a stage inside its block
                    in order (wrapping after PER_COL steps), edges routed between box sides; deterministic, no
                    Graphviz program needed
    svg()           the layout as inline SVG (HTML exports: open offline, print to PDF from any browser)
    MapFlowable     the same layout drawn with reportlab (PDF exports)
Documents are reportlab element lists (Paragraph, Table, Spacer, PageBreak, MapFlowable); pdf_bytes() renders them
as a PDF and elements_html() as an HTML page with the same content, so the HTML and the PDF never differ.
data_html() turns a page's data (the JSON the web page shows) into a readable HTML page of tables.
Nothing here computes a number: everything comes from the job's checked files or the API's page data.
"""
from __future__ import annotations

import html
import io
import json
import math
import re
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table

W, H = 176, 70                     # box size (px in SVG, points before scaling in PDF)
GX, GY = 54, 22                    # gaps between boxes
PAD, HEAD = 16, 44                 # padding inside a stage block, room for its title (two lines)
BLOCK_GAP = 34                     # gap between stage blocks
PER_COL = 5                        # steps per column inside a block before it wraps
MAX_DRAWN_EDGES = 40               # observed edges drawn on the map (all of them are in the table)
INK, MUTED, BLUE, GREY = "#182f50", "#5b6f8c", "#155eef", "#9aa5b1"


def _fmt_h(x) -> str:
    return "" if x in (None, "") else f"{float(x):,.1f} h"


def _wrap(text: str, width: int = 24, lines: int = 3) -> list[str]:
    words, out, cur = str(text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = f"{cur} {w}".strip()
        else:
            if cur:
                out.append(cur)
            cur = w if len(w) <= width else w[:width - 1] + "…"
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][:width - 1].rstrip() + "…"
    return out or [""]


def node_lines(n: dict) -> tuple[list[str], str, str]:
    """(label lines, metric line, apps line) for one box."""
    metric = []
    if n.get("missing"):
        metric.append("not seen in the data")
    elif n.get("hours") not in (None, ""):
        metric.append(_fmt_h(n["hours"]) + (f" · {100 * float(n['share']):.0f}%" if n.get("share") not in (None, "") else ""))
    elif n.get("events") not in (None, ""):
        metric.append(f"{int(n['events']):,} events")
    if n.get("cases") not in (None, "") and n.get("events") not in (None, ""):
        metric.append(f"{int(n['cases']):,} cases")
    apps = ", ".join(str(a.get("app")) for a in (n.get("apps") or [])[:2] if a.get("app"))
    return _wrap(n.get("label") or n.get("id")), " · ".join(metric), apps


# --------------------------------------------------------------------------- layout
def layout(nodes: list[dict], edges: list[dict], direction: str = "LR") -> dict:
    """Positions of blocks, boxes and edges. direction LR: stage blocks left to right, steps going down inside
    a block; TB: stage blocks top to bottom, steps going right inside a block. The size covers every curve."""
    tb = str(direction).upper() == "TB"
    groups: dict[str, list[dict]] = {}
    has_groups = any(n.get("group") for n in nodes)
    for n in nodes:
        groups.setdefault(str(n.get("group") or "") if has_groups else "", []).append(n)
    # pass 1: block sizes without the title, then the titles wrapped to their block (up to 3 lines)
    dims = []
    for label, members in groups.items():
        lanes, depth = math.ceil(len(members) / PER_COL), min(len(members), PER_COL)
        across, down = (lanes, depth) if not tb else (depth, lanes)
        bw = across * W + (across - 1) * GX + 2 * PAD
        bh = down * H + (down - 1) * GY + 2 * PAD
        title = _wrap(label or "(no stage)", max(12, int((bw - 2 * PAD) / 7.4)), 3) if has_groups else []
        dims.append((label, members, bw, bh, title))
    head = (14 + 15 * max(len(d[4]) for d in dims)) if has_groups and dims else 0
    # pass 2: positions
    boxes, blocks = {}, []
    cursor = 0.0
    for label, members, bw, bh, title in dims:
        bh += head
        bx, by = (cursor, 0.0) if not tb else (0.0, cursor)
        for k, n in enumerate(members):
            c, r = divmod(k, PER_COL) if not tb else divmod(k, PER_COL)[::-1]
            boxes[n["id"]] = (bx + PAD + c * (W + GX), by + head + PAD + r * (H + GY))
        cursor += (bw if not tb else bh) + BLOCK_GAP
        blocks.append({"label": label, "x": bx, "y": by, "w": bw, "h": bh, "framed": has_groups, "title": title})
    order_edges = [e for e in edges if e.get("kind") == "order"]
    observed = sorted([e for e in edges if e.get("kind") != "order"], key=lambda e: -float(e.get("count") or 0))
    drawn = order_edges + observed[:MAX_DRAWN_EDGES]
    top = max((float(e.get("count") or 0) for e in observed), default=0.0)
    paths = []
    for e in drawn:
        a, b = boxes.get(e.get("source")), boxes.get(e.get("target"))
        if not a or not b or e.get("source") == e.get("target"):
            continue
        paths.append({**e, "points": _route(a, b), "width": 1.4 if e.get("kind") == "order" or not top
                      else 1.2 + 3.0 * float(e.get("count") or 0) / top})
    xs = [b["x"] + b["w"] for b in blocks] + [x for p in paths for x, _ in p["points"]] + [W]
    ys = [b["y"] + b["h"] for b in blocks] + [y for p in paths for _, y in p["points"]] + [H]
    # a Bezier curve stays inside its control points, so they bound the drawing
    return {"width": max(xs), "height": max(ys), "boxes": boxes, "blocks": blocks, "paths": paths,
            "nodes": {n["id"]: n for n in nodes}, "hidden_edges": max(0, len(observed) - MAX_DRAWN_EDGES)}


def _route(a: tuple, b: tuple) -> list[tuple[float, float]]:
    """A cubic curve between the box sides that face each other: start, control 1, control 2, end."""
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    acx, acy, bcx, bcy = ax + W / 2, ay + H / 2, bx + W / 2, by + H / 2
    if bx >= ax + W:                                  # target to the right
        s, t = (ax + W, acy), (bx, bcy)
        k = max(30.0, (t[0] - s[0]) / 2)
        return [s, (s[0] + k, s[1]), (t[0] - k, t[1]), t]
    if bx + W <= ax:                                  # target to the left: leave below, come back below
        s, t = (acx, ay + H), (bcx, by + H)
        k = 40.0 + abs(ax - bx) / 6
        return [s, (s[0], s[1] + k), (t[0], t[1] + k), t]
    if by >= ay + H:                                  # same column, below
        s, t = (acx, ay + H), (bcx, by)
        return [s, (s[0], s[1] + GY / 2), (t[0], t[1] - GY / 2), t]
    s, t = (ax + W, acy), (bx + W, bcy)               # same column, above: loop out on the right
    k = 36.0
    return [s, (s[0] + k, s[1]), (t[0] + k, t[1]), t]


# --------------------------------------------------------------------------- SVG (HTML)
def svg(lay: dict) -> str:
    m = 12
    w, h = lay["width"] + 2 * m + 60, lay["height"] + 2 * m + 60
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-m} {-m} {w} {h}" width="100%" '
           f'font-family="Helvetica, Arial, sans-serif" role="img" aria-label="process map">',
           '<defs>'
           f'<marker id="a-order" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           f'<path d="M0,0 L10,5 L0,10 z" fill="{GREY}"/></marker>'
           f'<marker id="a-obs" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">'
           f'<path d="M0,0 L10,5 L0,10 z" fill="{BLUE}"/></marker></defs>']
    for b in lay["blocks"]:
        if b["framed"]:
            out.append(f'<rect x="{b["x"]:.1f}" y="{b["y"]:.1f}" width="{b["w"]:.1f}" height="{b["h"]:.1f}" rx="12" '
                       f'fill="#f5f8fd" stroke="#d5e0f0"/>')
            for i, ln in enumerate(b["title"]):
                out.append(f'<text x="{b["x"] + PAD:.1f}" y="{b["y"] + 20 + 15 * i:.1f}" font-size="13" '
                           f'font-weight="bold" fill="#4a5d7a">{html.escape(ln)}</text>')
    for p in lay["paths"]:
        (sx, sy), (c1x, c1y), (c2x, c2y), (tx, ty) = p["points"]
        order = p.get("kind") == "order"
        out.append(f'<path d="M{sx:.1f},{sy:.1f} C{c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {tx:.1f},{ty:.1f}" fill="none" '
                   f'stroke="{GREY if order else BLUE}" stroke-width="{p["width"]:.2f}"'
                   + (' stroke-dasharray="5 4"' if order else ' stroke-opacity="0.75"')
                   + f' marker-end="url(#{"a-order" if order else "a-obs"})"/>')
    for nid, (x, y) in lay["boxes"].items():
        n = lay["nodes"][nid]
        lines, metric, apps = node_lines(n)
        missing = n.get("missing")
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{W}" height="{H}" rx="8" '
                   f'fill="{"#f6f8fb" if missing else "#eef3fb" if n.get("boundary") else "#ffffff"}" '
                   f'stroke="{GREY if missing else "#9db4d8"}" stroke-width="1.2"'
                   + (' stroke-dasharray="5 4"' if missing else "") + '/>')
        ty = y + 17
        for ln in lines:
            out.append(f'<text x="{x + 9:.1f}" y="{ty:.1f}" font-size="12" font-weight="bold" fill="{INK}">'
                       f'{html.escape(ln)}</text>')
            ty += 14
        if metric:
            out.append(f'<text x="{x + 9:.1f}" y="{y + H - (20 if apps else 8):.1f}" font-size="10" fill="{MUTED}">'
                       f'{html.escape(metric)}</text>')
        if apps:
            out.append(f'<text x="{x + 9:.1f}" y="{y + H - 7:.1f}" font-size="9.5" fill="{BLUE}">{html.escape(apps)}</text>')
    for p in lay["paths"]:                         # counts last, so no box hides them
        if p.get("kind") != "order" and p.get("count") not in (None, ""):
            mx, my = _mid(p["points"])
            out.append(f'<text x="{mx:.1f}" y="{my + 3:.1f}" font-size="10" font-weight="bold" fill="{BLUE}" '
                       f'text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="4">'
                       f'{int(p["count"]):,}</text>')
    out.append("</svg>")
    return "".join(out)


def _mid(points) -> tuple[float, float]:
    (sx, sy), (c1x, c1y), (c2x, c2y), (tx, ty) = points
    return (sx + 3 * c1x + 3 * c2x + tx) / 8, (sy + 3 * c1y + 3 * c2y + ty) / 8


# --------------------------------------------------------------------------- PDF drawing
class MapFlowable(Flowable):
    """The map, scaled to fit the frame (never enlarged)."""

    def __init__(self, lay: dict, max_height: float | None = None):
        super().__init__()
        self.lay = lay
        self.max_height = max_height
        self.scale = 1.0

    def wrap(self, avail_w, avail_h):
        lw, lh = self.lay["width"] + 40, self.lay["height"] + 40
        limit_h = min(avail_h, self.max_height) if self.max_height else avail_h
        self.scale = min(1.0, avail_w / lw, limit_h / lh)
        self.width, self.height = lw * self.scale, lh * self.scale
        return self.width, self.height

    def draw(self):
        c, s, lay = self.canv, self.scale, self.lay
        top = self.height

        def X(x):
            return (x + 20) * s

        def Y(y):
            return top - (y + 20) * s
        for b in lay["blocks"]:
            if b["framed"]:
                c.setFillColor(colors.HexColor("#f5f8fd"))
                c.setStrokeColor(colors.HexColor("#d5e0f0"))
                c.roundRect(X(b["x"]), Y(b["y"] + b["h"]), b["w"] * s, b["h"] * s, 10 * s, stroke=1, fill=1)
                c.setFillColor(colors.HexColor("#4a5d7a"))
                c.setFont("Helvetica-Bold", 12 * s)
                for i, ln in enumerate(b["title"]):
                    c.drawString(X(b["x"] + PAD), Y(b["y"] + 20 + 15 * i), ln)
        for p in lay["paths"]:
            order = p.get("kind") == "order"
            col = colors.HexColor(GREY if order else BLUE)
            c.setStrokeColor(col)
            c.setFillColor(col)
            c.setLineWidth(max(0.4, p["width"] * s))
            c.setDash([4 * s, 3 * s] if order else [])
            pts = [(X(x), Y(y)) for x, y in p["points"]]
            path = c.beginPath()
            path.moveTo(*pts[0])
            path.curveTo(*pts[1], *pts[2], *pts[3])
            c.drawPath(path, stroke=1, fill=0)
            c.setDash([])
            (ex, ey), (px, py) = pts[3], pts[2]
            ang = math.atan2(ey - py, ex - px)
            size = 6 * s
            head = c.beginPath()
            head.moveTo(ex, ey)
            head.lineTo(ex - size * math.cos(ang - 0.4), ey - size * math.sin(ang - 0.4))
            head.lineTo(ex - size * math.cos(ang + 0.4), ey - size * math.sin(ang + 0.4))
            head.close()
            c.drawPath(head, stroke=0, fill=1)
        for nid, (x, y) in lay["boxes"].items():
            n = lay["nodes"][nid]
            lines, metric, apps = node_lines(n)
            missing = n.get("missing")
            c.setFillColor(colors.HexColor("#f6f8fb" if missing else "#eef3fb" if n.get("boundary") else "#ffffff"))
            c.setStrokeColor(colors.HexColor(GREY if missing else "#9db4d8"))
            c.setLineWidth(1.0 * s)
            c.setDash([4 * s, 3 * s] if missing else [])
            c.roundRect(X(x), Y(y + H), W * s, H * s, 7 * s, stroke=1, fill=1)
            c.setDash([])
            c.setFillColor(colors.HexColor(INK))
            c.setFont("Helvetica-Bold", 11 * s)
            ty = y + 17
            for ln in lines:
                c.drawString(X(x + 9), Y(ty), ln)
                ty += 14
            if metric:
                c.setFillColor(colors.HexColor(MUTED))
                c.setFont("Helvetica", 9.5 * s)
                c.drawString(X(x + 9), Y(y + H - (20 if apps else 8)), metric)
            if apps:
                c.setFillColor(colors.HexColor(BLUE))
                c.setFont("Helvetica", 9 * s)
                c.drawString(X(x + 9), Y(y + H - 7), apps)
        for p in lay["paths"]:                     # counts last, on a white backing, so no box hides them
            if p.get("kind") == "order" or p.get("count") in (None, ""):
                continue
            mx, my = _mid(p["points"])
            label = f"{int(p['count']):,}"
            c.setFont("Helvetica-Bold", 9 * s)
            tw = c.stringWidth(label, "Helvetica-Bold", 9 * s)
            c.setFillColor(colors.white)
            c.rect(X(mx) - tw / 2 - 2 * s, Y(my) - 3.5 * s, tw + 4 * s, 11 * s, stroke=0, fill=1)
            c.setFillColor(colors.HexColor(BLUE))
            c.drawCentredString(X(mx), Y(my + 3), label)


# --------------------------------------------------------------------------- documents
def _styles():
    from app.api.pdf import _styles as base
    return base()


def pdf_bytes(el: list, title: str, landscape_page: bool = False) -> bytes:
    buf = io.BytesIO()
    size = landscape(A4) if landscape_page else A4
    SimpleDocTemplate(buf, pagesize=size, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title=title).build(el)
    return buf.getvalue()


_ALLOWED = re.compile(r"&lt;(/?)(b|i|br/?)&gt;")


def _markup(text: str) -> str:
    """reportlab paragraph markup -> safe HTML: everything escaped, then <b>, <i>, <br/> restored."""
    return _ALLOWED.sub(lambda m: f"<{m.group(1)}{m.group(2)}>", html.escape(html.unescape(str(text)), quote=False))


def _cell(v) -> str:
    if isinstance(v, Paragraph):
        return _markup(v.text)
    if isinstance(v, (list, tuple)):
        return "".join(_cell(x) for x in v)
    return "" if v is None else html.escape(str(v))


TAG = {"t": ("h1", ""), "k": ("div", "kicker"), "h2": ("h2", ""), "b": ("p", ""), "s": ("p", "small")}

CSS = """
:root{--ink:#182f50;--muted:#5b6f8c;--line:#dce7f7;--soft:#f3f7fe;--blue:#155eef}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);margin:0;background:#fff}
main{max-width:1180px;margin:0 auto;padding:28px 24px 48px}
h1{font-size:26px;margin:4px 0 6px}h2{font-size:17px;margin:26px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line)}
h3{font-size:14px;margin:18px 0 6px}
.kicker{color:var(--blue);font-size:11px;font-weight:700;letter-spacing:.06em}
.small{color:var(--muted);font-size:12.5px}
p{line-height:1.45;font-size:14px}
table{border-collapse:collapse;width:100%;margin:8px 0 14px;font-size:12.5px}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
th{background:var(--soft)}td.n{text-align:right}
.map{border:1px solid var(--line);border-radius:10px;padding:10px;overflow:auto;background:#fff}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:6px 0}
.legend span::before{content:"";display:inline-block;width:26px;height:0;border-top:2px dashed #9aa5b1;
  vertical-align:middle;margin-right:6px}.legend span.obs::before{border-top:3px solid #155eef}
.legend span.miss::before{width:16px;height:10px;border:1.5px dashed #9aa5b1;border-radius:3px}
.page-break{height:0}.no-print{color:var(--muted);font-size:12px}
@media print{@page{size:A4 landscape;margin:12mm}main{padding:0;max-width:none}.no-print{display:none}
  .page-break{page-break-after:always}h2{page-break-after:avoid}table{page-break-inside:auto}tr{page-break-inside:avoid}}
"""


def elements_html(el: list, title: str) -> str:
    """The same element list as the PDF, as one self-contained HTML page (prints to PDF from a browser)."""
    body = []
    for f in el:
        if isinstance(f, MapFlowable):
            body.append(f'<div class="map">{svg(f.lay)}</div>')
            body.append('<div class="legend"><span>business order (documented)</span><span class="obs">observed '
                        'transition (thicker = more often)</span><span class="miss">step not seen in the data</span></div>')
        elif isinstance(f, Paragraph):
            tag, cls = TAG.get(getattr(f.style, "name", ""), ("p", ""))
            attr = f' class="{cls}"' if cls else ""
            body.append(f"<{tag}{attr}>{_markup(f.text)}</{tag}>")
        elif isinstance(f, Table):
            rows = f._cellvalues
            head = "".join(f"<th>{_cell(v)}</th>" for v in rows[0]) if rows else ""
            rest = "".join("<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in r) + "</tr>" for r in rows[1:])
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rest}</tbody></table>")
        elif isinstance(f, PageBreak):
            body.append('<div class="page-break"></div>')
        elif isinstance(f, Spacer):
            continue
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style>'
            f'</head><body><main>{"".join(body)}<p class="no-print">For a PDF: Print → Save as PDF (landscape).</p>'
            f'</main></body></html>')


# --------------------------------------------------------------------------- map documents
def map_elements(title: str, kicker: str, subtitle: str, nodes: list[dict], edges: list[dict],
                 direction: str = "LR", notes: list[str] | None = None) -> list:
    """Title, the map, its legend notes, the steps table and the transitions table."""
    from app.api.pdf import _table
    st = _styles()
    lay = layout(nodes, edges, direction)
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M")
    el = [Paragraph(html.escape(kicker), st["kicker"]), Paragraph(html.escape(title), st["title"]),
          Paragraph(html.escape(subtitle) + f" · exported {stamp} UTC", st["small"]), Spacer(1, 4),
          MapFlowable(lay, max_height=150 * mm)]
    legend = ["Grey dashed arrows: the business order (documented). Blue arrows: transitions observed in the data, "
              "thicker when more frequent. Dashed boxes: steps of the process not seen in the data."]
    if lay["hidden_edges"]:
        legend.append(f"{lay['hidden_edges']} less frequent transition(s) are not drawn; all are in the table below.")
    el += [Paragraph(html.escape(x), st["small"]) for x in legend + list(notes or [])]
    el += [PageBreak(), Paragraph("Steps", st["h2"])]
    has_hours = any(n.get("hours") not in (None, "") for n in nodes)
    head = ["#", "Stage", "Step"] + (["Hours", "Share"] if has_hours else ["Events", "Cases"]) + ["People", "Applications"]
    rows = [head]
    for i, n in enumerate(nodes, 1):
        metric = ([_fmt_h(n.get("hours")), "" if n.get("share") in (None, "") else f"{100 * float(n['share']):.1f}%"]
                  if has_hours else [f"{int(n['events']):,}" if n.get("events") not in (None, "") else "",
                                     f"{int(n['cases']):,}" if n.get("cases") not in (None, "") else ""])
        rows.append([i, Paragraph(html.escape(str(n.get("group") or "")), st["small"]),
                     Paragraph(html.escape(str(n.get("label") or n.get("id"))) + (" <i>(not seen)</i>" if n.get("missing")
                                                                                 else ""), st["small"])]
                    + metric + [n.get("actors", "") if n.get("actors") is not None else "",
                                Paragraph(html.escape(", ".join(a["app"] for a in (n.get("apps") or [])[:3])), st["small"])])
    el.append(_table(rows, widths=[w * mm for w in (10, 52, 66, 24, 20, 18, 69)]))
    if edges:
        el.append(Paragraph("Transitions", st["h2"]))
        rows = [["From", "To", "Kind", "Times", "Episodes / cases"]]
        for e in sorted(edges, key=lambda e: (e.get("kind") != "order", -float(e.get("count") or 0))):
            rows.append([Paragraph(html.escape(str(e.get("source"))), st["small"]),
                         Paragraph(html.escape(str(e.get("target"))), st["small"]),
                         "business order" if e.get("kind") == "order" else "observed",
                         "" if e.get("count") in (None, "") else f"{int(e['count']):,}",
                         e.get("episodes") if e.get("episodes") is not None else (e.get("cases") or "")])
        el.append(_table(rows, widths=[w * mm for w in (88, 88, 32, 26, 25)]))
    return el


# --------------------------------------------------------------------------- page data as HTML
def _label(key: str) -> str:
    return str(key).replace("_", " ").strip().capitalize()


def _scalar(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _value_html(v, depth: int = 0, max_rows: int = 300) -> str:
    if isinstance(v, dict):
        flat = {k: x for k, x in v.items() if not isinstance(x, (dict, list))}
        nested = {k: x for k, x in v.items() if isinstance(x, (dict, list)) and x}
        out = ""
        if flat:
            out += "<table><tbody>" + "".join(f"<tr><th>{html.escape(_label(k))}</th><td>{html.escape(_scalar(x))}</td></tr>"
                                              for k, x in flat.items()) + "</tbody></table>"
        for k, x in nested.items():
            tag = "h3" if depth < 2 else "p"
            out += f"<{tag}>{html.escape(_label(k))}</{tag}>" + _value_html(x, depth + 1, max_rows)
        return out
    if isinstance(v, list):
        if not v:
            return '<p class="small">None.</p>'
        if all(isinstance(x, dict) for x in v):
            cols = list(dict.fromkeys(k for x in v for k in x))[:14]
            body = "".join("<tr>" + "".join(
                f"<td>{html.escape(_scalar(x.get(c)) if not isinstance(x.get(c), (dict, list)) else json.dumps(x.get(c), ensure_ascii=False)[:160])}</td>"
                for c in cols) + "</tr>" for x in v[:max_rows])
            more = f'<p class="small">{len(v) - max_rows:,} more row(s) not shown.</p>' if len(v) > max_rows else ""
            return ("<table><thead><tr>" + "".join(f"<th>{html.escape(_label(c))}</th>" for c in cols)
                    + f"</tr></thead><tbody>{body}</tbody></table>{more}")
        return "<ul>" + "".join(f"<li>{html.escape(_scalar(x) if not isinstance(x, (dict, list)) else json.dumps(x, ensure_ascii=False)[:300])}</li>"
                                for x in v[:max_rows]) + "</ul>"
    return f"<p>{html.escape(_scalar(v))}</p>"


def data_html(title: str, kicker: str, subtitle: str, sections: list[tuple[str, object]]) -> str:
    """A page's data as one self-contained HTML file: a heading and tables per section."""
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M")
    body = [f'<div class="kicker">{html.escape(kicker)}</div><h1>{html.escape(title)}</h1>',
            f'<p class="small">{html.escape(subtitle)} · exported {stamp} UTC</p>']
    for heading, value in sections:
        if value in (None, {}, []):
            continue
        body.append(f"<h2>{html.escape(heading)}</h2>" + _value_html(value))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style>'
            f'</head><body><main>{"".join(body)}<p class="no-print">For a PDF: Print → Save as PDF.</p></main>'
            f'</body></html>')
