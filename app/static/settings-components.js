/* Master setup: the Settings tab. Region, time zone, date/time format, language
   and application defaults, stored once on the server for the whole instance.

   Picking a region only fills in the defaults; every field can then be changed on
   its own. The preview is rendered from the form values, so it shows what will
   happen before anything is saved. Helpers come from tf-settings.js; `e`, React
   hooks and `API` come from the dashboard script, which loads after this file. */

const SETTINGS_FIELDS = ["region", "timezone", "date_format", "time_format", "language",
  "default_windows_version", "screen_zoom"];

function settingsChanged(a, b) {
  return SETTINGS_FIELDS.some(key => a[key] !== b[key]);
}

function SettingsTab() {
  const saved = React.useSyncExternalStore(tfSubscribeSettings, tfSettings, tfSettings);
  const [catalog, setCatalog] = useState(() => tfSettingsCatalog());
  const [draft, setDraft] = useState(() => ({ ...tfSettings() }));
  const [zoneFilter, setZoneFilter] = useState("");
  const [now, setNow] = useState(() => new Date());
  const [loading, setLoading] = useState(!tfSettingsCatalog());
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  // The preview ticks once a second, so the clock in it is the real one.
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let gone = false;
    API.get("/api/settings").then(data => {
      if (gone) return;
      tfSetSettings(data.settings, data.options);
      setCatalog(data.options);
      setDraft({ ...data.settings });
    }).catch(err => { if (!gone) setError(tfT("Could not load the master settings: ") + err.message); })
      .finally(() => { if (!gone) setLoading(false); });
    return () => { gone = true; };
  }, []);

  const dirty = settingsChanged(draft, saved);
  const set = (key, value) => { setNote(""); setDraft(prev => ({ ...prev, [key]: value })); };

  // Choosing a region fills the four format fields in one go. The user can still
  // change any of them, so the region is a starting point, not a lock.
  const chooseRegion = (code) => {
    const region = (catalog && catalog.regions || []).find(item => item.code === code);
    if (!region) { set("region", code); return; }
    setNote(""); setError("");
    setDraft(prev => ({ ...prev, region: code, timezone: region.timezone, date_format: region.date_format,
      time_format: region.time_format, language: region.language }));
  };

  const save = async () => {
    setBusy(true); setError(""); setNote("");
    try {
      const data = await API.put("/api/settings", draft);
      tfSetSettings(data.settings, data.options);
      setCatalog(data.options);
      setDraft({ ...data.settings });
      setNote(tfT("Saved. Dates, times and language across the dashboard now follow these settings."));
    } catch (err) {
      setError(err.message);
    } finally { setBusy(false); }
  };

  const undo = () => { setError(""); setNote(""); setDraft({ ...saved }); };

  if (loading && !catalog) {
    return e("div", { className: "space-y-3" },
      e("h2", { className: "text-lg font-bold text-indigo-300" }, tfT("Master setup")),
      e("p", { role: "status", className: "text-xs text-slate-400" }, tfT("Loading settings…")));
  }
  const options = catalog || { regions: [], timezones: [], date_formats: [], time_formats: [],
    languages: [], windows_versions: [], screen_zooms: [] };
  const regionName = (options.regions.find(item => item.code === draft.region) || {}).name || draft.region;
  const zones = options.timezones.filter(name => name.toLowerCase().includes(zoneFilter.trim().toLowerCase()));
  const groups = new Map();
  zones.forEach(name => {
    const group = name.includes("/") ? name.split("/")[0] : "UTC";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(name);
  });
  const field = "w-full mt-1 bg-slate-800 rounded-lg px-3 py-2 text-sm border border-slate-700";
  const label = "block text-xs text-slate-300";
  const preview = { ...draft };
  const previewDate = tfFormatDate(now, preview);
  const previewTime = tfFormatTime(now, preview);

  return e("div", { className: "space-y-4 max-w-4xl" },
    e("header", { className: "space-y-1" },
      e("h2", { className: "text-lg font-bold text-indigo-300" }, tfT("Master setup")),
      e("p", { className: "text-xs text-slate-400" }, tfT("One place for the region, time zone, date and time format and language used across the dashboard. Saved on this server, so every user sees the same values."))),
    e("p", { className: "text-[11px] text-amber-200 bg-amber-950/30 border border-amber-900/40 rounded-xl p-3" },
      tfT("Translated so far: the navigation and this screen. Other text stays in English until it is translated.")),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-950 p-4 space-y-4", "aria-labelledby": "settings-region-heading" },
      e("h3", { id: "settings-region-heading", className: "text-xs font-bold text-indigo-300" }, tfT("Region")),
      e("p", { className: "text-[11px] text-slate-500" }, tfT("Choosing a region fills in the defaults below. Each one can still be changed on its own.")),
      e("label", { className: label, htmlFor: "settings-region" }, tfT("Region"),
        e("select", { id: "settings-region", value: draft.region, disabled: busy, onChange: evt => chooseRegion(evt.target.value), className: field },
          options.regions.map(item => e("option", { key: item.code, value: item.code }, item.name)))),

      e("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-4" },
        e("div", { className: "space-y-2" },
          e("label", { className: label, htmlFor: "settings-zone-filter" }, tfT("Search time zones"),
            e("input", { id: "settings-zone-filter", value: zoneFilter, placeholder: "e.g. Berlin, Karachi, New_York",
              onChange: evt => setZoneFilter(evt.target.value), disabled: busy, className: field })),
          e("label", { className: label, htmlFor: "settings-timezone" }, tfT("Time zone"),
            e("select", { id: "settings-timezone", value: draft.timezone, disabled: busy, onChange: evt => set("timezone", evt.target.value), className: field },
              // The chosen zone is always listed, even when the filter hides it.
              !zones.includes(draft.timezone) && e("option", { key: "current", value: draft.timezone }, draft.timezone),
              [...groups.entries()].map(([group, names]) => e("optgroup", { key: group, label: group },
                names.map(name => e("option", { key: name, value: name }, name.replace(/_/g, " ")))))))),
        e("div", { className: "space-y-2" },
          e("label", { className: label, htmlFor: "settings-date-format" }, tfT("Date format"),
            e("select", { id: "settings-date-format", value: draft.date_format, disabled: busy, onChange: evt => set("date_format", evt.target.value), className: field },
              options.date_formats.map(item => e("option", { key: item.id, value: item.id },
                `${item.id}  ·  ${tfFormatDate(now, { ...draft, date_format: item.id })}`)))),
          e("label", { className: label, htmlFor: "settings-time-format" }, tfT("Time format"),
            e("select", { id: "settings-time-format", value: draft.time_format, disabled: busy, onChange: evt => set("time_format", evt.target.value), className: field },
              options.time_formats.map(item => e("option", { key: item.id, value: item.id },
                `${item.id === "24h" ? tfT("24-hour") : tfT("12-hour")}  ·  ${tfFormatTime(now, { ...draft, time_format: item.id })}`)))),
          e("label", { className: label, htmlFor: "settings-language" }, tfT("Language"),
            e("select", { id: "settings-language", value: draft.language, disabled: busy, onChange: evt => set("language", evt.target.value), className: field },
              options.languages.map(item => e("option", { key: item.id, value: item.id }, item.label)))))),

      e("div", { className: "rounded-xl border border-indigo-900/50 bg-indigo-950/30 p-3", role: "status", "aria-live": "polite" },
        e("div", { className: "text-[10px] font-bold tracking-widest text-indigo-300" }, tfT("Preview")),
        e("p", { className: "text-[11px] text-slate-400" }, tfT("Shown with the current settings, before you save.")),
        e("p", { className: "text-lg font-bold text-slate-100 mt-1", "data-testid": "settings-preview" },
          `${previewDate} ${previewTime}`),
        e("p", { className: "text-[11px] text-slate-400" },
          `${tfZoneName(draft)} · ${regionName}`))),

    e("section", { className: "rounded-2xl border border-slate-800 bg-slate-950 p-4 space-y-3", "aria-labelledby": "settings-app-heading" },
      e("h3", { id: "settings-app-heading", className: "text-xs font-bold text-indigo-300" }, tfT("Application defaults")),
      e("label", { className: label, htmlFor: "settings-windows-version" },
        tfT("Windows version preselected for a new Windows desktop recording"),
        e("select", { id: "settings-windows-version", value: draft.default_windows_version, disabled: busy,
          onChange: evt => set("default_windows_version", evt.target.value), className: field },
          options.windows_versions.map(item => e("option", { key: item.id, value: item.id }, item.label)))),
      e("label", { className: label, htmlFor: "settings-screen-zoom" },
        tfT("Default screen zoom for the live recording and run screens"),
        e("select", { id: "settings-screen-zoom", value: draft.screen_zoom, disabled: busy,
          onChange: evt => set("screen_zoom", evt.target.value), className: field },
          options.screen_zooms.map(item => e("option", { key: item.id, value: item.id }, tfT(item.label)))))),

    e("div", { className: "flex flex-wrap items-center gap-2" },
      e("button", { type: "button", onClick: save, disabled: busy || !dirty,
        className: "bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-5 py-3 rounded-xl font-bold text-xs" },
        busy ? tfT("Saving…") : tfT("SAVE SETTINGS")),
      e("button", { type: "button", onClick: undo, disabled: busy || !dirty,
        className: "bg-slate-800 border border-slate-700 disabled:opacity-50 px-4 py-3 rounded-xl font-bold text-xs" },
        tfT("UNDO CHANGES")),
      dirty && e("span", { className: "text-[11px] text-amber-300" }, tfT("Unsaved changes"))),
    note && e("p", { role: "status", className: "text-xs text-emerald-200 bg-emerald-950/30 border border-emerald-900/40 rounded-xl p-3" }, note),
    error && e("p", { role: "alert", className: "text-xs text-red-300 bg-red-950/30 border border-red-900/40 rounded-xl p-3" }, error));
}
