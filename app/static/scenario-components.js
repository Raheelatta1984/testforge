/* Repository scenarios, explicit-save secrets and execution inputs. No extra services. */
const fieldClass = 'w-full bg-slate-950 border border-slate-700 rounded p-2 text-xs';
function InputFields({definitions=[], defaults={}, values={}, onChange}) {
  const names = [...new Set([...definitions.map(v=>v.name), ...Object.keys(defaults), ...Object.keys(values)])];
  return e('div', {className:'space-y-2'}, names.length===0 && e('p',{className:'text-xs text-slate-400'},'No inputs defined. Add project variables or parameters in the Variables tab.'),
    names.map(name=>{const definition=definitions.find(v=>v.name===name)||{}; const secret=definition.is_secret || defaults[name]==='[encrypted]';
      const value=Object.hasOwn(values,name)?values[name]:(Object.hasOwn(defaults,name)?defaults[name]:definition.value||'');
      return e('label',{key:name,className:'block text-xs text-slate-400'},`${name} · ${definition.category==='Parameter'?'parameter':'variable'}${secret?' · encrypted':''}${definition.missing?' · not saved in project yet':''}`,
        e('input',{className:fieldClass,'aria-label':name,type:secret?'password':'text',autoComplete:'off',value:secret && (!Object.hasOwn(values,name)||value==='[encrypted]')?'':value,
          placeholder:secret?'Stored encrypted value is retained unless replaced':'Value',onChange:ev=>{const next={...values}; if(secret && !ev.target.value) delete next[name]; else next[name]=ev.target.value; onChange(next);}}));}));
}

// The application provisions its own key when the environment has none, so this
// panel is normally informational. It still has to be candid about what that key
// does and does not protect, because a key stored beside its own ciphertext must
// never be presented as if it were an external secret.
function EncryptionNotice({status,onRecheck,onRotate,busy}) {
  const [copied,setCopied]=useState('');
  const [open,setOpen]=useState(false);
  if(!status) return null;
  const variable=status.variable||'TF_VARIABLE_ENCRYPTION_KEYS';
  const command=status.generate_command||'';
  const copy=async()=>{
    try{await navigator.clipboard.writeText(command);setCopied('Command copied.');}
    catch(_){setCopied('Clipboard blocked by the browser; select the text instead.');}
  };

  if(!status.ready) {
    return e('div',{role:'alert',className:'border border-amber-500/60 bg-amber-950/40 rounded-xl p-4 space-y-2'},
      e('p',{className:'text-xs font-bold text-amber-300'},'⚠ Encryption is unavailable on this server'),
      e('p',{className:'text-xs text-amber-100'},'Plaintext variables still save normally.'),
      status.reason && e('p',{className:'text-[11px] text-amber-200 break-words'},status.reason),
      command && e('div',{className:'flex items-center gap-2'},
        e('code',{className:'flex-1 block bg-slate-950 border border-slate-700 rounded p-2 text-[10px] break-all'},command),
        e('button',{type:'button',className:'key-btn shrink-0',onClick:copy},'COPY')),
      e('button',{type:'button',className:'key-btn',disabled:busy,onClick:onRecheck},busy?'CHECKING…':'RE-CHECK ENCRYPTION'),
      copied && e('p',{role:'status',className:'text-[10px] text-amber-300'},copied));
  }

  const managed = status.source === 'app-managed';
  return e('details',{open,onToggle:ev=>setOpen(ev.target.open),
      className:`border rounded-xl p-4 space-y-2 ${managed?'border-emerald-700/60 bg-emerald-950/20':'border-slate-700 bg-slate-900'}`},
    e('summary',{className:'text-xs font-bold cursor-pointer '+(managed?'text-emerald-300':'text-indigo-300')},
      managed
        ? `🔒 Encryption ready · key managed by the application (${status.keys} in the keyring)`
        : `🔒 Encryption ready · key supplied by the environment (${status.keys} in the keyring)`),
    managed && e('p',{className:'text-xs text-slate-300'},
      'Nothing to configure. The application generated the key itself on first use and keeps it in its own database, so encrypted values survive restarts and redeploys.'),
    status.note && e('p',{className:'text-[11px] text-amber-200/90'},status.note),
    !managed && e('p',{className:'text-xs text-slate-300'},
      `${variable} is set, so new values are sealed with its first key. Any key the application created earlier is kept in the ring so existing values still decrypt. Rotate by putting the new key first: ${variable}=new,old`),
    e('p',{className:'text-[11px] text-slate-400'},
      'Values are decrypted only while a step needs them and re-sealed immediately; nothing is ever written back in plaintext.'),
    e('div',{className:'flex flex-wrap gap-2 pt-1'},
      e('button',{type:'button',className:'key-btn',disabled:busy,onClick:onRecheck},busy?'CHECKING…':'RE-CHECK'),
      managed && e('button',{type:'button',className:'key-btn',disabled:busy,onClick:onRotate},
        busy?'WORKING…':'ROTATE KEY & RE-SEAL'),
      !managed && command && e('button',{type:'button',className:'key-btn',onClick:copy},'COPY KEY-GENERATION COMMAND')),
    managed && e('p',{className:'text-[10px] text-slate-500'},
      'Rotation mints a new key, decrypts each stored value with the ring that can still read it and re-encrypts it immediately. Superseded keys are kept so queued runs and older snapshots stay readable.'),
    copied && e('p',{role:'status',className:'text-[10px] text-emerald-300'},copied));
}

