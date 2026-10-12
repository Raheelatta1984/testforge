"""AI smart onboarding for script import: validate → AI repair → re-validate, per step.

The static parser (app.script_import / app.import_formats) stays the first pass
and the only authority on what a canonical step is. When it reports unsupported
syntax, this module runs an AI repair loop instead of refusing the upload:

1. VALIDATE   the file statically, exactly as before (never executes source).
2. CONVERT    the whole file with the AI: it must emit canonical step JSON
              conforming to the same grammar the static parser produces.
3. IMPORT     every AI step is re-validated with app.step_runtime.normalize_step
              -- the very same validator that guards saved recordings.
4. LOOP       a step that fails validation goes back to the AI with the exact
              validator error and is fixed and re-validated, looping per step
              until it passes, the per-step attempt budget or the import-wide
              call budget runs out. What still fails is dropped and reported.

Hard bounds that never change:

* Uploaded code is NEVER executed -- not by the parser, not by the AI loop.
  The AI only ever emits *data* (step objects), and every one of them is
  re-validated by the same normalize_step that guards recorded steps.
* Budgeted: a bounded number of bounded-size model calls per import, a bounded
  number of repair attempts per step, MAX_STEPS steps, MAX_TEXT values.
* The save path is unchanged: a passing draft test is still required before
  publication (or the explicit written unverified override).
* Graceful degradation: without ANTHROPIC_API_KEY, on any AI error, or when the
  AI converts nothing, the original static result is returned unchanged.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from app.script_import import Preview
from app.step_runtime import MAX_STEPS, normalize_step

MODEL = os.environ.get("TF_IMPORT_AI_MODEL", "claude-sonnet-4-5")
# Overridable so a self-hosted proxy/gateway can be used, and so tests can run
# the whole loop against a fake endpoint without network access.
API_URL = os.environ.get("TF_IMPORT_AI_URL", "https://api.anthropic.com/v1/messages")
API_VERSION = "2023-06-01"
MAX_CALL_TOKENS = 4096
CALL_TIMEOUT_SECONDS = 90
CALL_RETRIES = 1                     # one retry for transient 429/5xx/network
MAX_AI_CALLS = int(os.environ.get("TF_IMPORT_AI_MAX_CALLS", "24"))
TIME_BUDGET_SECONDS = float(os.environ.get("TF_IMPORT_AI_TIME_BUDGET", "240"))
MAX_SOURCE_CHARS = 48_000            # excerpt sent to the model per call
STEP_FIX_ATTEMPTS = 3                # per failing step
MAX_STEP_FIXES = 24                  # per import, across all steps
WHOLE_FILE_ROUNDS = 2                # convert pass + one "what did I miss" pass
BINARY_LIMIT = 0.10                  # >10% undecodable bytes -> not AI-convertible

ACTIONS_SPEC = [
    "navigate", "click", "dblclick", "right_click", "hover", "fill", "type", "press",
    "check", "uncheck", "select_option", "drag_to", "scroll", "save_variable",
    "capture_text", "assert_text", "assert_contains", "assert_value", "assert_visible",
    "assert_hidden", "assert_enabled", "assert_disabled", "assert_checked",
    "assert_unchecked", "assert_count", "assert_url", "assert_title",
    "wait_visible", "wait_hidden", "new_page", "switch_page", "close_page",
    "open_popup", "dialog", "upload_file", "download", "api_request", "mock_route",
    "clear_routes",
]
NO_LOCATOR_ACTIONS = {
    "navigate", "press", "assert_url", "assert_title", "new_page", "switch_page",
    "close_page", "api_request", "mock_route", "clear_routes", "save_variable",
    "capture_text", "dialog",
}

SYSTEM_CONVERT = (
    "You are TestForge's import onboarding converter. You convert uploaded browser "
    "test scripts into canonical step data. You never execute anything and never "
    "emit code: your entire output is one JSON document of step data.\n\n"
    "Output STRICT JSON only (no markdown fences, no commentary):\n"
    '{"name": "<short recording name>", "start_url": "<first URL visited or empty>",\n'
    ' "steps": [<step>, ...], "notes": ["<short facts the tester must know>", ...]}\n\n'
    "Each step is an object:\n"
    '{"action": "<one action>", "selector": <locator or {}>, "value": "<string>",\n'
    ' "repeat_count": <int 1-100, default 1>, "label": "<short human description>"}\n\n'
    "Actions allowed: " + ", ".join(ACTIONS_SPEC) + ".\n"
    "Locator (selector) forms:\n"
    '  element: {"kind": "css"|"role"|"label"|"text"|"test_id"|"placeholder", '
    '"primary": "<value>"} with optional "name" (role name), "exact": true/false, '
    '"frames": ["<css frame selector>", ...] (max 5), "timeout_ms": 1000-30000.\n'
    '  pointer-by-coordinate (click/dblclick/right_click/hover only): '
    '{"x": <int>, "y": <int>}.\n\n'
    "Rules:\n"
    "- These actions take NO locator (selector must be {}): "
    + ", ".join(sorted(NO_LOCATOR_ACTIONS)) + ".\n"
    "- navigate/assert_url take the URL in value (http/https only, no credentials).\n"
    "- press: value is the key ('Enter', 'Control+a'). select_option: value is the "
    "option value (or add selector.by='label'). capture_text/save_variable: value is "
    "the variable name to store into. scroll: value is 'up'|'down'|integer pixels.\n"
    "- assert_count: value is the expected element count as a string.\n"
    "- Reuse {{variable}} placeholders when the script clearly parameterises a value; "
    "otherwise inline the literal.\n"
    "- Convert loops into repeat_count (max 100 each). Convert branches by keeping "
    "only the main/dominant path and explaining the dropped branch in notes.\n"
    "- Skip imports, helper definitions, logging and browser bootstrap: they are not "
    "steps. Order steps exactly as the script executes them.\n"
    "- If the script is a runner that loads its steps from an external file "
    "(steps.json, execution-plan.json, a spreadsheet...), return an empty steps "
    "array and say in notes that the referenced data file must be uploaded together "
    "with the script (bundle upload); name the file.\n"
    "- Never invent interactions the script does not perform. Notes is the place for "
    "anything you could not convert."
)
SYSTEM_FIX_STEP = (
    "You fix one canonical test step that failed validation. You output STRICT JSON "
    "only: a single JSON object for the corrected step, same schema "
    '{"action","selector","value","repeat_count","label"}. Use only the allowed '
    "actions and locator kinds; keep the step's intent. No commentary, no fences."
)


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def api_key():
    return os.environ.get("ANTHROPIC_API_KEY", "")


class Budget:
    """One import's AI spend: calls, per-step repairs and wall clock, all capped.

    The clock matters as much as the counters: the preview endpoint serialises
    imports, so a slow or retrying AI service must not be able to hold the
    import gate open for long. When any budget runs out the loop stops and the
    result so far is returned (the static result if nothing landed).
    """

    def __init__(self):
        self.calls = 0
        self.step_fixes = 0
        self.deadline = time.monotonic() + TIME_BUDGET_SECONDS

    def exhausted(self):
        return time.monotonic() >= self.deadline

    def take_call(self):
        if self.calls >= MAX_AI_CALLS or self.exhausted():
            return False
        self.calls += 1
        return True

    def take_step_fix(self):
        if self.step_fixes >= MAX_STEP_FIXES or self.exhausted():
            return False
        self.step_fixes += 1
        return True


def _decode_source(data):
    """The file as text, or None when it is binary the AI cannot read."""
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            pass
    text = data.decode("utf-8", errors="replace")
    if text.count("\ufffd") > len(text) * BINARY_LIMIT:
        return None
    return text


def _excerpt(source):
    if len(source) <= MAX_SOURCE_CHARS:
        return source
    return source[:MAX_SOURCE_CHARS] + "\n... [truncated %d characters]" % (len(source) - MAX_SOURCE_CHARS)


def _extract_json(text):
    """The first JSON object in a model reply, tolerating fences or chatter."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty model reply")
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model reply")
    decoder = json.JSONDecoder()
    reply, pos = None, start
    while 0 <= pos < len(text):
        try:
            reply, pos = decoder.raw_decode(text, pos)
            return reply
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            if pos < 0:
                break
    if reply is not None:
        return reply
    raise ValueError("model reply is not valid JSON")


