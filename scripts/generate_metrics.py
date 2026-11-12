#!/usr/bin/env python3
"""
Generates monochrome minimalist SVG profile cards:

  stats.svg    -> general GitHub statistics + top language ring
  streak.svg   -> total contributions / current streak / longest streak
  graph.svg    -> last 31 days contribution graph
  heatmap.svg  -> daily commit heatmap (last 365 days)

Usage:
  GH_TOKEN=<token> GH_USER=Emerson349 python scripts/generate_metrics.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

USER = os.environ.get("GH_USER", "Emerson349")
DISPLAY = os.environ.get("GH_DISPLAY_NAME", "Emerson")
TOKEN = os.environ.get("GH_TOKEN", "").strip()
OUT = Path(os.environ.get("OUT_DIR", "dist"))
TZ = dt.timezone(dt.timedelta(hours=int(os.environ.get("TZ_OFFSET", "-3"))))
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
NO_ANIM = "* { animation: none !important; }" if os.environ.get("NO_ANIM") else ""

BG, BORDER, FG, MUTED, DIM, FAINT = "#050505", "#262626", "#f2f2f2", "#8b8b8b", "#2e2e2e", "#151515"
LEVELS = [FAINT, "#3a3a3a", "#6b6b6b", "#a8a8a8", "#f2f2f2"]
SANS = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"
MONO = "'JetBrains Mono', 'SF Mono', 'Fira Code', Consolas, 'Courier New', monospace"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_date(d: dt.date | None, year: bool = True) -> str:
    if d is None:
        return "—"
    return f"{MONTHS[d.month - 1]} {d.day}" + (f", {d.year}" if year else "")


def fmt_range(a: dt.date | None, b: dt.date | None) -> str:
    if a is None:
        return "No active streak"
    if b == TODAY:
        return f"{fmt_date(a, a.year != TODAY.year)} – Present"
    same = a.year == b.year
    return f"{fmt_date(a, not same)} – {fmt_date(b)}"


# ----------------------------------------------------------------------------- data
def request(url: str, body: dict | None = None, accept: str = "application/vnd.github+json") -> str:
    headers = {"User-Agent": f"{USER}-profile-metrics", "Accept": accept}
    if TOKEN:
        headers["Authorization"] = f"bearer {TOKEN}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=40) as res:
        return res.read().decode("utf-8")


def graphql(query: str, variables: dict) -> dict:
    res = json.loads(request("https://api.github.com/graphql", {"query": query, "variables": variables}))
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    return res["data"]


def compute_top_language(lang_Totals: dict[str, int]) -> tuple[str, float]:
    filtered = {k: v for k, v in lang_Totals.items() if k and k not in ("HTML", "CSS", "Jupyter Notebook") and v > 0}
    if not filtered:
        filtered = {k: v for k, v in lang_Totals.items() if k and v > 0}
    if not filtered:
        return "Java", 100.0
    total = sum(filtered.values())
    top_lang, top_val = max(filtered.items(), key=lambda kv: kv[1])
    return top_lang, (top_val / total) * 100.0


def fetch_graphql():
    q1 = """query($login:String!,$from:DateTime!){user(login:$login){
      createdAt followers{totalCount}
      repositories(ownerAffiliations:OWNER,isFork:false,first:100){
        nodes{
          stargazerCount
          languages(first:8,orderBy:{field:SIZE,direction:DESC}){edges{size node{name}}}
        }
      }
      pullRequests{totalCount} issues{totalCount}
      repositoriesContributedTo(contributionTypes:[COMMIT,ISSUE,PULL_REQUEST,REPOSITORY]){totalCount}
      year:contributionsCollection(from:$from){
        totalCommitContributions restrictedContributionsCount totalPullRequestReviewContributions}
      contributionsCollection{contributionYears}}}"""
    start = dt.datetime(TODAY.year, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    u = graphql(q1, {"login": USER, "from": start})["user"]

    years = u["contributionsCollection"]["contributionYears"]
    parts = [
        f'y{y}:contributionsCollection(from:"{y}-01-01T00:00:00Z",to:"{y}-12-31T23:59:59Z")'
        "{contributionCalendar{weeks{contributionDays{date contributionCount}}}}"
        for y in years
    ]
    q2 = "query($login:String!){user(login:$login){" + " ".join(parts) + "}}"
    cal: dict[dt.date, int] = {}
    for coll in graphql(q2, {"login": USER})["user"].values():
        for week in coll["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                cal[dt.date.fromisoformat(day["date"])] = day["contributionCount"]

    lang_totals: dict[str, int] = {}
    for repo in u["repositories"]["nodes"]:
        for edge in (repo.get("languages") or {}).get("edges") or []:
            name = edge["node"]["name"]
            lang_totals[name] = lang_totals.get(name, 0) + int(edge["size"])
    top_lang, top_pct = compute_top_language(lang_totals)

    y = u["year"]
    stats = {
        "stars": sum(n["stargazerCount"] for n in u["repositories"]["nodes"]),
        "commits": y["totalCommitContributions"] + y["restrictedContributionsCount"],
        "prs": u["pullRequests"]["totalCount"],
        "issues": u["issues"]["totalCount"],
        "contributed": u["repositoriesContributedTo"]["totalCount"],
        "reviews": y["totalPullRequestReviewContributions"],
        "followers": u["followers"]["totalCount"],
        "top_lang": top_lang,
        "top_lang_pct": top_pct,
    }
    return stats, cal, dt.date.fromisoformat(u["createdAt"][:10])


def fetch_public():
    u = json.loads(request(f"https://api.github.com/users/{USER}"))
    created = dt.date.fromisoformat(u["created_at"][:10])
    repos = json.loads(request(f"https://api.github.com/users/{USER}/repos?per_page=100&type=owner"))

    def search(kind: str, q: str) -> int:
        try:
            url = f"https://api.github.com/search/{kind}?q={urllib.parse.quote(q)}&per_page=1"
            return json.loads(request(url))["total_count"]
        except Exception:
            return 0

    lang_totals: dict[str, int] = {}
    for r in repos:
        if not r.get("fork") and r.get("language"):
            lang = r["language"]
            lang_totals[lang] = lang_totals.get(lang, 0) + max(int(r.get("size") or 1), 1)
    top_lang, top_pct = compute_top_language(lang_totals)

    stats = {
        "stars": sum(r["stargazers_count"] for r in repos if not r.get("fork")),
        "commits": search("commits", f"author:{USER} author-date:>={TODAY.year}-01-01"),
        "prs": search("issues", f"author:{USER} type:pr"),
        "issues": search("issues", f"author:{USER} type:issue"),
        "contributed": len({r["full_name"] for r in repos}),
        "reviews": 0,
        "followers": u["followers"],
        "top_lang": top_lang,
        "top_lang_pct": top_pct,
    }

    cal: dict[dt.date, int] = {}
    for year in range(created.year, TODAY.year + 1):
        page = request(
            f"https://github.com/users/{USER}/contributions?from={year}-01-01&to={year}-12-31",
            accept="text/html",
        )
        tips = {
            m.group(1): m.group(2)
            for m in re.finditer(r'<tool-tip[^>]*\bfor="([^"]+)"[^>]*>([^<]*)</tool-tip>', page)
        }
        for tag in re.findall(r"<td\b[^>]*data-date=[^>]*>", page):
            date = re.search(r'data-date="([\d-]+)"', tag).group(1)
            ident = re.search(r'\bid="([^"]+)"', tag)
            text = tips.get(ident.group(1), "") if ident else ""
            m = re.match(r"\s*(\d+)", text)
            cal[dt.date.fromisoformat(date)] = int(m.group(1)) if m else 0
    return stats, cal, created


def streaks(cal: dict[dt.date, int], created: dt.date):
    start = created
    total = sum(v for d, v in cal.items() if start <= d <= TODAY)

    longest = (0, None, None)
    run, run_start = 0, None
    d = start
    while d <= TODAY:
        if cal.get(d, 0) > 0:
            if run == 0:
                run_start = d
            run += 1
            if run > longest[0]:
                longest = (run, run_start, d)
        else:
            run = 0
        d += dt.timedelta(days=1)

    end = TODAY if cal.get(TODAY, 0) > 0 else TODAY - dt.timedelta(days=1)
    cur, d = 0, end
    while d >= start and cal.get(d, 0) > 0:
        cur += 1
        d -= dt.timedelta(days=1)
    current = (cur, d + dt.timedelta(days=1), end) if cur else (0, None, None)
    return total, current, longest, start


# ----------------------------------------------------------------------------- svg
def card(w: int, h: int, body: str, tag: str, css: str = "") -> str:
    c = 14
    corners = (
        f"M8,{8 + c} V8 H{8 + c} M{w - 8 - c},8 H{w - 8} V{8 + c} "
        f"M8,{h - 8 - c} V{h - 8} H{8 + c} M{w - 8 - c},{h - 8} H{w - 8} V{h - 8 - c}"
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" fill="none" role="img">
<style>
  text {{ font-family: {SANS}; fill: {FG}; }}
  .mono {{ font-family: {MONO}; }}
  .muted {{ fill: {MUTED}; }}
  .fade {{ animation: fade .7s ease-out both; }}
  @keyframes fade {{ from {{ opacity: 0; }} }}
  {css}
  {NO_ANIM}
</style>
<defs>
  <clipPath id="frame"><rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="6"/></clipPath>
  <filter id="grain" x="0" y="0" width="100%" height="100%">
    <feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" stitchTiles="stitch"/>
    <feColorMatrix type="saturate" values="0"/>
    <feComponentTransfer><feFuncA type="table" tableValues="0 .07"/></feComponentTransfer>
  </filter>
  <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </linearGradient>
</defs>
<g clip-path="url(#frame)">
  <rect width="{w}" height="{h}" fill="{BG}"/>
  <rect width="{w}" height="{h}" filter="url(#grain)"/>
  <path d="{corners}" stroke="{DIM}"/>
  <text x="{w - 26}" y="26" text-anchor="end" class="mono muted" font-size="9.5" letter-spacing="1">{esc(tag)}</text>
  {body}
</g>
<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="6" stroke="{BORDER}"/>
</svg>
"""


