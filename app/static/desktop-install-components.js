/* Install a custom desktop application before recording it.

   The Windows desktop tab records an application that is already on the target
   machine. This panel is the step in front of that: name the app, point at its
   installer, install it (through the configured Windows agent) and then select it
   for recording. Without an agent the install is simulated and says so, exactly
   like the rest of the desktop runner. Loaded before index.html's inline script. */

const DESKTOP_INSTALLER_HINTS = {
  msi: "C:\\Installers\\MyAppSetup.msi",
  exe: "C:\\Installers\\MyAppSetup.exe",
  msix: "C:\\Installers\\MyApp.msix",
  winget: "Contoso.MyApp",
  choco: "myapp",
  portable: "C:\\Installers\\MyApp.zip",
};

const DESKTOP_INSTALLER_KINDS = ["msi", "exe", "msix", "winget", "choco", "portable"];
const DESKTOP_APP_KINDS = ["custom", "access_vba", "foxpro", "cobol_terminal", "as400_terminal",
  "oracle_desktop", "winforms_legacy", "dotnet", "cpp", "winforms"];
const DESKTOP_INSTALL_FORM = {
  name: "", installer_kind: "msi", installer: "", install_args: "",
  expected_process: "", expected_window_title: "", framework: "dotnet", kind: "custom",
  version: "", windows_version: "windows_11",
};

function DesktopInstallRow({ row, working, showLog, onToggleLog, onVerify, onSelect, onRemove }) {
  const installed = row.status === "installed";
  const badgeClass = installed ? "fast-badge whitespace-nowrap" : "text-[9px] font-bold text-red-300";
  const badgeText = installed ? (row.mode === "simulator" ? "SIMULATED" : "INSTALLED") : "FAILED";
  const meta = `${(row.installer_kind || "installer").toUpperCase()} · ${row.mode} · ` +
    `${row.installed_at || "time unavailable"}${row.verified ? " · verified" : ""}`;
  const lines = row.install_log || [];
  return e("article", {
      className: "rounded-xl border p-3 space-y-2 " +
        (installed ? "border-emerald-900/60 bg-emerald-950/20" : "border-red-900/60 bg-red-950/20"),
    },
    e("div", { className: "flex flex-wrap justify-between items-start gap-2" },
      e("div", { className: "min-w-0" },
        e("div", { className: "text-xs font-bold text-slate-100" },
          row.name + (row.version ? " · " + row.version : "")),
        e("div", { className: "text-[10px] text-slate-400 font-mono break-all" }, row.installer),
        e("div", { className: "text-[10px] text-slate-500 mt-1" }, meta)),
      e("div", { className: "flex flex-wrap gap-2 shrink-0" },
        e("span", { className: badgeClass }, badgeText),
        e("button", { type: "button", className: "key-btn", disabled: working || !installed,
          onClick: () => onVerify(row) }, "VERIFY IT OPENS"),
        e("button", { type: "button", className: "key-btn", disabled: working,
          onClick: () => onSelect(row) }, "SELECT FOR RECORDING"),
        e("button", { type: "button", className: "key-btn", disabled: working,
          onClick: () => onRemove(row) }, "REMOVE"))),
    !installed && row.error && e("p", { className: "text-[10px] text-red-300" }, row.error),
    e("div", { className: "text-[10px] text-slate-400 font-mono break-all" }, "Command · " + row.command),
    lines.length > 0 && e("div", {},
      e("button", { type: "button", className: "text-[9px] font-bold text-indigo-300", onClick: onToggleLog },
        showLog ? "HIDE INSTALL LOG ▲" : "SHOW INSTALL LOG ▼"),
      showLog && e("pre", {
          className: "mt-1 text-[10px] whitespace-pre-wrap bg-slate-900 border border-slate-800 rounded-lg p-2 text-slate-300",
        }, lines.join("\n"))));
}

