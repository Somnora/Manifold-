"""Stale shipped defaults are migrated in user-owned config files.

The packaged app seeds DATA_ROOT/config.yaml once and never overwrites it,
so a corrected default (like the 900s boot timeout that cut off slow SXM
boots) would never reach existing installs. Migrations rewrite a value only
while it still equals the OLD shipped default; user-chosen values never
match and are never touched.
"""

import textwrap

from app.config import apply_config_migrations, load_settings

OLD_STYLE_CONFIG = textwrap.dedent("""\
    launch:
      max_attempts: 5
      # How long to wait for a launched instance to reach "active".
      boot_timeout_seconds: 900
      boot_poll_seconds: 10
""")


def test_stale_default_is_rewritten_and_comments_survive():
    text, applied = apply_config_migrations(OLD_STYLE_CONFIG)
    assert "boot_timeout_seconds: 2400" in text
    assert "boot_timeout_seconds: 900" not in text
    assert '# How long to wait for a launched instance' in text  # comment kept
    assert len(applied) == 1
    assert "900 -> 2400" in applied[0]


def test_user_chosen_value_is_never_touched():
    custom = OLD_STYLE_CONFIG.replace("900", "1200")
    text, applied = apply_config_migrations(custom)
    assert text == custom
    assert applied == []


def test_current_default_is_a_noop():
    current = OLD_STYLE_CONFIG.replace("900", "2400")
    text, applied = apply_config_migrations(current)
    assert text == current
    assert applied == []


def test_commented_line_is_not_migrated():
    commented = OLD_STYLE_CONFIG.replace(
        "  boot_timeout_seconds: 900", "  # boot_timeout_seconds: 900")
    text, applied = apply_config_migrations(commented)
    assert text == commented
    assert applied == []


def test_load_settings_applies_and_persists_the_migration(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(OLD_STYLE_CONFIG)
    settings = load_settings(config_path=config, env_path=tmp_path / ".env")
    # The running settings carry the fixed value...
    assert settings.launch.boot_timeout_seconds == 2400.0
    # ...and the file was rewritten so the fix survives and is visible.
    assert "boot_timeout_seconds: 2400" in config.read_text()
