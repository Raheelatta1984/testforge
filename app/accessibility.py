"""WCAG 2.x A/AA accessibility subset, deterministic and model-free.

Ten rules over a page the explorer already fetched (:class:`app.explorer.Page`),
each with a stable rule id, the WCAG criterion it evidences, its level (A/AA)
and an axe-style impact (critical/serious/moderate/minor). Results persist rule
ids + impact twice: inside the explorer report's ``accessibility`` section and
as a JSON artefact per ``POST /api/accessibility/scan`` call.

Honest scope, stated on every report:

* Labels, names, roles, landmarks, heading order, duplicate ids, document
  language/title and focus order (positive tabindex) are checked from static
  HTML and work in the default browserless scan.
* Contrast (1.4.3, AA) needs colours. Static scans check inline ``style``
  attributes that declare *both* foreground and background on one element; a
  rendered scan (``TF_EXPLORER_BROWSER=1``) additionally checks computed-style
  samples collected in-page. A page with no attributable pairs reports the
  rule as not-checked rather than passing it silently.
* Not claimed: full axe-core parity, focus-visibility in every state, motion,
  timing, or anything that needs a real assistive technology.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from app.qa_agents.common import clip

# Rule id -> (title, WCAG criterion, level, impact, description).
RULES = {
    "TF-A11Y-01": ("Page has a language", "3.1.1", "A", "serious",
                   "The <html> element declares lang so screen readers pronounce correctly."),
    "TF-A11Y-02": ("Page has a title", "2.4.2", "A", "moderate",
                   "A non-empty <title> names the page in history, tabs and readers."),
    "TF-A11Y-03": ("Images have alt text", "1.1.1", "A", "serious",
                   "Every image carries alt text (empty alt is a deliberate choice the scan cannot see)."),
    "TF-A11Y-04": ("Controls have names", "4.1.2", "A", "serious",
                   "Every link, button and field exposes an accessible name."),
    "TF-A11Y-05": ("Fields have labels", "3.3.2", "A", "serious",
                   "Every text-like input has a <label>, aria-label(ledby) or title."),
    "TF-A11Y-06": ("Ids are unique", "4.1.1", "A", "moderate",
                   "Duplicate ids break label targeting and ARIA references."),
    "TF-A11Y-07": ("Page has a main landmark", "1.3.1", "A", "moderate",
                   "A <main> (or role=main) landmark scopes the primary content."),
    "TF-A11Y-08": ("Headings do not skip levels", "1.3.1", "A", "moderate",
                   "Heading levels increase by one, so the outline is navigable."),
    "TF-A11Y-09": ("No positive tabindex", "2.4.3", "A", "serious",
                   "Positive tabindex values hijack the keyboard focus order."),
    "TF-A11Y-10": ("Text contrast meets 4.5:1", "1.4.3", "AA", "serious",
                   "Normal text reaches 4.5:1 against its background (3:1 for large text)."),
}

NAMED_COLOURS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192),
    "maroon": (128, 0, 0), "navy": (0, 0, 128), "teal": (0, 128, 128),
    "orange": (255, 165, 0), "purple": (128, 0, 128),
}

_HEX = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", re.I)
_RGB = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*[\d.]+)?\s*\)$", re.I)
_STYLE_DECL = re.compile(r"([a-z-]+)\s*:\s*([^;]+)")


def parse_colour(value) -> tuple[int, int, int] | None:
    """#rgb, #rrggbb, rgb()/rgba() and a small named map. None when unknown."""
    text = str(value or "").strip().lower()
    if text in NAMED_COLOURS:
        return NAMED_COLOURS[text]
    match = _HEX.match(text)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        return tuple(int(digits[index: index + 2], 16) for index in (0, 2, 4))
    match = _RGB.match(text)
    if match:
        channels = tuple(int(match.group(index)) for index in (1, 2, 3))
        if all(0 <= channel <= 255 for channel in channels):
            return channels
    return None


def _luminance(colour) -> float:
    def channel(value: int) -> float:
        part = value / 255.0
        return part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4
    red, green, blue = colour
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast_ratio(foreground, background) -> float:
    """WCAG contrast ratio: 1.0 (identical) to 21.0 (black on white)."""
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = (first, second) if first >= second else (second, first)
    return round((lighter + 0.05) / (darker + 0.05), 2)


