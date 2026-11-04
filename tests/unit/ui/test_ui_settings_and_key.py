"""The settings window and the API-key dialog."""

from __future__ import annotations

import threading
import tomllib

import pytest
from ui_qt_helpers import get_app, pump

from gloss.core.config import load_config
from gloss.core.contracts import PermanentError
from gloss.ui.api_key_dialog import ApiKeyDialog, looks_like_a_key
from gloss.ui.settings_window import SettingsWindow, hotkey_to_sequence, sequence_to_hotkey

GOOD_KEY = "k" * 40


class FakeStore:
    """A SecretStore that keeps values in memory. Never touches Credential Manager."""

    def __init__(self, fail: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.fail = fail

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        if self.fail:
            raise PermanentError("the vault is locked")
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "config.toml"


@pytest.fixture
def window(paths):
    get_app()
    config = load_config(user_path=paths).config
    win = SettingsWindow(config, user_path=paths, can_change_key=True)
    yield win
    win.close()


def written(paths):
    with paths.open("rb") as handle:
        return tomllib.load(handle)


# ---- hotkey text ---------------------------------------------------------------------------


def test_qt_key_text_becomes_config_text_and_back():
    assert sequence_to_hotkey("Ctrl+Alt+T") == "ctrl+alt+t"
    assert sequence_to_hotkey("Meta+Shift+F5") == "win+shift+f5"
    assert sequence_to_hotkey("") == ""
    assert hotkey_to_sequence("ctrl+alt+t").toString() == "Ctrl+Alt+T"


def test_the_window_shows_the_current_settings(window):
    assert window.mode.currentData() == "panel"
    assert window.current_hotkeys() == {
        "toggle": "ctrl+alt+t",
        "select_region": "ctrl+alt+r",
        "pause": "ctrl+alt+p",
    }
    assert window.languages["zh"].isChecked() and window.languages["ja"].isChecked()
    assert window.cloud.isChecked()


# ---- saving --------------------------------------------------------------------------------


def test_nothing_changed_writes_nothing(window, paths):
    window.save()
    assert not paths.exists()


def test_only_changed_values_are_written(window, paths):
    window.mode.setCurrentIndex(window.mode.findData("both"))
    window.font_size.setValue(20)
    window.save()
    assert written(paths) == {"config_version": 1, "render": {"mode": "both", "font_size": 20}}


def test_saved_values_load_back_through_the_config_loader(window, paths):
    window.profile.setCurrentIndex(window.profile.findData("accurate"))
    window.languages["ja"].setChecked(False)
    window.budget.setValue(25.0)
    window.hotkeys["toggle"].setKeySequence(hotkey_to_sequence("ctrl+alt+g"))
    window.save()
    loaded = load_config(user_path=paths)
    assert loaded.warnings == ()
    assert loaded.config.ocr.profile == "accurate"
    assert loaded.config.ocr.languages == ["zh", "en"]
    assert loaded.config.translate.budget.monthly_usd == 25.0
    assert loaded.config.hotkeys.toggle == "ctrl+alt+g"


def test_turning_cloud_translation_off_is_saved_in_the_privacy_table(window, paths):
    window.cloud.setChecked(False)
    window.save()
    assert written(paths)["privacy"] == {"cloud_translation": False}
    assert load_config(user_path=paths).config.privacy.cloud_translation is False


def test_saved_signal_carries_the_changes(window):
    seen: list[dict] = []
    window.saved.connect(seen.append)
    window.font_size.setValue(18)
    window.save()
    assert seen == [{"render": {"font_size": 18}}]


def test_two_actions_on_one_hotkey_are_refused_before_writing(window, paths):
    window.hotkeys["pause"].setKeySequence(hotkey_to_sequence("ctrl+alt+t"))
    window.save()
    assert not paths.exists()
    assert "same keys" in window.message.text()


def test_an_empty_hotkey_is_refused(window, paths):
    window.hotkeys["pause"].clear()
    window.save()
    assert not paths.exists()
    assert "Press the keys" in window.message.text()


def test_no_language_is_refused(window, paths):
    for box in window.languages.values():
        box.setChecked(False)
    window.save()
    assert not paths.exists()
    assert "at least one language" in window.message.text()


def test_a_value_the_config_rejects_shows_a_message_and_writes_nothing(window, paths):
    window.target.setText("e1")  # not letters only
    window.save()
    assert not paths.exists()
    assert "not valid" in window.message.text()
    assert "target_lang" in window.message.text()


def test_a_write_failure_is_explained_in_plain_words(paths):
    get_app()
    config = load_config(user_path=paths).config

    def broken(changes, **kwargs):
        raise PermissionError(13, "Access is denied")

    win = SettingsWindow(config, user_path=paths, writer=broken)
    win.font_size.setValue(22)
    win.save()
    assert "could not be written" in win.message.text()
    win.close()


def test_the_window_stays_open_for_the_shell_when_auto_close_is_off(paths):
    get_app()
    config = load_config(user_path=paths).config
    win = SettingsWindow(config, user_path=paths, auto_close=False)
    win.show()
    win.font_size.setValue(18)
    win.save()
    assert win.isVisible()
    win.finish({"toggle": "The 'toggle' hotkey ctrl+alt+g is already used by another program."})
    assert win.isVisible()
    assert "already used" in win.message.text()
    win.finish({})
    assert not win.isVisible()


def test_change_key_button_only_appears_when_a_store_exists(paths):
    get_app()
    config = load_config(user_path=paths).config
    win = SettingsWindow(config, user_path=paths, can_change_key=False)
    win.show()
    assert not win.key_button.isVisible()
    win.close()


# ---- API key dialog ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ok"),
    [("", False), ("has space " * 3, False), ("short", False), (GOOD_KEY, True)],
)
def test_key_shape_check(text, ok):
    assert (looks_like_a_key(text) is None) is ok


