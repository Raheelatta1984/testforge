/* QA Agents tab: nine QA agents that work on pasted text or on data already in
   this instance (runs, recordings, projects). The rules engine always answers;
   the optional AI layer only adds insights when the server has a key.

   The input form is built from each agent's sample (GET /api/agents/{id}/sample),
   so the fields always match what the server accepts. Text is sent as a string,
   a list of strings as one line per item, and JSON fields are parsed before
   sending. Loaded before index.html's inline script, like the other components. */

const QA_AGENT_INFO = {
  requirements: {
    blurb: "Finds vague terms, open questions, missing numbers, sensitive behaviour without security rules, missing error handling and unhandled edge cases.",
    existing: "No equivalent existed before this hub.",
  },
  "test-cases": {
    blurb: "Turns acceptance criteria into positive, negative and boundary test cases, with boundary values, a CSV export and a Gherkin feature file.",
    existing: "No equivalent existed. Recordings capture browser steps; nothing derived cases from requirements.",
  },
  "api-contract": {
    blurb: "Reviews an OpenAPI 3 or Swagger 2 document (JSON or YAML) for security, error responses and sensitive fields, and drafts contract tests.",
    existing: "Only per-test assertions inside the recorder. The spec itself was not reviewed.",
  },
  "code-review": {
    blurb: "Reviews Playwright or Selenium test code for fragile locators, hard-coded sleeps, credentials, hard-coded URLs and tests without assertions.",
    existing: "The import validator checks syntax only.",
  },
  "failure-triage": {
    blurb: "Classifies each failure as product, automation, data or environment with a confidence score and the signals behind it. Works on a run or on pasted errors.",
    existing: "classify_failure in batch_runner.py: six buckets, used only to decide retries.",
  },
  flaky: {
    blurb: "Explains why a test is flaky from its run history: verdict, flip rate, timing ratio, the errors that repeat, and likely causes with fixes.",
    existing: "A retry that passes is marked \"flaky\" in plan_runtime.py. No analysis of the history.",
  },
  "defect-drafting": {
    blurb: "Drafts a defect report from a run: summary, severity, steps, expected and actual results, redacted log excerpt. Output as Markdown, Jira wiki and a CSV row.",
    existing: "Jira CSV per step in export_formats.py. No narrative defect report.",
  },
  regression: {
    blurb: "Selects the tests that a change should run, from changed files and an optional diff, and returns a batch payload for POST /api/runs/batch.",
    existing: "No regression selection existed. Batches run whatever is chosen by hand.",
  },
  "release-summary": {
    blurb: "Gives a GO, CONDITIONAL GO, NO-GO or INSUFFICIENT EVIDENCE decision for a project window from latest run results, flaky tests, defects and risks, with a Markdown report.",
    existing: "Per-run PDF, HTML and JSON reports only (report_routes.py, pdf_reports.py). No release decision.",
  },
};

const QA_SOURCE_LABELS = {
  run: { label: "Run ID", hint: "A run from the Runs tab. Used by Failure Triage and Defect Drafting." },
  recording: { label: "Recording ID", hint: "A recording from the Test Library. Its recent runs feed Flaky Test Analysis." },
  project: { label: "Project ID", hint: "The active project. Its latest runs feed Regression Selection and Release Summary." },
};

const QA_LABELS = {
  requirements: "Requirement text", title: "Title", text: "Text", feature: "Feature name",
  spec: "OpenAPI / Swagger document", source: "Test code", filename: "File name",
  errors: "Error messages (one per line)", log: "Log excerpt", context: "Context (JSON)",
  runs: "Runs (JSON list)", name: "Name", changed_files: "Changed files (one per line)",
  diff: "Diff (optional)", tests: "Test catalogue (JSON list)", max_tests: "Maximum tests",
  release: "Release name", window: "Window label", known_defects: "Known defects (JSON list)",
  open_risks: "Open risks (one per line)", critical_names: "Critical test names (one per line)",
  steps: "Steps (one per line)", actual: "Actual result", expected: "Expected result",
  environment: "Environment (JSON)", severity: "Severity", reporter: "Reporter",
  component: "Component", run_id: "Run ID", recording: "Recording name",
};

function qaFieldKind(value) {
  if (Array.isArray(value) && value.every(item => typeof item === "string")) return "lines";
  if (typeof value === "number") return "number";
  if (typeof value === "string") return (value.includes("\n") || value.length > 80) ? "long" : "text";
  return "json";
}

