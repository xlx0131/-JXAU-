#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江西农业大学教务系统 —— 成绩查询工具
========================================

功能:
    1. 登录教务系统（验证码自动识别 / 手动输入）
    2. 查询全部学期成绩
    3. 按学期分组展示（课程名称、类别、学分、平时、考试、总评、绩点、教师）
    4. 汇总统计：总学分、平均绩点
    5. 支持导出 CSV 文件

作者: 许立鑫
    侵权联系: xlx20050131

技术要点:
    - 基于 requests 库模拟 ASP.NET MVC 教务系统 HTTP 请求
    - JXAUAuthSession 会话管理（登录、Cookie 维持、GUID 提取）
    - ddddocr 离线验证码识别

兼容性:
    - Python 3.8+
    - 依赖: requests, ddddocr
    - 安装: pip install requests ddddocr

—— 仅用于学习研究, 请勿用于非法用途 ——
"""

import os
import sys
import time
import json
import csv
import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

import requests

# ddddocr: 开源离线 OCR 库，专门针对字符验证码场景优化
try:
    import ddddocr
    _HAS_DDDDOCR = True
except ImportError:
    _HAS_DDDDOCR = False

# =============================================================================
# 第一部分：日志配置
# =============================================================================

def setup_logger(name: str = "GradeQuery") -> logging.Logger:
    """配置结构化日志，输出到控制台"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)

    # 文件日志（可选）
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir,
        f"grade_query_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


logger = setup_logger()

# =============================================================================
# 第二部分：配置管理
# =============================================================================

@dataclass
class GradeQueryConfig:
    """成绩查询工具核心配置"""
    BASE_URL: str = "https://jwgl.jxau.edu.cn"
    STUDENT_ID: str = field(default_factory=lambda: os.getenv("JXAU_STU_ID", ""))
    PASSWORD: str = field(default_factory=lambda: os.getenv("JXAU_PASS", ""))
    PASSWORD_MODE: str = "plain"         # plain / hidden / auto
    REQUEST_TIMEOUT: int = 15
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    PROXIES: Optional[Dict[str, str]] = None
    CAPTCHA_ENGINE: str = "ocr"
    OCR_CONFIDENCE_THRESHOLD: float = 0.3
    OCR_MAX_RETRIES: int = 3
    OCR_USE_CHARSET: bool = True
    CAPTCHA_WAIT_TIMEOUT: int = 120

    ROUTES: Dict[str, str] = field(default_factory=lambda: {
        "login": "/",
        "captcha": "/User/Validation/",
        "login_post": "/User/CheckLogin",
        "grade_query": "/SystemManage/CJManage/GetXsCjByXh/",
    })

    # ── 成绩导出路径（默认脚本同目录） ────────────────────────────────
    EXPORT_DIR: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))

    def __post_init__(self):
        self.LOGIN_URL = self.BASE_URL + self.ROUTES["login"]
        self.CAPTCHA_URL = self.BASE_URL + self.ROUTES["captcha"]
        self.LOGIN_POST_URL = self.BASE_URL + self.ROUTES["login_post"]
        self.GRADE_QUERY_URL = self.BASE_URL + self.ROUTES["grade_query"]

    def validate(self) -> List[str]:
        errors = []
        if not self.STUDENT_ID:
            errors.append("学号未设置 (STUDENT_ID)")
        if not self.PASSWORD:
            errors.append("密码未设置 (PASSWORD)")
        return errors


# =============================================================================
# 第三部分：验证码处理
# =============================================================================

