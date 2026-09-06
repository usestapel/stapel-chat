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
# Raised again in 0.8.1, from 5800, by 200: `chat.post_system_message` is the
# first WRITE on this module's comm surface, and the line that cannot be cut
# is the one saying WHY it is shaped so narrowly — sender null and kind system
# hard-coded, with no way for a caller to name an author. An agent that reads
# only "posts a system message" writes the general `chat.post_message` it was
# reaching for, which is a bus-reachable way to put words in a user's mouth in
# a product where the thread is the record of a deal.
# Trim before raising it again.
# Raised again in 0.8.2, from 6000, by 200: the conversation list gained
# `search` and `unread`, and the line that cannot be cut is the one saying
# WHICH three fields a search reads — the counterpart's display name, the
# subject card's title and the LAST line — because they are the three an inbox
# row draws. An agent that reads only "there is a search parameter" writes the
# body-icontains it was reaching for: that finds rows by an old message, a
# tombstone or a system marker (text nobody can see), and misses the two fields
# that are not in this database at all. The surface entry it costs is the one
# that keeps a fifty-row inbox from costing fifty unread counts.
# Trim before raising it again.
# Raised again in 0.8.3, from 6200, by 400: a conversation-list row now carries
# the LINE IT DRAWS (`last_message`), and the lines that cannot be cut are the
# two an agent gets wrong on its own. First, that the preview and `?search=`
# read ONE rule (`drawn_last_line`) — an agent that writes its own truncation
# ships rows found by words their preview does not contain. Second, that the
# last line is the newest message BY SEQ and never `Conversation.last_seq`:
# that counter doubles as the revision journal, so an agent anchoring on it
# writes code that goes blank for every thread anybody ever edited. The five
# surface entries this costs are the rule, the page annotation, its per-row
# fallback, the truncation and the system-marker label lookup.
# Trim before raising it again.
# Raised again in 0.8.5, from 6600, by 400: a person can now LEAVE a thread,
# which is two surface entries and a verb whose whole content is what it does
# NOT do. The lines that would have to go to fit 6600 are the three an agent
# gets wrong on its own, every time. First, that DELETE on a conversation
# hides it for the caller and deletes nothing — an agent that reads only
# "DELETE leaves the conversation" writes the row deletion it was reaching
# for, which takes the read markers, a direct thread's identity and the other
# party's history with it. Second, that an AUTHORED message resurfaces the
# thread and a SYSTEM line resurfaces nobody: without that clause the
# departure line puts the thread straight back in the leaver's inbox with
# their own goodbye on it. Third, that the inbox rule is one function
# (`inbox_of`) the list, the badge and the unread chip all read — a filter on
# `participants__user` alone shows threads the caller left, and split across
# two filter() calls it matches "is you" against "has not left" on different
# rows, which is every thread with two people in it.
# Trim before raising it again.
# Raised again in 0.8.6, from 7000, by 400: leaving became UNDOABLE, which is
# two more surface entries and the half of 0.8.5 that was missing. The lines
# that would have to go to fit 7000 are the two an agent cannot write correct
# code without. First, that `left_of` is the exact COMPLEMENT of `inbox_of`
# and orders on an annotated `viewer_left_at`: an agent that filters
# `participants__left_at__isnull=False` across two calls lists every thread
# anybody ever walked out of, and one that orders on the joined column sorts a
# thread both parties left by whichever departure the join produced. Second,
# that rejoining clears `left_at` and NOTHING else — an agent that "refreshes"
# the row by marking it read destroys the badge the leaver is coming back for,
# and one that stamps `updated_at` shuffles the inbox once per restored
# thread. Neither entry is decoration: the whole verb is what it does not
# touch, and that is exactly what a one-line summary drops.
# Trim before raising it again.
LLMS_BUDGET ?= 7400

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