ICONS = {
    "star": '<path d="M7 1.2l1.8 3.7 4 .6-2.9 2.8.7 4L7 10.4 3.4 12.3l.7-4L1.2 5.5l4-.6z"/>',
    "commit": '<circle cx="7" cy="7" r="2.6"/><path d="M.5 7h3.9M9.6 7h3.9"/>',
    "pr": '<circle cx="3" cy="2.8" r="1.8"/><circle cx="3" cy="11.2" r="1.8"/><circle cx="11" cy="11.2" r="1.8"/>'
          '<path d="M3 4.6v4.8M11 9.4V5.2a1.8 1.8 0 0 0-1.8-1.8H6.5M8.1 1.8 6.5 3.4 8.1 5"/>',
    "issue": '<circle cx="7" cy="7" r="5.8"/><circle cx="7" cy="7" r="1.2" fill="currentColor"/>',
    "repo": '<path d="M2.5 12V2.5A1.5 1.5 0 0 1 4 1h7.5v9.5H4A1.5 1.5 0 0 0 2.5 12zm0 0A1.5 1.5 0 0 0 4 13.5h7.5"/>',
}


def stats_svg(s: dict) -> str:
    top_lang = s.get("top_lang", "Java")
    top_pct = float(s.get("top_lang_pct", 100.0))
    rows = [
        ("star", "Total Stars Earned:", s["stars"]),
        ("commit", f"Total Commits ({TODAY.year}):", s["commits"]),
        ("pr", "Total PRs:", s["prs"]),
        ("issue", "Total Issues:", s["issues"]),
        ("repo", "Contributed to (last year):", s["contributed"]),
    ]
    body = [f'<text x="25" y="36" font-size="16" font-weight="700" class="fade">{esc(DISPLAY)}&apos;s GitHub Stats</text>']
    for i, (icon, label, value) in enumerate(rows):
        y = 74 + i * 24
        delay = 150 + i * 120
        value_str = f"{value:,}"
        body.append(
            f'<g class="fade" style="animation-delay:{delay}ms">'
            f'<g transform="translate(25 {y - 11})" stroke="{MUTED}" color="{MUTED}" stroke-width="1.3" '
            f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS[icon]}</g>'
            f'<text x="48" y="{y}" font-size="13">{esc(label)}</text>'
            f'<text x="262" y="{y}" font-size="13" font-weight="700" class="mono">{value_str}</text></g>'
        )

    cx, cy, r = 405, 102, 42
    circ = 2 * math.pi * r
    offset = circ * (1 - min(max(top_pct, 5.0), 100.0) / 100.0)
    ticks = "".join(
        f'<line x1="{cx + (r + 9) * math.cos(a):.1f}" y1="{cy + (r + 9) * math.sin(a):.1f}" '
        f'x2="{cx + (r + 13) * math.cos(a):.1f}" y2="{cy + (r + 13) * math.sin(a):.1f}"/>'
        for a in (2 * math.pi * k / 24 for k in range(24))
    )
    lang_font = 15 if len(top_lang) <= 6 else (12.5 if len(top_lang) <= 10 else 11)
    body.append(f"""
  <g stroke="{DIM}" stroke-width="1">{ticks}</g>
  <circle cx="{cx}" cy="{cy}" r="{r}" stroke="{DIM}" stroke-width="6"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" stroke="{FG}" stroke-width="6" stroke-linecap="round"
          stroke-dasharray="{circ:.2f}" stroke-dashoffset="{offset:.2f}" transform="rotate(-90 {cx} {cy})" class="ring"/>
  <text x="{cx}" y="{cy - 1}" text-anchor="middle" font-size="{lang_font}" font-weight="700" class="mono fade"
        style="animation-delay:900ms">{esc(top_lang)}</text>
  <text x="{cx}" y="{cy + 16}" text-anchor="middle" font-size="10.5" class="mono muted fade"
        style="animation-delay:1000ms">{top_pct:.0f}%</text>
  <text x="{cx}" y="{cy + r + 32}" text-anchor="middle" font-size="10.5" class="mono muted fade"
        style="animation-delay:1100ms">top language</text>""")
    css = (f".ring {{ animation: ring 1.6s cubic-bezier(.4,0,.2,1) .3s both; }} "
           f"@keyframes ring {{ from {{ stroke-dashoffset: {circ:.2f}; }} }}")
    return card(495, 195, "\n  ".join(body), "// STATS", css)