function SecureVariables({project}) {
  const [rows,setRows]=useState([]), [edited,setEdited]=useState({}), [name,setName]=useState(''), [value,setValue]=useState('');
  const [secret,setSecret]=useState(false), [category,setCategory]=useState('General'), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false);
  const [encryption,setEncryption]=useState(null), [checking,setChecking]=useState(false);
  const load=async()=>{if(project) setRows(await API.get(`/api/variables?project_id=${project.id}`));};
  // One cheap read of the readiness the server already computes, so the tab can
  // answer "can I tick Encrypt?" before a password is typed into a doomed field.
  const checkEncryption=async()=>{
    setChecking(true);
    try{setEncryption(await API.get('/api/encryption'));}
    catch(_){/* a failed probe must not block plaintext work */}
    finally{setChecking(false);}
  };
  const rotate=async()=>{
    if(!confirm('Mint a new encryption key and re-seal every stored value with it?\n\nSuperseded keys are kept, so nothing becomes unreadable. This publishes the re-encrypted library.')) return;
    setChecking(true);
    try{
      const result=await API.post('/api/encryption/rotate',{});
      setNotice(`Rotated. Re-sealed ${result.variables} variable(s), ${result.recording_defaults} recording default(s), ${result.scenario_entries} scenario input(s)`
        +(result.unreadable?`; ${result.unreadable} value(s) could not be opened and were left untouched`:'')
        +`. ${result.publish_message||''}`);
      await load();
    }catch(err){setNotice(err.message);}
    finally{setChecking(false);await checkEncryption();}
  };
  useEffect(()=>{checkEncryption();},[]);
  useEffect(()=>{setEdited({}); load().catch(err=>setNotice(err.message));},[project]);
  // Status can land after the name field already auto-ticked the box.
  useEffect(()=>{if(encryption && !encryption.ready) setSecret(false);},[encryption]);
  if(!project) return e('p',{},'SELECT PROJECT FIRST');
  const sealingReady = !encryption || encryption.ready;   // unknown stays permissive
  const sealingBlocked = !!encryption && !encryption.ready;
  const work=async(fn)=>{setBusy(true);try{const result=await fn();setNotice(result?.publish_message||'Saved. Persistent values are retained for subsequent executions.');setEdited({});await load();}catch(err){setNotice(err.message);if(/encryption|keyring|TF_VARIABLE_ENCRYPTION_KEYS/i.test(err.message||'')) checkEncryption();}finally{setBusy(false);}};
  return e('div',{className:'space-y-4'},
    e(GuideSection,{id:'var-head',label:'Variables & parameters',
      action:'Anything typed more than once belongs here as {{name}} instead of a literal.'},
    e('div',{className:'space-y-4'},
    e('h2',{className:'font-bold text-indigo-300'},`${project.name} · Variables & parameters`),
    e('p',{className:'text-xs text-slate-400'},'Use {{name}} in steps. Values are saved only when you press Save. Encrypted values stay masked; the server needs a stable TF_VARIABLE_ENCRYPTION_KEYS key outside Git. Plaintext exists briefly in memory and in the target app during use.'),
    e(EncryptionNotice,{status:encryption,onRecheck:checkEncryption,onRotate:rotate,busy:checking})),
    e(GuideSection,{id:'var-add',label:'Add a variable or parameter',
      action:'Name it, paste the value, tick ENCRYPT AT REST for a password or token, then SAVE INPUT.'},
    e('form',{className:'bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2',onSubmit:ev=>{ev.preventDefault();work(async()=>{const result=await API.post('/api/variables',{project_id:project.id,name,value,is_secret:secret&&sealingReady,category});setName('');setValue('');return result;});}},
      e('input',{className:fieldClass,required:true,'aria-label':'Variable name',placeholder:'Name, e.g. password or account',value:name,onChange:ev=>{setName(ev.target.value);setSecret(sealingReady && /password|secret|token/i.test(ev.target.value));}}),
      e('input',{className:fieldClass,'aria-label':'Variable value',type:secret?'password':'text',autoComplete:'off',placeholder:'Saved value',value,onChange:ev=>setValue(ev.target.value)}),
      e('select',{className:fieldClass,'aria-label':'Input category',value:category,onChange:ev=>setCategory(ev.target.value)},e('option',{value:'General'},'Variable'),e('option',{value:'Parameter'},'Parameter')),
      e('label',{className:`text-xs flex gap-2 ${sealingBlocked?'text-slate-500':''}`,title:sealingBlocked?`Unavailable until ${encryption.variable} is configured on this server`:''},
        e('input',{type:'checkbox',checked:secret,disabled:sealingBlocked,onChange:ev=>setSecret(ev.target.checked)}),
        sealingBlocked?'Encrypt at rest — unavailable until the server keyring is configured (see above)':'Encrypt at rest (password / token)'),
      e('button',{className:'key-btn',disabled:busy},'SAVE INPUT'))),
    e(GuideSection,{id:'var-rows',label:'Saved inputs',
      action:'Edit a value and SAVE CHANGES, or DELETE it. Encrypted values stay masked until you replace them.'},
    e('div',{className:'space-y-4'},
    notice && e('p',{role:'status',className:'text-xs text-amber-300'},notice),
    rows.map(row=>{const changes=edited[row.id]||{}; const sealed=row.is_secret||changes.is_secret; return e('div',{key:row.id,className:'bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2'},
      e('strong',{className:'text-xs'},`{{${row.name}}} · ${row.category}${sealed?' · encrypted':''}`),
      e('input',{className:fieldClass,'aria-label':`Saved ${row.name}`,type:sealed?'password':'text',autoComplete:'off',value:Object.hasOwn(changes,'value')?changes.value:(row.is_secret?'':row.value),placeholder:sealed?'Stored value hidden; leave unchanged to retain':'Saved value',onChange:ev=>setEdited({...edited,[row.id]:{...changes,value:ev.target.value}})}),
      !row.is_secret && e('label',{className:`text-xs flex gap-2 ${sealingBlocked?'text-slate-500':''}`,title:sealingBlocked?`Unavailable until ${encryption.variable} is configured on this server`:''},
        e('input',{type:'checkbox',checked:!!changes.is_secret,disabled:sealingBlocked,onChange:ev=>setEdited({...edited,[row.id]:{...changes,is_secret:ev.target.checked}})}),
        sealingBlocked?'Encrypt this saved value — unavailable until the server keyring is configured':'Encrypt this saved value'),
      row.is_secret && sealingBlocked && e('p',{className:'text-[10px] text-amber-400'},'This value was sealed with a keyring this server cannot load. Restore the original key to read or replace it.'),
      e('button',{className:'key-btn',disabled:busy||!Object.keys(changes).length,onClick:()=>work(()=>API.patch(`/api/variables/${row.id}`,changes))},'SAVE CHANGES'),
      e('button',{className:'key-btn',disabled:busy,onClick:()=>{if(confirm(`Delete ${row.name}?`)) work(()=>API.del(`/api/variables/${row.id}`));}},'DELETE'));})))));
}

