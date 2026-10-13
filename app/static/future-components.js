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
        'No dates, vendor partnerships, native integrations or certifications are committed. Treat every “future proposal” card as a candidate until it is implemented and verified.'))
  );
}
