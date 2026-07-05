.PHONY: all install dev-install test test_integration test_convergence test_e2e baseline \
	notebooks notebooks-preflight \
	lint lint-report lint-active lint-active-report lint-packages \
	format format-active format-active-report format-packages clean help typecheck typecheck-runtime \
	typecheck-shepherd2 typecheck-commons-vcs typecheck-vcs-core lock-check verify \
	test-core test-providers test-contexts test-banking test-coding test-meta test-vcs-core \
	test-shepherd2 test-commons-vcs test-kernel-v3-reference \
	build-dist check-dist publish-test publish

MAKE_PKGS := $(patsubst %/Makefile,%,$(wildcard shepherd/packages/*/Makefile shepherd/extras/*/Makefile vcs-core/packages/*/Makefile vcs-core/extras/*/Makefile))
RUFF_ACTIVE_TARGETS := \
	shepherd/__init__.py \
	shepherd/packages/*/src \
	shepherd/packages/*/tests \
	shepherd/extras/*/src \
	shepherd/extras/*/tests \
	vcs-core/packages/*/src \
	vcs-core/packages/*/tests \
	commons-vcs/src \
	commons-vcs/tests \
	shepherd2/src \
	shepherd2/tests \
	shepherd/integration-tests \
	integration-tests
UV_CACHE_DIR ?= .cache/uv
export UV_CACHE_DIR
VERIFY_IMPORTS := shepherd_core shepherd_providers shepherd_contexts shepherd_runtime \
	shepherd_export shepherd_transform shepherd_sandboxes shepherd_authoring \
	shepherd_tests shepherd_banking shepherd_coding shepherd \
	shepherd_kernel_v3_reference vcs_core commons_vcs

# Default target
all: install

# Show available commands
help:
	@echo "Shepherd Workspace Commands"
	@echo "=========================="
	@echo ""
	@echo "  make install          - Install all packages in development mode"
	@echo "  make dev-install      - Install the local editable quickstart closure with pip"
	@echo "  make notebooks        - Launch the use-case notebooks in JupyterLab"
	@echo "  make test             - Run default non-e2e workspace tests"
	@echo "  make test_integration - Run cross-package integration tests"
	@echo "  make test_convergence - Run cross-project commons-vcs convergence gate"
	@echo "  make test_e2e         - Run container/e2e tests"
	@echo "  make baseline         - Run the canonical end-to-end spine (deterministic, no API key)"
	@echo "  make lint             - Alias for lint-report during the transition"
	@echo "  make lint-report      - Report active-code lint/format debt without failing"
	@echo "  make lint-active      - Strict active-code lint/format gate"
	@echo "  make lint-packages    - Run legacy package-local lint loops"
	@echo "  make format           - Mutating autofix + format for active-code targets"
	@echo "  make format-packages  - Run legacy package-local format loops"
	@echo "  make typecheck-runtime - Type check shepherd-runtime with package-safe mypy module mapping"
	@echo "  make lock-check       - Verify root and standalone shepherd2 lockfiles"
	@echo "  make clean            - Clean build artifacts from all packages"
	@echo ""
	@echo "Publishing (single bundled shepherd-ai wheel):"
	@echo "  make build-dist       - Build the shepherd-ai sdist + wheel into dist/"
	@echo "  make check-dist       - Validate built artifacts with twine check"
	@echo "  make publish-test     - Upload dist/ to TestPyPI (needs credentials)"
	@echo "  make publish          - Upload dist/ to PyPI (needs credentials)"
	@echo ""
	@echo "Per-package commands:"
	@echo "  make test-core        - Run tests for shepherd-core"
	@echo "  make test-providers   - Run tests for shepherd-providers"
	@echo "  make test-contexts    - Run tests for shepherd-contexts"
	@echo "  make test-banking     - Run tests for shepherd-banking"
	@echo "  make test-coding      - Run tests for shepherd-coding"
	@echo "  make test-meta        - Run tests for shepherd (meta-package)"
	@echo "  make test-vcs-core    - Run tests for vcs-core"
	@echo "  make test-shepherd2    - Run shepherd2 package-local tests"
	@echo "  make test-commons-vcs - Run tests for commons-vcs"
	@echo "  make test-kernel-v3-reference - Run tests for the Shepherd kernel v3 reference package"

# Install all packages in development mode
install:
	uv sync