function ExecutionInputs({recording,onClose,onRun}) {
  // The dialog opens at once with a loading line; the inputs arrive a moment later.
  // `config` is re-read every time the dialog opens and on REFRESH, so a variable
  // saved from an AI recording a moment ago is listed without reloading the page.
  const [config,setConfig]=useState(null), [changes,setChanges]=useState({}), [note,setNote]=useState(''), [busy,setBusy]=useState(''), [loading,setLoading]=useState(true);
  const load=()=>{setLoading(true);return API.get(`/api/recordings/${recording.id}/inputs`).then(data=>{setConfig(data);setNote('');}).catch(err=>setNote(err.message)).finally(()=>setLoading(false));};
  useEffect(()=>{setChanges({});load();},[recording.id]);
  const work=async(label,fn)=>{setBusy(label);try{await fn();}catch(err){setNote(err.message);}finally{setBusy('');}};
  const used=new Set((config&&config.used_names)||[]);
  // A name the recording uses but the project does not hold yet still gets an
  // input, so nothing typed in an AI recording is silently unfillable.
  const missing=(config&&config.missing_names)||[];
  const definitions=config ? [...config.project_variables, ...missing.map(name=>({name,category:'General',value:'',missing:true}))] : [];
  const usedDefs=definitions.filter(v=>used.has(v.name));
  const otherDefs=definitions.filter(v=>!used.has(v.name));
  // Each group sees only its own names, so a value typed for a used variable is
  // never shown twice. Stored defaults that match no definition go to "other".
  const groupOf=(names,source)=>Object.fromEntries(Object.entries(source||{}).filter(([k])=>names(k)));
  const isUsed=k=>used.has(k);
  const isOther=k=>!used.has(k);
  const changeGroup=(names)=>next=>setChanges(prev=>{const out={...prev}; for(const k of Object.keys(prev)) if(names(k)) delete out[k]; return {...out,...next};});
  const otherExtra=config ? Object.keys(config.defaults||{}).filter(k=>!used.has(k) && !otherDefs.some(v=>v.name===k)) : [];
  return e('div',{role:'dialog','aria-label':'Execution inputs',className:'fixed inset-0 z-[110] overflow-y-auto bg-slate-950 p-6 space-y-4'},
    e('div',{className:'flex flex-wrap justify-between items-center gap-2'},
      e('h2',{className:'font-bold text-indigo-300'},`Inputs · ${recording.name}`),
      e('button',{className:'key-btn',disabled:loading||!!busy,onClick:()=>load()},loading?'LOADING…':'REFRESH')),
    e('p',{className:'text-xs text-slate-400'},'Run once uses these edits without changing persistent project values. Save recording defaults to reuse edits for this recording. These inputs also apply to imported/AI-authored recordings; this does not add an autonomous AI agent.'),
    loading && !config && e('p',{role:'status',className:'text-xs text-slate-400'},'Loading inputs…'),
    config && usedDefs.length>0 && e('div',{className:'space-y-2 border border-indigo-800/60 rounded-xl p-3'},
      e('p',{className:'text-xs font-bold text-indigo-300'},`Used by this recording (${usedDefs.length})`),
      e(InputFields,{definitions:usedDefs,defaults:groupOf(isUsed,config.defaults),values:groupOf(isUsed,changes),onChange:changeGroup(isUsed)})),
    config && (otherDefs.length>0 || otherExtra.length>0) && e('div',{className:'space-y-2 border border-slate-800 rounded-xl p-3'},
      e('p',{className:'text-xs font-bold text-slate-300'},`Other variables and parameters in this project (${otherDefs.length+otherExtra.length})`),
      e(InputFields,{definitions:otherDefs,defaults:groupOf(isOther,config.defaults),values:groupOf(isOther,changes),onChange:changeGroup(isOther)})),
    config && usedDefs.length===0 && otherDefs.length===0 && Object.keys(config.defaults||{}).length===0 &&
      e('p',{className:'text-xs text-slate-400'},'No inputs defined. Add project variables or parameters in the Variables tab.'),
    missing.length>0 && e('p',{role:'status',className:'text-[11px] text-amber-300'},`This recording uses ${missing.map(n=>'{{'+n+'}}').join(', ')} but the project has no variable with that name yet. Type a value here, or add it in the Variables tab and press REFRESH.`),
    e('div',{className:'flex flex-wrap gap-2'},
      e('button',{className:'key-btn',disabled:!!busy||!config,onClick:()=>work('run',async()=>{await onRun(recording,changes);onClose();})},busy==='run'?'STARTING…':'RUN ONCE'),
      e('button',{className:'key-btn',disabled:!!busy||!config,onClick:()=>work('save',async()=>{const result=await API.put(`/api/recordings/${recording.id}/inputs`,{variables:{...config.defaults,...changes}});setConfig({...config,defaults:result.defaults});setChanges({});setNote(result.publish_message||'Recording defaults saved.');})},busy==='save'?'SAVING…':'SAVE RECORDING DEFAULTS'),
      e('button',{className:'key-btn',onClick:onClose},'CLOSE')),
    note && e('p',{role:'alert',className:'text-xs text-amber-300'},note));
}