def _post(payload, key):
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for attempt in range(1 + CALL_RETRIES):
        request = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": API_VERSION,
        })
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504, 529}:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < CALL_RETRIES:
            time.sleep(2)
    raise RuntimeError("AI service unavailable: %s" % last_error)


def call_model(system, user, budget):
    """One bounded messages call. Returns the reply text, or raises RuntimeError."""
    if not budget.take_call():
        raise RuntimeError("AI call budget exhausted for this import")
    key = api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    payload = {"model": MODEL, "max_tokens": MAX_CALL_TOKENS, "system": system,
               "messages": [{"role": "user", "content": user[:MAX_SOURCE_CHARS * 2]}]}
    response = _post(payload, key)
    text = "".join(block.get("text", "") for block in response.get("content", [])
                   if isinstance(block, dict) and block.get("type") == "text")
    if not text:
        raise RuntimeError("AI reply contained no text")
    return text


def _diagnostic_lines(result):
    return "\n".join("  line %s: %s" % (item.get("line"), item.get("message"))
                     for item in result.get("diagnostics", []) if item.get("severity") == "error")


def convert_whole_file(filename, source, static_result, budget, feedback=""):
    """Ask the AI to convert the whole file; returns the parsed reply dict."""
    prompt = (
        "Convert this uploaded test script into TestForge canonical steps.\n"
        "File name: %s\n"
        "The static validator rejected it with these diagnostics:\n%s\n%s"
        "Script:\n%s" % (filename,
                         _diagnostic_lines(static_result) or "  (none listed)",
                         ("Previous attempt feedback:\n%s\n" % feedback) if feedback else "",
                         _excerpt(source)))
    return _extract_json(call_model(SYSTEM_CONVERT, prompt, budget))