dev-install:
	@test -x .venv/bin/pip || { echo "Create a virtualenv first: python -m venv .venv"; exit 1; }
	.venv/bin/pip install -r requirements-dev.txt

# Launch the public visual-artifact notebooks in JupyterLab, in the project environment.
VISUAL_ARTIFACT_EXAMPLE_DIR := examples/notebooks/visual_artifact
NOTEBOOK_DIR := $(VISUAL_ARTIFACT_EXAMPLE_DIR)/notebooks
notebooks: notebooks-preflight
	uv run --group notebook jupyter lab --ServerApp.root_dir=$(NOTEBOOK_DIR)

# Each notebook run forks an isolated, reversible workspace scope, which needs a
# copy-on-write overlay backend. macOS uses the APFS clonefile carrier automatically;
# Linux needs a kernel or FUSE overlay. Install fuse-overlayfs when nothing is available.
notebooks-preflight:
	@uv run --group notebook python -c "import sys; from vcs_core.substrates import detect_overlay_backend as d; sys.exit(0 if (d() is not None or sys.platform == 'darwin') else 1)" \
	  || { \
	    echo "No copy-on-write overlay backend found; the notebooks fork an isolated scope per run and need one."; \
	    if [ "$$(uname)" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then \
	      echo "Installing fuse-overlayfs (may prompt for sudo)..."; \
	      sudo apt-get update -qq && sudo apt-get install -y -qq fuse-overlayfs; \
	    else \
	      echo "Install a FUSE overlay and retry (Debian/Ubuntu: sudo apt-get install -y fuse-overlayfs)."; \
	      exit 1; \
	    fi; \
	  }

# Run default non-e2e tests for the merged workspace.
# The two heavy segments (shepherd packages, vcs-core core) run under
# pytest-xdist (`-n auto`); the small segments stay serial because worker
# startup outweighs their runtime. VM-overlay and container tests are excluded
# here (they contend for a single shared Podman VM and are not xdist-safe) and
# run serially via `make test_e2e`.
test:
	uv run --with openai --with pytest-xdist pytest shepherd/packages/ -m "not e2e and not vm_overlay and not container" -n auto --dist worksteal -q
	uv run --with openai pytest shepherd/integration-tests/ -q
	uv run --directory shepherd2 --group test pytest -q
	uv run --directory commons-vcs --group test pytest -q
	$(MAKE) -C vcs-core/packages/core test

# Run cross-package integration tests
test_integration:
	uv run --with openai pytest shepherd/integration-tests/

# Run cross-project convergence tests that require the sibling sgc workspace.
test_convergence:
	uv run --with cryptography --with openai pytest shepherd/integration-tests/test_commons_vcs_convergence.py -q

# Run container/e2e/overlay tests separately from the default suite. These
# share a single Podman VM and are not xdist-safe, so they run serially.
test_e2e:
	uv run pytest shepherd/packages/ -m "e2e or vm_overlay or container" -q

# The canonical end-to-end baseline — the "spine is green" smoke. Deterministic:
# no API key, no Podman. Runs the dialect integration spine (the `update_readme`
# acceptance fixture, the run-driver seam, and the run-ledger / output-resolution
# boundary). Every commit keeps this green: a break means the change is wrong.
baseline:
	uv run pytest \
		shepherd/packages/dialect/tests/test_update_readme_fixture.py \
		shepherd/packages/dialect/tests/test_run_driver.py \
		shepherd/packages/dialect/tests/test_output_resolution_boundary.py \
		-q

# Report active-code lint debt without blocking. This is the pre-burn-down
# default while the repo still has a known active Ruff baseline.
lint: lint-report

lint-report: lint-active-report format-active-report

lint-active-report:
	uv run ruff check $(RUFF_ACTIVE_TARGETS) --statistics --exit-zero

format-active-report:
	@uv run ruff format $(RUFF_ACTIVE_TARGETS) --check; \
	status=$$?; \
	if [ $$status -eq 1 ]; then \
		exit 0; \
	fi; \
	exit $$status

# Strict active-code lint gate. This is expected to fail until follow-up
# burn-down slices clear the current active Ruff baseline.
lint-active:
	uv run ruff check $(RUFF_ACTIVE_TARGETS)
	uv run ruff format $(RUFF_ACTIVE_TARGETS) --check

# Legacy package-local lint loop.
lint-packages:
	@for pkg in $(MAKE_PKGS); do \
		echo ""; \
		echo "=== Linting $$pkg ==="; \
		$(MAKE) -C $$pkg lint || exit 1; \
	done

# Format active code.
format: format-active

format-active:
	uv run ruff check --fix $(RUFF_ACTIVE_TARGETS) --exit-zero
	uv run ruff format $(RUFF_ACTIVE_TARGETS)

# Legacy package-local format loop.
format-packages:
	@for pkg in $(MAKE_PKGS); do \
		echo ""; \
		echo "=== Formatting $$pkg ==="; \
		$(MAKE) -C $$pkg format; \
	done

# Type check all packages
typecheck:
	@for pkg in $(MAKE_PKGS); do \
		echo ""; \
		echo "=== Type checking $$pkg ==="; \
		$(MAKE) -C $$pkg typecheck || exit 1; \
	done

# Type check shepherd-runtime without letting mypy map nested repo paths as
# shepherd.packages.runtime.src.* modules. Runtime is not yet part of the
# package Makefile loop because its full lint/typecheck cleanup is still in
# progress.
typecheck-runtime:
	uv run --package shepherd-runtime --group typing mypy --config-file shepherd/packages/runtime/pyproject.toml -p shepherd_runtime

typecheck-shepherd2:
	uv run --directory shepherd2 --locked --group typing mypy src/

typecheck-commons-vcs:
	uv run --directory commons-vcs --group typing mypy src/

typecheck-vcs-core:
	uv run --directory vcs-core/packages/core --group typing mypy src/

lock-check:
	uv lock --check
	uv lock --directory shepherd2 --check

# Clean all packages
clean:
	@for pkg in $(MAKE_PKGS); do \
		$(MAKE) -C $$pkg clean; \
	done
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Per-package test shortcuts
test-core:
	$(MAKE) -C shepherd/packages/core test

test-providers:
	$(MAKE) -C shepherd/packages/providers test

test-contexts:
	$(MAKE) -C shepherd/packages/contexts test

test-banking:
	$(MAKE) -C shepherd/extras/banking test

test-coding:
	$(MAKE) -C shepherd/extras/coding test

test-meta:
	$(MAKE) -C shepherd/packages/meta test

test-vcs-core:
	$(MAKE) -C vcs-core/packages/core test

test-shepherd2:
	uv run --directory shepherd2 --group test pytest -q

test-commons-vcs:
	uv run --directory commons-vcs --group test pytest -q

test-kernel-v3-reference:
	uv run --directory shepherd/packages/kernel-v3-reference --group dev pytest -q

# Build the single bundled shepherd-ai distribution for PyPI
build-dist:
	uv run --no-project python packaging/shepherd-ai/build.py

# Validate the built artifacts render/install-check cleanly
check-dist:
	uvx twine check dist/shepherd_ai-*

# Upload to TestPyPI first for a dry run (requires TestPyPI credentials/token)
publish-test:
	uvx twine upload --repository testpypi dist/shepherd_ai-*

# Upload to PyPI (requires PyPI credentials/token for the shepherd-ai project)
publish:
	uvx twine upload dist/shepherd_ai-*

# Verify all imports work
verify:
	@echo "Verifying package imports..."
	@for module in $(VERIFY_IMPORTS); do \
		uv run python -c "import $$module; print('  $$module: OK')" || exit 1; \
	done
	@uv run --directory shepherd2 python -c "import shepherd2; print('  shepherd2: OK')"
	@echo "All imports verified!"

# --- 0.2.0 release evidence -------------------------------------------------
# Executes the Lane C per-binding jail acceptance gate (A1-A7) to a JUnit
# report, then fails unless the sentinel confirms all 10 gate ids were
# collected AND passed with the 7 jailed legs skip-free. The release evidence
# packet and the public macOS CI job both cite this target, so "proven
# executed, not skipped" is the same mechanism in both places. Native syscall
# jail evidence requires a jail-capable host (macOS Seatbelt / Linux Landlock).
.PHONY: release-evidence-lane-c
release-evidence-lane-c:
	@mkdir -p tmp/release-evidence
	-uv run --directory shepherd/packages/dialect --group dev pytest tests/test_lane_c_acceptance_gate.py \
		-rA -q --junitxml=$(CURDIR)/tmp/release-evidence/lane-c.junit.xml
	uv run python scripts/check_executed_evidence.py \
		--junitxml tmp/release-evidence/lane-c.junit.xml --profile lane-c
