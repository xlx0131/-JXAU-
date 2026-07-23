#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江西农业大学教务系统 —— 课表查询工具
========================================

功能:
    1. 登录教务系统（验证码自动识别 / 手动输入）
    2. 获取学期列表，默认加载最新学期
    3. 按星期 X 节次展示课表（上午 / 下午 / 晚上分区）
    4. 查看详情：课程名称、周次、教师、教室、教室类型
    5. 支持学期切换

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

def setup_logger(name: str = "ScheduleQuery") -> logging.Logger:
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
        f"schedule_query_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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
class ScheduleQueryConfig:
    """课表查询工具核心配置"""
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
        "semester_list": "/Common/BaseData/GetKsXq/",
        "schedule_query": "/PaikeManage/KebiaoInfo/GetStudentKebiaoByXq/",
    })

    def __post_init__(self):
        self.LOGIN_URL = self.BASE_URL + self.ROUTES["login"]
        self.CAPTCHA_URL = self.BASE_URL + self.ROUTES["captcha"]
        self.LOGIN_POST_URL = self.BASE_URL + self.ROUTES["login_post"]
        self.SEMESTER_LIST_URL = self.BASE_URL + self.ROUTES["semester_list"]
        self.SCHEDULE_QUERY_URL = self.BASE_URL + self.ROUTES["schedule_query"]

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

    def __init__(self, config: ScheduleQueryConfig):
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

    def __init__(self, config: ScheduleQueryConfig):
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
                    logger.error("无法获取会话 GUID，课表查询接口可能不可用")
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
# 第五部分：课表数据结构
# =============================================================================

@dataclass
class ScheduleRecord:
    """单条课表记录"""
    kcmc: str               # 课程名称
    skzhou: str             # 上课周次（原始串，如 "2-5,7-9,11"）
    rkls: str               # 任课教师（原始串，如 "6545.黄文菊"）
    sjdtext: str            # 时段文本（如 "星期一 上午 1-2节"）
    xingqi: str             # 星期几（如 "1.星期一"）
    jieci: str              # 节次（如 "上午 1-2节"）
    skdd: str               # 上课地点/教室
    jslb: str               # 教室类型
    xueqi: str              # 学期编码
    xingqi_num: int = 0     # 星期数字（1=周一 .. 7=周日）
    teacher_name: str = ""  # 提取后的教师姓名
    time_slot: str = ""     # 归一化时段 key（如 "1-2"、"3-4"、"5-6"、"7-8"、"9-11"）

    @staticmethod
    def from_json(data: dict) -> "ScheduleRecord":
        """从 JSON 对象解析课表记录"""
        xingqi_raw = str(data.get("XingQi", "") or "")
        xingqi_num = ScheduleRecord._parse_xingqi_num(xingqi_raw)
        teacher_name = ScheduleRecord._extract_teacher_name(
            str(data.get("Rkls", "") or "")
        )
        time_slot = ScheduleRecord._normalize_time_slot(
            str(data.get("Jieci", "") or "")
        )

        return ScheduleRecord(
            kcmc=str(data.get("KcMc", "") or ""),
            skzhou=str(data.get("SkZhou", "") or ""),
            rkls=str(data.get("Rkls", "") or ""),
            sjdtext=str(data.get("SjdText", "") or ""),
            xingqi=xingqi_raw,
            jieci=str(data.get("Jieci", "") or ""),
            skdd=str(data.get("Skdd", "") or ""),
            jslb=str(data.get("Jslb", "") or ""),
            xueqi=str(data.get("XueQi", "") or ""),
            xingqi_num=xingqi_num,
            teacher_name=teacher_name,
            time_slot=time_slot,
        )

    @staticmethod
    def _parse_xingqi_num(xingqi: str) -> int:
        """解析星期数字："1.星期一" → 1，"星期零" → 0"""
        if not xingqi:
            return 0
        m = re.match(r"(\d+)", xingqi)
        if m:
            num = int(m.group(1))
            return num if 1 <= num <= 7 else 0
        return 0

    @staticmethod
    def _extract_teacher_name(rkls: str) -> str:
        """从工号.姓名 提取教师姓名："6545.黄文菊" → "黄文菊" """
        if not rkls:
            return ""
        # 尝试匹配 "数字.姓名" 模式
        m = re.match(r"\d+\.(.+)", rkls)
        if m:
            return m.group(1).strip()
        return rkls.strip()

    @staticmethod
    def _normalize_time_slot(jieci: str) -> str:
        """归一化节次 key"""
        if not jieci:
            return ""
        jieci = jieci.strip()
        # 上午
        m = re.search(r"上午\s*1[-—]2节", jieci)
        if m:
            return "1-2"
        m = re.search(r"上午\s*3[-—]4节", jieci)
        if m:
            return "3-4"
        # 下午
        m = re.search(r"下午\s*5[-—]6节", jieci)
        if m:
            return "5-6"
        m = re.search(r"下午\s*7[-—]8节", jieci)
        if m:
            return "7-8"
        # 晚上
        m = re.search(r"晚上\s*9[-—]11节", jieci)
        if m:
            return "9-11"
        # 白天全覆盖（白天 1-8节）
        m = re.search(r"白天\s*1[-—]8节", jieci)
        if m:
            return "1-8"
        # 兜底：从数字中提取
        m = re.search(r"(\d+)[-—](\d+)节", jieci)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
        return jieci