def fix_one_step(step, error, source, budget):
    """The per-step repair loop's single call: one bad step in, one fixed step out."""
    prompt = (
        "This canonical step failed TestForge validation:\n%s\n\n"
        "Validation error: %s\n\n"
        "The original script context (for intent only):\n%s\n\n"
        "Return ONLY the corrected step JSON object." % (
            json.dumps(step, ensure_ascii=False)[:2000], str(error)[:400],
            _excerpt(source)[:6000]))
    reply = _extract_json(call_model(SYSTEM_FIX_STEP, prompt, budget))
    if not isinstance(reply, dict):
        raise ValueError("step fix is not a JSON object")
    return reply


def validate_steps(raw_steps, source, budget, trace):
    """Re-validate every AI step; loop bad ones through the AI until they pass.

    This is the per-step loop the onboarding is built on: validate -> AI fix ->
    re-validate, for every single step, until it is canonical, its attempt budget
    is spent, or the import-wide repair budget is spent. Returns (steps, failures).
    """
    accepted, failures = [], []
    for index, raw in enumerate(raw_steps[:MAX_STEPS], 1):
        step = raw if isinstance(raw, dict) else {}
        outcome = None
        for attempt in range(1, STEP_FIX_ATTEMPTS + 1):
            try:
                accepted.append(normalize_step(step, len(accepted) + 1))
                outcome = "accepted%s" % ("" if attempt == 1 else " after %d AI repair(s)" % (attempt - 1))
                break
            except (ValueError, TypeError, OverflowError) as exc:
                error = exc
                if not budget.take_step_fix():
                    outcome = "dropped: repair budget exhausted (%s)" % error
                    break
                try:
                    step = fix_one_step(step, error, source, budget)
                    outcome = None  # loop: re-validate the fixed step
                except (RuntimeError, ValueError) as fix_error:
                    outcome = "dropped: AI repair failed (%s)" % fix_error
                    break
        if outcome and outcome.startswith("accepted"):
            trace.append({"step": index, "result": outcome})
        elif outcome:
            failures.append({"step": index, "raw": step, "reason": outcome})
            trace.append({"step": index, "result": outcome})
    return accepted, failures


def _merge_result(static_result, steps, notes, ai_meta):
    """Rebuild the preview result with AI steps; static errors become notes.

    The static diagnostics described the *source*; the AI loop has now handled
    that source. Keeping them as blocking errors would refuse an upload the
    onboarding did convert, so they are downgraded to explicit notes -- but what
    the AI could NOT convert is named in its own notes, and the save gate (a
    passing draft test) is unchanged.
    """
    handled = bool(steps)
    diagnostics = []
    for item in static_result.get("diagnostics", []):
        if item.get("severity") == "error" and handled:
            diagnostics.append({**item, "severity": "note",
                                "message": ("[ai-onboarding] source diagnostic (handled by conversion): "
                                            + str(item.get("message")))[:400]})
        else:
            diagnostics.append(item)
    for note in notes:
        diagnostics.append({"file": static_result.get("filename", ""), "line": 1,
                            "severity": "note", "message": ("[ai-onboarding] " + str(note))[:400]})
    if handled:
        diagnostics.append({"file": static_result.get("filename", ""), "line": 1,
                            "severity": "note",
                            "message": ("[ai-onboarding] converted %d step(s) with %d AI call(s) "
                                        "(%d per-step repair loop(s)); source code was never executed"
                                        % (len(steps), ai_meta["calls"], ai_meta["step_fixes"]))})
    preview = Preview(static_result.get("filename", "upload"))
    preview.name = static_result.get("name") or preview.name
    for step in steps:
        try:
            preview.add(step, step.get("order", len(preview.steps) + 1))
        except (ValueError, TypeError) as exc:   # already validated; belt and braces
            diagnostics.append({"file": static_result.get("filename", ""), "line": 1,
                                "severity": "note", "message": str(exc)[:400]})
    if not preview.steps and not any(d["severity"] == "error" for d in diagnostics):
        for item in static_result.get("diagnostics", []):
            if item.get("severity") == "error":
                diagnostics.append(item)
                break
    result = preview.result()
    result["diagnostics"] = diagnostics
    result["name"] = static_result.get("name") or result["name"]
    result["start_url"] = static_result.get("start_url") or result["start_url"]
    if static_result.get("detected"):
        result["detected"] = static_result["detected"]
    if static_result.get("variable_suggestions"):
        result["variable_suggestions"] = static_result["variable_suggestions"]
    if static_result.get("execution_plan"):
        result["execution_plan"] = static_result["execution_plan"]
    result["ai"] = {**ai_meta, "steps": len(steps)}
    return result