function ReportLinks({kind,id}) {
  const [notice,setNotice]=useState('');
  const [repo,setRepo]=useState(null);
  const url=kind==='batch'?`/api/runs/batch/${id}/report.pdf`:`/api/runs/${id}/report.pdf`;
  // The committed report is the durable copy: it is part of the library push and
  // does not expire with the PDF. Look it up so the row can link straight to it.
  useEffect(()=>{API.get(`/api/reports/${kind}/${id}`).then(setRepo).catch(()=>{});},[kind,id]);
  return e('div',{className:'flex flex-wrap gap-2 text-xs',onClick:ev=>ev.stopPropagation()},
    e('a',{href:url,target:'_blank',rel:'noopener noreferrer',className:'text-indigo-300 underline'},'VIEW PDF'),
    e('a',{href:url+'?download=true',className:'text-indigo-300 underline'},'DOWNLOAD PDF'),
    repo && repo.repository_url && e('a',{href:repo.repository_url,target:'_blank',rel:'noopener noreferrer',
      className:'text-emerald-300 underline',title:repo.repository_path},'REPORT IN REPOSITORY'),
    repo && !repo.repository_url && repo.repository_path && e('span',{className:'text-slate-500',
      title:'Set TF_GITHUB_REPO/TF_GITHUB_BRANCH, or mount a checkout, to link straight to GitHub'},
      `committed to ${repo.repository_path}`),
    e('button',{className:'text-indigo-300 underline',onClick:async()=>{try{const meta=await API.get(`/api/reports/${kind}/${id}`); if(meta.remote_status!=='published'||Date.now()/1000>=meta.expires_at) throw new Error('GitHub release attachment is not available (TF_REPORTS_GITHUB may be off, pending or expired). The report itself is still committed to the repository; download the PDF to share the evidence.');await navigator.clipboard.writeText(meta.url);setNotice('GitHub link copied. Expires 48h after completion; private repositories require access.');}catch(err){setNotice(err.message);}}},'COPY PDF SHARE LINK'),
    notice && e('span',{role:'status',className:'text-amber-300'},notice));
}

