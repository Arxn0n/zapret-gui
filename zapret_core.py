"""
zapret_core — логика без GUI: конфиг, скачивание/установка zapret с GitHub,
управление процессом winws.exe, проверка стратегий.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import psutil

# ------------------------------------------------------------------ константы
APP_NAME = "ZapretGUI"
REPO = "Flowseal/zapret-discord-youtube"          # официальный репозиторий
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
PROCESS_NAME = "winws.exe"
SKIP_PREFIXES = ("service",)                       # служебные батники не показываем
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
VERSIONS_DIR = DATA_DIR / "versions"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "gui.log"
LEGACY_CONFIG = Path.home() / ".zapret_gui.json"   # конфиг первого прототипа

UA_APP = f"{APP_NAME}/2.0"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# автопроверка: (название, url, минимум байт)
# min_bytes=1 -> достаточно любого ответа сервера (даже 404): TLS-соединение живое
DEFAULT_CHECKS = [
    ["страница", "https://www.youtube.com/", 40_000],
    ["картинка", "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", 8_000],
    ["видео-CDN", "https://redirector.googlevideo.com/", 1],
]
CHECK_TIMEOUT = 4.0     # максимум на одну стратегию (проверки идут параллельно)
START_WAIT = 4.0        # сколько ждать появления winws.exe после запуска батника
SETTLE = 0.8            # пауза после старта, чтобы фильтр успел подняться

DEFAULT_CONFIG = {
    "folder": "",
    "last_bat": "",
    "theme": "dark",                 # dark | light | system
    "autostart_strategy": False,     # запускать последнюю стратегию при старте GUI
    "check_updates": True,           # проверять обновления при старте GUI
    "tray": False,                   # сворачиваться в трей при закрытии окна
    "stop_on_exit": False,           # останавливать zapret при выходе из GUI
    "stop_first": True,              # автопроверка: остановиться на первой рабочей
    "checks": DEFAULT_CHECKS,
    "scan_results": {},              # {путь_к_папке: {имя_батника: "✔ 2.1с"}}
    "known_folders": [],             # сборки zapret, скачанные/найденные вне GUI
    "discovered": False,             # поиск сборок на компьютере уже выполнялся
}


class Cancelled(Exception):
    """Операция отменена пользователем."""


# ------------------------------------------------------------------ конфиг
class Config:
    def __init__(self, path: Path = None):
        self.path = Path(path or CONFIG_PATH)
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        src = self.path
        if not src.exists() and LEGACY_CONFIG.exists():
            src = LEGACY_CONFIG
        try:
            loaded = json.loads(src.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    if k in DEFAULT_CONFIG:
                        self.data[k] = v
        except Exception:
            pass

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()


# ------------------------------------------------------------------ система
def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    import ctypes
    # в собранном exe sys.argv[0] — сам exe, его передавать не нужно
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    params = " ".join(f'"{a}"' for a in args)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1)


def autostart_command(extra_args: str = "--autostart") -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {extra_args}'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")           # без консольного окна
    py = pyw if pyw.exists() else exe
    script = Path(sys.argv[0]).resolve()
    return f'"{py}" "{script}" {extra_args}'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    r = subprocess.run(["schtasks", "/Query", "/TN", APP_NAME],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Автозапуск GUI при входе в Windows через планировщик (с правами админа,
    поэтому без UAC-запроса при каждом входе)."""
    if sys.platform != "win32":
        return False, "Автозапуск поддерживается только в Windows"
    if enabled:
        cmd = ["schtasks", "/Create", "/TN", APP_NAME, "/TR", autostart_command(),
               "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"]
    else:
        cmd = ["schtasks", "/Delete", "/TN", APP_NAME, "/F"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or "schtasks вернул ошибку").strip()


