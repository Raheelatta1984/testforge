/* Variable onboarding after a save: the literal values that were typed become
   reusable variables or parameters.

   The browser AI recording and the Windows desktop recording both use this, so
   both ask the same question in the same way: the saved steps are proposed as
   {{variables}}, every proposal is ticked by default, nothing is written until
   the person confirms, and KEEP LITERALS leaves the steps exactly as typed.
   `e`, React hooks and `API` come from the dashboard script, which loads after
   this file. */

function useVariableReview({ onApplied } = {}) {
  const [review, setReview] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  // Proposal for a finished recording. Asked for by id, so the caller only needs
  // to know the recording it just saved.
  const open = async (recordingId) => {
    if (!recordingId) return;
    setBusy(true);
    try {
      const plan = await API.post("/api/recordings/" + recordingId + "/variable-plan", {});
      if (plan.candidates && plan.candidates.length) { setReview(plan); setNote(""); }
      else setNote("Every input already uses a variable — nothing to onboard.");
    } catch (err) {
      setNote("Variable review unavailable: " + err.message);
    } finally { setBusy(false); }
  };

  const apply = async () => {
    if (!review) return;
    const chosen = (review.candidates || []).filter(item => item.selected !== false);
    if (!chosen.length) { setReview(null); setNote("Values kept as typed."); return; }
    setBusy(true);
    try {
      const result = await API.post("/api/recordings/" + review.recording.id + "/variables", {
        assignments: chosen.map(item => ({ name: item.name, value: item.value, is_secret: !!item.is_secret,
          category: item.category, create: !!item.create }))
      });
      const counts = result.counts || {};
      setNote(`✅ ${counts.variables_created || 0} variable(s) created, ${counts.steps_changed || 0} step(s) now use {{variables}}` +
        (counts.variables_reused ? `, ${counts.variables_reused} linked to existing variables` : "") + ".");
      const recordingId = review.recording.id;
      setReview(null);
      if (onApplied) onApplied(recordingId);
    } catch (err) {
      setNote("Could not apply variables: " + err.message);
    } finally { setBusy(false); }
  };

  const edit = (item, change) => setReview(plan => plan && ({
    ...plan,
    candidates: (plan.candidates || []).map(entry => entry.value === item.value ? { ...entry, ...change } : entry)
  }));

  const keep = () => { setReview(null); setNote("Values kept as typed."); };

  return { review, setReview, note, setNote, busy, open, apply, edit, keep };
}

// The amber panel that asks the question. Rendered by whichever recorder finished.
function VariableReviewPanel({ review, busy, onEdit, onApply, onKeep }) {
  if (!review) return null;
  return e("div", { className: "bg-amber-950/30 border border-amber-800/50 p-4 rounded-2xl space-y-3" },
    e("p", { className: "text-sm font-bold text-amber-200" }, "Save complete — name the values you typed"),
    e("p", { className: "text-xs text-amber-100/80" }, `${review.summary}. Tick what should become a reusable variable/parameter; the saved steps will carry {{name}} instead of the literal value.`),
    (review.candidates || []).map((item, i) => e("div", { key: i, className: "bg-slate-950/70 p-2 rounded-xl space-y-1" },
      e("label", { className: "flex flex-wrap items-center gap-2 text-xs" },
        e("input", { type: "checkbox", checked: item.selected !== false, onChange: evt => onEdit(item, { selected: evt.target.checked }) }),
        e("span", { className: "text-slate-400" }, `${item.usages_count}×`),
        e("input", { "aria-label": "Variable name", value: item.name, onChange: evt => onEdit(item, { name: evt.target.value }), className: "bg-slate-800 p-1 rounded w-40" }),
        e("span", { className: "text-slate-300 break-all" }, `= ${item.is_secret ? "••••••" : item.value}`),
        e("label", { className: "flex items-center gap-1 text-slate-400" },
          e("input", { type: "checkbox", checked: !!item.is_secret, onChange: evt => onEdit(item, { is_secret: evt.target.checked }) }), "secret"),
        e("select", { "aria-label": "Variable category", value: item.category, onChange: evt => onEdit(item, { category: evt.target.value }), className: "bg-slate-800 rounded" },
          ["General", "Parameter", "Auth"].map(name => e("option", { key: name, value: name }, name))),
        item.reuse && !item.create && e("label", { className: "flex items-center gap-1 text-emerald-300" },
          e("input", { type: "checkbox", checked: !!item.create, onChange: evt => onEdit(item, { create: evt.target.checked }) }), "create separate"),
        item.reuse && e("span", { className: "text-emerald-300" }, "already a variable")),
      (item.preview || []).map((row, j) => e("p", { key: j, className: "text-[10px] text-slate-500 break-all" }, `#${row.order} ${row.before} → ${row.after}`)))),
    e("div", { className: "flex gap-2" },
      e("button", { onClick: onApply, disabled: busy, className: "bg-emerald-700 px-4 py-2 rounded text-xs disabled:opacity-50" }, "APPLY TO RECORDING"),
      e("button", { onClick: onKeep, disabled: busy, className: "bg-slate-700 px-4 py-2 rounded text-xs" }, "KEEP LITERALS")));
}