def test_key_is_hidden_while_typing():
    get_app()
    dialog = ApiKeyDialog(FakeStore(), "anthropic")
    assert dialog.key_edit.echoMode().name == "Password"
    dialog.show_box.setChecked(True)
    assert dialog.key_edit.echoMode().name == "Normal"
    dialog.close()


def test_saving_stores_the_key_through_the_port_and_forgets_it():
    get_app()
    store = FakeStore()
    dialog = ApiKeyDialog(store, "anthropic")
    saved: list[bool] = []
    dialog.saved.connect(lambda: saved.append(True))
    dialog.key_edit.setText(f"  {GOOD_KEY}\n")
    dialog._on_save()
    assert store.values == {"anthropic": GOOD_KEY}
    assert saved == [True]
    assert dialog.key_edit.text() == ""
    assert dialog.result() == dialog.DialogCode.Accepted


def test_a_bad_paste_is_explained_and_nothing_is_stored():
    get_app()
    store = FakeStore()
    dialog = ApiKeyDialog(store, "anthropic")
    dialog.key_edit.setText("two words")
    dialog._on_save()
    assert store.values == {}
    assert "no spaces" in dialog.message.text()
    dialog.close()


def test_a_store_failure_is_shown_and_the_dialog_stays_open():
    get_app()
    dialog = ApiKeyDialog(FakeStore(fail=True), "anthropic")
    dialog.key_edit.setText(GOOD_KEY)
    dialog._on_save()
    assert "locked" in dialog.message.text()
    assert dialog.result() != dialog.DialogCode.Accepted
    assert GOOD_KEY not in dialog.message.text()
    dialog.close()


def test_the_test_translation_runs_off_the_ui_thread_and_reports_success():
    get_app()
    ran_on: list[threading.Thread] = []

    def test() -> str:
        ran_on.append(threading.current_thread())
        return "Hello"

    dialog = ApiKeyDialog(FakeStore(), "anthropic", test=test)
    dialog.key_edit.setText(GOOD_KEY)
    dialog._on_save()
    pump(2, until=lambda: dialog.result() == dialog.DialogCode.Accepted)
    assert ran_on and ran_on[0] is not threading.main_thread()
    assert dialog.result() == dialog.DialogCode.Accepted


def test_a_failed_test_keeps_the_key_and_says_so():
    get_app()
    store = FakeStore()

    def test() -> str:
        raise RuntimeError("The key was not accepted.")

    dialog = ApiKeyDialog(store, "anthropic", test=test)
    dialog.key_edit.setText(GOOD_KEY)
    dialog._on_save()
    pump(2, until=lambda: "failed" in dialog.message.text())
    assert "The key is saved, but the test failed" in dialog.message.text()
    assert "not accepted" in dialog.message.text()
    assert dialog.save_button.isEnabled()
    assert store.values == {"anthropic": GOOD_KEY}
    dialog.close()


def test_the_ui_stays_responsive_while_the_key_test_is_slow():
    get_app()
    import time

    gate = threading.Event()

    def test() -> str:
        gate.wait(1.0)
        return "ok"

    dialog = ApiKeyDialog(FakeStore(), "anthropic", test=test)
    dialog.key_edit.setText(GOOD_KEY)
    start = time.perf_counter()
    dialog._on_save()
    assert time.perf_counter() - start < 0.2  # returned while the test is still waiting
    assert not dialog.save_button.isEnabled()
    gate.set()
    pump(2, until=lambda: dialog.result() == dialog.DialogCode.Accepted)
