/* Future-release planning view. Targets are implemented simulator-first with
   a live Support Matrix; nothing here claims vendor certification. The
   logo-style monograms are local placeholders so the tab remains
   self-contained and does not fetch vendor assets. */
const TESTFORGE_ROADMAP_TARGETS = [
  {
    group: 'ERP / CRM web applications', mark: 'ORCL', name: 'Oracle web applications',
    color: '#f80000', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The oracle_web pack models Fusion/EBS patterns — nested iframes, LOV dialogs and line-item tables — on the local /erp-crm-demo.html fixture with TF_SECRET_* sign-in references. A real Oracle tenant still needs authorized access; there is no vendor certification.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'SAP', name: 'SAP · Fiori / web UI',
    color: '#0875b9', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The sap_fiori pack uses data-sap-ui-id locators, an ARIA grid with scoped table lookup and a busy-indicator flow on the local fixture. SAP GUI and certified SAP integrations remain outside support.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'JD', name: 'JD Edwards',
    color: '#315b7d', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The jd_edwards pack covers fast-path (P0411) navigation, grid rows and batch-status assertions on the local EnterpriseOne-style fixture.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'D365', name: 'Microsoft Dynamics 365 F&O',
    color: '#5c2d91', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The ms_dynamics_fo pack models Finance & Operations sales-order forms with data-testid locators on the local fixture; credentials are environment references only.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'BC', name: 'Microsoft Dynamics 365 Business Central',
    color: '#243a5e', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The ms_dynamics_bc pack models Business Central list/form posting flows on the local fixture, separate from the F&O pack because the products differ.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'odoo', name: 'Odoo',
    color: '#714b67', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The odoo_erp pack exercises OWL-style forms, a wait-aware many2one suggestion pick and Draft → Confirmed statusbar transitions on the local fixture. There is no Odoo connector.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'SF', name: 'Salesforce',
    color: '#00a1e0', ink: '#ffffff', stage: 'Pack implemented · deterministic fixture',
    detail: 'The salesforce_crm pack uses Lightning-style locators, toast (role=alert) assertions and record-stage markup on the local fixture. Real sandboxes still need authorized access.'
  },
  {
    group: 'Windows desktop applications', mark: '.NET', name: '.NET / C# desktop apps',
    color: '#512bd4', ink: '#ffffff', stage: 'Adapter implemented · simulator + Windows agent bridge',
    detail: 'Plans with runner "windows_desktop" and framework "dotnet" run sequentially against a simulated UIA tree (Calculator and custom apps seeded), or a real Windows agent when TF_WINDOWS_AGENT_URL/TF_WINDOWS_AGENT_TOKEN are set.'
  },
  {
    group: 'Windows desktop applications', mark: 'C++', name: 'C++ desktop apps',
    color: '#00599c', ink: '#ffffff', stage: 'Adapter implemented · simulator + Windows agent bridge',
    detail: 'Native Win32/MFC targets use the same structured UIA locators and actions (framework "cpp") in the simulator or through the authenticated Windows agent bridge.'
  },
  {
    group: 'Windows desktop applications', mark: 'WF', name: 'WinForms · legacy and current',
    color: '#475569', ink: '#ffffff', stage: 'Adapter implemented · simulator + Windows agent bridge',
    detail: 'WinForms legacy/current plus custom Access/VBA, FoxPro, COBOL-terminal, AS/400-terminal and Oracle desktop app profiles can be registered and simulated; live control needs the Windows agent.'
  },
  {
    group: 'Mobile and rugged devices', mark: 'AND', name: 'Android apps',
    color: '#3ddc84', ink: '#052e16', stage: 'Adapter implemented · simulator + Appium hub bridge',
    detail: 'Plans with runner "mobile_device" and platform "android" run against the deterministic device simulator, or an authorized Appium lab via TF_MOBILE_HUB_URL/TF_MOBILE_HUB_TOKEN.'
  },
  {
    group: 'Mobile and rugged devices', mark: 'iOS', name: 'iPhone / iPad apps',
    color: '#111827', ink: '#ffffff', stage: 'Adapter implemented · simulator + Appium hub bridge',
    detail: 'iOS profiles use XCUITest-style locators (ios_predicate, ios_class_chain) in the simulator or through the Appium hub; Apple hardware is never simulated as real.'
  },
  {
    group: 'Mobile and rugged devices', mark: 'Z', name: 'Zebra rugged devices',
    color: '#111827', ink: '#ffffff', stage: 'Adapter implemented · simulator + DataWedge bridge',
    detail: 'Zebra profiles add hardware keys (F1–F12, Scan) and DataWedge barcode injection for EAN-13, CODE-128, QR and GS1-128, with the /mobile-zebra-demo.html fixture mirroring the simulator.'
  }
];