function qaFieldText(value, kind) {
  if (kind === "lines") return value.join("\n");
  if (kind === "json") return JSON.stringify(value, null, 2);
  return value === null || value === undefined ? "" : String(value);
}

function qaVerdictTone(verdict) {
  const v = String(verdict || "").toLowerCase();
  if (/no-go|fix before|gap|flaky|incomplete|needs|blocked|stop/.test(v)) return "bg-red-950/40 border-red-800/40 text-red-200";
  if (/conditional|mixed|review|insufficient|nothing/.test(v)) return "bg-amber-950/40 border-amber-800/60 text-amber-200";
  return "bg-emerald-900/60 border-emerald-700/60 text-emerald-200";
}

const QA_SEVERITY_TONE = {
  critical: "bg-red-900 border-red-700 text-red-100",
  high: "bg-red-950/40 border-red-800/40 text-red-200",
  medium: "bg-amber-950/40 border-amber-800/60 text-amber-200",
  low: "bg-slate-800 border-slate-700 text-slate-200",
  info: "bg-slate-900 border-slate-800 text-slate-400",
};

function qaDownload(filename, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function qaCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    return false;
  }
}

function QAFindings({ findings }) {
  if (!findings || !findings.length) {
    return e("p", { className: "text-xs text-slate-400" }, "No rule-based findings.");
  }
  return e("ul", { className: "space-y-2" }, findings.map((f, i) =>
    e("li", { key: f.id + i, className: "rounded-lg border border-slate-800 bg-slate-950/70 p-3" },
      e("div", { className: "flex flex-wrap items-center gap-2" },
        e("span", { className: `text-[10px] font-bold px-2 py-0.5 rounded border uppercase ${QA_SEVERITY_TONE[f.severity] || QA_SEVERITY_TONE.info}` }, f.severity),
        e("span", { className: "text-[10px] font-mono text-slate-400" }, f.id),
        e("span", { className: "text-sm text-slate-100" }, f.title)),
      f.detail && e("p", { className: "text-xs text-slate-300 mt-1 break-words" }, f.detail),
      f.location && e("p", { className: "text-[11px] font-mono text-slate-500 mt-1 break-all" }, f.location),
      f.suggestion && e("p", { className: "text-xs text-indigo-200 mt-1" }, "Fix: " + f.suggestion))));
}

function QATable({ headers, rows }) {
  return e("div", { className: "overflow-auto max-h-72 border border-slate-800 rounded-lg" },
    e("table", { className: "w-full text-xs" },
      e("thead", null, e("tr", { className: "text-left text-slate-400" },
        headers.map(h => e("th", { key: h, className: "px-2 py-1 font-bold whitespace-nowrap" }, h)))),
      e("tbody", null, rows.map((row, i) =>
        e("tr", { key: i, className: "border-t border-slate-800 align-top" },
          row.map((cell, j) => e("td", { key: j, className: "px-2 py-1 break-words" }, cell === null || cell === undefined ? "" : String(cell))))))));
}

function QACodeBlock({ title, text, filename, mime }) {
  const [copied, setCopied] = useState("");
  return e("div", { className: "space-y-1" },
    e("div", { className: "flex flex-wrap items-center gap-2" },
      e("span", { className: "text-xs font-bold text-slate-300" }, title),
      e("button", { type: "button", className: "key-btn", onClick: async () => {
        setCopied((await qaCopy(text)) ? "Copied" : "Copy blocked by browser");
        setTimeout(() => setCopied(""), 1500);
      } }, "COPY"),
      filename && e("button", { type: "button", className: "key-btn", onClick: () => qaDownload(filename, text, mime) }, "DOWNLOAD"),
      copied && e("span", { className: "text-[10px] text-emerald-300" }, copied)),
    e("pre", { className: "text-[11px] font-mono whitespace-pre-wrap break-words max-h-72 overflow-auto bg-slate-950/70 border border-slate-800 rounded-lg p-3 text-slate-200" }, text));
}

