/* Future-release planning view. These are exploration targets, not claims of
   current integrations. The logo-style monograms are local placeholders so the
   tab remains self-contained and does not fetch vendor assets. */
const TESTFORGE_ROADMAP_TARGETS = [
  {
    group: 'ERP / CRM web applications', mark: 'ORCL', name: 'Oracle web applications',
    color: '#f80000', ink: '#ffffff', stage: 'Browser UI now · dedicated pack proposed',
    detail: 'Oracle browser apps such as Fusion or E-Business Suite can be approached as ordinary web pages when reachable. Oracle-specific authentication, selectors and workflow templates are not implemented.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'SAP', name: 'SAP · Fiori / web UI',
    color: '#0875b9', ink: '#ffffff', stage: 'Browser UI now · dedicated pack proposed',
    detail: 'Web-based SAP Fiori workflows may be recorded with generic browser controls. SAP GUI and certified SAP-specific integrations are outside current support.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'JD', name: 'JD Edwards',
    color: '#315b7d', ink: '#ffffff', stage: 'Browser UI now · dedicated pack proposed',
    detail: 'The browser UI can be explored with generic recording where access is authorized; JDE-specific patterns and test environments would need separate work.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'odoo', name: 'Odoo',
    color: '#714b67', ink: '#ffffff', stage: 'Browser UI now · dedicated pack proposed',
    detail: 'Odoo web workflows are a candidate for reusable ERP test templates. There is no Odoo connector or product-specific coverage today.'
  },
  {
    group: 'ERP / CRM web applications', mark: 'SF', name: 'Salesforce',
    color: '#00a1e0', ink: '#ffffff', stage: 'Browser UI now · dedicated pack proposed',
    detail: 'Accessible Salesforce pages can be approached through generic browser recording. Salesforce-specific authentication, sandbox setup and stable page objects remain future work.'
  },
  {
    group: 'Windows desktop applications', mark: '.NET', name: '.NET / C# desktop apps',
    color: '#512bd4', ink: '#ffffff', stage: 'Future proposal · not supported yet',
    detail: 'A Windows-hosted automation agent would be needed for native desktop controls; the current Playwright browser cannot drive ordinary desktop windows.'
  },
  {
    group: 'Windows desktop applications', mark: 'C++', name: 'C++ desktop apps',
    color: '#00599c', ink: '#ffffff', stage: 'Future proposal · not supported yet',
    detail: 'Native C++ applications would need a separate Windows UI automation adapter and a controlled runner, not a browser-only change.'
  },
  {
    group: 'Windows desktop applications', mark: 'WF', name: 'WinForms · legacy and current',
    color: '#475569', ink: '#ffffff', stage: 'Future proposal · not supported yet',
    detail: 'Possible target: recording and replay for older and newer WinForms applications via Windows accessibility/UI Automation, with compatibility testing by framework version.'
  },
  {
    group: 'Mobile and rugged devices', mark: 'AND', name: 'Android apps',
    color: '#3ddc84', ink: '#052e16', stage: 'Future proposal · not supported yet',
    detail: 'Native Android app testing would require an Android automation driver/device lab. A responsive browser viewport is not native app support.'
  },
  {
    group: 'Mobile and rugged devices', mark: 'iOS', name: 'iPhone / iPad apps',
    color: '#111827', ink: '#ffffff', stage: 'Future proposal · not supported yet',
    detail: 'Native iOS apps need Apple-device infrastructure and a mobile automation adapter; neither is part of the current browser runner.'
  },
  {
    group: 'Mobile and rugged devices', mark: 'Z', name: 'Zebra rugged devices',
    color: '#111827', ink: '#ffffff', stage: 'Future proposal · not supported yet',
    detail: 'Potential handheld coverage includes Android-based Zebra devices, barcode scanners and device-specific input. Hardware access and a dedicated runner would be required.'
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
    '## Phase 1 · ERP / CRM Web Application Packs (Oracle, SAP Fiori, JD Edwards, Odoo, Salesforce)',
    '1. Extend `app/domain_packs.py` and `/api/imports/packs` with five enterprise ERP/CRM packs: `oracle_web` (Oracle Fusion / E-Business Suite), `sap_fiori` (SAP Fiori / UI5 web UI), `jd_edwards` (JD Edwards EnterpriseOne web UI), `odoo_erp` (Odoo web workflows), and `salesforce_crm` (Salesforce Lightning web workflows).',
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
          current: target.stage.startsWith('Browser UI now')
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
      'Tip: Keep "Complete End-to-End Scope (All Phases 1–4)" selected to copy the full end-to-end implementation prompt across all 11 application targets and R01–R15 requirements, or switch tabs above to copy one phase at a time.')
  );
}