const TESTFORGE_ROADMAP_GROUPS = [
  'ERP / CRM web applications',
  'Windows desktop applications',
  'Mobile and rugged devices'
];

const TESTFORGE_ROADMAP_PROMPT_HEADER = [
  'You are implementing the complete end-to-end Future Release Planning roadmap in the TestForge repository.',
  '',
  '## Architecture & Non-Negotiable Guardrails',
  '- Keep the existing Python/FastAPI backend (`app/`), repository-backed `library/`, SQLite/PostgreSQL models (`app/db.py`), and local React + precompiled Tailwind UI (`app/static/`).',
  '- Preserve the free-tier single-worker/sequential execution budget (`app/guardrails.py`), test-before-publishing draft gate (`app/import_drafts.py`), and encrypted secret store (`app/secrets_store.py`).',
  '- Never store plaintext credentials, cookies or tokens in `library/` or generated exports; use `TF_SECRET_*` and `TF_AUTH_STATE_*` environment references.',
  '- Every new capability must work across authoring/import, live or simulated preview, sequential execution, portable Python/JS/TS exports, HTML/PDF reports, and automated tests.'
].join('\n');

const TESTFORGE_ROADMAP_PHASE_PROMPTS = {
  phase1: [
    '## Phase 1 · ERP / CRM Web Application Packs (Oracle, SAP Fiori, JD Edwards, Microsoft Dynamics, Odoo, Salesforce)',
    '1. Extend `app/domain_packs.py` and `/api/imports/packs` with the enterprise ERP/CRM packs: `oracle_web` (Oracle Fusion / E-Business Suite), `sap_fiori` (SAP Fiori / UI5 web UI), `jd_edwards` (JD Edwards EnterpriseOne web UI), `ms_dynamics_fo` (Microsoft Dynamics 365 Finance & Operations), `ms_dynamics_bc` (Microsoft Dynamics 365 Business Central), `odoo_erp` (Odoo web workflows), and `salesforce_crm` (Salesforce Lightning web workflows). Keep the existing five synthetic packs unchanged.',
    '2. Add a local deterministic multi-tenant ERP/CRM simulation fixture (`app/static/erp-crm-demo.html` or extended `/domain-demo.html`) modeling realistic DOM patterns for each target:',
    '   - Oracle Fusion/EBS: dynamic region containers, nested iframes, LOV lookup dialogs, and line-item tables.',
    '   - SAP Fiori / UI5: `data-sap-ui` attributes, ARIA grid/tile roles, busy-indicator waits, and object-header assertions.',
    '   - JD Edwards: fast-path navigation header, grid row selection, and batch-status verification.',
    '   - Odoo: OWL form views, many2one autocomplete dropdowns, statusbar transitions (`Draft` → `Confirmed`), and kanban cards.',
    '   - Salesforce: Lightning-style `data-aura-rendered-by` / `lightning-input` role/label locators, toast assertions, and record-stage progression.',
    '3. Include in every ERP/CRM pack: `setup` (navigation + `auth_profile` / `TF_SECRET_*` references), reusable `flows`, positive and negative `dataset` rows, expected outcome assertions (`assert_text`, `assert_value`, `assert_visible`), and deterministic `teardown` steps that reset state.',
    '4. Update `app/step_runtime.py` and `app/recorder.py` selector extraction (`_ELEMENT_JS`) to prioritize stable enterprise attributes (`data-testid`, `data-sap-ui-id`, `data-field`, `aria-label`, `role`, `name`, `placeholder`) over brittle generated DOM IDs.'
  ].join('\n'),

  phase2: [
    '## Phase 2 · Windows Desktop Application Runner (.NET / C#, C++, WinForms Legacy & Current)',
    '1. Create `app/desktop_runner.py` implementing a bounded sequential Windows UI Automation adapter protocol supporting `.NET / C#` (WPF / WinUI), native `C++` (Win32 / MFC), and `WinForms` (legacy .NET Framework & modern .NET).',
    '2. Extend the canonical step schema (`app/step_runtime.py` & `app/plan_runtime.py`) with validated desktop target profiles (`runner: "windows_desktop"`, `framework: "dotnet" | "cpp" | "winforms"`) and desktop actions:',
    '   - `window_attach` (match by title/process/class), `ui_click`, `ui_double_click`, `ui_right_click`, `ui_type`, `ui_select`, `ui_check`, `ui_menu_invoke`, `ui_grid_read`, `assert_control_text`, `assert_control_state`, and `wait_window_ready`.',
    '   - Support structured UI Automation locators (`automation_id`, `control_type`, `name`, `class_name`, `framework_id`, `window_title`).',
    '3. Provide two execution modes in `app/desktop_runner.py`:',
    '   - **Remote Windows Agent Mode**: dispatches commands over an authenticated HTTPS/WebSocket bridge (`TF_WINDOWS_AGENT_URL`, `TF_WINDOWS_AGENT_TOKEN`) to a lightweight Windows UIA/FlaUI/WinAppDriver worker and collects per-step PNG/JPEG evidence.',
    '   - **Deterministic Local Simulation Mode**: validates desktop plans against an in-process simulated UI Automation control tree and renders synthetic window screenshots so CI, draft testing (`POST /api/imports/test`), and Linux deployments can verify desktop test plans without a live Windows machine.',
    '4. Update `app/export_formats.py` and `app/export_templates.py` so desktop recordings/plans export runnable Python (`pywinauto` / UIA bridge) and C#/JSON runner bundles alongside standard artifacts.'
  ].join('\n'),

  phase3: [
    '## Phase 3 · Mobile & Rugged Device Runner (Android, iPhone / iPad iOS, Zebra Rugged Handhelds & Scanners)',
    '1. Create `app/mobile_runner.py` implementing a sequential mobile and rugged-device automation adapter for native `Android apps` (UiAutomator2), `iPhone / iPad apps` (XCUITest), and `Zebra rugged devices` (Android Enterprise + Zebra DataWedge hardware scanner/key workflows).',
    '2. Extend the step and matrix schema (`app/step_runtime.py` & `app/plan_runtime.py`) with mobile/rugged device profiles (`runner: "mobile_device"`, `platform: "android" | "ios" | "zebra"`, `device_model`, `os_version`, `orientation`) and actions:',
    '   - `app_launch`, `tap_element`, `long_press`, `swipe`, `mobile_type`, `device_key` (hardware Enter/Tab/F1–F12/Scan trigger), `rotate_orientation`, `toggle_network_mode`, `scan_barcode` (DataWedge intent injection with `symbology`: `EAN-13`, `CODE-128`, `QR`, `GS1-128`, and `barcode_data`), `assert_mobile_text`, and `assert_mobile_visible`.',
    '   - Support mobile locators (`accessibility_id`, `resource_id`, `ios_predicate`, `ios_class_chain`, `xpath`, `text`).',
    '3. Provide two execution modes in `app/mobile_runner.py`:',
    '   - **Connected Device / Appium Lab Mode**: connects to an authorized Appium / WebDriver endpoint (`TF_MOBILE_HUB_URL`, `TF_MOBILE_HUB_TOKEN`) and dispatches native mobile & Zebra DataWedge broadcast intents.',
    '   - **Deterministic Local Mobile/Zebra Simulator**: provides a built-in interactive handheld/scanner simulation fixture (`/mobile-zebra-demo.html` + in-process state machine) so Android, iOS, and Zebra barcode-scanning warehouse/retail flows can be authored, draft-tested, replayed, and reported on any host.',
    '4. Export portable Appium Python/JS test runners and Zebra DataWedge profile manifests in `write_exports()`.'
  ].join('\n'),

  phase4: [
    '## Phase 4 · Pilot, Security Review, Support Matrix & Remaining Playwright Roadmap (R01–R15)',
    '1. Complete remaining R01–R15 items across web, desktop, and mobile runners:',
    '   - Rich expected vs. actual assertion diagnostics in step logs, HTML reports (`app/report_routes.py`), and PDF reports (`app/pdf_reports.py`).',
    '   - Scoped web-table row/cell lookup helpers and wait-aware autocomplete/suggestion selection in `app/step_runtime.py` and `app/export_runtime.js`.',
    '   - CSV/JSON dataset upload & row-override management in the Execution Plan / Import UI, preserving strict secret redaction (`TF_SECRET_*`) and per-attempt context isolation.',
    '2. Add a live **Support Matrix & Readiness Gate** endpoint (`GET /api/roadmap/support-matrix`) and UI panel in `FuturePlanningTab` (`app/static/future-components.js`):',
    '   - Report the real verification state of every target (`Oracle`, `SAP Fiori`, `JD Edwards`, `Odoo`, `Salesforce`, `.NET/C#`, `C++`, `WinForms`, `Android`, `iOS`, `Zebra`): whether its pack/adapter is registered, whether a live agent/hub is configured, and whether its automated pilot suite has passed.',
    '   - Only badge a target as `VERIFIED IN THIS ENVIRONMENT` when its end-to-end pilot test receipt is present and passing; otherwise clearly distinguish `SIMULATOR READY` vs. `EXTERNAL AGENT REQUIRED`.',
    '3. End-to-End Validation & Acceptance Gate:',
    '   - Add comprehensive unit and integration tests covering all 11 roadmap targets (ERP/CRM packs, Windows desktop adapter + simulator, Mobile/Zebra barcode adapter + simulator, export parity, and security redaction).',
    '   - Validate JavaScript syntax (`node --check` on `app/static/*.js`), rebuild local Tailwind CSS (`tools/ui/build.cjs`), and run `python3 -m unittest` and `python3 -m tests.harness` with zero failures.'
  ].join('\n')
};