function QAArtifacts({ data }) {
  const a = data.artifacts || {};
  const blocks = [];
  if (Array.isArray(a.cases)) {
    blocks.push(e("div", { key: "cases", className: "space-y-2" },
      e(QATable, {
        headers: ["ID", "Type", "Priority", "Technique", "Title", "Expected"],
        rows: a.cases.map(c => [c.id, c.type, c.priority, c.technique, c.title, c.expected]),
      }),
      a.csv && e(QACodeBlock, { title: "CSV export", text: a.csv, filename: "test-cases.csv", mime: "text/csv" }),
      a.gherkin && e(QACodeBlock, { title: "Gherkin feature", text: a.gherkin, filename: "test-cases.feature", mime: "text/plain" })));
  }
  if (Array.isArray(a.operations) && a.operations.length) {
    blocks.push(e("div", { key: "ops", className: "space-y-1" },
      e("span", { className: "text-xs font-bold text-slate-300" }, "Operations"),
      e(QATable, { headers: ["Method", "Path", "Operation", "Secured", "Responses"],
        rows: a.operations.map(o => [o.method, o.path, o.operation_id, o.secured ? "yes" : "no", (o.responses || []).join(", ")]) })));
  }
  if (Array.isArray(a.contract_tests) && a.contract_tests.length) {
    blocks.push(e("div", { key: "contract", className: "space-y-1" },
      e("span", { className: "text-xs font-bold text-slate-300" }, "Contract tests"),
      e(QATable, { headers: ["ID", "Operation", "Request", "Check", "Expected status"],
        rows: a.contract_tests.map(t => [t.id, t.operation, t.request, t.title, t.expected_status]) })));
  }
  if (Array.isArray(a.classifications) && a.classifications.length) {
    blocks.push(e("div", { key: "triage", className: "space-y-1" },
      e("span", { className: "text-xs font-bold text-slate-300" }, "Classifications"),
      e(QATable, { headers: ["#", "Category", "Confidence", "Owner", "Executor bucket", "Agrees", "Error"],
        rows: a.classifications.map(c => [c.index, c.category, c.confidence, c.owner,
          c.executor_bucket, c.executor_agrees === null ? "no opinion" : (c.executor_agrees ? "yes" : "no"), c.error] ) })));
  }
  if (Array.isArray(a.hypotheses) && a.hypotheses.length) {
    blocks.push(e("div", { key: "hyp", className: "space-y-1" },
      e("span", { className: "text-xs font-bold text-slate-300" }, "Likely causes"),
      e(QATable, { headers: ["Cause", "Confidence", "Evidence", "Suggested fix"],
        rows: a.hypotheses.map(h => [h.cause, h.confidence, (h.evidence || []).join("; "), h.fix]) })));
  }
  if (Array.isArray(a.selected)) {
    blocks.push(e("div", { key: "sel", className: "space-y-2" },
      e("span", { className: "text-xs font-bold text-slate-300" }, `Selected tests (${a.selected.length} of ${a.catalogue_size || "?"}, ${a.coverage_percent ?? 0}% of catalogue)`),
      e(QATable, { headers: ["Name", "Priority", "Score", "Matched on", "Changed by", "Always run"],
        rows: a.selected.map(t => [t.name, t.priority, t.score, (t.reasons || []).join(", "), (t.changed_by || []).join(", "), t.always_run ? "yes" : ""]) }),
      a.unmatched_changes && a.unmatched_changes.length > 0 && e("p", { className: "text-xs text-amber-200" },
        "Changes with no matching test: " + a.unmatched_changes.join(", ")),
      a.batch_payload && e(QACodeBlock, { title: "Batch payload for POST /api/runs/batch", text: JSON.stringify(a.batch_payload, null, 2), filename: "regression-batch.json", mime: "application/json" })));
  }
  if (a.draft) {
    const d = a.draft;
    blocks.push(e("div", { key: "draft", className: "space-y-2" },
      e(QACodeBlock, { title: "Markdown", text: d.markdown || "", filename: "defect.md", mime: "text/markdown" }),
      e(QACodeBlock, { title: "Jira wiki markup", text: d.jira_wiki || "", filename: "defect-jira.txt", mime: "text/plain" }),
      e(QACodeBlock, { title: "Jira CSV row", text: JSON.stringify(d.jira_csv_row || {}, null, 2), filename: "defect-jira.json", mime: "application/json" })));
  }
  if (typeof a.markdown === "string" && a.markdown) {
    blocks.push(e(QACodeBlock, { key: "md", title: "Release report (Markdown)", text: a.markdown, filename: "release-summary.md", mime: "text/markdown" }));
  }
  blocks.push(e("details", { key: "json", className: "text-xs" },
    e("summary", { className: "cursor-pointer text-slate-400" }, "Full result as JSON"),
    e(QACodeBlock, { title: "Result", text: JSON.stringify(data, null, 2), filename: (data.agent || "agent") + "-result.json", mime: "application/json" })));
  return e("div", { className: "space-y-4" }, blocks);
}