# ------------------------------------------------------------------ батники и процесс
def find_bats(folder: Path) -> list[Path]:
    """Ищет .bat в папке (и на один уровень глубже — если сборка во вложенной папке)."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    bats = list(folder.glob("*.bat"))
    if not bats:
        for sub in folder.iterdir():
            if sub.is_dir():
                bats += list(sub.glob("*.bat"))
    bats = [b for b in bats if not b.name.lower().startswith(SKIP_PREFIXES)]
    return sorted(bats, key=lambda p: p.name.lower())


def find_file(folder: Path, name: str) -> Optional[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        return None
    direct = folder / name
    if direct.is_file():
        return direct
    for sub in folder.iterdir():
        if sub.is_dir() and (sub / name).is_file():
            return sub / name
    return None


def winws_processes() -> list[psutil.Process]:
    result = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == PROCESS_NAME:
                result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def stop_zapret():
    for p in winws_processes():
        try:
            p.kill()
        except Exception:
            pass
    if sys.platform == "win32":
        # иногда драйвер остаётся висеть — пробуем остановить
        for svc in ("WinDivert", "WinDivert14"):
            subprocess.run(["sc", "stop", svc], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)


def launch_bat(bat: Path):
    subprocess.Popen(["cmd.exe", "/c", str(bat)], cwd=str(Path(bat).parent),
                     creationflags=CREATE_NO_WINDOW)


def wait_for_winws(timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if winws_processes():
            return True
        time.sleep(0.2)
    return False


# ------------------------------------------------------------------ поиск уже установленных сборок
SKIP_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "system volume information", "node_modules", "__pycache__",
    "site-packages", "venv", ".venv", "steamapps", "$windows.~bt",
}


def norm_path(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_managed(folder: Path) -> bool:
    """Папка установлена самим GUI (лежит прямо в VERSIONS_DIR)."""
    try:
        return Path(folder).resolve().parent == VERSIONS_DIR.resolve()
    except Exception:
        return False


def is_build_dir(d: Path) -> bool:
    """Похоже на сборку zapret: рядом лежит general*.bat или bin/winws.exe."""
    try:
        with os.scandir(d) as it:
            for e in it:
                n = e.name.lower()
                if e.is_file() and n.startswith("general") and n.endswith(".bat"):
                    return True
    except OSError:
        return False
    return (Path(d) / "bin" / "winws.exe").is_file()


def default_search_roots() -> list[tuple[Path, int]]:
    """(папка, глубина). Специфичные места — первыми, чтобы искать в них глубже."""
    home = Path.home()
    roots: list[tuple[Path, int]] = []
    for name in ("Downloads", "Desktop", "Documents"):
        roots.append((home / name, 3))
    for var in ("OneDrive", "OneDriveConsumer"):
        od = os.environ.get(var)
        if od:
            for name in ("Desktop", "Documents", "Downloads"):
                roots.append((Path(od) / name, 3))
    try:
        here = Path(sys.argv[0]).resolve().parent
        roots.append((here, 3))
        roots.append((here.parent, 2))
    except Exception:
        pass
    roots.append((home, 2))
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:/")
            try:
                if drive.exists():
                    roots.append((drive, 2))
            except OSError:
                pass
    return roots


def discover_builds(roots=None, time_limit: float = 8.0,
                    cancel: threading.Event = None) -> list[Path]:
    """Ищет на диске сборки zapret (папки с general*.bat). Быстро и с лимитом по времени."""
    roots = roots if roots is not None else default_search_roots()
    deadline = time.monotonic() + time_limit
    skip_data = norm_path(DATA_DIR)
    visited: set[str] = set()
    found: dict[str, Path] = {}

    for root, max_depth in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        queue = deque([(root, 0)])
        while queue:
            if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                break
            d, lvl = queue.popleft()
            key = norm_path(d)
            if key in visited or key.startswith(skip_data):
                continue
            visited.add(key)
            if is_build_dir(d):
                found[key] = d
                continue                      # внутрь сборки не заходим
            if lvl >= max_depth:
                continue
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if (e.is_dir(follow_symlinks=False)
                                and not e.name.startswith((".", "$"))
                                and e.name.lower() not in SKIP_DIR_NAMES):
                            queue.append((Path(e.path), lvl + 1))
            except OSError:
                continue

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(found.values(), key=mtime, reverse=True)


# ------------------------------------------------------------------ проверки доступности
def http_check(url: str, min_bytes: int, timeout: float) -> bool:
    """True, если удалось скачать >= min_bytes за отведённое время."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while got < min_bytes and time.monotonic() < deadline:
                chunk = r.read(16384)
                if not chunk:
                    break
                got += len(chunk)
    except urllib.error.HTTPError:
        return min_bytes <= 1        # сервер ответил -> соединение живое
    except Exception:
        return False
    return got >= min_bytes


