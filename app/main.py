<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
<title>TestForge AI ROG Agent</title>
<script src="https://cdn.tailwindcss.com"></script>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<style>
  body { background: #020617; color: #f8fafc; font-family: system-ui; overflow: hidden; height: 100vh; width: 100vw; }
  .nav-btn { flex: 1; padding: 12px; font-size: 11px; font-weight: bold; color: #64748b; border-top: 2px solid transparent; }
  .nav-btn.active { color: #818cf8; border-top-color: #818cf8; background: rgba(30, 41, 59, 0.5); }
  .content-area { height: calc(100vh - 70px); overflow-y: auto; padding: 15px; padding-bottom: 80px; }
  .progress-fill { height: 100%; background: #6366f1; transition: width 0.5s ease; }
  .key-btn { background: #1e293b; padding: 10px; border-radius: 8px; font-size: 11px; font-weight: bold; border: 1px solid #334155; }
</style>
</head>
<body>
<div id="root"></div>
<script>
const { useState, useEffect, useRef } = React;
const e = React.createElement;
const API = {
  get: u => fetch(u).then(r => r.json()),
  post: (u, b) => fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(b)}).then(r => r.json()),
  patch: (u, b) => fetch(u, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(b)}).then(r => r.json()),
  del: u => fetch(u, {method:'DELETE'}).then(r => r.json())
};

function AiInput({ value, onChange, placeholder, isArea }) {
  const [mic, setMic] = useState(false);
  const startMic = () => {
    const rec = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
    rec.onstart = () => setMic(true); rec.onend = () => setMic(false);
    rec.onresult = (ev) => onChange({ target: { value: (value || "") + " " + ev.results[0][0].transcript } });
    rec.start();
  };
  return e('div', { className: "relative w-full" },
    e(isArea ? 'textarea' : 'input', { className: "w-full bg-slate-800 rounded-lg px-4 py-2 text-sm pr-20 border border-slate-700", placeholder, value, onChange }),
    e('div', { className: "absolute right-2 top-2 flex gap-1" },
      e('button', { onClick: startMic, className: `p-1 rounded ${mic?'bg-red-600 animate-pulse':'bg-slate-700'}` }, "🎤"),
      e('button', { onClick: async () => { const res = await API.post("/api/ai/rephrase", { text: value }); onChange({ target: { value: res.rephrased } }); }, className: "p-1 rounded bg-purple-900" }, "✨")
    )
  );
}

function App() {
  const [tab, setTab] = useState("projects");
  const [activeProject, setActiveProject] = useState(() => JSON.parse(localStorage.getItem("tf_project") || 'null'));
  const [activeRunId, setActiveRunId] = useState(null);
  const tabs = [["projects","📁"], ["variables","🔤"], ["record","⏺"], ["library","📚"], ["runs","▶"], ["comparison","⚖️"], ["artifacts","🎥"], ["github","🐙"]];
  return e('div', { className: "h-full flex flex-col" },
    e('header', { className: "p-4 border-b border-slate-800 flex justify-between bg-slate-950/80 backdrop-blur" },
      e('span', { className: "font-bold text-indigo-400" }, "⚡ TESTFORGE ROG"),
      activeProject && e('span', { className: "text-[10px] bg-indigo-900 px-2 py-1 rounded" }, activeProject.name)
    ),
    e('div', { className: "content-area" },
      tab === "projects" && e(Projects, { setActiveProject }),
      tab === "variables" && e(Variables, { project: activeProject }),
      tab === "record" && e(Recorder, { project: activeProject }),
      tab === "library" && e(Library, { project: activeProject, onRun: (id) => { setActiveRunId(id); setTab("runs"); } }),
      tab === "runs" && e(Runs, { runId: activeRunId, setRunId: setActiveRunId }),
      tab === "comparison" && e(Comparison), tab === "artifacts" && e(Artifacts), tab === "github" && e(Github)
    ),
    e('nav', { className: "fixed bottom-0 left-0 right-0 bg-slate-900 border-t border-slate-800 flex overflow-x-auto" },
      tabs.map(([k, icon]) => e('button', { key: k, onClick: () => setTab(k), className: `nav-btn ${tab===k?'active':''}` }, icon))
    )
  );
}

function Recorder({ project }) {
  const [recId, setRecId] = useState(null);
  const [frame, setFrame] = useState(null);
  const [zoom, setZoom] = useState(1);
  const [url, setUrl] = useState("https://google.com");
  const [name, setName] = useState("Session_"+Date.now());
  const ws = useRef(null);

  const start = async () => {
    const r = await API.post("/api/recordings", { project_id: project.id, name, start_url: url });
    setRecId(r.id);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws.current = new WebSocket(`${proto}://${location.host}/ws/record/${r.id}`);
    ws.current.onmessage = ev => { const m = JSON.parse(ev.data); if (m.type === "frame") setFrame("data:image/jpeg;base64," + m.data); };
  };

  const send = (m) => ws.current?.send(JSON.stringify(m));

  if (recId) return e('div', { className: "fixed inset-0 bg-slate-950 z-[100] flex flex-col p-2" },
    e('div', { className: "flex justify-between p-2" }, e('span', {className:"text-xs font-bold text-red-500 animate-pulse"}, "● ROG RECORDING"), e('div', {className:"flex gap-2"}, e('button', { onClick:()=>setZoom(z=>z+0.1), className:"bg-slate-800 px-2 rounded" }, "+"), e('button', { onClick:()=>setZoom(z=>z-0.1), className:"bg-slate-800 px-2 rounded" }, "-"), e('button', { onClick:()=> { send({type:"stop"}); setRecId(null); }, className:"bg-red-600 px-4 py-1 rounded font-bold text-xs" }, "SAVE"))),
    e('div', { className: "flex-1 overflow-auto bg-black rounded-xl border border-slate-800" }, frame && e('img', { src: frame, className: "origin-top-left", style: { transform: `scale(${zoom})` }, onClick: (evt) => {
        const r = evt.target.getBoundingClientRect();
        send({ type: "tap", x: Math.round(((evt.clientX - r.left)/zoom / r.width) * 1280), y: Math.round(((evt.clientY - r.top)/zoom / r.height) * 800) });
    }})),
    e('div', { className: "bg-slate-900 p-2 space-y-2 rounded-b-xl" },
      e('div', { className: "grid grid-cols-4 gap-1" },
        ["Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].map(k => e('button', { key: k, className: "key-btn", onClick: () => send({type:"key", key: k}) }, k))
      ),
      e('div', { className: "flex gap-2" }, e('input', { className: "flex-1 bg-slate-800 p-2 rounded text-sm", placeholder: "Type text..." }), e('button', { className: "bg-indigo-600 px-4 rounded" }, "Type"))
    )
  );

  return e('div', {className:"space-y-4"},
    e('div', {className:"bg-slate-900 p-6 rounded-3xl border border-slate-800 shadow-xl"},
      e('h2', {className:"font-bold text-indigo-400 mb-4"}, "⏺ ROG SESSION START"),
      e(AiInput, { value: name, onChange: e => setName(e.target.value), placeholder: "Recording Name" }), e('div', {className:"h-3"}),
      e(AiInput, { value: url, onChange: e => setUrl(e.target.value), placeholder: "Target URL" }),
      e('button', { onClick: start, className: "w-full bg-red-600 py-4 mt-4 rounded-xl font-bold" }, "LAUNCH AI RECORDER")
    )
  );
}

function Library({ project, onRun }) {
  const [recs, setRecs] = useState([]);
  const load = () => project && API.get(`/api/projects/${project.id}/recordings`).then(setRecs);
  useEffect(() => { load(); }, [project]);
  return e('div', { className: "space-y-4" },
    recs.filter(r => !r.parent_id).map(r => e('div', { key: r.id, className: "bg-slate-900 rounded-2xl border border-slate-800 overflow-hidden" },
      e('div', { className: "p-4 flex justify-between items-center bg-slate-800/20" },
        e('div', {}, e('div', {className:"font-bold text-indigo-300"}, r.name), e('div', {className:"text-[10px] text-slate-500"}, `${r.step_count} steps`)),
        e('div', {className:"flex gap-2"},
          e('button', { onClick: () => onRun(r.id), className: "bg-emerald-600 px-5 py-2 rounded-xl font-bold text-xs shadow-lg" }, "RUN"),
          e('a', { href:`/api/recordings/${r.id}/export/jenkins`, className:"bg-slate-800 p-2 rounded-lg" }, "🥒")
        )
      ),
      recs.filter(c => c.parent_id === r.id).map(c => e('div', {key:c.id, className:"ml-6 p-3 border-l-2 border-indigo-600 flex justify-between items-center"}, e('span',{className:"text-xs text-slate-400 font-medium"},`↳ ${c.name}`), e('button', {onClick:()=>onRun(c.id), className:"text-[10px] text-indigo-400 font-bold"}, "RUN")))
    ))
  );
}

function Runs({ runId, setRunId }) {
  const [runs, setRuns] = useState([]);
  const [activeLog, setActiveLog] = useState([]);
  const [frame, setFrame] = useState(null);
  const [percent, setPercent] = useState(0);
  useEffect(() => { API.get("/api/runs").then(setRuns); }, []);
  useEffect(() => {
    if (runId) {
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/runs/${runId}`);
      ws.onmessage = ev => {
        const m = JSON.parse(ev.data);
        if (m.type === "frame") setFrame("data:image/jpeg;base64," + m.data);
        if (m.percent !== undefined) setPercent(m.percent);
        if (m.status) setActiveLog(prev => [...prev.filter(x => x.order !== m.order), m]);
      };
      return () => ws.close();
    }
  }, [runId]);

  return e('div', { className: "space-y-4" },
    runId && e('div', { className: "bg-slate-900 border-2 border-indigo-500 rounded-3xl p-5 space-y-4 shadow-2xl" },
      e('div', { className: "flex justify-between font-bold text-xs" }, e('span', {}, "ROG EXECUTION JOURNEY"), e('button', {onClick:()=>setRunId(null)}, "✕")),
      e('div', { className: "w-full bg-slate-800 h-3 rounded-full overflow-hidden" }, e('div', { className: "progress-fill shadow-[0_0_15px_rgba(99,102,241,0.5)]", style: { width: `${percent}%` } })),
      e('div', {className:"bg-black rounded-2xl overflow-hidden border border-slate-800 min-h-[160px]"}, frame ? e('img', { src: frame, className: "w-full" }) : e('div', {className:"h-40 flex items-center justify-center text-slate-700 text-[10px]"}, "Waiting for cloud browser stream...")),
      e('div', { className: "max-h-48 overflow-y-auto space-y-2" }, activeLog.sort((a,b)=>b.order-a.order).map((s,i) => e('div', {key:i, className:"p-3 bg-slate-950 rounded-xl flex gap-3 border border-slate-900 shadow-inner"}, e('span', {}, s.status==='passed'?'✅':s.status==='failed'?'❌':'⏳'), e('div', {className:"flex-1"}, e('div', {className:"text-xs font-bold"}, s.label), s.screenshot && e('img', {src: s.screenshot, className:"mt-2 rounded-lg border border-slate-800 max-h-32"})))))
    ),
    runs.map(r => e('div', { key: r.id, onClick: () => setRunId(r.id), className: "p-5 bg-slate-900 rounded-2xl border border-slate-800 space-y-2 mb-2 cursor-pointer hover:border-indigo-500" },
      e('div', {className:"flex justify-between"}, e('span', {className:"font-bold text-indigo-400 text-xs font-mono"}, `AGENT RUN: ${r.id.slice(0,8)}`), e('span', {className:`text-[10px] font-bold px-2 py-0.5 rounded ${r.status==='passed'?'bg-emerald-900 text-emerald-400':'bg-red-900 text-red-400'}`}, r.status.toUpperCase())),
      r.investigation && e('div', {className:"text-[9px] bg-black p-2 rounded text-amber-500 font-mono italic leading-tight border border-amber-900/30"}, r.investigation)
    ))
  );
}

function Projects({ setActiveProject }) {
  const [projs, setProjs] = useState([]);
  const [name, setName] = useState("");
  useEffect(() => { API.get("/api/projects").then(setProjs); }, []);
  return e('div', { className: "space-y-4" },
    e('div', { className: "bg-slate-900 p-4 rounded-xl border border-slate-800 shadow-xl" },
      e(AiInput, { value: name, onChange: e => setName(e.target.value), placeholder: "Project Name" }),
      e('button', { onClick: async () => { await API.post("/api/projects", { name }); setName(""); API.get("/api/projects").then(setProjs); }, className: "w-full bg-indigo-600 mt-2 py-2 rounded-lg font-bold" }, "Create Project")
    ),
    projs.map(p => e('div', { key: p.id, onClick: () => { setActiveProject(p); localStorage.setItem("tf_project", JSON.stringify(p)); }, className: "p-4 bg-slate-900 rounded-xl border border-slate-800 mb-2 cursor-pointer hover:border-indigo-500 transition-all shadow-md" }, p.name))
  );
}

function Variables({ project }) {
  const [vars, setVars] = useState([]);
  const [name, setName] = useState("");
  const [val, setVal] = useState("");
  const load = () => project && API.get(`/api/variables?project_id=${project.id}`).then(setVars);
  useEffect(() => { load(); }, [project]);
  return e('div', { className: "space-y-4" },
    e('div', { className: "bg-slate-900 p-4 rounded-xl border border-slate-800" },
      e(AiInput, { value: name, onChange: e => setName(e.target.value), placeholder: "Var Key" }), e('div', {className:"h-2"}),
      e(AiInput, { value: val, onChange: e => setVal(e.target.value), placeholder: "Value" }),
      e('button', { onClick: async () => { await API.post("/api/variables", { project_id: project.id, name, value: val }); setName(""); setVal(""); load(); }, className: "w-full bg-indigo-600 mt-2 py-2 rounded-lg font-bold" }, "Save Variable")
    ),
    vars.map(v => e('div', { key: v.id, className: "p-4 bg-slate-900 rounded-xl border border-slate-800 flex justify-between shadow-lg" },
      e('div', {}, e('code', {className:"text-indigo-400 font-bold"}, `{{${v.name}}}`), e('div', {className:"text-xs text-slate-500"}, v.value)),
      e('div', {className:"flex gap-1 flex-wrap"}, (v.associated_recordings || []).map((tag, i) => e('span', { key: i, className: "text-[9px] bg-indigo-900/50 text-indigo-300 px-2 py-0.5 rounded" }, tag)))
    ))
  );
}

function Comparison() {
  const [runs, setRuns] = useState([]);
  const [sel, setSel] = useState(null);
  useEffect(() => { API.get("/api/runs").then(setRuns); }, []);
  return e('div', { className: "space-y-4" },
    runs.map(r => e('div', { key: r.id, className: "bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-2xl" },
      e('div', { className: "p-4 flex justify-between bg-slate-800/30", onClick: () => setSel(sel === r.id ? null : r.id) }, e('div', {}, e('div', {className:"font-bold text-sm text-indigo-300 font-mono"}, `Audit Run Analysis #${r.id.slice(0,8)}`)), e('span', {className:"text-indigo-400 text-xs font-bold"}, "COMPARE ➔")),
      sel === r.id && e('div', { className: "grid grid-cols-2 gap-2 p-2 bg-black/40 border-t border-slate-800 shadow-inner" },
        e('div', { className: "space-y-1 border-r border-slate-800 pr-1" }, e('div', {className:"text-[9px] font-bold text-purple-400 mb-1 uppercase text-center"}, "Recording Baseline"), (r.baseline || []).map((s, i) => e('div', {key:i, className:"text-[9px] bg-slate-900 p-2 rounded font-mono"}, `${s.action}`))),
        e('div', { className: "space-y-1 pl-1" }, e('div', {className:"text-[9px] font-bold text-emerald-400 mb-1 uppercase text-center"}, "AI Execution Result"), (r.log || []).map((s, i) => e('div', {key:i, className:"text-[9px] bg-slate-900 p-2 rounded flex justify-between font-mono"}, e('span',{},s.action), e('span',{},s.status==='passed'?'✅':'❌'))))
      )
    ))
  );
}

function Artifacts() {
  const [runs, setRuns] = useState([]);
  useEffect(() => { API.get("/api/runs").then(setRuns); }, []);
  return e('div', { className: "space-y-4" },
    runs.filter(r => r.has_video).map(r => e('div', { key: r.id, className: "bg-slate-900 p-4 rounded-xl border border-slate-800 shadow-2xl" }, e('video', { controls: true, className: "w-full rounded bg-black", src: `/api/runs/video/${r.id}` })))
  );
}

function Github() {
  return e('div', { className: "text-center py-20 bg-slate-900 rounded-3xl border border-slate-800 shadow-2xl" },
    e('div', {className:"text-6xl mb-4"}, "🐙"), e('h2', {className:"font-bold text-xl mb-4 text-indigo-300"}, "AI ROG SYNC ENGINE"),
    e('a', { href: "/api/sync/github-bundle", className: "bg-indigo-600 px-10 py-4 rounded-2xl font-bold shadow-lg" }, "Download Full Bundle")
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(e(App));
</script>
</body>
</html>