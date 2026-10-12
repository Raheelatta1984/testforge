/* Loaded after index.html's API helpers and before the app renders. No services. */
function ActionComposer({ onSave, label = 'ADD STEP' }) {
  const [action, setAction] = useState('assert_text');
  const [primary, setPrimary] = useState('');
  const [value, setValue] = useState('');
  const [kind, setKind] = useState('css');
  const [options, setOptions] = useState('{}');
  const [actions, setActions] = useState([]);
  const [fixture, setFixture] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { API.get('/api/imports/formats').then(d => setActions(d.actions)).catch(err => setError(err.message)); }, []);
  const save = async () => {
    setBusy(true); setError('');
    try {
      const extra = JSON.parse(options);
      if (!extra || Array.isArray(extra) || typeof extra !== 'object') throw new Error('Options must be a JSON object');
      await onSave({action, value, label: action, selector:{...(primary ? {primary,kind} : {}), ...extra, ...(action==='upload_file' && fixture ? fixture : {})}});
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  return e('details', {className:'bg-slate-900 border border-slate-700 p-3 rounded-xl space-y-2'},
    e('summary', {className:'text-xs font-bold text-indigo-300 cursor-pointer'}, 'Assertions & advanced actions'),
    e('p', {className:'text-xs text-slate-400'}, 'No fixed sleeps. Locators and assertions wait only until ready, up to their timeout. For page/URL/API actions leave the locator blank.'),
    e('div', {className:'flex flex-wrap gap-2'},
      e('select', {'aria-label':'Action', value:action,onChange:ev=>setAction(ev.target.value),className:'bg-slate-800 p-2 rounded text-xs'},actions.map(a=>e('option',{key:a,value:a},a))),
      e('select', {'aria-label':'Locator type',value:kind,onChange:ev=>setKind(ev.target.value),className:'bg-slate-800 p-2 rounded text-xs'},['css','role','label','text','test_id','placeholder'].map(a=>e('option',{key:a,value:a},a)))
    ),
    e('input', {'aria-label':'Locator',placeholder:'Locator, e.g. #out or button (role)',value:primary,onChange:ev=>setPrimary(ev.target.value),className:'w-full bg-slate-800 p-2 rounded text-xs'}),
    e('input', {'aria-label':'Action value',placeholder:'Input / expected value / URL / variable name',value,onChange:ev=>setValue(ev.target.value),className:'w-full bg-slate-800 p-2 rounded text-xs'}),
    action === 'upload_file' && e('label', {className:'block text-xs'}, 'Synthetic upload fixture (max 512 KiB; becomes part of saved/published steps)',
      e('input', {type:'file', 'aria-label':'Target application upload fixture', onChange:ev=>{
        const file=ev.target.files?.[0]; setFixture(null); if(!file) return;
        if(file.size>512*1024) {setError('Fixture exceeds 512 KiB');return;}
        const reader=new FileReader(); reader.onerror=()=>setError('Unable to read fixture');
        reader.onload=()=>{setFixture({filename:file.name,mime_type:file.type || 'application/octet-stream',base64:String(reader.result).split(',')[1]});setError('');};
        reader.readAsDataURL(file);
      },className:'block text-xs mt-2'})),
    e('label', {className:'block text-xs'}, 'Additional options (JSON)',e('textarea', {value:options,onChange:ev=>setOptions(ev.target.value),className:'w-full bg-slate-800 p-2 rounded text-xs font-mono',rows:3})),
    e('p', {className:'text-xs text-slate-400'}, 'Examples: {"name":"Continue"} for role; {"frames":["#frame"]}; {"alias":"details"} for new_page; {"method":"GET","status":200,"json_path":"id","capture":"id"} for api_request.'),
    e('button', {onClick:save,disabled:busy,className:'bg-indigo-600 px-3 py-2 rounded text-xs disabled:opacity-50'},busy ? 'WORKING…' : label),
    error && e('p',{role:'alert',className:'text-red-300 text-xs'},error)
  );
}

function ImportScripts({ project, onSaved, onRun }) {
  const [files, setFiles] = useState([]);
  const [preview, setPreview] = useState(null);
  const [name, setName] = useState('');
  const [tags, setTags] = useState('imported');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saved, setSaved] = useState(null);
  const [draft,setDraft] = useState(null);
  const [planOptions,setPlanOptions] = useState('{}');
  const [packs,setPacks]=useState([]);
  // Template pack: one downloadable example per supported format. Loading one runs
  // the same preview as an upload, so a team can see the parsed steps before they
  // replace them with their own (and see that onboarding reports no errors).
  const [templates,setTemplates]=useState([]);
  const [templatesNotice,setTemplatesNotice]=useState('');
  const [templateId,setTemplateId]=useState('');
  // Input-review state. These declarations (and the three helpers below) belong
  // to this component: without them every render of the Library tab threw
  // `setVariablePlan is not defined` and the tab rendered nothing at all.
  const [variablePlan,setVariablePlan] = useState(null);
  const [adopted,setAdopted] = useState([]);
  const [existingVars,setExistingVars] = useState([]);
  useEffect(()=>{API.get('/api/imports/packs').then(setPacks).catch(()=>{});},[]);
  useEffect(()=>{API.get('/api/imports/templates').then(data=>{setTemplates(data.templates||[]);setTemplatesNotice(data.notice||'');}).catch(()=>{});},[]);
  const base64ToBlob = (encoded, name) => {
    const raw = atob(encoded || '');
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return new File([bytes], name, {type:'application/octet-stream'});
  };
  const loadTemplate = async () => {
    const entry = templates.find(item => item.id === templateId);
    if (!entry) return;
    setBusy(true); setError(''); setNotice(''); setSaved(null); setDraft(null); setVariablePlan(null);
    const version = generation.current;
    try {
      const names = entry.files || [];
      const built = names.map(name => base64ToBlob((entry.content||{})[name], name));
      if (!built.length) throw new Error('This template has no files to preview');
      setFiles(built);
      const result = entry.bundle
        ? await API.post('/api/imports/preview-bundle',{files:names.map(name=>({name,base64:(entry.content||{})[name]}))})
        : await apiRequest('/api/imports/preview?filename='+encodeURIComponent(names[0]),
            {method:'POST',headers:{'Content-Type':'application/octet-stream'},body:built[0]});
      if (version !== generation.current) return;
      const errors = (result.diagnostics || []).filter(item => item.severity !== 'note').length;
      setPreview(result); setPlanOptions(JSON.stringify(result.execution_plan || {},null,2)); setName(result.name);
      setNotice(`Template "${entry.name}" parsed: ${result.steps.length} step(s), ${errors} error(s). ${result.notice || ''}`);
      if (errors) setError(`${errors} diagnostic(s) in this template — report it, the pack is meant to onboard cleanly.`);
    } catch(err) { if(version===generation.current) setError(err.message); }
    finally { setBusy(false); }
  };
  const generation = useRef(0);
  const reviewVariables = async () => {
    if(!preview?.steps?.length) return;
    setBusy(true); setError(''); const version=generation.current;
    try {
      const result=await API.post('/api/imports/variable-plan',{project_id:project.id,steps:preview.steps});
      if(version!==generation.current) return;
      setVariablePlan(result); setExistingVars(result.existing || []);
      if(!result.count) setNotice('No literal input values found — every value already uses a variable.');
    } catch(err){if(version===generation.current)setError(err.message);}
    finally {setBusy(false);}
  };
  const adoptVariables = async () => {
    if(!variablePlan) return;
    setBusy(true); setError(''); const version=generation.current;
    try {
      const chosen=(variablePlan.candidates||[]).filter(item=>item.selected!==false);
      if(!chosen.length) throw new Error('Keep at least one value, or cancel the review.');
      const result=await API.post('/api/imports/variables',{project_id:project.id,steps:preview.steps,
        assignments:chosen.map(item=>({name:item.name,value:item.value,is_secret:!!item.is_secret,category:item.category,create:!!item.create}))});
      if(version!==generation.current) return;
      setPreview({...preview,steps:result.steps}); setAdopted(result.created_variables || []);
      setVariablePlan(null); setDraft(null); setSaved(null);
      setNotice(`${result.counts.variables_created} variable(s) created, ${result.counts.variables_reused} reused. Steps now reference the variables — test before publishing.`);
    } catch(err){if(version===generation.current)setError(err.message);}
    finally {setBusy(false);}
  };
  const edits=(item,change)=>setVariablePlan(plan=>({...plan,candidates:(plan.candidates||[]).map(entry=>entry.value===item.value?{...entry,...change}:entry)}));
  useEffect(()=>{
    if(!draft || !['queued','running'].includes(draft.status)) return;
    let disposed=false, timer;
    const check=async()=>{
      if(document.hidden) {timer=setTimeout(check,1000);return;}
      try {const result=await API.get('/api/imports/tests/'+draft.id);if(!disposed) setDraft(result);}
      catch(err) {if(!disposed) {setError(err.message);setDraft(null);}}
      if(!disposed) timer=setTimeout(check,500);
    };
    timer=setTimeout(check,200);
    return ()=>{disposed=true;clearTimeout(timer);};
  },[draft?.id,draft?.status]);
  const payload=()=>{
    const opts=JSON.parse(planOptions);
    if(!opts || Array.isArray(opts) || typeof opts!=='object') throw new Error('Plan options must be a JSON object');
    return {project_id:project.id, name,tags,steps:preview.steps,
      ...(Object.keys(opts).length ? {execution_plan:{...opts,steps:opts.steps || preview.steps}} : {})};
  };
  const testDraft=async()=>{
    setBusy(true);setError('');setDraft(null);const version=generation.current;
    try {const result=await API.post('/api/imports/test',payload());if(version===generation.current) setDraft(result);}
    catch(err) {if(version===generation.current) setError(err.message);}
    finally {setBusy(false);}
  };
  useEffect(() => { generation.current++; setPreview(null); setSaved(null); setFiles([]); setNotice(''); setError(''); setDraft(null);setPlanOptions('{}');setVariablePlan(null);setAdopted([]); }, [project.id]);
  const readBase64 = file => new Promise((resolve,reject) => {
    const reader=new FileReader(); reader.onerror=()=>reject(new Error('Unable to read '+file.name));
    reader.onload=()=>resolve(String(reader.result).split(',')[1]); reader.readAsDataURL(file);
  });
  const inspect = async () => {
    setBusy(true); setError(''); setPreview(null); setSaved(null); setNotice('');setDraft(null);setVariablePlan(null);setAdopted([]);
    const version=generation.current;
    try {
      if(!files.length || files.length>8 || files.reduce((n,f)=>n+f.size,0)>1048576) throw new Error('Choose 1–8 files totalling at most 1 MiB. One test per recording; Jenkins bundles include the referenced script.');
      const result=files.length===1
        ? await apiRequest('/api/imports/preview?filename='+encodeURIComponent(files[0].name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:files[0]})
        : await API.post('/api/imports/preview-bundle',{files:await Promise.all(files.map(async f=>({name:f.name,base64:await readBase64(f)})))});
      if(version!==generation.current) return;
      setPreview(result); setPlanOptions(JSON.stringify(result.execution_plan || {},null,2)); setName(result.name);
      setNotice(result.notice || 'Preview only: no browser launched, no source code executed, nothing saved or published.');
      if(result.can_save && result.variable_suggestions && Object.keys(result.variable_suggestions).length)
        setNotice((result.notice || 'Preview ready.')+` Detected ${Object.keys(result.variable_suggestions).length} declared variable(s) — review input values below.`);
    } catch(err) { if(version===generation.current) setError(err.message); }
    finally {setBusy(false);}
  };
  const save = async () => {
    if(!preview?.can_save) return;
    setBusy(true); setError(''); const version=generation.current;
    try {
      const result=await API.post('/api/imports/save',{...payload(), draft_id:draft?.id,require_test:true});
      if(version!==generation.current) return;
      setSaved(result); setNotice(result.publish_message || 'Saved locally. Check GitHub publishing status.');
      setVariablePlan(null); setAdopted([]);
      onSaved(result);
    } catch(err) {if(version===generation.current) setError(err.message);}
    finally {setBusy(false);}
  };
  return e('details',{className:'border border-indigo-800 bg-slate-900 p-4 rounded-xl space-y-3'},
    e('summary',{className:'font-bold text-indigo-300 cursor-pointer'},'UPLOAD TEST SCRIPTS'),
    e('p',{className:'text-xs text-slate-300'},'Any format you already have: plain text/Markdown/logs, CSV/TSV/PSV or any smart table, Excel (.xlsx/.xls), Word (.docx), PDF text layers, XML/YAML, JUnit-style case lists, Selenium IDE (.side) or legacy HTML suites, Postman collections, Robot Framework, Cypress, supported Playwright Python/JS/TS, restricted Java/C# Selenium, HAR captures, BDD/Cucumber (.feature), or a restricted Jenkinsfile + its test script. The file is sniffed when the extension is unknown; preview reports unsupported syntax. Arbitrary code, shell commands, macros and custom step definitions never run.'),
    e('p',{className:'text-xs text-amber-300'},'Use {{variables}} instead of passwords. Saving uses the existing GitHub publication settings. Unsupported source is never silently treated as tested. No paid parser or AI service is used.'),
    e('label',{className:'block text-xs'},'Or start with a synthetic domain pack',
      e('select',{'aria-label':'Synthetic domain pack',defaultValue:'',disabled:busy || ['queued','running'].includes(draft?.status),className:'block bg-slate-800 p-2 rounded',onChange:async ev=>{
        const slug=ev.target.value;if(!slug) return;
        setBusy(true);setError('');setDraft(null);setSaved(null);const version=generation.current;
        try {const result=await API.get('/api/imports/packs/'+slug);if(version!==generation.current)return;
          result.execution_plan.variables={demo_url:result.demo_url};
          setPreview(result);setName(result.name);setPlanOptions(JSON.stringify(result.execution_plan,null,2));setNotice(result.notice);
        } catch(err){if(version===generation.current)setError(err.message);} finally{setBusy(false);}
      }},e('option',{value:''},'Choose an example…'),packs.map(pack=>e('option',{key:pack.id,value:pack.id},pack.name)))),
    e('div',{className:'border border-slate-700 bg-slate-950 p-3 rounded-xl space-y-2'},
      e('p',{className:'text-xs font-bold text-indigo-300'},'START FROM A TEMPLATE (ANY FORMAT)'),
      e('p',{className:'text-[11px] text-slate-400'},templatesNotice || 'Every template in this pack previews with zero errors. Load one to see its steps, then replace them with your own scenario.'),
      e('div',{className:'flex flex-wrap gap-2 items-center'},
        e('select',{'aria-label':'Onboarding template',value:templateId,disabled:busy,onChange:ev=>setTemplateId(ev.target.value),className:'bg-slate-800 p-2 rounded text-xs flex-1 min-w-[14rem]'},
          e('option',{value:''},'Choose a template…'),
          templates.map(item=>e('option',{key:item.id,value:item.id},`${item.name} — ${item.format}`))),
        e('button',{type:'button',disabled:busy || !templateId,onClick:loadTemplate,className:'key-btn bg-indigo-700 disabled:opacity-50'},busy?'LOADING…':'PREVIEW THIS TEMPLATE'),
        templateId && (()=>{const entry=templates.find(item=>item.id===templateId);if(!entry)return null;
          return e('a',{href:'/api/imports/templates/'+entry.id,className:'text-indigo-300 underline text-xs'},entry.bundle?'DOWNLOAD (.zip)':`DOWNLOAD ${entry.files[0]}`);})(),
        e('a',{href:'/api/imports/templates.zip',className:'text-indigo-300 underline text-xs'},'DOWNLOAD ALL (.zip)'),
        e('a',{href:'/api/imports/template.csv',className:'text-indigo-300 underline text-xs'},'DOWNLOAD CSV TEMPLATE'))),
    e('input',{key:project.id,type:'file',multiple:true,accept:'.csv,.tsv,.psv,.tab,.txt,.md,.markdown,.log,.rst,.rtf,.xlsx,.xls,.xlsm,.json,.jsonc,.xml,.yaml,.yml,.har,.html,.htm,.docx,.doc,.pdf,.py,.js,.ts,.mjs,.jsx,.tsx,.cs,.java,.feature,.bdd,.gherkin,.robot,.resource,.side,.jenkins,.groovy,Jenkinsfile','aria-label':'Upload test scripts',disabled:busy || ['queued','running'].includes(draft?.status),onChange:ev=>{setFiles(Array.from(ev.target.files || []));setPreview(null);setSaved(null);setDraft(null);},className:'block w-full text-xs'}),
    e('button',{disabled:busy || !files.length || ['queued','running'].includes(draft?.status),onClick:inspect,className:'bg-indigo-600 px-4 py-2 rounded text-xs disabled:opacity-50'},busy?'WORKING…':'VALIDATE & PREVIEW'),
    error && e('p',{role:'alert',className:'text-red-300 text-xs'},error),
    notice && e('p',{role:'status',className:'text-emerald-300 text-xs'},notice),
    preview && e('div',{className:'space-y-2'},
      e('p',{className:'text-sm font-bold'},`${preview.steps.length} supported steps · ${preview.can_save?'ready to save':'corrections required'} · sequential execution`),
      preview.detected && e('p',{className:'text-xs text-indigo-300'},`Detected as ${preview.detected.format} — ${preview.detected.why}`),
      e('button',{onClick:reviewVariables,disabled:busy || !preview.steps.length,className:'bg-amber-700 px-3 py-2 rounded text-xs disabled:opacity-50'},'REVIEW INPUT VALUES → VARIABLES'),
      adopted.length>0 && e('p',{role:'status',className:'text-xs text-emerald-300'},`Variables created: ${adopted.map(v=>v.name).join(', ')}`),
      variablePlan && e('div',{className:'border border-amber-700 bg-slate-950 p-3 rounded space-y-2'},
        e('p',{className:'text-xs font-bold text-amber-300'},`${variablePlan.summary}. Values already typed stay working; names become reusable variables/parameters.`),
        (variablePlan.candidates||[]).map((item,i)=>e('div',{key:i,className:'space-y-1 border-b border-slate-800 pb-2'},
          e('label',{className:'flex items-center gap-2 text-xs'},
            e('input',{type:'checkbox',checked:item.selected!==false,onChange:ev=>edits(item,{selected:ev.target.checked})}),
            e('span',{className:'text-slate-400'},`${item.usages_count}× `),
            e('input',{'aria-label':'Variable name',value:item.name,onChange:ev=>edits(item,{name:ev.target.value}),className:'bg-slate-800 p-1 rounded w-40'}),
            e('span',{className:'text-slate-300 break-all'},`= ${item.is_secret?'••••••':item.value}`),
            e('label',{className:'flex items-center gap-1 text-slate-400'},e('input',{type:'checkbox',checked:!!item.is_secret,onChange:ev=>edits(item,{is_secret:ev.target.checked})}),'secret'),
            e('select',{'aria-label':'Variable category',value:item.category,onChange:ev=>edits(item,{category:ev.target.value}),className:'bg-slate-800 rounded'},
              ['General','Parameter','Auth'].map(name=>e('option',{key:name,value:name},name))),
            item.reuse && e('span',{className:'text-emerald-300'},'already a variable'),
            existingVars.some(v=>v.name===item.name) && !item.reuse && e('span',{className:'text-amber-300'},'name exists — pick another')),
          (item.preview||[]).map((row,j)=>e('p',{key:j,className:'text-xs text-slate-500 break-all'},`#${row.order} ${row.before} → ${row.after}`)) )),
        e('div',{className:'space-x-2'},
          e('button',{onClick:adoptVariables,disabled:busy,className:'bg-emerald-700 px-3 py-2 rounded text-xs disabled:opacity-50'},'CREATE VARIABLES & REWRITE STEPS'),
          e('button',{onClick:()=>setVariablePlan(null),className:'bg-slate-700 px-3 py-2 rounded text-xs'},'CANCEL'))),
      (preview.diagnostics||[]).filter(d=>d.severity!=='note').map((d,i)=>e('p',{key:i,role:'alert',className:'text-xs text-red-300'},`${d.file}:${d.line} — ${d.message}`)),
      (preview.diagnostics||[]).some(d=>d.severity==='note') && e('details',{className:'border border-slate-700 p-2 rounded'},
        e('summary',{className:'text-xs cursor-pointer text-slate-400'},'Import notes (non-blocking)'),
        (preview.diagnostics||[]).filter(d=>d.severity==='note').map((d,i)=>e('p',{key:i,className:'text-xs text-slate-400'},`${d.file}:${d.line} — ${d.message}`))),
      e('div',{className:'max-h-64 overflow-auto space-y-1'},preview.steps.map(s=>e('div',{key:s.order,className:'bg-slate-950 p-2 rounded text-xs break-all'},`#${s.order} ${s.action} · ${s.selector.primary || ''} · ${s.action==='upload_file'?'[fixture]':s.value}`))),
      e('input',{'aria-label':'Imported recording name',value:name,onChange:ev=>setName(ev.target.value),placeholder:'Recording name',className:'w-full bg-slate-800 p-2 rounded text-xs'}),
      e('input',{'aria-label':'Imported test tags',value:tags,onChange:ev=>setTags(ev.target.value),placeholder:'Comma-separated tags',className:'w-full bg-slate-800 p-2 rounded text-xs'}),
      e('details',{className:'border border-slate-700 p-3 rounded'},
        e('summary',{className:'text-xs cursor-pointer'},'Datasets, reusable flows, fixtures & profiles (optional)'),
        e('p',{className:'text-xs text-slate-400 my-2'},'JSON options: dataset rows, variables, setup/teardown steps, named flows, matrix [{browser:"chromium",device:"mobile"}], retries 0 or 1. Secret values use {"env":"TF_SECRET_NAME"}. No parallelism. Extra cases consume work only when you explicitly TEST/RUN.'),
        e('textarea',{'aria-label':'Execution plan options',value:planOptions,disabled:busy || ['queued','running'].includes(draft?.status),onChange:ev=>{setPlanOptions(ev.target.value);setDraft(null);setSaved(null);},rows:6,className:'w-full bg-slate-950 p-2 font-mono text-xs'})),
      e('button',{onClick:testDraft,disabled:busy || !preview.can_save || ['queued','running'].includes(draft?.status),className:'bg-indigo-600 px-4 py-2 rounded text-xs disabled:opacity-50'},'TEST BEFORE PUBLISHING'),
      draft && e('div',{role:'status',className:'text-xs space-y-2'},
        e('p',{},`Draft test: ${draft.status} · ${draft.percent || 0}% · ${saved ? 'recording saved; see publication status above' : 'not in GitHub/library'}`),
        draft.error && e('p',{className:'text-red-300'},draft.error),
        draft.result && e('details',{},e('summary',{},'Test results'),e('pre',{className:'max-h-64 overflow-auto'},JSON.stringify(draft.result,null,2))),
        ['queued','running'].includes(draft.status) && e('button',{className:'key-btn',onClick:async()=>{await API.post('/api/imports/tests/'+draft.id+'/cancel',{});}},'CANCEL DRAFT TEST')),
      e('button',{onClick:save,disabled:busy || draft?.status!=='passed' || !preview.can_save || !name.trim() || !!saved,className:'bg-emerald-700 px-4 py-2 rounded text-xs disabled:opacity-50'},saved?'SAVED':'SAVE TO LIBRARY & PUBLISH'),
      saved && e('button',{onClick:()=>onRun(saved),className:'ml-2 bg-indigo-600 px-4 py-2 rounded text-xs'},'RUN SAVED STEPS')
    )
  );
}

