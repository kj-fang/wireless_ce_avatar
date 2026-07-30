"""Tkinter file-dialog adapter for local desktop chatbot profiles."""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog


def _run_dialog(select: Callable[[], str]) -> str:
    result = {"path": ""}

    def open_dialog() -> None:
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        try:
            result["path"] = select() or ""
        finally:
            root.destroy()

    thread = threading.Thread(target=open_dialog)
    thread.start()
    thread.join(timeout=60)
    return result["path"]


def choose_skills_directory() -> str:
    return _run_dialog(
        lambda: filedialog.askdirectory(
            title="Select skills data directory",
        )
    )


def choose_skills_yaml() -> str:
    return _run_dialog(
        lambda: filedialog.askopenfilename(
            title="Select skills YAML file",
            filetypes=[
                ("YAML files", "*.yaml *.yml"),
                ("All files", "*.*"),
            ],
        )
    )
