"""Chat's docs name the substrate's terminal close codes, not their own set.

MODULE.md once called 4401 terminal: a client built from it never refreshed an
expired token (every session died at expiry) and kept retrying 4410.
"""
import re
from pathlib import Path

from stapel_realtime.close_codes import TERMINAL_CLOSE_CODES

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["MODULE.md", "CONFIG.MD", "README.md", "docs/llms.txt"]


def _sentences(text: str):
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if any(line.lstrip().startswith("|") for line in lines):
            yield from lines  # a table row is its own statement
            continue
        yield from re.split(r"(?<=[.;])\s", re.sub(r"\s+", " ", block))


def terminal_sentences(text: str):
    for sentence in _sentences(text):
        low = sentence.lower()
        if "terminal" in low and "not terminal" not in low:
            codes = {int(c) for c in re.findall(r"\b4\d{3}\b", sentence)}
            if codes:
                yield sentence, codes


def test_every_terminal_sentence_names_exactly_the_substrate_canon():
    seen = 0
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        for sentence, codes in terminal_sentences(path.read_text()):
            seen += 1
            assert codes == set(TERMINAL_CLOSE_CODES), f"{name}: {sentence!r}"
    assert seen, "MODULE.md must spell out the terminal close codes"