const TESTFORGE_ROADMAP_PROMPT_SCOPES = [
  { id: 'all', label: 'Complete End-to-End Scope (All Phases 1–4)' },
  { id: 'phase1', label: 'Phase 1 · ERP / CRM Web Packs' },
  { id: 'phase2', label: 'Phase 2 · Windows Desktop Runner' },
  { id: 'phase3', label: 'Phase 3 · Mobile & Zebra Rugged' },
  { id: 'phase4', label: 'Phase 4 · Pilot, Matrix & R01–R15' }
];

function buildRoadmapAiPrompt(scopeId) {
  const sections = scopeId === 'all'
    ? [
        TESTFORGE_ROADMAP_PHASE_PROMPTS.phase1,
        TESTFORGE_ROADMAP_PHASE_PROMPTS.phase2,
        TESTFORGE_ROADMAP_PHASE_PROMPTS.phase3,
        TESTFORGE_ROADMAP_PHASE_PROMPTS.phase4
      ]
    : [TESTFORGE_ROADMAP_PHASE_PROMPTS[scopeId] || TESTFORGE_ROADMAP_PHASE_PROMPTS.phase1];
  return [TESTFORGE_ROADMAP_PROMPT_HEADER, ...sections].join('\n\n');
}

const TESTFORGE_COMPLETE_ROADMAP_PROMPT = buildRoadmapAiPrompt('all');
window.TESTFORGE_COMPLETE_ROADMAP_PROMPT = TESTFORGE_COMPLETE_ROADMAP_PROMPT;