function LinkedInputs({entry,projectVariables,onChange}) {
  const [config,setConfig]=useState(null);
  useEffect(()=>{setConfig(null);if(entry.recording_id) API.get(`/api/recordings/${entry.recording_id}/inputs`).then(setConfig).catch(()=>{});},[entry.recording_id]);
  if(!config) return null;
  const definitions=[...config.project_variables.filter(v=>!projectVariables.some(p=>p.name===v.name)),...projectVariables];
  return e(InputFields,{definitions,defaults:config.defaults,values:entry.variables||{},onChange:values=>onChange({...entry,variables:values})});
}

function ScenarioPlans({project}) {
  const [scenarios,setScenarios]=useState([]), [recordings,setRecordings]=useState([]), [variables,setVariables]=useState([]), [draft,setDraft]=useState(null);
  const [execution,setExecution]=useState(null), [overrides,setOverrides]=useState({}), [batch,setBatch]=useState(null), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false);
  const endpoint=`/api/projects/${project.id}/scenarios`;
  const load=()=>API.get(endpoint).then(setScenarios);
  useEffect(()=>{setDraft(null);setExecution(null);setBatch(null);load().catch(err=>setNotice(err.message));API.get('/api/library/recordings').then(setRecordings);API.get(`/api/variables?project_id=${project.id}`).then(setVariables);},[project.id]);
  useEffect(()=>{if(!batch||!['queued','running'].includes(batch.status))return;const timer=setTimeout(()=>API.get(`/api/runs/batch/${batch.id}`).then(setBatch).catch(err=>setNotice(err.message)),1500);return()=>clearTimeout(timer);},[batch]);
  const work=async(fn)=>{setBusy(true);try{await fn();}catch(err){setNotice(err.message);}finally{setBusy(false);}};
  const updatePlan=(index,plan)=>setDraft({...draft,plans:draft.plans.map((p,i)=>i===index?plan:p)});
  const save=()=>work(async()=>{const result=draft.id?await API.put(endpoint+'/'+draft.id,draft):await API.post(endpoint,draft);setDraft(null);setNotice(result.publish_message||'Scenario saved.');await load();});
  return e('section',{className:'bg-slate-900 rounded-xl border border-slate-700 p-4 space-y-3'},
    e('h2',{className:'font-bold text-indigo-300'},`${project.name} / High-level scenarios / Execution plans`),
    e('p',{className:'text-xs text-slate-400'},'Link existing recordings in exact order. Login and logout share one browser session. A failed recording stops the plan; following recordings are skipped. No automatic retry or adaptive reordering.'),
    e('button',{className:'key-btn',onClick:()=>setDraft({name:'',plans:[{name:'Execution plan',entries:[]}]})},'NEW HIGH-LEVEL SCENARIO'),
    scenarios.map(scenario=>e('div',{key:scenario.id,className:'bg-slate-950 rounded p-3 space-y-2'},
      e('strong',{className:'text-sm'},scenario.name), e('button',{className:'key-btn',onClick:()=>setDraft(JSON.parse(JSON.stringify(scenario)))},'EDIT SCENARIO'),
      scenario.plans.map(plan=>e('div',{key:plan.id,className:'space-y-2'},e('p',{className:'text-xs text-indigo-300'},plan.name),
        e('ol',{className:'text-xs text-slate-400'},plan.entries.map((entry,index)=>e('li',{key:entry.id||index},`${index+1}. ${entry.name}`))),
        e('button',{className:'key-btn',disabled:busy,onClick:()=>{setOverrides({});setExecution({scenario,plan});}},'REVIEW INPUTS & RUN'))))),
    draft && e('div',{className:'space-y-3 border border-indigo-500 rounded p-3'},
      e('input',{className:fieldClass,'aria-label':'Scenario name',placeholder:'Validate Application login & logout',value:draft.name,onChange:ev=>setDraft({...draft,name:ev.target.value})}),
      draft.plans.map((plan,pi)=>e('div',{key:pi,className:'space-y-2 bg-slate-950 p-3 rounded'},
        e('input',{className:fieldClass,'aria-label':`Plan ${pi+1} name`,value:plan.name,onChange:ev=>updatePlan(pi,{...plan,name:ev.target.value})}),
        plan.entries.map((entry,index)=>e('div',{key:entry.id||index,className:'border border-slate-700 rounded p-3 space-y-2'},
          e('p',{className:'text-xs'},`Recording ${index+1}`),
          e('select',{className:fieldClass,'aria-label':`Recording ${pi+1}.${index+1}`,value:entry.recording_id,onChange:ev=>updatePlan(pi,{...plan,entries:plan.entries.map((v,i)=>i===index?{recording_id:ev.target.value,variables:{}}:v)})},
            e('option',{value:''},'Choose a repository recording'),recordings.map(r=>e('option',{key:r.id,value:r.id},`${r.project_name||r.project_id} / ${r.name}`))),
          e(LinkedInputs,{entry,projectVariables:variables,onChange:changed=>updatePlan(pi,{...plan,entries:plan.entries.map((v,i)=>i===index?changed:v)})}),
          e('div',{className:'flex gap-2'},['UP','DOWN','REMOVE'].map((label,n)=>e('button',{key:label,className:'key-btn',disabled:n===0&&index===0||n===1&&index===plan.entries.length-1,onClick:()=>{const entries=[...plan.entries];if(n===2)entries.splice(index,1);else{const to=index+(n===0?-1:1);[entries[index],entries[to]]=[entries[to],entries[index]];}updatePlan(pi,{...plan,entries});}},label))))),
        e('button',{className:'key-btn',onClick:()=>updatePlan(pi,{...plan,entries:[...plan.entries,{recording_id:'',variables:{}}]})},'LINK RECORDING'),
        draft.plans.length>1 && e('button',{className:'key-btn',onClick:()=>setDraft({...draft,plans:draft.plans.filter((_,i)=>i!==pi)})},'REMOVE PLAN'))),
      e('button',{className:'key-btn',onClick:()=>setDraft({...draft,plans:[...draft.plans,{name:'Execution plan',entries:[]}]})},'ADD PLAN'),
      e('button',{className:'key-btn',disabled:busy,onClick:save},'SAVE SCENARIO'),e('button',{className:'key-btn',onClick:()=>setDraft(null)},'CANCEL EDIT')),
    execution && e('div',{role:'dialog','aria-label':'Plan execution inputs',className:'space-y-3 border border-indigo-500 rounded p-3'},
      e('h3',{className:'text-sm font-bold'},`${execution.scenario.name} / ${execution.plan.name}`),
      e('p',{className:'text-xs text-slate-400'},'Only edits are sent as one-off overrides. Project and plan defaults remain unchanged.'),
      execution.plan.entries.map((entry,index)=>e('div',{key:entry.id||index,className:'space-y-2'},e('p',{className:'text-xs'},`${index+1}. ${entry.name}`),
        e(LinkedInputs,{entry:{...entry,variables:{...entry.variables,...(overrides[index]||{})}},projectVariables:variables,onChange:changed=>{const values={};for(const [name,value] of Object.entries(changed.variables))if(value!=='[encrypted]' && value!==entry.variables[name])values[name]=value;setOverrides({...overrides,[index]:values});}}))),
      e('button',{className:'key-btn',disabled:busy,onClick:()=>work(async()=>{const result=await API.post(`${endpoint}/${execution.scenario.id}/plans/${execution.plan.id}/run`,{entry_overrides:overrides});setBatch({id:result.batch_id,status:result.status});setExecution(null);setNotice('Ordered plan queued.');})},'EXECUTE ORDERED PLAN'),
      e('button',{className:'key-btn',onClick:()=>setExecution(null)},'CLOSE INPUTS')),
    batch && e('div',{className:'space-y-2'},e('p',{role:'status',className:'text-xs'},`Plan execution: ${batch.status} · ${batch.progress_pct||0}% · ${batch.passed||0} passed / ${batch.total||0}`),
      ['queued','running'].includes(batch.status)?e('button',{className:'key-btn',onClick:()=>work(()=>API.post(`/api/runs/batch/${batch.id}/cancel`,{}))},'CANCEL EXECUTION'):e(ReportLinks,{kind:'batch',id:batch.id})),
    notice && e('p',{role:'status',className:'text-xs text-amber-300'},notice));
}

