"""Tests for name normalization — the most critical shared function."""

from pubrecsearch.normalize import normalize_name, normalize_org_name


def test_basic_lowercase():
    assert normalize_name("JOHN DOE") == "john doe"


def test_accents_stripped():
    assert normalize_name("José García") == "jose garcia"


def test_punctuation_stripped():
    # comma → space, then multi-space collapsed to single space
    assert normalize_name("O'Brien, John") == "o'brien john"


def test_hyphen_preserved():
    assert normalize_name("Mary-Jane Watson") == "mary-jane watson"


def test_extra_whitespace():
    assert normalize_name("  John   Doe  ") == "john doe"


def test_empty():
    assert normalize_name("") == ""


def test_org_suffix_stripped():
    norm, suffix = normalize_org_name("Acme Corporation LLC")
    assert "llc" in (suffix or "").lower()
    assert "llc" not in norm


def test_idempotent():
    name = "Alice Smith-Jones"
    assert normalize_name(normalize_name(name)) == normalize_name(name)