def smart_repair(filename, source, static_result):
    """The full validate → AI convert → per-step fix loop for one text file."""
    started = time.monotonic()
    budget = Budget()
    trace = []
    ai_meta = {"available": True, "used": True, "model": MODEL, "calls": 0,
               "step_fixes": 0, "converted_steps": 0, "repaired_steps": 0,
               "unconverted": 0, "loop": "validate → convert → validate per step → fix → re-validate"}
    if not source:
        ai_meta["note"] = "Binary content cannot be AI-converted; fix the diagnostics manually."
        return static_result
    steps, notes = [], []
    dropped = 0
    feedback = ""
    for round_no in range(1, WHOLE_FILE_ROUNDS + 1):
        try:
            reply = convert_whole_file(filename, source, static_result, budget, feedback)
        except (RuntimeError, ValueError) as exc:
            trace.append({"stage": "convert round %d" % round_no, "result": "failed: %s" % exc})
            break
        raw_steps = reply.get("steps") if isinstance(reply, dict) else None
        if not isinstance(raw_steps, list):
            raw_steps = []
        trace.append({"stage": "convert round %d" % round_no, "result": "%d step(s) proposed" % len(raw_steps)})
        batch, failures = validate_steps(raw_steps, source, budget, trace)
        dropped += len(failures)
        steps.extend(batch)
        for failure in failures:
            note = "Step %d could not be converted after repeated repair attempts (%s)." % (
                failure["step"], failure["reason"])
            if note not in notes:
                notes.append(note)
        reply_notes = reply.get("notes") if isinstance(reply, dict) else None
        if isinstance(reply_notes, list):
            notes.extend(str(note)[:300] for note in reply_notes[:10]
                         if str(note)[:300] not in notes)
        if steps or not failures:
            break
        # Nothing landed yet: one more round, telling the model what failed.
        feedback = "Your previous conversion produced only invalid steps: %s" % (
            "; ".join(str(f["reason"])[:160] for f in failures[:4]))
    ai_meta.update(calls=budget.calls, step_fixes=budget.step_fixes,
                   converted_steps=len(steps),
                   repaired_steps=sum(1 for item in trace if "repair" in str(item.get("result", ""))),
                   unconverted=dropped,
                   elapsed_seconds=round(time.monotonic() - started, 1),
                   trace=trace[:200])
    return _merge_result(static_result, steps, notes, ai_meta)


def _ai_unavailable_meta(reason):
    return {"available": False, "used": False, "reason": reason}


def smart_parse_file(filename, data):
    """Static validation first; AI smart onboarding only when it reports errors."""
    from app.script_import import parse_file
    static_result = parse_file(filename, data)
    if static_result.get("can_save"):
        static_result["ai"] = {"available": available(), "used": False,
                               "reason": "static validation passed; AI repair was not needed"}
        return static_result
    if not available():
        static_result["ai"] = _ai_unavailable_meta(
            "ANTHROPIC_API_KEY is not set; set it to enable AI smart onboarding")
        return static_result
    source = _decode_source(data)
    if source is None:
        static_result["ai"] = _ai_unavailable_meta(
            "file content is binary; AI onboarding converts text-based scripts")
        return static_result
    return smart_repair(filename, source, static_result)


def smart_parse_bundle(files):
    """Bundle variant: repair the script file the bundle actually parsed."""
    from app.script_import import parse_bundle
    result = parse_bundle(files)
    result["ai"] = _ai_unavailable_meta("not needed; static validation passed")
    result["ai"]["available"] = available()
    if result.get("can_save") or not available():
        return result
    script_name = result.get("filename") or ""
    data = next((payload for name, payload in files if name == script_name), None)
    source = _decode_source(data) if data is not None else None
    if source is None:
        result["ai"] = _ai_unavailable_meta(
            "the rejected script could not be read as text for AI onboarding")
        return result
    repaired = smart_repair(script_name, source, result)
    for key in ("wrapper", "notice"):
        if result.get(key):
            repaired[key] = result[key]
    return repaired
