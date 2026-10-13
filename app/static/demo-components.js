/* Demo tab: the built-in sample app, public retail learning URLs and synthetic fixtures.
   The local fixtures are deterministic; the external URLs are optional learning targets.
   Loaded after index.html's API helpers. */

const TESTFORGE_LEARNING_SITES = Object.freeze([
  { category: 'Shopping', name: 'Amazon', url: 'https://www.amazon.com/' },
  { category: 'Shopping', name: 'eBay', url: 'https://www.ebay.com/' },
  { category: 'Grocery', name: 'Kroger', url: 'https://www.kroger.com/' },
  { category: 'Grocery', name: 'Instacart', url: 'https://www.instacart.com/' },
  { category: 'Fashion', name: 'Zara', url: 'https://www.zara.com/' },
  { category: 'Fashion', name: 'H&M', url: 'https://www.hm.com/' },
]);
window.TESTFORGE_LEARNING_SITES = TESTFORGE_LEARNING_SITES;
const DEMO_SITE_CATEGORIES = ['Shopping', 'Grocery', 'Fashion'];

const DEMO_PACK_BLURB = {
  commerce: 'Order quantity → line total. Negative row rejects a zero quantity.',
  banking: 'Transfer amount → remaining balance. Negative row trips insufficient funds.',
  insurance: 'Applicant age → quote. Negative row rejects an under-18 applicant.',
  hrms: 'Leave days → remaining balance. Negative row trips insufficient leave.',
  travel: 'Seats requested → booking total. Negative row runs out of seats.',
  oracle_web: 'Purchase order lookup inside nested iframes with an LOV dialog. Negative row finds no record.',
  sap_fiori: 'Sales order lookup with data-sap-ui-id locators, busy indicator and ARIA grid. Negative row finds no record.',
  jd_edwards: 'Fast-path P0411 → order/batch status in the grid. Negative row finds no record.',
  ms_dynamics_fo: 'Dynamics 365 F&O sales order confirmation. Negative row finds no record.',
  ms_dynamics_bc: 'Business Central sales order posting. Negative row finds no record.',
  odoo_erp: 'Many2one partner pick (wait-aware suggestion) → Draft to Confirmed. Negative row finds no record.',
  salesforce_crm: 'Lightning opportunity save with toast assertion. Negative row finds no record.',
};

function DemoStep({ index, title, children }) {
  return e('div', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 flex gap-3' },
    e('span', { className: 'shrink-0 w-6 h-6 rounded-full bg-indigo-600 text-white text-xs font-bold flex items-center justify-center' }, index),
    e('div', { className: 'space-y-2 min-w-0 flex-1' },
      e('p', { className: 'text-sm font-bold text-indigo-300' }, title),
      children));
}

function VoiceDemo() {
  const [text, setText] = useState('');
  const supported = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  return e('div', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-2' },
    e('h3', { className: 'text-sm font-bold text-indigo-300' }, '🎤 Voice input and text cleanup · local helpers'),
    e('p', { className: 'text-xs text-slate-400' },
      'The microphone button on this field runs your own browser\u2019s speech recognition — nothing is sent to a server and no paid AI is involved. The sparkle button then applies the deterministic local cleanup (collapse whitespace, capitalise, add a full stop) via /api/ai/rephrase. Tap 🎤, say a sentence, then press ✨.'),
    !supported && e('p', { role: 'alert', className: 'text-xs text-amber-300' },
      'This browser does not expose speech recognition (Chrome and Edge do), so the microphone will not work here. You can still type a sentence and try the cleanup button.'),
    e(AiInput, { value: text, onChange: ev => setText(ev.target.value), placeholder: 'Tap 🎤 and speak, or type here, then press ✨', isArea: true }),
    text && e('button', { type: 'button', className: 'key-btn', onClick: () => setText('') }, 'CLEAR'));
}