function RoadmapStatus({ stage, current }) {
  return e('span', {
    className: 'inline-flex items-center rounded-full border px-2 py-1 text-[9px] font-bold leading-tight ' +
      (current ? 'border-emerald-800/60 bg-emerald-950/40 text-emerald-200'
               : 'border-amber-800/60 bg-amber-950/30 text-amber-200')
  }, stage);
}

function RoadmapTargetCard({ target }) {
  return e('article', { className: 'flex gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-3' },
    e('div', {
      className: 'roadmap-mark flex h-12 w-12 shrink-0 items-center justify-center rounded-xl text-[10px] font-bold tracking-tight',
      style: { backgroundColor: target.color, color: target.ink }
    }, target.mark),
    e('div', { className: 'min-w-0 space-y-1.5' },
      e('div', { className: 'flex flex-wrap items-center justify-between gap-2' },
        e('h4', { className: 'text-sm font-bold text-slate-100' }, target.name),
        e(RoadmapStatus, {
          stage: target.stage,
          current: target.stage.startsWith('Pack implemented') || target.stage.startsWith('Adapter implemented')
        })),
      e('p', { className: 'text-xs leading-relaxed text-slate-400' }, target.detail)));
}

function RoadmapAiPromptPanel() {
  const [scope, setScope] = useState('all');
  const [copyStatus, setCopyStatus] = useState('');
  const promptRef = useRef(null);
  const promptText = buildRoadmapAiPrompt(scope);

  const copyPrompt = async () => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(promptText);
        setCopyStatus('✓ Copied AI prompt to clipboard — ready to paste!');
        return;
      }
    } catch (_) {}
    try {
      if (promptRef.current) {
        promptRef.current.focus();
        promptRef.current.select();
        document.execCommand('copy');
        setCopyStatus('✓ Copied AI prompt to clipboard — ready to paste!');
        return;
      }
    } catch (_) {}
    if (promptRef.current) {
      promptRef.current.focus();
      promptRef.current.select();
    }
    setCopyStatus('Prompt selected — press Ctrl+C / Cmd+C to copy.');
  };

  const selectAllPrompt = () => {
    if (promptRef.current) {
      promptRef.current.focus();
      promptRef.current.select();
      setCopyStatus('Prompt text selected — press Ctrl+C / Cmd+C to copy.');
    }
  };

  return e('section', { className: 'rounded-2xl border border-indigo-800 bg-slate-900 p-4 space-y-3' },
    e('div', { className: 'flex flex-wrap items-center justify-between gap-2' },
      e('div', { className: 'space-y-1' },
        e('div', { className: 'flex flex-wrap items-center gap-2' },
          e('h2', { className: 'text-sm font-bold text-indigo-300' }, '🤖 AI Prompt · Implement Complete End-to-End Future Roadmap Scope'),
          e('span', { className: 'rounded-full border border-emerald-700 bg-emerald-950/40 px-2 py-0.5 text-[9px] font-bold text-emerald-200' }, 'COPY & PASTE READY')),
        e('p', { className: 'text-xs text-slate-400' },
          'Copy and paste this specification prompt into your AI coding agent to implement the complete end-to-end Future Release Planning scope (ERP/CRM web packs, Windows desktop runner, Mobile & Zebra rugged runner, and R01–R15 validation gate) or select a single phase below.')),
      e('div', { className: 'flex flex-wrap items-center gap-2' },
        e('button', {
          type: 'button',
          onClick: copyPrompt,
          className: 'bg-indigo-600 px-3 py-1.5 rounded-lg font-bold text-xs text-white'
        }, '📋 COPY AI PROMPT'),
        e('button', {
          type: 'button',
          onClick: selectAllPrompt,
          className: 'key-btn'
        }, 'SELECT ALL'))),

    e('div', { className: 'flex flex-wrap gap-1.5' },
      TESTFORGE_ROADMAP_PROMPT_SCOPES.map(item => e('button', {
        key: item.id,
        type: 'button',
        onClick: () => { setScope(item.id); setCopyStatus(''); },
        'aria-pressed': scope === item.id,
        className: 'key-btn ' + (scope === item.id ? 'bg-indigo-600 border-indigo-400 text-white' : '')
      }, item.label))),

    copyStatus && e('p', {
      role: 'status',
      className: 'text-xs font-bold text-emerald-300 bg-emerald-950/40 border border-emerald-800/60 rounded-lg px-3 py-1.5'
    }, copyStatus),

    e('textarea', {
      ref: promptRef,
      readOnly: true,
      rows: 20,
      value: promptText,
      'aria-label': 'AI prompt to implement the future roadmap plan',
      onClick: selectAllPrompt,
      className: 'w-full bg-slate-950 border border-slate-800 rounded-xl p-3 text-xs font-mono text-slate-200 leading-relaxed'
    }),

    e('p', { className: 'text-[10px] text-slate-400' },
      'Tip: Keep "Complete End-to-End Scope (All Phases 1–4)" selected to copy the full end-to-end implementation prompt across all 13 support-matrix targets (the 11 named roadmap targets with Microsoft Dynamics split into F&O and Business Central) and R01–R15 requirements, or switch tabs above to copy one phase at a time.')
  );
}

