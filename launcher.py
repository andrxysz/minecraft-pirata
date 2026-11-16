"""Interface Tkinter do Minecraft Pirata. Execute: python launcher.py."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from minecraft import (APP_DIR, GAME_DIR, LaunchError, build_plan, installed_versions,
                       load_settings, save_settings, start_game)
from installer import InstallCancelled, fetch_catalog, install_version


NICK_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
MIN_GB, MAX_GB = 1.0, 16.0  # memory_mb aceito pelo motor: 512–16384

THEMES = {
    "light": {
        "BG": "#f3f5f2", "CARD": "#ffffff", "FIELD": "#f7f9f7", "BORDER": "#dfe5df",
        "TEXT": "#16201a", "MUTED": "#6c7a71", "ACCENT": "#2f9e44", "ACCENT_HOVER": "#268738",
        "ACCENT_SOFT": "#e6f4e8", "DISABLED": "#c9d3cb", "ON_DISABLED": "#ffffff",
        "WARN": "#d08a1e", "DANGER": "#d64545",
        "SCENE": {
            "sky": ("#bfe0ff", "#f5fbff"), "sun": "#ffd866", "halo": "#fff4cc", "stars": False,
            "cloud": "#ffffff", "cloud_shadow": "#e3eef8", "hill": "#cfe6c6", "hill_top": "#bddcb2",
            "grass": "#6cc24a", "grass_light": "#84d65f", "grass_dark": "#5aa83c",
            "dirt": "#9b6b43", "dirt_speck": "#85593a", "stone": "#a3a9ad", "stone_speck": "#8f959a",
            "log": "#7a5230", "log_dark": "#694425", "leaf": ("#4caf50", "#43a047"),
            "subtitle": "#4a5a50", "eyebrow": "BEM-VINDO!",
        },
    },
    "dark": {
        "BG": "#0f1411", "CARD": "#171e1a", "FIELD": "#1e2722", "BORDER": "#2a352e",
        "TEXT": "#e8efe9", "MUTED": "#8a998f", "ACCENT": "#3fb950", "ACCENT_HOVER": "#52cc63",
        "ACCENT_SOFT": "#1d3324", "DISABLED": "#2f3a33", "ON_DISABLED": "#7d8a81",
        "WARN": "#e3a53a", "DANGER": "#f06b6b",
        "SCENE": {
            "sky": ("#0a1328", "#233558"), "sun": "#eef1f8", "halo": "#33456b", "stars": True,
            "cloud": "#34435f", "cloud_shadow": "#2a3750", "hill": "#1b3328", "hill_top": "#244236",
            "grass": "#3c8a35", "grass_light": "#4ea345", "grass_dark": "#2f722a",
            "dirt": "#5b412e", "dirt_speck": "#4b3424", "stone": "#50575e", "stone_speck": "#454b51",
            "log": "#4c3420", "log_dark": "#3d2918", "leaf": ("#2d7535", "#27662e"),
            "subtitle": "#b2c0b7", "eyebrow": "BOA NOITE, AVENTUREIRO",
        },
    },
}

# Cores do tema ativo (preenchidas por apply_theme).
BG = CARD = FIELD = BORDER = TEXT = MUTED = ACCENT = ACCENT_HOVER = ACCENT_SOFT = ""
DISABLED = ON_DISABLED = WARN = DANGER = ""
SCENE: dict = {}


def apply_theme(name):
    globals().update(THEMES.get(name, THEMES["light"]))


apply_theme("light")

# Ícone 8×8 do bloco de grama (logo da janela e da barra superior).
ICON_COLORS = {"G": "#5fb944", "g": "#4c9a37", "D": "#9b6b43", "d": "#85593a", "l": "#b07e52"}
ICON_ROWS = ("GGGGGGGG", "GGgGGGgG", "GGGGGGGG", "DGgDGgGD",
             "DDDDDDDD", "DdDDDDlD", "DDDDDdDD", "DDDlDDDD")


def mix(a, b, t):
    """Interpola duas cores hex (t=0 → a, t=1 → b)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def total_ram_gb():
    """RAM física do computador em GB (só Windows; None se indisponível)."""
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / 1024 ** 3
    except (AttributeError, OSError):
        pass
    return None


