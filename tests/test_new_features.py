"""Тесты: тихий запуск батников (без check_updates), порядок автоподбора, новые ключи конфига."""
from pathlib import Path

import zapret_core as z


def test_quiet_bat_strips_update_check(tmp_path):
    bat = tmp_path / "general.bat"
    bat.write_bytes(b"@echo off\r\ncall service.bat status_zapret\r\n"
                    b"call service.bat check_updates\r\nstart winws.exe\r\n")
    tmp = z._make_quiet_bat(bat)
    assert tmp is not None and tmp.name.startswith(z.TMP_BAT_PREFIX)
    data = tmp.read_bytes()
    assert b"check_updates" not in data
    assert b"status_zapret" in data and b"start winws.exe" in data
    assert b"check_updates" in bat.read_bytes()          # оригинал не тронут


def test_quiet_bat_none_when_no_call(tmp_path):
    bat = tmp_path / "general.bat"
    bat.write_bytes(b"@echo off\r\nstart winws.exe\r\n")
    assert z._make_quiet_bat(bat) is None


def test_tmp_bats_hidden_and_cleaned(tmp_path):
    (tmp_path / "general.bat").write_bytes(b"call service.bat check_updates\r\n")
    tmp = z._make_quiet_bat(tmp_path / "general.bat")
    assert [b.name for b in z.find_bats(tmp_path)] == ["general.bat"]
    z._cleanup_tmp_bats(tmp_path)
    assert not tmp.exists()


def test_update_flag_removed(tmp_path):
    flag = tmp_path / "utils" / "check_updates.enabled"
    flag.parent.mkdir()
    flag.write_text("")
    z._disable_update_flag(tmp_path)
    assert not flag.exists()


def test_launch_bat_runs_patched_copy(tmp_path, monkeypatch):
    bat = tmp_path / "general.bat"
    bat.write_bytes(b"call service.bat check_updates\r\n")
    calls = []
    monkeypatch.setattr(z.subprocess, "Popen", lambda args, **kw: calls.append(args))
    z.launch_bat(bat)
    assert Path(calls[0][-1]).name == z.TMP_BAT_PREFIX + "general.bat"
    calls.clear()
    z.launch_bat(bat, quiet_updates=False)
    assert Path(calls[0][-1]) == bat


def test_order_bats():
    bats = [Path(n) for n in ("a.bat", "b.bat", "c.bat", "d.bat")]
    prev = {"c.bat": "✔ 1.2с", "d.bat": "✔ 3.0с", "a.bat": "✘ x"}
    assert [b.name for b in z.order_bats(bats, "d.bat", prev)] == ["d.bat", "c.bat", "a.bat", "b.bat"]
    assert [b.name for b in z.order_bats(bats, "", prev)] == ["c.bat", "d.bat", "a.bat", "b.bat"]


def test_new_config_defaults():
    for key in ("click_to_start", "smart_order", "start_wait", "check_timeout"):
        assert key in z.DEFAULT_CONFIG
    assert all(len(c) == 3 for c in z.DISCORD_CHECKS)