/* Shared execution-speed slider (25%–300%). The factor is persisted locally,
   mirrored on window.TESTFORGE_SPEED_FACTOR for every run-queue call site, and
   sent to the server as execution_speed; 1.0 is the historical behaviour. */
function tfSpeedFactor() {
  try {
    const value = parseFloat(localStorage.getItem('tf_execution_speed') || '1');
    return Number.isFinite(value) ? Math.min(3, Math.max(0.25, value)) : 1;
  } catch (_) { return 1; }
}
try { window.TESTFORGE_SPEED_FACTOR = tfSpeedFactor(); } catch (_) {}

function ExecutionSpeedControl({ compact } = {}) {
  const [factor, setFactor] = useState(tfSpeedFactor());
  useEffect(() => {
    const sync = () => setFactor(tfSpeedFactor());
    window.addEventListener('tf-speed-change', sync);
    return () => window.removeEventListener('tf-speed-change', sync);
  }, []);
  const update = (raw) => {
    const value = Math.min(3, Math.max(0.25, Number(raw) || 1));
    setFactor(value);
    try { localStorage.setItem('tf_execution_speed', String(value)); } catch (_) {}
    window.TESTFORGE_SPEED_FACTOR = value;
    window.dispatchEvent(new Event('tf-speed-change'));
  };
  const label = factor >= 2 ? 'fast' : factor > 1 ? 'faster' : factor === 1 ? 'normal' : 'slow · watchable';
  return e('label', { className: 'flex items-center gap-2 text-xs text-slate-300', title: 'Execution speed: faster shrinks timeouts, slower adds a watchable pace and typing delay. Applies to runs queued from any tab.' },
    e('span', { className: 'font-bold whitespace-nowrap' }, '⚡ Speed ' + factor.toFixed(2) + '×'),
    e('input', {
      type: 'range', min: 0.25, max: 3, step: 0.25, value: factor,
      'aria-label': 'Test script execution speed',
      onChange: event => update(event.target.value),
      className: compact ? 'w-24 accent-indigo-500' : 'w-36 accent-indigo-500'
    }),
    e('span', { className: 'text-[10px] text-slate-400 whitespace-nowrap' }, Math.round(factor * 100) + '% · ' + label));
}

