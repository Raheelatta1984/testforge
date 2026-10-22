/* Market position and QA intelligence tabs.

   MarketTab renders GET /api/market/matrix: every capability against every
   compared tool, with TestForge's own column backed by machine-checked evidence
   rather than marketing copy. The audit button re-verifies those claims on the
   server and shows the receipt hash, so a stale claim is visible instead of
   quietly persuasive.

   IntelTab puts the three reuse-first tools behind one panel: step resolution,
   impact/blast radius over the stored test library, and the zero-LLM explorer.
   Nothing here writes to the library; the explorer scans a URL you paste and
   returns findings with their verification state. */

const MARKET_RATING = {
  yes: { mark: "✔", label: "Yes", cls: "bg-emerald-950/60 border-emerald-700 text-emerald-200" },
  partial: { mark: "◐", label: "Partial", cls: "bg-amber-950/50 border-amber-700 text-amber-200" },
  no: { mark: "✖", label: "No", cls: "bg-red-950/40 border-red-800 text-red-200" },
  unknown: { mark: "?", label: "Not stated", cls: "bg-slate-800 border-slate-600 text-slate-300" },
  unverified: { mark: "!", label: "Unverified", cls: "bg-red-900/60 border-red-500 text-red-100" }
};

function MarketCell({ rating, note, self }) {
  const kind = MARKET_RATING[rating] || MARKET_RATING.unknown;
  return e("span", {
    className: `inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold whitespace-nowrap ${kind.cls}${self ? " ring-1 ring-indigo-500/40" : ""}`,
    title: note || kind.label,
    "aria-label": `${kind.label}${note ? ": " + note : ""}`
  }, kind.mark + " " + kind.label);
}

