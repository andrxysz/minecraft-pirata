from bisect import bisect_left, bisect_right
from tkinter import ttk


class VersionCombobox(ttk.Combobox):
    def __init__(self, parent, *, heading_background, heading_foreground, **kwargs):
        super().__init__(parent, postcommand=self._prepare_popup, **kwargs)
        self.heading_background = heading_background
        self.heading_foreground = heading_foreground
        self.headings = set()
        self.version_ids = ()
        self.selectable_rows = []
        self._last_valid = ""
        self._popup_bound = False
        self.bind("<<ComboboxSelected>>", self._guard_selection)
        self.bind("<Up>", lambda event: self._move_closed(-1))
        self.bind("<MouseWheel>", lambda event: self._move_closed(-1 if event.delta > 0 else 1))
        self.bind("<Button-4>", lambda event: self._move_closed(-1))
        self.bind("<Button-5>", lambda event: self._move_closed(1))

    def set_groups(self, installed, available):
        installed = list(dict.fromkeys(installed))
        local = set(installed)
        available = list(dict.fromkeys(value for value in available if value not in local))
        self.version_ids = tuple(installed + available)
        self.headings = set()
        if installed and available:
            rows = ["── INSTALADAS ──", *installed, "── DISPONÍVEIS PARA BAIXAR ──", *available]
            self.headings = {0, len(installed) + 1}
        else:
            rows = installed + available
        self.selectable_rows = [i for i in range(len(rows)) if i not in self.headings]
        self.configure(values=rows)
        if self.get() in self.version_ids:
            self._last_valid = self.get()
        elif self._last_valid not in self.version_ids:
            self._last_valid = self.version_ids[0] if self.version_ids else ""

    def _guard_selection(self, event=None):
        if self.get() in self.version_ids:
            self._last_valid = self.get()
        else:
            self.set(self._last_valid)

    def _next_row(self, current, direction):
        rows = self.selectable_rows
        if not rows:
            return None
        position = bisect_right(rows, current) if direction > 0 else bisect_left(rows, current) - 1
        return rows[max(0, min(position, len(rows) - 1))]

    def _move_closed(self, direction):
        if not self.instate(["disabled"]):
            row = self._next_row(self.current(), direction)
            if row is not None:
                self.current(row)
                self.event_generate("<<ComboboxSelected>>")
        return "break"

    def _prepare_popup(self):
        self.after_idle(self._style_popup)

    def _style_popup(self):
        if not self.winfo_exists():
            return
        popup = self.tk.call("ttk::combobox::PopdownWindow", self._w)
        self._listbox = str(popup) + ".f.l"
        for row in self.headings:
            self.tk.call(self._listbox, "itemconfigure", row,
                         "-background", self.heading_background, "-foreground", self.heading_foreground,
                         "-selectbackground", self.heading_background, "-selectforeground", self.heading_foreground)
        if self._popup_bound:
            return
        self._popup_bound = True
        for sequence in ("<Motion>", "<ButtonPress-1>", "<B1-Motion>"):
            self._bind(("bind", self._listbox), sequence, self._hover, None)
        self._bind(("bind", self._listbox), "<ButtonRelease-1>", self._release, None)
        self._bind(("bind", self._listbox), "<Return>", self._confirm, None)
        for key, direction in (("Up", -1), ("Down", 1), ("Prior", -10), ("Next", 10),
                               ("Home", -1000000), ("End", 1000000)):
            self._bind(("bind", self._listbox), f"<{key}>",
                       lambda event, step=direction: self._move_popup(step), None)

    def _hover(self, event):
        row = int(self.tk.call(self._listbox, "nearest", event.y))
        self.tk.call(self._listbox, "selection", "clear", 0, "end")
        if row not in self.headings:
            self.tk.call(self._listbox, "activate", row)
            self.tk.call(self._listbox, "selection", "set", row)
        return "break"
    def _release(self, event):
        row = int(self.tk.call(self._listbox, "nearest", event.y))
        self._hover(event)
        return "break" if row in self.headings else None

    def _confirm(self, event):
        selection = self.tk.splitlist(self.tk.call(self._listbox, "curselection"))
        if not selection or int(selection[0]) in self.headings:
            return "break"

    def _move_popup(self, direction):
        if not self.selectable_rows:
            return "break"
        current = int(self.tk.call(self._listbox, "index", "active"))
        if abs(direction) == 1:
            row = self._next_row(current, direction)
        else:
            target = current + direction
            row = self.selectable_rows[min(bisect_left(self.selectable_rows, target), len(self.selectable_rows) - 1)]
        self.tk.call(self._listbox, "selection", "clear", 0, "end")
        self.tk.call(self._listbox, "selection", "set", row)
        self.tk.call(self._listbox, "activate", row)
        self.tk.call(self._listbox, "see", row)
        return "break"