def streak_svg(total: int, current, longest, start: dt.date) -> str:
    cur_n, cur_a, cur_b = current
    lon_n, lon_a, lon_b = longest
    cx, cy, r = 247.5, 80, 38
    circ = 2 * math.pi * r
    flame = "M0,-9 C4,-4 6.5,0 4.5,4.5 C3,7.5 -3,7.5 -4.5,4.5 C-6.5,0 -2,-3.5 0,-9 Z"
    body = f"""
  <line x1="165" y1="30" x2="165" y2="165" stroke="{BORDER}"/>
  <line x1="330" y1="30" x2="330" y2="165" stroke="{BORDER}"/>

  <g class="fade" style="animation-delay:150ms">
    <text x="82.5" y="96" text-anchor="middle" font-size="28" font-weight="700" class="mono">{total}</text>
    <text x="82.5" y="128" text-anchor="middle" font-size="13">Total Contributions</text>
    <text x="82.5" y="155" text-anchor="middle" font-size="10.5" class="muted">{esc(fmt_date(start))} – Present</text>
  </g>

  <circle cx="{cx}" cy="{cy}" r="{r}" stroke="{DIM}" stroke-width="5"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" stroke="{FG}" stroke-width="5" stroke-linecap="round"
          stroke-dasharray="{circ:.2f}" stroke-dashoffset="0" transform="rotate(-90 {cx} {cy})" class="ring"/>
  <circle cx="{cx}" cy="{cy - r}" r="11" fill="{BG}"/>
  <path d="{flame}" transform="translate({cx} {cy - r})" fill="{FG}" class="fade" style="animation-delay:1.2s"/>
  <g class="fade" style="animation-delay:400ms">
    <text x="{cx}" y="{cy + 10}" text-anchor="middle" font-size="28" font-weight="700" class="mono">{cur_n}</text>
    <text x="{cx}" y="142" text-anchor="middle" font-size="13.5" font-weight="700">Current Streak</text>
    <text x="{cx}" y="163" text-anchor="middle" font-size="10.5" class="muted">{esc(fmt_range(cur_a, cur_b))}</text>
  </g>

  <g class="fade" style="animation-delay:650ms">
    <text x="412.5" y="96" text-anchor="middle" font-size="28" font-weight="700" class="mono">{lon_n}</text>
    <text x="412.5" y="128" text-anchor="middle" font-size="13">Longest Streak</text>
    <text x="412.5" y="155" text-anchor="middle" font-size="10.5" class="muted">{esc(fmt_range(lon_a, lon_b))}</text>
  </g>"""
    css = f".ring {{ animation: ring 1.4s cubic-bezier(.4,0,.2,1) .2s both; }} @keyframes ring {{ from {{ stroke-dashoffset: {circ:.2f}; }} }}"
    return card(495, 195, body, "// STREAK", css)