function DemoFrame({ src, title, height = 320 }) {
  const [nonce, setNonce] = useState(0);
  return e('div', { className: 'browser-shell' },
    e('div', { className: 'browser-bar' },
      e('span', { className: 'dot red' }), e('span', { className: 'dot yellow' }), e('span', { className: 'dot green' }),
      e('span', { className: 'browser-url' }, src),
      e('button', { type: 'button', className: 'key-btn', onClick: () => setNonce(nonce + 1) }, 'RELOAD')),
    // Same-origin page from this instance, so the browser renders it directly;
    // no screencast, no server-side browser and no run budget is consumed.
    e('iframe', { key: nonce, src, title, className: 'w-full bg-white', style: { height: `${height}px`, border: '0' } }));
}

function LearningSitesPanel({ onUse }) {
  return e('div', { className: 'grid grid-cols-1 md:grid-cols-3 gap-3' },
    DEMO_SITE_CATEGORIES.map(category => e('section', {
      key: category, className: 'bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-2'
    },
      e('h4', { className: 'text-xs font-bold uppercase tracking-wide text-slate-300' }, category),
      TESTFORGE_LEARNING_SITES.filter(site => site.category === category).map(site =>
        e('div', { key: site.url, className: 'flex items-center justify-between gap-2 border-t border-slate-800 pt-2' },
          e('div', { className: 'min-w-0' },
            e('a', { href: site.url, target: '_blank', rel: 'noopener noreferrer',
              className: 'text-xs font-bold text-indigo-300 underline' }, site.name),
            e('p', { className: 'text-[10px] text-slate-500 break-all' }, site.url)),
          e('button', { type: 'button', className: 'key-btn shrink-0', onClick: () => onUse(site),
            title: `Use ${site.name} as the AI recording start URL` }, 'USE IN AI RECORDING'))))));
}

