"""Aria answer sheet: retrieval quality and rendered-document coverage."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle_engine.aria_answers import ANSWERS, select_answers, sheet_markdown

CASES = [
    ("How do I get started?", "what-is-bridle"),
    ("Do I need internet?", "internet-needed"),
    ("Which assistant should I choose for writing?", "categories"),
    ("My download failed, what now?", "download-failed"),
    ("Why won't chat start?", "chat-wont-start"),
    ("How do I use models I already downloaded?", "use-my-models"),
    ("GGUF file won't import", "import-unsupported"),
    ("How do I share Bridle with my phone?", "share-phone"),
    ("I lost my API key", "api-key-lost"),
    ("What is the fingerprint I have to trust?", "remote-trust"),
    ("My SSH key is locked", "remote-key-agent"),
    ("How do I stop it using memory when idle?", "model-sleeping"),
    ("Can you download that for me?", "aria-actions-approval"),
    ("How do I delete everything and reset?", "uninstall-reset"),
]


@pytest.mark.parametrize("query,expected", CASES)
def test_sheet_retrieves_the_right_entry(query, expected):
    hits = select_answers(query)
    assert hits, query
    assert hits[0]["id"] == expected, (query, [h["id"] for h in hits])


def test_sheet_entries_are_well_formed_and_unique():
    ids = [e["id"] for e in ANSWERS]
    assert len(ids) == len(set(ids)) and len(ids) >= 60
    for entry in ANSWERS:
        assert entry["topic"] and len(entry["questions"]) >= 2
        assert 40 <= len(entry["answer"]) <= 700, entry["id"]
        # Aria's voice: the word 'repo' (developer jargon) never appears as a token.
        assert "repo" not in entry["answer"].lower().replace("/", " ").split(), entry["id"]


def test_rendered_sheet_matches_source_data():
    rendered = sheet_markdown()
    sheet_file = Path(__file__).resolve().parents[2] / "ARIA-ANSWER-SHEET.md"
    assert sheet_file.read_text() == rendered
    for entry in ANSWERS:
        assert f"### {entry['id']}" in rendered
        assert entry["answer"] in rendered


def test_unrelated_question_gets_no_forced_match():
    assert select_answers("What is the capital of France?") == []
    assert select_answers("Write me a poem about autumn") == []


def test_canonical_answer_only_for_strong_matches():
    from bridle_engine.aria_answers import canonical_answer
    assert canonical_answer("I lost my API key, what do I do?")["id"] == "api-key-lost"
    assert canonical_answer("Why won't chat start?")["id"] == "chat-wont-start"
    assert canonical_answer("Write me a poem about autumn") is None
    assert canonical_answer("Tell me something interesting") is None