function AccessBoundary() {
  const [status,setStatus]=useState(null), [token,setToken]=useState(''), [error,setError]=useState(''), [busy,setBusy]=useState(false);
  const check=()=>API.get('/api/auth/status').then(setStatus).catch(err=>setError(err.message));
  useEffect(()=>{check();},[]);
  const login=async()=>{setBusy(true);setError('');try{await API.post('/api/auth/login',{token});setToken('');await check();}catch(err){setError(err.message);}finally{setBusy(false);}};
  if(status?.authenticated) return e(App,{access:status});
  return e('div',{className:'max-w-md mx-auto p-6 space-y-4'},
    e('h1',{className:'text-xl font-bold'},'TestForge access'),
    !status ? e('p',{},'Checking access…') : e('div',{className:'space-y-3'},
      e('p',{className:'text-sm'},'Enter the deployment access token. It is sent only to this server and is not saved in localStorage.'),
      e('input',{type:'password',autoComplete:'current-password',value:token,onChange:ev=>setToken(ev.target.value),'aria-label':'Access token',className:'w-full bg-slate-800 p-3 rounded',onKeyDown:ev=>{if(ev.key==='Enter') login();}}),
      e('button',{onClick:login,disabled:busy,className:'bg-indigo-600 px-4 py-2 rounded'},busy?'SIGNING IN…':'SIGN IN')),
    error && e('p',{role:'alert',className:'text-red-300'},error));
}