function DemoTab({ project, setActiveProject, setTab, liveWindow, onRun }) {
  const [projects, setProjects] = useState([]);
  const [recordings, setRecordings] = useState([]);
  const [packs, setPacks] = useState([]);
  const [pack, setPack] = useState('');
  const [packDetail, setPackDetail] = useState(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');

  const sample = projects.find(p => p.id === 'qa-sample-app')
    || projects.find(p => (p.base_url || '').endsWith('/demo.html'));
  const sampleActive = !!(project && sample && project.id === sample.id);

  const useLearningSite = (site) => {
    if (!project && !sample) {
      setNotice('Choose or create a project before starting a learning-site recording.');
      setTab('projects');
      return;
    }
    if (!project && sample) {
      setActiveProject(sample);
      try { localStorage.setItem('tf_project', JSON.stringify(sample)); } catch (_) {}
    }
    try { localStorage.setItem('tf_pending_recorder_url', site.url); } catch (_) {}
    setTab('record');
  };

  useEffect(() => {
    API.get('/api/projects?fast=true').then(list => setProjects(Array.isArray(list) ? list : []))
      .catch(err => setError(err.message));
    API.get('/api/imports/packs').then(list => setPacks(Array.isArray(list) ? list : [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (!sample) { setRecordings([]); return; }
    API.get(`/api/projects/${sample.id}/recordings?fast=true`)
      .then(list => setRecordings(Array.isArray(list) ? list : [])).catch(() => setRecordings([]));
  }, [sample && sample.id]);

  const chooseSample = () => {
    if (!sample) return;
    setActiveProject(sample);
    localStorage.setItem('tf_project', JSON.stringify(sample));
    setNotice(`“${sample.name}” is now the active project.`);
  };

  // The sample project ships in library/, but an instance pointed at an empty
  // TF_LIBRARY_DIR will not have it. Creating it is an ordinary project save and
  // publishes like one, so it stays an explicit click rather than a side effect.
  const createSample = async () => {
    setBusy('project'); setError('');
    try {
      const created = await API.post('/api/projects', { name: 'TestForge Testing Tool', base_url: '/demo.html' });
      setProjects(prev => [created, ...prev.filter(p => p.id !== created.id)]);
      setActiveProject(created);
      localStorage.setItem('tf_project', JSON.stringify(created));
      setNotice(created.publish_message || `“${created.name}” created and selected.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(''); }
  };

  const replay = async (recording) => {
    setBusy(recording.id); setError('');
    try {
      const result = await API.post('/api/runs', { target_id: recording.id, display_window: liveWindow !== false, execution_speed: window.TESTFORGE_SPEED_FACTOR || 1 });
      onRun(result.run_id);
    } catch (err) { setError(err.message); }
    finally { setBusy(''); }
  };

  const loadPack = async (slug) => {
    setPack(slug); setPackDetail(null); setError('');
    if (!slug) return;
    setBusy('pack');
    try { setPackDetail(await API.get('/api/imports/packs/' + slug)); }
    catch (err) { setError(err.message); }
    finally { setBusy(''); }
  };

  const dataset = (packDetail && packDetail.execution_plan && packDetail.execution_plan.dataset) || [];

  return e('div', { className: 'space-y-4' },
    e(GuideSection, { id: 'demo-intro', label: 'Demo tab · start here',
      action: 'Read this card, then work down steps 1–4. Nothing here touches a real system.' },
    e('div', { className: 'bg-slate-900 border border-indigo-800 rounded-2xl p-4 space-y-2' },
      e('h2', { className: 'font-bold text-lg text-indigo-300' }, '🧪 Demo · try TestForge without a real application'),
      e('p', { className: 'text-xs text-slate-400' },
        'The built-in pages are static files served by this instance. The optional store links farther down open real public sites only when you choose them; no site is contacted automatically. Never use real accounts, submit purchases or enter payment details. Use this tab to prove recording, replay and evidence with the local sample before you point TestForge at your own application.'),
      !sample && e('p', { role: 'alert', className: 'text-xs text-amber-300' },
        'No sample project found in this library. Create it below to follow the walkthrough.'))),

    error && e('p', { role: 'alert', className: 'text-sm text-red-300' }, error),
    notice && e('p', { role: 'status', className: 'text-sm text-emerald-300' }, notice),

    e(GuideSection, { id: 'demo-step1', label: 'Step 1 · pick the sample project',
      action: 'Press USE THE SAMPLE PROJECT — every other tab then works against it.' },
    e(DemoStep, { index: 1, title: 'Select the sample project' },
      e('p', { className: 'text-xs text-slate-400' },
        sample ? `“${sample.name}” points at the built-in page /demo.html and already holds the variable {{user}} = Ada.`
               : 'The sample project is missing from this library. Creating it saves and publishes exactly like any other project.'),
      sample
        ? e('button', { type: 'button', className: 'key-btn', onClick: chooseSample, disabled: sampleActive },
            sampleActive ? '✓ ACTIVE PROJECT' : 'USE THE SAMPLE PROJECT')
        : e('button', { type: 'button', className: 'key-btn', onClick: createSample, disabled: busy === 'project' },
            busy === 'project' ? 'CREATING…' : 'CREATE SAMPLE PROJECT'))),

    e(GuideSection, { id: 'demo-step2', label: 'Step 2 · replay a ready-made test',
      action: 'Press RUN on “Hello variable” to watch {{user}} get substituted at replay time.' },
    e(DemoStep, { index: 2, title: 'Replay a ready-made test' },
      e('p', { className: 'text-xs text-slate-400' },
        'These two recordings ship with the repository. “Hello literal” types a fixed name; “Hello variable” types {{user}}, so it shows how a variable is substituted at replay time. Each run opens the single server-side Chromium, captures one low-quality JPEG per step, and lands in Runs and Artifacts.'),
      !sampleActive && e('p', { className: 'text-xs text-amber-300' }, 'Select the sample project first.'),
      sampleActive && recordings.length === 0 && e('p', { className: 'text-xs text-slate-500' }, 'No recordings in this project yet — record one in step 3.'),
      sampleActive && recordings.map(r => e('div', { key: r.id, className: 'flex items-center justify-between gap-2 bg-slate-950 border border-slate-800 rounded-lg p-2' },
        e('div', { className: 'min-w-0' },
          e('div', { className: 'text-xs font-bold text-indigo-300 truncate' }, r.name),
          e('div', { className: 'text-[10px] text-slate-500 truncate' }, `${r.step_count != null ? r.step_count + ' steps · ' : ''}${r.start_url || '/demo.html'}`)),
        e('button', { type: 'button', className: 'key-btn shrink-0', disabled: !!busy, onClick: () => replay(r) },
          busy === r.id ? 'QUEUEING…' : 'RUN'))))),

    e(GuideSection, { id: 'demo-step3', label: 'Step 3 · record your own test',
      action: 'Open the ⏺ Record tab, leave the URL blank, press START — that records against the sample app.' },
    e(DemoStep, { index: 3, title: 'Record your own against the sample app' },
      e('p', { className: 'text-xs text-slate-400' },
        'Open the Record tab and press START with the URL left blank — a blank URL opens /demo.html on the server browser. Click the picture to click, type into the text box, then SAVE.'),
      e('div', { className: 'flex flex-wrap gap-2' },
        e('button', { type: 'button', className: 'key-btn', onClick: () => setTab('record') }, 'OPEN RECORD TAB'),
        e('button', { type: 'button', className: 'key-btn', onClick: () => setTab('library') }, 'OPEN LIBRARY TAB')))),

    e(GuideSection, { id: 'demo-step4', label: 'Step 4 · check the evidence',
      action: 'Open ▶ Runs and 🎥 Artifacts for the per-step screenshots and the PDF report.' },
    e(DemoStep, { index: 4, title: 'Check the evidence' },
      e('p', { className: 'text-xs text-slate-400' },
        'Runs shows each step with its status and live picture; Artifacts collects the screenshots and any video; Comparison diffs the step logs of recent runs. A PDF report is available per run.'),
      e('div', { className: 'flex flex-wrap gap-2' },
        e('button', { type: 'button', className: 'key-btn', onClick: () => setTab('runs') }, 'OPEN RUNS TAB'),
        e('button', { type: 'button', className: 'key-btn', onClick: () => setTab('artifacts') }, 'OPEN ARTIFACTS TAB')))),

    e(GuideSection, { id: 'demo-learning-sites', label: 'Online store learning URLs',
      action: 'Open a public site or choose USE IN AI RECORDING to prefill its URL in the recorder.' },
    e('div', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-3' },
      e('h3', { className: 'text-sm font-bold text-indigo-300' }, '🌐 Shopping, grocery and fashion learning sites'),
      e('p', { className: 'text-xs text-slate-400' },
        'These are public external websites for learning the recorder — they are not stable test fixtures and may vary by region, cookies, sign-in requirements or anti-automation rules.'),
      e(LearningSitesPanel, { onUse: useLearningSite }),
      e('p', { className: 'text-[10px] text-amber-300' },
        'Use only pages and flows you are authorized to test. Do not sign in with customer accounts, place real orders or enter payment details. For deterministic runs, use the local sample app or your own staging site.'))),

    e(GuideSection, { id: 'demo-sample-app', label: 'The sample app itself',
      action: 'Type a name and press Continue — this is the exact page the server browser drives on replay.' },
    e('div', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-2' },
      e('h3', { className: 'text-sm font-bold text-indigo-300' }, 'TestForge Testing Tool · /demo.html'),
      e('p', { className: 'text-xs text-slate-400' },
        'This is the page your recordings drive. It is rendered here by your own browser so you can see what the steps are doing; replay drives the identical page in the server browser. Type a name and press Continue.'),
      e(DemoFrame, { src: '/demo.html', title: 'TestForge Testing Tool', height: 300 }))),

    e(GuideSection, { id: 'demo-voice', label: 'Voice input + text cleanup',
      action: 'Tap 🎤, speak a sentence, then ✨. Speech recognition runs in your own browser, not here.' },
    e(VoiceDemo, null)),

    e(GuideSection, { id: 'demo-domains', label: 'Synthetic domain fixtures',
      action: 'Pick a domain to see one positive and one negative dataset row, then save it as a real test.' },
    e('div', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-3' },
      e('h3', { className: 'text-sm font-bold text-indigo-300' }, 'Synthetic domain fixtures · /domain-demo.html'),
      e('p', { className: 'text-xs text-slate-400' },
        'One local page simulating five business domains, each with a positive and a negative dataset row and a teardown that resets the form. Simulation only: no real transaction is performed and no domain certification is implied.'),
      e('label', { className: 'block text-xs text-slate-400' }, 'Choose a domain',
        e('select', {
          'aria-label': 'Demo domain fixture', value: pack, disabled: busy === 'pack',
          className: 'block mt-1 bg-slate-800 p-2 rounded text-xs',
          onChange: ev => loadPack(ev.target.value)
        }, e('option', { value: '' }, 'Choose an example…'),
           packs.map(p => e('option', { key: p.id, value: p.id }, p.name)))),
      pack && e('p', { className: 'text-xs text-slate-400' }, DEMO_PACK_BLURB[pack] || ''),
      pack && e(DemoFrame, { src: `${packDetail && packDetail.demo_url ? new URL(packDetail.demo_url, location.origin).pathname : '/domain-demo.html'}?pack=${encodeURIComponent(pack)}`, title: `Domain fixture ${pack}`, height: 340 }),
      dataset.length > 0 && e('div', { className: 'space-y-1' },
        e('p', { className: 'text-xs font-bold text-slate-300' }, 'Dataset rows this example asserts'),
        e('div', { className: 'overflow-x-auto border border-slate-800 rounded-lg' },
          e('table', { className: 'w-full text-[10px]' },
            e('thead', {}, e('tr', { className: 'bg-slate-950 text-slate-400' },
              e('th', { className: 'text-left p-2' }, 'Input'), e('th', { className: 'text-left p-2' }, 'Expected result'))),
            e('tbody', {}, dataset.map((row, i) => e('tr', { key: i, className: 'border-t border-slate-800' },
              e('td', { className: 'p-2 font-mono' }, row.amount !== undefined ? row.amount : row.input),
              e('td', { className: 'p-2' }, row.expected))))))),
      packDetail && e('p', { className: 'text-xs text-slate-400' },
        `To save this as a real test: Library tab → UPLOAD TEST SCRIPTS → “Or start with a synthetic domain pack” → ${packDetail.name}, then TEST BEFORE PUBLISHING and SAVE TO LIBRARY.`),
      packDetail && e('button', { type: 'button', className: 'key-btn', onClick: () => setTab('library') }, 'OPEN LIBRARY TAB'),
      packDetail && packDetail.notice && e('p', { className: 'text-[10px] text-amber-300' }, packDetail.notice))),

    e(GuideSection, { id: 'demo-erp-crm', label: 'ERP/CRM + desktop & mobile fixtures',
      action: 'Open the deterministic ERP/CRM fixture or the Zebra handheld fixture. Enterprise packs sign in with TF_SECRET_* references only.' },
    e('div', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-3' },
      e('h3', { className: 'text-sm font-bold text-indigo-300' }, 'Enterprise fixtures · /erp-crm-demo.html · /mobile-zebra-demo.html'),
      e('p', { className: 'text-xs text-slate-400' },
        'The ERP/CRM fixture is multi-tenant (demo/acme/globex) and models Oracle nested iframes and LOV dialogs, SAP Fiori data-sap-ui-id grids with a busy indicator, JD Edwards fast-path and batch status, Dynamics 365 F&O and Business Central forms, Odoo many2one suggestions with Draft to Confirmed status, and Salesforce Lightning toasts. Choose an enterprise pack in the domain selector above to preview it; the seven ERP/CRM packs appear there alongside the five synthetic domains. Windows desktop and mobile plans instead run against the in-process simulators in app/desktop_runner.py and app/mobile_runner.py, with the Zebra fixture mirroring the handheld state machine.'),
      e('div', { className: 'flex flex-wrap gap-2' },
        e('a', { href: '/erp-crm-demo.html', className: 'key-btn' }, 'OPEN ERP/CRM DEMO'),
        e('a', { href: '/mobile-zebra-demo.html', className: 'key-btn' }, 'OPEN MOBILE/ZEBRA DEMO'),
        e('button', { type: 'button', className: 'key-btn', onClick: () => setTab('future') }, 'OPEN SUPPORT MATRIX')))));
}
