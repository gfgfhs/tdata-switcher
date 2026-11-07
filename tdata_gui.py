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
from tkinter import filedialog, messagebox, simpledialog, ttk

import tdata_core as core
from tdata_core import PasswordRequired, SwitcherError, TelegramNotFound


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App(tk.Tk):
    def __init__(self, root_dir: Path):
        super().__init__()
        self.root_dir = root_dir
        self.accounts: list[tuple[str, Path]] = []
        self.shown: dict[str, tuple[str, Path]] = {}  # iid строки → аккаунт
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.buttons: list[ttk.Button] = []

        self.title(f"tdata-switcher {core.__version__}")
        self.geometry("640x500")
        self.minsize(500, 380)

        self._build()
        self.refresh()
        self.search_entry.focus_set()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
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
        self.search_var.trace_add("write", lambda *_: self._fill())
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree = ttk.Treeview(body, columns=("last",), selectmode="extended")
        self.tree.heading("#0", text="Аккаунт", anchor="w")
        self.tree.heading("last", text="")
        self.tree.column("#0", width=420, stretch=True)
        self.tree.column("last", width=110, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=(4, 2))
        for text, command in (
            ("Запустить", self.launch_selected),
            ("Ярлык на рабочий стол", self.make_shortcuts),
            ("Импорт архива…", self.import_archives),
            ("Обновить", self.refresh),
        ):
            btn = ttk.Button(row, text=text, command=command)
            btn.pack(side="left", padx=(0, 6))
            self.buttons.append(btn)

        self.close_var = tk.BooleanVar(value=bool(core.load_settings().get("close_after_launch")))
        self.close_var.trace_add(
            "write", lambda *_: core.save_settings(close_after_launch=self.close_var.get()))
        ttk.Checkbutton(self, text="Закрывать окно после запуска",
                        variable=self.close_var).pack(anchor="w", padx=10, pady=(2, 0))

        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w",
                  padding=(6, 2)).pack(fill="x", side="bottom", pady=(6, 0))

        # контекстное меню
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Запустить", command=self.launch_selected)
        self.menu.add_command(label="Ярлык на рабочий стол", command=self.make_shortcuts)
        self.menu.add_command(label="Открыть папку аккаунта", command=self.open_account_folder)

        self.tree.bind("<Double-1>", lambda e: self.launch_selected())
        self.tree.bind("<Return>", lambda e: self.launch_selected())
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Button-2>", self._popup)
        self.search_entry.bind("<Return>", lambda e: self.launch_selected())
        self.search_entry.bind("<Down>", lambda e: (self.tree.focus_set(), "break")[1])
        self.bind("<F5>", lambda e: self.refresh())
        self.bind("<Control-i>", lambda e: self.import_archives())
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Escape>", lambda e: self.search_var.set(""))

    def _popup(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for btn in self.buttons:
            btn.state(["disabled"] if busy else ["!disabled"])

    # ------------------------------------------------------------ аккаунты

    def refresh(self) -> None:
        try:
            self.accounts = core.find_accounts(self.root_dir)
        except OSError as e:
            self.accounts = []
            messagebox.showerror("Ошибка чтения папки", str(e), parent=self)
        self._fill()
        if self.busy:
            return
        if self.accounts:
            self.status.set(f"Аккаунтов: {len(self.accounts)}")
        else:
            self.status.set("Аккаунтов нет — нажмите «Импорт архива…» или положите папки в tdata/")

    def _fill(self) -> None:
        query = self.search_var.get().strip().lower()
        last = core.load_last()
        keep = {self.shown[i][0] for i in self.tree.selection() if i in self.shown}
        self.tree.delete(*self.tree.get_children())
        self.shown.clear()
        to_select = []
        for idx, acc in enumerate(self.accounts):
            name = acc[0]
            if query and query not in name.lower():
                continue
            iid = str(idx)
            self.shown[iid] = acc
            self.tree.insert("", "end", iid=iid, text=name,
                             values=("последний" if name == last else "",))
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

    def _selected(self) -> list[tuple[str, Path]]:
        return [self.shown[i] for i in self.tree.selection() if i in self.shown]

    def _select_names(self, names: list[str]) -> None:
        wanted = [i for i, a in self.shown.items() if a[0] in set(names)]
        if wanted:
            self.tree.selection_set(wanted)
            self.tree.see(wanted[0])

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
            parent=self, title="Выберите архивы с tdata",
            filetypes=[("Архивы", "*.zip *.rar *.7z"), ("Все файлы", "*.*")])
        if not files:
            return
        self._set_busy(True)
        self.status.set("Импорт…")
        threading.Thread(target=self._import_worker,
                         args=([Path(f) for f in files],), daemon=True).start()

    def _import_worker(self, archives: list[Path]) -> None:
        """Работает в потоке; с интерфейсом общается только через очередь."""
        imported: list[str] = []
        errors: list[str] = []
        for archive in archives:
            password = None
            for _attempt in range(3):
                try:
                    result = core.import_archive(
                        archive, self.root_dir, password=password,
                        progress=lambda msg: self.events.put(("status", msg)))
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
                    pw = simpledialog.askstring(
                        "Пароль архива",
                        f"Архив «{event[1].name}» защищён паролем.\nВведите пароль:",
                        show="*", parent=self)
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
