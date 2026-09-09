"""Render one explicit reviewer specification; no prompt patches or variants."""

import json
from copy import deepcopy
from pathlib import Path
from string import Template
from typing import Any

from ._engine.checkpoints import output_paths
from ._engine.claims import required_claims, surface


def prompt(packet, config) -> Any:
    """Render the sole reviewer specification with frozen budgets and evidence."""
    view = deepcopy(packet)
    paths = {identifier: path for path, identifier in output_paths(view["references"]).items()}
    for ref in view["references"]:
        ref.update(
            output_file=paths[ref["id"]],
            comparison_text=surface(ref["raw"]),
            required_field_checks=required_claims(ref["raw"]),
        )
    return Template(Path(__file__).with_name("reviewer.md").read_text()).substitute(
        seconds=max(15, config.provider_seconds - 15),
        searches=config.searches,
        fetches=config.fetches,
        packet=json.dumps(view, ensure_ascii=False, separators=(",", ":")),
    )