def nice_max(v: int) -> tuple[int, int]:
    step = max(1, math.ceil(max(v, 4) / 4))
    return step * 4, step


def graph_svg(cal: dict[dt.date, int]) -> str:
    W, H = 1000, 340
    L, R, T, B = 72, 968, 78, 268
    days = [TODAY - dt.timedelta(days=30 - i) for i in range(31)]
    vals = [cal.get(d, 0) for d in days]
    top, step = nice_max(max(vals))
    x = lambda i: L + (R - L) * i / 30  # noqa: E731
    y = lambda v: B - (B - T) * v / top  # noqa: E731

    grid, ylabels = [], []
    for k in range(0, top + 1, step):
        grid.append(f'<line x1="{L}" y1="{y(k):.1f}" x2="{R}" y2="{y(k):.1f}" stroke="{DIM}" stroke-dasharray="2 5"/>')
        ylabels.append(f'<text x="{L - 14}" y="{y(k) + 4:.1f}" text-anchor="end" font-size="10.5" class="mono muted">{k}</text>')

    xlabels = []
    for i, d in enumerate(days):
        xlabels.append(f'<text x="{x(i):.1f}" y="{B + 20}" text-anchor="middle" font-size="10" class="mono muted">{d.day}</text>')
        if i == 0 or d.day == 1:
            xlabels.append(f'<text x="{x(i):.1f}" y="{B + 36}" text-anchor="middle" font-size="9.5" class="mono" '
                           f'fill="{FG}" letter-spacing="1">{MONTHS[d.month - 1].upper()}</text>')

    pts = [(x(i), y(v)) for i, v in enumerate(vals)]
    line = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = line + f" L{R},{B} L{L},{B} Z"
    dots, labels = [], []
    for i, ((px, py), v) in enumerate(zip(pts, vals)):
        dots.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.2" fill="{BG}" stroke="{FG}" stroke-width="1.6" '
                    f'class="fade" style="animation-delay:{300 + i * 55}ms"/>')
        if v > 0:
            labels.append(f'<text x="{px:.1f}" y="{py - 10:.1f}" text-anchor="middle" font-size="9.5" class="mono muted fade" '
                          f'style="animation-delay:{1600 + i * 30}ms">{v}</text>')
    tx, ty = pts[-1]
    total = sum(vals)
    body = f"""
  <text x="{W / 2}" y="40" text-anchor="middle" font-size="16" font-weight="700" class="fade">{esc(DISPLAY)}&apos;s Contribution Graph</text>
  <text x="26" y="26" font-size="9.5" class="mono muted" letter-spacing="1">LAST 31 DAYS · {total} CONTRIBUTIONS · AVG {total / 31:.1f}/DAY</text>
  {''.join(grid)}
  {''.join(ylabels)}
  {''.join(xlabels)}
  <path d="{area}" fill="url(#area)" class="fade" style="animation-delay:1.2s;animation-duration:1.2s"/>
  <path d="{line}" stroke="{FG}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"
        pathLength="1" stroke-dasharray="1" stroke-dashoffset="0" class="draw"/>
  {''.join(dots)}
  {''.join(labels)}
  <circle cx="{tx:.1f}" cy="{ty:.1f}" r="4" stroke="{FG}" stroke-width="1.2">
    <animate attributeName="r" values="4;14" dur="2s" repeatCount="indefinite"/>
    <animate attributeName="opacity" values=".9;0" dur="2s" repeatCount="indefinite"/>
  </circle>
  <text transform="translate(26 {(T + B) / 2}) rotate(-90)" text-anchor="middle" font-size="10" class="mono muted" letter-spacing="1.5">CONTRIBUTIONS</text>
  <text x="{(L + R) / 2}" y="{H - 12}" text-anchor="middle" font-size="10" class="mono muted" letter-spacing="1.5">DAYS</text>"""
    css = ".draw { animation: draw 2.2s cubic-bezier(.4,0,.2,1) .2s both; } @keyframes draw { from { stroke-dashoffset: 1; } }"
    return card(W, H, body, "// 31D", css)