class _StructureParser(HTMLParser):
    """The structure the explorer's collector does not keep: lang, landmarks,
    heading order, label associations, tabindex and inline colour pairs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.landmarks: set[str] = set()
        self.headings: list[tuple[int, str]] = []
        self.labels: dict[str, str] = {}       # id/name -> label text
        self.inputs: list[dict] = []
        self.tabindex: list[dict] = []
        self.colour_pairs: list[dict] = []
        self._label_for = None
        self._label_text: list[str] = []
        self._label_inputs: list[dict] = []
        self._heading_level = 0
        self._heading_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(name).lower(): (value or "") for name, value in attrs}
        if tag == "html" and attrs.get("lang", "").strip():
            self.lang = attrs["lang"].strip()[:35]
        if tag == "main" or attrs.get("role") == "main":
            self.landmarks.add("main")
        if tag in ("header", "footer", "nav", "aside", "search") or attrs.get("role") in {
                "banner", "contentinfo", "navigation", "complementary", "search"}:
            self.landmarks.add(tag if tag != "search" else "search")
        if re.fullmatch(r"h[1-6]", tag):
            self._heading_level = int(tag[1])
            self._heading_text = []
        if tag == "label":
            self._label_for = attrs.get("for", "").strip()[:200]
            self._label_text = []
            self._label_inputs = []
        if tag == "input":
            record = {"id": attrs.get("id", "")[:200], "name": attrs.get("name", "")[:200],
                      "type": (attrs.get("type") or "text").lower()[:40],
                      "aria": bool(attrs.get("aria-label") or attrs.get("aria-labelledby")),
                      "title": bool(attrs.get("title", "").strip()),
                      "labelled": False}
            self.inputs.append(record)
            if self._label_for is not None:
                self._label_inputs.append(record)
        tabindex = attrs.get("tabindex", "").strip()
        if tabindex:
            try:
                value = int(tabindex)
            except ValueError:
                value = None
            self.tabindex.append({"tag": tag, "id": attrs.get("id", "")[:120], "value": value})
        style = attrs.get("style", "")
        if style and "color" in style.lower():
            declarations = dict((key.strip().lower(), item.strip())
                                for key, item in _STYLE_DECL.findall(style))
            foreground = parse_colour(declarations.get("color", ""))
            background = parse_colour(declarations.get("background-color", "") or
                                      declarations.get("background", ""))
            if foreground and background:
                self.colour_pairs.append({"tag": tag, "id": attrs.get("id", "")[:120],
                                          "fg": declarations["color"][:60],
                                          "bg": (declarations.get("background-color")
                                                 or declarations["background"])[:60],
                                          "ratio": contrast_ratio(foreground, background)})

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) and self._heading_level:
            text = " ".join(part for part in self._heading_text if part).strip()
            self.headings.append((self._heading_level, clip(text, 120)))
            self._heading_level = 0
        if tag == "label" and self._label_for is not None:
            text = " ".join(part for part in self._label_text if part).strip()
            if self._label_for:
                self.labels[self._label_for] = clip(text, 120)
            for record in self._label_inputs:
                record["labelled"] = True
            self._label_for = None

    def handle_data(self, data):
        if self._heading_level:
            self._heading_text.append(data.strip())
        if self._label_for is not None:
            self._label_text.append(data.strip())


def _accessible_name(item: dict) -> str:
    for key in ("aria_label", "title", "alt", "placeholder", "text", "name"):
        value = str(item.get(key) or "").strip()
        if value and not (key == "text" and item.get("tag") == "input"):
            return value
    return ""


def _result(rule_id: str, status: str, detail: str, nodes=None) -> dict:
    title, wcag, level, impact, _ = RULES[rule_id]
    return {"id": rule_id, "title": title, "wcag": wcag, "level": level,
            "impact": impact, "status": status, "detail": detail,
            "nodes": (nodes or [])[:20]}


def scan_page(page) -> dict:
    """Run the ten-rule subset over an explorer page. Pure and bounded."""
    structure = _StructureParser()
    try:
        structure.feed(page.body or "")
    except Exception:  # malformed markup is data, not a crash
        pass
    rules = []

    rules.append(_result(
        "TF-A11Y-01", "pass" if structure.lang else "fail",
        f"html lang=\"{structure.lang}\"" if structure.lang else "the <html> element declares no lang"))

    rules.append(_result(
        "TF-A11Y-02", "pass" if str(page.title or "").strip() else "fail",
        "the page has a title" if str(page.title or "").strip() else "the page has no <title>"))

    unlabelled_images = [image for image in (page.images or [])
                         if not str(image.get("alt") or "").strip()]
    rules.append(_result(
        "TF-A11Y-03", "pass" if not unlabelled_images else "fail",
        "every image has alt text" if not unlabelled_images
        else f"{len(unlabelled_images)} image(s) without alt text",
        [{"target": clip(str(image.get("src") or ""), 200)} for image in unlabelled_images]))

    labelled_inputs = set(structure.labels)
    for record in structure.inputs:
        if record["labelled"]:
            labelled_inputs.add(record["id"])
            labelled_inputs.add(record["name"])
    labelled_inputs.discard("")
    unnamed = []
    for item in (page.interactive or []):
        tag = str(item.get("tag") or "").lower()
        kind = str(item.get("type") or "").lower()
        if item.get("hidden") or kind in ("hidden", "submit", "button", "reset", "image"):
            continue
        if tag not in ("a", "button", "input", "select", "textarea"):
            continue
        if tag == "a" and not item.get("href"):
            continue
        if tag == "input" and (str(item.get("id") or "") in labelled_inputs
                               or str(item.get("name") or "") in labelled_inputs):
            continue
        if not _accessible_name(item):
            unnamed.append(item)
    rules.append(_result(
        "TF-A11Y-04", "pass" if not unnamed else "fail",
        "every control has an accessible name" if not unnamed
        else f"{len(unnamed)} control(s) without an accessible name",
        [{"target": f"<{item.get('tag')} id={item.get('id') or '-'}>"} for item in unnamed]))

    unlabelled_fields = [record for record in structure.inputs
                         if record["type"] not in ("hidden", "submit", "button", "reset", "image")
                         and not record["labelled"] and not record["aria"] and not record["title"]
                         and record["id"] not in structure.labels and record["name"] not in structure.labels]
    rules.append(_result(
        "TF-A11Y-05", "pass" if not unlabelled_fields else "fail",
        "every field has a label" if not unlabelled_fields
        else f"{len(unlabelled_fields)} field(s) without a label",
        [{"target": f"<input id={record['id'] or '-'} name={record['name'] or '-'}>"} for record in unlabelled_fields]))

    counts: dict[str, int] = {}
    for value in (page.ids or []):
        counts[value] = counts.get(value, 0) + 1
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    rules.append(_result(
        "TF-A11Y-06", "pass" if not duplicates else "fail",
        "every id is unique" if not duplicates else f"{len(duplicates)} duplicated id(s)",
        [{"target": f"#{value}"} for value in duplicates]))

    rules.append(_result(
        "TF-A11Y-07", "pass" if "main" in structure.landmarks else "fail",
        "the page has a main landmark" if "main" in structure.landmarks
        else "no <main> or role=main landmark"))

    skips = []
    previous = 0
    for level, text in structure.headings:
        if previous and level > previous + 1:
            skips.append({"target": f"h{level}: {text}"})
        previous = level
    rules.append(_result(
        "TF-A11Y-08", "pass" if not skips else "fail",
        "headings increase by one level" if not skips
        else f"{len(skips)} heading level(s) skipped",
        skips))

    positive = [entry for entry in structure.tabindex
                if entry["value"] is not None and entry["value"] > 0]
    rules.append(_result(
        "TF-A11Y-09", "pass" if not positive else "fail",
        "focus follows the document order" if not positive
        else f"{len(positive)} element(s) with a positive tabindex",
        [{"target": f"<{entry['tag']} id={entry['id'] or '-'} tabindex={entry['value']}>"} for entry in positive]))

    failures = [pair for pair in structure.colour_pairs if pair["ratio"] < 4.5]
    rendered = [sample for sample in (getattr(page, "a11y_samples", None) or [])
                if isinstance(sample, dict)]
    for sample in rendered:
        foreground = parse_colour(sample.get("fg"))
        background = parse_colour(sample.get("bg"))
        if not foreground or not background:
            continue
        ratio = contrast_ratio(foreground, background)
        size = float(sample.get("size_px") or 0)
        threshold = 3.0 if size >= 24 or (sample.get("bold") and size >= 18.66) else 4.5
        if ratio < threshold:
            failures.append({"tag": str(sample.get("tag") or "text")[:20],
                             "id": str(sample.get("selector") or "")[:120],
                             "fg": str(sample.get("fg") or "")[:60],
                             "bg": str(sample.get("bg") or "")[:60], "ratio": ratio,
                             "rendered": True})
    if failures:
        rules.append(_result("TF-A11Y-10", "fail",
                             f"{len(failures)} text colour pair(s) below 4.5:1", failures))
    elif structure.colour_pairs or rendered:
        rules.append(_result("TF-A11Y-10", "pass",
                             "every attributable colour pair reaches 4.5:1" +
                             ("" if rendered else " (inline styles only; no rendered samples)")))
    else:
        rules.append(_result("TF-A11Y-10", "not-checked",
                             "no attributable colour pairs; run a rendered scan (TF_EXPLORER_BROWSER=1) "
                             "for computed-style contrast"))

    violations = [rule for rule in rules if rule["status"] == "fail"]
    return {"url": getattr(page, "url", ""), "rules": rules,
            "violations": [{"id": rule["id"], "title": rule["title"], "wcag": rule["wcag"],
                            "level": rule["level"], "impact": rule["impact"],
                            "detail": rule["detail"], "nodes": rule["nodes"]}
                           for rule in violations],
            "counts": {"rules": len(rules),
                       "passed": sum(1 for rule in rules if rule["status"] == "pass"),
                       "failed": len(violations),
                       "not_checked": sum(1 for rule in rules if rule["status"] == "not-checked")}}
