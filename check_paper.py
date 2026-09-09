"""Compatibility launcher for the installed Shepherd citation-checker package."""

import sys
import uuid
from pathlib import Path

from shepherd_citation_checker.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not any(arg == "--output" or arg.startswith("--output=") for arg in args) and not {"-h", "--help"} & set(args):
        args += ["--output", str(Path(".runs/paper-audits") / uuid.uuid4().hex[:12])]
    sys.argv[1:] = ["paper", *args]
    raise SystemExit(main())