function FuturePlanningTab() {
  return e('div', { className: 'space-y-4' },
    e('section', { className: 'rounded-2xl border border-indigo-800 bg-slate-900 p-5 space-y-3' },
      e('div', { className: 'flex flex-wrap items-center justify-between gap-2' },
        e('span', { className: 'text-[10px] font-bold tracking-wide text-indigo-300' }, '🗺️ FUTURE RELEASE PLANNING'),
        e('span', { className: 'rounded-full bg-slate-800 px-2 py-1 text-[9px] font-bold text-amber-200' }, 'PROPOSED · NO DATES COMMITTED')),
      e('h1', { className: 'text-xl font-bold text-slate-100' }, 'Plan beyond browser testing'),
      e('p', { className: 'text-sm leading-relaxed text-slate-300' },
        'This page separates the current TestForge capability from possible future targets. It is a planning view, not a promise of vendor support or a release schedule.'),
      e('div', { className: 'rounded-xl border border-emerald-900/60 bg-emerald-950/30 p-3 space-y-1' },
        e('div', { className: 'flex flex-wrap items-center gap-2' },
          e('div', { className: 'flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-600 text-xs font-bold text-white' }, 'PW'),
          e('h2', { className: 'text-sm font-bold text-emerald-200' }, 'Supported today · browser UI'),
          e('span', { className: 'rounded-full border border-emerald-700 px-2 py-0.5 text-[9px] font-bold text-emerald-200' }, 'AVAILABLE NOW')),
        e('p', { className: 'text-xs leading-relaxed text-slate-300' },
          'TestForge records and replays accessible web pages using Playwright and Chromium. A web-based ERP or CRM may be tried through this generic browser workflow when you have authorized access; this does not mean TestForge has a dedicated or certified Oracle, SAP, JD Edwards, Odoo or Salesforce integration.'),
        e('p', { className: 'text-[10px] text-slate-500' },
          'Native Windows desktop apps and native Android/iOS apps are not supported by the current browser runner.'))),

    e('section', { className: 'rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-3' },
      e('div', { className: 'space-y-1' },
        e('h2', { className: 'text-sm font-bold text-indigo-300' }, 'Suggested application and tool targets'),
        e('p', { className: 'text-xs text-slate-400' },
          'The colorful initials below are local logo-style placeholders for planning. Vendor names identify possible targets only; these are not official logo assets or endorsements.')),
      TESTFORGE_ROADMAP_GROUPS.map(group => e('div', { key: group, className: 'space-y-2' },
        e('h3', { className: 'text-xs font-bold uppercase tracking-wide text-slate-300' }, group),
        e('div', { className: 'grid grid-cols-1 gap-2 md:grid-cols-2' },
          TESTFORGE_ROADMAP_TARGETS.filter(target => target.group === group).map(target =>
            e(RoadmapTargetCard, { key: target.name, target })))))),

    e('section', { className: 'rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-3' },
      e('h2', { className: 'text-sm font-bold text-indigo-300' }, 'A practical extension roadmap'),
      e('ol', { className: 'space-y-3' },
        [
          ['1 · ERP / CRM web packs', 'Discovery first: validate authorized test tenants, predictable authentication, reusable selectors and data-reset guidance for SAP Fiori, Oracle web apps, JD Edwards, Odoo and Salesforce.'],
          ['2 · Windows desktop runner', 'Research a separately deployed Windows agent using accessibility/UI Automation for .NET/C#, C++ and legacy/current WinForms. Isolate the agent and define version, permissions and evidence requirements.'],
          ['3 · Mobile and rugged device runner', 'Evaluate Android and iOS device automation, then Zebra handheld/scanner workflows, device availability, OS versions and hardware input. This needs native-device infrastructure; browser emulation is not enough.'],
          ['4 · Pilot, security review and support matrix', 'Only promote a target to “supported” after a repeatable end-to-end pilot, documented limits, safe test data, compatibility coverage and a maintained adapter.']
        ].map(([title, detail]) => e('li', { key: title, className: 'rounded-xl border border-slate-800 bg-slate-950 p-3 space-y-1' },
          e('h3', { className: 'text-xs font-bold text-slate-100' }, title),
          e('p', { className: 'text-xs leading-relaxed text-slate-400' }, detail)))),
      e('p', { className: 'text-[10px] text-amber-200' },
        'No dates, vendor partnerships, native integrations or certifications are committed. Treat every “future proposal” card as a candidate until it is implemented and verified.')),

    e(RoadmapAiPromptPanel, null)
  );
}
