"""API Contract: review an OpenAPI 3.x or Swagger 2.0 document for validation and security gaps.

Reads the spec only. It never calls the API it describes. The output is a list
of findings plus contract test cases (expected status codes) that a test
runner can execute later.
"""
from __future__ import annotations

import json
import re

from .common import MAX_ITEMS, as_text, envelope, finding, scrub

MAX_SPEC_CHARS = 2_000_000
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
SENSITIVE_FIELD = re.compile(r"password|passwd|secret|token|api[_-]?key|\bssn\b|credit|cvv|card[_-]?number|iban", re.I)
TRACE_FIELD = re.compile(r"stack|trace|sql|exception", re.I)
SENSITIVE_PATH = re.compile(r"user|account|admin|payment|order|invoice|customer|profile|password|token|salary|"
                            r"health|patient", re.I)
COLLECTION_HINT = re.compile(r"list|search|all|index", re.I)


def parse_spec(raw: str) -> dict:
    text = as_text(raw, limit=MAX_SPEC_CHARS, field="API specification")
    if not text.strip():
        raise ValueError("The API specification is empty.")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml  # PyYAML is a declared runtime dependency
        except ImportError as exc:  # pragma: no cover - guarded by requirements
            raise ValueError("YAML support is not installed on this server.") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError("The specification is neither valid JSON nor valid YAML.") from exc
    if not isinstance(data, dict) or not ("openapi" in data or "swagger" in data):
        raise ValueError("Not an OpenAPI 3.x or Swagger 2.0 document (no 'openapi' or 'swagger' key).")
    return data


class _Refs:
    """Resolve local $ref pointers with a depth bound, so a cyclic spec cannot hang the agent."""

    def __init__(self, root: dict):
        self.root = root

    def get(self, node, depth: int = 0):
        while isinstance(node, dict) and "$ref" in node and depth < 20:
            ref = str(node["$ref"])
            if not ref.startswith("#/"):
                return {}
            target = self.root
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part) if isinstance(target, dict) else None
                if target is None:
                    return {}
            node = target
            depth += 1
        return node if isinstance(node, dict) else {}


def _operations(spec: dict, refs: _Refs):
    for path, item in (spec.get("paths") or {}).items():
        item = refs.get(item)
        for method in HTTP_METHODS:
            if method in item and isinstance(item[method], dict):
                op = item[method]
                params = [refs.get(p) for p in (item.get("parameters") or []) + (op.get("parameters") or [])]
                yield method.upper(), str(path), op, [p for p in params if p]


def _schema_props(schema, refs: _Refs, depth: int = 0):
    schema = refs.get(schema)
    if depth > 4 or not schema:
        return {}
    props = dict(schema.get("properties") or {})
    for part in ("allOf", "oneOf", "anyOf"):
        for sub in schema.get(part) or []:
            props.update(_schema_props(sub, refs, depth + 1))
    if schema.get("type") == "array":
        props.update(_schema_props(schema.get("items"), refs, depth + 1))
    return props


