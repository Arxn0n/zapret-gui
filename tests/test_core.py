"""Запуск из корня репозитория: python -m pytest"""
import hashlib
import threading
import zipfile
from pathlib import Path

import pytest

import zapret_core as core


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(core, "VERSIONS_DIR", tmp_path / "data" / "versions")
    monkeypatch.setattr(core, "LEGACY_CONFIG", tmp_path / "nolegacy.json")
    return tmp_path


def make_zip(path: Path, files: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def test_version_key_order():
    tags = ["1.9.9", "1.10.1", "1.9.9c", "1.10.0", "1.9.10"]
    assert sorted(tags, key=core.version_key) == [
        "1.9.9", "1.9.9c", "1.9.10", "1.10.0", "1.10.1"]


def test_parse_release_picks_zip_and_digest():
    data = {
        "tag_name": "1.10.1", "name": "1.10.1", "body": "notes",
        "assets": [
            {"name": "other.rar", "browser_download_url": "http://x/o.rar"},
            {"name": "zapret-discord-youtube-1.10.1.zip",
             "browser_download_url": "http://x/z.zip", "size": 10,
             "digest": "sha256:ABCDEF"},
        ],
    }
    rel = core.parse_release(data)
    assert rel.tag == "1.10.1" and rel.url.endswith("z.zip")
    assert rel.sha256 == "abcdef"


def test_parse_release_no_zip():
    with pytest.raises(RuntimeError):
        core.parse_release({"tag_name": "1", "assets": [
            {"name": "a.rar", "browser_download_url": "http://x"}]})


def test_install_release_flat_zip_with_sha_and_user_files(sandbox):
    # старая версия с пользовательским списком
    old = core.VERSIONS_DIR / "1.9.0" / "lists"
    old.mkdir(parents=True)
    (old / "list-general-user.txt").write_text("mysite.com", encoding="utf-8")
    (old / "list-general.txt").write_text("default", encoding="utf-8")

    archive = sandbox / "z.zip"
    make_zip(archive, {"general.bat": "@echo off", "lists/list-general.txt": "x"})
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    rel = core.Release("1.10.1", "1.10.1", "z.zip", archive.as_uri(),
                       archive.stat().st_size, sha, "")

    logs = []
    path = core.install_release(rel, log=logs.append)
    assert path == core.VERSIONS_DIR / "1.10.1"
    assert (path / "general.bat").exists()
    assert (path / "lists" / "list-general-user.txt").read_text(encoding="utf-8") == "mysite.com"
    assert (path / "lists" / "list-general.txt").read_text(encoding="utf-8") == "x"
    assert any("SHA-256 совпала" in m for m in logs)
    # временные файлы убраны
    assert not [p for p in core.DATA_DIR.iterdir() if p.name.startswith("dl_")]


def test_install_release_single_top_dir_is_flattened(sandbox):
    archive = sandbox / "z.zip"
    make_zip(archive, {"zapret-1.0/general.bat": "x", "zapret-1.0/bin/winws.exe": "y"})
    rel = core.Release("1.0", "1.0", "z.zip", archive.as_uri(), 0, None, "")
    path = core.install_release(rel)
    assert (path / "general.bat").exists()
    assert (path / "bin" / "winws.exe").exists()


def test_install_release_bad_sha(sandbox):
    archive = sandbox / "z.zip"
    make_zip(archive, {"general.bat": "x"})
    rel = core.Release("2.0", "2.0", "z.zip", archive.as_uri(), 0, "0" * 64, "")
    with pytest.raises(RuntimeError, match="SHA-256"):
        core.install_release(rel)
    assert not (core.VERSIONS_DIR / "2.0").exists()


def test_install_release_cancel(sandbox):
    archive = sandbox / "z.zip"
    make_zip(archive, {"general.bat": "x"})
    rel = core.Release("3.0", "3.0", "z.zip", archive.as_uri(), 0, None, "")
    ev = threading.Event()
    ev.set()
    with pytest.raises(core.Cancelled):
        core.install_release(rel, cancel=ev)
    assert not (core.VERSIONS_DIR / "3.0").exists()
    assert not [p for p in core.DATA_DIR.iterdir() if p.name.startswith("dl_")]


def test_safe_extract_blocks_zip_slip(sandbox):
    archive = sandbox / "evil.zip"
    make_zip(archive, {"../evil.txt": "boom"})
    out = sandbox / "out"
    out.mkdir()
    with pytest.raises(RuntimeError):
        core.safe_extract(archive, out)
    assert not (sandbox / "evil.txt").exists()


def test_installed_versions_and_delete(sandbox):
    for v in ("1.9.9", "1.10.1", "1.9.10"):
        (core.VERSIONS_DIR / v).mkdir(parents=True)
    names = [d.name for d in core.installed_versions()]
    assert names == ["1.10.1", "1.9.10", "1.9.9"]
    core.delete_version(core.VERSIONS_DIR / "1.9.9")
    assert [d.name for d in core.installed_versions()] == ["1.10.1", "1.9.10"]
    with pytest.raises(ValueError):
        core.delete_version(sandbox)


def test_detect_version(sandbox):
    v = core.VERSIONS_DIR / "1.10.1"
    v.mkdir(parents=True)
    assert core.detect_version(v) == "1.10.1"

    custom = sandbox / "custom"
    custom.mkdir()
    (custom / "service.bat").write_text('set "LOCAL_VERSION=1.9.7"\n', encoding="utf-8")
    assert core.detect_version(custom) == "1.9.7"
    assert core.detect_version(sandbox / "empty") is None


def test_find_bats_skips_service_and_goes_one_level_deeper(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "general.bat").write_text("x")
    (tmp_path / "sub" / "general (ALT).bat").write_text("x")
    (tmp_path / "sub" / "service.bat").write_text("x")
    names = [b.name for b in core.find_bats(tmp_path)]
    assert names == ["general (ALT).bat", "general.bat"]
    assert core.find_file(tmp_path, "service.bat") == tmp_path / "sub" / "service.bat"
    assert core.find_bats(tmp_path / "nope") == []


def test_parse_checks_text():
    checks, errors = core.parse_checks_text(
        "# комментарий\nyt | https://youtube.com | 1000\nds|https://discord.com\nbad line\nx | ftp://a | 1\ny | https://a | abc")
    assert checks == [["yt", "https://youtube.com", 1000], ["ds", "https://discord.com", 1]]
    assert len(errors) == 3
    assert core.parse_checks_text(core.format_checks(core.DEFAULT_CHECKS))[0] == core.DEFAULT_CHECKS


def test_config_roundtrip_and_unknown_keys(sandbox):
    p = sandbox / "cfg.json"
    p.write_text('{"folder": "C:/z", "garbage": 1}', encoding="utf-8")
    cfg = core.Config(p)
    assert cfg["folder"] == "C:/z"
    assert "garbage" not in cfg.data
    cfg["tray"] = True
    assert core.Config(p)["tray"] is True


def test_scan_strategies_flow(monkeypatch, tmp_path):
    bats = [tmp_path / n for n in ("a.bat", "b.bat", "c.bat")]
    calls = []
    monkeypatch.setattr(core, "stop_zapret", lambda: calls.append("stop"))
    monkeypatch.setattr(core, "launch_bat", lambda b: calls.append(b.name))
    monkeypatch.setattr(core, "wait_for_winws", lambda t: True)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)

    # контроль без zapret: провал; a: провал, b: успех, c: успех
    seq = iter([({"x": False}, 1.0), ({"x": False}, 4.0),
                ({"x": True}, 2.0), ({"x": True}, 1.0)])
    monkeypatch.setattr(core, "run_checks", lambda checks, timeout=0: next(seq))

    out = []
    res = core.scan_strategies(
        bats, [["x", "http://x", 1]], stop_first=False, cancel=threading.Event(),
        progress=lambda *a: None,
        result=lambda bat, text, ok, el: out.append((bat.name, text)),
        say=lambda t: None)
    assert res.status == "ok"
    assert [w[1].name for w in res.winners] == ["b.bat", "c.bat"]
    assert min(res.winners)[1].name == "c.bat"
    assert ("a.bat", "✘ x") in out and ("b.bat", "✔ 2.0с") in out