def simplify_weeks(weeks_str: str) -> str:
    """
    简化周次显示："2,3,4,5,7,8,9,11" → "2-5,7-9,11"
    """
    if not weeks_str:
        return ""
    # 先解析已有区间（如 "2-5"）
    parts = weeks_str.replace("，", ",").split(",")
    weeks_set = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"(\d+)[-—](\d+)", p)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            weeks_set.update(range(start, end + 1))
        else:
            try:
                weeks_set.add(int(p))
            except ValueError:
                continue

    if not weeks_set:
        return weeks_str

    sorted_weeks = sorted(weeks_set)
    ranges = []
    start = sorted_weeks[0]
    end = sorted_weeks[0]

    for w in sorted_weeks[1:]:
        if w == end + 1:
            end = w
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = w
            end = w

    # 处理最后一个区间
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def time_slot_order_key(time_slot: str) -> int:
    """返回时段排序键"""
    order = {
        "1-2": 1,
        "3-4": 2,
        "5-6": 3,
        "7-8": 4,
        "9-11": 5,
        "1-8": 0,  # 特殊：白天1-8节，放在最前面特殊处理
    }
    return order.get(time_slot, 99)


def time_slot_display_name(time_slot: str) -> str:
    """返回时段显示名称"""
    names = {
        "1-2": "1-2节",
        "3-4": "3-4节",
        "5-6": "5-6节",
        "7-8": "7-8节",
        "9-11": "9-11节",
        "1-8": "1-8节",
    }
    return names.get(time_slot, time_slot)


# =============================================================================
# 第六部分：学期列表 & 课表查询
# =============================================================================

class SemesterManager:
    """学期管理器"""

    def __init__(self, auth: JXAUAuthSession):
        self.auth = auth
        self.semesters: List[Dict[str, str]] = []

    def fetch_semesters(self) -> bool:
        """获取可选学期列表"""
        guid = self.auth.session_guid
        if not guid:
            logger.error("会话 GUID 为空，无法获取学期列表")
            return False

        url = f"{self.auth.config.SEMESTER_LIST_URL}{guid}"
        logger.info("正在获取学期列表...")

        try:
            resp = self.auth.session.post(
                url,
                timeout=self.auth.config.REQUEST_TIMEOUT,
            )
            resp.encoding = "utf-8"
            text = resp.text.strip()

            if not text.startswith("{"):
                logger.error("学期列表接口返回非 JSON 格式")
                return False

            data = json.loads(text)
            items = data.get("Data", [])
            if not isinstance(items, list) or not items:
                logger.warning("学期列表为空")
                return False

            self.semesters = [
                {"key": item.get("Key", ""), "value": item.get("Value", "")}
                for item in items
            ]
            logger.info(f"共获取 {len(self.semesters)} 个可选学期")
            return True

        except (requests.RequestException, json.JSONDecodeError, Exception) as e:
            logger.error(f"获取学期列表失败: {e}")
            return False

    def display_semesters(self) -> None:
        """展示可选学期列表"""
        if not self.semesters:
            print("\n  暂无可用学期数据。")
            return

        print("\n  ── 可选学期列表 ──")
        for i, sem in enumerate(self.semesters):
            label = parse_semester_label(sem["key"])
            print(f"    [{i + 1}] {label}  ({sem['key']})")
        print()

    def parse_semester_label(self, xq: str) -> str:
        return parse_semester_label(xq)


def parse_semester_label(xq: str) -> str:
    """将学期代码转换为可读标签，如 '20252' → '2025-2026 第二学期'"""
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