class CaptchaHandler:
    """验证码处理器 — 复用抢课脚本逻辑"""

    def __init__(self, config: GradeQueryConfig):
        self.config = config
        self._captcha_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "captcha_cache"
        )
        os.makedirs(self._captcha_dir, exist_ok=True)
        self._ocr_instance = None

    def recognize(self, image_data: bytes, img_name: str = "captcha") -> str:
        engine = self.config.CAPTCHA_ENGINE
        if engine == "ocr":
            return self._ocr_recognition(image_data, img_name)
        elif engine == "manual":
            return self._manual_recognition(image_data, img_name)
        else:
            logger.warning(f"未知识别引擎 '{engine}'，回退 ddddocr")
            return self._ocr_recognition(image_data, img_name)

    def _get_ocr(self) -> "ddddocr.DdddOcr":
        if self._ocr_instance is None:
            if not _HAS_DDDDOCR:
                raise RuntimeError(
                    "ddddocr 未安装，请执行: pip install ddddocr\n"
                    "安装后即可自动识别验证码，无需人工干预。"
                )
            logger.info("初始化 ddddocr 模型（首次加载约 2~5 秒）...")
            self._ocr_instance = ddddocr.DdddOcr(
                show_ad=False, ocr=True, det=False,
            )
            logger.info("ddddocr 模型加载完成")
        return self._ocr_instance

    def _ocr_recognition(self, image_data: bytes, img_name: str) -> str:
        max_retries = self.config.OCR_MAX_RETRIES
        threshold = self.config.OCR_CONFIDENCE_THRESHOLD
        for attempt in range(1, max_retries + 1):
            try:
                ocr = self._get_ocr()
                result = ocr.classification(image_data)
                if not result or not result.strip():
                    logger.warning(f"OCR 识别为空 (第{attempt}/{max_retries}次)")
                    continue
                result = result.strip()
                if self.config.OCR_USE_CHARSET:
                    filtered = re.sub(r"[^a-zA-Z0-9]", "", result)
                    if filtered != result:
                        logger.debug(f"OCR 原始结果 '{result}' 经字符集过滤为 '{filtered}'")
                        result = filtered
                min_len = max(1, int(4 * threshold))
                if len(result) < min_len:
                    logger.warning(f"OCR 结果 '{result}' 长度 {len(result)} 过短")
                    continue
                logger.info(f"ddddocr 识别成功: '{result}'")
                return result
            except RuntimeError as e:
                if "未安装" in str(e):
                    logger.warning(f"ddddocr 不可用: {e}")
                    break
                logger.warning(f"OCR 识别异常 (第{attempt}次): {e}")
            except Exception as e:
                logger.warning(f"OCR 识别异常 (第{attempt}次): {e}")
            time.sleep(0.3)
        logger.warning("ddddocr 自动识别失败，降级为手动输入验证码")
        return self._manual_recognition(image_data, img_name)

    def _manual_recognition(self, image_data: bytes, img_name: str) -> str:
        save_path = os.path.join(self._captcha_dir, f"{img_name}.png")
        with open(save_path, "wb") as f:
            f.write(image_data)
        logger.info(f"验证码已保存至: {save_path}")
        print(f"\n{'=' * 50}")
        print(f"  [手动输入] 请打开图片查看验证码: {save_path}")
        print(f"  输入验证码（{self.config.CAPTCHA_WAIT_TIMEOUT}秒超时）:")
        print(f"{'=' * 50}")
        try:
            code = input("  > ").strip()
            return code
        except (KeyboardInterrupt, EOFError):
            logger.warning("用户取消验证码输入")
            return ""


# =============================================================================
# 第四部分：教务系统会话管理（复用 JXAUAuthSession）
# =============================================================================