def test_scan_stop_first_and_baseline(monkeypatch, tmp_path):
    bats = [tmp_path / n for n in ("a.bat", "b.bat")]
    monkeypatch.setattr(core, "stop_zapret", lambda: None)
    monkeypatch.setattr(core, "launch_bat", lambda b: None)
    monkeypatch.setattr(core, "wait_for_winws", lambda t: True)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)

    seq = iter([({"x": False}, 1.0), ({"x": True}, 2.0)])
    monkeypatch.setattr(core, "run_checks", lambda checks, timeout=0: next(seq))
    res = core.scan_strategies(bats, [["x", "http://x", 1]], stop_first=True,
                               cancel=threading.Event(), progress=lambda *a: None,
                               result=lambda *a: None, say=lambda t: None)
    assert res.status == "ok" and len(res.winners) == 1 and res.last.name == "a.bat"

    monkeypatch.setattr(core, "run_checks", lambda checks, timeout=0: ({"x": True}, 1.0))
    res = core.scan_strategies(bats, [["x", "http://x", 1]], stop_first=True,
                               cancel=threading.Event(), progress=lambda *a: None,
                               result=lambda *a: None, say=lambda t: None)
    assert res.status == "baseline_ok"


def test_discover_builds(sandbox):
    root = sandbox / "home"
    (root / "a" / "zapret").mkdir(parents=True)
    (root / "a" / "zapret" / "general.bat").write_text("x")
    (root / "b" / "c" / "d" / "e").mkdir(parents=True)              # слишком глубоко
    (root / "b" / "c" / "d" / "e" / "general.bat").write_text("x")
    (root / "AppData" / "x").mkdir(parents=True)                     # пропускаемая папка
    (root / "AppData" / "x" / "general.bat").write_text("x")
    (root / "f" / "bin").mkdir(parents=True)                         # признак: bin/winws.exe
    (root / "f" / "bin" / "winws.exe").write_text("x")
    (root / "g").mkdir()
    (root / "g" / "readme.txt").write_text("x")

    found = {p.relative_to(root).as_posix() for p in core.discover_builds([(root, 3)])}
    assert found == {"a/zapret", "f"}

    # папки, установленные самим GUI, не попадают в выдачу
    managed = core.DATA_DIR / "versions" / "1.0"
    managed.mkdir(parents=True)
    (managed / "general.bat").write_text("x")
    assert managed not in core.discover_builds([(core.DATA_DIR, 3)])

    # лимит времени / отмена
    ev = threading.Event()
    ev.set()
    assert core.discover_builds([(root, 3)], cancel=ev) == []


def test_managed_and_norm_path(sandbox):
    v = core.VERSIONS_DIR / "1.0"
    v.mkdir(parents=True)
    assert core.is_managed(v)
    assert not core.is_managed(sandbox)
    assert core.norm_path(sandbox / "a" / ".." / "b") == core.norm_path(sandbox / "b")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