def format_gb(gb):
    return f"{gb:g} GB".replace(".", ",")


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title("Minecraft Pirata")
        self.geometry(f"{self.px(1060)}x{self.px(680)}")
        self.minsize(self.px(940), self.px(660))
        self._set_icon()
        self.settings = load_settings()
        apply_theme(self.settings.get("theme", "light"))
        self.nickname = tk.StringVar(value=str(self.settings.get("nickname", "Player")))
        self.version = tk.StringVar()
        self.status = tk.StringVar()
        self.status_tone = "idle"
        self.version_list: list[str] = []
        self.local_versions: list[str] = []
        self.catalog = fetch_catalog(refresh=False)
        self.catalog_loading = False
        self.cancel_install = threading.Event()
        self.progress_value = 0
        self.progress_active = False
        self.play_text = "PLAY"
        self.events = queue.Queue()
        self.process = None
        self.busy = False
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.nickname.trace_add("write", lambda *_: self.on_nick())
        self.version.trace_add("write", lambda *_: self.on_version())
        self._build()
        self.refresh()
        self.after(100, self.poll)
        self.bind("<Return>", lambda event: self.play())

    def px(self, value):
        return int(value * self.scale)

    def _set_icon(self):
        base = tk.PhotoImage(width=8, height=8)
        for y, row in enumerate(ICON_ROWS):
            for x, key in enumerate(row):
                base.put(ICON_COLORS[key], to=(x, y, x + 1, y + 1))
        self.icons = [base.zoom(k) for k in (8, 4, 2)]  # 64, 32 e 16 px
        self.iconphoto(True, *self.icons)

    # ------------------------------------------------------------------ estilo
    def _style(self):
        self.option_add("*Font", ("Segoe UI", 10))
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, arrowcolor=ACCENT, bordercolor=BORDER,
                        lightcolor=FIELD, darkcolor=FIELD, padding=(10, 9), arrowsize=15)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", FIELD)],
                  foreground=[("disabled", MUTED), ("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)], arrowcolor=[("disabled", DISABLED)],
                  background=[("active", ACCENT_SOFT), ("readonly", FIELD)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        style.configure("Horizontal.TScale", background=ACCENT, troughcolor=BORDER,
                        bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, sliderlength=16,
                        gripcount=0)
        style.map("Horizontal.TScale", background=[("active", ACCENT_HOVER)])
        style.configure("TCheckbutton", background=CARD, foreground=TEXT, font=("Segoe UI", 10),
                        indicatorbackground=FIELD, indicatorforeground=ACCENT,
                        upperbordercolor=BORDER, lowerbordercolor=BORDER, focuscolor=CARD)
        style.map("TCheckbutton", background=[("active", CARD)],
                  indicatorbackground=[("pressed", ACCENT_SOFT), ("selected", ACCENT_SOFT)])
        style.configure("Install.Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=FIELD, bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)

    def card(self, parent, **kwargs):
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, **kwargs)

    def text(self, parent, text="", size=10, weight="normal", color=None, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color or TEXT,
                        font=("Segoe UI", size, weight), **kwargs)

    def link(self, parent, text, command, color=None, hover=None, **kwargs):
        color, hover = color or MUTED, hover or ACCENT
        button = tk.Button(parent, text=text, command=command, bg=parent.cget("bg"), fg=color,
                           activebackground=parent.cget("bg"), activeforeground=hover,
                           disabledforeground=DISABLED, relief="flat", bd=0, cursor="hand2",
                           padx=0, pady=0, **kwargs)
        button.bind("<Enter>", lambda e: button.cget("state") == "normal" and button.configure(fg=hover))
        button.bind("<Leave>", lambda e: button.configure(fg=color))
        return button

    def button(self, parent, text, command, primary=True, **kwargs):
        bg, hover, fg = (ACCENT, ACCENT_HOVER, "#ffffff") if primary else (FIELD, BORDER, TEXT)
        widget = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=hover,
                           activeforeground=fg, disabledforeground=ON_DISABLED, relief="flat", bd=0,
                           cursor="hand2", **kwargs)
        widget.bind("<Enter>", lambda e: widget.cget("state") == "normal" and widget.configure(bg=hover))
        widget.bind("<Leave>", lambda e: widget.cget("state") == "normal" and widget.configure(bg=bg))
        return widget

    # ------------------------------------------------------------------ layout
    def _build(self):
        """(Re)constrói a interface com o tema ativo, preservando o estado."""
        for child in self.winfo_children():
            child.destroy()
        self.progress_active = False
        self.configure(bg=BG)
        self._style()
        self._topbar()
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        self._footer()
        body = tk.Frame(self, bg=BG, padx=self.px(24), pady=self.px(22))
        body.pack(fill="both", expand=True)
        self._play_panel(body)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, self.px(20)))
        self._hero(left)
        self._stats(left)
        self.on_nick()
        self._sync()
        if self.busy and self.process is None:
            self.set_progress(self.status.get())

    def _topbar(self):
        bar = tk.Frame(self, bg=CARD, padx=self.px(24), pady=self.px(14))
        bar.pack(fill="x")
        size = self.px(30)
        logo = tk.Canvas(bar, width=size, height=size, bg=CARD, highlightthickness=0)
        logo.pack(side="left")
        self.draw_block_icon(logo, size)
        names = tk.Frame(bar, bg=CARD)
        names.pack(side="left", padx=(self.px(12), 0))
        self.text(names, "Minecraft Pirata", 13, "bold").pack(anchor="w")
        self.text(names, "Minecraft Java Edition", 8, color=MUTED).pack(anchor="w")

        gear = tk.Button(bar, text="⚙", command=self.open_settings, bg=CARD, fg=MUTED,
                         activebackground=FIELD, activeforeground=TEXT, relief="flat", bd=0,
                         cursor="hand2", font=("Segoe UI Symbol", 16), padx=self.px(8), pady=0)
        gear.pack(side="right")
        gear.bind("<Enter>", lambda e: gear.configure(bg=FIELD, fg=TEXT))
        gear.bind("<Leave>", lambda e: gear.configure(bg=CARD, fg=MUTED))

        tabs = tk.Frame(bar, bg=CARD)
        tabs.pack(side="left", padx=(self.px(48), 0), fill="y")
        tab = tk.Frame(tabs, bg=CARD)
        tab.pack(side="left", fill="y")
        self.text(tab, "Início", 10, "bold").pack(pady=(self.px(4), self.px(6)))
        tk.Frame(tab, bg=ACCENT, height=self.px(3)).pack(fill="x", side="bottom")

    def _footer(self):
        footer = tk.Frame(self, bg=CARD, padx=self.px(24), pady=self.px(10))
        footer.pack(side="bottom", fill="x")
        tk.Frame(self, bg=BORDER, height=1).pack(side="bottom", fill="x")
        for text, path in (("Abrir pasta do jogo", GAME_DIR), ("Abrir logs", APP_DIR / "logs")):
            self.link(footer, text, lambda p=path: self.open_folder(p),
                      font=("Segoe UI", 9)).pack(side="left", padx=(0, self.px(22)))
        self.text(footer, "v1.2", 8, color=MUTED).pack(side="right")

    def _hero(self, parent):
        frame = self.card(parent)
        frame.pack(fill="both", expand=True)
        scene = tk.Canvas(frame, bg=SCENE["sky"][0], highlightthickness=0, height=self.px(300))
        scene.pack(fill="both", expand=True)
        scene.bind("<Configure>", lambda e: self.draw_scene(scene, e.width, e.height))

    def _stats(self, parent):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(self.px(16), 0))
        self.stat_values = {}
        items = (("versions", "VERSÕES INSTALADAS", ACCENT),
                 ("last", "ÚLTIMA VERSÃO JOGADA", "#3b82c4"),
                 ("memory", "MEMÓRIA SELECIONADA", "#d08a1e"))
        for column, (key, title, color) in enumerate(items):
            row.columnconfigure(column, weight=1, uniform="stats")
            box = self.card(row)
            box.grid(row=0, column=column, sticky="nsew",
                     padx=(0 if column == 0 else self.px(8), 0 if column == 2 else self.px(8)))
            tk.Frame(box, bg=color, height=self.px(3)).pack(fill="x")
            inner = tk.Frame(box, bg=CARD, padx=self.px(16), pady=self.px(12))
            inner.pack(fill="both", expand=True)
            self.text(inner, title, 8, "bold", MUTED).pack(anchor="w")
            value = self.text(inner, "—", 16, "bold", anchor="w")
            value.pack(fill="x", pady=(self.px(2), 0))
            value.full_text = "—"
            value.bind("<Configure>", lambda e, v=value: self.fit(v))
            self.stat_values[key] = value

    def _play_panel(self, parent):
        panel = self.card(parent, width=self.px(360))
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)
        tk.Frame(panel, bg=ACCENT, height=self.px(4)).pack(fill="x")
        inner = tk.Frame(panel, bg=CARD, padx=self.px(26), pady=self.px(22))
        inner.pack(fill="both", expand=True)

        self.text(inner, "JOGAR", 8, "bold", ACCENT).pack(anchor="w")
        self.text(inner, "É só selecionar um nick e jogar.", 15, "bold").pack(anchor="w", pady=(self.px(2), 0))

        # Perfil
        profile = tk.Frame(inner, bg=FIELD, highlightthickness=1, highlightbackground=BORDER,
                           padx=self.px(12), pady=self.px(10))
        profile.pack(fill="x", pady=(self.px(14), self.px(16)))
        size = self.px(40)
        self.avatar = tk.Canvas(profile, width=size, height=size, bg=FIELD, highlightthickness=0)
        self.avatar.pack(side="left")
        who = tk.Frame(profile, bg=FIELD)
        who.pack(side="left", padx=(self.px(12), 0))
        self.profile_name = self.text(who, "", 11, "bold")
        self.profile_name.pack(anchor="w")
        self.text(who, "Minecraft pirata", 8, color=MUTED).pack(anchor="w")

        # Nickname
        self.text(inner, "NICKNAME", 8, "bold", MUTED).pack(anchor="w", pady=(0, self.px(6)))
        self.nick_box = tk.Frame(inner, bg=FIELD, highlightthickness=1, highlightbackground=BORDER,
                                 padx=self.px(10))
        self.nick_box.pack(fill="x")
        self.nick_input = tk.Entry(self.nick_box, textvariable=self.nickname, bg=FIELD, fg=TEXT,
                                   insertbackground=ACCENT, relief="flat", highlightthickness=0,
                                   disabledbackground=FIELD, disabledforeground=MUTED,
                                   font=("Segoe UI", 11))
        self.nick_input.pack(fill="x", ipady=self.px(8))
        self.nick_input.bind("<FocusIn>", lambda e: self.nick_box.configure(highlightbackground=ACCENT))
        self.nick_input.bind("<FocusOut>", lambda e: self.nick_box.configure(highlightbackground=BORDER))
        self.nick_hint = self.text(inner, "", 8, color=MUTED)
        self.nick_hint.pack(anchor="w", pady=(self.px(5), self.px(16)))

        # Versão
        version_header = tk.Frame(inner, bg=CARD)
        version_header.pack(fill="x", pady=(0, self.px(6)))
        self.text(version_header, "VERSÃO DO MINECRAFT", 8, "bold", MUTED).pack(side="left")
        self.refresh_button = self.link(version_header, "↻  Atualizar", self.refresh, color=ACCENT,
                                        hover=ACCENT_HOVER, font=("Segoe UI", 9, "bold"))
        self.refresh_button.pack(side="right")
        self.versions = ttk.Combobox(inner, textvariable=self.version, state="readonly",
                                     font=("Segoe UI", 11), height=18)
        self.versions.pack(fill="x")
        self.count_label = self.text(inner, "", 8, color=MUTED)
        self.count_label.pack(anchor="w", pady=(self.px(5), 0))

        # Rodapé do painel: status + botão
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        self.progress_bar = ttk.Progressbar(bottom, style="Install.Horizontal.TProgressbar",
                                           maximum=100, value=self.progress_value)
        self.progress_bar.pack(fill="x", pady=(0, self.px(8)))
        self.play_button = self.button(bottom, "PLAY", self.play, font=("Segoe UI", 14, "bold"),
                                       pady=self.px(12))
        self.play_button.pack(fill="x")
        status_row = tk.Frame(bottom, bg=CARD)
        status_row.pack(fill="x", pady=(self.px(12), 0))
        dot = self.px(8)
        self.status_dot = tk.Canvas(status_row, width=dot, height=dot, bg=CARD, highlightthickness=0)
        self.status_dot.create_oval(0, 0, dot, dot, fill=CARD, outline="", tags="dot")
        self.status_dot.pack(side="left", anchor="n", pady=(self.px(5), 0), padx=(0, self.px(8)))
        tk.Label(status_row, textvariable=self.status, bg=CARD, fg=MUTED, anchor="w",
                 wraplength=self.px(280), justify="left",
                 font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------------ desenho
    @staticmethod
    def draw_block_icon(canvas, size):
        p = size / 8
        for y, row in enumerate(ICON_ROWS):
            for x, key in enumerate(row):
                canvas.create_rectangle(x * p, y * p, (x + 1) * p, (y + 1) * p,
                                        fill=ICON_COLORS[key], outline="")

    def draw_scene(self, canvas, width, height):
        s = SCENE
        canvas.delete("all")
        # Céu em degradê
        band = 4
        for y in range(0, height, band):
            canvas.create_rectangle(0, y, width, y + band, outline="",
                                    fill=mix(*s["sky"], min(1, y / (height * 0.7))))
        if s["stars"]:
            for i in range(60):
                x, y = (i * 97 + 31) % max(width, 1), (i * 53 + 17) % max(int(height * 0.6), 1)
                size = self.px(3) if i % 5 == 0 else self.px(2)
                canvas.create_rectangle(x, y, x + size, y + size, outline="",
                                        fill="#ffffff" if i % 3 else "#9fb3d9")
        tile = self.px(24)
        # Sol (ou lua)
        sx, sy, sz = width - self.px(130), self.px(46), self.px(46)
        canvas.create_rectangle(sx - self.px(10), sy - self.px(10), sx + sz + self.px(10), sy + sz + self.px(10),
                                fill=s["halo"], outline="")
        canvas.create_rectangle(sx, sy, sx + sz, sy + sz, fill=s["sun"], outline="")
        if s["stars"]:  # crateras da lua
            for cx, cy, c in ((0.2, 0.25, 0.2), (0.6, 0.55, 0.25), (0.3, 0.7, 0.15)):
                x0, y0 = sx + sz * cx, sy + sz * cy
                canvas.create_rectangle(x0, y0, x0 + sz * c, y0 + sz * c, fill="#c9cfdc", outline="")
        # Nuvens
        for cx, cy, blocks in ((0.52, 0.12, 5), (0.30, 0.30, 4), (0.78, 0.36, 6), (0.08, 0.50, 3)):
            x0, y0, c = int(width * cx), int(height * cy), self.px(14)
            canvas.create_rectangle(x0, y0 + c, x0 + blocks * c, y0 + 2 * c, fill=s["cloud"], outline="")
            canvas.create_rectangle(x0 + c, y0, x0 + (blocks - 1) * c, y0 + c, fill=s["cloud"], outline="")
            canvas.create_rectangle(x0, y0 + 2 * c - 3, x0 + blocks * c, y0 + 2 * c,
                                    fill=s["cloud_shadow"], outline="")
        ground = int(height * 0.70)
        cols = width // tile + 2
        # Colinas ao fundo
        for i in range(cols):
            h = round(2.2 + 1.6 * math.sin(i * 0.23 + 2) + math.sin(i * 0.61))
            top = ground - h * tile + tile
            canvas.create_rectangle(i * tile, top, (i + 1) * tile, height, fill=s["hill"], outline="")
            canvas.create_rectangle(i * tile, top, (i + 1) * tile, top + self.px(5), fill=s["hill_top"], outline="")
        # Terreno principal
        tops = []
        for i in range(cols):
            x = i * tile
            top = ground - tile * round(0.9 * math.sin(i * 0.38) + 0.7 * math.sin(i * 0.14 + 1.3))
            tops.append(top)
            for row, y in enumerate(range(top, height, tile)):
                if row == 0:
                    canvas.create_rectangle(x, y, x + tile, y + tile, fill=s["dirt"], outline="")
                    canvas.create_rectangle(x, y, x + tile, y + tile // 3, fill=s["grass"], outline="")
                    canvas.create_rectangle(x + tile // 4, y + tile // 3, x + tile // 2, y + tile // 2,
                                            fill=s["grass_dark"], outline="")
                    canvas.create_line(x, y, x + tile, y, fill=s["grass_light"])
                else:
                    stone = row >= 3
                    fill = s["stone"] if stone else s["dirt"]
                    speck = s["stone_speck"] if stone else s["dirt_speck"]
                    canvas.create_rectangle(x, y, x + tile, y + tile, fill=fill, outline="")
                    ox = ((i * 7 + row * 3) % 3) * tile // 4
                    canvas.create_rectangle(x + ox + 2, y + tile // 3, x + ox + tile // 4 + 2,
                                            y + tile // 3 + tile // 5, fill=speck, outline="")
        # Árvores (só na metade direita, longe do texto)
        for i in range(cols):
            if i % 7 != 3 or i * tile < width * 0.45:
                continue
            x, top = i * tile, tops[i]
            for k in range(1, 4):
                canvas.create_rectangle(x, top - k * tile, x + tile, top - (k - 1) * tile, fill=s["log"], outline="")
                canvas.create_rectangle(x + tile // 3, top - k * tile, x + tile // 3 + 2, top - (k - 1) * tile,
                                        fill=s["log_dark"], outline="")
            for dx, dy in ((-1, 3), (0, 3), (1, 3), (-1, 4), (0, 4), (1, 4), (0, 5), (-2, 3), (2, 3)):
                lx, ly = x + dx * tile, top - (dy + 1) * tile
                canvas.create_rectangle(lx, ly, lx + tile, ly + tile, fill=s["leaf"][(dx + dy) % 2], outline="")
        # Título
        pad = self.px(34)
        canvas.create_text(pad, pad, anchor="nw", text=s["eyebrow"], font=("Segoe UI", 9, "bold"), fill=ACCENT)
        title = canvas.create_text(pad, pad + self.px(20), anchor="nw", text="O melhor launcher\ntotalmente de graça :).",
                                   font=("Segoe UI", 26, "bold"), fill=TEXT)
    
    def draw_avatar(self, nickname):
        canvas, size = self.avatar, self.px(40)
        canvas.delete("all")
        digest = hashlib.md5(nickname.encode("utf-8")).digest()
        skins = ("#f2c9a0", "#e0ac7e", "#c68a5a", "#8d5a3b", "#f6d7b8")
        hairs = ("#3b2a1e", "#6b4226", "#d9a441", "#1f1f1f", "#a5442c", "#7d7d7d")
        eyes = ("#3a6ed1", "#2f8f4a", "#6b4226", "#5b3fb0")
        skin, hair, eye = skins[digest[0] % 5], hairs[digest[1] % 6], eyes[digest[2] % 4]
        p = size / 8
        grid = [[skin] * 8 for _ in range(8)]
        for c in range(8):
            grid[0][c] = grid[1][c] = hair
        grid[2][0] = grid[2][7] = hair
        if digest[3] % 2:
            grid[2][1] = grid[2][6] = hair
        grid[4][1], grid[4][2], grid[4][5], grid[4][6] = "#ffffff", eye, eye, "#ffffff"
        grid[6][3] = grid[6][4] = mix(skin, "#000000", 0.35)
        grid[5][3] = grid[5][4] = mix(skin, "#000000", 0.12)
        for r, row in enumerate(grid):
            for c, color in enumerate(row):
                canvas.create_rectangle(c * p, r * p, (c + 1) * p, (r + 1) * p, fill=color, outline="")

    # ------------------------------------------------------------------ estado
    def say(self, text, tone="idle"):
        self.status.set(text)
        self.status_tone = tone
        colors = {"ok": ACCENT, "busy": WARN, "error": DANGER, "idle": MUTED}
        self.status_dot.itemconfigure("dot", fill=colors[tone] if text else CARD)

    def on_nick(self):
        nick = self.nickname.get().strip()
        valid = bool(NICK_RE.fullmatch(nick))
        self.profile_name.configure(text=nick or "Sem nick")
        self.draw_avatar(nick or "Player")
        if not nick:
            self.nick_hint.configure(text="3–16 caracteres · letras, números e _", fg=MUTED)
        elif valid:
            self.nick_hint.configure(text="✓  Nick válido", fg=ACCENT)
        else:
            self.nick_hint.configure(text="Use 3–16 caracteres: letras, números e _", fg=DANGER)

    def set_stat(self, key, text):
        label = self.stat_values[key]
        label.full_text = text
        self.fit(label)

    @staticmethod
    def fit(label):
        """Encurta o texto com reticências para caber na largura do rótulo."""
        text, width = label.full_text, label.winfo_width() - 4
        font = tkfont.Font(font=label.cget("font"))
        if width > 20 and font.measure(text) > width:
            while len(text) > 1 and font.measure(text + "…") > width:
                text = text[:-1]
            text += "…"
        if label.cget("text") != text:
            label.configure(text=text)

    def update_stats(self):
        self.set_stat("versions", str(len(self.local_versions)))
        self.set_stat("last", str(self.settings.get("version") or "—"))
        try:
            memory = int(self.settings.get("memory_mb", 2048))
            self.set_stat("memory", f"{memory:,} MB".replace(",", "."))
        except (TypeError, ValueError):
            self.set_stat("memory", "inválida")

    def _sync(self):
        """Aplica o estado atual (versões, status, ocupado) aos widgets."""
        self.versions["values"] = self.version_list
        self.on_version()
        self.update_stats()
        self.say(self.status.get(), self.status_tone)
        self.set_busy(self.busy, self.play_text)

    def refresh(self):
        if self.busy or self.catalog_loading:
            return
        try:
            self.local_versions = installed_versions()
        except OSError as error:
            self.say(f"Não foi possível listar as versões: {error}", "error")
            return
        self.merge_versions()
        self._sync()
        self.catalog_loading = True
        self.refresh_button.configure(state="disabled")
        self.say("Atualizando o catálogo de versões…", "busy")
        def worker():
            try:
                self.events.put(("catalog", fetch_catalog()))
            except Exception as error:
                self.events.put(("catalog_error", str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def merge_versions(self):
        previous = self.version.get() or self.settings.get("version")
        official = list(self.catalog.versions) if self.catalog else []
        self.version_list = list(dict.fromkeys(self.local_versions + official))
        fallback = self.catalog.latest_release if self.catalog else ""
        if fallback not in self.version_list:
            fallback = self.version_list[0] if self.version_list else ""
        self.version.set(previous if previous in self.version_list else fallback)

    def on_version(self):
        if not hasattr(self, "count_label"):
            return
        version = self.version.get()
        if not version:
            text = "Carregando versões disponíveis…"
        elif version in self.local_versions:
            text = "Arquivos locais · Play verifica e abre"
        else:
            text = "Não instalada · Baixa ao clicar em Play"
        self.count_label.configure(text=text)

    def set_progress(self, message, completed=0, total=0):
        self.say(message, "busy")
        if total:
            if self.progress_active:
                self.progress_bar.stop()
            self.progress_active = False
            self.progress_value = completed * 100 / total
            self.progress_bar.configure(mode="determinate", value=self.progress_value)
        elif not self.progress_active:
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start(12)
            self.progress_active = True

    def persist(self):
        self.settings.update(nickname=self.nickname.get().strip(), version=self.version.get())
        save_settings(self.settings)

    def set_busy(self, busy, text="PLAY"):
        self.busy, self.play_text = busy, text
        state = "disabled" if busy or not self.version.get() else "normal"
        self.play_button.configure(state=state, text=text, bg=ACCENT if state == "normal" else DISABLED,
                                   cursor="hand2" if state == "normal" else "arrow")
        self.nick_input.configure(state="disabled" if busy else "normal")
        self.versions.configure(state="disabled" if busy else "readonly")
        self.refresh_button.configure(state="disabled" if busy or self.catalog_loading else "normal")
        if not busy:
            self.progress_bar.stop()
            self.progress_active = False
            self.progress_value = 0
            self.progress_bar.configure(mode="determinate", value=0)

    def play(self):
        if self.busy or not self.version.get():
            return
        nickname, version = self.nickname.get().strip(), self.version.get()
        if not NICK_RE.fullmatch(nickname):
            self.say("Informe um nick válido antes de jogar.", "error")
            self.nick_input.focus_set()
            return
        self.set_busy(True, "PREPARANDO…")
        self.cancel_install.clear()
        self.set_progress("Conferindo os arquivos da versão…")
        try:
            self.persist()
        except OSError as error:
            self.set_busy(False)
            self.say("Não foi possível salvar as configurações.", "error")
            messagebox.showerror("Não foi possível salvar", str(error), parent=self)
            return
        self.update_stats()
        settings = dict(self.settings)
        catalog = self.catalog
        def worker():
            try:
                install_version(version, settings, catalog=catalog, cancel=self.cancel_install,
                                progress=lambda *value: self.events.put(("progress", value)))
                if self.cancel_install.is_set():
                    return
                plan = build_plan(version, nickname, settings)
                self.events.put(("progress", ("Extraindo arquivos e iniciando Minecraft…", 0, 0)))
                if self.cancel_install.is_set():
                    return
                process = start_game(plan)
                self.events.put(("started", process))
            except InstallCancelled:
                pass
            except Exception as error:
                self.events.put(("error", str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind in ("catalog", "catalog_error"):
                    self.catalog_loading = False
                    if kind == "catalog":
                        self.catalog = value
                        self.merge_versions()
                    self._sync()
                    if not self.busy:
                        if kind == "catalog_error":
                            self.say("Sem catálogo online. Confira sua conexão e clique em Atualizar.", "error")
                        elif value.cached:
                            self.say("Catálogo em cache. Versões completas podem abrir sem internet.")
                        else:
                            self.say(f"{len(self.catalog.versions)} versões disponíveis. O download começa no Play.")
                elif kind == "progress":
                    self.set_progress(*value)
                elif kind == "error":
                    self.set_busy(False)
                    self.say("Não foi possível iniciar. Confira os detalhes.", "error")
                    messagebox.showerror("Não foi possível iniciar", value, parent=self)
                elif kind == "started":
                    self.process = value
                    self.local_versions = installed_versions()
                    self._sync()
                    self.progress_bar.stop()
                    self.progress_active = False
                    self.progress_bar.configure(mode="determinate", value=100)
                    self.set_busy(True, "EM JOGO")
                    self.say("Minecraft iniciado. Você pode fechar o launcher.", "busy")
                    if self.settings.get("close_on_launch"):
                        self.after(1200, self.close)
        except queue.Empty:
            pass
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                self.process = None
                self.set_busy(False)
                if code == 0:
                    self.say("Jogo encerrado.", "ok")
                else:
                    self.say(f"O jogo encerrou com código {code}. Veja Abrir logs → game.log.", "error")
        self.after(150, self.poll)

    # ------------------------------------------------------------------ configurações
    def open_settings(self):
        SettingsDialog(self)

    def apply_settings(self, changes):
        theme_changed = changes.get("theme") != self.settings.get("theme", "light")
        self.settings.update(changes)
        if not self.settings.get("java_path"):
            self.settings.pop("java_path", None)
        try:
            self.persist()
        except OSError as error:
            messagebox.showerror("Não foi possível salvar", str(error), parent=self)
        if theme_changed:
            apply_theme(self.settings["theme"])
            self._build()
        else:
            self.update_stats()

    def open_folder(self, path):
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as error:
            messagebox.showerror("Não foi possível abrir a pasta", str(error), parent=self)

    def close(self):
        self.cancel_install.set()
        try:
            self.persist()
        except OSError as error:
            messagebox.showerror("Não foi possível salvar", str(error), parent=self)
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """Janela modal de configurações: tema, memória, Java e comportamento."""

    def __init__(self, app: Launcher):
        super().__init__(app, bg=CARD)
        self.app, px = app, app.px
        self.title("Configurações")
        self.resizable(False, False)
        self.transient(app)
        settings = app.settings
        self.theme = settings.get("theme", "light")
        try:
            gb = int(settings.get("memory_mb", 2048)) / 1024
        except (TypeError, ValueError):
            gb = 2.0
        self.memory = tk.DoubleVar(value=min(MAX_GB, max(MIN_GB, round(gb * 2) / 2)))
        self.java = tk.StringVar(value=str(settings.get("java_path", "")))
        self.close_on_launch = tk.BooleanVar(value=bool(settings.get("close_on_launch", False)))
        self.ram = total_ram_gb()

        tk.Frame(self, bg=ACCENT, height=px(4)).pack(fill="x")
        body = tk.Frame(self, bg=CARD, padx=px(28), pady=px(22))
        body.pack(fill="both", expand=True)
        app.text(body, "CONFIGURAÇÕES", 8, "bold", ACCENT).pack(anchor="w")
        app.text(body, "Ajuste o launcher do seu jeito", 15, "bold").pack(anchor="w", pady=(px(2), 0))

        # Tema
        self.section(body, "TEMA", "Aparência do launcher.")
        row = tk.Frame(body, bg=BORDER, padx=1, pady=1)
        row.pack(fill="x")
        self.theme_buttons = {}
        for column, (key, label) in enumerate((("light", "☀  Claro"), ("dark", "☾  Escuro"))):
            row.columnconfigure(column, weight=1, uniform="theme")
            button = tk.Button(row, text=label, relief="flat", bd=0, cursor="hand2",
                               font=("Segoe UI", 10, "bold"), pady=px(8),
                               command=lambda k=key: self.pick_theme(k))
            button.grid(row=0, column=column, sticky="nsew", padx=(0, 1) if column == 0 else 0)
            self.theme_buttons[key] = button
        self.pick_theme(self.theme)

        # Memória
        self.section(body, "MEMÓRIA DO MINECRAFT", "Quanto de RAM o jogo pode usar (-Xmx).")
        memory_row = tk.Frame(body, bg=CARD)
        memory_row.pack(fill="x")
        self.memory_label = app.text(memory_row, "", 13, "bold", ACCENT, width=6, anchor="e")
        self.memory_label.pack(side="right")
        ttk.Scale(memory_row, from_=MIN_GB, to=MAX_GB, variable=self.memory,
                  command=lambda v: self.on_memory()).pack(side="left", fill="x", expand=True,
                                                           padx=(0, px(12)), pady=px(4))
        self.memory_hint = app.text(body, "", 8, color=MUTED, anchor="w", justify="left")
        self.memory_hint.pack(fill="x", pady=(px(4), 0))
        self.on_memory()

        # Java
        self.section(body, "JAVA", "Deixe em automático para o launcher detectar a versão certa.")
        java_row = tk.Frame(body, bg=CARD)
        java_row.pack(fill="x")
        box = tk.Frame(java_row, bg=FIELD, highlightthickness=1, highlightbackground=BORDER, padx=px(8))
        box.pack(side="left", fill="x", expand=True)
        self.java_label = app.text(box, "", 9, anchor="w")
        self.java_label.pack(fill="x", ipady=px(6))
        app.button(java_row, "Procurar…", self.browse_java, primary=False, font=("Segoe UI", 9),
                   padx=px(10), pady=px(5)).pack(side="left", padx=(px(8), 0))
        self.auto_link = app.link(body, "Usar detecção automática", lambda: self.java.set(""),
                                  color=ACCENT, hover=ACCENT_HOVER, font=("Segoe UI", 8, "bold"))
        self.auto_link.pack(anchor="w", pady=(px(5), 0))
        self.java.trace_add("write", lambda *_: self.on_java())
        self.on_java()

        # Comportamento
        self.section(body, "AO INICIAR O JOGO", None)
        ttk.Checkbutton(body, text="Fechar o launcher quando o Minecraft abrir",
                        variable=self.close_on_launch).pack(anchor="w")

        # Ações
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        actions = tk.Frame(self, bg=CARD, padx=px(28), pady=px(14))
        actions.pack(fill="x")
        app.button(actions, "Salvar", self.save, font=("Segoe UI", 10, "bold"),
                   padx=px(22), pady=px(7)).pack(side="right")
        app.button(actions, "Cancelar", self.destroy, primary=False, font=("Segoe UI", 10),
                   padx=px(16), pady=px(7)).pack(side="right", padx=(0, px(8)))

        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.save())
        self.update_idletasks()
        width = max(px(470), self.winfo_reqwidth())
        height = self.winfo_reqheight()
        x = app.winfo_rootx() + (app.winfo_width() - width) // 2
        y = app.winfo_rooty() + (app.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_set()

    def section(self, parent, title, hint):
        px = self.app.px
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(px(18), px(14)))
        self.app.text(parent, title, 8, "bold", MUTED).pack(anchor="w")
        if hint:
            self.app.text(parent, hint, 8, color=MUTED).pack(anchor="w", pady=(0, px(8)))
        else:
            tk.Frame(parent, bg=CARD, height=px(6)).pack()

    def pick_theme(self, key):
        self.theme = key
        for name, button in self.theme_buttons.items():
            selected = name == key
            button.configure(bg=ACCENT if selected else FIELD, fg="#ffffff" if selected else TEXT,
                             activebackground=ACCENT_HOVER if selected else BORDER,
                             activeforeground="#ffffff" if selected else TEXT)

    def on_memory(self):
        gb = round(self.memory.get() * 2) / 2
        if gb != self.memory.get():
            self.memory.set(gb)
        self.memory_label.configure(text=format_gb(gb))
        if self.ram:
            hint, color = f"Seu computador tem {format_gb(round(self.ram))} de RAM.", MUTED
            if gb > self.ram * 0.75:
                hint, color = hint + " Reservar tanto pode travar o sistema.", WARN
            else:
                hint += " Para a maioria das versões, 2–4 GB bastam."
        else:
            hint, color = "Para a maioria das versões, 2–4 GB bastam.", MUTED
        self.memory_hint.configure(text=hint, fg=color)

    def on_java(self):
        path = self.java.get()
        self.java_label.configure(text=path or "Detectar automaticamente",
                                  fg=TEXT if path else MUTED)
        if path:
            self.auto_link.pack(anchor="w", pady=(self.app.px(5), 0))
        else:
            self.auto_link.pack_forget()

    def browse_java(self):
        types = [("java.exe", "java.exe"), ("Executáveis", "*.exe")] if os.name == "nt" else [("java", "java")]
        path = filedialog.askopenfilename(parent=self, title="Escolha o executável do Java", filetypes=types)
        if path:
            self.java.set(os.path.normpath(path))

    def save(self):
        self.app.apply_settings({
            "theme": self.theme,
            "memory_mb": int(round(self.memory.get() * 2) / 2 * 1024),
            "java_path": self.java.get().strip(),
            "close_on_launch": self.close_on_launch.get(),
        })
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description="Minecraft Pirata - Minecraft de graça!.")
    parser.add_argument("--check", metavar="VERSAO", help="Valida uma instalação sem abrir o jogo nem extrair arquivos.")
    parser.add_argument("--nick", default="Player", help="Nick usado na validação (padrão: Player).")
    options = parser.parse_args()
    if options.check:
        try:
            plan = build_plan(options.check, options.nick, load_settings())
        except (LaunchError, OSError, ValueError, KeyError, TypeError) as error:
            parser.exit(1, f"Erro: {error}\n")
        print(f"OK: {plan.version} | Java {plan.java_version} | {plan.command[0]}")
        print("Manifesto, JAR, bibliotecas e índice de assets encontrados. O jogo não foi iniciado.")
        return
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        try:  # ícone próprio na barra de tarefas, em vez do ícone do Python
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BlockLauncher.Launcher")
        except (AttributeError, OSError):
            pass
    Launcher().mainloop()


if __name__ == "__main__":
    main()