def run_checks(checks, timeout: float = CHECK_TIMEOUT) -> tuple[dict, float]:
    """Все проверки параллельно. Возвращает ({имя: ок?}, затраченное время)."""
    t0 = time.monotonic()
    ex = ThreadPoolExecutor(max_workers=max(1, len(checks)))
    futs = {c[0]: ex.submit(http_check, c[1], int(c[2]), timeout) for c in checks}
    done, _ = wait(futs.values(), timeout=timeout + 1)
    ex.shutdown(wait=False)
    result = {n: (f in done and bool(f.result())) for n, f in futs.items()}
    return result, time.monotonic() - t0


def parse_checks_text(text: str) -> tuple[list, list[str]]:
    """Строки вида: название | url | мин.байт  (мин.байт необязателен, по умолчанию 1)."""
    checks, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            errors.append(f"строка {n}: нужен формат «название | url | мин.байт»")
            continue
        url = parts[1]
        if not re.match(r"^https?://\S+$", url):
            errors.append(f"строка {n}: некорректный url")
            continue
        size = 1
        if len(parts) >= 3 and parts[2]:
            try:
                size = max(1, int(parts[2].replace("_", "")))
            except ValueError:
                errors.append(f"строка {n}: мин.байт должно быть числом")
                continue
        checks.append([parts[0], url, size])
    return checks, errors


def format_checks(checks) -> str:
    return "\n".join(f"{n} | {u} | {b}" for n, u, b in checks)


@dataclass
class ScanResult:
    status: str                       # ok | none | cancelled | baseline_ok
    winners: list                     # [(время, Path)]
    last: Optional[Path]              # последняя запущенная стратегия


def scan_strategies(bats, checks, *, stop_first: bool, cancel: threading.Event,
                    progress: Callable, result: Callable, say: Callable) -> ScanResult:
    """Перебирает стратегии по очереди.
    progress(i, total, bat); result(bat, текст, ok|None, секунды); say(текст)."""
    total = len(bats)
    winners: list = []
    last = None

    stop_zapret()
    time.sleep(0.5)
    say("Контрольная проверка без zapret...")
    base, _ = run_checks(checks)
    if all(base.values()):
        return ScanResult("baseline_ok", [], None)

    for i, bat in enumerate(bats, 1):
        if cancel.is_set():
            break
        progress(i, total, bat)
        result(bat, "⏳", None, 0.0)

        stop_zapret()
        time.sleep(0.3)
        launch_bat(bat)
        last = bat

        if not wait_for_winws(START_WAIT):
            result(bat, "⚠ не запустился", False, 0.0)
            continue
        time.sleep(SETTLE)

        res, elapsed = run_checks(checks)
        failed = [n for n, ok in res.items() if not ok]
        if not failed:
            winners.append((elapsed, bat))
            result(bat, f"✔ {elapsed:.1f}с", True, elapsed)
            if stop_first:
                break
        else:
            result(bat, "✘ " + ", ".join(failed), False, elapsed)

    if cancel.is_set():
        status = "cancelled"
    else:
        status = "ok" if winners else "none"
    return ScanResult(status, winners, last)


# ------------------------------------------------------------------ версии
def version_key(tag: str):
    """'1.9.9c' -> ((1, 9, 9), 'c'); годится для сравнения."""
    nums = tuple(int(x) for x in re.findall(r"\d+", tag or ""))
    m = re.search(r"([A-Za-z]+)\s*$", tag or "")
    return nums, (m.group(1).lower() if m else "")


def detect_version(folder: Path) -> Optional[str]:
    """Версия сборки: имя папки в VERSIONS_DIR или LOCAL_VERSION из service.bat."""
    folder = Path(folder)
    try:
        if folder.resolve().parent == VERSIONS_DIR.resolve():
            return folder.name
    except Exception:
        pass
    svc = find_file(folder, "service.bat")
    if svc:
        try:
            text = svc.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'LOCAL_VERSION\s*=\s*([^"\s&]+)', text)
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


def installed_versions() -> list[Path]:
    """Установленные через GUI версии, новые первыми."""
    if not VERSIONS_DIR.is_dir():
        return []
    dirs = [d for d in VERSIONS_DIR.iterdir() if d.is_dir()]
    return sorted(dirs, key=lambda d: version_key(d.name), reverse=True)


