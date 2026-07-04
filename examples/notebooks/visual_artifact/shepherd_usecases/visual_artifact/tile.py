"""Gradient-descent infographic tile genre for the launch use cases.

This replaces the older landing-page hero genre. The artifact is intentionally
small and semantic: a correct tile shows update steps moving downhill toward a
minimum; the planted defect shows the path moving uphill. The static gate and
critic both key off that same decision-critical direction.
"""

# ruff: noqa: TC003

from __future__ import annotations

import html as _html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ARTIFACT_PATH = "index.html"
DEFAULT_MODEL = "sonnet"
DEFAULT_STRATEGIES = ("contour-map", "uphill-path")
PLANTED_FAILURE_STRATEGY = "uphill-path"
REQUEST = "Please output an infographic tile explaining gradient descent."
EXAMPLE_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = EXAMPLE_ROOT / "sample_outputs" / "gradient-tile-spike"
FIXTURE_BY_ID = {
    "contour-map": FIXTURE_ROOT / "uc1-variant-studio" / "contour-map" / "index.html",
    "uphill-path": FIXTURE_ROOT / "uc1-variant-studio" / "uphill-path" / "index.html",
    "sonnet": FIXTURE_ROOT / "uc2-model-right-sizing" / "sonnet" / "index.html",
    "haiku": FIXTURE_ROOT / "uc2-model-right-sizing" / "haiku" / "index.html",
    "opus": FIXTURE_ROOT / "uc2-model-right-sizing" / "opus" / "index.html",
    "draft_v1": FIXTURE_ROOT / "uc3-pipeline-recovery" / "draft-v1" / "index.html",
    "draft_retry": FIXTURE_ROOT / "uc3-pipeline-recovery" / "draft-retry" / "index.html",
}


@dataclass(frozen=True)
class TileBrief:
    name: str
    request: str
    required_labels: tuple[str, ...]
    format: str

    @classmethod
    def default(cls) -> TileBrief:
        return load_brief()

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "request": self.request,
            "required_labels": list(self.required_labels),
            "format": self.format,
        }


def load_brief(name: str = "gradient_tile") -> TileBrief:
    if name != "gradient_tile":
        raise ValueError(f"unknown tile brief: {name!r}")
    return TileBrief(
        name=name,
        request=REQUEST,
        required_labels=("Gradient Descent", "Start", "Step", "Minimum", "Small downhill steps reduce error."),
        format="single self-contained HTML infographic tile with inline CSS and SVG",
    )


@dataclass(frozen=True)
class TileSpec:
    id: str
    title: str = "Gradient Descent"
    caption: str = "Small downhill steps reduce error."
    style: str = "contour"
    accent: str = "#0f9f8f"
    secondary: str = "#f05d5e"
    state: str = "candidate"
    defect: str | None = None
    model: str | None = None
    cost: str | None = None


UC1_SPECS = (
    TileSpec(id="contour-map", state="selected"),
    TileSpec(id="uphill-path", accent="#986f0b", secondary="#c84b62", state="failed", defect="wrong_direction"),
)

UC2_SPECS = (
    TileSpec(id="sonnet", state="selected", model="sonnet", cost="medium"),
    TileSpec(
        id="haiku",
        style="vectors",
        accent="#7a6ff0",
        secondary="#ef6f6c",
        state="discarded",
        defect="wrong_direction",
        model="haiku",
        cost="low",
    ),
    TileSpec(id="opus", accent="#3f7fca", secondary="#d66a3f", state="not_selected", model="opus", cost="high"),
)

UC3_DRAFT = TileSpec(id="draft_v1", accent="#a65f00", secondary="#c84b62", state="discarded", defect="wrong_direction")
UC3_RETRY = TileSpec(id="draft_retry", state="selected")

SPEC_BY_ID = {spec.id: spec for spec in (*UC1_SPECS, *UC2_SPECS, UC3_DRAFT, UC3_RETRY)}


@dataclass(frozen=True)
class GateReport:
    branch: str
    render: str
    content: str
    layout: str
    scope: str
    direction: str
    assets: str = "pass"
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_row(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "render": self.render,
            "content": self.content,
            "layout": self.layout,
            "direction": self.direction,
            "assets": self.assets,
            "scope": self.scope,
            "passed": self.passed,
            "failures": list(self.failures),
        }