def review(raw_spec: str, title: str | None = None) -> dict:
    spec = parse_spec(raw_spec)
    refs = _Refs(spec)
    findings: list[dict] = []
    operations: list[dict] = []
    tests: list[dict] = []
    is_v3 = "openapi" in spec
    servers = [s.get("url", "") for s in (spec.get("servers") or []) if isinstance(s, dict)]
    if not is_v3:
        servers = [f"{'https' if 'https' in (spec.get('schemes') or []) else 'http'}://{spec.get('host', '')}"]

    global_security = bool(spec.get("security"))
    schemes = (spec.get("components") or {}).get("securitySchemes") or spec.get("securityDefinitions") or {}

    if any(str(url).lower().startswith("http://") for url in servers):
        findings.append(finding("API-01", "high", "Plain HTTP server URL",
                                "The spec advertises an http:// server; credentials and tokens would travel unencrypted.",
                                "Publish https:// servers only and redirect HTTP at the edge."))
    if not servers:
        findings.append(finding("API-02", "low", "No server URL",
                                "The spec does not say where the API is served.",
                                "Declare servers (OpenAPI 3) or host/basePath (Swagger 2)."))

    if not global_security and not schemes:
        findings.append(finding("API-03", "medium", "No security scheme defined",
                                "Neither components.securitySchemes nor a global security requirement exists.",
                                "Declare the scheme (bearer, API key or OAuth2) and reference it per operation."))

    count = 0
    for method, path, op, params in _operations(spec, refs):
        count += 1
        op_id = str(op.get("operationId") or f"{method} {path}")
        secured = global_security or bool(op.get("security"))
        if op.get("security") == []:
            secured = False  # explicit opt-out: the operation is public on purpose
        responses = {str(code): refs.get(resp) for code, resp in (op.get("responses") or {}).items()}
        codes = sorted(responses)
        where = f"{method} {path}"
        operations.append({"method": method, "path": path, "operationId": op.get("operationId"),
                           "secured": secured, "responses": codes,
                           "parameters": len(params), "request_body": bool(op.get("requestBody"))})

        if not op.get("operationId"):
            findings.append(finding("API-04", "low", "Operation has no operationId", where,
                                    "Add a stable operationId; contract tests and changelogs reference it."))
        if not secured and SENSITIVE_PATH.search(path) and method != "OPTIONS":
            findings.append(finding("API-05", "high", "Sensitive-looking operation without security",
                                    where + " is not protected by any security requirement.",
                                    "Require authentication, or mark it public explicitly with security: []."))
        if not any(code.startswith("4") for code in codes) and codes:
            findings.append(finding("API-06", "medium", "No 4xx responses documented", where,
                                    "Document 400/401/403/404/422 with an error schema."))
        if not any(code.startswith("5") for code in codes) and codes:
            findings.append(finding("API-07", "low", "No 5xx response documented", where,
                                    "Document 500/503 so clients know the failure contract."))
        if method == "GET" and op.get("requestBody"):
            findings.append(finding("API-08", "medium", "GET with a request body", where,
                                    "Move the input to query parameters; many proxies drop GET bodies."))

        for param in params:
            name = str(param.get("name", ""))
            schema = refs.get(param.get("schema") or {})
            if param.get("in") == "path" and not param.get("required", True):
                findings.append(finding("API-09", "medium", "Path parameter not marked required",
                                        f"{where} path parameter {name}", "Path parameters are always required."))
            if schema.get("type") == "string" and not (schema.get("maxLength") or schema.get("enum")
                                                      or schema.get("format") or schema.get("pattern")):
                findings.append(finding("API-10", "medium", "Unbounded string parameter",
                                        f"{where} parameter '{name}' has no maxLength, enum, format or pattern.",
                                        "Bound the length and pattern so invalid input is rejected at the edge."))
            if schema.get("type") in ("integer", "number") and schema.get("minimum") is None and \
                    schema.get("maximum") is None:
                findings.append(finding("API-11", "low", "Numeric parameter without range",
                                        f"{where} parameter '{name}'.", "Declare minimum and maximum."))
            if param.get("in") == "query" and schema.get("type") == "array" and not schema.get("maxItems"):
                findings.append(finding("API-12", "medium", "Unbounded array query parameter",
                                        f"{where} parameter '{name}'.", "Declare maxItems."))

        body = refs.get(op.get("requestBody") or {})
        media = (body.get("content") or {}) if body else {}
        for content in media.values():
            schema = refs.get(content.get("schema") or {})
            if schema and schema.get("type") == "object":
                if not schema.get("required"):
                    findings.append(finding("API-13", "medium", "Request body has no required fields",
                                            where, "List the required properties so validation is testable."))
                if schema.get("additionalProperties") is not False and "additionalProperties" not in schema:
                    findings.append(finding("API-14", "low", "Request body accepts unknown fields",
                                            where, "Set additionalProperties: false to reject typos and mass assignment."))
            break

        for code, resp in responses.items():
            if not code.startswith("2"):
                continue
            for content in (resp.get("content") or {}).values():
                for prop, _schema in _schema_props(content.get("schema") or {}, refs).items():
                    if SENSITIVE_FIELD.search(prop):
                        findings.append(finding("API-15", "high", "Sensitive field in a response",
                                                f"{where} {code} returns '{prop}'.",
                                                "Remove it from the response or mask it; never echo secrets."))
            break

        for code, resp in responses.items():
            if code.startswith(("4", "5")):
                for content in (resp.get("content") or {}).values():
                    for prop in _schema_props(content.get("schema") or {}, refs):
                        if TRACE_FIELD.search(prop):
                            findings.append(finding("API-16", "medium", "Error response exposes internals",
                                                    f"{where} {code} returns '{prop}'.",
                                                    "Return a stable error code and message; log details server-side."))

        if len(tests) < MAX_ITEMS * 2:
            tests.extend(_contract_tests(method, path, op_id, params, codes, secured, body, responses))

    for number, test in enumerate(tests, start=1):  # one numbering across every operation
        test["id"] = f"AC-API-{number:03d}"

    if count == 0:
        findings.append(finding("API-00", "high", "No operations found",
                                "The document has no paths with HTTP methods.",
                                "Check that the paths section is present and not empty."))

    tests = tests[: MAX_ITEMS * 2]
    summary = (f"Reviewed {count} operation(s) in {'OpenAPI ' + str(spec.get('openapi')) if is_v3 else 'Swagger ' + str(spec.get('swagger'))}; "
               f"{len(findings)} finding(s) and {len(tests)} contract test(s).")
    verdict = "contract gaps" if any(f["severity"] in ("critical", "high") for f in findings) else (
        "reviewed" if findings else "clean")
    return envelope("api-contract", title or "API Contract", summary, verdict, findings,
                    artifacts={"operations": operations[:MAX_ITEMS * 2], "contract_tests": tests,
                               "title": scrub(str((spec.get("info") or {}).get("title", "")))[:200]})


