# stapel-chat — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json +
# errors.json) + capabilities.json per-module, from a single-module {chat +
# core} Django instance mounted at the canonical /chat/api/ prefix (see
# _codegen.py / _codegen_settings.py / codegen_urls.py).
#
# The authoritative CI gate is tests/test_contract.py (run under pytest); these
# targets are the dev-loop convenience.
PYTHON ?= python3

# Deliberate ceiling for docs/llms.txt, raised from the 4000 default in 0.3.0.
# The module's addressable surface grew in one release — two sockets instead of
# one, two OPEN registries (attachment types, activity states), edit/delete with
# tombstone semantics, receipts, and 19 error codes instead of 12 — and the
# lines that would have to go to fit 4000 are the ones explaining WHY a mutation
# takes a fresh revision sequence and why a delete leaves a row behind. Those are
# exactly the sentences an agent reading this file needs. Trim before raising it
# again.
# Raised again in 0.5.0, from 4600, by 200: the moderation seam is a new
# fleet-visible surface (a comm Function, a registered target type, a config
# axis) and the lines that would have to go to fit 4600 are its two
# load-bearing rules — that a tombstone reads as GONE rather than as an empty
# body, and that the type is registered only into a gap so a composite's own
# policy is never overwritten. An agent that reads neither writes the bug.
# Trim before raising it again.
# Raised again in 0.6.0, from 4800, by 800: this release adds the largest
# fleet-visible surface since 0.3.0 — a conversation's SUBJECT and the open
# registry behind it, a new emit, a new comm read, and block enforcement with
# five config axes. The lines that would have to go to fit 4800 are the three
# an agent cannot write correct code without: that a DIRECT thread's identity
# now includes its subject, that a block refusal must disclose nothing, and
# that a block provider which is present and failing answers 503 rather than
# delivering the message. An agent that reads none of them writes the bug.
# Trim before raising it again.
# Raised again in 0.7.0, from 5600, by 200: presence is a new fleet-visible
# surface — a model, a signal, three config axes and two fields on every
# participant on the wire. The lines that would have to go to fit 5600 are the
# two an agent cannot write correct code without: that presence is a fact about
# the OTHER party's own connections (never about the reader's socket, which is
# the bug this release deletes), and that "online" is the AND of a connection
# count and a lease so it degrades to offline rather than to a false yes.
# Trim before raising it again.
LLMS_BUDGET ?= 5800

.PHONY: contract contract-check

# Emit the contract triad + capabilities.json into docs/, then the fifth
# artifact docs/llms.txt (badge-canon §3, stapel_tools.llms_txt) — an
# agent-sized slice of capabilities.json (+ schema/errors/flows), rendered
# last so it always reflects this same run's triad + capabilities.json.
#
# Then README.md (stapel_tools.readme), assembled from docs/readme.md — the
# human half, the only file a person edits — plus everything emitted above.
# Badges, version, surface counts and doc links are generated, so a release
# cannot leave them behind. Edit docs/readme.md; never README.md.
contract:
	$(PYTHON) -m stapel_chat._codegen --out docs
	$(PYTHON) -m stapel_chat._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget $(LLMS_BUDGET)
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*
# (mirrors the monolith's `make codegen-check` and the frontend's `gen:*:check`).
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_chat._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_chat._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget $(LLMS_BUDGET) || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc


.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