class JXAUAuthSession:
    """教务系统会话管理器 — 负责登录、Cookie 维持"""

    def __init__(self, config: GradeQueryConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        if config.PROXIES:
            self.session.proxies.update(config.PROXIES)
        self.logged_in = False
        self.session_guid = ""

    def login(self) -> bool:
        """执行登录流程"""
        logger.info("开始登录流程 ...")

        # Step 1: GET 登录页，获取 Session Cookie
        try:
            resp = self.session.get(
                self.config.LOGIN_URL,
                timeout=self.config.REQUEST_TIMEOUT,
            )
            resp.encoding = "utf-8"
            logger.debug(f"登录页状态码: {resp.status_code}")
        except requests.RequestException as e:
            logger.error(f"访问登录页失败: {e}")
            return False

        # Step 2: 获取并识别验证码
        captcha_code = self._fetch_and_recognize_captcha()
        if not captcha_code:
            logger.error("验证码获取失败，登录终止")
            return False

        # Step 3: POST 登录
        login_data = {
            "UserName": self.config.STUDENT_ID,
            "PassWord": self.config.PASSWORD,
            "validation": captcha_code,
        }
        logger.debug(f"登录参数: UserName={login_data['UserName']}, validation={login_data['validation']}")

        try:
            resp = self.session.post(
                self.config.LOGIN_POST_URL,
                data=login_data,
                timeout=self.config.REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            resp.encoding = "utf-8"
        except requests.RequestException as e:
            logger.error(f"登录请求失败: {e}")
            return False

        # Step 4: 验证登录
        if self._check_login_success(resp):
            self.logged_in = True
            guid_match = re.search(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                resp.url, re.I
            )
            if guid_match:
                self.session_guid = guid_match.group()
                logger.info(f"会话 GUID: {self.session_guid}")
            else:
                # 尝试从 Cookie / 隐藏字段中提取
                logger.warning("未从 URL 提取到 GUID，尝试从页面内容提取...")
                guid_match = re.search(
                    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                    resp.text, re.I
                )
                if guid_match:
                    self.session_guid = guid_match.group()
                    logger.info(f"会话 GUID (页面): {self.session_guid}")
                else:
                    logger.error("无法获取会话 GUID，成绩查询接口可能不可用")
                    return False
            logger.info("=== 登录成功 ===")
            return True
        else:
            logger.warning("登录失败，请检查学号/密码/验证码")
            return False

    def _fetch_and_recognize_captcha(self) -> str:
        """获取验证码图片并识别"""
        captcha_handler = CaptchaHandler(self.config)
        captcha_url = f"{self.config.CAPTCHA_URL}?t={int(time.time() * 1000)}"
        try:
            resp = self.session.get(
                captcha_url,
                timeout=self.config.REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.error(f"获取验证码失败, HTTP {resp.status_code}")
                return ""
            return captcha_handler.recognize(resp.content, img_name="login_captcha")
        except requests.RequestException as e:
            logger.error(f"获取验证码图片异常: {e}")
            return ""

    def _check_login_success(self, resp: requests.Response) -> bool:
        """校验登录是否成功"""
        if "login" not in resp.url.lower():
            return True
        for keyword in ["退出", "注销", "欢迎", "logout", "main"]:
            if keyword in resp.text:
                return True
        try:
            j = resp.json()
            if j.get("success") or j.get("flag") == "1" or j.get("state") == "1":
                return True
            if j.get("msg") and ("失败" in j["msg"] or "错误" in j["msg"]):
                return False
        except (json.JSONDecodeError, AttributeError):
            pass
        if "验证码" in resp.text and ("UserName" in resp.text or "login" in resp.url.lower()):
            return False
        return False

    def logout(self):
        """退出登录"""
        if not self.logged_in:
            return
        try:
            self.session.get(self.config.LOGIN_URL, timeout=5)
        except Exception:
            pass
        finally:
            self.logged_in = False
            self.session.close()
            logger.info("已退出登录，会话已清理")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()


# =============================================================================
# 第五部分：成绩数据结构
# =============================================================================

@dataclass
class GradeRecord:
    """单条成绩记录"""
    kcmc: str               # 课程名称
    kclb: str               # 课程类别
    xf: str                  # 学分（字符串，可能含小数）
    pscj: str                # 平时成绩
    kscj: str                # 考试成绩
    zpcj: str                # 总评成绩
    bkcj: str                # 补考成绩
    cxcj: str                # 重修成绩
    xq: str                  # 学期代码
    jgbj: int                # 及格标志 (1=及格, 0=不及格)
    point: str               # 绩点（字符串，可能含 -1.0）
    cbls: str                # 任课教师
    kssj: str                # 考试时间

    @staticmethod
    def from_json(data: dict) -> "GradeRecord":
        """从 JSON 对象解析成绩记录"""
        return GradeRecord(
            kcmc=str(data.get("Kcmc", "") or ""),
            kclb=str(data.get("Kclb", "") or ""),
            xf=str(data.get("Zxf", data.get("xf", "")) or ""),
            pscj=str(data.get("Pscj", "") or ""),
            kscj=str(data.get("Kscj", "") or ""),
            zpcj=str(data.get("Zpcj", "") or ""),
            bkcj=str(data.get("Bkcj", "") or ""),
            cxcj=str(data.get("Cxcj", "") or ""),
            xq=str(data.get("Xq", "") or ""),
            jgbj=int(data.get("Jgbj", 0) or 0),
            point=str(data.get("Point", "") or ""),
            cbls=str(data.get("Cbls", "") or ""),
            kssj=str(data.get("Kssj", "") or ""),
        )


# =============================================================================
# 第六部分：成绩查询模块
# =============================================================================

class GradeQueryService:
    """成绩查询服务"""

    def __init__(self, auth: JXAUAuthSession):
        self.auth = auth

    def query_all_grades(self) -> List[GradeRecord]:
        """
        查询全部学期成绩
        POST {grade_query_url}/{session_guid}
        返回成绩列表（按学期排序）
        """
        if not self.auth.session_guid:
            logger.error("会话 GUID 为空，无法查询成绩")
            return []

        url = f"{self.auth.config.GRADE_QUERY_URL}{self.auth.session_guid}"
        logger.info(f"正在查询成绩...")

        try:
            resp = self.auth.session.post(
                url,
                timeout=self.auth.config.REQUEST_TIMEOUT,
            )
            resp.encoding = "utf-8"
            text = resp.text.strip()

            if not text.startswith("{"):
                logger.error("成绩接口返回非 JSON 格式，可能会话已过期")
                return []

            return self._parse_grade_data(text)

        except requests.RequestException as e:
            logger.error(f"成绩查询请求失败: {e}")
            return []

    def _parse_grade_data(self, text: str) -> List[GradeRecord]:
        """解析成绩 JSON 数据"""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            return []

        if not data.get("Result", False):
            msg = data.get("Message", "未知错误")
            logger.error(f"成绩查询失败: {msg}")
            return []

        rows = data.get("Data", [])
        if not isinstance(rows, list):
            logger.warning("成绩数据格式异常：Data 字段非列表")
            return []

        records = []
        for row in rows:
            try:
                record = GradeRecord.from_json(row)
                records.append(record)
            except Exception as e:
                logger.debug(f"解析成绩行失败: {e} | 原始: {row}")
                continue

        logger.info(f"共获取 {len(records)} 条成绩记录")
        return records


# =============================================================================
# 第七部分：成绩展示与统计（优化排版：固定列宽、对齐、截断等）
# =============================================================================

# ── 列宽定义（按显示宽度：中文=2，ASCII=1） ─────────────────────────────
_COL_DEFS = [
    ("课程名称", 40, "left"),    # max 20 Chinese chars
    ("类别",     12, "left"),    # max 6  Chinese chars
    ("学分",      6, "right"),
    ("平时",      6, "right"),
    ("考试",      6, "right"),
    ("总评",      8, "right"),
    ("绩点",      6, "right"),
    ("教师",     12, "left"),    # max 6  Chinese chars
]
_TABLE_WIDTH = sum(w for _, w, _ in _COL_DEFS) + len(_COL_DEFS) - 1  # 列间空格


def _char_width(ch: str) -> int:
    """返回单个字符的显示宽度"""
    code = ord(ch)
    if 0x4e00 <= code <= 0x9fff or 0x3000 <= code <= 0x303f or 0xff00 <= code <= 0xffef:
        return 2
    return 1


def display_width(s: str) -> int:
    """计算字符串的显示宽度（中文=2，ASCII=1）"""
    return sum(_char_width(ch) for ch in s)


def _truncate_to_width(s: str, max_width: int, align: str = "left") -> str:
    """按显示宽度截断，超长末尾加 …，返回后总显示宽度恰好为 max_width"""
    w = 0
    cut_idx = len(s)
    for i, ch in enumerate(s):
        cw = _char_width(ch)
        if w + cw > max_width - 1:  # 留1个字符宽度给 …
            cut_idx = i
            break
        w += cw
    if cut_idx == len(s):
        return s
    result = s[:cut_idx] + "…"
    # 补空格到精确宽度
    pad = max_width - display_width(result)
    if pad > 0:
        result += " " * pad
    return result


def pad_str(s: str, width: int, align: str = "left") -> str:
    """按显示宽度填充/截断字符串，align=left/right/center"""
    s = str(s) if s is not None else ""
    dw = display_width(s)
    if dw > width:
        return _truncate_to_width(s, width, align)
    padding = width - dw
    if align == "left":
        return s + " " * padding
    elif align == "right":
        return " " * padding + s
    else:  # center
        left_pad = padding // 2
        right_pad = padding - left_pad
        return " " * left_pad + s + " " * right_pad


def _build_row(cols: list, row_type: str = "data") -> str:
    """
    row_type:
      "header" — 表头，居中
      "data"   — 数据行
      "sep"    — 分隔线
    """
    parts = []
    for i, col_def in enumerate(_COL_DEFS):
        _, w, align = col_def
        if row_type == "header":
            parts.append(pad_str(col_def[0], w, "center"))
        elif row_type == "sep":
            parts.append("─" * w)
        else:
            parts.append(pad_str(str(cols[i]), w, align))
    return " " + " ".join(parts)


def parse_semester_label(xq: str) -> str:
    """将学期代码转换为可读标签，如 '20231' → '2023-2024 第一学期'"""
    if not xq or len(xq) < 5:
        return xq
    try:
        year = int(xq[:4])
        term_code = xq[4]
        term_map = {"1": "第一学期", "2": "第二学期"}
        term_label = term_map.get(term_code, f"第{term_code}学期")
        return f"{year}-{year + 1} {term_label}"
    except (ValueError, IndexError):
        return xq


def safe_float(val: str) -> float:
    """安全转换为浮点数，失败返回 0.0"""
    if not val or val in ("无", "-1.0", "-1"):
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def group_by_semester(records: List[GradeRecord]) -> Dict[str, List[GradeRecord]]:
    """按学期分组"""
    groups: Dict[str, List[GradeRecord]] = {}
    for rec in records:
        groups.setdefault(rec.xq, []).append(rec)
    return dict(sorted(groups.items(), key=lambda x: x[0]))


def _format_score(val: str) -> str:
    """格式化分数：空值/无/-1 显示 —，其他原样"""
    if not val or val in ("无", "-1", "-1.0"):
        return "—"
    return val


def _format_grade_point(point_str: str) -> str:
    """
    格式化绩点
    - Point > 0   → 显示数值（如 3.0）
    - Point == -1 → 显示 —（非主干课无绩点）
    - Point == 0  → 显示 0.0
    """
    if not point_str or point_str.strip() in ("-1.0", "-1", "无"):
        return "—"
    try:
        val = float(point_str)
    except (ValueError, TypeError):
        return "—"
    if val > 0:
        return f"{val:.1f}"
    elif val == 0.0:
        return "0.0"
    return "—"


def display_grades(records: List[GradeRecord]):
    """
    展示成绩——按学期分组，固定列宽、对齐规整
    每学期一张表格，尾部汇总总学分 | 平均绩点 | 课程数 | 及格/不及格
    """
    if not records:
        print("\n没有查到成绩记录。")
        return

    groups = group_by_semester(records)

    total_credits = 0.0       # 总学分（所有课程）
    total_courses = 0          # 总课程数
    pass_count = 0             # 及格课程数
    fail_count = 0             # 不及格课程数
    valid_point_sum = 0.0     # 有效绩点总和
    valid_point_count = 0     # 有效绩点课程数

    print()
    sep_full = "=" * _TABLE_WIDTH
    print(sep_full)
    print(f"  江西农业大学 · 成绩查询结果（共 {len(records)} 条记录）")
    print(sep_full)

    for xq in sorted(groups.keys(), reverse=True):
        semester_label = parse_semester_label(xq)
        semester_records = groups[xq]
        semester_credits = 0.0
        semester_pass = 0
        semester_fail = 0

        # ── 学期分隔行（醒目标题） ──
        print()
        print("━" * _TABLE_WIDTH)
        title_text = f"  {semester_label}  （{len(semester_records)} 门课）"
        # 居中显示
        left_pad = (_TABLE_WIDTH - display_width(title_text)) // 2
        print(" " * max(left_pad, 0) + title_text)
        print("━" * _TABLE_WIDTH)

        # ── 表头 ──
        print(_build_row(None, "header"))
        print(_build_row(None, "sep"))

        # ── 数据行 ──
        for rec in semester_records:
            # 分数格式化
            xf_disp = _format_score(rec.xf)
            pscj_disp = _format_score(rec.pscj)
            kscj_disp = _format_score(rec.kscj)
            # 总评：文字型成绩（优秀/良好等）原样显示，无数据则 —
            zpcj_disp = rec.zpcj if rec.zpcj and rec.zpcj not in ("无", "-1", "-1.0") else "—"
            point_disp = _format_grade_point(rec.point)

            cols = [
                rec.kcmc, rec.kclb, xf_disp, pscj_disp,
                kscj_disp, zpcj_disp, point_disp, rec.cbls,
            ]
            print(_build_row(cols, "data"))

            # 统计
            credit = safe_float(rec.xf)
            semester_credits += credit
            total_credits += credit
            total_courses += 1

            if rec.jgbj == 1:
                semester_pass += 1
                pass_count += 1
            else:
                semester_fail += 1
                fail_count += 1

            pv = safe_float(rec.point)
            if pv > 0:
                valid_point_sum += pv
                valid_point_count += 1

        # ── 学期底部分隔 + 小计 ──
        print(_build_row(None, "sep"))
        print(f"  本学期学分小计: {semester_credits:.1f}    及格: {semester_pass}    不及格: {semester_fail}")
        print()

    # ── 汇总统计（分隔线隔开） ──
    avg_point = valid_point_sum / valid_point_count if valid_point_count > 0 else 0.0
    print("━" * _TABLE_WIDTH)
    print(f"  【汇总统计】")
    print(f"  总课程数: {total_courses}    总学分: {total_credits:.1f}    "
          f"及格: {pass_count}    不及格: {fail_count}")
    print(f"  平均绩点: {avg_point:.2f}（仅统计 Point > 0 的 {valid_point_count} 门课程）")
    print("━" * _TABLE_WIDTH)
    print()


# =============================================================================
# 第八部分：CSV 导出
# =============================================================================

def export_to_csv(records: List[GradeRecord], file_path: str) -> bool:
    """导出成绩到 CSV 文件"""
    try:
        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "学期", "学期标签", "课程名称", "课程类别", "学分",
                "平时成绩", "考试成绩", "总评成绩", "补考成绩", "重修成绩",
                "绩点", "及格标志", "任课教师", "考试时间"
            ])
            for rec in records:
                writer.writerow([
                    rec.xq,
                    parse_semester_label(rec.xq),
                    rec.kcmc,
                    rec.kclb,
                    rec.xf,
                    rec.pscj,
                    rec.kscj,
                    rec.zpcj,
                    rec.bkcj,
                    rec.cxcj,
                    rec.point,
                    "及格" if rec.jgbj == 1 else ("不及格" if rec.jgbj == 0 else "待定"),
                    rec.cbls,
                    rec.kssj,
                ])
        logger.info(f"成绩已导出至: {file_path}")
        return True
    except (OSError, IOError) as e:
        logger.error(f"导出 CSV 失败: {e}")
        return False


# =============================================================================
# 第九部分：交互引导
# =============================================================================

def _input_password(mode: str = "plain") -> str:
    """密码输入辅助函数"""
    if mode == "plain":
        print("  [明文输入] 密码将明文显示，请确保周围环境安全")
        return input("  请输入密码: ").strip()
    elif mode == "hidden":
        import getpass
        try:
            return getpass.getpass("  请输入密码: ").strip()
        except (Exception, KeyboardInterrupt):
            logger.warning("getpass 输入失败，降级为明文输入")
            return input("  请输入密码 (明文): ").strip()
    elif mode == "auto":
        import getpass
        try:
            return getpass.getpass("  请输入密码: ").strip()
        except (Exception, KeyboardInterrupt):
            logger.warning("getpass 不可用，降级为明文输入")
            return input("  请输入密码 (明文): ").strip()
    else:
        return input("  请输入密码: ").strip()


def interactive_setup() -> GradeQueryConfig:
    """交互式配置引导"""
    config = GradeQueryConfig()
    print("\n===== 江西农业大学 · 成绩查询工具 =====")
    stu_id = os.getenv("JXAU_STU_ID", "")
    if not stu_id:
        stu_id = input("请输入学号: ").strip()
    config.STUDENT_ID = stu_id
    password = os.getenv("JXAU_PASS", "")
    if not password:
        password = _input_password(config.PASSWORD_MODE)
    config.PASSWORD = password
    base_url = input(f"教务系统地址 (回车默认 {config.BASE_URL}): ").strip()
    if base_url:
        config.BASE_URL = base_url.rstrip("/")
        config.__post_init__()
    print("配置完成！开始登录并查询成绩...\n")
    return config


# =============================================================================
# 主函数
# =============================================================================

def main():
    print("")
    print("╔══════════════════════════════════════════════════╗")
    print("║   江西农业大学 教务系统 · 成绩查询工具           ║")
    print("║                                                  ║")
    print("║  作者: 许立鑫    侵权联系: xlx20050131          ║")
    print("║  免责声明: 本工具仅用于学习研究，禁止非法用途    ║")
    print("╚══════════════════════════════════════════════════╝")
    print("")

    # 配置初始化
    if os.getenv("JXAU_STU_ID") and os.getenv("JXAU_PASS"):
        config = GradeQueryConfig()
    else:
        config = interactive_setup()

    errors = config.validate()
    if errors:
        logger.error("配置不完整，请补充以下项:")
        for e in errors:
            logger.error(f"  ✗ {e}")
        print("\n提示: 可通过环境变量 JXAU_STU_ID / JXAU_PASS 设置账号密码")
        return

    # 登录
    auth = JXAUAuthSession(config)
    if not auth.login():
        logger.error("登录失败，请检查网络或账号信息")
        return

    # 查询成绩
    service = GradeQueryService(auth)
    records = service.query_all_grades()

    if not records:
        logger.warning("未获取到任何成绩记录")
        auth.logout()
        return

    # 展示成绩
    display_grades(records)

    # 询问是否导出 CSV
    print("是否将成绩导出为 CSV 文件？")
    print("  y / 回车 → 导出")
    print("  n       → 跳过")
    choice = input("> ").strip().lower()
    if choice != "n":
        default_name = f"jxau_grades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        file_path = os.path.join(config.EXPORT_DIR, default_name)
        print(f"  导出路径: {file_path}")
        if export_to_csv(records, file_path):
            print(f"  ✅ CSV 导出成功！")
        else:
            print(f"  ❌ CSV 导出失败，请检查磁盘权限")

    auth.logout()
    print("\n成绩查询完成。\n")


if __name__ == "__main__":
    main()
