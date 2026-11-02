"""Standard-user desktop browser and explicit-file CLI."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import __version__
from .documents import FORMATS, export, filtered, load_document


def gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("SIWX × wexport · 安全离线版")
    root.geometry("980x680")
    document = None
    query, fmt, status = tk.StringVar(), tk.StringVar(value="html"), tk.StringVar(value="请选择已有聊天 JSON 文件")
    bar = ttk.Frame(root, padding=12)
    bar.pack(fill="x")
    ttk.Label(root, text="离线浏览与导出 · 不读取微信进程 · 不提取密钥 · 不联网", padding=8).pack(fill="x")
    table = ttk.Treeview(root, columns=("time", "sender", "text"), show="headings")
    for key, label, width in (("time", "时间", 170), ("sender", "发送者", 150), ("text", "内容", 580)):
        table.heading(key, text=label)
        table.column(key, width=width)
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    table.pack(in_=frame, side="left", fill="both", expand=True)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
    scroll.pack(side="right", fill="y")
    table.configure(yscrollcommand=scroll.set)
    preview = tk.Text(root, height=7, wrap="word")
    preview.pack(fill="x", padx=12, pady=8)
    preview.configure(state="disabled")
    ttk.Label(root, textvariable=status, padding=10).pack(fill="x")
    visible = []

    def refresh(*_):
        nonlocal visible
        table.delete(*table.get_children())
        visible = filtered(document, query.get())["messages"] if document else []
        for i, m in enumerate(visible[:5000]):
            table.insert("", "end", iid=str(i), values=(m["createTime"], m["senderDisplayName"], m["content"][:300]))
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.configure(state="disabled")
        status.set(f"匹配 {len(visible)} 条；列表最多显示 5000 条，导出包含全部匹配结果。")

    def select(*_):
        selected = table.selection()
        if selected:
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", visible[int(selected[0])]["content"])
            preview.configure(state="disabled")

    def open_file():
        nonlocal document
        path = filedialog.askopenfilename(filetypes=[("聊天导出 JSON", "*.json")])
        if path:
            try:
                loaded = load_document(Path(path))
                document = loaded
                query.set("")
                refresh()
            except (ValueError, OSError, RecursionError, OverflowError) as exc:
                messagebox.showerror("无法导入", str(exc))

    def save():
        if document is None:
            messagebox.showinfo("提示", "请先选择 JSON 文件。")
            return
        target = filedialog.asksaveasfilename(defaultextension="." + fmt.get(),
                                            initialfile="聊天导出." + fmt.get())
        if target:
            try:
                export(filtered(document, query.get()), Path(target), fmt.get())
                messagebox.showinfo("导出完成", "已保存匹配的记录。")
            except (ValueError, OSError) as exc:
                messagebox.showerror("未保存", str(exc) + "\n请选择一个尚不存在的文件名。")

    ttk.Button(bar, text="选择 JSON 文件", command=open_file).pack(side="left")
    ttk.Label(bar, text="  搜索  ").pack(side="left")
    ttk.Entry(bar, textvariable=query).pack(side="left", fill="x", expand=True)
    ttk.Combobox(bar, textvariable=fmt, values=FORMATS, state="readonly", width=7).pack(side="left", padx=8)
    ttk.Button(bar, text="导出搜索结果", command=save).pack(side="left")
    query.trace_add("write", refresh)
    table.bind("<<TreeviewSelect>>", select)
    root.mainloop()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="SIWX × wexport 安全离线版：仅处理主动选择的 JSON 导出文件")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--format", choices=FORMATS, default="html")
    parser.add_argument("--search", default="")
    args = parser.parse_args(argv)
    if args.input is None:
        if args.out or args.search:
            parser.error("请指定输入 JSON 文件")
        gui()
        return 0
    if args.out is None:
        parser.error("命令行导出必须指定 --out；已有文件不会被覆盖")
    try:
        document = filtered(load_document(args.input), args.search)
        export(document, args.out, args.format)
    except (ValueError, OSError, RecursionError, OverflowError) as exc:
        parser.exit(1, f"无法处理：{exc}\n")
    print(f"已离线导出 {len(document['messages'])} 条记录。")
    return 0