function orderedRecordingTree(recordings) {
  const ids=new Set(recordings.map(r=>r.id)), children=new Map(), result=[], visited=new Set();
  for(const r of recordings) { const parent=ids.has(r.parent_id)?r.parent_id:null; if(!children.has(parent)) children.set(parent,[]); children.get(parent).push(r); }
  const walk=(r,depth)=>{ if(visited.has(r.id)) return; visited.add(r.id); result.push({...r,tree_depth:Math.min(depth,8)}); for(const child of children.get(r.id)||[]) walk(child,depth+1); };
  for(const r of children.get(null)||[]) walk(r,0);
  for(const r of recordings) if(!visited.has(r.id)) walk(r,0);
  return result;
}

function RecordingMetadata({ recording, recordings, onSaved }) {
  const [name,setName]=useState(recording.name),[tags,setTags]=useState(recording.tags || ''),[parent,setParent]=useState(recording.parent_id || ''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{setName(recording.name);setTags(recording.tags||'');setParent(recording.parent_id||'');},[recording.id]);
  const save=async()=>{setBusy(true);setError('');try{const saved=await API.patch(`/api/recordings/${recording.id}/metadata`,{name,tags,parent_id:parent||null});onSaved(saved);setError(saved.publish_message||'Saved');}catch(err){setError(err.message);}finally{setBusy(false);}};
  return e('div',{className:'bg-slate-900 p-3 rounded space-y-2'},
    e('input',{'aria-label':'Recording name',value:name,onChange:ev=>setName(ev.target.value),className:'w-full bg-slate-800 p-2 rounded text-xs'}),
    e('input',{'aria-label':'Recording tags',value:tags,onChange:ev=>setTags(ev.target.value),placeholder:'Tags, e.g. smoke, checkout',className:'w-full bg-slate-800 p-2 rounded text-xs'}),
    e('label',{className:'block text-xs'},'Parent recording/group',e('select',{value:parent,onChange:ev=>setParent(ev.target.value),className:'w-full bg-slate-800 p-2 rounded'},
      e('option',{value:''},'No parent'),recordings.filter(r=>r.id!==recording.id).map(r=>e('option',{key:r.id,value:r.id},r.name)))),
    e('button',{onClick:save,disabled:busy,className:'bg-indigo-600 p-2 rounded text-xs'},busy?'SAVING…':'SAVE NAME / TAGS / GROUP'),
    error && e('p',{role:'status',className:'text-xs text-amber-300'},error));
}