function DesktopInstallPanel({ mode, busy, disabled, onInstalled }) {
  const [state, setState] = useState({ installs: [], mode: mode || "simulator", installer_kinds: [], limit: 20 });
  const [draft, setDraft] = useState(DESKTOP_INSTALL_FORM);
  const [busyLocal, setBusyLocal] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [openLog, setOpenLog] = useState(null);
  const working = busyLocal || !!busy;

  useEffect(() => {
    let stop = false;
    API.get("/api/desktop/installs")
      .then(result => { if (!stop) setState(result); })
      .catch(err => { if (!stop) setError("Installed applications could not be loaded: " + err.message); });
    return () => { stop = true; };
  }, []);

  const set = (key) => (event) => setDraft(prev => ({ ...prev, [key]: event.target.value }));

  const run = async (action) => {
    setBusyLocal(true); setError(""); setNote("");
    try {
      await action();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyLocal(false);
    }
  };

  const install = (event) => {
    event.preventDefault();
    if (!draft.name.trim()) { setError("Enter the application name so it can be registered."); return; }
    if (!draft.installer.trim()) { setError("Enter the installer path, https URL or package id."); return; }
    return run(async () => {
      const result = await API.post("/api/desktop/installs", draft);
      const receipt = result.install || {};
      setState(await API.get("/api/desktop/installs"));
      if (receipt.status === "installed") {
        const how = receipt.mode === "simulator" ? "simulated" : "completed";
        const version = receipt.version ? " · version " + receipt.version : "";
        setNote(`${receipt.name} · ${(receipt.installer_kind || "").toUpperCase()} install ${how}${version}. ` +
          "It is selected for recording — verify it opens, then start.");
        if (onInstalled) onInstalled(result.app);
        setDraft({ ...DESKTOP_INSTALL_FORM, installer_kind: draft.installer_kind,
          framework: draft.framework, kind: draft.kind, windows_version: draft.windows_version });
      } else {
        setError("The install was not successful: " + (receipt.error || "the target reported a failure"));
      }
    });
  };

  const verify = (row) => run(async () => {
    const result = await API.post(`/api/desktop/installs/${row.app_id}/verify`, {});
    setState(await API.get("/api/desktop/installs"));
    setNote(result.verified
      ? `${row.name} opened and closed cleanly (${result.controls} control(s) addressable). Ready to record.`
      : `${row.name} did not open. Check the installer and the expected window title.`);
  });

  const select = (row) => {
    if (onInstalled) onInstalled({ id: row.app_id, name: row.expected_window_title });
    setNote(`${row.name} is selected in the application list above.`);
  };

  const remove = (row) => run(async () => {
    await API.del(`/api/desktop/installs/${row.app_id}`);
    setState(await API.get("/api/desktop/installs"));
    setNote(`${row.name} was removed from the install list.`);
  });

  const simulated = state.mode === "simulator";
  const kinds = (state.installer_kinds && state.installer_kinds.length) ? state.installer_kinds : DESKTOP_INSTALLER_KINDS;
  const field = "block w-full mt-1 bg-slate-800 rounded-lg px-3 py-2 text-xs border border-slate-700";

  return e("details", { className: "rounded-2xl border border-slate-800 bg-slate-950 p-4 space-y-3", open: true },
    e("summary", { className: "text-xs font-bold text-indigo-300 cursor-pointer flex flex-wrap items-center gap-2" },
      "⬇ INSTALL A DESKTOP APPLICATION BEFORE RECORDING",
      e("span", { className: "text-[9px] font-mono text-slate-500" },
        simulated ? "SIMULATOR · NOTHING IS INSTALLED ON A REAL MACHINE" : "WINDOWS AGENT CONNECTED"),
      e("span", { className: "text-[9px] text-slate-500" },
        `${state.installs.length} of ${state.limit} install records`)),
    e("p", { className: "text-[11px] text-slate-400" },
      "Install the build you want to test, verify that it opens, then record against it in the same session. ",
      simulated
        ? "No Windows agent is configured on this server (TF_WINDOWS_AGENT_URL and TF_WINDOWS_AGENT_TOKEN), so the install is validated and registered against the deterministic simulator rather than run on a machine. Connect an agent to install for real."
        : "The install command is sent to the authenticated Windows agent over HTTPS. This server never runs an installer."),
    e("form", { className: "grid grid-cols-1 md:grid-cols-2 gap-2", onSubmit: install },
      e("label", { className: "text-[10px] text-slate-400" }, "Application name",
        e("input", { value: draft.name, onChange: set("name"), disabled: working || disabled, required: true,
          placeholder: "Northwind Orders 4.2", className: field })),
      e("label", { className: "text-[10px] text-slate-400" }, "Installer type",
        e("select", { value: draft.installer_kind, onChange: set("installer_kind"),
          disabled: working || disabled, className: field },
          kinds.map(kind => e("option", { key: kind, value: kind }, kind.toUpperCase())))),
      e("label", { className: "text-[10px] text-slate-400 md:col-span-2" },
        ["winget", "choco"].includes(draft.installer_kind)
          ? "Package id"
          : "Installer on the Windows target (absolute path or https URL)",
        e("input", { value: draft.installer, onChange: set("installer"), disabled: working || disabled,
          required: true, placeholder: DESKTOP_INSTALLER_HINTS[draft.installer_kind],
          className: field + " font-mono" })),
      e("label", { className: "text-[10px] text-slate-400" }, "Extra install arguments (optional)",
        e("input", { value: draft.install_args, onChange: set("install_args"), disabled: working || disabled,
          placeholder: "/quiet /norestart", className: field + " font-mono" })),
      e("label", { className: "text-[10px] text-slate-400" }, "Version (optional)",
        e("input", { value: draft.version, onChange: set("version"), disabled: working || disabled,
          placeholder: "4.2.1", className: field })),
      e("label", { className: "text-[10px] text-slate-400" }, "Expected process name",
        e("input", { value: draft.expected_process, onChange: set("expected_process"),
          disabled: working || disabled, placeholder: "Orders.exe", className: field + " font-mono" })),
      e("label", { className: "text-[10px] text-slate-400" }, "Expected window title",
        e("input", { value: draft.expected_window_title, onChange: set("expected_window_title"),
          disabled: working || disabled, placeholder: "Northwind Orders", className: field })),
      e("label", { className: "text-[10px] text-slate-400" }, "Framework",
        e("select", { value: draft.framework, onChange: set("framework"), disabled: working || disabled,
          className: field },
          ["dotnet", "cpp", "winforms"].map(item => e("option", { key: item, value: item }, item)))),
      e("label", { className: "text-[10px] text-slate-400" }, "Application kind",
        e("select", { value: draft.kind, onChange: set("kind"), disabled: working || disabled, className: field },
          DESKTOP_APP_KINDS.map(item => e("option", { key: item, value: item }, item)))),
      e("label", { className: "text-[10px] text-slate-400" }, "Windows version",
        e("select", { value: draft.windows_version, onChange: set("windows_version"),
          disabled: working || disabled, className: field },
          e("option", { value: "windows_10" }, "Windows 10"),
          e("option", { value: "windows_11" }, "Windows 11"))),
      e("button", { type: "submit", disabled: working || disabled,
        className: "md:col-span-2 bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 rounded-lg px-3 py-2 text-xs font-bold" },
        busyLocal ? "INSTALLING…" : "⬇ INSTALL & REGISTER APPLICATION")),
    state.installs.length === 0
      ? e("p", { className: "text-[10px] text-slate-500" },
          "No desktop application has been installed from this dashboard yet.")
      : e("div", { className: "space-y-2" },
          state.installs.map(row => e(DesktopInstallRow, {
            key: row.app_id, row, working,
            showLog: openLog === row.app_id,
            onToggleLog: () => setOpenLog(openLog === row.app_id ? null : row.app_id),
            onVerify: verify, onSelect: select, onRemove: remove,
          }))),
    note && e("p", { role: "status",
      className: "text-[11px] text-emerald-200 bg-emerald-950/30 border border-emerald-900/40 rounded-xl p-3" }, note),
    error && e("p", { role: "alert",
      className: "text-[11px] text-red-300 bg-red-950/30 border border-red-900/40 rounded-xl p-3" }, error));
}

window.DesktopInstallPanel = DesktopInstallPanel;