function QAResult({ data }) {
  const ai = data.ai || {};
  return e("section", { className: "rounded-2xl border border-slate-800 bg-slate-900/40 p-4 space-y-4", "aria-live": "polite" },
    e("div", { className: "flex flex-wrap items-center gap-3" },
      e("h3", { className: "text-lg font-bold text-slate-100" }, data.title),
      e("span", { className: `text-xs font-bold px-3 py-1 rounded-full border uppercase ${qaVerdictTone(data.verdict)}` }, data.verdict),
      data.score !== null && data.score !== undefined && e("span", { className: "text-xs text-slate-300" }, `Score ${data.score}/100`),
      e("span", { className: "text-[10px] font-bold px-2 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-200" },
        data.engine === "rules+ai" ? "RULES + AI" : "RULES ENGINE"),
      data.generated_at && e("span", { className: "text-[10px] text-slate-500" }, data.generated_at)),
    e("p", { className: "text-sm text-slate-200" }, data.summary),
    (data.warnings || []).length > 0 && e("ul", { className: "text-xs text-amber-200 list-disc pl-5" },
      data.warnings.map((w, i) => e("li", { key: i }, w))),
    e("div", { className: "rounded-lg border border-indigo-900/60 bg-indigo-950/30 p-3 space-y-2" },
      e("span", { className: "text-[10px] font-bold tracking-widest text-indigo-300" }, "AI INSIGHTS"),
      (data.ai_insights || []).length > 0
        ? e("ul", { className: "text-xs text-slate-200 list-disc pl-5 space-y-1" }, data.ai_insights.map((t, i) => e("li", { key: i }, t)))
        : e("p", { className: "text-xs text-slate-400" }, ai.reason || "No AI insights for this run."),
      ai.error && e("p", { className: "text-xs text-amber-300" }, "AI step: " + ai.error)),
    e("div", null,
      e("span", { className: "text-xs font-bold text-slate-300" }, `Findings (${(data.findings || []).length})`),
      e("div", { className: "mt-2" }, e(QAFindings, { findings: data.findings }))),
    e(QAArtifacts, { data }));
}