def spec_for(branch: str) -> TileSpec:
    return SPEC_BY_ID.get(branch, TileSpec(id=branch))


def render_static_tile(
    brief: Mapping[str, object] | TileBrief,
    branch: str,
    *,
    generator: str = "static-fixture-v0",
    model: str | None = None,
) -> str:
    del brief
    spec = spec_for(branch)
    return tile_html(spec, generator=generator, model=model)


def tile_html(spec: TileSpec, *, generator: str = "static-fixture-v0", model: str | None = None) -> str:
    fixture = _fixture_tile_html(spec, generator=generator, model=model)
    if fixture is not None:
        return fixture
    wrong = spec.defect == "wrong_direction"
    direction = "uphill" if wrong else "downhill"
    model_attr = "" if model is None else f' data-model="{_html.escape(model)}"'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html.escape(spec.title)} - {spec.id}</title>
  <style>{_css(spec)}</style>
</head>
<body>
  <main class="tile {spec.style}" data-layout="infographic-tile" data-topic="gradient-descent"
        data-direction="{direction}" data-generator="{generator}"{model_attr}>
    <section class="stage">
      <h1>Gradient Descent</h1>
      {_svg(spec, wrong_direction=wrong)}
      <p class="caption">Small downhill steps reduce error.</p>
    </section>
    <aside class="legend" aria-label="visual callouts">
      <span>Start</span>
      <span>Step</span>
      <span>Minimum</span>
    </aside>
  </main>