class ScheduleQueryService:
    """课表查询服务"""

    def __init__(self, auth: JXAUAuthSession):
        self.auth = auth
        self.semester_mgr = SemesterManager(auth)

    def query_schedule(self, semester_code: str) -> List[ScheduleRecord]:
        """
        查询指定学期课表
        POST {schedule_query_url}/{session_guid}
        Body: xq=学期编码
        """
        if not self.auth.session_guid:
            logger.error("会话 GUID 为空，无法查询课表")
            return []

        url = f"{self.auth.config.SCHEDULE_QUERY_URL}{self.auth.session_guid}"
        logger.info(f"正在查询课表（学期: {semester_code}）...")

        try:
            resp = self.auth.session.post(
                url,
                data={"xq": semester_code},
                timeout=self.auth.config.REQUEST_TIMEOUT,
            )
            resp.encoding = "utf-8"
            text = resp.text.strip()

            if not text.startswith("{"):
                logger.error("课表接口返回非 JSON 格式，可能会话已过期")
                return []

            return self._parse_schedule_data(text)

        except requests.RequestException as e:
            logger.error(f"课表查询请求失败: {e}")
            return []

    def _parse_schedule_data(self, text: str) -> List[ScheduleRecord]:
        """解析课表 JSON 数据"""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            return []

        if not data.get("Result", data.get("success", False)):
            msg = data.get("Message", "未知错误")
            logger.error(f"课表查询失败: {msg}")
            return []

        rows = data.get("Data", [])
        if not isinstance(rows, list):
            logger.warning("课表数据格式异常：Data 字段非列表")
            return []

        records = []
        for row in rows:
            try:
                record = ScheduleRecord.from_json(row)
                records.append(record)
            except Exception as e:
                logger.debug(f"解析课表行失败: {e} | 原始: {row}")
                continue

        logger.info(f"共获取 {len(records)} 条课表记录")
        return records


# =============================================================================
# 第七部分：课表渲染
# =============================================================================

# 星期名称映射
WEEKDAY_NAMES = {1: "星期一", 2: "星期二", 3: "星期三", 4: "星期四",
                 5: "星期五", 6: "星期六", 7: "星期日"}

# 时段分区（按节次排序）
SLOT_ORDER = ["1-2", "3-4", "5-6", "7-8", "9-11"]

# 时段分区标签
SLOT_SECTION = {
    "1-2": "上午",
    "3-4": "上午",
    "5-6": "下午",
    "7-8": "下午",
    "9-11": "晚上",
    "1-8": "全天",
}

# 各分区在显示中的分隔
SLOT_SECTION_SEPARATOR = {
    "上午": None,
    "下午": "────── 下 午 ──────",
    "晚上": "────── 晚 上 ──────",
}


def _expand_full_day(records: List[ScheduleRecord]) -> List[ScheduleRecord]:
    """
    展开"白天 1-8节"记录为多条记录（1-2、3-4、5-6、7-8）
    """
    expanded = []
    for rec in records:
        if rec.time_slot == "1-8":
            # 拆分为 4 个时段
            for sub_slot in ["1-2", "3-4", "5-6", "7-8"]:
                new_rec = ScheduleRecord(
                    kcmc=rec.kcmc,
                    skzhou=rec.skzhou,
                    rkls=rec.rkls,
                    sjdtext=rec.sjdtext,
                    xingqi=rec.xingqi,
                    jieci=rec.jieci,
                    skdd=rec.skdd,
                    jslb=rec.jslb,
                    xueqi=rec.xueqi,
                    xingqi_num=rec.xingqi_num,
                    teacher_name=rec.teacher_name,
                    time_slot=sub_slot,
                )
                expanded.append(new_rec)
        else:
            expanded.append(rec)
    return expanded


def build_schedule_grid(records: List[ScheduleRecord]) -> Dict[int, Dict[str, List[ScheduleRecord]]]:
    """
    构建课表网格：
    grid[星期_num][time_slot] = [record1, record2, ...]
    仅保留 1~7 周一到周日
    """
    # 展开"白天 1-8节"
    records = _expand_full_day(records)

    grid: Dict[int, Dict[str, List[ScheduleRecord]]] = {}
    for day in range(1, 8):
        grid[day] = {slot: [] for slot in SLOT_ORDER}

    for rec in records:
        day = rec.xingqi_num
        if day < 1 or day > 7:
            continue
        slot = rec.time_slot
        if slot not in SLOT_ORDER:
            continue
        grid[day][slot].append(rec)

    return grid