function QAAgentsTab({ project, setTab }) {
  const [catalogue, setCatalogue] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [selectedId, setSelectedId] = useState("requirements");
  const [mode, setMode] = useState("auto");
  const [kinds, setKinds] = useState({});
  const [form, setForm] = useState({});
  const [sourceType, setSourceType] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  useEffect(() => {
    API.get("/api/agents")
      .then(data => { setCatalogue(data); setSelectedId(data.agents[0] ? data.agents[0].id : ""); })
      .catch(err => setLoadError(String(err.message || err)));
  }, []);

  const agents = catalogue ? catalogue.agents : [];
  const agent = agents.find(a => a.id === selectedId) || null;
  const info = QA_AGENT_INFO[selectedId] || { blurb: "", existing: "" };

  const loadSample = async (id) => {
    setError("");
    try {
      const data = await API.get(`/api/agents/${id}/sample`);
      const nextKinds = {};
      const nextForm = {};
      Object.entries(data.input || {}).forEach(([key, value]) => {
        const kind = qaFieldKind(value);
        nextKinds[key] = kind;
        nextForm[key] = qaFieldText(value, kind);
      });
      setKinds(nextKinds);
      setForm(nextForm);
    } catch (err) {
      setError("Could not load the sample: " + (err.message || err));
    }
  };

  // Switching agent starts from that agent's sample and its first stored-data source.
  useEffect(() => {
    if (!catalogue || !selectedId) return;
    const chosen = catalogue.agents.find(a => a.id === selectedId);
    const sources = chosen ? chosen.sources || [] : [];
    setResult(null);
    setError("");
    setSourceType(sources[0] || "");
    setSourceId(sources[0] === "project" && project ? project.id : "");
    loadSample(selectedId);
  }, [selectedId, catalogue]);

  const buildInput = () => {
    const out = {};
    for (const [key, kind] of Object.entries(kinds)) {
      const raw = form[key] === undefined ? "" : String(form[key]);
      if (kind === "lines") {
        out[key] = raw.split("\n").map(s => s.trim()).filter(Boolean);
      } else if (kind === "number") {
        if (raw.trim() !== "") out[key] = Number(raw);
      } else if (kind === "json") {
        if (raw.trim() === "") continue;
        try { out[key] = JSON.parse(raw); } catch (err) {
          throw new Error(`"${QA_LABELS[key] || key}" is not valid JSON: ${err.message}`);
        }
      } else {
        out[key] = raw;
      }
    }
    return out;
  };

  const send = async (path, body) => {
    setBusy(true);
    setError("");
    try {
      const data = await API.post(path, body);
      setResult(data);
    } catch (err) {
      setResult(null);
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  };

  const runPasted = () => {
    let input;
    try { input = buildInput(); } catch (err) { setError(err.message); return; }
    send(`/api/agents/${selectedId}/run`, { input, mode });
  };

  const runFromSource = () => {
    if (!sourceType) { setError("This agent takes pasted input only."); return; }
    if (!sourceId.trim()) { setError(`Enter the ${(QA_SOURCE_LABELS[sourceType] || {}).label || "ID"} first.`); return; }
    const key = sourceType === "run" ? "run_id" : sourceType === "recording" ? "recording_id" : "project_id";
    send(`/api/agents/${selectedId}/from-source`, { source: { type: sourceType, [key]: sourceId.trim() }, mode });
  };

  if (loadError) {
    return e("section", { className: "p-6 space-y-2" },
      e("h2", { className: "text-xl font-bold text-slate-100" }, "QA Agents"),
      e("p", { className: "text-sm text-red-300" }, "The agent catalogue did not load: " + loadError),
      e("p", { className: "text-xs text-slate-400" }, "Reload the page. If it persists, check the server log for app/qa_agents."));
  }
  if (!catalogue) {
    return e("section", { className: "p-6 text-sm text-slate-400" }, "Loading QA agents…");
  }

  const sourceOptions = agent ? agent.sources || [] : [];
  const aiStatus = catalogue.ai || {};

  return e("section", { className: "space-y-4 p-2", "aria-labelledby": "qa-agents-title" },
    e("header", { className: "space-y-2" },
      e("h2", { id: "qa-agents-title", className: "text-2xl font-bold text-slate-100" }, "QA Agents"),
      e("p", { className: "text-sm text-slate-300 max-w-md" },
        "Nine QA agents for requirements, tests, API contracts, automation code, failures, flakiness, defects, regression scope and release decisions. "
        + "They are deterministic by default. An optional AI layer adds insights when the server has an API key; it never changes the rule findings."),
      e("div", { className: "flex flex-wrap gap-2 text-[11px]" },
        e("span", { className: `px-2 py-1 rounded border ${aiStatus.available ? "bg-emerald-900/60 border-emerald-700/60 text-emerald-200" : "bg-slate-800 border-slate-700 text-slate-300"}` },
          aiStatus.available ? `AI insights on (${aiStatus.model})` : "AI insights off: rules only"),
        e("span", { className: "text-slate-400" }, aiStatus.reason || "")),
      e("p", { className: "text-[11px] text-slate-500" },
        "Results are advisory: a QA lead decides. Stored data is read-only; nothing is published or run from here.")),
    e("div", { className: "grid grid-cols-1 md:grid-cols-3 gap-4" },
      e("nav", { className: "space-y-2", "aria-label": "QA agents" },
        agents.map(a => e("button", {
          key: a.id, type: "button", onClick: () => setSelectedId(a.id),
          "aria-current": a.id === selectedId ? "true" : undefined,
          className: `w-full text-left rounded-xl border p-3 ${a.id === selectedId ? "border-indigo-500 bg-indigo-950/40" : "border-slate-800 bg-slate-900/40 hover:border-indigo-700"}`,
        },
          e("div", { className: "flex items-center gap-2" },
            e("span", { "aria-hidden": true }, a.icon),
            e("span", { className: "text-sm font-bold text-slate-100" }, a.name)),
          e("div", { className: "text-[10px] text-slate-400 mt-1" },
            "Input: " + a.input + ((a.sources || []).length ? " · also from " + a.sources.join(" / ") : ""))))),
      agent && e("div", { className: "md:col-span-2 space-y-4" },
        e("div", { className: "rounded-2xl border border-slate-800 bg-slate-900/40 p-4 space-y-2" },
          e("h3", { className: "text-lg font-bold text-slate-100" }, agent.icon + " " + agent.name),
          e("p", { className: "text-sm text-slate-300" }, info.blurb),
          e("p", { className: "text-xs text-slate-400" }, "Existing in TestForge: " + info.existing)),
        e("div", { className: "rounded-2xl border border-slate-800 bg-slate-900/40 p-4 space-y-3" },
          e("div", { className: "flex flex-wrap items-end gap-3" },
            e("label", { className: "text-xs text-slate-300 flex flex-col gap-1" }, "Mode",
              e("select", { value: mode, onChange: ev => setMode(ev.target.value),
                className: "bg-slate-950/70 border border-slate-700 rounded-lg p-2 text-sm text-slate-100" },
                e("option", { value: "auto" }, "Auto (AI when available)"),
                e("option", { value: "rules" }, "Rules only"))),
            e("button", { type: "button", className: "key-btn", onClick: () => loadSample(selectedId), disabled: busy }, "LOAD SAMPLE"),
            e("button", { type: "button", className: "key-btn", onClick: runPasted, disabled: busy }, busy ? "RUNNING…" : "RUN ON INPUT BELOW")),
          Object.keys(kinds).length === 0 && e("p", { className: "text-xs text-slate-400" }, "No input fields. Load the sample to see the expected fields."),
          Object.keys(kinds).map(key => e("label", { key, className: "block text-xs text-slate-300 space-y-1" },
            e("span", { className: "font-bold" }, QA_LABELS[key] || key),
            kinds[key] === "long" || kinds[key] === "lines" || kinds[key] === "json"
              ? e("textarea", { value: form[key] || "", rows: kinds[key] === "json" ? 6 : 8, spellCheck: false,
                  onChange: ev => setForm({ ...form, [key]: ev.target.value }),
                  className: "w-full font-mono text-[11px] bg-slate-950/70 border border-slate-700 rounded-lg p-2 text-slate-100" })
              : e("input", { type: kinds[key] === "number" ? "number" : "text", value: form[key] || "",
                  onChange: ev => setForm({ ...form, [key]: ev.target.value }),
                  className: "w-full font-mono text-[11px] bg-slate-950/70 border border-slate-700 rounded-lg p-2 text-slate-100" })))),
        sourceOptions.length > 0 && e("div", { className: "rounded-2xl border border-slate-800 bg-slate-900/40 p-4 space-y-3" },
          e("span", { className: "text-sm font-bold text-slate-100" }, "Or run on data already in this instance"),
          e("div", { className: "flex flex-wrap items-end gap-3" },
            e("label", { className: "text-xs text-slate-300 flex flex-col gap-1" }, "Source",
              e("select", { value: sourceType, onChange: ev => { setSourceType(ev.target.value); setSourceId(ev.target.value === "project" && project ? project.id : ""); },
                className: "bg-slate-950/70 border border-slate-700 rounded-lg p-2 text-sm text-slate-100" },
                sourceOptions.map(s => e("option", { key: s, value: s }, QA_SOURCE_LABELS[s].label)))),
            e("label", { className: "text-xs text-slate-300 flex flex-col gap-1 flex-1 min-w-[12rem]" },
              (QA_SOURCE_LABELS[sourceType] || {}).label || "ID",
              e("input", { type: "text", value: sourceId, onChange: ev => setSourceId(ev.target.value), placeholder: (QA_SOURCE_LABELS[sourceType] || {}).hint,
                className: "w-full font-mono text-[11px] bg-slate-950/70 border border-slate-700 rounded-lg p-2 text-slate-100" })),
            e("button", { type: "button", className: "key-btn", onClick: runFromSource, disabled: busy }, "RUN ON STORED DATA")),
          e("p", { className: "text-[11px] text-slate-400" }, (QA_SOURCE_LABELS[sourceType] || {}).hint || "")),
        e("div", { className: "flex flex-wrap items-center gap-3" },
          e("span", { className: "text-[11px] text-slate-400" }, "Need a run or recording ID?"),
          e("button", { type: "button", className: "key-btn", onClick: () => setTab && setTab("runs") }, "OPEN RUNS"),
          e("button", { type: "button", className: "key-btn", onClick: () => setTab && setTab("library") }, "OPEN TEST LIBRARY")),
        error && e("p", { role: "alert", className: "text-sm text-red-300 bg-red-950/40 border border-red-800/40 rounded-lg p-3" }, error),
        result && e(QAResult, { data: result })))
  );
}

window.QAAgentsTab = QAAgentsTab;