function SupportMatrixPanel() {
  const [matrix, setMatrix] = useState(null);
  const [apps, setApps] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [appName, setAppName] = useState('');
  const [appKind, setAppKind] = useState('custom');
  const [appNote, setAppNote] = useState('');
  const load = () => API.get('/api/roadmap/support-matrix').then(data => { setMatrix(data); setError(''); }).catch(err => setError(err.message));
  const loadApps = () => API.get('/api/roadmap/desktop-apps').then(data => setApps(data.apps || [])).catch(() => {});
  useEffect(() => { load(); loadApps(); }, []);
  const runPilot = async () => {
    setBusy(true);
    try { setMatrix(await API.post('/api/roadmap/pilot/run', {})); setError(''); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const addApp = async (event) => {
    event.preventDefault();
    if (!appName.trim()) return;
    try {
      await API.post('/api/roadmap/desktop-apps', { profile: { name: appName.trim(), kind: appKind } });
      setAppName(''); setAppNote('Custom desktop app registered for simulation.'); loadApps();
    } catch (err) { setAppNote(err.message); }
  };
  const badgeClass = badge => badge === 'VERIFIED IN THIS ENVIRONMENT'
    ? 'border-emerald-700 bg-emerald-950/50 text-emerald-200'
    : badge === 'SIMULATOR READY' ? 'border-sky-800 bg-sky-950/40 text-sky-200'
    : 'border-amber-800 bg-amber-950/40 text-amber-200';
  return e('section', { className: 'rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-3' },
    e('div', { className: 'flex flex-wrap items-center justify-between gap-2' },
      e('div', null,
        e('h2', { className: 'text-sm font-bold text-indigo-300' }, '✅ Support Matrix & Readiness Gate (live)'),
        e('p', { className: 'text-xs text-slate-400' }, 'Real state from GET /api/roadmap/support-matrix. VERIFIED requires a passing, unexpired pilot receipt; simulator success is never presented as live-device verification.')),
      e('div', { className: 'flex gap-2' },
        e('button', { type: 'button', onClick: load, className: 'key-btn' }, 'REFRESH'),
        e('button', { type: 'button', onClick: runPilot, disabled: busy, className: 'bg-indigo-600 px-3 py-1.5 rounded-lg font-bold text-xs text-white disabled:opacity-50' }, busy ? 'RUNNING PILOTS…' : '▶ RUN PILOT SUITE'))),
    error && e('p', { role: 'alert', className: 'text-xs font-bold text-red-300' }, error),
    matrix && matrix.receipt && e('p', { className: 'text-[11px] text-slate-400' },
      'Pilot receipt: ' + (matrix.receipt.suite || '') + ' · generated ' + (matrix.receipt.generated_at ? tfFormatDateTime(matrix.receipt.generated_at) : '') + ' · expires ' + (matrix.receipt.expires_at ? tfFormatDateTime(matrix.receipt.expires_at) : '')),
    matrix && e('div', { className: 'overflow-x-auto' },
      e('table', { className: 'w-full text-left text-xs' },
        e('thead', null, e('tr', null, ['Target', 'Pack / adapter', 'Simulator', 'Live bridge', 'Pilot', 'Badge', 'Note'].map(h => e('th', { key: h, className: 'border-b border-slate-800 px-2 py-1 text-slate-400' }, h)))),
        e('tbody', null, matrix.targets.map(target => e('tr', { key: target.id },
          e('td', { className: 'border-b border-slate-800 px-2 py-1 font-bold text-slate-100' }, target.name),
          e('td', { className: 'border-b border-slate-800 px-2 py-1 text-slate-300' }, target.pack ? ('pack: ' + target.pack) : (target.runner + (target.framework ? ' · ' + target.framework : '') + (target.platform ? ' · ' + target.platform : ''))),
          e('td', { className: 'border-b border-slate-800 px-2 py-1' }, target.simulator_ready ? 'ready' : '—'),
          e('td', { className: 'border-b border-slate-800 px-2 py-1' }, target.live_configured ? 'configured' : (target.live_env.length ? 'not configured' : 'local fixture')),
          e('td', { className: 'border-b border-slate-800 px-2 py-1' }, target.pilot_status),
          e('td', { className: 'border-b border-slate-800 px-2 py-1' }, e('span', { className: 'rounded-full border px-2 py-0.5 text-[9px] font-bold ' + badgeClass(target.badge) }, target.badge)),
          e('td', { className: 'border-b border-slate-800 px-2 py-1 text-slate-400' }, target.note)))))),
    !matrix && !error && e('p', { className: 'text-xs text-slate-400' }, 'Loading support matrix…'),
    e('div', { className: 'rounded-xl border border-slate-800 bg-slate-950 p-3 space-y-2' },
      e('h3', { className: 'text-xs font-bold text-slate-100' }, 'Testable desktop applications'),
      e('p', { className: 'text-[11px] text-slate-400' }, (apps.length ? apps.map(app => app.name + ' (' + app.framework + (app.builtin ? '' : ', custom') + ')').join(' · ') : 'Loading…')),
      e('form', { onSubmit: addApp, className: 'flex flex-wrap items-end gap-2' },
        e('label', { className: 'text-[11px] text-slate-300' }, 'Add tested desktop app',
          e('input', { value: appName, onChange: event => setAppName(event.target.value), placeholder: 'e.g. Orders (Access/VBA)', className: 'mt-1 block rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-white' })),
        e('label', { className: 'text-[11px] text-slate-300' }, 'Kind',
          e('select', { value: appKind, onChange: event => setAppKind(event.target.value), className: 'mt-1 block rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-white' },
            [['custom', 'Custom app'], ['access_vba', 'MS Access / VBA'], ['foxpro', 'Visual FoxPro'], ['cobol_terminal', 'COBOL terminal'], ['as400_terminal', 'AS/400 terminal'], ['oracle_desktop', 'Oracle desktop'], ['dotnet', '.NET C#'], ['cpp', 'C++'], ['winforms', 'WinForms'], ['winforms_legacy', 'WinForms legacy'], ['calculator', 'Calculator'], ['settings', 'Settings']].map(([value, label]) => e('option', { key: value, value }, label)))),
        e('button', { type: 'submit', className: 'bg-emerald-700 px-3 py-1.5 rounded-lg font-bold text-xs text-white' }, 'ADD APP')),
      appNote && e('p', { className: 'text-[11px] text-emerald-300' }, appNote),
      e('p', { className: 'text-[11px] text-slate-400' }, 'Fixtures: ',
        e('a', { href: '/erp-crm-demo.html', className: 'text-sky-300 underline' }, 'ERP/CRM demo'), ' · ',
        e('a', { href: '/mobile-zebra-demo.html', className: 'text-sky-300 underline' }, 'Mobile/Zebra demo'))));
}

function FuturePlanningTab() {
  return e('div', { className: 'space-y-4' },
    e(GuideSection, { id: "future-intro", label: "Future release planning · overview",
      action: "Read what is available today and what needs an agent or hub to go live." },
    e('section', { className: 'rounded-2xl border border-indigo-800 bg-slate-900 p-5 space-y-3' },
      e('div', { className: 'flex flex-wrap items-center justify-between gap-2' },
        e('span', { className: 'text-[10px] font-bold tracking-wide text-indigo-300' }, '🗺️ FUTURE RELEASE PLANNING'),
        e('span', { className: 'rounded-full bg-slate-800 px-2 py-1 text-[9px] font-bold text-emerald-200' }, 'IMPLEMENTED · SIMULATOR-FIRST, LIVE VIA AGENTS')),
      e('h1', { className: 'text-xl font-bold text-slate-100' }, 'Plan beyond browser testing'),
      e('p', { className: 'text-sm leading-relaxed text-slate-300' },
        'The roadmap targets below are implemented simulator-first: ERP/CRM packs, a Windows desktop adapter and a mobile/Zebra adapter all author, draft-test, execute sequentially, export and report in this build. Live enterprise systems, Windows machines and physical devices are reached only through the authenticated agent/hub bridges — the Support Matrix states plainly which is which.'),
      e(ExecutionSpeedControl, null),
      e('div', { className: 'rounded-xl border border-emerald-900/60 bg-emerald-950/30 p-3 space-y-1' },
        e('div', { className: 'flex flex-wrap items-center gap-2' },
          e('div', { className: 'flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-600 text-xs font-bold text-white' }, 'PW'),
          e('h2', { className: 'text-sm font-bold text-emerald-200' }, 'Supported today · browser UI'),
          e('span', { className: 'rounded-full border border-emerald-700 px-2 py-0.5 text-[9px] font-bold text-emerald-200' }, 'AVAILABLE NOW')),
        e('p', { className: 'text-xs leading-relaxed text-slate-300' },
          'TestForge records and replays accessible web pages using Playwright and Chromium, and now ships dedicated ERP/CRM packs (Oracle, SAP Fiori, JD Edwards, Dynamics 365 F&O and Business Central, Odoo, Salesforce) against a deterministic local fixture. Trying a real tenant through the browser still needs authorized access and is not vendor certification.'),
        e('p', { className: 'text-[10px] text-slate-500' },
          'Windows desktop and native mobile plans execute against deterministic simulators by default, or a real target only when TF_WINDOWS_AGENT_URL/TF_WINDOWS_AGENT_TOKEN or TF_MOBILE_HUB_URL/TF_MOBILE_HUB_TOKEN are configured.'))),

    e(GuideSection, { id: "future-matrix", label: "Support Matrix & Readiness Gate",
      action: "REFRESH reads the live state; RUN PILOT mints a 24-hour receipt that decides the VERIFIED badge." },
    e(SupportMatrixPanel, null)),

    e(GuideSection, { id: "future-targets", label: "Implemented application and tool targets",
      action: "Browse ERP/CRM, Windows desktop and Mobile/Zebra targets — initials are local placeholders." },
    e('section', { className: 'rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-3' },
      e('div', { className: 'space-y-1' },
        e('h2', { className: 'text-sm font-bold text-indigo-300' }, 'Implemented application and tool targets'),
        e('p', { className: 'text-xs text-slate-400' },
          'The colorful initials below are local logo-style placeholders. Vendor names identify implemented pack/adapter targets only; these are not official logo assets, endorsements or certifications.')),
      TESTFORGE_ROADMAP_GROUPS.map(group => e('div', { key: group, className: 'space-y-2' },
        e('h3', { className: 'text-xs font-bold uppercase tracking-wide text-slate-300' }, group),
        e('div', { className: 'grid grid-cols-1 gap-2 md:grid-cols-2' },
          TESTFORGE_ROADMAP_TARGETS.filter(target => target.group === group).map(target =>
            e(RoadmapTargetCard, { key: target.name, target }))))))),

    e(GuideSection, { id: "future-roadmap", label: "A practical extension roadmap",
      action: "Each phase is already implemented simulator-first; live verification needs your authorized environment." },
    e('section', { className: 'rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-3' },
      e('h2', { className: 'text-sm font-bold text-indigo-300' }, 'A practical extension roadmap'),
      e('ol', { className: 'space-y-3' },
        [
          ['1 · ERP / CRM web packs', 'Implemented: seven enterprise packs (Oracle, SAP Fiori, JD Edwards, Dynamics 365 F&O, Dynamics 365 Business Central, Odoo, Salesforce) with TF_SECRET_* sign-in, reusable flows, positive/negative datasets and reset teardowns against /erp-crm-demo.html.'],
          ['2 · Windows desktop runner', 'Implemented: app/desktop_runner.py provides a bounded sequential UIA adapter for .NET/C#, C++ and WinForms (legacy/current), with custom Access/VBA, FoxPro, COBOL, AS/400 and Oracle desktop profiles, a simulated UIA tree and an authenticated Windows agent bridge.'],
          ['3 · Mobile and rugged device runner', 'Implemented: app/mobile_runner.py covers Android (UiAutomator2), iOS (XCUITest) and Zebra (DataWedge barcode injection for EAN-13, CODE-128, QR, GS1-128), with a deterministic simulator and an authenticated Appium hub bridge.'],
          ['4 · Pilot, security review and support matrix', 'Implemented above: run the deterministic pilot suite to mint a 24-hour receipt; only then do ERP/CRM packs badge VERIFIED IN THIS ENVIRONMENT, and desktop/mobile stay SIMULATOR READY until a live agent/hub pilot exists.']
        ].map(([title, detail]) => e('li', { key: title, className: 'rounded-xl border border-slate-800 bg-slate-950 p-3 space-y-1' },
          e('h3', { className: 'text-xs font-bold text-slate-100' }, title),
          e('p', { className: 'text-xs leading-relaxed text-slate-400' }, detail)))),
      e('p', { className: 'text-[10px] text-amber-200' },
        'No vendor partnerships or certifications are claimed. Simulator verification proves the TestForge contract locally; live verification needs your authorized tenant, Windows agent or device lab.'))),

    e(GuideSection, { id: "future-prompt", label: "AI prompt for the full roadmap",
      action: "COPY AI PROMPT gives you the spec to hand to a coding agent; switch scope for one phase only." },
    e(RoadmapAiPromptPanel, null)))
  );
}
