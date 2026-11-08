#!/usr/bin/env python3
"""tdata_gui — графический интерфейс tdata-switcher (tkinter, без доп. зависимостей)."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

import tdata_core as core
from tdata_core import PasswordRequired, SwitcherError, TelegramNotFound

SWATCH = 12        # размер цветного квадратика, px
STRIP_MAX = 5      # сколько квадратиков меток показываем в строке списка
STRIP_GAP = 3
ACTIVE_BORDER = "#202020"
NO_LABEL = object()  # служебный ключ фильтра «Без метки»


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def frame_background(widget) -> str:
    return ttk.Style(widget).lookup("TFrame", "background") or "#f0f0f0"


# ---------------------------------------------------------------- виджеты

class ChipBar(tk.Frame):
    """Цветные плашки-метки; сами переносятся на следующую строку при нехватке ширины."""

    def __init__(self, master, bg: str):
        super().__init__(master, bg=bg, height=1)
        self.bg = bg
        self.chips: list[tuple[object, tk.Frame]] = []
        self._height = 1
        self.bind("<Configure>", lambda _e: self.reflow())

    def clear(self) -> None:
        for _key, chip in self.chips:
            chip.destroy()
        self.chips = []

    def add_chip(self, key, text: str, color: str, on_click) -> None:
        border = tk.Frame(self, bg=self.bg, padx=2, pady=2, cursor="hand2")
        label = tk.Label(border, text=text, bg=color, fg=core.text_color_for(color),
                         padx=8, pady=2, cursor="hand2")
        label.pack()
        for w in (border, label):
            w.bind("<Button-1>", lambda _e, k=key: on_click(k))
        self.chips.append((key, border))

    def set_active(self, keys: set) -> None:
        for key, chip in self.chips:
            chip.configure(bg=ACTIVE_BORDER if key in keys else self.bg)

    def reflow(self) -> None:
        width = max(self.winfo_width(), 1)
        x = y = row_h = 0
        for _key, chip in self.chips:
            w, h = chip.winfo_reqwidth(), chip.winfo_reqheight()
            if x and x + w > width:
                x, y, row_h = 0, y + row_h, 0
            chip.place(x=x, y=y)
            x += w + 2
            row_h = max(row_h, h)
        need = max(y + row_h, 1)
        if need != self._height:  # защита от бесконечного <Configure>
            self._height = need
            self.configure(height=need)


class LabelDialog(tk.Toplevel):
    """Диалог метки: название + цвет из палитры (или любой через системный выбор цвета)."""

    def __init__(self, parent, title: str, validator, name: str = "", color: str | None = None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.validator = validator
        self.result: tuple[str, str] | None = None
        self.color = core.normalize_color(color) if color else core.LABEL_PALETTE[0]
        self._bg = frame_background(self)
        self.cells: dict[str, tk.Frame] = {}

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Название:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=name)
        self.name_var.trace_add("write", lambda *_: self._refresh_preview())
        self.entry = ttk.Entry(frm, textvariable=self.name_var, width=30)
        self.entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))

        ttk.Label(frm, text="Цвет:").grid(row=2, column=0, sticky="w")
        palette = tk.Frame(frm, bg=self._bg)
        palette.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 4))
        for i, c in enumerate(core.LABEL_PALETTE):
            cell = tk.Frame(palette, bg=self._bg, padx=2, pady=2, cursor="hand2")
            swatch = tk.Label(cell, bg=c, width=3, height=1, cursor="hand2")
            swatch.pack()
            for w in (cell, swatch):
                w.bind("<Button-1>", lambda _e, col=c: self._pick(col))
            cell.grid(row=i // 5, column=i % 5)
            self.cells[c] = cell
        ttk.Button(frm, text="Другой цвет…", command=self._custom).grid(
            row=4, column=0, sticky="w", pady=(4, 8))

        self.preview = tk.Label(frm, padx=10, pady=3)
        self.preview.grid(row=4, column=1, sticky="e", pady=(4, 8))

        self.error = tk.StringVar()
        ttk.Label(frm, textvariable=self.error, foreground="#c0392b").grid(
            row=5, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(frm)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._pick(self.color)

        self.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 60}")
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _refresh_preview(self) -> None:
        self.preview.configure(text=" ".join(self.name_var.get().split()) or "Метка",
                               bg=self.color, fg=core.text_color_for(self.color))

    def _pick(self, color: str) -> None:
        self.color = color
        for c, cell in self.cells.items():
            cell.configure(bg=ACTIVE_BORDER if c == color else self._bg)
        self._refresh_preview()

    def _custom(self) -> None:
        _rgb, chosen = colorchooser.askcolor(color=self.color, parent=self, title="Цвет метки")
        if chosen:
            try:
                self._pick(core.normalize_color(chosen))
            except SwitcherError:
                pass

    def _ok(self) -> None:
        try:
            clean = self.validator(self.name_var.get())
        except SwitcherError as e:
            self.error.set(str(e))
            return
        self.result = (clean, self.color)
        self.destroy()


class SessionDialog(tk.Toplevel):
    """Параметры входа по .session: какая сессия создаётся и пароль двухэтапной проверки."""

    def __init__(self, parent, count: int, new_default: bool):
        super().__init__(parent)
        self.title("Вход по .session")
        self.resizable(False, False)
        self.transient(parent)
        self.result: tuple[str, str | None] | None = None

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"Файлов .session: {count}. Понадобится доступ к Telegram.").grid(
            row=0, column=0, sticky="w")

        self.new_var = tk.BooleanVar(value=new_default)
        ttk.Checkbutton(frm, text="Создать новую сессию (безопаснее)",
                        variable=self.new_var).grid(row=1, column=0, sticky="w", pady=(10, 2))
        ttk.Label(
            frm, foreground="#666", wraplength=400, justify="left",
            text=("Выключено: берётся тот же ключ, что в .session. Быстро, но потом не запускайте "
                  "эту .session параллельно с Telegram Desktop: Telegram может её сбросить.\n"
                  "Включено: .session остаётся рабочей независимо от Telegram Desktop. "
                  "Если включена двухэтапная проверка, понадобится пароль."),
        ).grid(row=2, column=0, sticky="w")

        ttk.Label(frm, text="Пароль 2FA (если есть):").grid(row=3, column=0, sticky="w", pady=(10, 2))
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(frm, textvariable=self.pw_var, show="*", width=32)
        self.pw_entry.grid(row=4, column=0, sticky="w")

        buttons = ttk.Frame(frm)
        buttons.grid(row=5, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Импортировать", command=self._ok).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 60}")
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.pw_entry.focus_set()

    def _ok(self) -> None:
        self.result = ("new" if self.new_var.get() else "current", self.pw_var.get() or None)
        self.destroy()


class LabelManager(tk.Toplevel):
    """Окно «Метки»: список всех меток с цветом и числом аккаунтов; добавить / изменить / удалить."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("Метки")
        self.geometry("380x340")
        self.minsize(320, 240)
        self.transient(app)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.tree = ttk.Treeview(body, columns=("count",), selectmode="browse")
        self.tree.heading("#0", text="Метка", anchor="w")
        self.tree.heading("count", text="Аккаунтов")
        self.tree.column("#0", width=230, stretch=True)
        self.tree.column("count", width=80, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(row, text="Добавить…", command=self._add).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Изменить…", command=self._edit).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Удалить", command=self._delete).pack(side="left")
        ttk.Button(row, text="Закрыть", command=self.destroy).pack(side="right")

        self.tree.bind("<Double-1>", lambda _e: self._edit())
        self.tree.bind("<Delete>", lambda _e: self._delete())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.reload()

    def reload(self) -> None:
        counts = self.app.label_counts()
        self.tree.delete(*self.tree.get_children())
        for name, color in self.app.labels["labels"].items():
            self.tree.insert("", "end", iid=name, text=name, image=self.app.swatch(color),
                             values=(counts.get(name, 0),))

    def _current(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _add(self) -> None:
        self.app.new_label(assign=False)

    def _edit(self) -> None:
        name = self._current()
        if name:
            self.app.edit_label(name)

    def _delete(self) -> None:
        name = self._current()
        if name:
            self.app.delete_label(name)


# --------------------------------------------------------------- приложение

class App(tk.Tk):
    def __init__(self, root_dir: Path):
        super().__init__()
        self.root_dir = root_dir
        self.accounts: list[tuple[str, Path]] = []
        self.shown: dict[str, tuple[str, Path]] = {}  # iid строки → аккаунт
        self.labels: dict = {"labels": {}, "accounts": {}}
        self.filter_keys: set = set()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.buttons: list[ttk.Button] = []
        self.manager: LabelManager | None = None
        self._swatches: dict[str, tk.PhotoImage] = {}
        self._strips: dict[tuple, tk.PhotoImage] = {}
        self._menu_vars: list[tk.BooleanVar] = []
        self._popup_menu: tk.Menu | None = None

        self.title(f"tdata-switcher {core.__version__}")
        self.geometry("700x580")
        self.minsize(520, 420)

        self._build()
        self.refresh()
        self.search_entry.focus_set()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        bg = frame_background(self)

        header = ttk.Frame(self)
        header.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(header, text="Папка с аккаунтами:").pack(side="left")
        ttk.Button(header, text="Открыть", command=self.open_root).pack(side="right")
        ttk.Label(header, text=str(self.root_dir), foreground="#666").pack(
            side="left", padx=(6, 6), fill="x", expand=True)

        search = ttk.Frame(self)
        search.pack(fill="x", padx=10, pady=4)
        ttk.Label(search, text="Поиск:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refilter())
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))

        chips_row = ttk.Frame(self)
        chips_row.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(chips_row, text="Метки:").pack(side="left", anchor="n", pady=(4, 0))
        self.chips_hint = ttk.Label(
            chips_row, foreground="#666",
            text="пока нет — создайте через кнопку «Метки ▾»")
        self.chips_hint.pack(side="left", padx=(6, 0), pady=(4, 0))
        self.chipbar = ChipBar(chips_row, bg)
        self.chipbar.pack(side="left", fill="x", expand=True, padx=(6, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree = ttk.Treeview(body, columns=("labels", "last"), selectmode="extended")
        self.tree.heading("#0", text="Аккаунт", anchor="w")
        self.tree.heading("labels", text="Метки", anchor="w")
        self.tree.heading("last", text="")
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("labels", width=190, stretch=True)
        self.tree.column("last", width=90, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=(4, 2))
        for text, command in (
            ("Запустить", self.launch_selected),
            ("Ярлык на рабочий стол", self.make_shortcuts),
            ("Метки ▾", self._popup_label_menu),
            ("Импорт…", self.import_archives),
            ("Обновить", self.refresh),
        ):
            btn = ttk.Button(row, text=text, command=command)
            btn.pack(side="left", padx=(0, 6))
            self.buttons.append(btn)
        self.labels_btn = self.buttons[2]

        self.close_var = tk.BooleanVar(value=bool(core.load_settings().get("close_after_launch")))
        self.close_var.trace_add(
            "write", lambda *_: core.save_settings(close_after_launch=self.close_var.get()))
        ttk.Checkbutton(self, text="Закрывать окно после запуска",
                        variable=self.close_var).pack(anchor="w", padx=10, pady=(2, 0))

        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w",
                  padding=(6, 2)).pack(fill="x", side="bottom", pady=(6, 0))

        self.tree.bind("<Double-1>", lambda _e: self.launch_selected())
        self.tree.bind("<Return>", lambda _e: self.launch_selected())
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Button-2>", self._popup)
        self.search_entry.bind("<Return>", lambda _e: self.launch_selected())
        self.search_entry.bind("<Down>", lambda _e: (self.tree.focus_set(), "break")[1])
        self.bind("<F5>", lambda _e: self.refresh())
        self.bind("<Control-i>", lambda _e: self.import_archives())
        self.bind("<Control-f>", lambda _e: self.search_entry.focus_set())
        self.bind("<Control-l>", lambda _e: self._popup_label_menu())
        self.bind("<Escape>", lambda _e: self.search_var.set(""))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for btn in self.buttons:
            btn.state(["disabled"] if busy else ["!disabled"])

    # ---------------------------------------------------- картинки (цвета)

    def swatch(self, color: str) -> tk.PhotoImage:
        img = self._swatches.get(color)
        if img is None:
            img = tk.PhotoImage(master=self, width=SWATCH, height=SWATCH)
            img.put(color, to=(0, 0, SWATCH, SWATCH))
            self._swatches[color] = img
        return img

    def _strip(self, colors: list[str]) -> tk.PhotoImage:
        """Полоска цветных квадратиков фиксированной ширины (чтобы имена выравнивались)."""
        key = tuple(colors[:STRIP_MAX])
        img = self._strips.get(key)
        if img is None:
            img = tk.PhotoImage(master=self, width=STRIP_MAX * (SWATCH + STRIP_GAP), height=SWATCH)
            for i, c in enumerate(key):
                x = i * (SWATCH + STRIP_GAP)
                img.put(c, to=(x, 0, x + SWATCH, SWATCH))
            self._strips[key] = img
        return img

    # ------------------------------------------------------------ аккаунты

    def refresh(self) -> None:
        try:
            self.accounts = core.find_accounts(self.root_dir)
        except OSError as e:
            self.accounts = []
            messagebox.showerror("Ошибка чтения папки", str(e), parent=self)
        self.labels = core.load_labels(self.root_dir)
        self._rebuild_chips()
        self._fill()
        if self.busy:
            return
        if self.accounts:
            self.status.set(f"Аккаунтов: {len(self.accounts)}")
        else:
            self.status.set("Аккаунтов нет — нажмите «Импорт…» или положите папки в tdata/")

    def label_counts(self) -> dict[str, int]:
        """Сколько из ТЕКУЩИХ аккаунтов несёт каждая метка."""
        counts = {n: 0 for n in self.labels["labels"]}
        for name, _p in self.accounts:
            for label in core.account_labels(self.labels, name):
                counts[label] += 1
        return counts

    def _matches(self, name: str, query: str) -> bool:
        mine = core.account_labels(self.labels, name)
        if NO_LABEL in self.filter_keys:
            if mine:
                return False
        elif not all(k in mine for k in self.filter_keys):
            return False
        if query and query not in name.lower() and not any(query in m.lower() for m in mine):
            return False
        return True

    def _fill(self) -> None:
        query = self.search_var.get().strip().lower()
        last = core.load_last()
        keep = {self.shown[i][0] for i in self.tree.selection() if i in self.shown}
        self.tree.delete(*self.tree.get_children())
        self.shown.clear()
        to_select = []
        for idx, acc in enumerate(self.accounts):
            name = acc[0]
            if not self._matches(name, query):
                continue
            mine = core.account_labels(self.labels, name)
            colors = [self.labels["labels"][m] for m in mine]
            iid = str(idx)
            self.shown[iid] = acc
            self.tree.insert("", "end", iid=iid, text=name, image=self._strip(colors),
                             values=(", ".join(mine), "последний" if name == last else ""))
            if name in keep:
                to_select.append(iid)
        if not to_select:
            first_last = next((i for i, a in self.shown.items() if a[0] == last), None)
            fallback = first_last or next(iter(self.shown), None)
            to_select = [fallback] if fallback else []
        if to_select:
            self.tree.selection_set(to_select)
            self.tree.focus(to_select[0])
            self.tree.see(to_select[0])

    def _refilter(self) -> None:
        self._fill()
        if self.search_var.get().strip() or self.filter_keys:
            self.status.set(f"Показано {len(self.shown)} из {len(self.accounts)}")

    def _selected(self) -> list[tuple[str, Path]]:
        return [self.shown[i] for i in self.tree.selection() if i in self.shown]

    def _select_names(self, names: list[str]) -> None:
        wanted = [i for i, a in self.shown.items() if a[0] in set(names)]
        if wanted:
            self.tree.selection_set(wanted)
            self.tree.see(wanted[0])

    # -------------------------------------------------------------- метки

    def _rebuild_chips(self) -> None:
        labels = self.labels["labels"]
        counts = self.label_counts()
        unlabeled = sum(1 for name, _p in self.accounts
                        if not core.account_labels(self.labels, name))
        self.filter_keys &= set(labels) | {NO_LABEL}  # удалённые метки из фильтра уходят
        self.chipbar.clear()
        for name, color in labels.items():
            self.chipbar.add_chip(name, f"{name} · {counts[name]}", color, self._toggle_filter)
        if labels:
            self.chipbar.add_chip(NO_LABEL, f"Без метки · {unlabeled}", "#d0d0d0",
                                  self._toggle_filter)
            self.chips_hint.pack_forget()
        else:
            self.chips_hint.pack(side="left", padx=(6, 0), pady=(4, 0), before=self.chipbar)
        self.chipbar.set_active(self.filter_keys)
        self.chipbar.reflow()

    def _toggle_filter(self, key) -> None:
        if key is NO_LABEL:
            self.filter_keys = set() if NO_LABEL in self.filter_keys else {NO_LABEL}
        else:
            self.filter_keys.discard(NO_LABEL)
            self.filter_keys ^= {key}  # переключатель; несколько меток = аккаунты со ВСЕМИ ими
        self.chipbar.set_active(self.filter_keys)
        self._refilter()
        if not self.filter_keys and not self.search_var.get().strip():
            self.status.set(f"Аккаунтов: {len(self.accounts)}")

    def _labels_changed(self) -> None:
        self._rebuild_chips()
        self._fill()
        if self.manager is not None and self.manager.winfo_exists():
            self.manager.reload()

    def _build_label_menu(self, parent) -> tk.Menu:
        """Меню «Метки»: галочки для выделенных аккаунтов + создание и управление."""
        menu = tk.Menu(parent, tearoff=0)
        selected = [n for n, _p in self._selected()]
        self._menu_vars = []
        for name, color in self.labels["labels"].items():
            has_all = bool(selected) and all(
                name in core.account_labels(self.labels, a) for a in selected)
            var = tk.BooleanVar(value=has_all)
            self._menu_vars.append(var)
            menu.add_checkbutton(
                label=name, variable=var, image=self.swatch(color), compound="left",
                state="normal" if selected else "disabled",
                command=lambda n=name, v=var: self._toggle_label(n, bool(v.get())))
        if self.labels["labels"]:
            menu.add_separator()
        menu.add_command(label="Новая метка…", command=lambda: self.new_label(assign=True))
        menu.add_command(label="Управление метками…", command=self.manage_labels)
        return menu

    def _popup_label_menu(self) -> None:
        menu = self._build_label_menu(self)
        self._popup_menu = menu
        b = self.labels_btn
        try:
            menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            menu.grab_release()

    def _popup(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Запустить", command=self.launch_selected)
        menu.add_cascade(label="Метки", menu=self._build_label_menu(menu))
        menu.add_command(label="Ярлык на рабочий стол", command=self.make_shortcuts)
        menu.add_command(label="Открыть папку аккаунта", command=self.open_account_folder)
        self._popup_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_label(self, label: str, present: bool) -> None:
        names = [n for n, _p in self._selected()]
        if not names:
            return
        try:
            self.labels = core.set_account_label(self.root_dir, names, label, present)
        except SwitcherError as e:
            messagebox.showerror("Метки", str(e), parent=self)
            return
        self._labels_changed()
        verb = "поставлена" if present else "снята"
        self.status.set(f"Метка «{label}» {verb}: аккаунтов {len(names)}")

    def _ask_label(self, title: str, validator, name: str = "", color: str | None = None):
        dialog = LabelDialog(self, title, validator, name, color)
        self.wait_window(dialog)
        return dialog.result

    def new_label(self, assign: bool = False) -> None:
        """Создать метку; assign=True — сразу повесить на выделенные аккаунты."""
        result = self._ask_label(
            "Новая метка", lambda n: core.check_label_name(self.labels, n),
            color=core.next_label_color(self.labels))
        if not result:
            return
        name, color = result
        targets = [n for n, _p in self._selected()] if assign else []
        try:
            self.labels = core.add_label(self.root_dir, name, color)
            if targets:
                self.labels = core.set_account_label(self.root_dir, targets, name, True)
        except SwitcherError as e:
            messagebox.showerror("Метки", str(e), parent=self)
        self._labels_changed()
        if targets:
            self.status.set(f"Метка «{name}» создана и поставлена: аккаунтов {len(targets)}")
        else:
            self.status.set(f"Метка «{name}» создана")

    def edit_label(self, name: str) -> None:
        old_color = self.labels["labels"].get(name)
        if old_color is None:
            return
        result = self._ask_label(
            "Изменить метку", lambda n: core.check_label_name(self.labels, n, ignore=name),
            name, old_color)
        if not result:
            return
        new_name, color = result
        try:
            if color != old_color:
                self.labels = core.recolor_label(self.root_dir, name, color)
            if new_name != name:
                self.labels = core.rename_label(self.root_dir, name, new_name)
                if name in self.filter_keys:
                    self.filter_keys.discard(name)
                    self.filter_keys.add(new_name)
        except SwitcherError as e:
            messagebox.showerror("Метки", str(e), parent=self)
        self._labels_changed()

    def delete_label(self, name: str) -> None:
        used = self.label_counts().get(name, 0)
        extra = f"\nОна будет снята с аккаунтов: {used}." if used else ""
        if not messagebox.askyesno("Удалить метку", f"Удалить метку «{name}»?{extra}", parent=self):
            return
        try:
            self.labels = core.remove_label(self.root_dir, name)
        except SwitcherError as e:
            messagebox.showerror("Метки", str(e), parent=self)
        self._labels_changed()

    def manage_labels(self) -> None:
        if self.manager is not None and self.manager.winfo_exists():
            self.manager.lift()
            self.manager.focus_set()
            return
        self.manager = LabelManager(self)

    # ------------------------------------------------------------- запуск

    def launch_selected(self) -> None:
        sel = self._selected()
        if not sel:
            self.status.set("Выберите аккаунт в списке")
            return
        name, path = sel[0]
        try:
            try:
                core.launch(name, path)
            except TelegramNotFound:
                tg = filedialog.askopenfilename(
                    parent=self, title="Telegram.exe не найден — укажите его вручную",
                    filetypes=[("Telegram", "Telegram.exe"), ("Все файлы", "*.*")])
                if not tg:
                    self.status.set("Telegram.exe не указан")
                    return
                core.save_settings(telegram=tg)
                core.launch(name, path, telegram=Path(tg))
        except SwitcherError as e:
            messagebox.showerror("Не удалось запустить", str(e), parent=self)
            return
        self.status.set(f"Запущен: {name}")
        self._fill()
        if self.close_var.get():
            self.after(600, self.destroy)

    # ------------------------------------------------------------- ярлыки

    def make_shortcuts(self) -> None:
        sel = self._selected()
        if not sel:
            self.status.set("Выберите аккаунт (или несколько с Ctrl/Shift)")
            return
        made, errors = [], []
        for name, _path in sel:
            try:
                made.append(core.create_shortcut(name, self.root_dir))
            except SwitcherError as e:
                errors.append(f"{name}: {e}")
        if made:
            self.status.set(f"Ярлыков создано: {len(made)}")
            messagebox.showinfo(
                "Ярлыки созданы",
                f"Создано ярлыков: {len(made)}\nПапка: {made[0].parent}", parent=self)
        if errors:
            messagebox.showerror("Ярлыки", "\n".join(errors), parent=self)

    # ------------------------------------------------------------- папки

    def open_root(self) -> None:
        try:
            self.root_dir.mkdir(parents=True, exist_ok=True)
            open_in_file_manager(self.root_dir)
        except OSError as e:
            messagebox.showerror("Ошибка", str(e), parent=self)

    def open_account_folder(self) -> None:
        sel = self._selected()
        if not sel:
            return
        try:
            open_in_file_manager(sel[0][1])
        except OSError as e:
            messagebox.showerror("Ошибка", str(e), parent=self)

    # ------------------------------------------------------------- импорт

    def import_archives(self) -> None:
        if self.busy:
            return
        files = filedialog.askopenfilenames(
            parent=self, title="Выберите архивы с tdata или файлы .session",
            filetypes=[("Архивы и сессии", "*.zip *.rar *.7z *.session"),
                       ("Архивы", "*.zip *.rar *.7z"), ("Сессии Telegram", "*.session"),
                       ("Все файлы", "*.*")])
        if not files:
            return
        paths = [Path(f) for f in files]
        session_opts = ("current", None)
        sessions = [p for p in paths if core.is_session_file(p)]
        if sessions:
            dialog = SessionDialog(self, len(sessions),
                                   bool(core.load_settings().get("session_new")))
            self.wait_window(dialog)
            if dialog.result is None:
                return
            session_opts = dialog.result
            core.save_settings(session_new=session_opts[0] == "new")
        self._set_busy(True)
        self.status.set("Импорт…")
        threading.Thread(target=self._import_worker, args=(paths, session_opts),
                         daemon=True).start()

    def _import_worker(self, archives: list[Path], session_opts: tuple = ("current", None)) -> None:
        """Работает в потоке; с интерфейсом общается только через очередь."""
        imported: list[str] = []
        errors: list[str] = []
        mode, session_password = session_opts
        for archive in archives:
            is_session = core.is_session_file(archive)
            password = session_password if is_session else None
            for _attempt in range(3):
                try:
                    progress = lambda msg: self.events.put(("status", msg))  # noqa: E731
                    if is_session:
                        name, _path = core.import_session(
                            archive, self.root_dir, mode=mode, password=password, progress=progress)
                        imported.append(name)
                    else:
                        result = core.import_archive(
                            archive, self.root_dir, password=password, progress=progress)
                        imported += [name for name, _ in result]
                    break
                except PasswordRequired:
                    reply: queue.Queue = queue.Queue()
                    self.events.put(("password", archive, reply))
                    password = reply.get()
                    if password is None:
                        errors.append(f"{archive.name}: пароль не введён")
                        break
                except SwitcherError as e:
                    errors.append(f"{archive.name}: {e}")
                    break
                except Exception as e:  # noqa: BLE001 — не даём потоку молча умереть
                    errors.append(f"{archive.name}: {type(e).__name__}: {e}")
                    break
            else:
                errors.append(f"{archive.name}: неверный пароль")
        self.events.put(("done", imported, errors))

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.status.set(event[1])
                elif kind == "password":
                    if core.is_session_file(event[1]):
                        title = "Пароль 2FA"
                        prompt = (f"Для «{event[1].name}» нужен пароль двухэтапной "
                                  "проверки (2FA).\nВведите пароль:")
                    else:
                        title = "Пароль архива"
                        prompt = f"Архив «{event[1].name}» защищён паролем.\nВведите пароль:"
                    pw = simpledialog.askstring(title, prompt, show="*", parent=self)
                    event[2].put(pw or None)
                elif kind == "done":
                    self._import_done(event[1], event[2])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _import_done(self, imported: list[str], errors: list[str]) -> None:
        self._set_busy(False)
        self.search_var.set("")
        self.refresh()
        self._select_names(imported)
        self.status.set(f"Импортировано аккаунтов: {len(imported)}")
        if imported:
            messagebox.showinfo("Импорт завершён",
                                "Добавлено:\n" + "\n".join(imported), parent=self)
        if errors:
            messagebox.showerror("Не удалось импортировать", "\n".join(errors), parent=self)


def run_gui(root_dir: Path) -> None:
    if sys.platform == "win32":
        try:  # чёткий текст на HiDPI-экранах
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App(root_dir).mainloop()