def _display_width(s: str) -> int:
    """计算字符串的显示宽度（中文=2，ASCII=1）"""
    width = 0
    for ch in s:
        code = ord(ch)
        if 0x4e00 <= code <= 0x9fff or 0x3000 <= code <= 0x303f or 0xff00 <= code <= 0xffef:
            width += 2
        else:
            width += 1
    return width


def _pad_display(s: str, width: int, align: str = "left") -> str:
    """按显示宽度填充空格"""
    dw = _display_width(s)
    if dw >= width:
        return s
    padding = width - dw
    if align == "left":
        return s + " " * padding
    elif align == "right":
        return " " * padding + s
    else:
        left = padding // 2
        right = padding - left
        return " " * left + s + " " * right


def _truncate_to_width(s: str, max_width: int) -> str:
    """按显示宽度截断，超长末尾加 …"""
    if not s:
        return ""
    w = 0
    cut = len(s)
    for i, ch in enumerate(s):
        cw = _display_width(ch)
        if w + cw > max_width - 1:
            cut = i
            break
        w += cw
    if cut == len(s):
        return s
    return s[:cut] + "…"


def render_schedule_table(records: List[ScheduleRecord], semester_label: str):
    """
    渲染课表表格 — 按行统一渲染，保证横向对齐。
    每个课程条目固定占 4 行：课程名 / 周次 / 教师 / 教室。
    同一时段内所有列等高等宽，无课列填充空白。
    """
    if not records:
        print("\n  没有查询到课表记录。")
        return

    grid = build_schedule_grid(records)

    # ── 列宽配置 ──
    # 星期一到五各 22 字符，星期六、日各 16 字符，节次标签列 8 字符
    COL_WIDTHS = {1: 22, 2: 22, 3: 22, 4: 22, 5: 22, 6: 16, 7: 16}
    WEEKDAY_LABELS = {1: "星期一", 2: "星期二", 3: "星期三", 4: "星期四",
                      5: "星期五", 6: "星期六", 7: "星期日"}
    LABEL_W = 8  # 节次标签列宽

    lines: List[str] = []

    def _build_sep(ch_h: str, ch_l: str, ch_r: str, ch_cross: str) -> str:
        """构建分隔线，ch_h=水平线, ch_l=左端, ch_r=右端, ch_cross=交叉点"""
        parts = [ch_h * LABEL_W]
        for d in range(1, 8):
            parts.append(ch_h * COL_WIDTHS[d])
        return ch_l + ch_cross.join(parts) + ch_r

    # ══ 标题 ══
    inner_w = LABEL_W + sum(COL_WIDTHS.values())  # 内容总宽（不含边框和分隔线）
    total_w = inner_w + 7  # 加 7 条 `│` 分隔线后的总宽（不含左右 ║）
    title = f"  {semester_label}  课表  "
    pad = total_w - _display_width(title)
    if pad > 0:
        title = " " * (pad // 2) + title + " " * (pad - pad // 2)
    lines.append("╔" + "═" * total_w + "╗")
    lines.append("║" + title + "║")

    # ══ 表头（星期标签） ══
    hdr = [_pad_display("节次", LABEL_W, "center")]
    for d in range(1, 8):
        hdr.append(_pad_display(WEEKDAY_LABELS[d], COL_WIDTHS[d], "center"))
    lines.append(_build_sep("═", "╠", "╣", "╤"))
    lines.append("║" + "│".join(hdr) + "║")

    # ══ 各时段 ══
    prev_section = None

    for slot_idx, slot in enumerate(SLOT_ORDER):
        current_section = SLOT_SECTION.get(slot, "")

        # ── 分区变更：双线 ══ 分隔 ──
        if current_section and current_section != prev_section and slot_idx > 0:
            lines.append(_build_sep("═", "╠", "╣", "╪"))

        prev_section = current_section

        # ── 收集该时段各天的课程数据 ──
        # 每条 course_entry = (name, week_str, teacher, room)
        day_entries: Dict[int, List[Tuple[str, str, str, str]]] = {}
        max_courses = 0
        for day in range(1, 8):
            courses = grid[day].get(slot, [])
            entries = []
            for c in courses:
                name = _truncate_to_width(c.kcmc, COL_WIDTHS[day])
                week_raw = simplify_weeks(c.skzhou) if c.skzhou else ""
                week_str = _truncate_to_width(
                    f"{week_raw}周" if week_raw else "", COL_WIDTHS[day]
                )
                teacher = _truncate_to_width(c.teacher_name, COL_WIDTHS[day])
                room = _truncate_to_width(c.skdd, COL_WIDTHS[day])
                entries.append((name, week_str, teacher, room))
            day_entries[day] = entries
            max_courses = max(max_courses, len(entries))

        # 全空时段只输出一行空白
        if max_courses == 0:
            cells = [" " * LABEL_W] + [" " * COL_WIDTHS[d] for d in range(1, 8)]
            lines.append("║" + "│".join(cells) + "║")
            # 非最后时段加分隔线
            if slot_idx < len(SLOT_ORDER) - 1:
                lines.append(_build_sep("─", "╟", "╢", "┼"))
            continue

        # ── 按行渲染（每个课程 4 行，堆叠） ──
        slot_label = time_slot_display_name(slot)
        for course_idx in range(max_courses):
            for line_type in range(4):  # 0=name, 1=week, 2=teacher, 3=room
                cells = []
                # 节次标签：仅第一门课的第一行显示
                if course_idx == 0 and line_type == 0:
                    cells.append(_pad_display(slot_label, LABEL_W, "center"))
                else:
                    cells.append(" " * LABEL_W)

                for day in range(1, 8):
                    entries = day_entries[day]
                    if course_idx < len(entries):
                        content = entries[course_idx][line_type]
                    else:
                        content = ""
                    cells.append(_pad_display(content, COL_WIDTHS[day]))

                lines.append("║" + "│".join(cells) + "║")

        # 时段间分隔线
        if slot_idx < len(SLOT_ORDER) - 1:
            lines.append(_build_sep("─", "╟", "╢", "┼"))

    # ══ 底部 ══
    lines.append(_build_sep("═", "╚", "╝", "╧"))

    # 输出
    print()
    for line in lines:
        print(line)
    print()


def display_schedule(records: List[ScheduleRecord], semester_label: str):
    """展示课表——表格日历格式"""
    render_schedule_table(records, semester_label)


# =============================================================================
# 第八部分：交互引导
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


def interactive_setup() -> ScheduleQueryConfig:
    """交互式配置引导"""
    config = ScheduleQueryConfig()
    print("\n===== 江西农业大学 · 课表查询工具 =====")
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
    print("配置完成！开始登录并查询课表...\n")
    return config


# =============================================================================
# 第九部分：主函数
# =============================================================================

def main():
    print("")
    print("╔══════════════════════════════════════════════════╗")
    print("║   江西农业大学 教务系统 · 课表查询工具           ║")
    print("║                                                  ║")
    print("║  作者: 许立鑫    侵权联系: xlx20050131          ║")
    print("║  免责声明: 本工具仅用于学习研究，禁止非法用途    ║")
    print("╚══════════════════════════════════════════════════╝")
    print("")

    # 配置初始化
    if os.getenv("JXAU_STU_ID") and os.getenv("JXAU_PASS"):
        config = ScheduleQueryConfig()
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

    # 获取学期列表
    semester_mgr = SemesterManager(auth)
    if not semester_mgr.fetch_semesters():
        logger.error("获取学期列表失败")
        auth.logout()
        return

    semester_mgr.display_semesters()

    # 默认选最新学期（列表第一个）
    current_sem = semester_mgr.semesters[0]
    current_sem_key = current_sem["key"]

    # 查询课表
    service = ScheduleQueryService(auth)
    records = service.query_schedule(current_sem_key)

    if not records:
        logger.warning(f"未获取到课表记录（学期: {current_sem_key}）")
    else:
        semester_label = parse_semester_label(current_sem_key)
        display_schedule(records, semester_label)

    # 学期切换交互
    while True:
        print("\n操作选项：")
        print("  [1-{}] 切换到对应学期查看课表".format(len(semester_mgr.semesters)))
        print("  [q]   退出")
        choice = input("> ").strip().lower()

        if choice == "q":
            break

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(semester_mgr.semesters):
                selected = semester_mgr.semesters[idx]
                if selected["key"] == current_sem_key:
                    print(f"  已在当前学期，无需切换。")
                    continue
                current_sem_key = selected["key"]
                semester_label = parse_semester_label(current_sem_key)
                print(f"\n  切换到: {semester_label}")
                records = service.query_schedule(current_sem_key)
                if records:
                    display_schedule(records, semester_label)
                else:
                    print(f"\n  该学期（{semester_label}）暂无课表数据。")
            else:
                print(f"  请输入 1~{len(semester_mgr.semesters)} 或 q")
        except ValueError:
            print(f"  请输入数字 1~{len(semester_mgr.semesters)} 或 q")

    auth.logout()
    print("\n课表查询完成。\n")


if __name__ == "__main__":
    main()
