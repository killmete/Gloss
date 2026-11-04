"""Redaction (reversible, per-line numbering) and the glossary loader with reload."""

from __future__ import annotations

import os
import time

import pytest
from testkit.discovery import NO_USER_CONFIG, registered_names
from translate_fakes import KeySecrets  # noqa: F401

from gloss.core.config import load_config
from gloss.core.contracts import AdapterContext, TranslationItem, TranslationRequest
from gloss.core.registry import default_registry
from gloss.services.glossary import GlossaryStore, parse_glossary, resolve_glossary_path
from gloss.services.redaction import Redactor, restore

MIN = load_config(user_path=NO_USER_CONFIG).config.translate.redact_digit_runs


# ---- redaction ---------------------------------------------------------------------------


def test_default_threshold_comes_from_config():
    assert MIN == 8


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call 13800138000 now", "call [#1] now"),
        ("卡号 6222021234567890123 谢谢", "卡号 [#1] 谢谢"),
        ("12345678", "[#1]"),
        ("1234567", "1234567"),  # one short of the limit
        ("Order #123 accepted", "Order #123 accepted"),
        ("520", "520"),
        ("４１１１１１１１１１１１", "[#1]"),  # full-width digits
        ("4111 1111 1111 1111", "[#1]"),
        ("138-1234-5678", "[#1]"),
        ("2024-11-04", "2024-11-04"),  # a date is not a number to hide
        ("1 2 3 4 5 6 7 8", "1 2 3 4 5 6 7 8"),
        ("mail me a.b+c@example.co.uk please", "mail me [#1] please"),
        ("no digits here 你好", "no digits here 你好"),
        ("", ""),
    ],
)
def test_redaction_cases(text, expected):
    assert Redactor(MIN).redact(text).text == expected


def test_two_runs_get_two_placeholders_in_order():
    red = Redactor(MIN).redact("A 12345678 B 87654321")
    assert red.text == "A [#1] B [#2]"
    assert red.values == ("12345678", "87654321")


def test_a_short_group_next_to_a_long_run_does_not_hide_the_long_run_from_redaction():
    red = Redactor(MIN).redact("12345678 9")
    assert "12345678" not in red.text and red.values == ("12345678",)


def test_same_text_with_different_numbers_has_the_same_redacted_form():
    r = Redactor(MIN)
    assert r.redact("转账 11111111").text == r.redact("转账 22222222").text


@pytest.mark.parametrize(
    "text", ["call 13800138000 now", "a 12345678 b a@b.com c", "4111 1111 1111 1111", "x"]
)
def test_restore_undoes_redaction(text):
    red = Redactor(MIN).redact(text)
    assert restore(red.text, red.values) == text


def test_restore_puts_values_back_into_a_translation_that_moved_them():
    red = Redactor(MIN).redact("转账 11111111 给 22222222")
    assert restore("Send [#2] from [#1]", red.values) == "Send 22222222 from 11111111"


def test_restore_tolerates_spacing_the_model_added():
    assert restore("pay [ # 1 ] now", ("12345678",)) == "pay 12345678 now"


def test_restore_leaves_unknown_placeholders_alone():
    assert restore("see [#3]", ("1",)) == "see [#3]"
    assert restore("see [#1]", ()) == "see [#1]"


def test_the_threshold_is_configurable():
    assert Redactor(4).redact("code 1234").text == "code [#1]"
    with pytest.raises(ValueError):
        Redactor(0)


def test_nothing_sensitive_survives_in_a_redacted_request_text():
    red = Redactor(MIN).redact("我的号码13800138000, 卡 6222 0212 3456 7890")
    assert "1380" not in red.text and "6222" not in red.text


# ---- glossary ----------------------------------------------------------------------------

YAML = """
version: 1
terms:
  - {term: 老板, en: boss, note: "Paying customer"}
  - {term: 520, en: I love you}
  - {term: 派单, en: dispatch an order}
"""


def write(path, text):
    path.write_text(text, encoding="utf-8")


def bump_mtime(path):
    """Make sure a rewrite looks newer even on a coarse-timestamp filesystem."""
    stamp = time.time_ns() + 5_000_000_000
    os.utime(path, ns=(stamp, stamp))