/* The Execution-plan tab sits next to AI recording: pick recordings in order and
   the inputs they need appear from one cached catalog read, not one fetch per
   recording. Linking a recording never touches the network. */
function ExecutionPlan({project, onRun}) {
  const [catalog,setCatalog]=useState(null), [catalogMs,setCatalogMs]=useState(null), [error,setError]=useState('');
  const [plan,setPlan]=useState([]), [activeId,setActiveId]=useState(''), [search,setSearch]=useState('');
  const [values,setValues]=useState({}), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false), [batch,setBatch]=useState(null);
  const loadCatalog=async()=>{
    const started=performance.now();
    try { const data=await API.get('/api/plans/catalog'); setCatalog(data); setCatalogMs(Math.round(performance.now()-started)); setError(''); }
    catch(err) { setError(err.message); }
  };
  useEffect(()=>{ setPlan([]); setActiveId(''); setValues({}); setBatch(null); setNotice(''); loadCatalog(); },[project && project.id]);
  useEffect(()=>{ if(!batch || !['queued','running'].includes(batch.status)) return; const timer=setTimeout(()=>API.get(`/api/runs/batch/${batch.id}`).then(setBatch).catch(err=>setError(err.message)),1500); return ()=>clearTimeout(timer); },[batch]);
  const work=async(fn)=>{ setBusy(true); try { await fn(); } catch(err) { setError(err.message); } finally { setBusy(false); } };
  if(!project) return e('section',{className:'text-center text-slate-500 font-bold py-20'},'SELECT A PROJECT, THEN BUILD AN EXECUTION PLAN');

  const recordings=(catalog && catalog.recordings) || [];
  const needle=search.trim().toLowerCase();
  const matches=needle ? recordings.filter(r=>`${r.project_name||''} ${r.name||''} ${r.tags||''}`.toLowerCase().includes(needle)) : recordings;
  const active=recordings.find(r=>r.id===activeId) || null;
  const defaults=(catalog && catalog.recording_defaults && activeId && catalog.recording_defaults[activeId]) || {};
  // Project parameters plus whatever this recording needs, resolved locally.
  const projectVars=((catalog && catalog.projects && project && catalog.projects[project.id]) || {}).variables || [];
  const activeProjectId=active && active.project_id;
  // The recording's own project first: those are the variables it will resolve.
  const activeProjectVars=((catalog && catalog.projects && activeProjectId && catalog.projects[activeProjectId]) || {}).variables || [];
  const definitions=[...activeProjectVars, ...projectVars];
  const seen=new Set(), uniqueDefs=definitions.filter(v=>{ if(seen.has(v.name)) return false; seen.add(v.name); return true; });
  const inputsReady=Object.keys(values).length>0;
  const link=(recording)=>{
    if(!recording) return;
    setPlan(prev=>prev.some(entry=>entry.recording_id===recording.id) ? prev : [...prev,{recording_id:recording.id,name:recording.name,project_name:recording.project_name}]);
    setActiveId(recording.id); setValues({}); setBatch(null);
    setNotice(`Linked ${recording.name}. Inputs appeared instantly from the cached catalog — no extra request.`);
  };
  const move=(index,to)=>{ const next=[...plan]; const [entry]=next.splice(index,1); next.splice(to,0,entry); setPlan(next); };
  const activeDefaults=defaults;

  return e('section',{className:'space-y-4'},
    e(GuideSection,{id:'plan-head',label:'Execution plan · run recordings in order',
      action:'Link recordings in the order they must run, then queue the whole thing as one batch.'},
    e('div',{className:'bg-slate-900 border border-slate-700 rounded-xl p-4 space-y-2'},
      e('h2',{className:'font-bold text-indigo-300'},'Execution plan'),
      e('p',{className:'text-xs text-slate-400'},'Build an ordered run: pick repository recordings in the order they must execute, review the variables/parameters each one needs, then queue one batch. Login and logout share the browser session; a failed recording stops the plan. Selecting a recording costs no request — the whole catalog is one cached read.'),
      e('div',{className:'flex flex-wrap items-center gap-2 text-xs'},
        e('input',{className:'flex-1 min-w-[12rem] bg-slate-800 p-2 rounded','aria-label':'Search recordings',placeholder:'Search recordings by name, project or tag…',value:search,onChange:ev=>setSearch(ev.target.value)}),
        e('button',{className:'key-btn',onClick:loadCatalog},'REFRESH CATALOG'),
        catalog && e('span',{className:'text-slate-500'},`${recordings.length} recordings · catalog in ${catalogMs} ms · cached ${catalog.cache_seconds}s on the server`)),
      error && e('p',{role:'alert',className:'text-xs text-red-300'},error)))),
    e(GuideSection,{id:'plan-build',label:'1 Link · 2 Order · run',
      action:'Pick recordings on the left, reorder with UP/DOWN, then RUN ORDERED PLAN.'},
    e('div',{className:'grid gap-4 md:grid-cols-2'},
      e('div',{className:'bg-slate-900 border border-slate-700 rounded-xl p-3 space-y-2'},
        e('h3',{className:'text-sm font-bold text-indigo-300'},'1 · Link a recording'),
        e('div',{className:'max-h-72 overflow-auto space-y-1'},
          !catalog && e('p',{className:'text-xs text-slate-400'},'Loading the catalog…'),
          catalog && matches.length===0 && e('p',{className:'text-xs text-slate-400'},'No recording matches that search.'),
          matches.slice(0,200).map(recording=>e('div',{key:recording.id,className:'flex items-center gap-2 bg-slate-950 p-2 rounded text-xs'},
            e('button',{className:'flex-1 text-left',onClick:()=>link(recording),'aria-label':`Link ${recording.name}`},
              e('span',{className:'font-bold'},recording.name),
              e('span',{className:'block text-slate-500'},`${recording.project_name||recording.project_id} · ${recording.step_count} steps${plan.some(entry=>entry.recording_id===recording.id)?' · linked':''}`)),
            e('button',{className:'key-btn',onClick:()=>setActiveId(recording.id)},'INPUTS')))),
      e('div',{className:'bg-slate-900 border border-slate-700 rounded-xl p-3 space-y-2'},
        e('h3',{className:'text-sm font-bold text-indigo-300'},'2 · Ordered plan'),
        plan.length===0 && e('p',{className:'text-xs text-slate-400'},'Nothing linked yet. Pick a recording on the left.'),
        e('ol',{className:'space-y-1'},plan.map((entry,index)=>e('li',{key:entry.recording_id,className:'flex items-center gap-2 bg-slate-950 p-2 rounded text-xs'},
          e('button',{className:'flex-1 text-left',onClick:()=>{setActiveId(entry.recording_id);setValues({});}},
            e('span',{className:activeId===entry.recording_id?'text-emerald-300':'text-slate-200'},`${index+1}. ${entry.name}`),
            e('span',{className:'block text-slate-500'},entry.project_name||'')),
          e('button',{className:'key-btn',disabled:index===0,onClick:()=>move(index,index-1)},'UP'),
          e('button',{className:'key-btn',disabled:index===plan.length-1,onClick:()=>move(index,index+1)},'DOWN'),
          e('button',{className:'key-btn',onClick:()=>setPlan(plan.filter((_,i)=>i!==index))},'REMOVE'))))),
        e('div',{className:'flex flex-wrap gap-2'},
          e('button',{className:'key-btn',disabled:busy||!plan.length,onClick:()=>work(async()=>{
            const result=await API.post('/api/runs/batch',{recording_ids:plan.map(entry=>entry.recording_id),name:'Execution plan',share_session:true,screenshots:'all',display_window:false,retry_transient:false});
            setBatch({id:result.batch_id,status:result.status}); setNotice('Ordered plan queued.');})},'RUN ORDERED PLAN'),
          e('button',{className:'key-btn',onClick:()=>{setPlan([]);setActiveId('');setValues({});setBatch(null);}},'CLEAR PLAN')))),
    e(GuideSection,{id:'plan-inputs',label:'3 · Inputs for the selected recording',
      action:'Fill in what this recording needs, then SAVE AS RECORDING DEFAULTS so the next run reuses it.'},
    active && e('div',{className:'bg-slate-900 border border-slate-700 rounded-xl p-4 space-y-3'},
      e('h3',{className:'text-sm font-bold text-indigo-300'},`3 · Variables & parameters · ${active.name}`),
      e('p',{className:'text-xs text-slate-400'},`Loaded from the cached catalog in ${catalogMs} ms. Values here are the recording's saved defaults; edits can be saved back to this recording, and {{placeholders}} stay variables when the plan runs.`),
      e(InputFields,{definitions:uniqueDefs,defaults:activeDefaults,values,onChange:setValues}),
      e('div',{className:'flex flex-wrap gap-2'},
        e('button',{className:'key-btn',disabled:busy,onClick:()=>work(async()=>{
          const result=await API.put(`/api/recordings/${active.id}/inputs`,{variables:{...activeDefaults,...values}});
          setNotice(result.publish_message||'Recording defaults saved for this plan.'); setValues({}); loadCatalog();})},'SAVE AS RECORDING DEFAULTS'),
        e('button',{className:'key-btn',disabled:busy,onClick:()=>work(async()=>{
          const result=await API.post(`/api/recordings/${active.id}/run`,{});
          if(result && result.run_id && onRun) onRun(result.run_id); else setNotice('Run started — open the Runs tab.');})},'RUN THIS RECORDING'),
        e('button',{className:'key-btn',onClick:()=>{setActiveId('');setValues({});}},'CLOSE INPUTS')))),
    batch && e('div',{className:'bg-slate-900 border border-slate-700 rounded-xl p-3 space-y-2'},
      e('p',{role:'status',className:'text-xs'},`Plan execution: ${batch.status} · ${batch.progress_pct||0}% · ${batch.passed||0} passed / ${batch.total||0}`),
      ['queued','running'].includes(batch.status) ? e('button',{className:'key-btn',onClick:()=>work(()=>API.post(`/api/runs/batch/${batch.id}/cancel`,{}))},'CANCEL EXECUTION') : e(ReportLinks,{kind:'batch',id:batch.id})),
    notice && e('p',{role:'status',className:'text-xs text-amber-300'},notice),
    inputsReady && e('p',{className:'text-xs text-emerald-300'},'Edits are kept in this tab until you save them as the recording defaults.'));
}