</body>
</html>
"""


def _fixture_tile_html(spec: TileSpec, *, generator: str, model: str | None) -> str | None:
    path = FIXTURE_BY_ID.get(spec.id)
    if path is None or not path.exists():
        return None
    return _stamp_fixture_contract(_read_fixture(path), spec=spec, generator=generator, model=model)


@cache
def _read_fixture(path: Path) -> str:
    return path.read_text()


def _stamp_fixture_contract(html: str, *, spec: TileSpec, generator: str, model: str | None) -> str:
    """Add the current gate metadata to committed spike fixtures.

    The spike-generated tiles are the visual source of truth for static mode, but
    they were produced before the migrated gate required stable layout and
    direction attributes. Stamping those attributes here keeps the fixtures
    readable while making every static branch satisfy the same contract as live
    provider output.
    """
    direction = "uphill" if spec.defect == "wrong_direction" else "downhill"
    attrs = {
        "data-layout": "infographic-tile",
        "data-topic": "gradient-descent",
        "data-direction": direction,
        "data-generator": generator,
    }
    if model:
        attrs["data-model"] = model
    stamped = _strip_known_attrs(html, attrs)
    attr_text = "".join(f' {key}="{_html.escape(value, quote=True)}"' for key, value in attrs.items())

    stamped, replaced = re.subn(
        r"<main\b([^>]*)>", lambda match: f"<main{match.group(1)}{attr_text}>", stamped, count=1
    )
    if replaced:
        return stamped

    stamped, replaced = re.subn(r"<div\b([^>]*)>", lambda match: f"<main{match.group(1)}{attr_text}>", stamped, count=1)
    if replaced:
        head, sep, tail = stamped.rpartition("</div>")
        return f"{head}</main>{tail}" if sep else stamped

    stamped, replaced = re.subn(
        r"<body\b([^>]*)>", lambda match: f"<body{match.group(1)}{attr_text}>", stamped, count=1
    )
    return stamped if replaced else stamped.replace("<html", f"<html{attr_text}", 1)


def _strip_known_attrs(html: str, attrs: Mapping[str, str]) -> str:
    stamped = html
    for attr in attrs:
        stamped = re.sub(rf"\s{re.escape(attr)}=\"[^\"]*\"", "", stamped)
    return stamped


def inject_wrong_direction(html: str) -> str:
    """Mark and redraw the path as moving uphill, independent of model behavior."""
    updated = re.sub(r'data-direction="[^"]*"', 'data-direction="uphill"', html, count=1)
    updated = updated.replace('class="path good"', 'class="path bad"')
    updated = updated.replace('id="arrowGood"', 'id="arrowBad"')
    updated = updated.replace('marker-end="url(#arrowGood)"', 'marker-end="url(#arrowBad)"')
    return re.sub(
        r'<polyline class="path (?:good|bad)"[^>]+>',
        '<polyline class="path bad" points="270,560 390,470 505,385 625,300 760,215" '
        'fill="none" stroke="#c84b62" stroke-width="28" stroke-linecap="round" '
        'stroke-linejoin="round" marker-end="url(#arrowBad)">',
        updated,
        count=1,
    )


def plant_defect(html: str, branch: str, plant_failure: bool) -> str:
    return inject_wrong_direction(html) if plant_failure and branch == PLANTED_FAILURE_STRATEGY else html


def evaluate_gate(*, branch: str, html: str, changed_paths: tuple[str, ...], brief: TileBrief) -> GateReport:
    failures: list[str] = []
    if changed_paths != (ARTIFACT_PATH,):
        scope = "fail"
        failures.append(f"unexpected changed paths: {', '.join(changed_paths) or '<none>'}")
    else:
        scope = "pass"

    lower = html.lower()
    render = "pass" if "<svg" in lower and "</html>" in lower and len(html.strip()) > 400 else "fail"
    if render == "fail":
        failures.append("html did not look like a complete renderable tile")

    missing = [label for label in brief.required_labels if label.lower() not in lower]
    content = "pass" if not missing else "fail"
    if missing:
        failures.append("missing required labels: " + ", ".join(missing))

    layout = "pass" if 'data-layout="infographic-tile"' in html and "<main" in lower else "fail"
    if layout == "fail":
        failures.append('missing data-layout="infographic-tile"')

    assets = "pass" if not _external_assets(html) else "fail"
    if assets == "fail":
        failures.append("external asset(s), must be single-file")

    wrong = 'data-direction="uphill"' in html or 'class="path bad"' in html or "wrong_direction" in html
    direction = "fail" if wrong else "pass"
    if wrong:
        failures.append("descent path moves uphill away from the minimum")

    return GateReport(
        branch=branch,
        render=render,
        content=content,
        layout=layout,
        scope=scope,
        direction=direction,
        assets=assets,
        failures=tuple(failures),
    )


def review_output_schema() -> dict[str, object]:
    verdict = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "verdict": {"enum": ["pass", "fail"]},
            "issues": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": ["id", "verdict", "rationale"],
    }
    return {
        "type": "object",
        "properties": {
            "candidates": {"type": "array", "items": verdict},
            "selected": {"type": "string"},
        },
        "required": ["candidates", "selected"],
    }


def static_review_verdicts(
    candidates: Sequence[Mapping[str, str]],
    brief: TileBrief,
    *,
    prefer: str = "contour-map",
) -> dict[str, object]:
    verdicts: list[dict[str, object]] = []
    passing: list[str] = []
    for candidate in candidates:
        report = evaluate_gate(
            branch=candidate["id"],
            html=candidate["html"],
            changed_paths=(ARTIFACT_PATH,),
            brief=brief,
        )
        verdicts.append(
            {
                "id": candidate["id"],
                "verdict": "pass" if report.passed else "fail",
                "issues": [] if report.passed else list(report.failures),
                "rationale": (
                    "The tile is self-contained and its update path descends toward the minimum."
                    if report.passed
                    else "; ".join(report.failures)
                ),
            }
        )
        if report.passed:
            passing.append(candidate["id"])
    selected = prefer if prefer in passing else (passing[0] if passing else (candidates[0]["id"] if candidates else ""))
    return {"candidates": verdicts, "selected": selected}


def review_blocks(candidates: Sequence[Mapping[str, str]]) -> str:
    return "\n\n".join(f'### candidate id="{c["id"]}"\n```html\n{c["html"]}\n```' for c in candidates)


def strategy_instruction(strategy: str, *, plant_failure: bool = True) -> str:
    del plant_failure
    if strategy == "contour-map":
        return "Create a contour-map tile where update steps descend toward the minimum."
    if strategy == "uphill-path":
        return "Create a contour-map tile; the test harness will inject the wrong-direction path."
    return f"Create the {strategy} version of the gradient-descent tile."


def visual_contract(spec: TileSpec) -> str:
    if spec.defect == "wrong_direction":
        return "Show a visible wrong-direction update path moving uphill away from the minimum."
    if spec.style == "vectors":
        return "Show a vector-field style tile with steps moving toward lower loss."
    return "Show a contour-map tile with an update path descending to the minimum."


def _external_assets(html: str) -> list[str]:
    patterns = (
        re.compile(
            r"<(?:img|link|script|iframe|source|video|audio|track|embed)\b[^>]*?\b(?:src|href)\s*=\s*['\"]?\s*(?:https?:)?//",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//", re.IGNORECASE),
        re.compile(r"@import\b[^;]*?(?:https?:)?//", re.IGNORECASE),
    )
    hits: list[str] = []
    for pattern in patterns:
        hits.extend(match.group(0) for match in pattern.finditer(html))
    return hits


def _css(spec: TileSpec) -> str:
    return f"""
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; min-height: 100%; }}
body {{
  width: 100vw;
  height: 100vh;
  display: grid;
  place-items: center;
  background: #dfe5ee;
  color: #142033;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  overflow: hidden;
}}
.tile {{
  width: min(92vw, 920px);
  aspect-ratio: 4 / 3;
  display: grid;
  grid-template-rows: 1fr auto;
  gap: 14px;
  padding: 34px;
  background: #f8fbff;
  border: 1px solid #cfd8e6;
  box-shadow: 0 24px 60px rgba(20, 32, 51, .16);
}}
.stage {{ min-width: 0; display: grid; grid-template-rows: auto 1fr auto; gap: 12px; }}
h1 {{ margin: 0; font-size: 52px; line-height: 1; letter-spacing: 0; }}
svg {{ width: 100%; height: 100%; min-height: 0; display: block; }}
.caption {{ margin: 0; font-size: 24px; font-weight: 700; color: #34415a; }}
.legend {{ display: flex; justify-content: space-between; gap: 12px; }}
.legend span {{
  min-width: 0;
  padding: 8px 12px;
  border: 1px solid #d9e1ec;
  background: white;
  font-weight: 800;
  color: #26364f;
}}
.path.good {{ stroke: {spec.accent}; }}
.path.bad {{ stroke: {spec.secondary}; }}
"""


def _svg(spec: TileSpec, *, wrong_direction: bool) -> str:
    points = "270,560 390,520 505,475 625,430 760,385"
    cls = "good"
    marker = "arrowGood"
    stroke = spec.accent
    if wrong_direction:
        points = "270,560 390,470 505,385 625,300 760,215"
        cls = "bad"
        marker = "arrowBad"
        stroke = spec.secondary
    return f"""
<svg viewBox="0 0 1200 680" role="img" aria-label="Gradient descent path">
  <defs>
    <marker id="arrowGood" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{spec.accent}"></path>
    </marker>
    <marker id="arrowBad" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{spec.secondary}"></path>
    </marker>
  </defs>
  <rect x="20" y="20" width="1160" height="620" rx="28" fill="#eef4fb"></rect>
  <ellipse cx="760" cy="470" rx="310" ry="150" fill="none" stroke="#c7d3e3" stroke-width="28"></ellipse>
  <ellipse cx="760" cy="470" rx="220" ry="105" fill="none" stroke="#d6dfec" stroke-width="24"></ellipse>
  <ellipse cx="760" cy="470" rx="120" ry="58" fill="#ffffff" stroke="#e1e8f2" stroke-width="18"></ellipse>
  <circle cx="270" cy="560" r="30" fill="#142033"></circle>
  <circle cx="760" cy="470" r="38" fill="{spec.accent}"></circle>
  <polyline class="path {cls}" points="{points}" fill="none" stroke="{stroke}" stroke-width="28"
            stroke-linecap="round" stroke-linejoin="round" marker-end="url(#{marker})"></polyline>
  <text x="205" y="625" font-size="42" font-weight="800" fill="#142033">Start</text>
  <text x="505" y="375" font-size="42" font-weight="800" fill="#142033">Step</text>
  <text x="690" y="555" font-size="42" font-weight="800" fill="#142033">Minimum</text>
</svg>
"""