/* --- Previous recordings: tree view, rescue and reuse ---------------------
   The Record tab lists every recording that already exists, grouped by the
   project it belongs to and nested by parent/child, so a session that was never
   saved (or a recording that fell out of the catalog) can be found, rescued and
   reused instead of being recorded again by hand. */
function recordingGroups(recordings, activeProjectId) {
  const groups = new Map();
  for (const row of recordings || []) {
    const key = row.project_id || 'unknown';
    if (!groups.has(key)) groups.set(key, {
      id: key,
      name: row.project_name || key,
      rows: [],
      active: key === activeProjectId,
    });
    groups.get(key).rows.push(row);
  }
  const list = [...groups.values()];
  for (const group of list) {
    group.rows.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    group.roots = buildRecordingTree(group.rows);
  }
  // Active project first, then alphabetical: the one being recorded is the one
  // the operator is looking for.
  list.sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0) || a.name.localeCompare(b.name));
  return list;
}

function buildRecordingTree(rows) {
  const nodes = new Map(rows.map(row => [row.id, { ...row, children: [] }]));
  const roots = [];
  for (const node of nodes.values()) {
    const parent = node.parent_id ? nodes.get(node.parent_id) : null;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

function RecordingTreeRow({ node, depth, onResume, onRun, busyId, onOpen }) {
  const [open, setOpen] = useState(depth < 1);
  const children = node.children || [];
  return e('div', { className: depth ? 'border-l border-slate-800 ml-2 pl-2' : '' },
    e('div', { className: 'flex items-center gap-2 py-1' },
      children.length
        ? e('button', { type: 'button', className: 'key-btn w-6 shrink-0', 'aria-expanded': open,
            'aria-label': (open ? 'Collapse ' : 'Expand ') + (node.name || node.id),
            onClick: () => setOpen(!open) }, open ? '▾' : '▸')
        : e('span', { className: 'w-6 shrink-0 text-center text-slate-700' }, '•'),
      e('div', { className: 'min-w-0 flex-1' },
        e('div', { className: 'text-xs font-bold text-slate-200 truncate' }, node.name || node.id,
          children.length ? e('span', { className: 'text-[9px] text-slate-500 font-normal' }, ` +${children.length}`) : null),
        e('div', { className: 'text-[9px] text-slate-500 truncate' },
          `${node.step_count || 0} step(s)` +
          (node.tags ? ` · ${node.tags}` : '') +
          (node.created_at ? ` · ${String(node.created_at).slice(0, 16).replace('T', ' ')}` : '')),
        node.unreadable ? e('div', { className: 'text-[9px] text-amber-400' }, 'folder has no readable recording.json') : null),
      e('div', { className: 'flex gap-1 shrink-0' },
        onOpen && e('button', { type: 'button', className: 'key-btn', onClick: () => onOpen(node) }, 'OPEN'),
        onResume && !node.unreadable && e('button', { type: 'button', disabled: busyId === node.id,
          className: 'key-btn ' + (children.length ? '' : 'bg-indigo-900'),
          title: 'Open this recording in the remote browser and append new steps to it',
          onClick: () => onResume(node) }, busyId === node.id ? '…' : 'RESUME'),
        onRun && !node.unreadable && e('button', { type: 'button', disabled: busyId === node.id,
          className: 'key-btn', title: 'Execute this recording in a fresh browser',
          onClick: () => onRun(node) }, 'RUN'))),
    open && children.map(child => e(RecordingTreeRow, {
      key: child.id, node: child, depth: depth + 1, onResume, onRun, busyId, onOpen,
    })));
}

function PreviousRecordings({ project, recordings, onResume, onRun, onOpen, busyId, onRescued }) {
  const [openGroups, setOpenGroups] = useState({});
  const [filter, setFilter] = useState('');
  const [rescue, setRescue] = useState(null);
  const [rescueBusy, setRescueBusy] = useState(false);
  const [rescueNote, setRescueNote] = useState('');
  const [show, setShow] = useState(true);
  const visible = (recordings || []).filter(row => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return true;
    return [row.name, row.tags, row.project_name, row.id].some(value =>
      String(value || '').toLowerCase().includes(needle));
  });
  const groups = recordingGroups(visible, project && project.id);
  const scan = async (apply) => {
    setRescueBusy(true); setRescueNote('');
    try {
      const result = await API.post('/api/library/rescue', { apply: !!apply });
      setRescue(result);
      if (apply) {
        setRescueNote(`Rescued ${result.rescued_count || 0} recording(s) that existed only in the database.` +
          (result.remaining_orphans && result.remaining_orphans.length
            ? ` ${result.remaining_orphans.length} folder(s) are still unreadable.` : '') +
          (result.healthy ? ' Library is healthy.' : ''));
        if (onRescued) onRescued();
      }
    } catch (err) { setRescueNote('Rescue failed: ' + err.message); }
    finally { setRescueBusy(false); }
  };
  return e('details', { className: 'bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-3',
      open: show, onToggle: ev => setShow(ev.target.open) },
    e('summary', { className: 'font-bold text-indigo-300 cursor-pointer text-sm' },
      `📼 Previous recordings · ${recordings.length} in the library`),
    e('p', { className: 'text-[11px] text-slate-400' },
      'Browse, resume or run anything that is already saved — including recordings from other projects. ' +
      'RESUME opens the old recording in the browser and appends what you do next; RUN executes it in a fresh browser.'),
    e('input', { value: filter, onChange: ev => setFilter(ev.target.value),
      'aria-label': 'Filter previous recordings', placeholder: 'Filter by name, tag or project',
      className: 'w-full bg-slate-800 p-2 rounded text-xs' }),
    groups.length === 0 && e('p', { className: 'text-xs text-slate-500' },
      recordings.length ? 'No recording matches that filter.' : 'Nothing saved yet — start a recording below.'),
    groups.map(group => e('div', { key: group.id, className: 'bg-slate-950 rounded-xl border border-slate-800 p-2' },
      e('button', { type: 'button', className: 'w-full flex justify-between items-center text-left',
        'aria-expanded': openGroups[group.id] !== undefined ? !!openGroups[group.id] : !!group.active,
        onClick: () => setOpenGroups(prev => ({ ...prev, [group.id]: !(prev[group.id] !== undefined ? prev[group.id] : group.active) })) },
        e('span', { className: 'text-[11px] font-bold ' + (group.active ? 'text-indigo-300' : 'text-slate-300') },
          (group.active ? '● ' : '') + group.name + (group.active ? ' (current project)' : '')),
        e('span', { className: 'text-[10px] text-slate-500' }, `${group.rows.length} recording(s)`)),
      ((openGroups[group.id] !== undefined) ? openGroups[group.id] : group.active) &&
        e('div', { className: 'mt-1 space-y-0.5' },
          group.roots.map(node => e(RecordingTreeRow, {
            key: node.id, node, depth: 0, onResume, onRun, busyId, onOpen,
          }))) )),
    e('div', { className: 'flex flex-wrap items-center gap-2 border-t border-slate-800 pt-2' },
      e('button', { type: 'button', className: 'key-btn', disabled: rescueBusy,
        onClick: () => scan(false) }, rescueBusy ? 'SCANNING…' : 'SCAN LIBRARY'),
      e('button', { type: 'button', className: 'key-btn bg-emerald-900', disabled: rescueBusy,
        title: 'Re-export recordings that only exist in the database and re-index the catalog',
        onClick: () => scan(true) }, rescueBusy ? 'RESCUING…' : 'RESCUE / RE-INDEX'),
      rescue && e('span', { className: 'text-[10px] ' + (rescue.healthy ? 'text-emerald-300' : 'text-amber-300') },
        `on disk ${rescue.recordings_on_disk} · catalog ${rescue.recordings_in_catalog} · ` +
        `database-only ${(rescue.database_only_recordings || []).length} · orphans ${(rescue.orphan_recordings || []).length} · ` +
        `unreadable ${(rescue.unreadable_recordings || []).length} · live ${(rescue.live_recordings || []).length}`),
      rescueNote && e('span', { role: 'status', className: 'text-[10px] text-emerald-300' }, rescueNote)),
    rescue && (rescue.database_only_recordings || []).length > 0 && e('div', { className: 'text-[10px] text-amber-300 space-y-0.5' },
      e('div', { className: 'font-bold' }, 'Recordings that exist only in the database (rescue exports them to the library):'),
      rescue.database_only_recordings.slice(0, 8).map(row => e('div', { key: row.id },
        `• ${row.name || row.id} · ${row.step_count || 0} step(s) · project ${row.project_id}`))),
    rescue && (rescue.unreadable_recordings || []).length > 0 && e('div', { className: 'text-[10px] text-amber-400' },
      `${rescue.unreadable_recordings.length} folder(s) hold no readable recording.json and cannot be listed.`));
}

function RunExistingRecording({ recId, recordings, onDone }) {
  const [sourceId, setSourceId] = useState('');
  const [record, setRecord] = useState(true);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const options = (recordings || []).filter(row => row.id !== recId);
  const run = async () => {
    if (!sourceId) return;
    setBusy(true); setError(''); setNote('');
    try {
      const result = await API.post(`/api/recordings/${recId}/replay`, { recording_id: sourceId, record });
      setNote(`✅ ${result.message}` + (result.url ? ` Now on ${result.url}` : ''));
      if (onDone) onDone(result);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  return e('details', { className: 'bg-slate-900 border border-slate-800 rounded-xl p-2' },
    e('summary', { className: 'text-xs font-bold text-indigo-300 cursor-pointer' },
      '▶ RUN AN EXISTING RECORDING IN THIS SESSION'),
    e('p', { className: 'text-[10px] text-slate-400 mt-1' },
      'Executes a saved recording in the browser this session already owns, so its steps happen live and ' +
      (record ? 'are added to this recording as editable steps.' : 'are not written into this recording.')),
    e('div', { className: 'flex flex-wrap gap-2 items-center mt-2' },
      e('select', { value: sourceId, disabled: busy, 'aria-label': 'Recording to run inside this session',
        onChange: ev => setSourceId(ev.target.value), className: 'flex-1 min-w-[12rem] bg-slate-800 p-2 rounded text-xs' },
        e('option', { value: '' }, options.length ? 'Choose a saved recording…' : 'No other recording is saved in the library'),
        options.map(row => e('option', { key: row.id, value: row.id },
          `${row.project_name ? row.project_name + ' · ' : ''}${row.name} (${row.step_count || 0} steps)`))),
      e('label', { className: 'text-[10px] text-slate-400 flex items-center gap-1' },
        e('input', { type: 'checkbox', checked: record, disabled: busy, onChange: () => setRecord(!record) }),
        'record its steps'),
      e('button', { type: 'button', className: 'key-btn bg-indigo-800 disabled:opacity-50',
        disabled: busy || !sourceId, onClick: run }, busy ? 'RUNNING…' : 'RUN NOW')),
    busy && e('p', { role: 'status', className: 'text-[10px] text-indigo-300' },
      'Running the saved recording in the live browser — watch the window above.'),
    note && e('p', { role: 'status', className: 'text-[10px] text-emerald-300' }, note),
    error && e('p', { role: 'alert', className: 'text-[10px] text-red-300' }, error));
}