def heatmap_svg(cal: dict[dt.date, int], current) -> str:
    W, H = 1000, 242
    L, T, P, C = 72, 74, 17, 13
    first = TODAY - dt.timedelta(days=(TODAY.weekday() + 1) % 7) - dt.timedelta(weeks=52)
    days = [first + dt.timedelta(days=i) for i in range((TODAY - first).days + 1)]
    vals = [cal.get(d, 0) for d in days]

    nz = sorted(v for v in vals if v > 0)
    q = [nz[int(len(nz) * f)] for f in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]

    def level(v: int) -> int:
        if v <= 0:
            return 0
        return 1 if v <= q[0] else 2 if v <= q[1] else 3 if v <= q[2] else 4

    cells, months = [], []
    last_month = None
    for d, v in zip(days, vals):
        col, row = (d - first).days // 7, (d.weekday() + 1) % 7
        cx, cy = L + col * P, T + row * P
        lv = level(v)
        stroke = ' stroke="#1f1f1f"' if lv == 0 else ""
        cells.append(f'<rect x="{cx}" y="{cy}" width="{C}" height="{C}" rx="2.5" fill="{LEVELS[lv]}"{stroke} '
                     f'class="cell" style="animation-delay:{col * 18 + row * 6}ms"/>')
        if row == 0 and d.month != last_month:
            if col < 52:
                months.append(f'<text x="{cx}" y="{T - 10}" font-size="10" class="mono muted">{MONTHS[d.month - 1].lower()}</text>')
            last_month = d.month

    tcol, trow = (TODAY - first).days // 7, (TODAY.weekday() + 1) % 7
    tx, ty = L + tcol * P, T + trow * P
    weekdays = "".join(
        f'<text x="{L - 12}" y="{T + r * P + 10}" text-anchor="end" font-size="9.5" class="mono muted">{n}</text>'
        for r, n in ((1, "mon"), (3, "wed"), (5, "fri"))
    )
    empty_stroke = ' stroke="#1f1f1f"'
    legend = "".join(
        f'<rect x="{W - 136 + i * P}" y="{H - 34}" width="{C}" height="{C}" rx="2.5" fill="{c}"'
        f'{empty_stroke if i == 0 else ""}/>'
        for i, c in enumerate(LEVELS)
    )
    total = sum(vals)
    today_n = cal.get(TODAY, 0)
    body = f"""
  <text x="26" y="44" font-size="16" font-weight="700" class="fade">{total} contributions in the last year</text>
  <text x="{W - 26}" y="46" text-anchor="end" font-size="11" class="mono fade" style="animation-delay:300ms">
    <tspan class="muted">today</tspan> {today_n}  <tspan class="muted">·  streak</tspan> {current[0]}d
  </text>
  {''.join(months)}
  {weekdays}
  {''.join(cells)}
  <rect x="{tx - 2.5}" y="{ty - 2.5}" width="{C + 5}" height="{C + 5}" rx="4" stroke="{FG}">
    <animate attributeName="opacity" values="1;.15;1" dur="2s" repeatCount="indefinite"/>
  </rect>
  <text x="26" y="{H - 24}" font-size="9.5" class="mono muted" letter-spacing="1">
    UPDATED {esc(fmt_date(TODAY).upper())} · {NOW:%H:%M} UTC-3
  </text>
  <text x="{W - 144}" y="{H - 24}" text-anchor="end" font-size="9.5" class="mono muted">less</text>
  {legend}
  <text x="{W - 26}" y="{H - 24}" text-anchor="end" font-size="9.5" class="mono muted">more</text>"""
    css = ".cell { animation: fade .45s ease-out both; }"
    return card(W, H, body, "// 365D", css)


# ----------------------------------------------------------------------------- main
def main() -> None:
    if TOKEN:
        try:
            stats, cal, created = fetch_graphql()
        except Exception as exc:  # pragma: no cover
            print(f"GraphQL failed ({exc}); using public fallback.")
            stats, cal, created = fetch_public()
    else:
        stats, cal, created = fetch_public()

    cal = {d: v for d, v in cal.items() if d <= TODAY}
    total, current, longest, start = streaks(cal, created)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stats.svg").write_text(stats_svg(stats), encoding="utf-8")
    (OUT / "streak.svg").write_text(streak_svg(total, current, longest, start), encoding="utf-8")
    (OUT / "graph.svg").write_text(graph_svg(cal), encoding="utf-8")
    (OUT / "heatmap.svg").write_text(heatmap_svg(cal, current), encoding="utf-8")
    print(json.dumps({"stats": stats, "total": total, "current": current[0], "longest": longest[0]}, default=str))


if __name__ == "__main__":
    main()
