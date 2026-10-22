"""Render the market matrix as artefacts an audience can actually read.

Two outputs, both generated from :mod:`app.market_intel` so they can never
disagree with the dashboard:

* a self-contained HTML document — no scripts, no external assets, no network,
  strict CSP, printable and readable on a phone;
* Markdown for a README or a PR description.

Everything is escaped. Competitor names and notes are data, not markup, and the
same document is served with ``Content-Disposition: attachment`` so opening it
cannot run anything.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from app import market_intel

RATING_LABEL = {"yes": "Yes", "partial": "Partial", "no": "No", "unknown": "Not stated", "unverified": "Unverified"}
RATING_MARK = {"yes": "✔", "partial": "◐", "no": "✖", "unknown": "?", "unverified": "!"}
RATING_WORD = {"yes": "yes", "partial": "partial", "no": "no", "unknown": "not stated", "unverified": "unverified"}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tool_lookup(full: dict) -> dict:
    return {tool["id"]: tool for tool in full["tools"]}


def _capability_rows(full: dict) -> list[dict]:
    """Flatten the matrix into rows in a stable display order."""
    groups = {group["id"]: group for group in full["groups"]}
    order = {group["id"]: index for index, group in enumerate(full["groups"])}
    rows = []
    for cap in full["capabilities"]:
        group = groups.get(cap["group"], {})
        rows.append({**cap, "group_name": group.get("name", cap["group"]), "group_order": order.get(cap["group"], 99)})
    rows.sort(key=lambda row: (row["group_order"], row["name"].lower()))
    return rows


# ---------------------------------------------------------------------------
# HTML

_CSS = """
:root { color-scheme: dark; --bg:#020617; --panel:#0b1222; --line:#1e293b; --ink:#e2e8f0;
  --muted:#94a3b8; --self:#6366f1; --yes:#34d399; --partial:#fbbf24; --no:#f87171; --unknown:#64748b; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 28px 18px 64px; }
h1 { font-size: 1.9rem; margin: 0 0 6px; letter-spacing: -.02em; }
h2 { font-size: 1.15rem; margin: 34px 0 10px; color:#c7d2fe; }
h3 { font-size: .95rem; margin: 18px 0 6px; }
p, li { color: var(--ink); }
small, .muted { color: var(--muted); }
a { color:#a5b4fc; }
.eyebrow { font-size: .68rem; letter-spacing:.16em; text-transform: uppercase; color:#a5b4fc; font-weight: 700; }
.lede { font-size: 1rem; color:#cbd5e1; max-width: 78ch; }
.badges { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 0; }
.badge { border:1px solid var(--line); border-radius:999px; padding:5px 10px; font-size:.72rem; font-weight:700; }
.badge.ok { border-color:#065f46; background:#052e2b; color:#6ee7b7; }
.badge.bad { border-color:#7f1d1d; background:#2b0b0b; color:#fca5a5; }
.badge.info { border-color:#3730a3; background:#1e1b4b; color:#c7d2fe; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:18px 0 0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px; }
.card .n { font-size:1.7rem; font-weight:800; letter-spacing:-.03em; }
.card .k { font-size:.7rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }
.table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:14px; background:var(--panel); }
table { border-collapse: collapse; width:100%; font-size:.8rem; }
th, td { border-bottom:1px solid var(--line); padding:8px 9px; text-align:left; vertical-align:top; }
thead th { position:sticky; top:0; background:#0f172a; z-index:2; font-size:.7rem; text-transform:uppercase;
  letter-spacing:.05em; color:#cbd5e1; }
tbody tr:last-child td { border-bottom:none; }
td.cap { min-width: 210px; }
td.cap strong { display:block; font-size:.83rem; }
td.cap span { color:var(--muted); font-size:.72rem; }
th.self, td.self { background:rgba(99,102,241,.12); border-left:2px solid var(--self); border-right:2px solid var(--self); }
th.self { color:#e0e7ff; }
.group-row td { background:#111c33; font-weight:700; color:#c7d2fe; font-size:.72rem;
  text-transform:uppercase; letter-spacing:.07em; }
.pill { display:inline-flex; align-items:center; gap:4px; border-radius:999px; padding:2px 8px;
  font-weight:700; font-size:.72rem; white-space:nowrap; }
.pill.yes { background:rgba(52,211,153,.14); color:#6ee7b7; border:1px solid #065f46; }
.pill.partial { background:rgba(251,191,36,.14); color:#fcd34d; border:1px solid #78350f; }
.pill.no { background:rgba(248,113,113,.10); color:#fca5a5; border:1px solid #7f1d1d; }
.pill.unknown { background:rgba(100,116,139,.16); color:#cbd5e1; border:1px solid #334155; }
.pill.unverified { background:rgba(248,113,113,.18); color:#fecaca; border:1px solid #b91c1c; }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin:10px 0 0; font-size:.78rem; color:var(--muted); }
.note { font-size:.7rem; color:var(--muted); }
details { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin:8px 0; }
summary { cursor:pointer; font-weight:700; color:#c7d2fe; }
ul.tight { margin:6px 0 0 18px; padding:0; }
ul.tight li { margin:2px 0; }
code { font:0.88em ui-monospace, "SFMono-Regular", Consolas, monospace; background:#0f172a;
  border:1px solid var(--line); border-radius:6px; padding:1px 5px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px; }
.callout { border-left:3px solid var(--self); background:rgba(99,102,241,.08); padding:10px 14px; border-radius:0 10px 10px 0; }
footer { margin-top:40px; border-top:1px solid var(--line); padding-top:14px; font-size:.75rem; color:var(--muted); }
@media print {
  :root { color-scheme: light; }
  body { background:#fff; color:#111; }
  main { max-width:none; padding:0; }
  .table-wrap { border-color:#bbb; background:#fff; }
  thead th { background:#eee; color:#111; }
  td, th { border-color:#ccc; }
  .card, details { background:#fff; border-color:#bbb; }
  td.self, th.self { background:#eef; }
  a { color:#111; text-decoration:none; }
  .pill { border:1px solid #999; color:#111 !important; background:#fff !important; }
}
"""


def html_report(full: dict | None = None) -> str:
    full = full or market_intel.matrix()
    summary = full["summary"]
    tool_rows = full["tools"]
    tools = _tool_lookup(full)
    rows = _capability_rows(full)
    audit_ok = summary["audit_passed"]
    generated = _stamp()
    parts: list[str] = []
    add = parts.append

    add("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    add("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    add("<title>TestForge market position · capability comparison</title>")
    add(f"<style>{_CSS}</style></head><body><main>")
    add("<p class=\"eyebrow\">TestForge · market position</p>")
    add("<h1>What TestForge does, and how it compares</h1>")
    add("<p class=\"lede\">A capability-by-capability comparison against the tools proposed to the same buyers: "
        "autonomous QA MCPs, browser-driver MCPs, open-source runners and commercial platforms. "
        "TestForge's column is not marketing — each <strong>Yes</strong> is a claim with machine-checkable "
        "evidence, re-verified below.</p>")
    add("<div class=\"badges\">")
    add(f"<span class=\"badge {'ok' if audit_ok else 'bad'}\">"
        f"{'✔ claim audit passed' if audit_ok else '✖ claim audit failed'} · "
        f"{summary['claims_verified']}/{summary['capabilities']} claims verified</span>")
    add(f"<span class=\"badge info\">{summary['capabilities']} capabilities · {summary['tools']} tools</span>")
    add(f"<span class=\"badge info\">reviewed {_esc(full['last_reviewed'])}</span>")
    add(f"<span class=\"badge info\">document generated {_esc(generated)}</span>")
    add("</div>")

    add("<div class=\"cards\">")
    cards = [
        (f"{summary['self_coverage_pct']}%", "TestForge coverage of the compared capabilities"),
        (f"{summary['self_yes']}", "capabilities claimed Yes"),
        (f"{summary['self_partial']}", "claimed Partial (honest, described)"),
        (f"{summary['differentiators']}", "capabilities no compared tool claims"),
        (f"{summary['open_gaps']}", "open gaps where a competitor leads"),
    ]
    for number, label in cards:
        add(f"<div class=\"card\"><div class=\"n\">{_esc(number)}</div><div class=\"k\">{_esc(label)}</div></div>")
    add("</div>")

    add("<div class=\"callout\" style=\"margin-top:18px\">")
    add("<h3 style=\"margin-top:0\">How to read the table</h3>")
    add("<div class=\"legend\">"
        "<span><span class=\"pill yes\">✔ Yes</span> evidenced, documented capability</span>"
        "<span><span class=\"pill partial\">◐ Partial</span> a subset, vendor-tier only, or needs user code</span>"
        "<span><span class=\"pill no\">✖ No</span> not a capability of that tool</span>"
        "<span><span class=\"pill unknown\">? Not stated</span> the review could not evidence it either way</span>"
        "<span><span class=\"pill unverified\">! Unverified</span> TestForge claim whose evidence no longer passes</span>"
        "</div>")
    add("<p class=\"note\" style=\"margin-bottom:0\">Hover or read the note on a row for the basis of the "
        "important cells. 'Not stated' is never counted as a competitor win and never counted against them.</p>")
    add("</div>")

    add("<h2>Capability matrix</h2>")
    add("<div class=\"table-wrap\"><table><thead><tr><th scope=\"col\">Capability</th>")
    for tool in tool_rows:
        cls = " class=\"self\"" if tool.get("self") else ""
        add(f"<th scope=\"col\"{cls}>{_esc(tool['name'])}<div class=\"note\">{_esc(tool['vendor'])}</div></th>")
    add("</tr></thead><tbody>")
    current_group = None
    columns = [tool["id"] for tool in tool_rows]
    for row in rows:
        if row["group_name"] != current_group:
            current_group = row["group_name"]
            add(f"<tr class=\"group-row\"><td colspan=\"{len(columns) + 1}\">{_esc(current_group)}</td></tr>")
        add("<tr>")
        why = f"<span>{_esc(row['why'])}</span>"
        add(f"<td class=\"cap\"><strong>{_esc(row['name'])}</strong>{why}"
            f"<div class=\"note\">{_esc(row['roadmap'])}</div></td>")
        for tool in tool_rows:
            if tool.get("self"):
                rating = row["self"]["rating"]
                note = row["self"].get("note") or ""
            else:
                rating = row["ratings"].get(tool["id"], "unknown")
                note = (row.get("notes") or {}).get(tool["id"]) or ""
            label = RATING_LABEL.get(rating, rating)
            mark = RATING_MARK.get(rating, "")
            title = f"{tool['name']}: {label}. {note}".strip()
            cls = "self " if tool.get("self") else ""
            add(f"<td class=\"{cls.strip()}\" title=\"{_esc(title)}\">"
                f"<span class=\"pill {_esc(rating)}\" aria-label=\"{_esc(label)}\">{mark} {_esc(label)}</span>"
                + (f"<div class=\"note\">{_esc(note)}</div>" if note and tool.get("self") else "")
                + "</td>")
        add("</tr>")
    add("</tbody></table></div>")

    add("<h2>Scoreboard</h2>")
    add("<p class=\"note\">Score = Yes (1) + Partial (0.5) ÷ capabilities the review could evidence. "
        "'Not stated' cells are excluded so an undocumented tool is neither rewarded nor punished. "
        "This is a breadth measure, not a quality judgement: ten shallow Yes cells do not beat three deep ones.</p>")
    add("<div class=\"table-wrap\"><table><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\">Kind</th>"
        "<th scope=\"col\">Score</th><th scope=\"col\">Yes</th><th scope=\"col\">Partial</th>"
        "<th scope=\"col\">No</th><th scope=\"col\">Not stated</th><th scope=\"col\">Licence and cost</th>"
        "</tr></thead><tbody>")
    for row in full["scoreboard"]:
        tool = tools[row["id"]]
        cls = " class=\"self\"" if row["self"] else ""
        add(f"<tr><td{cls}><strong>{_esc(row['name'])}</strong><div class=\"note\">{_esc(row['vendor'])}</div></td>"
            f"<td>{_esc(row['kind'])}</td><td><strong>{_esc(row['score'])}%</strong></td>"
            f"<td>{row['yes']}</td><td>{row['partial']}</td><td>{row['no']}</td><td>{row['unknown']}</td>"
            f"<td>{_esc(tool['licence'])} · {_esc(tool['price'])}</td></tr>")
    add("</tbody></table></div>")

    add("<h2>Where a competitor leads — the honest gap list</h2>")
    if full["gaps"]:
        add("<p class=\"lede\">These are the capabilities one or more compared tools claim and TestForge does not. "
            "They are published on purpose: a comparison that only lists wins is advertising.</p>")
        add("<div class=\"table-wrap\"><table><thead><tr><th scope=\"col\">Capability</th>"
            "<th scope=\"col\">Our status</th><th scope=\"col\">Tools that claim it</th>"
            "<th scope=\"col\">Disposition</th></tr></thead><tbody>")
        for gap in full["gaps"]:
            add(f"<tr><td><strong>{_esc(gap['name'])}</strong></td>"
                f"<td><span class=\"pill {_esc(gap['our_rating'])}\">{_esc(RATING_LABEL.get(gap['our_rating'], gap['our_rating']))}</span></td>"
                f"<td>{_esc(', '.join(tools[tool]['name'] for tool in gap['leaders']))}</td>"
                f"<td class=\"note\">{_esc(gap['roadmap'])}</td></tr>")
        add("</tbody></table></div>")
    else:
        add("<p>No compared tool claims a capability TestForge does not.</p>")

    add("<h2>Where TestForge leads</h2>")
    add("<div class=\"grid2\">")
    add("<div class=\"card\"><h3 style=\"margin-top:0\">No compared tool claims these</h3>")
    if full["differentiators"]:
        add("<ul class=\"tight\">")
        for row in full["differentiators"]:
            partial = f" (partial elsewhere: {', '.join(tools[t]['name'] for t in row['partial_elsewhere'])})" if row["partial_elsewhere"] else ""
            add(f"<li><strong>{_esc(row['name'])}</strong>{_esc(partial)}</li>")
        add("</ul>")
    else:
        add("<p>None.</p>")
    add("</div>")
    add("<div class=\"card\"><h3 style=\"margin-top:0\">At most one competitor claims these</h3>")
    if full["leads"]:
        add("<ul class=\"tight\">")
        for row in full["leads"]:
            add(f"<li><strong>{_esc(row['name'])}</strong> — also claimed by {_esc(', '.join(tools[t]['name'] for t in row['leaders']))}</li>")
        add("</ul>")
    else:
        add("<p>None.</p>")
    add("</div></div>")

    add("<h2>Trust and operations</h2>")
    add("<div class=\"table-wrap\"><table><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\">Licence</th>"
        "<th scope=\"col\">Price</th><th scope=\"col\">Deployment</th><th scope=\"col\">Model dependency</th>"
        "<th scope=\"col\">Basis for this review</th></tr></thead><tbody>")
    for tool in tool_rows:
        cls = " class=\"self\"" if tool.get("self") else ""
        add(f"<tr><td{cls}><strong>{_esc(tool['name'])}</strong></td><td>{_esc(tool['licence'])}</td>"
            f"<td>{_esc(tool['price'])}</td><td>{_esc(tool['deployment'])}</td>"
            f"<td>{_esc(tool.get('model_dependency') or '')}</td>"
            f"<td class=\"note\">{_esc(tool['basis'])}</td></tr>")
    add("</tbody></table></div>")

    add("<h2>Evidence appendix — every TestForge claim, checked</h2>")
    add(f"<p class=\"note\">Audit generated {_esc(full['audit'].get('generated_at'))} · "
        f"expires {_esc(full['audit'].get('expires_at'))} · receipt "
        f"<code>{_esc(full['audit'].get('receipt_sha256'))}</code>. "
        "Run <code>GET /api/market/audit</code> or <code>python -m tools.market_audit</code> to reproduce it.</p>")
    for row in rows:
        own = row["self"]
        status = "verified" if own["verified"] else "unverified"
        add("<details>")
        add(f"<summary>{_esc(RATING_MARK.get(own['rating'], ''))} {_esc(row['name'])} — "
            f"{_esc(RATING_LABEL.get(own['rating'], own['rating']))} · {_esc(status)}</summary>")
        add(f"<p>{_esc(own.get('note') or '')}</p>")
        if own["probe_results"]:
            add("<ul class=\"tight\">")
            for item in own["probe_results"]:
                probe = item["probe"]
                mark = "✔" if item["ok"] else "✖"
                add(f"<li>{mark} <code>{_esc(probe.get('kind'))}</code> "
                    f"<code>{_esc(probe.get('path'))}</code>"
                    f"{(' · ' + _esc(probe.get('contains'))) if probe.get('contains') else ''}"
                    f" — {_esc(item['detail'])}</li>")
            add("</ul>")
        else:
            add("<p class=\"note\">No evidence probes: this capability is not claimed.</p>")
        add("</details>")

    add("<h2>Method and limits</h2>")
    add(f"<p>{_esc(full['disclaimer'])}</p>")
    add("<h3>What this comparison does not prove</h3>")
    add("<ul class=\"tight\">")
    add("<li>It does not run the other tools. Ratings come from vendor documentation and the supplied "
        "GitHub MCP listings, not from a hands-on bake-off against the same application.</li>")
    add("<li>A 'Yes' means the capability is documented or evidenced, not that it is fast, stable or pleasant. "
        "Breadth scores are not a quality ranking.</li>")
    add("<li>Commercial tools are priced and packaged per contract; the price column is indicative.</li>")
    add("<li>TestForge claims are audited against this repository only. That is a strong statement about "
        "existence and wiring, not a claim of production deployment.</li>")
    add("</ul>")
    add("<h3>Sources</h3><ul class=\"tight\">")
    for source in full.get("sources") or []:
        add(f"<li><a href=\"{_esc(source['url'])}\" rel=\"noopener noreferrer nofollow\">{_esc(source['title'])}</a> "
            f"<span class=\"note\">({_esc(source.get('kind'))}, accessed {_esc(source.get('accessed'))})</span></li>")
    add("</ul>")

    add("<footer>Generated by TestForge from <code>app/market_data.json</code> and re-verified against the "
        "repository by the claim audit. Offline, deterministic and reproducible: the same inputs produce the "
        "same document. Competitor names are used for identification only; no affiliation or endorsement is "
        "claimed.</footer>")
    add("</main></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Markdown

def _md(value) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_report(full: dict | None = None) -> str:
    full = full or market_intel.matrix()
    summary = full["summary"]
    tools = _tool_lookup(full)
    rows = _capability_rows(full)
    columns = full["tools"]
    lines: list[str] = []
    add = lines.append
    add("# TestForge market position")
    add("")
    add(f"Capability comparison of **{summary['tools']} tools** across **{summary['capabilities']} capabilities**. "
        f"Reviewed {full['last_reviewed']}. Generated {_stamp()}.")
    add("")
    add(f"**Claim audit:** {'PASSED' if summary['audit_passed'] else 'FAILED'} — "
        f"{summary['claims_verified']}/{summary['capabilities']} TestForge claims verified by evidence probes. "
        f"Receipt `{summary.get('receipt_sha256') or 'n/a'}`.")
    add("")
    add("Legend: ✔ yes · ◐ partial · ✖ no · ? not stated by the source (not counted as a win or a loss) · "
        "! unverified TestForge claim.")
    add("")
    add("## Scoreboard")
    add("")
    add("| Tool | Kind | Score | Yes | Partial | No | Not stated | Licence / price |")
    add("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in full["scoreboard"]:
        tool = tools[row["id"]]
        add(f"| {'**' + _md(row['name']) + '**' if row['self'] else _md(row['name'])} | {_md(row['kind'])} | "
            f"{row['score']}% | {row['yes']} | {row['partial']} | {row['no']} | {row['unknown']} | "
            f"{_md(tool['licence'])} · {_md(tool['price'])} |")
    add("")
    add("## Capability matrix")
    add("")
    add("| Capability | " + " | ".join(_md(tool["name"]) for tool in columns) + " |")
    add("| --- | " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        cells = []
        for tool in columns:
            rating = row["self"]["rating"] if tool.get("self") else row["ratings"].get(tool["id"], "unknown")
            cells.append({"yes": "✔", "partial": "◐", "no": "✖", "unknown": "?", "unverified": "!"}.get(rating, "?"))
        add(f"| {_md(row['name'])} | " + " | ".join(cells) + " |")
    add("")
    add("## Gaps where a competitor leads")
    add("")
    if full["gaps"]:
        add("| Capability | Ours | Leaders | Disposition |")
        add("| --- | --- | --- | --- |")
        for gap in full["gaps"]:
            add(f"| {_md(gap['name'])} | {_md(gap['our_rating'])} | "
                f"{_md(', '.join(tools[tool]['name'] for tool in gap['leaders']))} | {_md(gap['roadmap'])} |")
    else:
        add("None.")
    add("")
    add("## Differentiators (no compared tool claims them)")
    add("")
    for row in full["differentiators"]:
        add(f"- **{_md(row['name'])}**")
    if not full["differentiators"]:
        add("- None.")
    add("")
    add("## Evidence for every TestForge claim")
    add("")
    for row in rows:
        own = row["self"]
        add(f"### {row['name']} — {own['rating']} ({'verified' if own['verified'] else 'unverified'})")
        add("")
        add(_md(own.get("note") or ""))
        add("")
        if own["probe_results"]:
            for item in own["probe_results"]:
                probe = item["probe"]
                add(f"- {'PASS' if item['ok'] else 'FAIL'} `{probe.get('kind')}` `{probe.get('path')}` — {_md(item['detail'])}")
        else:
            add("- No evidence probes: capability not claimed.")
        add("")
    add("## Method and limits")
    add("")
    add(full["disclaimer"])
    add("")
    add("Ratings are a point-in-time reading of documentation, not a hands-on bake-off. A 'Yes' means the "
        "capability exists and is documented, not that it is fast or pleasant. Breadth is not quality. "
        "Prices are indicative. TestForge claims are audited against this repository; that proves wiring, "
        "not production deployment.")
    add("")
    add("### Sources")
    add("")
    for source in full.get("sources") or []:
        add(f"- [{_md(source['title'])}]({source['url']}) — {_md(source.get('kind'))}, accessed {_md(source.get('accessed'))}")
    add("")
    return "\n".join(lines)