def delete_version(path: Path):
    path = Path(path).resolve()
    if path.parent != VERSIONS_DIR.resolve():
        raise ValueError("Можно удалять только версии, установленные через GUI")
    shutil.rmtree(path)


# ------------------------------------------------------------------ GitHub
@dataclass
class Release:
    tag: str
    name: str
    asset_name: str
    url: str
    size: int
    sha256: Optional[str]
    notes: str


def pick_asset(assets: list[dict]) -> dict:
    zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
    if not zips:
        names = ", ".join(a.get("name", "?") for a in assets) or "нет файлов"
        raise RuntimeError(f"В релизе нет .zip архива (есть: {names})")
    for a in zips:
        if "zapret" in a["name"].lower():
            return a
    return zips[0]


def parse_release(data: dict) -> Release:
    asset = pick_asset(data.get("assets") or [])
    digest = asset.get("digest") or ""
    sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None
    return Release(
        tag=str(data.get("tag_name") or data.get("name") or "unknown"),
        name=str(data.get("name") or data.get("tag_name") or ""),
        asset_name=asset["name"],
        url=asset["browser_download_url"],
        size=int(asset.get("size") or 0),
        sha256=sha,
        notes=str(data.get("body") or ""),
    )


def fetch_latest_release(timeout: float = 10) -> Release:
    req = urllib.request.Request(API_LATEST, headers={
        "User-Agent": UA_APP, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_release(json.loads(r.read().decode("utf-8")))


def download_file(url: str, dest: Path, progress: Callable = None,
                  cancel: threading.Event = None, expected_size: int = 0,
                  timeout: float = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA_APP})
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    done = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or expected_size or 0)
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    part.replace(dest)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_extract(zip_path: Path, target: Path):
    """Распаковка с защитой от zip-slip (выход за пределы target)."""
    target = Path(target).resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            dest = (target / member).resolve()
            if dest != target and target not in dest.parents:
                raise RuntimeError(f"Небезопасный путь в архиве: {member}")
        zf.extractall(target)


def copy_user_files(src_root: Path, dst_root: Path) -> int:
    """Переносит пользовательские списки (*user*.txt) из старой версии в новую."""
    count = 0
    src_root, dst_root = Path(src_root), Path(dst_root)
    for f in src_root.rglob("*.txt"):
        if "user" not in f.name.lower() or not f.is_file():
            continue
        rel = f.relative_to(src_root)
        out = dst_root / rel
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            count += 1
        except Exception:
            pass
    return count


def _safe_dirname(tag: str) -> str:
    return re.sub(r"[^\w.\-+]", "_", tag).strip("._") or "unknown"


def is_installed(tag: str) -> bool:
    return (VERSIONS_DIR / _safe_dirname(tag)).is_dir()


def install_release(rel: Release, *, progress: Callable = None,
                    cancel: threading.Event = None,
                    log: Callable = lambda m: None) -> Path:
    """Скачивает, проверяет sha256 (если GitHub отдал), распаковывает в
    VERSIONS_DIR/<тег>, переносит пользовательские списки из прошлой версии."""
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    target = VERSIONS_DIR / _safe_dirname(rel.tag)
    if target.exists():
        log(f"Версия {rel.tag} уже установлена")
        return target

    previous = [v for v in installed_versions()]
    tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=DATA_DIR))
    try:
        archive = tmp / rel.asset_name
        log(f"Скачиваю {rel.asset_name}...")
        download_file(rel.url, archive, progress, cancel, rel.size)

        if rel.sha256:
            if sha256_of(archive) != rel.sha256:
                raise RuntimeError("Контрольная сумма SHA-256 не совпала — файл повреждён или подменён")
            log("SHA-256 совпала")
        else:
            log("GitHub не вернул SHA-256 — проверка целостности пропущена")

        extract_dir = tmp / "x"
        extract_dir.mkdir()
        log("Распаковываю...")
        safe_extract(archive, extract_dir)

        entries = list(extract_dir.iterdir())
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else extract_dir
        shutil.move(str(root), str(target))

        if previous:
            n = copy_user_files(previous[0], target)
            if n:
                log(f"Перенесено пользовательских файлов из {previous[0].name}: {n}")
        log(f"Установлено: {rel.tag}")
        return target
    except BaseException:
        if target.exists() and not any(target.iterdir()):
            shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