def _contract_tests(method, path, op_id, params, codes, secured, body, responses) -> list[dict]:
    where = f"{method} {path}"
    out = []
    success = next((c for c in codes if c.startswith("2")), "200")

    def add(title, status, detail=""):
        out.append({"id": "", "operation": op_id, "request": where,
                    "title": title, "expected_status": status, "detail": detail})

    add("Valid request returns the documented success response", success)
    required_params = [p for p in params if p.get("required") or p.get("in") == "path"]
    for param in required_params[:6]:
        add(f"Omit required {param.get('in')} parameter '{param.get('name')}' is rejected", "400 or 422")
    for param in params:
        schema = param.get("schema") or {}
        if schema.get("maxLength"):
            add(f"'{param.get('name')}' longer than {schema['maxLength']} is rejected", "400 or 422")
        if schema.get("enum"):
            add(f"'{param.get('name')}' outside the enum is rejected", "400 or 422",
                "allowed: " + ", ".join(map(str, schema["enum"][:8])))
        if schema.get("maximum") is not None:
            add(f"'{param.get('name')}' above maximum {schema['maximum']} is rejected", "400 or 422")
        if len(out) >= 14:
            break
    if secured:
        add("Request without credentials is rejected", "401")
    if "403" in codes:
        add("Authenticated user without permission is refused", "403")
    if "404" in codes and any(p.get("in") == "path" for p in params):
        add("Unknown resource identifier returns not found", "404")
    if body:
        add("Request body with a missing required property is rejected", "400 or 422")
        add("Request body with a wrong type is rejected", "400 or 422")
    if method in ("POST",) and "409" in codes:
        add("Duplicate creation returns a conflict", "409")
    return out
