"""
Zapret GUI v4.

Зависимости:
    pip install -r requirements.txt      (customtkinter, psutil, pystray, Pillow)

Запуск (Windows):
    python zapret_gui.py                 # сам перезапустится с правами администратора
"""
from __future__ import annotations

import importlib
import math
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from zapret_core import (
    APP_NAME, CREATE_NO_WINDOW, DEFAULT_CHECKS, DISCORD_CHECKS, LOG_PATH, REPO,
    Cancelled, Config, delete_version, detect_version, discover_builds,
    fetch_latest_release, find_bats, find_file, format_checks, install_release,
    installed_versions, is_admin, is_autostart_enabled, is_installed, is_managed,
    launch_bat, norm_path, order_bats, parse_checks_text, relaunch_as_admin, scan_strategies,
    set_autostart, stop_zapret, version_key, wait_for_winws, winws_processes,
)

# ------------------------------------------------------------------ трей (pystray + Pillow)
pystray = Image = ImageDraw = None
TRAY_OK = False


def _load_tray() -> bool:
    global pystray, Image, ImageDraw, TRAY_OK
    try:
        import pystray as _p
        from PIL import Image as _i, ImageDraw as _d
        pystray, Image, ImageDraw, TRAY_OK = _p, _i, _d, True
    except Exception:
        TRAY_OK = False
    return TRAY_OK


_load_tray()
CAN_PIP = not getattr(sys, "frozen", False)

# ------------------------------------------------------------------ палитра (светлая, тёмная)
# Все цвета — пары, поэтому тема переключается «на лету» без перезапуска.
FONT = "Segoe UI"
BG = ("#e7ebf2", "#101217")             # окно
PAGE = ("#f2f4f8", "#171a20")           # фон вкладок
CARD = ("#ffffff", "#20242c")           # карточки
BORDER = ("#d2d8e3", "#323946")
ROW = ("#f6f8fb", "#1b1f26")
ROW_SEL = ("#e3eeff", "#1e2b44")
TEXT = ("#182031", "#e7eaf0")
MUTED = ("#66718a", "#8a93a6")
ACCENT = ("#2f6fed", "#4b8bff")
ACCENT_H = ("#2559c4", "#6aa0ff")
GHOST = ("#e4e8f0", "#2a2f3a")
GHOST_H = ("#d3d9e6", "#363c4a")
TRACK = ("#d9dfea", "#303643")
OK_TXT = ("#15803d", "#4ade80")
ERR_TXT = ("#b91c1c", "#f87171")
WARN_TXT = ("#a15c07", "#fbbf24")
WARN_BG = ("#fff3d6", "#3a2f14")
GREEN, GREEN_H = "#2e9e4f", "#247c3e"   # кнопка «Запустить» (одинаковая в обеих темах)
RED, RED_H = "#c0392b", "#992d22"
AMBER = "#d99a1e"

TAB_HOME, TAB_STRAT, TAB_INSTALL, TAB_SETTINGS, TAB_LOG = (
    "Главная", "Стратегии", "Установка", "Настройки", "Лог")
THEMES = {"Светлая": "light", "Тёмная": "dark", "Системная": "system"}
SCAN_TEXT = "🔍  Проверить все стратегии"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ------------------------------------------------------------------ утилиты оформления
def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def blend(a: str, b: str, t: float) -> str:
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(_rgb(a), _rgb(b)))


def _mi() -> int:
    """0 — светлая тема, 1 — тёмная (для случаев, когда нужен один hex-цвет)."""
    return 1 if str(ctk.get_appearance_mode()).lower() == "dark" else 0


def card(master, **kw):
    return ctk.CTkFrame(master, corner_radius=14, fg_color=CARD,
                        border_width=1, border_color=BORDER, **kw)


def label(master, text="", muted=False, size=13, bold=False, **kw):
    return ctk.CTkLabel(master, text=text, text_color=MUTED if muted else TEXT,
                        font=(FONT, size, "bold" if bold else "normal"), **kw)


def title(master, text):
    return label(master, text, size=14, bold=True, anchor="w")


def button(master, text, command, kind="ghost", **kw):
    styles = {
        "primary": dict(fg_color=ACCENT, hover_color=ACCENT_H, text_color="#ffffff"),
        "ghost": dict(fg_color=GHOST, hover_color=GHOST_H, text_color=TEXT),
        "danger": dict(fg_color=RED, hover_color=RED_H, text_color="#ffffff"),
    }
    return ctk.CTkButton(master, text=text, command=command, corner_radius=10,
                         text_color_disabled=MUTED, font=(FONT, 13), **styles[kind], **kw)


def menu(master, values, command=None, **kw):
    return ctk.CTkOptionMenu(
        master, values=values, command=command, corner_radius=10,
        fg_color=GHOST, button_color=GHOST_H, button_hover_color=ACCENT,
        text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=GHOST,
        dropdown_text_color=TEXT, font=(FONT, 13), dropdown_font=(FONT, 13), **kw)


def scroll_page(tab):
    f = ctk.CTkScrollableFrame(tab, fg_color=PAGE, corner_radius=0)
    f.pack(fill="both", expand=True)
    return f