def test_loads_the_shipped_glossary():
    root = resolve_glossary_path(load_config(user_path=NO_USER_CONFIG).config.storage.glossary_path)
    store = GlossaryStore(root)
    glossary = store.current()
    assert len(glossary.entries) > 40 and glossary.version != "0"
    assert store.last_error is None
    assert any(e.term == "老板" and e.translation == "boss" for e in glossary.entries)


def test_entries_are_sorted_and_numbers_become_text(tmp_path):
    path = tmp_path / "g.yaml"
    write(path, YAML)
    entries = GlossaryStore(path).current().entries
    assert [e.term for e in entries] == sorted(e.term for e in entries)
    assert any(e.term == "520" for e in entries)
    assert next(e for e in entries if e.term == "老板").note == "Paying customer"


def test_reload_on_change_and_version_changes(tmp_path):
    path = tmp_path / "g.yaml"
    write(path, YAML)
    store = GlossaryStore(path)
    first = store.current()
    assert store.current() is first, "an unchanged file is not re-read"
    write(path, YAML.replace("boss", "sir"))
    bump_mtime(path)
    second = store.current()
    assert second.version != first.version
    assert any(e.translation == "sir" for e in second.entries)


def test_version_ignores_file_order_and_the_yaml_version_field(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    write(a, "version: 1\nterms:\n  - {term: a, en: x}\n  - {term: b, en: y}\n")
    write(b, "version: 9\nterms:\n  - {term: b, en: y}\n  - {term: a, en: x}\n")
    assert GlossaryStore(a).current().version == GlossaryStore(b).current().version


def test_a_broken_edit_keeps_the_last_good_glossary(tmp_path):
    path = tmp_path / "g.yaml"
    write(path, YAML)
    store = GlossaryStore(path)
    good = store.current()
    write(path, "terms: [unclosed")
    bump_mtime(path)
    assert store.current() == good
    assert store.last_error and "could not be used" in store.last_error
    assert "unclosed" not in store.last_error, "file contents must not leak into messages"
    write(path, YAML.replace("boss", "chief"))
    bump_mtime(path)
    assert store.current() != good and store.last_error is None


def test_missing_file_gives_an_empty_glossary_then_loads_when_it_appears(tmp_path):
    path = tmp_path / "later.yaml"
    store = GlossaryStore(path)
    empty = store.current()
    assert empty.entries == () and empty.version == "0" and store.last_error
    write(path, YAML)
    assert len(store.current().entries) == 3 and store.last_error is None


def test_bad_entries_are_skipped_with_warnings():
    glossary, warnings = parse_glossary(
        {"terms": [{"term": "a", "en": "x"}, {"term": "b"}, "junk", {"term": " ", "en": "y"}]}
    )
    assert [e.term for e in glossary.entries] == ["a"]
    assert len(warnings) == 3


@pytest.mark.parametrize("data", [None, [], "text", {"terms": 5}])
def test_unusable_top_level_shapes_raise(data):
    with pytest.raises(ValueError):
        parse_glossary(data)


def test_relative_glossary_path_is_read_from_the_app_root(tmp_path):
    assert resolve_glossary_path("docs/g.yaml", root=tmp_path) == tmp_path / "docs" / "g.yaml"
    absolute = tmp_path / "x.yaml"
    assert resolve_glossary_path(str(absolute), root=tmp_path / "other") == absolute


# ---- the null translator -----------------------------------------------------------------


def test_null_translator_is_registered_local_and_passes_text_through():
    assert "null" in registered_names("translate")
    cls = type(default_registry.create("translate", "null", AdapterContext()))
    assert vars(cls)["LOCAL_ONLY"] is True
    adapter = default_registry.create("translate", "null", AdapterContext())
    adapter.open()
    request = TranslationRequest(
        (TranslationItem("a", "你好 🎮"), TranslationItem("b", "520")), None, "en"
    )
    response = adapter.translate(request)
    assert [(r.text, r.status, r.model) for r in response.results] == [
        ("你好 🎮", "passthrough", None),
        ("520", "passthrough", None),
    ]
    assert response.usage is None
    assert adapter.health().state == "degraded"
    adapter.close()
    adapter.close()
    assert adapter.health().state == "down"


def test_privacy_off_keeps_null_and_drops_anthropic_from_the_chain():
    registered_names("translate")
    chain = default_registry.resolve_chain("translate", "anthropic", ["null"], local_only=True)
    assert chain == ("null",)
    both = default_registry.resolve_chain("translate", "anthropic", ["null"], local_only=False)
    assert both == ("anthropic", "null")
