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
  useEffect(()=>{API.get('/api/imports/packs').then(setPacks).catch(()=>{});},[]);
  const generation = useRef(0);
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
  useEffect(() => { generation.current++; setPreview(null); setSaved(null); setFiles([]); setNotice(''); setError(''); setDraft(null);setPlanOptions('{}'); }, [project.id]);
  const readBase64 = file => new Promise((resolve,reject) => {
    const reader=new FileReader(); reader.onerror=()=>reject(new Error('Unable to read '+file.name));
    reader.onload=()=>resolve(String(reader.result).split(',')[1]); reader.readAsDataURL(file);
  });
  const inspect = async () => {
    setBusy(true); setError(''); setPreview(null); setSaved(null); setNotice('');setDraft(null);
    const version=generation.current;
    try {
      if(!files.length || files.length>8 || files.reduce((n,f)=>n+f.size,0)>1048576) throw new Error('Choose 1–8 files totalling at most 1 MiB. One test per recording; Jenkins bundles include the referenced script.');
      const result=files.length===1
        ? await apiRequest('/api/imports/preview?filename='+encodeURIComponent(files[0].name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:files[0]})
        : await API.post('/api/imports/preview-bundle',{files:await Promise.all(files.map(async f=>({name:f.name,base64:await readBase64(f)})))});
      if(version!==generation.current) return;
      setPreview(result); setPlanOptions(JSON.stringify(result.execution_plan || {},null,2)); setName(result.name); setNotice(result.notice || 'Preview only: no browser launched, no source code executed, nothing saved or published.');
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
      onSaved(result);
    } catch(err) {if(version===generation.current) setError(err.message);}
    finally {setBusy(false);}
  };
  return e('details',{className:'border border-indigo-800 bg-slate-900 p-4 rounded-xl space-y-3'},
    e('summary',{className:'font-bold text-indigo-300 cursor-pointer'},'UPLOAD TEST SCRIPTS'),
    e('p',{className:'text-xs text-slate-300'},'CSV/TSV, Excel (.xlsx), JSON, supported Playwright Python/JS/TS, BDD/Cucumber (.feature), or a restricted Jenkinsfile + its test script. Preview reports unsupported syntax; arbitrary code, shell commands, macros and custom step definitions never run.'),
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
    e('a',{href:'/api/imports/template.csv',className:'text-indigo-300 underline text-xs'},'Download CSV template'),
    e('input',{key:project.id,type:'file',multiple:true,accept:'.csv,.tsv,.xlsx,.json,.py,.js,.ts,.feature,.bdd,.jenkins,.groovy,Jenkinsfile','aria-label':'Upload test scripts',disabled:busy || ['queued','running'].includes(draft?.status),onChange:ev=>{setFiles(Array.from(ev.target.files || []));setPreview(null);setSaved(null);setDraft(null);},className:'block w-full text-xs'}),
    e('button',{disabled:busy || !files.length || ['queued','running'].includes(draft?.status),onClick:inspect,className:'bg-indigo-600 px-4 py-2 rounded text-xs disabled:opacity-50'},busy?'WORKING…':'VALIDATE & PREVIEW'),
    error && e('p',{role:'alert',className:'text-red-300 text-xs'},error),
    notice && e('p',{role:'status',className:'text-emerald-300 text-xs'},notice),
    preview && e('div',{className:'space-y-2'},
      e('p',{className:'text-sm font-bold'},`${preview.steps.length} supported steps · ${preview.can_save?'ready to save':'corrections required'} · sequential execution`),
      preview.diagnostics.map((d,i)=>e('p',{key:i,role:'alert',className:'text-xs text-red-300'},`${d.file}:${d.line} — ${d.message}`)),
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