def resource_path(rel: str) -> Path:
    """Путь к ресурсу и из исходников, и из собранного PyInstaller exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def shorten(text: str, limit: int = 46) -> str:
    return text if len(text) <= limit else text[:16] + "…" + text[-(limit - 17):]


def build_label(path: Path) -> str:
    if is_managed(path):
        return f"{path.name}  ·  установлено через GUI"
    return f"{detect_version(path) or 'версия ?'}  ·  {shorten(str(path))}"


# ------------------------------------------------------------------ виджеты
class AnimatedBar(ctk.CTkProgressBar):
    """Плавный прогресс-бар: ease-out к целевому значению, «ползёт» вперёд, пока
    нет точных данных, и умеет неопределённый режим."""

    def __init__(self, master, **kw):
        kw.setdefault("height", 10)
        kw.setdefault("corner_radius", 5)
        kw.setdefault("fg_color", TRACK)
        kw.setdefault("progress_color", ACCENT)
        super().__init__(master, **kw)
        ctk.CTkProgressBar.set(self, 0)
        self._target = 0.0
        self._shown = 0.0
        self._cap: float | None = None
        self._rate = 0.0
        self._indet = False
        self._alive = True
        self._last = time.monotonic()
        self.after(30, self._step)

    def destroy(self):
        self._alive = False
        super().destroy()

    def goto(self, value: float, animate: bool = True):
        self._cap, self._rate = None, 0.0
        self._target = max(0.0, min(1.0, value))
        if not animate:
            self._shown = self._target
            if not self._indet:
                ctk.CTkProgressBar.set(self, self._shown)

    def creep(self, cap: float, seconds: float):
        """Медленно ползти к cap за seconds секунд, пока не придёт точное значение."""
        self._cap = max(self._target, min(1.0, cap))
        self._rate = (self._cap - self._target) / max(0.1, seconds)

    def indeterminate(self, on: bool):
        if on == self._indet:
            return
        self._indet = on
        if on:
            self.configure(mode="indeterminate")
            self.start()
        else:
            self.stop()
            self.configure(mode="determinate")
            ctk.CTkProgressBar.set(self, self._shown)

    def reset(self):
        self.indeterminate(False)
        self.goto(0.0, animate=False)

    def _step(self):
        if not self._alive:
            return
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        if not self._indet:
            if self._cap is not None and self._rate > 0 and self._target < self._cap:
                self._target = min(self._cap, self._target + self._rate * dt)
            diff = self._target - self._shown
            if abs(diff) > 0.0015:
                self._shown += diff * min(1.0, dt * 9)
                ctk.CTkProgressBar.set(self, self._shown)
            elif diff:
                self._shown = self._target
                ctk.CTkProgressBar.set(self, self._shown)
        try:
            self.after(16, self._step)
        except Exception:
            pass


class StrategyRow(ctk.CTkFrame):
    """Строка списка стратегий: имя + цветной «чип» с результатом проверки."""

    def __init__(self, master, bat: Path, on_click):
        super().__init__(master, corner_radius=10, fg_color=ROW,
                         border_width=1, border_color=BORDER)
        self.bat = bat
        self.kind = "none"
        self.name_lbl = ctk.CTkLabel(self, text=bat.name, anchor="w",
                                     text_color=TEXT, font=(FONT, 13))
        self.name_lbl.pack(side="left", padx=(12, 6), pady=9)
        self.chip = ctk.CTkLabel(self, text="", anchor="e", text_color=MUTED,
                                 font=(FONT, 12, "bold"))
        self.chip.pack(side="right", padx=(6, 12))
        for w in (self, self.name_lbl, self.chip):
            w.bind("<Button-1>", lambda e, b=bat: on_click(b))
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

    def set_active(self, flag: bool):
        self.name_lbl.configure(text=("▶  " if flag else "") + self.bat.name)

    def set_selected(self, flag: bool):
        self.configure(fg_color=ROW_SEL if flag else ROW,
                       border_color=ACCENT if flag else BORDER,
                       border_width=2 if flag else 1)

    def set_result(self, text: str):
        t = (text or "").strip()
        if t.startswith("✔"):
            self.kind, color = "ok", OK_TXT
        elif t.startswith("✘"):
            self.kind, color = "fail", ERR_TXT
        elif t.startswith("⚠"):
            self.kind, color = "warn", WARN_TXT
        elif t.startswith("⏳"):
            self.kind, color = "busy", ACCENT
        else:
            self.kind, color = "none", MUTED
        self.chip.configure(text="проверка…" if self.kind == "busy" else t, text_color=color)

    def spin(self, frame: str):
        if self.kind == "busy":
            self.chip.configure(text=f"{frame} проверка")


# ------------------------------------------------------------------ приложение
class App(ctk.CTk):
    def __init__(self, autostart: bool = False):
        super().__init__()
        self.cfg = Config()
        ctk.set_appearance_mode(self.cfg["theme"])
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=BG)

        self.title("Zapret GUI")
        self.geometry("700x820")
        self.minsize(640, 700)
        self._icon = resource_path("assets/icon.ico")
        if sys.platform == "win32" and self._icon.exists():
            # customtkinter подставляет свою иконку через ~200 мс, поэтому ставим позже
            self.after(300, self._set_window_icon)

        self.q: queue.Queue = queue.Queue()         # события из потоков -> UI
        self._stop_poll = threading.Event()
        self._anim_tokens: dict[str, int] = {}

        self.folder: Path | None = None
        self.bats: list[Path] = []
        self.selected: Path | None = None
        self.results: dict[str, str] = {}
        self.rows: dict[str, StrategyRow] = {}

        self.running = False
        self.launching = False                      # батник запущен, winws ещё не поднялся
        self.started_at: float | None = None
        self.active_name: str | None = None
        self.scan_current = ""
        self._status_kind = "off"
        self._pulse_on = False
        self._card_hex: str | None = None
        self._power_fg = GREEN
        self._spin_on = False
        self._spin_i = 0

        self.scanning = False
        self._from_click = False
        self.cancel_scan = threading.Event()
        self._scan_done = 0
        self._scan_total = 1
        self.latest = None                          # последний Release с GitHub
        self.installing = False
        self.checking = False
        self.discovering = False
        self.cancel_dl = threading.Event()
        self._version_paths: dict[str, Path] = {}
        self.tray = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        hidden_start = autostart and self.cfg["tray"] and TRAY_OK
        if not hidden_start:                         # плавное появление окна
            try:
                self.attributes("-alpha", 0.0)
                self.animate("fade", 320, lambda e: self.attributes("-alpha", e))
            except Exception:
                pass

        self.log(f"{APP_NAME} запущен" + (" (без прав администратора)"
                                          if sys.platform == "win32" and not is_admin() else ""))
        self._load_initial_folder()

        if self.cfg["tray"] and TRAY_OK:
            self._tray_enable()
            if hidden_start:
                self.after(300, self.withdraw)

        self.after(50, self._pump)
        self.after(1000, self._tick)
        threading.Thread(target=self._poller, daemon=True).start()
        if self.cfg["check_updates"]:
            self.check_updates(manual=False)
        self.after(6 * 3600 * 1000, self._periodic_update_check)
        if not self.cfg["discovered"]:
            self.after(600, lambda: self.find_builds(auto=True))
        if self.cfg["autostart_strategy"] and self.selected:
            self.after(800, self.start)

    def _set_window_icon(self):
        try:
            self.iconbitmap(str(self._icon))
        except Exception:
            pass

    # ================================================================ инфраструктура
    def ui(self, fn, *args, **kwargs):
        """Вызвать fn в UI-потоке (можно из любого потока)."""
        self.q.put((fn, args, kwargs))

    def _pump(self):
        try:
            while True:
                fn, args, kwargs = self.q.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception as e:                      # не роняем UI
                    self._append_log(f"[ui error] {e!r}")
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def animate(self, key: str, ms: int, fn):
        """fn(e), e: 0→1 с ease-out. Новая анимация с тем же key отменяет прежнюю."""
        token = self._anim_tokens.get(key, 0) + 1
        self._anim_tokens[key] = token
        t0, dur = time.monotonic(), ms / 1000

        def step():
            if self._anim_tokens.get(key) != token:
                return
            t = min(1.0, (time.monotonic() - t0) / dur)
            try:
                fn(1 - (1 - t) ** 3)
            except Exception:
                return
            if t < 1:
                self.after(16, step)
        step()

    def log(self, msg: str):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        self.ui(self._append_log, line)
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        except Exception:
            pass

    def _append_log(self, line: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poller(self):
        """Следит за winws.exe в фоне (psutil на занятой системе может тормозить)."""
        while not self._stop_poll.is_set():
            try:
                running = bool(winws_processes())
            except Exception:
                running = False
            self.ui(self._on_status, running)
            self._stop_poll.wait(1.5)

    def _tick(self):
        if self.running and self.started_at is not None:
            s = int(time.monotonic() - self.started_at)
            self.uptime_lbl.configure(
                text=f"Аптайм {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}")
        else:
            self.uptime_lbl.configure(text="")
        self.after(1000, self._tick)

    # ================================================================ построение UI
    def _build_ui(self):
        self.tabs = ctk.CTkTabview(
            self, corner_radius=16, border_width=1, border_color=BORDER, fg_color=PAGE,
            segmented_button_fg_color=GHOST,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_H,
            segmented_button_unselected_color=GHOST,
            segmented_button_unselected_hover_color=GHOST_H,
            text_color=TEXT)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        for name in (TAB_HOME, TAB_STRAT, TAB_INSTALL, TAB_SETTINGS, TAB_LOG):
            self.tabs.add(name)
        self._build_home(self.tabs.tab(TAB_HOME))
        self._build_strategies(self.tabs.tab(TAB_STRAT))
        self._build_install(scroll_page(self.tabs.tab(TAB_INSTALL)))
        self._build_settings(scroll_page(self.tabs.tab(TAB_SETTINGS)))
        self._build_log(self.tabs.tab(TAB_LOG))

    # ---- главная
    def _build_home(self, tab):
        self.status_card = card(tab)
        self.status_card.pack(fill="x", pady=(4, 10))
        self.dot = ctk.CTkLabel(self.status_card, text="●", font=(FONT, 48), text_color=MUTED)
        self.dot.pack(side="left", padx=(24, 12), pady=18)
        box = ctk.CTkFrame(self.status_card, fg_color="transparent")
        box.pack(side="left", fill="x", expand=True, pady=18)
        self.status_lbl = label(box, "Остановлено", size=24, bold=True, anchor="w")
        self.status_lbl.pack(fill="x")
        self.strategy_lbl = label(box, "—", muted=True, anchor="w")
        self.strategy_lbl.pack(fill="x")
        self.uptime_lbl = label(box, "", muted=True, anchor="w")
        self.uptime_lbl.pack(fill="x")

        self.banner = ctk.CTkLabel(tab, text="", text_color=WARN_TXT, fg_color=WARN_BG,
                                   corner_radius=10, wraplength=560, justify="left",
                                   anchor="w", font=(FONT, 13))
        self._banner_shown = False
        self.banner.bind("<Button-1>", lambda e: self.tabs.set(TAB_INSTALL))
        try:
            self.banner.configure(cursor="hand2")
        except Exception:
            pass

        pick = card(tab)
        pick.pack(fill="x", pady=(0, 10))
        label(pick, "Стратегия", muted=True).pack(side="left", padx=(16, 10), pady=12)
        self.strategy_menu = menu(pick, ["—"], command=self._on_menu)
        self.strategy_menu.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=12)

        self.power_btn = ctk.CTkButton(
            tab, text="▶  Запустить", height=62, corner_radius=14,
            font=(FONT, 19, "bold"), fg_color=GREEN, hover_color=GREEN_H,
            text_color="#ffffff", text_color_disabled="#e8efe9",
            command=self.toggle_power)
        self.power_btn.pack(fill="x", pady=(0, 10))

        button(tab, "🔍  Подобрать рабочую стратегию", self.quick_scan, height=42
               ).pack(fill="x", pady=(0, 12))

        self.version_lbl = label(tab, "", muted=True, anchor="w")
        self.version_lbl.pack(fill="x", padx=4)
        self.info_lbl = label(tab, "", anchor="w", justify="left", wraplength=560)
        self.info_lbl.configure(text_color=WARN_TXT)
        self.info_lbl.pack(fill="x", padx=4, pady=(6, 0))
        if sys.platform == "win32" and not is_admin():
            self.info_lbl.configure(
                text="⚠ Нет прав администратора — WinDivert может не загрузиться")

    def _set_banner(self, text: str):
        if text:
            self.banner.configure(text="  " + text + "  ")
            if not self._banner_shown:
                self.banner.pack(fill="x", pady=(0, 10), after=self.status_card, ipady=6)
                self._banner_shown = True
        elif self._banner_shown:
            self.banner.pack_forget()
            self._banner_shown = False

    # ---- стратегии
    def _build_strategies(self, tab):
        top = card(tab)
        top.pack(fill="x", pady=(4, 10))
        self.scan_btn = button(top, SCAN_TEXT, self.toggle_scan, kind="primary", height=40)
        self.scan_btn.pack(fill="x", padx=14, pady=(14, 6))
        self.stop_first = ctk.CTkCheckBox(
            top, text="Остановиться на первой рабочей (быстрее)", text_color=TEXT,
            fg_color=ACCENT, hover_color=ACCENT_H, border_color=BORDER,
            font=(FONT, 13),
            command=lambda: self.cfg.__setitem__("stop_first", bool(self.stop_first.get())))
        if self.cfg["stop_first"]:
            self.stop_first.select()
        self.stop_first.pack(anchor="w", padx=16, pady=6)
        self.scan_bar = AnimatedBar(top)
        self.scan_bar.pack(fill="x", padx=16, pady=(6, 4))
        self.scan_lbl = label(top, "", muted=True, anchor="w")
        self.scan_lbl.pack(fill="x", padx=16, pady=(0, 12))

        lst = card(tab)
        lst.pack(fill="both", expand=True)
        title(lst, "Стратегии (.bat)").pack(fill="x", padx=16, pady=(12, 4))
        self.list_frame = ctk.CTkScrollableFrame(lst, fg_color=CARD, corner_radius=10)
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ---- установка
    def _build_install(self, page):
        src = card(page)
        src.pack(fill="x", pady=(4, 10))
        title(src, "Текущая сборка").pack(fill="x", padx=16, pady=(14, 2))
        self.folder_lbl = label(src, "не выбрана", muted=True, anchor="w",
                                wraplength=560, justify="left")
        self.folder_lbl.pack(fill="x", padx=16)
        self.cur_version_lbl = label(src, "", muted=True, anchor="w")
        self.cur_version_lbl.pack(fill="x", padx=16, pady=(0, 8))
        r = ctk.CTkFrame(src, fg_color="transparent")
        r.pack(fill="x", padx=10, pady=(0, 14))
        for text, cmd in (("Открыть папку", self.open_folder),
                          ("service.bat", self.open_service),
                          ("Добавить папку…", self.choose_folder)):
            button(r, text, cmd, width=120).pack(side="left", padx=4, expand=True, fill="x")

        upd = card(page)
        upd.pack(fill="x", pady=(0, 10))
        title(upd, "Скачивание и обновление").pack(fill="x", padx=16, pady=(14, 2))
        self.latest_lbl = label(upd, "Последняя версия: —", muted=True, anchor="w")
        self.latest_lbl.pack(fill="x", padx=16)
        r = ctk.CTkFrame(upd, fg_color="transparent")
        r.pack(fill="x", padx=10, pady=(8, 4))
        self.check_btn = button(r, "Проверить", lambda: self.check_updates(True), width=110)
        self.check_btn.pack(side="left", padx=4)
        self.install_btn = button(r, "Скачать и установить", self.install_latest,
                                  kind="primary", state="disabled")
        self.install_btn.pack(side="left", padx=4, expand=True, fill="x")
        self.dl_bar = AnimatedBar(upd)
        self.dl_bar.pack(fill="x", padx=16, pady=(8, 2))
        self.dl_lbl = label(upd, "", muted=True, anchor="w")
        self.dl_lbl.pack(fill="x", padx=16, pady=(0, 12))

        ver = card(page)
        ver.pack(fill="x", pady=(0, 10))
        title(ver, "Доступные сборки").pack(fill="x", padx=16, pady=(14, 2))
        label(ver, "Скачанные через GUI и найденные на компьютере.", muted=True,
              anchor="w").pack(fill="x", padx=16)
        r = ctk.CTkFrame(ver, fg_color="transparent")
        r.pack(fill="x", padx=10, pady=(8, 4))
        self.versions_menu = menu(r, ["—"])
        self.versions_menu.pack(side="left", padx=4, expand=True, fill="x")
        button(r, "Использовать", self.use_version, kind="primary", width=110
               ).pack(side="left", padx=4)
        button(r, "Убрать", self.delete_selected_version, kind="danger", width=80
               ).pack(side="left", padx=4)
        r2 = ctk.CTkFrame(ver, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=(4, 2))
        self.find_btn = button(r2, "🔎  Найти на компьютере", lambda: self.find_builds(False))
        self.find_btn.pack(side="left", padx=4)
        self.disc_lbl = label(r2, "", muted=True, anchor="w")
        self.disc_lbl.pack(side="left", padx=8, fill="x", expand=True)
        self.disc_bar = AnimatedBar(ver, height=6, corner_radius=3)
        self.disc_bar.pack(fill="x", padx=16, pady=(4, 14))

        label(page, (f"Источник загрузки: github.com/{REPO} (официальный репозиторий). "
                     "Клонов с похожими названиями много — часто это подделки. "
                     "Антивирус может ругаться на winws.exe/WinDivert: это известное "
                     "ложное срабатывание, но решение об исключениях — за тобой."),
              muted=True, size=12, wraplength=580, justify="left", anchor="w"
              ).pack(fill="x", padx=8, pady=(2, 8))

    # ---- настройки
    def _switch(self, parent, text, key, extra=None):
        sw = ctk.CTkSwitch(parent, text=text, text_color=TEXT, progress_color=ACCENT,
                           fg_color=TRACK, button_color=("#ffffff", "#d7dbe4"),
                           button_hover_color=("#f0f2f6", "#ffffff"), font=(FONT, 13))
        sw.configure(command=lambda: self._on_switch(key, sw, extra))
        if self.cfg[key]:
            sw.select()
        sw.pack(anchor="w", padx=16, pady=7)
        return sw

    def _build_settings(self, page):
        beh = card(page)
        beh.pack(fill="x", pady=(4, 10))
        title(beh, "Поведение").pack(fill="x", padx=16, pady=(14, 4))
        self.sw_autostart = ctk.CTkSwitch(
            beh, text="Запускать GUI при входе в Windows", text_color=TEXT,
            progress_color=ACCENT, fg_color=TRACK, font=(FONT, 13),
            button_color=("#ffffff", "#d7dbe4"), button_hover_color=("#f0f2f6", "#ffffff"),
            command=self._on_autostart_switch)
        if is_autostart_enabled():
            self.sw_autostart.select()
        self.sw_autostart.pack(anchor="w", padx=16, pady=7)
        self._switch(beh, "Запускать стратегию по клику в списке", "click_to_start")
        self._switch(beh, "Автоподбор: сначала прошлая и ранее рабочие", "smart_order")
        self._switch(beh, "Запускать последнюю стратегию при старте GUI", "autostart_strategy")
        self._switch(beh, "Проверять обновления при старте", "check_updates")
        self._switch(beh, "Останавливать zapret при выходе из GUI", "stop_on_exit")
        self.sw_tray = self._switch(beh, "Сворачиваться в трей при закрытии окна",
                                    "tray", self._apply_tray)
        self.tray_note = label(beh, "", muted=True, size=12, anchor="w",
                               wraplength=540, justify="left")
        self.tray_btn = button(beh, "Установить pystray и Pillow", self.install_tray_deps)
        self._sync_tray_ui()
        ctk.CTkFrame(beh, height=8, fg_color="transparent").pack()

        look = card(page)
        look.pack(fill="x", pady=(0, 10))
        title(look, "Тема оформления").pack(fill="x", padx=16, pady=(14, 6))
        inv = {v: k for k, v in THEMES.items()}
        self.theme_seg = ctk.CTkSegmentedButton(
            look, values=list(THEMES), command=self._on_theme, font=(FONT, 13),
            selected_color=ACCENT, selected_hover_color=ACCENT_H,
            unselected_color=GHOST, unselected_hover_color=GHOST_H,
            fg_color=GHOST, text_color=TEXT)
        self.theme_seg.set(inv.get(self.cfg["theme"], "Тёмная"))
        self.theme_seg.pack(fill="x", padx=16, pady=(0, 16))

        chk = card(page)
        chk.pack(fill="x", pady=(0, 10))
        title(chk, "Что проверять при автоподборе").pack(fill="x", padx=16, pady=(14, 2))
        label(chk, ("По строке на проверку:  название | url | мин.байт\n"
                    "мин.байт = 1 — достаточно любого ответа сервера."),
              muted=True, size=12, anchor="w", justify="left").pack(fill="x", padx=16)
        self.checks_box = ctk.CTkTextbox(
            chk, height=100, corner_radius=10, fg_color=ROW, text_color=TEXT,
            border_width=1, border_color=BORDER, font=("Consolas", 12))
        self.checks_box.insert("1.0", format_checks(self.cfg["checks"]))
        self.checks_box.pack(fill="x", padx=16, pady=8)
        r = ctk.CTkFrame(chk, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 14))
        button(r, "Сохранить", self.save_checks, kind="primary", width=110
               ).pack(side="left", padx=4)
        button(r, "По умолчанию", self.reset_checks, width=120).pack(side="left", padx=4)
        button(r, "+ Discord", self.add_discord_checks, width=90).pack(side="left", padx=4)
        self.checks_lbl = label(r, "", muted=True, size=12)
        self.checks_lbl.pack(side="left", padx=8)

        tm = card(page)
        tm.pack(fill="x", pady=(0, 10))
        title(tm, "Тайминги автоподбора (секунды)").pack(fill="x", padx=16, pady=(14, 2))
        label(tm, ("Если на медленном ПК стратегии ложно помечаются «не запустился» "
                   "или «✘», увеличь значения."),
              muted=True, size=12, anchor="w", justify="left", wraplength=540
              ).pack(fill="x", padx=16)
        tr = ctk.CTkFrame(tm, fg_color="transparent")
        tr.pack(fill="x", padx=12, pady=(8, 14))
        label(tr, "Ожидание winws.exe").pack(side="left", padx=(4, 6))
        self.start_wait_e = ctk.CTkEntry(tr, width=60, font=(FONT, 13))
        self.start_wait_e.insert(0, str(self.cfg["start_wait"]))
        self.start_wait_e.pack(side="left")
        label(tr, "Таймаут проверки").pack(side="left", padx=(16, 6))
        self.check_to_e = ctk.CTkEntry(tr, width=60, font=(FONT, 13))
        self.check_to_e.insert(0, str(self.cfg["check_timeout"]))
        self.check_to_e.pack(side="left")
        button(tr, "Сохранить", self.save_timings, kind="primary", width=100
               ).pack(side="left", padx=12)
        self.timing_lbl = label(tr, "", muted=True, size=12)
        self.timing_lbl.pack(side="left")

    def _sync_tray_ui(self):
        """Показывает переключатель трея или предлагает доустановить зависимости."""
        if TRAY_OK:
            self.sw_tray.configure(state="normal")
            self.tray_note.pack_forget()
            self.tray_btn.pack_forget()
            return
        self.sw_tray.configure(state="disabled")
        self.tray_note.configure(text="Для иконки в трее нужны пакеты pystray и Pillow.")
        self.tray_note.pack(fill="x", padx=16, pady=(0, 2))
        if CAN_PIP:
            self.tray_btn.pack(anchor="w", padx=16, pady=(2, 4))
        else:
            self.tray_note.configure(text="Для трея пересобери exe с pystray и Pillow.")

    def install_tray_deps(self):
        self.tray_btn.configure(state="disabled", text="Устанавливаю…")
        self.log("pip install pystray Pillow …")

        def work():
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pystray", "Pillow"],
                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                ok = r.returncode == 0
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
            except Exception as e:
                ok, tail = False, [str(e)]
            if ok:
                importlib.invalidate_caches()
                ok = _load_tray()
            self.ui(self._tray_deps_done, ok, tail[0])
        threading.Thread(target=work, daemon=True).start()

    def _tray_deps_done(self, ok: bool, detail: str):
        if ok:
            self.log("pystray и Pillow установлены — трей доступен")
            self._sync_tray_ui()
        else:
            self.log(f"Не удалось установить зависимости трея: {detail}")
            self.tray_btn.configure(state="normal", text="Повторить установку")

    # ---- лог
    def _build_log(self, tab):
        box = card(tab)
        box.pack(fill="both", expand=True, pady=(4, 8))
        self.log_box = ctk.CTkTextbox(
            box, state="disabled", corner_radius=10, fg_color=ROW, text_color=TEXT,
            border_width=0, font=("Consolas", 12))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        r = ctk.CTkFrame(tab, fg_color="transparent")
        r.pack(fill="x")
        button(r, "Очистить", self._clear_log, width=100).pack(side="left", padx=4)
        label(r, f"Файл лога: {LOG_PATH}", muted=True, size=12, anchor="w"
              ).pack(side="left", padx=8)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ================================================================ статус и питание
    def _on_status(self, running: bool):
        changed = running != self.running
        if running and not self.running:
            self.started_at = time.monotonic()
        if not running:
            self.started_at = None
            if not self.scanning and not self.launching:
                self.active_name = None
        self.running = running
        self._render_status()
        self._refresh_power()
        if changed:
            if not self.scanning:
                self.log("zapret работает" if running else "zapret остановлен")
            if self.tray:
                self.tray.icon = self._make_icon("#2ecc71" if running else "#888888")

    def _render_status(self):
        if self.scanning:
            kind, text, sub = "busy", "Идёт подбор…", self.scan_current
        elif self.running:
            kind, text, sub = "on", "Работает", self.active_name or "запущено вне GUI"
        elif self.launching:
            kind, text, sub = "busy", "Запуск…", self.active_name or ""
        else:
            kind, text, sub = "off", "Остановлено", "—"
        self.status_lbl.configure(text=text)
        self.strategy_lbl.configure(text=sub)
        if kind != self._status_kind:
            self._status_kind = kind
            self._animate_card(kind)
        if kind != "off" and not self._pulse_on:
            self._pulse_on = True
            self._pulse()
        elif kind == "off":
            self.dot.configure(text_color=MUTED)
        self._sync_rows()

    def _pulse(self):
        kind = self._status_kind
        if kind == "off":
            self._pulse_on = False
            self.dot.configure(text_color=MUTED)
            return
        s = (math.sin(time.monotonic() * 2.6) + 1) / 2
        lo, hi = ("#1f8f52", "#45e88f") if kind == "on" else ("#b9780f", "#f5b942")
        self.dot.configure(text_color=blend(lo, hi, s))
        self.after(60, self._pulse)

    def _animate_card(self, kind: str, instant: bool = False):
        target = {"on": GREEN, "busy": AMBER}.get(kind) or BORDER[_mi()]
        start = self._card_hex or BORDER[_mi()]
        self._card_hex = target
        self.status_card.configure(border_width=2 if kind != "off" else 1)
        if instant:
            self.status_card.configure(border_color=target)
            return
        self.animate("card", 380, lambda e: self.status_card.configure(
            border_color=blend(start, target, e)))

    def _fade_power(self, target: str):
        if target == self._power_fg:
            return
        start, self._power_fg = self._power_fg, target
        self.animate("power", 260, lambda e: self.power_btn.configure(
            fg_color=blend(start, target, e)))

    def _refresh_power(self):
        if self.scanning:
            self.power_btn.configure(text="Идёт подбор стратегии…", state="disabled")
            return
        if self.launching and not self.running:
            self.power_btn.configure(text="Запускается…", state="disabled")
            return
        if self.running:
            self.power_btn.configure(text="■  Остановить", state="normal", hover_color=RED_H)
            self._fade_power(RED)
        else:
            self.power_btn.configure(text="▶  Запустить", state="normal", hover_color=GREEN_H)
            self._fade_power(GREEN)

    def toggle_power(self):
        if self.scanning or self.launching:
            return
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.scanning or self.launching:
            return
        if not self.selected or not self.selected.exists():
            self.info_lbl.configure(text="Сначала выбери стратегию (или установи zapret)")
            return
        self.info_lbl.configure(text="")
        bat = self.selected
        self.cfg["last_bat"] = str(bat)
        self.active_name = bat.name
        self.launching = True
        self._render_status()
        self._refresh_power()
        threading.Thread(target=self._start_worker, args=(bat,), daemon=True).start()

    def _start_worker(self, bat: Path):
        stop_zapret()
        time.sleep(0.2)
        launch_bat(bat)
        ok = wait_for_winws(self._f("start_wait", 4.0))
        if ok:
            self.log(f"Запущено: {bat.name}")
        else:
            self.log(f"winws.exe не появился после запуска {bat.name} "
                     "(проверь права администратора и антивирус)")
        self.ui(self._after_start, ok)

    def _after_start(self, ok: bool):
        self.launching = False
        if self._from_click:
            self._from_click = False
            name = self.active_name or (self.selected.name if self.selected else "")
            self.scan_lbl.configure(
                text=(f"Запущено: {name}" if ok else
                      "Не запустилось — проверь права администратора и антивирус"))
        if not ok:
            self.active_name = None
        self._render_status()
        self._refresh_power()

    def stop(self):
        if self.scanning or self.launching:
            return
        self.active_name = None
        self.launching = True                      # блокирует кнопку на время остановки
        self.power_btn.configure(text="Останавливаю…", state="disabled")

        def work():
            stop_zapret()
            self.log("Остановлено пользователем")
            self.ui(self._after_stop)
        threading.Thread(target=work, daemon=True).start()

    def _after_stop(self):
        self.launching = False
        self._render_status()
        self._refresh_power()

    # ================================================================ папка и стратегии
    def _load_initial_folder(self):
        folder = self.cfg["folder"]
        if folder and Path(folder).is_dir():
            self.set_folder(Path(folder))
            return
        builds = self._collect_builds()
        if builds:
            self.set_folder(builds[0])
        else:
            self._update_folder_info()
            self._refresh_versions()

    def choose_folder(self):
        if self.scanning:
            return
        path = filedialog.askdirectory(title="Папка со сборкой zapret")
        if path:
            self.set_folder(Path(path))

    def _remember(self, folder: Path):
        """Запоминает внешнюю (не из GUI) сборку, чтобы она была в списке сборок."""
        if is_managed(folder):
            return
        k = norm_path(folder)
        known = list(self.cfg["known_folders"])
        if not any(norm_path(x) == k for x in known):
            known.append(str(folder))
            self.cfg["known_folders"] = known

    def _forget(self, folder: Path):
        k = norm_path(folder)
        self.cfg["known_folders"] = [x for x in self.cfg["known_folders"]
                                     if norm_path(x) != k]
        saved = dict(self.cfg["scan_results"])
        saved.pop(str(folder), None)
        self.cfg["scan_results"] = saved

    def set_folder(self, folder: Path):
        if self.scanning:
            return
        self.folder = Path(folder)
        self.cfg["folder"] = str(self.folder)
        self._remember(self.folder)
        self.bats = find_bats(self.folder)
        self.results = dict(self.cfg["scan_results"].get(str(self.folder), {}))
        self._rebuild_list()
        self.strategy_menu.configure(values=[b.name for b in self.bats] or ["—"])

        last = Path(self.cfg["last_bat"]).name if self.cfg["last_bat"] else ""
        sel = (next((b for b in self.bats if b.name == last), None)
               or (self.bats[0] if self.bats else None))
        self._select(sel, save=False)
        self._update_folder_info()
        self._refresh_versions()
        self.log(f"Папка: {self.folder} (батников: {len(self.bats)})")

    def _update_folder_info(self):
        if self.folder:
            ver = detect_version(self.folder)
            self.folder_lbl.configure(text=str(self.folder), text_color=TEXT)
            self.cur_version_lbl.configure(text=f"Версия: {ver or 'неизвестна'}")
            self.version_lbl.configure(text=f"Версия сборки: {ver or 'неизвестна'}")
        else:
            self.folder_lbl.configure(text="не выбрана", text_color=MUTED)
            self.cur_version_lbl.configure(text="")
            self.version_lbl.configure(text="")
        self._refresh_banner()

    def _refresh_banner(self):
        txt = ""
        if not self.folder or not self.bats:
            txt = "zapret не найден. Открой вкладку «Установка»: скачай или найди сборку."
        elif self.latest:
            cur = detect_version(self.folder)
            if cur is None or version_key(self.latest.tag) > version_key(cur):
                txt = (f"Доступна новая версия {self.latest.tag}"
                       + (f" (у тебя {cur})" if cur else "") + " — вкладка «Установка».")
        self._set_banner(txt)

    def _rebuild_list(self):
        self.rows.clear()
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not self.bats:
            label(self.list_frame, "Батники не найдены", muted=True).pack(pady=20)
            return
        for bat in self.bats:
            row = StrategyRow(self.list_frame, bat, self._on_row_click)
            row.pack(fill="x", padx=6, pady=3)
            row.set_result(self.results.get(bat.name, ""))
            self.rows[bat.name] = row
        self._sync_rows()

    def _sync_rows(self):
        live = self.active_name if (self.running or self.launching) else None
        for name, row in self.rows.items():
            row.set_selected(bool(self.selected) and name == self.selected.name)
            row.set_active(bool(live) and name == live)

    def _f(self, key: str, default: float) -> float:
        try:
            return max(0.5, float(self.cfg[key]))
        except (TypeError, ValueError):
            return default

    def _select(self, bat: Path | None, save: bool = True):
        self.selected = bat
        self.strategy_menu.set(bat.name if bat else "—")
        self._sync_rows()
        if bat and save:
            self.cfg["last_bat"] = str(bat)

    def _on_row_click(self, bat: Path):
        if self.scanning:
            return
        self._select(bat)
        if not self.cfg["click_to_start"]:
            return
        if self.launching:
            self.scan_lbl.configure(text="Подожди, идёт запуск/остановка…")
            return
        if self.running and self.active_name == bat.name:
            self.scan_lbl.configure(text=f"{bat.name} уже работает")
            return
        self._from_click = True
        self.scan_lbl.configure(text=f"Запускаю {bat.name}…")
        self.start()

    def _on_menu(self, name: str):
        bat = next((b for b in self.bats if b.name == name), None)
        if bat and not self.scanning:
            self._select(bat)

    def _mark(self, bat: Path, text: str):
        self.results[bat.name] = text
        row = self.rows.get(bat.name)
        if row:
            row.set_result(text)
        if text.startswith("⏳") and not self._spin_on:
            self._spin_on = True
            self._spin()

    def _spin(self):
        busy = [r for r in self.rows.values() if r.kind == "busy"]
        if not busy:
            self._spin_on = False
            return
        self._spin_i = (self._spin_i + 1) % len(SPIN)
        for r in busy:
            r.spin(SPIN[self._spin_i])
        self.after(90, self._spin)

    def open_folder(self):
        if self.folder and self.folder.is_dir():
            try:
                os.startfile(str(self.folder))                      # только Windows
            except Exception as e:
                self.log(f"Не удалось открыть папку: {e}")

    def open_service(self):
        svc = find_file(self.folder, "service.bat") if self.folder else None
        if not svc:
            self.log("service.bat не найден в текущей сборке")
            return
        subprocess.Popen(["cmd.exe", "/c", "start", "", str(svc)], cwd=str(svc.parent))
        self.log("Открыт service.bat")

    # ================================================================ автоподбор
    def quick_scan(self):
        self.tabs.set(TAB_STRAT)
        if not self.scanning:
            self.toggle_scan()

    def toggle_scan(self):
        if self.scanning:
            self.cancel_scan.set()
            self.scan_btn.configure(text="Останавливаю…", state="disabled")
            return
        if self.launching:
            return
        if not self.bats:
            self.info_lbl.configure(text="Сначала выбери сборку или установи zapret")
            return
        checks = self.cfg["checks"]
        if not checks:
            self.scan_lbl.configure(text="Список проверок пуст (вкладка «Настройки»)")
            return
        order = list(self.bats)
        if self.cfg["smart_order"]:
            prev = self.cfg["scan_results"].get(str(self.folder), {})
            last = Path(self.cfg["last_bat"]).name if self.cfg["last_bat"] else ""
            order = order_bats(order, last, prev)
        self.scanning = True
        self.cancel_scan.clear()
        self.scan_btn.configure(text="✖  Отмена")
        self.results.clear()
        for bat in self.bats:
            self._mark(bat, "")
        self._scan_done, self._scan_total = 0, len(order)
        self.scan_current = ""
        self.scan_bar.reset()
        self._render_status()
        self._refresh_power()
        self.log(f"Автоподбор: {len(order)} стратегий")
        threading.Thread(
            target=self._scan_worker,
            args=(order, [list(c) for c in checks], bool(self.stop_first.get())),
            daemon=True).start()

    def _scan_worker(self, bats, checks, stop_first):
        try:
            res = scan_strategies(
                bats, checks, stop_first=stop_first, cancel=self.cancel_scan,
                progress=lambda i, t, b: self.ui(self._scan_progress, i, t, b),
                result=lambda b, text, ok, el: self.ui(self._scan_result, b, text),
                say=lambda t: self.ui(self._scan_say, t),
                start_wait=self._f("start_wait", 4.0),
                check_timeout=self._f("check_timeout", 4.0))
        except Exception as e:
            self.log(f"Ошибка автоподбора: {e!r}")
            res = None
        self.ui(self._finish_scan, res)

    def _scan_say(self, text: str):
        self.scan_lbl.configure(text=text)
        self.scan_bar.indeterminate(True)             # фаза без известной длительности

    def _scan_progress(self, i: int, total: int, bat: Path):
        self.scan_bar.indeterminate(False)
        self.scan_current = bat.name
        self.scan_bar.goto((i - 1) / total)
        self.scan_bar.creep((i - 0.12) / total, 6.5)  # плавно ползём, пока идёт проверка
        self.scan_lbl.configure(text=f"[{i}/{total}]  {bat.name}")
        self._render_status()

    def _scan_result(self, bat: Path, text: str):
        self._mark(bat, text)
        if not text.startswith("⏳"):
            self.log(f"{bat.name}: {text}")
            self._scan_done += 1
            self.scan_bar.goto(self._scan_done / self._scan_total)

    def _finish_scan(self, res):
        self.scanning = False
        self.scan_current = ""
        self.scan_bar.indeterminate(False)
        self.scan_btn.configure(text=SCAN_TEXT, state="normal")

        saved = dict(self.cfg["scan_results"])        # запоминаем результаты для этой папки
        saved[str(self.folder)] = {k: v for k, v in self.results.items()
                                   if not v.startswith("⏳")}
        self.cfg["scan_results"] = saved

        def halt():
            threading.Thread(target=stop_zapret, daemon=True).start()
            self.active_name = None

        if res is None:
            halt()
            self.scan_bar.goto(0, animate=False)
            self.scan_lbl.configure(text="Ошибка — подробности во вкладке «Лог»")
        elif res.status == "baseline_ok":
            self.scan_bar.goto(0, animate=False)
            self.scan_lbl.configure(
                text="Без zapret всё и так открывается (VPN/другой обход?) — проверять нечего")
        elif res.status == "cancelled":
            halt()
            self.scan_lbl.configure(text="Проверка отменена")
        elif res.status == "none":
            halt()
            self.scan_bar.goto(1.0)
            self.scan_lbl.configure(text="Рабочих стратегий не найдено")
            self.log("Автоподбор: рабочих стратегий нет")
        else:
            best_time, best = min(res.winners, key=lambda w: w[0])
            self.scan_bar.goto(1.0)
            self.scan_lbl.configure(
                text=f"Готово: {best.name} ({best_time:.1f}с). Рабочих: {len(res.winners)}")
            self.log(f"Автоподбор: лучшая — {best.name} ({best_time:.1f}с)")
            self._select(best)
            self.active_name = best.name
            if res.last != best:                      # последней запускалась не лучшая
                self.launching = True
                threading.Thread(target=self._start_worker, args=(best,),
                                 daemon=True).start()
        self._render_status()
        self._refresh_power()

    # ================================================================ обновления и установка
    def check_updates(self, manual: bool = True):
        if self.checking or self.installing:
            return
        self.checking = True
        self.check_btn.configure(state="disabled")
        self.latest_lbl.configure(text="Проверяю GitHub…", text_color=MUTED)
        self.dl_bar.indeterminate(True)

        def work():
            try:
                rel = fetch_latest_release()
                self.ui(self._on_release, rel, manual)
            except Exception as e:
                self.ui(self._on_update_error, e, manual)
        threading.Thread(target=work, daemon=True).start()

    def _on_release(self, rel, manual: bool):
        self.checking = False
        self.dl_bar.reset()
        self.latest = rel
        self.check_btn.configure(state="normal")
        self.latest_lbl.configure(text=f"Последняя версия: {rel.tag}", text_color=TEXT)
        if is_installed(rel.tag):
            self.install_btn.configure(text=f"Версия {rel.tag} уже установлена", state="disabled")
        elif not self.installing:
            self.install_btn.configure(text=f"Скачать и установить {rel.tag}", state="normal")
        self._refresh_banner()
        self.log(f"Последняя версия на GitHub: {rel.tag}")
        self._notify_update(rel)

    def _notify_update(self, rel):
        """Один раз на версию: запись в лог и (если есть трей) всплывающее уведомление."""
        if not self.folder:
            return
        cur = detect_version(self.folder)
        if cur is not None and version_key(rel.tag) <= version_key(cur):
            return
        if self.cfg["notified_tag"] == rel.tag:
            return
        self.cfg["notified_tag"] = rel.tag
        self.log(f"Вышла новая версия zapret: {rel.tag} (вкладка «Установка»)")
        if self.tray:
            try:
                self.tray.notify(f"Доступна версия {rel.tag}", "Zapret GUI")
            except Exception:
                pass

    def _periodic_update_check(self):
        if self.cfg["check_updates"] and not self.scanning:
            self.check_updates(manual=False)
        self.after(6 * 3600 * 1000, self._periodic_update_check)

    def _on_update_error(self, err: Exception, manual: bool):
        self.checking = False
        self.dl_bar.reset()
        self.check_btn.configure(state="normal")
        self.latest_lbl.configure(text="Не удалось проверить (GitHub недоступен?)",
                                  text_color=WARN_TXT)
        self.log(f"Проверка обновлений не удалась: {err}")

    def install_latest(self):
        if self.installing:                            # кнопка работает как «Отмена»
            self.cancel_dl.set()
            self.install_btn.configure(text="Отменяю…", state="disabled")
            return
        if self.scanning:
            self.dl_lbl.configure(text="Дождись окончания проверки стратегий")
            return
        if not self.latest or self.checking:
            return
        self.installing = True
        self.cancel_dl.clear()
        self.check_btn.configure(state="disabled")
        self.dl_bar.reset()
        self.dl_bar.indeterminate(True)                # пока идёт подключение
        self.dl_lbl.configure(text="Подключаюсь…")
        self.install_btn.configure(text="✖  Отмена", state="normal")
        rel = self.latest

        def work():
            try:
                path = install_release(
                    rel, cancel=self.cancel_dl, log=self.log,
                    progress=lambda d, t: self.ui(self._dl_progress, d, t))
                self.ui(self._on_installed, path, None)
            except Cancelled:
                self.ui(self._on_installed, None, "Скачивание отменено")
            except Exception as e:
                self.log(f"Установка не удалась: {e}")
                self.ui(self._on_installed, None, f"Ошибка: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _dl_progress(self, done: int, total: int):
        if total:
            self.dl_bar.indeterminate(False)
            self.dl_bar.goto(done / total)
            self.dl_lbl.configure(text=f"{done / 1e6:.1f} / {total / 1e6:.1f} МБ")
        else:
            self.dl_bar.indeterminate(True)
            self.dl_lbl.configure(text=f"{done / 1e6:.1f} МБ")

    def _on_installed(self, path: Path | None, error: str | None):
        self.installing = False
        self.check_btn.configure(state="normal")
        if path is None:
            self.dl_bar.reset()
            self.dl_lbl.configure(text=error or "")
            self.install_btn.configure(
                text=(f"Скачать и установить {self.latest.tag}"
                      if self.latest else "Скачать и установить"),
                state="normal" if self.latest else "disabled")
            return
        self.dl_bar.indeterminate(False)
        self.dl_bar.goto(1.0)
        self.dl_lbl.configure(text=f"Установлено: {path.name}")
        self.install_btn.configure(text=f"Версия {path.name} установлена", state="disabled")
        self.set_folder(path)
        if self.running:
            self.log("Новая версия выбрана. Перезапусти стратегию, чтобы использовать её.")

    # ---- сборки: установленные GUI + найденные/добавленные вручную
    def _collect_builds(self) -> list[Path]:
        out, seen = [], set()

        def add(p):
            p = Path(p)
            k = norm_path(p)
            if k not in seen and p.is_dir():
                seen.add(k)
                out.append(p)
        for p in installed_versions():
            add(p)
        for s in self.cfg["known_folders"]:
            add(s)
        if self.folder:
            add(self.folder)
        return out

    def _refresh_versions(self):
        self._version_paths = {}
        for p in self._collect_builds():
            lab = base = build_label(p)
            n = 2
            while lab in self._version_paths:
                lab, n = f"{base} #{n}", n + 1
            self._version_paths[lab] = p
        labels = list(self._version_paths) or ["—"]
        self.versions_menu.configure(values=labels)
        cur = labels[0]
        if self.folder:
            for lab, p in self._version_paths.items():
                if norm_path(p) == norm_path(self.folder):
                    cur = lab
                    break
        self.versions_menu.set(cur)

    def use_version(self):
        path = self._version_paths.get(self.versions_menu.get())
        if path:
            self.set_folder(path)

    def delete_selected_version(self):
        path = self._version_paths.get(self.versions_menu.get())
        if not path:
            return
        managed = is_managed(path)
        is_current = bool(self.folder) and norm_path(self.folder) == norm_path(path)
        if managed and is_current and (self.running or self.launching):
            messagebox.showinfo("Удаление",
                                "Сначала останови zapret — эта версия сейчас используется.")
            return
        if managed:
            ok = messagebox.askyesno("Удаление", f"Удалить версию {path.name} с диска?")
        else:
            ok = messagebox.askyesno(
                "Убрать из списка", f"Убрать сборку из списка?\n{path}\n\n"
                                    "Файлы на диске останутся нетронутыми.")
        if not ok:
            return
        try:
            if managed:
                delete_version(path)
            else:
                self._forget(path)
        except Exception as e:
            self.log(f"Не удалось удалить {path.name}: {e}")
            return
        self.log(("Удалена версия " if managed else "Убрана из списка: ") + str(path.name))
        if is_current:
            self.folder = None
            rest = self._collect_builds()
            if rest:
                self.set_folder(rest[0])
            else:
                self.bats, self.selected = [], None
                self.cfg["folder"] = ""
                self._rebuild_list()
                self.strategy_menu.configure(values=["—"])
                self.strategy_menu.set("—")
                self._update_folder_info()
        self._refresh_versions()
        if self.latest:
            self._on_release(self.latest, False)

    def find_builds(self, auto: bool = False):
        if self.discovering:
            return
        self.discovering = True
        self.find_btn.configure(state="disabled")
        self.disc_lbl.configure(text="Ищу сборки на компьютере…")
        self.disc_bar.indeterminate(True)

        def work():
            try:
                found = discover_builds()
            except Exception as e:
                self.log(f"Поиск сборок не удался: {e!r}")
                found = []
            self.ui(self._on_discovered, found, auto)
        threading.Thread(target=work, daemon=True).start()

    def _on_discovered(self, found: list[Path], auto: bool):
        self.discovering = False
        self.disc_bar.reset()
        self.find_btn.configure(state="normal")
        self.cfg["discovered"] = True
        known = {norm_path(p) for p in self._collect_builds()}
        new = [p for p in found if norm_path(p) not in known]
        for p in new:
            self._remember(p)
            self.log(f"Найдена сборка: {p}")
        self.disc_lbl.configure(
            text=(f"Найдено: {len(found)}, новых: {len(new)}" if found
                  else "Сборки не найдены — добавь папку вручную"))
        if found and not self.folder and not self.scanning:
            self.set_folder(found[0])
        else:
            self._refresh_versions()
            self._refresh_banner()

    # ================================================================ настройки
    def _on_switch(self, key: str, sw: ctk.CTkSwitch, extra=None):
        self.cfg[key] = bool(sw.get())
        if extra:
            extra()

    def _on_autostart_switch(self):
        want = bool(self.sw_autostart.get())
        ok, msg = set_autostart(want)
        if ok:
            self.log("Автозапуск GUI " + ("включён" if want else "выключен"))
        else:
            self.log(f"Автозапуск: {msg}")
            (self.sw_autostart.deselect if want else self.sw_autostart.select)()

    def _on_theme(self, label_text: str):
        mode = THEMES.get(label_text, "dark")
        self.cfg["theme"] = mode
        ctk.set_appearance_mode(mode)
        self.after(80, self._reapply_theme)

    def _reapply_theme(self):
        """Цвета, вычисленные в hex (а не парами), надо пересчитать под новую тему."""
        self._card_hex = None
        self._animate_card(self._status_kind, instant=True)

    def save_checks(self):
        checks, errors = parse_checks_text(self.checks_box.get("1.0", "end"))
        if errors:
            self.checks_lbl.configure(text="; ".join(errors[:2]), text_color=WARN_TXT)
            return
        if not checks:
            self.checks_lbl.configure(text="Нужна хотя бы одна проверка", text_color=WARN_TXT)
            return
        self.cfg["checks"] = checks
        self.checks_lbl.configure(text=f"Сохранено ({len(checks)})", text_color=OK_TXT)

    def add_discord_checks(self):
        text = self.checks_box.get("1.0", "end")
        have = {u for _, u, _ in parse_checks_text(text)[0]}
        added = 0
        for n, u, b in DISCORD_CHECKS:
            if u not in have:
                self.checks_box.insert("end", ("" if text.endswith("\n\n") else "\n")
                                       + f"{n} | {u} | {b}")
                text = self.checks_box.get("1.0", "end")
                added += 1
        if added:
            self.save_checks()
        else:
            self.checks_lbl.configure(text="Discord уже в списке", text_color=MUTED)

    def save_timings(self):
        try:
            sw = float(self.start_wait_e.get().replace(",", "."))
            ct = float(self.check_to_e.get().replace(",", "."))
            if not (1 <= sw <= 60 and 1 <= ct <= 60):
                raise ValueError
        except ValueError:
            self.timing_lbl.configure(text="Нужны числа от 1 до 60", text_color=WARN_TXT)
            return
        self.cfg["start_wait"], self.cfg["check_timeout"] = sw, ct
        self.timing_lbl.configure(text="Сохранено", text_color=OK_TXT)

    def reset_checks(self):
        self.checks_box.delete("1.0", "end")
        self.checks_box.insert("1.0", format_checks(DEFAULT_CHECKS))
        self.save_checks()

    # ================================================================ трей и выход
    def _make_icon(self, color: str):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color)
        return img

    def _apply_tray(self):
        if self.cfg["tray"]:
            self._tray_enable()
        else:
            self._tray_disable()

    def _tray_enable(self):
        if not TRAY_OK or self.tray:
            return
        items = pystray.Menu(
            pystray.MenuItem("Показать", lambda: self.ui(self.show_window), default=True),
            pystray.MenuItem("Запустить / Остановить", lambda: self.ui(self.toggle_power)),
            pystray.MenuItem("Выход", lambda: self.ui(self.quit_app)),
        )
        self.tray = pystray.Icon(
            APP_NAME, self._make_icon("#2ecc71" if self.running else "#888888"),
            "Zapret GUI", items)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_disable(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self):
        if self.cfg["tray"] and self.tray:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        self._stop_poll.set()
        was_scanning = self.scanning
        if was_scanning:
            self.cancel_scan.set()
        if self.cfg["stop_on_exit"] or was_scanning:
            stop_zapret()
        self._tray_disable()
        self.destroy()


def main():
    if sys.platform == "win32" and not is_admin():
        relaunch_as_admin()
        return
    App(autostart="--autostart" in sys.argv).mainloop()


if __name__ == "__main__":
    main()
