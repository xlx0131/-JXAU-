#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江西农业大学教务系统 —— 一体化 GUI 工具
====================================================
集成功能:
    1. 抢课选课（jxau_course_grabber.py）
    2. 成绩查询（jxau_grade_query.py）
    3. 课表查询（jxau_schedule_query.py）

作者: 许立鑫
侵权联系删除: xlx20050131

技术要点:
    - tkinter + ttk 构建多标签页 GUI
    - importlib 动态导入三个后端脚本核心类
    - 共享登录会话，一次登录全模块可用

兼容性:
    - Python 3.8+
    - 依赖: requests, ddddocr
    - 安装: pip install requests ddddocr

免责声明: 本程序仅用于学习研究，禁止用于非法用途
"""

import importlib.util
import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime
from typing import Optional, List, Dict
import time
import random
import re
import logging
import json
import csv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
CAPTCHA_DIR = os.path.join(SCRIPT_DIR, "captcha_cache")


# =============================================================================
# 动态导入三个后端脚本
# =============================================================================
def _resolve_path(filename):
    """定位脚本路径，兼容源码运行和 PyInstaller 打包"""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
        candidate = os.path.join(base, filename)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def _import_module(module_name, filename):
    """通过 importlib 动态导入 Python 脚本为模块"""
    path = _resolve_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到脚本: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


# ── 导入抢课脚本 ────────────────────────────────────────────────────────
_grabber_mod, _GRABBER_PATH = _import_module("_grabber_core", "jxau_course_grabber.py")
_GrabberConfig = _grabber_mod.GrabberConfig
_JXAUAuthSession = _grabber_mod.JXAUAuthSession
_CourseQuery = _grabber_mod.CourseQuery
_CourseSelector = _grabber_mod.CourseSelector
_CourseInfo = _grabber_mod.CourseInfo
_CaptchaHandler = _grabber_mod.CaptchaHandler
_get_default_year_term = _grabber_mod.get_default_year_term
_HAS_DDDDOCR = getattr(_grabber_mod, '_HAS_DDDDOCR', False)

# ── 导入成绩查询脚本 ────────────────────────────────────────────────────
_grade_mod, _GRADE_PATH = _import_module("_grade_core", "jxau_grade_query.py")
_GradeQueryService = _grade_mod.GradeQueryService
_GradeRecord = _grade_mod.GradeRecord
_grade_parse_semester_label = _grade_mod.parse_semester_label
_grade_group_by_semester = _grade_mod.group_by_semester

# ── 导入课表查询脚本 ────────────────────────────────────────────────────
_sched_mod, _SCHED_PATH = _import_module("_schedule_core", "jxau_schedule_query.py")
_ScheduleQueryService = _sched_mod.ScheduleQueryService
_SemesterManager = _sched_mod.SemesterManager
_ScheduleRecord = _sched_mod.ScheduleRecord
_sched_parse_semester_label = _sched_mod.parse_semester_label
_build_schedule_grid = _sched_mod.build_schedule_grid
_simplify_weeks = _sched_mod.simplify_weeks
SLOT_ORDER = _sched_mod.SLOT_ORDER
SLOT_SECTION = _sched_mod.SLOT_SECTION
WEEKDAY_NAMES = _sched_mod.WEEKDAY_NAMES

# ── 共享的学期标签解析（两个模块实现一致，选一个即可） ──────────────────
parse_semester_label = _grade_parse_semester_label


# =============================================================================
# GUI 日志处理器
# =============================================================================
class TextHandler(logging.Handler):
    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        self.queue = queue.Queue()
        self.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.queue.put(self.format(record))
        except Exception:
            pass


# =============================================================================
# 验证码手动输入对话框
# =============================================================================
class CaptchaDialog:
    def __init__(self, parent, img_path: str):
        self.result = ""
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("验证码手动输入")
        self.dialog.geometry("420x250")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        frame = ttk.Frame(self.dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="OCR 自动识别失败，请手动输入验证码",
                  font=("微软雅黑", 10, "bold")).pack(pady=(0, 10))
        ttk.Label(frame, text=f"验证码图片已保存至:").pack(anchor="w")
        path_label = ttk.Label(frame, text=img_path, foreground="blue", wraplength=360)
        path_label.pack(anchor="w", pady=(0, 10))

        entry_frame = ttk.Frame(frame)
        entry_frame.pack(fill="x", pady=5)
        ttk.Label(entry_frame, text="验证码:").pack(side="left", padx=(0, 8))
        self.entry = ttk.Entry(entry_frame, width=15, font=("Consolas", 14))
        self.entry.pack(side="left")
        self.entry.focus_set()

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=(12, 0))
        ttk.Button(btn_frame, text="确定", width=10, command=self._on_ok).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", width=10, command=self._on_cancel).pack(side="left", padx=5)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.entry.bind("<Return>", lambda e: self._on_ok())
        parent.wait_window(self.dialog)

    def _on_ok(self):
        self.result = self.entry.get().strip()
        self.dialog.destroy()

    def _on_cancel(self):
        self.result = ""
        self.dialog.destroy()


# =============================================================================
# GUI 版验证码处理器
# =============================================================================
class GuiCaptchaHandler(_CaptchaHandler):
    def __init__(self, config, parent_widget):
        super().__init__(config)
        self._parent = parent_widget

    def _manual_recognition(self, image_data: bytes, img_name: str) -> str:
        save_path = os.path.join(CAPTCHA_DIR, f"{img_name}.png")
        os.makedirs(CAPTCHA_DIR, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(image_data)
        result = []

        def _show():
            dlg = CaptchaDialog(self._parent, save_path)
            result.append(dlg.result)
        self._parent.after(0, _show)
        while not result:
            time.sleep(0.1)
        code = result[0]
        if code:
            logging.getLogger("JiaowuGUI").info(f"手动输入验证码: '{code}'")
        else:
            logging.getLogger("JiaowuGUI").warning("用户取消验证码输入")
        return code


# =============================================================================
# 主应用
# =============================================================================
class JiaowuGUI:
    COLORS = {
        "bg": "#f5f5f5",
        "frame_bg": "#ffffff",
        "accent": "#2b5797",
        "success": "#107c10",
        "warning": "#d4380d",
        "info": "#0078d4",
        "border": "#e0e0e0",
    }

    # ── 成绩与课表 URL（登录后注入到 config 中） ─────────────────────────
    _GRADE_QUERY_URL = "https://jwgl.jxau.edu.cn/SystemManage/CJManage/GetXsCjByXh/"
    _SEMESTER_LIST_URL = "https://jwgl.jxau.edu.cn/Common/BaseData/GetKsXq/"
    _SCHEDULE_QUERY_URL = "https://jwgl.jxau.edu.cn/PaikeManage/KebiaoInfo/GetStudentKebiaoByXq/"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("江西农业大学 · 教务一体化工具")
        self.root.geometry("1400x960")
        self.root.minsize(1100, 760)
        self.root.configure(bg=self.COLORS["bg"])

        self._setup_styles()

        # 状态
        self._auth = None
        self._config = None
        self._logged_in = False
        self._running = False
        self._stop_event = threading.Event()

        # 课程数据（抢课标签页使用）
        self._all_courses = []
        self._selected = set()

        # 日志 — 挂到 root logger 以捕获所有模块的输出
        self._logger = logging.getLogger("JiaowuGUI")
        self._text_handler = TextHandler()
        logging.getLogger().addHandler(self._text_handler)
        logging.getLogger().setLevel(logging.INFO)
        self._logger.info("GUI 启动完成")

        self._build_ui()
        self._poll_log()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── ttk 样式 ───────────────────────────────────────────────────────────
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("Accent.TButton", font=("微软雅黑", 9, "bold"))
        style.configure("Success.TButton", font=("微软雅黑", 9, "bold"), foreground="#107c10")
        style.configure("Danger.TButton", font=("微软雅黑", 9, "bold"), foreground="#d4380d")
        style.configure("Title.TLabel", font=("微软雅黑", 12, "bold"),
                        foreground=self.COLORS["accent"])

    # ── 构建界面 ───────────────────────────────────────────────────────────
    def _build_ui(self):
        pad_x = 10

        # ── 顶部标题栏 ─────────────────────────────────────────────────
        title_frame = tk.Frame(self.root, bg=self.COLORS["accent"], height=44)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)
        tk.Label(title_frame,
                 text=" 江西农业大学 · 教务一体化工具",
                 font=("微软雅黑", 13, "bold"),
                 fg="white", bg=self.COLORS["accent"]).pack(side="left", padx=14, pady=8)
        tk.Label(title_frame,
                 text="仅用于学习研究 · 禁止非法用途  ",
                 font=("微软雅黑", 8), fg="#cce0ff",
                 bg=self.COLORS["accent"]).pack(side="right", padx=14)

        # ── 登录区域（所有标签页共享） ─────────────────────────────────
        self._login_frame = ttk.LabelFrame(self.root, text=" 登录信息 ",
                                           padding=(12, 10))
        self._login_frame.pack(fill="x", padx=pad_x, pady=(6, 4))

        r1 = ttk.Frame(self._login_frame)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="学号", width=5).pack(side="left")
        self._stu_id_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self._stu_id_var, width=18,
                  font=("微软雅黑", 10)).pack(side="left", padx=(2, 20))
        ttk.Label(r1, text="密码", width=5).pack(side="left")
        self._pass_var = tk.StringVar()
        self._pass_entry = ttk.Entry(r1, textvariable=self._pass_var, width=18,
                                     show="*", font=("微软雅黑", 10))
        self._pass_entry.pack(side="left", padx=(2, 6))
        self._show_pass = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="显示", variable=self._show_pass,
                        command=self._toggle_password).pack(side="left")

        r2 = ttk.Frame(self._login_frame)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="地址", width=5).pack(side="left")
        self._base_url_var = tk.StringVar(value="https://jwgl.jxau.edu.cn")
        ttk.Entry(r2, textvariable=self._base_url_var, width=42,
                  font=("微软雅黑", 9)).pack(side="left", padx=(2, 0))
        ttk.Label(r2, text="学期", width=5).pack(side="left", padx=(12, 0))
        yt = _get_default_year_term() if _get_default_year_term else "2025-2026-1"
        self._year_term_var = tk.StringVar(value=yt)
        ttk.Entry(r2, textvariable=self._year_term_var, width=14,
                  font=("微软雅黑", 9)).pack(side="left", padx=(2, 0))

        r3 = ttk.Frame(self._login_frame)
        r3.pack(fill="x", pady=(8, 0))
        self._login_btn = ttk.Button(r3, text="登  录", style="Accent.TButton",
                                     width=12, command=self._login)
        self._login_btn.pack(side="left", padx=(0, 8))
        self._logout_btn = ttk.Button(r3, text="退出登录", width=10,
                                      command=self._logout, state="disabled")
        self._logout_btn.pack(side="left")
        self._status_label = tk.Label(r3, text=" ● 未登录",
                                      font=("微软雅黑", 9), fg="gray",
                                      bg=self.COLORS["frame_bg"])
        self._status_label.pack(side="left", padx=(16, 0))
        self._status_detail = tk.Label(r3, text="",
                                       font=("微软雅黑", 9), fg="gray",
                                       bg=self.COLORS["frame_bg"])
        self._status_detail.pack(side="left", padx=(8, 0))

        # ── Notebook 标签页 ────────────────────────────────────────────
        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill="both", expand=True, padx=pad_x, pady=(0, 2))

        # 标签页 1：抢课选课
        self._tab_grab = tk.Frame(self._notebook, bg=self.COLORS["bg"])
        self._notebook.add(self._tab_grab, text="  抢课选课  ")
        self._build_grab_tab()

        # 标签页 2：成绩查询
        self._tab_grade = tk.Frame(self._notebook, bg=self.COLORS["bg"])
        self._notebook.add(self._tab_grade, text="  成绩查询  ")
        self._build_grade_tab()

        # 标签页 3：课表查询
        self._tab_schedule = tk.Frame(self._notebook, bg=self.COLORS["bg"])
        self._notebook.add(self._tab_schedule, text="  课表查询  ")
        self._build_schedule_tab()

        # ── 底部日志 ───────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self.root, text=" 运行日志 ", padding=(8, 6))
        log_frame.pack(fill="both", expand=True, padx=pad_x, pady=(2, pad_x))
        log_toolbar = tk.Frame(log_frame, bg=self.COLORS["frame_bg"])
        log_toolbar.pack(fill="x", pady=(0, 4))
        tk.Button(log_toolbar, text="清空日志", font=("微软雅黑", 8),
                  relief="flat", bg="#e8e8e8", cursor="hand2",
                  command=self._clear_log).pack(side="right", padx=(6, 0))
        self._auto_scroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(log_toolbar, text="自动滚动", variable=self._auto_scroll_var,
                       font=("微软雅黑", 8), bg=self.COLORS["frame_bg"],
                       activebackground=self.COLORS["frame_bg"],
                       selectcolor=self.COLORS["frame_bg"]).pack(side="right", padx=(0, 4))

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=32, state="normal",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", wrap="word", relief="flat", borderwidth=1)
        self._log_text.pack(fill="both", expand=True)

        for tag, color in [("INFO", "#6a9955"), ("WARNING", "#ce9178"),
                           ("ERROR", "#f44747"), ("DEBUG", "#569cd6"),
                           ("CRITICAL", "#ff4444")]:
            self._log_text.tag_config(tag, foreground=color)

    # =========================================================================
    # 标签页 1：抢课选课
    # =========================================================================
    def _build_grab_tab(self):
        pad_x = 6
        main_pane = tk.PanedWindow(self._tab_grab, orient="horizontal",
                                   bg=self.COLORS["bg"], sashwidth=5, sashrelief="flat")
        main_pane.pack(fill="both", expand=True, padx=pad_x, pady=(6, 2))

        left_frame = tk.Frame(main_pane, bg=self.COLORS["bg"])
        main_pane.add(left_frame, width=750, minsize=500)
        right_frame = tk.Frame(main_pane, bg=self.COLORS["bg"])
        main_pane.add(right_frame, width=400, minsize=300)

        # ── 左列：课程列表 ──────────────────────────────────────────────
        self._grab_course_frame = ttk.LabelFrame(left_frame, text=" 可选课程列表 ",
                                                 padding=(8, 6))
        self._grab_course_frame.pack(fill="both", expand=True)

        sf = ttk.Frame(self._grab_course_frame)
        sf.pack(fill="x", pady=(0, 5))
        ttk.Label(sf, text="搜索").pack(side="left", padx=(0, 4))
        self._grab_search_var = tk.StringVar()
        self._grab_search_var.trace("w", lambda *_: self._filter_grab_courses())
        ttk.Entry(sf, textvariable=self._grab_search_var, width=24,
                  font=("微软雅黑", 9)).pack(side="left", padx=(0, 10))
        self._grab_refresh_btn = ttk.Button(sf, text="刷新课程",
                                            command=self._refresh_grab_courses,
                                            state="disabled")
        self._grab_refresh_btn.pack(side="left", padx=2)
        ttk.Separator(sf, orient="vertical").pack(side="left", padx=8, fill="y")
        self._grab_select_all_btn = ttk.Button(sf, text="全选", command=self._grab_select_all,
                                               state="disabled")
        self._grab_select_all_btn.pack(side="left", padx=2)
        self._grab_deselect_all_btn = ttk.Button(sf, text="取消全选",
                                                 command=self._grab_deselect_all,
                                                 state="disabled")
        self._grab_deselect_all_btn.pack(side="left", padx=2)
        self._grab_count_label = tk.Label(sf, text="共 0 门",
                                          font=("微软雅黑", 9), fg="gray",
                                          bg=self.COLORS["frame_bg"])
        self._grab_count_label.pack(side="right", padx=(4, 0))

        tree_container = tk.Frame(self._grab_course_frame, bg=self.COLORS["border"])
        tree_container.pack(fill="both", expand=True)

        columns = ("sel", "jxb", "name", "teacher", "credit", "cap", "remain", "status", "schedule")
        self._grab_tree = ttk.Treeview(tree_container, columns=columns,
                                       show="headings", height=14, selectmode="none")
        headers = {"sel": "选", "jxb": "教学班编号", "name": "课程名称", "teacher": "教师",
                   "credit": "学分", "cap": "已选/容量", "remain": "余量",
                   "status": "状态", "schedule": "上课时间"}
        widths = {"sel": 30, "jxb": 110, "name": 195, "teacher": 52,
                  "credit": 40, "cap": 70, "remain": 42, "status": 40, "schedule": 120}
        for col in columns:
            self._grab_tree.heading(col, text=headers[col])
            w = widths[col]
            anchor = "w" if col in ("name", "schedule") else "center"
            self._grab_tree.column(col, width=w, minwidth=w, anchor=anchor)

        self._grab_tree.tag_configure("selected", background="#cce5ff", foreground="#1a1a1a")
        self._grab_tree.tag_configure("full", foreground="#aaaaaa")
        self._grab_tree.tag_configure("selected_full", background="#d9d9d9", foreground="#666666")
        self._grab_tree.bind("<ButtonRelease-1>", self._on_grab_tree_click)

        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self._grab_tree.yview)
        self._grab_tree.configure(yscrollcommand=vsb.set)
        self._grab_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ── 右列：参数 + 控制 ────────────────────────────────────────────
        param_frame = ttk.LabelFrame(right_frame, text=" 抢课参数 ", padding=(10, 8))
        param_frame.pack(fill="x", pady=(0, 8))

        param_groups = [
            ("轮询设置", [("间隔最小值 (秒)", "poll_min", "0.3"),
                         ("间隔最大值 (秒)", "poll_max", "1.5")]),
            ("重试策略", [("最大重试次数", "max_retries", "3"),
                         ("重试延迟 (秒)", "retry_delay", "1.0")]),
            ("运行限制", [("最大运行时间 (秒)", "max_run", "3600"),
                         ("并发线程数", "threads", "3")]),
        ]
        self._param_vars = {}
        first = True
        for group_title, items in param_groups:
            if not first:
                ttk.Separator(param_frame, orient="horizontal").pack(fill="x", pady=(2, 4))
            first = False
            tk.Label(param_frame, text=group_title, font=("微软雅黑", 9, "bold"),
                     fg=self.COLORS["accent"], bg=self.COLORS["frame_bg"]).pack(
                anchor="w", pady=(2, 1))
            for label_text, key, default_val in items:
                f = ttk.Frame(param_frame)
                f.pack(fill="x", pady=2)
                ttk.Label(f, text=label_text, width=14, anchor="e").pack(
                    side="left", padx=(0, 6))
                v = tk.StringVar(value=default_val)
                self._param_vars[key] = v
                ttk.Entry(f, textvariable=v, width=8, font=("Consolas", 9)).pack(side="left")

        ctrl_frame = ttk.LabelFrame(right_frame, text=" 控制面板 ", padding=(10, 10))
        ctrl_frame.pack(fill="x", pady=(0, 8))

        self._grab_target_info = tk.Label(ctrl_frame, text="已选目标: 0 门课程",
                                          font=("微软雅黑", 10, "bold"),
                                          fg=self.COLORS["accent"],
                                          bg=self.COLORS["frame_bg"])
        self._grab_target_info.pack(anchor="w", pady=(0, 10))

        btn_f = tk.Frame(ctrl_frame, bg=self.COLORS["frame_bg"])
        btn_f.pack(fill="x")
        self._grab_start_btn = tk.Button(btn_f, text="开始抢课",
                                         font=("微软雅黑", 11, "bold"),
                                         fg="#0d6b0d", bg="#e8f5e9",
                                         activeforeground="#0d6b0d",
                                         activebackground="#c8e6c9",
                                         relief="groove", cursor="hand2",
                                         borderwidth=2, padx=18, pady=6,
                                         command=self._start_grab, state="disabled")
        self._grab_start_btn.pack(side="left", padx=(0, 8))
        self._grab_stop_btn = tk.Button(btn_f, text="停止",
                                        font=("微软雅黑", 11, "bold"),
                                        fg="#b71c1c", bg="#fce4ec",
                                        activeforeground="#b71c1c",
                                        activebackground="#f8bbd0",
                                        relief="groove", cursor="hand2",
                                        borderwidth=2, padx=18, pady=6,
                                        command=self._stop_grab, state="disabled")
        self._grab_stop_btn.pack(side="left")

        self._grab_run_status = tk.Label(ctrl_frame, text="状态: 等待开始",
                                         font=("微软雅黑", 9), fg="gray",
                                         bg=self.COLORS["frame_bg"])
        self._grab_run_status.pack(anchor="w", pady=(10, 0))

        stats_f = ttk.LabelFrame(ctrl_frame, text=" 实时统计 ", padding=(10, 8))
        stats_f.pack(fill="x", pady=(10, 0))
        self._grab_stats_vars = {}
        for idx, (label, key) in enumerate([("尝试次数", "attempts"), ("成功选课", "success"),
                                            ("课程已满", "full"), ("其他失败", "errors")]):
            row, col = divmod(idx, 2)
            v = tk.StringVar(value="0")
            self._grab_stats_vars[key] = v
            cell = tk.Frame(stats_f, bg=self.COLORS["frame_bg"])
            cell.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
            stats_f.grid_columnconfigure(col, weight=1)
            tk.Label(cell, text=label, font=("微软雅黑", 9),
                     bg=self.COLORS["frame_bg"], anchor="w").pack(side="left")
            tk.Label(cell, textvariable=v, width=5, anchor="e",
                     font=("Consolas", 12, "bold"), fg=self.COLORS["accent"],
                     bg=self.COLORS["frame_bg"]).pack(side="right")

    # =========================================================================
    # 标签页 2：成绩查询
    # =========================================================================
    def _build_grade_tab(self):
        # 顶部工具栏
        toolbar = ttk.Frame(self._tab_grade, padding=(10, 8))
        toolbar.pack(fill="x")
        self._grade_query_btn = ttk.Button(toolbar, text="查询全部成绩",
                                           style="Accent.TButton",
                                           command=self._query_grades,
                                           state="disabled")
        self._grade_query_btn.pack(side="left", padx=(0, 10))
        self._grade_export_btn = ttk.Button(toolbar, text="导出 CSV",
                                            command=self._export_grades_csv,
                                            state="disabled")
        self._grade_export_btn.pack(side="left", padx=(0, 10))
        self._grade_status_label = tk.Label(toolbar, text="请先登录后查询",
                                            font=("微软雅黑", 9), fg="gray",
                                            bg=self.COLORS["bg"])
        self._grade_status_label.pack(side="left", padx=(10, 0))

        # 主内容：成绩列表 + 统计
        content_pane = tk.PanedWindow(self._tab_grade, orient="horizontal",
                                      bg=self.COLORS["bg"], sashwidth=5, sashrelief="flat")
        content_pane.pack(fill="both", expand=True, padx=6, pady=(4, 6))

        # 左：成绩 Treeview
        left = tk.Frame(content_pane, bg=self.COLORS["bg"])
        content_pane.add(left, width=880, minsize=600)

        grade_list_frame = ttk.LabelFrame(left, text=" 成绩明细 ", padding=(8, 6))
        grade_list_frame.pack(fill="both", expand=True)

        grade_cols = ("semester", "name", "category", "credit", "pscj", "kscj",
                      "zpcj", "point", "teacher")
        self._grade_tree = ttk.Treeview(grade_list_frame, columns=grade_cols,
                                        show="headings", height=16)
        grade_headers = {"semester": "学期", "name": "课程名称", "category": "类别",
                         "credit": "学分", "pscj": "平时", "kscj": "考试",
                         "zpcj": "总评", "point": "绩点", "teacher": "教师"}
        grade_widths = {"semester": 150, "name": 260, "category": 70, "credit": 50,
                        "pscj": 50, "kscj": 50, "zpcj": 70, "point": 50, "teacher": 80}
        for col in grade_cols:
            self._grade_tree.heading(col, text=grade_headers[col])
            anchor = "w" if col in ("name", "semester", "teacher") else "center"
            self._grade_tree.column(col, width=grade_widths[col],
                                    minwidth=grade_widths[col], anchor=anchor)

        self._grade_tree.tag_configure("semester_header", background="#e8f0fe",
                                       font=("微软雅黑", 9, "bold"))
        self._grade_tree.tag_configure("fail", foreground="#d4380d")

        vsb = ttk.Scrollbar(grade_list_frame, orient="vertical",
                            command=self._grade_tree.yview)
        self._grade_tree.configure(yscrollcommand=vsb.set)
        self._grade_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 右：汇总统计
        right = tk.Frame(content_pane, bg=self.COLORS["bg"])
        content_pane.add(right, width=280, minsize=220)

        summary_frame = ttk.LabelFrame(right, text=" 成绩汇总 ", padding=(10, 10))
        summary_frame.pack(fill="x", pady=(0, 8))

        self._grade_summary_vars = {}
        for label, key in [("总课程数", "total"), ("总学分", "credits"),
                           ("及格", "passed"), ("不及格", "failed"),
                           ("平均绩点", "gpa")]:
            f = ttk.Frame(summary_frame)
            f.pack(fill="x", pady=4)
            tk.Label(f, text=label, font=("微软雅黑", 9), width=8, anchor="e",
                     bg=self.COLORS["frame_bg"]).pack(side="left", padx=(0, 8))
            v = tk.StringVar(value="—")
            self._grade_summary_vars[key] = v
            tk.Label(f, textvariable=v, font=("微软雅黑", 11, "bold"),
                     fg=self.COLORS["accent"],
                     bg=self.COLORS["frame_bg"]).pack(side="left")

        # 学期导航
        sem_frame = ttk.LabelFrame(right, text=" 按学期查看 ", padding=(10, 8))
        sem_frame.pack(fill="x", pady=(0, 8))
        self._grade_sem_listbox = tk.Listbox(sem_frame, height=10,
                                             font=("微软雅黑", 9),
                                             exportselection=False)
        self._grade_sem_listbox.pack(fill="both", expand=True)
        self._grade_sem_listbox.bind("<<ListboxSelect>>", self._on_grade_semester_select)

    # =========================================================================
    # 标签页 3：课表查询
    # =========================================================================
    def _build_schedule_tab(self):
        toolbar = ttk.Frame(self._tab_schedule, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="学期").pack(side="left", padx=(0, 4))
        self._sched_semester_var = tk.StringVar()
        self._sched_semester_combo = ttk.Combobox(toolbar,
                                                  textvariable=self._sched_semester_var,
                                                  state="readonly", width=28,
                                                  font=("微软雅黑", 9))
        self._sched_semester_combo.pack(side="left", padx=(0, 10))
        self._sched_query_btn = ttk.Button(toolbar, text="查询课表",
                                           style="Accent.TButton",
                                           command=self._query_schedule,
                                           state="disabled")
        self._sched_query_btn.pack(side="left", padx=(0, 10))
        self._sched_refresh_sem_btn = ttk.Button(toolbar, text="刷新学期",
                                                 command=self._refresh_semesters,
                                                 state="disabled")
        self._sched_refresh_sem_btn.pack(side="left", padx=(0, 10))
        self._sched_status_label = tk.Label(toolbar, text="请先登录后查询",
                                            font=("微软雅黑", 9), fg="gray",
                                            bg=self.COLORS["bg"])
        self._sched_status_label.pack(side="left", padx=(10, 0))

        # 课表网格显示（Canvas + 滚动）
        grid_container = ttk.Frame(self._tab_schedule, padding=(10, 4))
        grid_container.pack(fill="both", expand=True)

        self._sched_canvas = tk.Canvas(grid_container, bg="#ffffff",
                                       highlightthickness=0)
        self._sched_canvas.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(grid_container, orient="vertical",
                            command=self._sched_canvas.yview)
        vsb.pack(side="right", fill="y")
        self._sched_canvas.configure(yscrollcommand=vsb.set)

        self._sched_inner = tk.Frame(self._sched_canvas, bg="#ffffff")
        self._sched_canvas.create_window((0, 0), window=self._sched_inner,
                                         anchor="nw")
        self._sched_inner.bind("<Configure>",
                               lambda e: self._sched_canvas.configure(
                                   scrollregion=(0, 0,
                                                 self._sched_inner.winfo_width(),
                                                 self._sched_inner.winfo_height())))

    # =========================================================================
    # 登录逻辑
    # =========================================================================
    def _toggle_password(self):
        self._pass_entry.configure(show="" if self._show_pass.get() else "*")

    def _login(self):
        stu_id = self._stu_id_var.get().strip()
        password = self._pass_var.get().strip()
        if not stu_id or not password:
            messagebox.showwarning("信息不完整", "请输入学号和密码")
            return

        self._login_btn.configure(state="disabled", text="登录中...")
        self._status_label.configure(text="● 登录中...", foreground="orange")
        threading.Thread(target=self._do_login, daemon=True,
                         args=(stu_id, password)).start()

    def _do_login(self, stu_id, password):
        try:
            self._config = _GrabberConfig()
            self._config.STUDENT_ID = stu_id
            self._config.PASSWORD = password
            self._config.BASE_URL = self._base_url_var.get().strip() or self._config.BASE_URL
            self._config.YEAR_TERM = self._year_term_var.get().strip()
            self._config.__post_init__()

            # 注入成绩/课表查询 URL
            self._config.GRADE_QUERY_URL = self._GRADE_QUERY_URL
            self._config.SEMESTER_LIST_URL = self._SEMESTER_LIST_URL
            self._config.SCHEDULE_QUERY_URL = self._SCHEDULE_QUERY_URL

            self._auth = _JXAUAuthSession(self._config)
            gui_handler = GuiCaptchaHandler(self._auth.config, self.root)
            self._auth._fetch_and_recognize_captcha = (
                lambda: self._gui_captcha_fetch(gui_handler))

            success = self._auth.login()
            if success:
                self._logged_in = True
                self.root.after(0, self._on_login_success)
            else:
                self.root.after(0, self._on_login_fail)
        except Exception as e:
            self._logger.error(f"登录异常: {e}")
            self.root.after(0, lambda: self._on_login_error(str(e)))

    def _gui_captcha_fetch(self, gui_handler):
        captcha_url = f"{self._auth.config.CAPTCHA_URL}?t={int(time.time() * 1000)}"
        resp = self._auth.session.get(captcha_url,
                                      timeout=self._auth.config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            self._logger.error(f"获取验证码失败, HTTP {resp.status_code}")
            return ""
        return gui_handler.recognize(resp.content, img_name="login_captcha")

    def _on_login_success(self):
        self._login_btn.configure(state="disabled", text="已登录")
        self._logout_btn.configure(state="normal")
        self._status_label.configure(text=" ● 已登录", foreground="green")
        self._status_detail.configure(text=f"学号: {self._config.STUDENT_ID}")

        # 启用各功能按钮
        self._grab_refresh_btn.configure(state="normal")
        self._grab_select_all_btn.configure(state="normal")
        self._grab_deselect_all_btn.configure(state="normal")
        self._grade_query_btn.configure(state="normal")
        self._grade_status_label.configure(text="已登录，可查询成绩")
        self._sched_refresh_sem_btn.configure(state="normal")
        self._sched_status_label.configure(text="已登录，可查询课表")

        self._logger.info("登录成功！")
        # 自动加载课程和学期
        self._refresh_grab_courses()
        self._refresh_semesters()

    def _on_login_fail(self):
        self._login_btn.configure(state="normal", text="登  录")
        self._status_label.configure(text=" ● 登录失败", foreground="red")
        messagebox.showerror("登录失败", "登录失败，请检查学号/密码/验证码。\n详情请查看运行日志。")

    def _on_login_error(self, error_msg):
        self._login_btn.configure(state="normal", text="登  录")
        self._status_label.configure(text=" ● 错误", foreground="red")
        messagebox.showerror("登录错误", f"登录过程发生异常:\n{error_msg}")

    def _logout(self):
        if self._auth:
            try:
                self._auth.logout()
            except Exception:
                pass
        self._logged_in = False
        self._auth = None
        self._config = None
        self._all_courses = []
        self._selected.clear()
        self._clear_grab_tree()
        self._clear_grade_tree()
        self._clear_schedule_grid()

        self._login_btn.configure(state="normal", text="登  录")
        self._logout_btn.configure(state="disabled")
        self._status_label.configure(text=" ● 已退出", foreground="gray")
        self._status_detail.configure(text="")

        for btn in [self._grab_refresh_btn, self._grab_select_all_btn,
                    self._grab_deselect_all_btn, self._grab_start_btn,
                    self._grade_query_btn, self._grade_export_btn,
                    self._sched_query_btn, self._sched_refresh_sem_btn]:
            btn.configure(state="disabled")

        self._grab_count_label.configure(text="共 0 门")
        self._grab_target_info.configure(text="已选目标: 0 门课程")
        self._grade_status_label.configure(text="请先登录后查询")
        self._sched_status_label.configure(text="请先登录后查询")
        self._grade_summary_vars["total"].set("—")
        self._grade_summary_vars["credits"].set("—")
        self._grade_summary_vars["passed"].set("—")
        self._grade_summary_vars["failed"].set("—")
        self._grade_summary_vars["gpa"].set("—")
        self._grade_sem_listbox.delete(0, "end")
        self._sched_semester_combo["values"] = []
        self._logger.info("已退出登录")

    # =========================================================================
    # 抢课标签页逻辑
    # =========================================================================
    def _refresh_grab_courses(self):
        if not self._logged_in:
            return
        self._grab_refresh_btn.configure(state="disabled", text="查询中...")
        self._logger.info("正在查询可选课程列表...")
        threading.Thread(target=self._do_grab_refresh, daemon=True).start()

    def _do_grab_refresh(self):
        try:
            query = _CourseQuery(self._auth, self._config)
            courses = query.get_all_courses(limit=200)
            self._all_courses = courses
            self._selected.clear()
            self.root.after(0, self._update_grab_course_list)
            self.root.after(0, lambda: self._logger.info(
                f"查询完成，共 {len(courses)} 门课程"))
        except Exception as e:
            self._logger.error(f"查询课程失败: {e}")
        finally:
            self.root.after(0, lambda: self._grab_refresh_btn.configure(
                state="normal", text="刷新课程"))

    def _update_grab_course_list(self):
        self._clear_grab_tree()
        keyword = self._grab_search_var.get().strip().lower()
        display = [c for c in self._all_courses
                   if not keyword
                   or keyword in c.course_name.lower()
                   or keyword in c.jxb_bh.lower()
                   or keyword in c.teacher.lower()]
        for c in display:
            sel_mark = " ✓" if c.jxb_bh in self._selected else "  "
            is_sel = c.jxb_bh in self._selected
            is_full = c.is_full
            item_id = self._grab_tree.insert("", "end", iid=c.jxb_bh, values=(
                sel_mark, c.jxb_bh, c.course_name, c.teacher, c.credit,
                f"{c.enrolled}/{c.capacity}", c.remain,
                "可" if not is_full else "满", c.schedule))
            if is_sel and is_full:
                self._grab_tree.item(item_id, tags=("selected_full",))
            elif is_sel:
                self._grab_tree.item(item_id, tags=("selected",))
            elif is_full:
                self._grab_tree.item(item_id, tags=("full",))

        selected_count = len(self._selected)
        self._grab_count_label.configure(
            text=f"显示 {len(display)} 门 | 已选 {selected_count} 门")
        self._update_grab_target_info()

    def _clear_grab_tree(self):
        for item in self._grab_tree.get_children():
            self._grab_tree.delete(item)

    def _filter_grab_courses(self):
        self._update_grab_course_list()

    def _on_grab_tree_click(self, event):
        item = self._grab_tree.identify_row(event.y)
        if not item:
            return
        jxb_bh = item
        if jxb_bh in self._selected:
            self._selected.discard(jxb_bh)
        else:
            self._selected.add(jxb_bh)
        self._update_grab_course_list()

    def _grab_select_all(self):
        keyword = self._grab_search_var.get().strip().lower()
        for c in self._all_courses:
            if not keyword or keyword in c.course_name.lower() or keyword in c.jxb_bh.lower():
                self._selected.add(c.jxb_bh)
        self._update_grab_course_list()

    def _grab_deselect_all(self):
        self._selected.clear()
        self._update_grab_course_list()

    def _update_grab_target_info(self):
        count = len(self._selected)
        self._grab_target_info.configure(text=f"已选目标: {count} 门课程")
        self._grab_start_btn.configure(state="normal" if count and self._logged_in else "disabled")

    def _start_grab(self):
        if not self._selected:
            messagebox.showwarning("未选择课程", "请先选择目标课程")
            return
        if self._running:
            return
        if not _HAS_DDDDOCR:
            ret = messagebox.askyesno("缺少依赖",
                                      "未检测到 ddddocr 库，验证码需要手动输入。\n是否继续？")
            if not ret:
                return

        self._running = True
        self._stop_event.clear()
        for k in self._grab_stats_vars:
            self._grab_stats_vars[k].set("0")

        self._grab_start_btn.configure(state="disabled")
        self._grab_stop_btn.configure(state="normal")
        self._grab_refresh_btn.configure(state="disabled")
        self._login_btn.configure(state="disabled")
        self._logout_btn.configure(state="disabled")
        self._grab_run_status.configure(text="状态: 抢课运行中...", foreground="green")

        targets = [c for c in self._all_courses if c.jxb_bh in self._selected]
        self._logger.info("=" * 50)
        self._logger.info(f"抢课任务启动 | 目标: {len(targets)} 门")
        self._logger.info("=" * 50)

        threading.Thread(target=self._grab_loop, daemon=True,
                         args=(targets,)).start()

    def _grab_loop(self, targets):
        stats = {"attempts": 0, "success": 0, "full": 0, "errors": 0}
        start_time = time.time()

        try:
            max_run = float(self._param_vars["max_run"].get())
        except (ValueError, KeyError):
            max_run = 3600.0

        query = _CourseQuery(self._auth, self._config)
        selector = _CourseSelector(self._auth, self._config)
        completed_courses = set()

        while not self._stop_event.is_set():
            elapsed = time.time() - start_time
            if elapsed > max_run:
                self._logger.info(f"已达最大运行时间 ({max_run // 60:.0f}分钟)，自动停止")
                break

            fresh_courses = query.get_all_courses(limit=200)
            if not fresh_courses:
                self._logger.warning("会话可能已过期，尝试重新登录...")
                if not self._auth.login():
                    self._logger.error("重新登录失败，停止抢课")
                    break
                self._logger.info("重新登录成功")
                query = _CourseQuery(self._auth, self._config)
                selector = _CourseSelector(self._auth, self._config)
                continue

            time.sleep(random.uniform(1.5, 3.0))

            all_done = True
            for target in targets:
                if target.jxb_bh in completed_courses:
                    continue
                all_done = False

                current = next(
                    (c for c in fresh_courses if c.jxb_bh == target.jxb_bh), None)
                if current is None:
                    continue
                if current.is_full:
                    stats["full"] += 1
                    self._update_grab_stats_ui(stats)
                    self._logger.info(
                        f"[已满] {current.course_name} ({current.enrolled}/{current.capacity})")
                    continue

                stats["attempts"] += 1
                self._update_grab_stats_ui(stats)
                self._logger.info(
                    f"尝试选课: {current.course_name} (余量 {current.remain})")

                success = selector.select_course(current)
                if success:
                    stats["success"] += 1
                    completed_courses.add(target.jxb_bh)
                    self._logger.info(f"✓ 选课成功: {current.course_name}")
                else:
                    stats["errors"] += 1
                    self._logger.warning(f"✗ 选课失败: {current.course_name}")
                self._update_grab_stats_ui(stats)
                time.sleep(random.uniform(0.5, 1.0))

            if all_done:
                self._logger.info("所有目标课程已完成，任务结束")
                break

            poll_interval = random.uniform(
                float(self._param_vars["poll_min"].get()),
                float(self._param_vars["poll_max"].get()))
            time.sleep(poll_interval)

        elapsed = time.time() - start_time
        self._logger.info("=" * 50)
        self._logger.info(f"抢课任务结束 | 运行 {elapsed:.0f} 秒 | "
                          f"成功 {stats['success']} | "
                          f"已满 {stats['full']} | "
                          f"失败 {stats['errors']}")
        self._logger.info("=" * 50)
        self.root.after(0, self._on_grab_finish)

    def _update_grab_stats_ui(self, stats):
        def _update():
            for k, v in stats.items():
                if k in self._grab_stats_vars:
                    self._grab_stats_vars[k].set(str(v))
        self.root.after(0, _update)

    def _on_grab_finish(self):
        self._running = False
        self._grab_start_btn.configure(
            state="normal" if self._selected and self._logged_in else "disabled")
        self._grab_stop_btn.configure(state="disabled")
        self._grab_refresh_btn.configure(state="normal")
        self._login_btn.configure(state="disabled")
        self._logout_btn.configure(state="normal")
        self._grab_run_status.configure(text="状态: 已停止", foreground="gray")

    def _stop_grab(self):
        self._stop_event.set()
        self._running = False
        self._grab_run_status.configure(text="状态: 正在停止...", foreground="orange")
        self._grab_stop_btn.configure(state="disabled")
        self._logger.info("用户请求停止抢课...")

    # =========================================================================
    # 成绩查询标签页逻辑
    # =========================================================================
    def _query_grades(self):
        if not self._logged_in or not self._auth:
            messagebox.showwarning("未登录", "请先登录")
            return

        self._grade_query_btn.configure(state="disabled", text="查询中...")
        self._grade_status_label.configure(text="正在查询成绩...")
        self._logger.info("正在查询成绩...")
        threading.Thread(target=self._do_query_grades, daemon=True).start()

    def _do_query_grades(self):
        try:
            svc = _GradeQueryService(self._auth)
            records = svc.query_all_grades()
            self.root.after(0, lambda: self._display_grades(records))
        except Exception as e:
            self._logger.error(f"成绩查询异常: {e}")
            self.root.after(0, lambda: self._grade_status_label.configure(
                text=f"查询失败: {e}"))
        finally:
            self.root.after(0, lambda: self._grade_query_btn.configure(
                state="normal", text="查询全部成绩"))

    def _display_grades(self, records):
        self._clear_grade_tree()
        self._grade_sem_listbox.delete(0, "end")
        self._grade_records = records
        self._grade_groups = _grade_group_by_semester(records)

        if not records:
            self._grade_status_label.configure(text="没有查到成绩记录")
            self._grade_export_btn.configure(state="disabled")
            for k in self._grade_summary_vars:
                self._grade_summary_vars[k].set("—")
            return

        self._grade_status_label.configure(text=f"共 {len(records)} 条成绩记录")
        self._grade_export_btn.configure(state="normal")

        # 统计
        total_courses = len(records)
        total_credits = 0.0
        pass_count = 0
        fail_count = 0
        valid_point_sum = 0.0
        valid_point_count = 0

        for rec in records:
            try:
                credit_val = float(rec.xf) if rec.xf and rec.xf not in ("无", "-1") else 0.0
            except (ValueError, TypeError):
                credit_val = 0.0
            total_credits += credit_val
            if rec.jgbj == 1:
                pass_count += 1
            else:
                fail_count += 1
            try:
                pv = float(rec.point)
            except (ValueError, TypeError):
                pv = 0.0
            if pv > 0:
                valid_point_sum += pv
                valid_point_count += 1

        gpa = valid_point_sum / valid_point_count if valid_point_count > 0 else 0.0
        self._grade_summary_vars["total"].set(str(total_courses))
        self._grade_summary_vars["credits"].set(f"{total_credits:.1f}")
        self._grade_summary_vars["passed"].set(str(pass_count))
        self._grade_summary_vars["failed"].set(str(fail_count))
        self._grade_summary_vars["gpa"].set(f"{gpa:.2f}")

        # 学期列表
        sem_keys = sorted(self._grade_groups.keys(), reverse=True)
        for xq in sem_keys:
            label = parse_semester_label(xq)
            count = len(self._grade_groups[xq])
            self._grade_sem_listbox.insert("end", f"{label}  ({count}门)")

        # 默认全显示
        self._populate_grade_tree(show_all=True)
        self._logger.info(f"成绩查询完成: {total_courses}条, GPA {gpa:.2f}")

    def _populate_grade_tree(self, show_all=True, semester_filter=None):
        self._clear_grade_tree()

        def _fmt(val):
            if not val or val in ("无", "-1", "-1.0"):
                return "—"
            return val

        sem_keys = sorted(self._grade_groups.keys()) if not show_all else sorted(
            self._grade_groups.keys(), reverse=True)

        if semester_filter and semester_filter in sem_keys:
            sem_keys = [semester_filter]

        for xq in sem_keys:
            if xq not in self._grade_groups:
                continue
            recs = self._grade_groups[xq]
            label = parse_semester_label(xq)
            # 学期分组标题行
            header_id = self._grade_tree.insert("", "end", values=(
                f"▸ {label}  ({len(recs)}门)", "", "", "", "", "", "", "", ""),
                tags=("semester_header",))

            for rec in recs:
                zpcj_disp = rec.zpcj if rec.zpcj and rec.zpcj not in (
                    "无", "-1", "-1.0") else "—"
                point_disp = _fmt(rec.point)
                if point_disp == "—":
                    point_disp_val = "—"
                else:
                    try:
                        pv = float(rec.point)
                        point_disp_val = f"{pv:.1f}" if pv > 0 else "—"
                    except (ValueError, TypeError):
                        point_disp_val = "—"

                tags = ()
                if rec.jgbj != 1:
                    tags = ("fail",)

                self._grade_tree.insert("", "end", values=(
                    label, rec.kcmc, rec.kclb, _fmt(rec.xf),
                    _fmt(rec.pscj), _fmt(rec.kscj), zpcj_disp,
                    point_disp_val, rec.cbls),
                    tags=tags)

    def _on_grade_semester_select(self, event):
        sel = self._grade_sem_listbox.curselection()
        if not sel:
            self._populate_grade_tree(show_all=True)
            return
        idx = sel[0]
        sem_keys = sorted(self._grade_groups.keys(), reverse=True)
        if idx < len(sem_keys):
            self._populate_grade_tree(
                show_all=False, semester_filter=sem_keys[idx])

    def _clear_grade_tree(self):
        for item in self._grade_tree.get_children():
            self._grade_tree.delete(item)

    def _export_grades_csv(self):
        if not hasattr(self, '_grade_records') or not self._grade_records:
            messagebox.showwarning("无数据", "没有可导出的成绩数据")
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        filename = f"grades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = os.path.join(SCRIPT_DIR, filename)

        try:
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["学期", "课程名称", "类别", "学分",
                                 "平时", "考试", "总评", "绩点", "教师"])
                for rec in self._grade_records:
                    writer.writerow([
                        parse_semester_label(rec.xq),
                        rec.kcmc, rec.kclb, rec.xf,
                        rec.pscj, rec.kscj, rec.zpcj,
                        rec.point, rec.cbls
                    ])
            self._logger.info(f"成绩已导出至: {filepath}")
            messagebox.showinfo("导出成功",
                                f"成绩数据已导出到:\n{filepath}")
        except Exception as e:
            self._logger.error(f"导出失败: {e}")
            messagebox.showerror("导出失败", str(e))

    # =========================================================================
    # 课表查询标签页逻辑
    # =========================================================================
    def _refresh_semesters(self):
        if not self._logged_in or not self._auth:
            return
        self._sched_refresh_sem_btn.configure(state="disabled", text="查询中...")
        self._logger.info("正在获取学期列表...")
        threading.Thread(target=self._do_refresh_semesters, daemon=True).start()

    def _do_refresh_semesters(self):
        try:
            mgr = _SemesterManager(self._auth)
            ok = mgr.fetch_semesters()
            if ok:
                self.root.after(0, lambda: self._update_semester_list(mgr.semesters))
            else:
                self.root.after(0, lambda: self._sched_status_label.configure(
                    text="学期列表获取失败"))
        except Exception as e:
            self._logger.error(f"获取学期列表异常: {e}")
            self.root.after(0, lambda: self._sched_status_label.configure(
                text=f"获取失败: {e}"))
        finally:
            self.root.after(0, lambda: self._sched_refresh_sem_btn.configure(
                state="normal", text="刷新学期"))

    def _update_semester_list(self, semesters):
        # 按学期 key 降序排列（最新学期在前）
        self._sched_semesters = sorted(semesters, key=lambda s: s["key"], reverse=True)
        labels = []
        for s in self._sched_semesters:
            label = parse_semester_label(s["key"])
            labels.append(f"{label}  ({s['key']})")

        self._sched_semester_combo["values"] = labels
        if labels:
            self._sched_semester_combo.current(0)
            self._sched_query_btn.configure(state="normal")
            self._sched_status_label.configure(
                text=f"共 {len(semesters)} 个学期可选")

    def _query_schedule(self):
        if not self._logged_in or not self._auth:
            return

        idx = self._sched_semester_combo.current()
        if idx < 0:
            messagebox.showwarning("未选择", "请先选择学期")
            return

        sem = self._sched_semesters[idx]
        self._sched_query_btn.configure(state="disabled", text="查询中...")
        self._sched_status_label.configure(text="正在查询课表...")
        self._logger.info(f"正在查询课表（{sem['key']}）...")
        threading.Thread(target=self._do_query_schedule,
                         daemon=True, args=(sem,)).start()

    def _do_query_schedule(self, sem):
        try:
            svc = _ScheduleQueryService(self._auth)
            records = svc.query_schedule(sem["key"])
            label = parse_semester_label(sem["key"])
            self.root.after(0, lambda: self._display_schedule(records, label))
        except Exception as e:
            self._logger.error(f"课表查询异常: {e}")
            self.root.after(0, lambda: self._sched_status_label.configure(
                text=f"查询失败: {e}"))
        finally:
            self.root.after(0, lambda: self._sched_query_btn.configure(
                state="normal", text="查询课表"))

    def _display_schedule(self, records, semester_label):
        self._clear_schedule_grid()
        if not records:
            self._sched_status_label.configure(text="没有查询到课表记录")
            return

        self._sched_status_label.configure(
            text=f"{semester_label} — 共 {len(records)} 条")

        grid = _build_schedule_grid(records)

        # 渲染表格到 Canvas 内 Frame
        inner = self._sched_inner
        for widget in inner.winfo_children():
            widget.destroy()

        # Canvas 背景
        bg_color = "#ffffff"
        inner.configure(bg=bg_color)

        # 常量
        col_w = 152
        row_h = 100
        time_col_w = 70
        header_h = 36
        pad = 4
        font_header = ("微软雅黑", 9, "bold")
        font_day = ("微软雅黑", 9, "bold")
        font_course = ("微软雅黑", 10, "bold")
        font_small = ("微软雅黑", 9)

        # 表头：时段 | 星期一 ~ 星期日
        tk.Label(inner, text=f"  {semester_label}",
                 font=("微软雅黑", 11, "bold"), bg=bg_color).grid(
            row=0, column=0, columnspan=8, sticky="w", padx=10, pady=(6, 4))

        # 列标题行
        tk.Label(inner, text="时段", font=font_header, bg="#f0f0f0",
                 relief="groove", borderwidth=1, width=8, height=2).grid(
            row=1, column=0, sticky="nsew", padx=1, pady=1)
        for day_num in range(1, 8):
            day_label = WEEKDAY_NAMES.get(day_num, "")
            tk.Label(inner, text=day_label, font=font_day, bg="#e8f0fe",
                     relief="groove", borderwidth=1, width=18, height=2).grid(
                row=1, column=day_num, sticky="nsew", padx=1, pady=1)

        # 时段行
        last_section = None
        row = 2
        for slot in SLOT_ORDER:
            section = SLOT_SECTION.get(slot, "")
            if section != last_section and section in ("下午", "晚上"):
                # 分隔行
                sep_label = f"── {section} ──"
                tk.Label(inner, text=sep_label, font=("微软雅黑", 8, "bold"),
                         fg="#888888", bg=bg_color).grid(
                    row=row, column=0, columnspan=8, sticky="w", padx=10, pady=(4, 1))
                row += 1
            last_section = section

            slot_name = f"{slot}节"
            tk.Label(inner, text=slot_name, font=font_header, bg="#fafafa",
                     relief="groove", borderwidth=1).grid(
                row=row, column=0, sticky="nsew", padx=1, pady=1,
                ipady=row_h // 6)

            for day_num in range(1, 8):
                cell_records = grid.get(day_num, {}).get(slot, [])
                cell_frame = tk.Frame(inner, bg="#ffffff",
                                      relief="groove", borderwidth=1,
                                      width=col_w, height=row_h)
                cell_frame.grid(row=row, column=day_num, sticky="nsew",
                                padx=1, pady=1)
                cell_frame.grid_propagate(False)

                if cell_records:
                    for rec in cell_records[:2]:  # 最多显示2门课
                        course_text = rec.kcmc or ""
                        if len(course_text) > 14:
                            course_text = course_text[:13] + "…"
                        tk.Label(cell_frame, text=course_text, font=font_course,
                                 fg="#0d3b66", bg="#ffffff", anchor="w").pack(
                            anchor="w", padx=2, fill="x")

                        detail_parts = []
                        if rec.skzhou:
                            weeks = _simplify_weeks(rec.skzhou)
                            if len(weeks) > 18:
                                weeks = weeks[:17] + "…"
                            detail_parts.append(weeks)
                        if rec.teacher_name:
                            detail_parts.append(rec.teacher_name)
                        if rec.skdd:
                            addr = rec.skdd
                            if len(addr) > 12:
                                addr = addr[:11] + "…"
                            detail_parts.append(addr)

                        for part in detail_parts:
                            tk.Label(cell_frame, text=part, font=font_small,
                                     fg="#333333", bg="#ffffff", anchor="w").pack(
                                anchor="w", padx=2)
                else:
                    tk.Label(cell_frame, text="", bg="#ffffff").pack()

            row += 1

        # 用 inner frame 实际尺寸设置 scrollregion（bbox("all") 不包含 window 类型）
        self._sched_inner.update_idletasks()
        self._sched_canvas.configure(
            scrollregion=(0, 0, self._sched_inner.winfo_width(),
                          self._sched_inner.winfo_height()))
        self._logger.info(f"课表渲染完成: {semester_label}")

    def _clear_schedule_grid(self):
        for widget in self._sched_inner.winfo_children():
            widget.destroy()

    # =========================================================================
    # 日志
    # =========================================================================
    def _poll_log(self):
        try:
            while True:
                msg = self._text_handler.queue.get_nowait()
                self._log_text.insert("end", msg + "\n")
                for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
                    if f"] {level}" in msg:
                        self._log_text.tag_add(level,
                                               f"end-{len(msg) + 1}c",
                                               f"end-1c")
                        break
                if self._auto_scroll_var.get():
                    self._log_text.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _clear_log(self):
        self._log_text.delete("1.0", "end")

    def _on_close(self):
        if self._running:
            if not messagebox.askokcancel("确认退出",
                                          "抢课任务正在运行，确定要退出吗？"):
                return
            self._stop_event.set()
        if self._auth:
            try:
                self._auth.logout()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# =============================================================================
# 入口
# =============================================================================
if __name__ == "__main__":
    app = JiaowuGUI()
    app.run()