function MarketTab() {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [group, setGroup] = React.useState("all");
  const [query, setQuery] = React.useState("");
  const [gapsOnly, setGapsOnly] = React.useState(false);
  const [showNotes, setShowNotes] = React.useState(true);
  const [openRow, setOpenRow] = React.useState(null);
  const [audit, setAudit] = React.useState(null);

  const load = () => {
    setBusy(true);
    API.get("/api/market/matrix")
      .then(payload => { setData(payload); setError(""); })
      .catch(err => setError(err.message))
      .finally(() => setBusy(false));
  };
  React.useEffect(load, []);

  const runAudit = async () => {
    setBusy(true);
    try {
      const receipt = await API.post("/api/market/audit", {});
      setAudit(receipt);
      setError("");
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return e("div", { className: "space-y-3" },
      e("h1", { className: "text-xl font-bold text-slate-100" }, "⚔️ Market position"),
      error
        ? e("p", { role: "alert", className: "text-sm text-red-300 bg-red-950/40 border border-red-800/40 rounded-lg p-3" }, error)
        : e("p", { className: "text-sm text-slate-400" }, "Loading the audited comparison…"));
  }

  const summary = data.summary || {};
  const tools = data.tools || [];
  const gapIds = new Set((data.gaps || []).map(row => row.id));
  const rows = (data.capabilities || []).filter(row => {
    if (group !== "all" && row.group !== group) return false;
    if (gapsOnly && !gapIds.has(row.id)) return false;
    if (query.trim()) {
      const haystack = `${row.name} ${row.why} ${row.roadmap} ${row.self.note}`.toLowerCase();
      if (!haystack.includes(query.trim().toLowerCase())) return false;
    }
    return true;
  });
  const groups = data.groups || [];
  const ratingOf = (row, tool) => tool.self ? row.self.rating : (row.ratings[tool.id] || "unknown");
  const noteOf = (row, tool) => tool.self ? row.self.note : ((row.notes || {})[tool.id] || "");
  const card = (value, label, hint) => e("div", { className: "rounded-xl border border-slate-800 bg-slate-900 p-3" },
    e("div", { className: "text-xl font-bold text-slate-50" }, String(value)),
    e("div", { className: "text-[9px] font-bold uppercase tracking-wider text-slate-400" }, label),
    hint && e("div", { className: "text-[9px] text-slate-500 mt-1" }, hint));

  return e("div", { className: "space-y-4" },
    e("section", { className: "rounded-2xl border border-indigo-800 bg-slate-900 p-4 space-y-3" },
      e("div", { className: "flex flex-wrap items-center justify-between gap-2" },
        e("div", null,
          e("div", { className: "text-[10px] font-bold tracking-widest text-indigo-300" }, "⚔️ MARKET POSITION"),
          e("h1", { className: "text-xl font-bold text-slate-100" }, "What TestForge does, and how it compares")),
        e("span", {
          className: `rounded-full border px-2 py-1 text-[9px] font-bold ${summary.audit_passed
            ? "border-emerald-700 bg-emerald-950/50 text-emerald-200"
            : "border-red-700 bg-red-950/50 text-red-200"}`
        }, summary.audit_passed ? "CLAIM AUDIT PASSED" : "CLAIM AUDIT FAILED")),
      e("p", { className: "text-sm leading-relaxed text-slate-300" },
        "Capabilities compared against autonomous-QA MCPs, browser-driver MCPs, open-source runners and commercial platforms. ",
        e("strong", { className: "text-slate-100" }, "TestForge's column is audited: "),
        "every Yes is a claim with evidence in this repository, re-checked by the button below. Competitor cells are a point-in-time reading of vendor documentation; a capability the review could not evidence is shown as “Not stated” and is never counted as a win or a loss."),
      e("div", { className: "flex flex-wrap gap-2" },
        e("button", { type: "button", onClick: runAudit, disabled: busy,
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50" },
          busy ? "VERIFYING…" : "RUN CLAIM AUDIT"),
        e("a", { href: "/api/market/report.html", target: "_blank", rel: "noopener noreferrer",
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-slate-800 border border-slate-700 hover:border-indigo-500" },
          "OPEN HTML REPORT ↗"),
        e("a", { href: "/api/market/report.html?download=1",
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-slate-800 border border-slate-700 hover:border-indigo-500" },
          "DOWNLOAD HTML"),
        e("a", { href: "/api/market/report.md?download=1",
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-slate-800 border border-slate-700 hover:border-indigo-500" },
          "DOWNLOAD MARKDOWN"),
        e("a", { href: "/api/market/matrix", target: "_blank", rel: "noopener noreferrer",
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-slate-800 border border-slate-700 hover:border-indigo-500" },
          "RAW JSON ↗")),
      e("p", { className: "text-[10px] text-slate-500" },
        `Reviewed ${data.last_reviewed} · generated ${data.generated_at} · ` +
        `${summary.capabilities} capabilities · ${summary.tools} tools · ` +
        `${summary.claims_verified}/${summary.claims_verified + summary.claims_failed} TestForge claims verified · receipt ${String(summary.receipt_sha256 || "").slice(0, 16)}…`),
      error && e("p", { role: "alert", className: "text-xs text-red-300 bg-red-950/40 border border-red-800/40 rounded-lg p-2" }, error),
      audit && e("div", {
        className: `rounded-lg border p-3 text-[10px] ${audit.summary && audit.summary.passed
          ? "border-emerald-800 bg-emerald-950/30 text-emerald-200"
          : "border-red-800 bg-red-950/30 text-red-200"}`
      },
        e("div", { className: "font-bold" }, audit.summary && audit.summary.passed ? "✔ Every claim still has evidence" : "✖ A claim lost its evidence"),
        e("div", { className: "mt-1 text-slate-300" },
          `${audit.summary ? audit.summary.verified : 0} verified · ${audit.summary ? audit.summary.failed : 0} failed · ` +
          `${audit.summary ? audit.summary.failed_probes : 0} failed probe(s) · receipt ${audit.receipt_sha256 || ""}`),
        audit.receipt_path && e("div", { className: "mt-1 text-slate-400" }, "Receipt written to " + audit.receipt_path))),

    e("div", { className: "grid grid-cols-2 gap-2 md:grid-cols-5" },
      card(`${summary.self_coverage_pct}%`, "TestForge coverage", "Yes + Partial ÷ evidenced capabilities"),
      card(summary.self_yes, "capabilities claimed Yes", "each one evidenced"),
      card(summary.self_partial, "claimed Partial", "subset or needs an agent"),
      card(summary.differentiators, "differentiators", "no compared tool claims them"),
      card(summary.open_gaps, "open gaps", "where a competitor leads")),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-3 space-y-2" },
      e("div", { className: "flex flex-wrap items-center gap-2" },
        e("select", { value: group, onChange: event => setGroup(event.target.value),
          "aria-label": "Filter by group",
          className: "bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-[11px] text-slate-200" },
          e("option", { value: "all" }, "All groups"),
          groups.map(item => e("option", { key: item.id, value: item.id }, item.name))),
        e("input", { value: query, onChange: event => setQuery(event.target.value),
          placeholder: "Search capabilities, notes or roadmap", "aria-label": "Search capabilities",
          className: "flex-1 min-w-[180px] bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-[11px] text-slate-200" }),
        e("label", { className: "flex items-center gap-1 text-[10px] text-slate-300" },
          e("input", { type: "checkbox", checked: gapsOnly, onChange: event => setGapsOnly(event.target.checked) }),
          "Gaps only"),
        e("label", { className: "flex items-center gap-1 text-[10px] text-slate-300" },
          e("input", { type: "checkbox", checked: showNotes, onChange: event => setShowNotes(event.target.checked) }),
          "Show notes"),
        e("span", { className: "text-[10px] text-slate-500" },
          `${rows.length} of ${data.capabilities.length} shown`)),
      e("p", { className: "text-[10px] text-slate-500" },
        "Yes · Partial · No · “Not stated” means the source did not evidence it either way · “Unverified” means a TestForge claim lost its evidence and must not be repeated.")),

    e(MarketMatrixTable, { rows, tools, groups, gapIds, showNotes, openRow, setOpenRow, ratingOf, noteOf }),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2" },
      e("h2", { className: "text-sm font-bold text-indigo-300" }, "Where a competitor leads"),
      e("p", { className: "text-[10px] text-slate-400" },
        "Published deliberately: a comparison that lists only wins is advertising. Each gap names the leading tools and what TestForge plans to do about it."),
      data.gaps.length
        ? e("div", { className: "space-y-1" }, data.gaps.map(gap => e("div", {
          key: gap.id,
          className: "rounded-lg border border-slate-800 bg-slate-950 p-2 text-[10px] flex flex-wrap gap-x-2"
        },
          e("span", { className: "font-bold text-slate-100" }, gap.name),
          e(MarketCell, { rating: gap.our_rating }),
          e("span", { className: "text-amber-200" }, "Leaders: " + gap.leaders.map(id => {
            const tool = tools.find(item => item.id === id);
            return tool ? tool.name : id;
          }).join(", ")),
          e("span", { className: "text-slate-400" }, "· " + gap.roadmap))))
        : e("p", { className: "text-[11px] text-emerald-200" }, "No compared tool claims a capability TestForge does not.")),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2" },
      e("h2", { className: "text-sm font-bold text-indigo-300" }, "Where TestForge leads"),
      e("div", { className: "grid gap-2 md:grid-cols-2" },
        e("div", { className: "rounded-xl border border-slate-800 bg-slate-950 p-3" },
          e("div", { className: "text-[10px] font-bold uppercase tracking-wider text-emerald-300" }, "No compared tool claims these"),
          e("ul", { className: "mt-1 space-y-1 text-[11px] text-slate-300" },
            data.differentiators.map(row => e("li", { key: row.id }, "✔ " + row.name)))),
        e("div", { className: "rounded-xl border border-slate-800 bg-slate-950 p-3" },
          e("div", { className: "text-[10px] font-bold uppercase tracking-wider text-indigo-300" }, "At most one competitor claims these"),
          e("ul", { className: "mt-1 space-y-1 text-[11px] text-slate-300" },
            data.leads.map(row => e("li", { key: row.id }, "✔ " + row.name + " — also " + row.leaders.join(", "))))))),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2" },
      e("h2", { className: "text-sm font-bold text-indigo-300" }, "Trust and operations, per tool"),
      e("div", { className: "overflow-auto" },
        e("table", { className: "w-full text-[10px] border-collapse" },
          e("thead", null, e("tr", null,
            ["Tool", "Licence", "Price", "Deployment", "Model dependency", "Basis"].map(head =>
              e("th", { key: head, className: "text-left p-1.5 border-b border-slate-800 text-slate-300" }, head)))),
          e("tbody", null, tools.map(tool => e("tr", { key: tool.id, className: tool.self ? "bg-indigo-950/30" : "" },
            e("td", { className: "p-1.5 border-b border-slate-800/60 font-bold text-slate-100" }, tool.name),
            e("td", { className: "p-1.5 border-b border-slate-800/60" }, tool.licence),
            e("td", { className: "p-1.5 border-b border-slate-800/60" }, tool.price),
            e("td", { className: "p-1.5 border-b border-slate-800/60" }, tool.deployment),
            e("td", { className: "p-1.5 border-b border-slate-800/60" }, tool.model_dependency || ""),
            e("td", { className: "p-1.5 border-b border-slate-800/60 text-slate-400" }, tool.basis))))))),

    e("p", { className: "text-[10px] text-slate-500" }, data.disclaimer));
}

function MarketMatrixTable({ rows, tools, groups, gapIds, showNotes, openRow, setOpenRow, ratingOf, noteOf }) {
  const body = [];
  groups.forEach(group => {
    const groupRows = rows.filter(row => row.group === group.id);
    if (!groupRows.length) return;
    body.push(e("tr", { key: "group-" + group.id, className: "bg-slate-800/50" },
      e("td", { colSpan: tools.length + 1, className: "p-2 font-bold uppercase tracking-wider text-[9px] text-indigo-200" },
        group.name,
        e("span", { className: "ml-2 font-normal normal-case tracking-normal text-slate-400" }, group.blurb))));
    groupRows.forEach(row => {
      const open = openRow === row.id;
      body.push(e("tr", {
        key: row.id,
        className: "align-top border-b border-slate-800/70 cursor-pointer hover:bg-slate-800/30" +
          (gapIds.has(row.id) ? " ring-1 ring-inset ring-amber-900/40" : ""),
        onClick: () => setOpenRow(open ? null : row.id)
      },
        e("td", { className: "p-2" },
          e("div", { className: "font-bold text-slate-100" }, row.name),
          e("div", { className: "text-[9px] text-slate-500 mt-0.5" }, row.why),
          showNotes && e("div", { className: "text-[9px] text-slate-500 mt-1" }, row.roadmap)),
        tools.map(tool => e("td", {
          key: tool.id,
          className: "p-2" + (tool.self ? " bg-indigo-950/30" : "")
        },
          e(MarketCell, { rating: ratingOf(row, tool), note: noteOf(row, tool), self: tool.self }),
          showNotes && !tool.self && noteOf(row, tool)
            ? e("div", { className: "text-[10px] text-slate-500 mt-1 max-w-[220px]" }, noteOf(row, tool))
            : null))));
      if (open) {
        body.push(e("tr", { key: row.id + "-detail", className: "bg-slate-950/70" },
          e("td", { colSpan: tools.length + 1, className: "p-3 space-y-2" },
            e("div", { className: "text-[11px] text-slate-300" },
              e("strong", { className: "text-slate-100" }, "TestForge: "),
              row.self.note || "Not claimed."),
            row.self.evidence.length
              ? e("div", { className: "space-y-1" },
                e("div", { className: "text-[9px] font-bold uppercase tracking-wider text-slate-400" }, "Evidence probes"),
                row.self.evidence.map((probe, index) => {
                  const result = (row.self.probe_results || [])[index];
                  const ok = result ? result.ok : row.self.verified;
                  return e("div", { key: index, className: "text-[10px] " + (ok ? "text-emerald-300" : "text-red-300") },
                    (ok ? "PASS " : "FAIL ") + probe.kind + " · " + probe.path +
                    (probe.contains ? " · " + probe.contains : "") +
                    (result ? " — " + result.detail : ""));
                }))
              : e("div", { className: "text-[10px] text-slate-500" }, "No evidence probes: this capability is not claimed."),
            e("div", { className: "text-[10px] text-slate-400" },
              e("strong", { className: "text-slate-200" }, "Disposition: "), row.roadmap),
            gapIds.has(row.id)
              ? e("div", { className: "text-[10px] text-amber-300" },
                "A compared tool leads here. Published on purpose.")
              : null)));
      }
    });
  });
  return e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-3 space-y-2" },
    e("div", { className: "flex flex-wrap items-center gap-2" },
      e("span", { className: "text-[10px] font-bold uppercase tracking-wider text-slate-300" }, "Capability matrix"),
      e("span", { className: "text-[10px] text-slate-500" },
        "Tap a row for TestForge evidence and the disposition. Competitor cells carry their basis in the tooltip.")),
    e("div", { className: "overflow-auto max-h-[70vh] rounded-xl border border-slate-800" },
      e("table", { className: "w-full text-[10px] border-collapse" },
        e("thead", { className: "sticky top-0 z-10 bg-slate-950" },
          e("tr", null,
            e("th", { className: "text-left p-2 border-b border-slate-800 min-w-[190px]" }, "Capability"),
            tools.map(tool => e("th", {
              key: tool.id,
              scope: "col",
              title: tool.vendor + " · " + tool.licence + " · " + tool.price,
              className: "p-2 border-b border-slate-800 align-bottom " +
                (tool.self ? "bg-indigo-950/50 text-indigo-200" : "text-slate-300")
            },
              e("div", { className: "font-bold whitespace-nowrap" }, tool.name),
              e("div", { className: "font-normal text-[8px] text-slate-500" }, tool.vendor))))),
        e("tbody", null, body))));
}

window.MarketMatrixTable = MarketMatrixTable;

window.MarketTab = MarketTab;

/* ------------------------------------------------------------------------- */

function IntelResultTable({ rows, empty }) {
  if (!rows || !rows.length) return e("p", { className: "text-[10px] text-slate-500" }, empty || "No results.");
  return e("div", { className: "space-y-1" }, rows.map((row, index) => e("div", {
    key: row.id || index, className: "rounded-lg border border-slate-800 bg-slate-950 p-2 text-[10px]"
  },
    e("div", { className: "flex flex-wrap items-center gap-2" },
      e("span", { className: "font-bold text-slate-100" }, row.name || row.phrase || row.label),
      row.status && e("span", { className: "rounded-full border border-slate-600 px-1.5 text-slate-300" }, row.status),
      row.score !== undefined && e("span", { className: "text-slate-400" }, "score " + row.score),
      row.severity && e("span", {
        className: `rounded-full border px-1.5 ${row.verified ? "border-emerald-700 text-emerald-200" : "border-amber-700 text-amber-200"}`
      }, row.severity + (row.verified ? " · verified" : " · NOT verified"))),
    row.phrase && row.label && row.phrase !== row.label && e("div", { className: "text-slate-400 mt-0.5" }, "phrase: " + row.phrase),
    row.reasons && e("div", { className: "text-slate-400 mt-0.5" }, row.reasons.join(" · ")),
    row.detail && e("div", { className: "text-slate-400 mt-0.5" }, row.detail),
    row.actual && e("div", { className: "text-slate-500 mt-0.5" }, "Actual: " + row.actual))));
}

function IntelTab() {
  const [phrase, setPhrase] = React.useState("");
  const [resolve, setResolve] = React.useState(null);
  const [changed, setChanged] = React.useState("");
  const [impact, setImpact] = React.useState(null);
  const [url, setUrl] = React.useState("");
  const [scan, setScan] = React.useState(null);
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState("");
  const [steps, setSteps] = React.useState(null);

  React.useEffect(() => {
    API.get("/api/steps/summary").then(setSteps).catch(() => {});
  }, []);

  const run = async (label, work) => {
    setBusy(label);
    setError("");
    try { await work(); } catch (err) { setError(err.message); } finally { setBusy(""); }
  };

  const doResolve = () => run("resolve", async () => {
    setResolve(await API.post("/api/steps/resolve", { phrase }));
  });
  const doImpact = () => run("impact", async () => {
    const files = changed.split(/[\n,]+/).map(item => item.trim()).filter(Boolean);
    setImpact(await API.post("/api/impact/analyse", { changed_files: files, routes: [], labels: [] }));
  });
  const doScan = () => run("scan", async () => {
    setScan(await API.post("/api/explorer/scan", {
      url, max_pages: 4, max_depth: 1, include_report: true
    }));
  });

  return e("div", { className: "space-y-4" },
    e("section", { className: "rounded-2xl border border-indigo-800 bg-slate-900 p-4 space-y-2" },
      e("div", { className: "text-[10px] font-bold tracking-widest text-indigo-300" }, "🧠 QA INTELLIGENCE"),
      e("h1", { className: "text-xl font-bold text-slate-100" }, "Reuse, impact and discovery — without a model in the loop"),
      e("p", { className: "text-sm text-slate-300" },
        "Three deterministic answers that decide the cost of a suite: does this step already exist, which tests does my change touch, and what is broken on this page that nobody scripted? ",
        steps && e("span", { className: "text-slate-400" },
          `Catalogue: ${steps.unique_steps} unique steps across ${steps.recordings} recording(s) in ${steps.projects} project(s).`)),
      error && e("p", { role: "alert", className: "text-xs text-red-300 bg-red-950/40 border border-red-800/40 rounded-lg p-2" }, error)),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2" },
      e("h2", { className: "text-sm font-bold text-indigo-300" }, "1 · Resolve a phrase before recording it again"),
      e("p", { className: "text-[10px] text-slate-400" },
        "Type the step the way a tester would say it. Bound means reuse it; ambiguous means choose deliberately; unbound means a new step is justified."),
      e("div", { className: "flex flex-wrap gap-2" },
        e("input", { value: phrase, onChange: event => setPhrase(event.target.value),
          placeholder: "e.g. click place order", "aria-label": "Step phrase",
          className: "flex-1 min-w-[200px] bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-[11px]" }),
        e("button", { type: "button", onClick: doResolve, disabled: !phrase.trim() || busy === "resolve",
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50" },
          busy === "resolve" ? "RESOLVING…" : "RESOLVE STEP")),
      resolve && e("div", { className: "space-y-1" },
        e("div", { className: "text-[11px] text-slate-200" },
          e("strong", null, resolve.status.toUpperCase()), " · " + resolve.advice),
        e(IntelResultTable, { rows: resolve.candidates, empty: "No candidates." }))),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2" },
      e("h2", { className: "text-sm font-bold text-indigo-300" }, "2 · Which tests does this change reach?"),
      e("p", { className: "text-[10px] text-slate-400" },
        "Paste changed file paths (one per line). Smoke and critical tests always run; every other selection names the term that matched. The response includes a ready batch payload."),
      e("textarea", { value: changed, onChange: event => setChanged(event.target.value), rows: 3,
        placeholder: "app/static/checkout-components.js", "aria-label": "Changed files",
        className: "w-full bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-[11px] font-mono" }),
      e("button", { type: "button", onClick: doImpact, disabled: !changed.trim() || busy === "impact",
        className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50" },
        busy === "impact" ? "ANALYSING…" : "ANALYSE IMPACT"),
      impact && e("div", { className: "space-y-1" },
        e("div", { className: "text-[10px] text-slate-400" },
          `${impact.selected_count} of ${impact.catalogue_size} recordings selected · catalogue reduced by ${impact.reduction_pct}% ` +
          (impact.unmatched_hint ? "· " + impact.unmatched_hint : "")),
        e(IntelResultTable, { rows: impact.selected, empty: "Nothing matched. That is a coverage answer." }))),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2" },
      e("h2", { className: "text-sm font-bold text-indigo-300" }, "3 · Explore a black-box target for unscripted defects"),
      e("p", { className: "text-[10px] text-slate-400" },
        "Deterministic checks: HTTP errors, console and page errors (browser mode), failed requests, broken images, duplicate ids, unnamed controls, missing titles, password exposure, mixed content, error leaks, missing security headers, slow responses and placeholder links. A finding is verified only when it reproduces on a fresh fetch."),
      e("div", { className: "flex flex-wrap gap-2" },
        e("input", { value: url, onChange: event => setUrl(event.target.value),
          placeholder: "https://your-test-environment.example", "aria-label": "URL to explore",
          className: "flex-1 min-w-[240px] bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-[11px]" }),
        e("button", { type: "button", onClick: doScan, disabled: !url.trim() || busy === "scan",
          className: "text-[10px] font-bold px-3 py-2 rounded-lg bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50" },
          busy === "scan" ? "SCANNING…" : "SCAN FOR DEFECTS")),
      scan && e("div", { className: "space-y-2" },
        e("div", { className: "text-[10px] text-slate-300" },
          `${scan.counts.total} finding(s) · ${scan.counts.verified} verified · ${scan.counts.unverified} unverified · ` +
          `${scan.counts.not_checked || 0} not checked · ` +
          `mode ${scan.mode} · ${scan.pages.length} page(s) · ${scan.elapsed_seconds}s · receipt ${String(scan.receipt.sha256).slice(0, 16)}…`),
        e(IntelResultTable, { rows: scan.findings, empty: "No findings from these checks." }),
        scan.markdown && e("details", { className: "rounded-lg border border-slate-800 bg-slate-950 p-2" },
          e("summary", { className: "cursor-pointer text-[10px] font-bold text-indigo-200" }, "Defect report (Markdown)"),
          e("pre", { className: "mt-1 max-h-64 overflow-auto whitespace-pre-wrap text-[9px] text-slate-300" }, scan.markdown)))));

}

window.IntelTab = IntelTab;
