#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江西农业大学教务系统 —— 抢课脚本 (学习研究版)
===============================================

作者: 许立鑫
    侵权联系删除: xlx20050131

    免责声明:
    本脚本仅用于学习 Python 网络编程、HTTP 协议、多线程编程等技术知识。
    作者无意破坏任何学校的教务管理系统，禁止用于任何非法用途或商业行为。
    使用者应遵守《江西农业大学本科生选课管理办法》及相关法律法规。
    如因使用本脚本造成的一切后果，由使用者自行承担。

技术要点:
    1. 基于 requests 库模拟 ASP.NET MVC 自研教务系统 HTTP 请求
    2. Session 维持登录态 + Cookie 自动管理
    3. 验证码自动识别（ddddocr 离线 OCR，失败降级手动输入）
    4. 单线程轮询 + 随机延时规避检测
    5. 结构化日志输出，方便排查问题

兼容性:
    - Python 3.8+
    - 依赖: requests, beautifulsoup4, lxml, ddddocr
    - 安装: pip install requests beautifulsoup4 lxml ddddocr

—— 仅用于学习研究, 请勿用于非法用途 ——
"""

import os
import sys
import time
import json
import random
import logging
import threading
import re
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

# ddddocr: 开源离线 OCR 库，专门针对字符验证码场景优化
# 首次导入时会自动下载内置模型文件（约 5MB），此后离线可用
try:
    import ddddocr
    _HAS_DDDDOCR = True
except ImportError:
    _HAS_DDDDOCR = False

# =============================================================================
# 第一部分：日志配置
# =============================================================================

def setup_logger(name: str = "CourseGrabber") -> logging.Logger:
    """配置结构化日志，同时输出到控制台和文件"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 控制台 Handler —— INFO 级别
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)

    # 文件 Handler —— DEBUG 级别（记录全部细节）
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir,
        f"grabber_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


logger = setup_logger()

# =============================================================================
# 排版辅助函数（统一中英文混排对齐方案）
# =============================================================================

def _char_width(ch: str) -> int:
    """返回单个字符的显示宽度（中文=2，ASCII=1）"""
    code = ord(ch)
    if 0x4e00 <= code <= 0x9fff or 0x3000 <= code <= 0x303f or 0xff00 <= code <= 0xffef:
        return 2
    return 1


def display_width(s: str) -> int:
    """计算字符串的显示宽度（中文=2，ASCII=1）"""
    return sum(_char_width(ch) for ch in s)


def _truncate_to_width(s: str, max_width: int) -> str:
    """按显示宽度截断，超长末尾加 …，返回后总显示宽度恰好为 max_width"""
    w = 0
    cut_idx = len(s)
    for i, ch in enumerate(s):
        cw = _char_width(ch)
        if w + cw > max_width - 1:
            cut_idx = i
            break
        w += cw
    if cut_idx == len(s):
        return s
    result = s[:cut_idx] + "…"
    pad = max_width - display_width(result)
    if pad > 0:
        result += " " * pad
    return result


def pad_str(s: str, width: int, align: str = "left") -> str:
    """按显示宽度填充/截断字符串，align=left/right/center"""
    s = str(s) if s is not None else ""
    dw = display_width(s)
    if dw > width:
        return _truncate_to_width(s, width)
    padding = width - dw
    if align == "left":
        return s + " " * padding
    elif align == "right":
        return " " * padding + s
    else:  # center
        left_pad = padding // 2
        right_pad = padding - left_pad
        return " " * left_pad + s + " " * right_pad


# =============================================================================
# 第二部分：配置管理
# =============================================================================

@dataclass
class GrabberConfig:
    """
    抢课脚本核心配置
    所有敏感信息（密码等）从环境变量读取，避免硬编码泄露
    """
    # ── 教务系统地址（请根据实际情况修改） ──────────────────────────────
    BASE_URL: str = "https://jwgl.jxau.edu.cn"  # 自研 ASP.NET MVC 教务系统
    LOGIN_URL: str = ""                         # 会自动拼装
    CAPTCHA_URL: str = ""
    COURSE_LIST_URL: str = ""
    SUBMIT_URL: str = ""

    # ── 账号信息（优先读环境变量，降低泄露风险） ──────────────────────
    STUDENT_ID: str = field(default_factory=lambda: os.getenv("JXAU_STU_ID", ""))
    PASSWORD: str = field(default_factory=lambda: os.getenv("JXAU_PASS", ""))
    # 如果环境变量未设置，可在运行时手动输入

    # ── 密码输入模式 ─────────────────────────────────────────────────────
    PASSWORD_MODE: str = "plain"         # "plain": 明文 input / "hidden": getpass 隐式 / "auto": 自动检测
    # plain: 输入时明文回显，方便确认输入正确，适合个人电脑
    # hidden: 使用 getpass 无回显，适合公共场合（注意：部分终端/IDE 不兼容）
    # auto: 先尝试 getpass，失败则回退 plain

    # ── 目标课程（课程代码 或 课程名称关键词） ─────────────────────────
    TARGET_COURSES: List[str] = field(default_factory=lambda: [
        # 示例: "B2001234",         # 按课程代码
        #        "形势与政策",       # 按课程名称关键词
    ])

    # ── 选课学年学期（如 "2025-2026-1" 代表 2025-2026 第一学期） ──────
    YEAR_TERM: str = ""

    # ── 抢课策略 ─────────────────────────────────────────────────────────
    POLL_INTERVAL: Tuple[float, float] = (0.5, 1.5)  # 轮询间隔秒数范围（随机）
    MAX_RETRIES: int = 3                             # 单次请求最大重试次数
    RETRY_DELAY: float = 1.0                         # 重试前等待秒数
    WORKER_THREADS: int = 3                          # 并发线程数
    MAX_RUN_TIME: int = 3600                         # 最大运行时间（秒），默认1小时
    CAPTCHA_WAIT_TIMEOUT: int = 120                  # 等待验证码手动输入超时（秒）

    # ── 网络设置 ─────────────────────────────────────────────────────────
    REQUEST_TIMEOUT: int = 15                        # HTTP 请求超时秒数
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    PROXIES: Optional[Dict[str, str]] = None         # 代理（一般不需要）

    # ── 验证码识别配置（默认使用 ddddocr 离线自动识别） ───────────
    CAPTCHA_ENGINE: str = "ocr"          # 识别引擎: "ocr" (ddddocr) / "manual" / "ttshitu"
    OCR_CONFIDENCE_THRESHOLD: float = 0.3  # OCR 置信度阈值（低于此值视为低置信度，降级手动）
    OCR_MAX_RETRIES: int = 3               # OCR 识别失败时的最大重试次数
    OCR_USE_CHARSET: bool = True           # 是否启用字符集约束（仅匹配数字+字母，提高准确率）

    # ── 打码平台 API（预留，默认关闭） ──────────────────────────────────
    CAPTCHA_API_URL: str = ""
    CAPTCHA_API_KEY: str = ""
    # 支持: "ttshitu" / "dama" 等（需配合 CAPTCHA_ENGINE="ttshitu" 使用）

    # ── 自研 ASP.NET MVC 教务系统路由（基于实际抓包） ────────────────
    ROUTES: Dict[str, str] = field(default_factory=lambda: {
        "login": "/",
        "captcha": "/User/Validation/",
        "login_post": "/User/CheckLogin",
        "get_kc_info": "/KcManage/GxKcManage/GetKcInfo/",
        "xk_info": "/KcManage/GxKcManage/XkInfo/",
    })

    def __post_init__(self):
        """自动拼装完整 URL"""
        self.LOGIN_URL = self.BASE_URL + self.ROUTES["login"]
        self.CAPTCHA_URL = self.BASE_URL + self.ROUTES["captcha"]
        self.LOGIN_POST_URL = self.BASE_URL + self.ROUTES["login_post"]
        # 以下路由需要 session_guid，运行时动态拼装
        self.GET_KC_INFO_PREFIX = self.BASE_URL + self.ROUTES["get_kc_info"]
        self.XK_INFO_PREFIX = self.BASE_URL + self.ROUTES["xk_info"]

    def validate(self) -> List[str]:
        """检查配置完整性，返回缺失项列表（空列表表示完整）"""
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
    """
    验证码处理器
    默认使用 ddddocr 离线 OCR 自动识别，识别失败或置信度过低时自动降级为手动输入。
    同时保留打码平台接口（ttshitu 等）作为备用引擎，通过 config.CAPTCHA_ENGINE 切换。

    处理流程:
        OCR 模式: ddddocr 识别 → 置信度检查 → 成功返回 | 低置信度/失败 → 降级手动输入
        手动模式: 保存图片 → 用户查看 → 键盘输入
        打码平台: HTTP 调用第三方 API → 返回结果
    """

    def __init__(self, config: GrabberConfig):
        self.config = config
        self._captcha_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "captcha_cache"
        )
        os.makedirs(self._captcha_dir, exist_ok=True)
        self._ocr_instance = None  # 懒初始化

    # ── 公共入口 ─────────────────────────────────────────────────────────

    def recognize(self, image_data: bytes, img_name: str = "captcha") -> str:
        """
        识别验证码图片，根据配置自动路由到对应引擎。
        优先级: ddddocr (默认) > 手动输入 > 打码平台
        """
        engine = self.config.CAPTCHA_ENGINE

        if engine == "ocr":
            return self._ocr_recognition(image_data, img_name)
        elif engine == "manual":
            return self._manual_recognition(image_data, img_name)
        elif engine == "ttshitu":
            return self._platform_recognition(image_data, img_name)
        else:
            logger.warning(f"未知识别引擎 '{engine}'，回退 ddddocr")
            return self._ocr_recognition(image_data, img_name)

    # ── ddddocr 自动识别（默认引擎） ────────────────────────────────────

    def _get_ocr(self) -> "ddddocr.DdddOcr":
        """懒初始化 ddddocr 实例（全局单例，避免重复加载模型）"""
        if self._ocr_instance is None:
            if not _HAS_DDDDOCR:
                raise RuntimeError(
                    "ddddocr 未安装，请执行: pip install ddddocr\n"
                    "安装后即可自动识别验证码，无需人工干预。"
                )
            logger.info("初始化 ddddocr 模型（首次加载约 2~5 秒）...")
            self._ocr_instance = ddddocr.DdddOcr(
                show_ad=False,       # 关闭广告输出
                ocr=True,            # 启用 OCR 模式（字符识别）
                det=False,           # 关闭目标检测（纯字符验证码不需要）
            )
            logger.info("ddddocr 模型加载完成")
        return self._ocr_instance

    def _ocr_recognition(self, image_data: bytes, img_name: str) -> str:
        """
        ddddocr 自动识别 + 降级策略
        流程: 最多重试 OCR_MAX_RETRIES 次 → 检查置信度 → 达标则返回 → 否则降级手动
        """
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
                # 可选的字符集过滤：只保留数字和字母（正方系统典型验证码格式）
                if self.config.OCR_USE_CHARSET:
                    filtered = re.sub(r"[^a-zA-Z0-9]", "", result)
                    if filtered != result:
                        logger.debug(f"OCR 原始结果 '{result}' 经字符集过滤为 '{filtered}'")
                        result = filtered

                # ddddocr 不直接暴露置信度分数，因此采用间接判断：
                #   - 结果长度异常（过长/过短，典型验证码 4~6 位）  → 低置信度
                #   - 空结果上述已过滤
                #   - 用户可通过 OCR_CONFIDENCE_THRESHOLD 调节，此处映射为 min_len 要求
                min_len = max(1, int(4 * threshold))  # 典型 4 位验证码，阈值 0.3 → 至少 1 位
                if len(result) < min_len:
                    logger.warning(
                        f"OCR 结果 '{result}' 长度 {len(result)} 过短 "
                        f"(阈值 {min_len})，视为低置信度"
                    )
                    continue

                logger.info(f"ddddocr 识别成功: '{result}'")
                return result

            except RuntimeError as e:
                if "未安装" in str(e):
                    logger.warning(f"ddddocr 不可用: {e}")
                    break  # 直接降级，不再重试
                logger.warning(f"OCR 识别异常 (第{attempt}次): {e}")
            except Exception as e:
                logger.warning(f"OCR 识别异常 (第{attempt}次): {e}")

            time.sleep(0.3)  # 重试间隔

        # 全部 OCR 重试失败 → 降级为手动输入
        logger.warning("ddddocr 自动识别失败，降级为手动输入验证码")
        return self._manual_recognition(image_data, img_name)

    # ── 手动输入（降级方案） ────────────────────────────────────────────

    def _manual_recognition(self, image_data: bytes, img_name: str) -> str:
        """手动输入模式：保存图片到本地，由用户查看后输入"""
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

    # ── 打码平台（预留备用） ────────────────────────────────────────────

    def _platform_recognition(self, image_data: bytes, img_name: str) -> str:
        """
        打码平台自动识别（预留接口）
        当前实现了 ttshitu 平台的调用框架，需自行配置 API 地址和 Key 使用
        """
        if not self.config.CAPTCHA_API_URL or not self.config.CAPTCHA_API_KEY:
            logger.warning("打码平台 API 地址或 Key 未配置，降级为 ddddocr")
            return self._ocr_recognition(image_data, img_name)

        logger.info("调用打码平台识别验证码 ...")
        try:
            # ── 以下为 ttshitu 平台调用示例 ──
            # resp = requests.post(
            #     self.config.CAPTCHA_API_URL,
            #     data={
            #         "key": self.config.CAPTCHA_API_KEY,
            #         "type": 3,    # 纯数字/字母验证码类型
            #     },
            #     files={"image": ("captcha.png", image_data)},
            #     timeout=15,
            # )
            # result = resp.json()
            # if result.get("success"):
            #     code = result["data"]["result"]
            #     logger.info(f"打码平台识别成功: '{code}'")
            #     return code
            # else:
            #     logger.error(f"打码失败: {result.get('msg', '')}")
            logger.warning("打码平台调用未实际启用，降级 ddddocr")
            return self._ocr_recognition(image_data, img_name)
        except Exception as e:
            logger.error(f"打码平台异常: {e}，降级 ddddocr")
            return self._ocr_recognition(image_data, img_name)


# =============================================================================
# 第四部分：教务系统会话管理
# =============================================================================

class JXAUAuthSession:
    """
    自研 ASP.NET MVC 教务系统会话管理器
    负责登录、Cookie 维持、心跳检测与登出
    系统架构：纯 ASP.NET MVC，路由在根路径下，无需 CSRF Token / RSA 加密
    """

    def __init__(self, config: GrabberConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        if config.PROXIES:
            self.session.proxies.update(config.PROXIES)
        self.logged_in = False
        self.session_guid = ""               # 登录后从 URL 中提取的会话 GUID

    def login(self) -> bool:
        """
        执行登录流程（自研 ASP.NET MVC 教务系统）
        步骤: 1) GET 登录页（获取 Session Cookie）→ 2) 获取验证码 → 3) POST 登录
        """
        logger.info("开始登录流程 ...")

        # ── Step 1: GET 登录页，获取 Session Cookie ────────────────────
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

        # ── Step 2: 获取验证码 ──────────────────────────────────────────
        captcha_code = self._fetch_and_recognize_captcha()
        if not captcha_code:
            logger.error("验证码获取失败，登录终止")
            return False

        # ── Step 3: POST 登录 ──────────────────────────────────────────
        login_data = {
            "UserName": self.config.STUDENT_ID,
            "PassWord": self.config.PASSWORD,
            "validation": captcha_code,
        }
        logger.debug(f"登录参数: UserName={login_data['UserName']}, "
                     f"PassWord=***, validation={login_data['validation']}")

        try:
            resp = self.session.post(
                self.config.LOGIN_POST_URL,
                data=login_data,
                timeout=self.config.REQUEST_TIMEOUT,
                allow_redirects=True,        # ASP.NET 登录后通常 redirect
            )
            resp.encoding = "utf-8"
        except requests.RequestException as e:
            logger.error(f"登录请求失败: {e}")
            return False

        # ── Step 4: 验证登录是否成功 ────────────────────────────────────
        if self._check_login_success(resp):
            self.logged_in = True
            # 从最终 URL 中提取会话 GUID（后续选课提交接口需要）
            guid_match = re.search(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                resp.url, re.I
            )
            if guid_match:
                self.session_guid = guid_match.group()
                logger.info(f"会话 GUID: {self.session_guid}")
            else:
                logger.warning("未能从 URL 中提取会话 GUID，选课提交接口可能无法使用")
            logger.info("=== 登录成功 ===")
            return True
        else:
            logger.warning("登录失败，请检查学号/密码/验证码")
            logger.debug(f"最终 URL: {resp.url}")
            logger.debug(f"响应内容(前500字): {resp.text[:500]}")
            return False

    def _fetch_and_recognize_captcha(self) -> str:
        """
        获取验证码图片（加时间戳防缓存）并识别
        验证码 Cookie 与图片绑定，必须用同一 Session
        """
        captcha_handler = CaptchaHandler(self.config)
        # 加时间戳参数防止浏览器缓存影响
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
        """
        校验登录是否成功（ASP.NET MVC 自研系统）
        判断依据（按优先级）:
          1. 重定向到主页 / 非登录页
          2. 响应内容含"退出/注销/欢迎"
          3. 响应 JSON 含 success 标识
        """
        # 方式一：检查是否跳转离开登录页
        if "login" not in resp.url.lower():
            return True

        # 方式二：检查内容关键词
        for keyword in ["退出", "注销", "欢迎", "logout", "main"]:
            if keyword in resp.text:
                return True

        # 方式三：检查 JSON 返回
        try:
            j = resp.json()
            if j.get("success") or j.get("flag") == "1" or j.get("state") == "1":
                return True
            # 明确失败信号
            if j.get("msg") and ("失败" in j["msg"] or "错误" in j["msg"]):
                return False
        except (json.JSONDecodeError, AttributeError):
            pass

        # 方式四：检查是否跳回登录页（失败特征）
        if "验证码" in resp.text and ("UserName" in resp.text or "login" in resp.url.lower()):
            return False

        return False

    def keep_alive(self) -> bool:
        """
        发送心跳请求，保持会话不过期
        返回 True 表示会话仍然有效
        """
        if not self.logged_in:
            return False
        try:
            resp = self.session.get(
                self.config.LOGIN_URL,
                timeout=self.config.REQUEST_TIMEOUT,
                allow_redirects=False,
            )
            # 未重定向到登录页说明会话有效
            return resp.status_code == 200 and "login" not in resp.url.lower()
        except requests.RequestException:
            return False

    def logout(self):
        """退出登录（清理会话）"""
        if not self.logged_in:
            return
        try:
            # ASP.NET MVC 常见退出路由，暂时使用 GET 主页后关闭 Session
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
# 第五部分：课程查询模块
# =============================================================================

@dataclass
class CourseInfo:
    """课程信息数据结构"""
    course_id: str               # 课程 ID
    course_name: str             # 课程名称
    jxb_bh: str = ""             # 教学班编号（选课提交的关键参数，来自 Data[].JxbBh）
    teacher: str = ""            # 授课教师
    credit: str = ""             # 学分
    capacity: int = 0            # 总容量
    enrolled: int = 0            # 已选人数
    remain: int = 0              # 剩余名额
    schedule: str = ""           # 上课时间地点
    status: str = ""             # 选课状态（可选/已满/冲突）

    @property
    def is_full(self) -> bool:
        """是否已满"""
        return self.remain <= 0

    @property
    def raw_remain(self) -> int:
        """精确剩余（有些系统 enrolled > capacity 表示超选）"""
        return max(0, self.capacity - self.enrolled)


class CourseQuery:
    """
    课程查询模块
    通过 GetKcInfo 接口从教务系统获取可选课程列表
    """

    def __init__(self, auth: JXAUAuthSession, config: GrabberConfig):
        self.auth = auth
        self.config = config
        # 连续返回 HTML（会话过期）计数器
        self._consecutive_html = 0

    def get_all_courses(self, limit: int = 200) -> List[CourseInfo]:
        """
        通过 GetKcInfo 接口拉取所有公选课
        POST {prefix}/{session_guid}  参数: xklb=任选, start=0, limit=N
        返回: 课程列表（已按剩余名额降序排列）
        """
        if not self.auth.session_guid:
            logger.error("会话 GUID 为空，无法查询课程")
            return []

        url = f"{self.config.GET_KC_INFO_PREFIX}{self.auth.session_guid}"
        params = {
            "xklb": "任选",
            "start": "0",
            "limit": str(limit),
        }

        logger.info(f"正在查询可选课程 (limit={limit}) ...")
        try:
            resp = self.auth.session.post(
                url,
                data=params,
                timeout=self.config.REQUEST_TIMEOUT,
            )
            resp.encoding = "utf-8"
            text = resp.text.strip()

            # 检测非 JSON 响应 → 大概率是登录页（会话过期）
            if not text.startswith("{"):
                self._consecutive_html += 1
                logger.warning(
                    f"GetKcInfo 返回非 JSON（第 {self._consecutive_html} 次连续），"
                    "会话可能已过期"
                )
                return []

            # 正常 JSON 响应，重置计数器
            self._consecutive_html = 0

            courses = self._parse_course_list(text)
            # 按剩余名额降序排列
            courses.sort(key=lambda c: c.remain, reverse=True)
            logger.info(f"查询完成，共获取 {len(courses)} 门课程")
            return courses
        except requests.RequestException as e:
            logger.error(f"查询课程列表失败: {e}")
            return []

    def _parse_course_list(self, text: str) -> List[CourseInfo]:
        """
        解析 GetKcInfo 接口返回的 JSON
        响应格式: {"Data": [{"JxbBh":..., "Jxb":..., "RkLs":..., "Zxf":...,
                             "SkRs":..., "MaxRs":..., "Xkrl":..., "Sksj":...}, ...]}
        """
        courses = []
        if not text.strip().startswith("{"):
            logger.warning("GetKcInfo 返回非 JSON 格式，尝试 HTML 降级")
            return self._parse_html_fallback(text)

        try:
            data = json.loads(text)
            rows = data.get("Data", data.get("data", []))
            if not isinstance(rows, list):
                logger.warning("GetKcInfo 返回数据格式异常：Data 字段非列表")
                return []

            for row in rows:
                try:
                    jxb_bh = str(row.get("JxbBh", row.get("jxbBh", "")) or "")
                    course_name = str(row.get("Jxb", row.get("jxb", "")) or "")
                    teacher = str(row.get("RkLs", row.get("rkLs", "")) or "")
                    credit = str(row.get("Zxf", row.get("zxf", "")) or "0")
                    enrolled = int(row.get("SkRs", row.get("skRs", 0)) or 0)
                    capacity = int(row.get("MaxRs", row.get("maxRs", 0)) or 0)
                    schedule = str(row.get("Sksj", row.get("sksj", "")) or "")
                    xkrl = str(row.get("Xkrl", row.get("xkrl", "")) or "")

                    # 通过 Xkrl 字段解析实际剩余（如有则优先）
                    remain = 0
                    if xkrl:
                        parts = xkrl.replace(" ", "").split("/")
                        if len(parts) >= 2:
                            try:
                                remain = int(parts[1]) - int(parts[0])
                            except ValueError:
                                pass
                    if remain <= 0:
                        remain = capacity - enrolled

                    course = CourseInfo(
                        course_id=jxb_bh,
                        course_name=course_name,
                        jxb_bh=jxb_bh,
                        teacher=teacher,
                        credit=credit,
                        capacity=capacity,
                        enrolled=enrolled,
                        remain=remain,
                        schedule=schedule,
                    )
                    courses.append(course)
                except (ValueError, TypeError) as e:
                    logger.debug(f"解析课程行失败: {e} | 原始: {row}")
                    continue

        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}，尝试 HTML 降级")

        return courses

    def _parse_html_fallback(self, html: str) -> List[CourseInfo]:
        """HTML 表格降级解析（兼容旧版）"""
        courses = []
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table") or soup.find("tbody")
        if not table:
            return courses
        rows = table.find_all("tr")
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            cols = [td.get_text(strip=True) for td in tds]
            try:
                course = CourseInfo(
                    course_id=cols[0],
                    course_name=cols[1],
                    teacher=cols[2],
                    credit=cols[3],
                    capacity=int(cols[4]) if cols[4].isdigit() else 0,
                    enrolled=int(cols[5]) if cols[5].isdigit() else 0,
                )
                course.remain = course.capacity - course.enrolled
                courses.append(course)
            except (IndexError, ValueError):
                continue
        return courses


# =============================================================================
# 第六部分：选课提交模块
# =============================================================================

class CourseSelector:
    """
    选课提交模块
    负责构造选课请求、处理冲突和重试
    """

    def __init__(self, auth: JXAUAuthSession, config: GrabberConfig):
        self.auth = auth
        self.config = config
        self.success_log: List[Dict] = []  # 选课成功记录

    def select_course(self, course: CourseInfo) -> bool:
        """
        提交选课请求（单次尝试）
        使用 XkInfo 接口：POST /KcManage/GxKcManage/XkInfo/{session_guid}
        参数: JxbBh（教学班编号）
        返回: {"Result": true/false, "Message": "提示信息"}
        """
        if not self.auth.session_guid:
            logger.error("会话 GUID 为空，无法调用选课提交接口，请重新登录")
            return False

        if not course.jxb_bh:
            logger.error(f"课程 {course.course_name} 的教学班编号 (JxbBh) 为空，无法提交")
            return False

        # ── 构造选课 URL ────────────────────────────────────────────────
        submit_url = f"{self.config.XK_INFO_PREFIX}{self.auth.session_guid}"
        data = {"JxbBh": course.jxb_bh}

        logger.debug(f"选课提交 URL: {submit_url}")
        logger.debug(f"选课参数: {data}")
        logger.info(f"正在提交选课: {course.course_name} (JxbBh: {course.jxb_bh})")

        # ── 发送选课请求 ────────────────────────────────────────────────
        for attempt in range(1, self.config.MAX_RETRIES + 1):
            try:
                resp = self.auth.session.post(
                    submit_url,
                    json=data,
                    timeout=self.config.REQUEST_TIMEOUT,
                )
                resp.encoding = "utf-8"
                result = self._parse_submit_result(resp.text)
            except requests.RequestException as e:
                logger.warning(f"选课请求异常 (第{attempt}次): {e}")
                time.sleep(self.config.RETRY_DELAY)
                continue

            # ── 处理结果 ────────────────────────────────────────────────
            if result["success"]:
                msg = result.get("msg", "")
                logger.info(f"选课成功！课程: {course.course_name} ({course.jxb_bh})"
                            f"{' - ' + msg if msg else ''}")
                self.success_log.append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "course_id": course.course_id,
                    "course_name": course.course_name,
                    "teacher": course.teacher,
                    "jxb_bh": course.jxb_bh,
                })
                return True
            else:
                msg = result.get("msg", "未知原因")
                logger.warning(f"选课失败 (第{attempt}次): {msg}")

                # 如果"已满"则不必再重试本轮
                if "满" in msg or "名额不足" in msg:
                    logger.info(f"课程 {course.course_name} 已满，退出重试")
                    return False

                # 如果"冲突"或"时间冲突"
                if "冲突" in msg or "时间" in msg:
                    logger.info(f"课程 {course.course_name} 时间冲突")
                    return False

                # 如果"已选"或"已选择"——已选过，视为成功
                if "已选" in msg:
                    logger.info(f"课程 {course.course_name} 已在课表中，视为成功")
                    self.success_log.append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "course_id": course.course_id,
                        "course_name": course.course_name,
                        "teacher": course.teacher,
                        "jxb_bh": course.jxb_bh,
                        "note": "已存在"
                    })
                    return True

                time.sleep(self.config.RETRY_DELAY)

        return False

    def _parse_submit_result(self, text: str) -> Dict:
        """
        解析选课提交接口的返回结果
        XkInfo 接口返回格式: {"Result": true/false, "Message": "提示信息"}
        """
        # ── JSON 返回（首选） ────────────────────────────────────────────
        if text.strip().startswith("{"):
            try:
                data = json.loads(text)
                result_flag = data.get("Result", data.get("result", data.get("success", False)))
                msg = data.get("Message", data.get("message", data.get("msg", "")))
                if isinstance(result_flag, str):
                    success = result_flag.lower() in ("true", "1", "success")
                else:
                    success = bool(result_flag)
                logger.debug(f"解析选课结果: success={success}, msg={msg}")
                return {"success": success, "msg": msg, "data": data}
            except json.JSONDecodeError:
                pass

        # ── HTML 返回（降级兜底） ────────────────────────────────────────
        soup = BeautifulSoup(text, "lxml")
        text_content = soup.get_text("\n", strip=True)

        success_keywords = ["选课成功", "成功", "success", "您已选上"]
        fail_keywords = ["失败", "已满", "冲突", "已选", "人数已满", "限选", "名额不足"]

        for kw in success_keywords:
            if kw in text_content:
                return {"success": True, "msg": text_content[:200]}

        for kw in fail_keywords:
            if kw in text_content:
                return {"success": False, "msg": text_content[:200]}

        # 兜底：既不明确成功也不明确失败
        logger.debug(f"未识别选课结果: {text_content[:200]}")
        return {"success": False, "msg": text_content[:200]}


# =============================================================================
# 第七部分：抢课调度引擎
# =============================================================================

class GrabberEngine:
    """
    抢课调度引擎
    多线程并发轮询 + 状态监控 + 自动重登
    """

    def __init__(self, config: GrabberConfig):
        self.config = config
        self.auth = JXAUAuthSession(config)
        self.query = CourseQuery(self.auth, config)
        self.selector = CourseSelector(self.auth, config)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._start_time: Optional[float] = None
        self._stats = {
            "total_attempts": 0,
            "success_count": 0,
            "full_count": 0,
            "error_count": 0,
        }

    def run(self):
        """启动抢课流程"""
        # ── 0. 配置校验 ─────────────────────────────────────────────────
        errors = self.config.validate()
        if errors:
            logger.error("配置校验失败，请补充以下项:")
            for e in errors:
                logger.error(f"  ✗ {e}")
            logger.info("\n提示: 可通过环境变量 JXAU_STU_ID / JXAU_PASS 设置账号密码")
            return

        logger.info("=" * 55)
        logger.info("  江西农业大学 · 抢课脚本 (学习研究版)")
        logger.info(f"  学年学期: {self.config.YEAR_TERM}")
        logger.info(f"  轮询间隔: {self.config.POLL_INTERVAL[0]}~{self.config.POLL_INTERVAL[1]}秒")
        logger.info(f"  最大运行: {self.config.MAX_RUN_TIME // 60} 分钟")
        logger.info("=" * 55)

        # ── 1. 登录 ─────────────────────────────────────────────────────
        if not self.auth.login():
            logger.error("登录失败，请检查网络或账号信息")
            return
        self._start_time = time.time()

        # ── 2. 查询全部可选课程并展示 ───────────────────────────────────
        all_courses = self.query.get_all_courses(limit=200)
        if not all_courses:
            logger.error("未获取到任何课程，请检查网络连接或学年学期设置")
            self.auth.logout()
            return

        targets = self._interactive_select_courses(all_courses)
        if not targets:
            logger.warning("未选择任何目标课程，退出")
            self.auth.logout()
            return

        # ── 2.5 初始全满检查（仅一次） ─────────────────────────────────
        fresh_check = self.query.get_all_courses(limit=200)
        if fresh_check:
            all_full = True
            for c in targets:
                for fc in fresh_check:
                    if fc.jxb_bh == c.jxb_bh and not fc.is_full:
                        all_full = False
                        break
                if not all_full:
                    break

            if all_full:
                print("\n所有目标课程当前均已满。是否继续等待有人退课？")
                print("  y / 回车 → 继续轮询等待")
                print("  n       → 返回课程列表重新选课")
                choice = input("> ").strip().lower()
                if choice == 'n':
                    targets = self._interactive_select_courses(fresh_check)
                    if not targets:
                        logger.warning("未选择任何目标课程，退出")
                        self.auth.logout()
                        return

        # ── 3. 单线程抢课循环 ───────────────────────────────────────────
        logger.info("进入抢课循环 ... (按 Ctrl+C 停止)")
        try:
            while not self._stop_event.is_set():
                # 检查运行时长
                if time.time() - self._start_time > self.config.MAX_RUN_TIME:
                    logger.info(f"已达最大运行时间 ({self.config.MAX_RUN_TIME // 60}分钟)，自动停止")
                    break

                # 检查会话有效性（含连续 HTML 自动重登）
                if not self._check_session():
                    break

                # 统一查询一次全量课程（limit=200 确保覆盖所有可选课程）
                fresh_courses = self.query.get_all_courses(limit=200)

                # 全量查询后等待 1.5~3 秒，降低请求频率防封禁
                query_delay = random.uniform(1.5, 3.0)
                time.sleep(query_delay)

                # 会话过期时本轮跳过，重登后下一轮继续
                if not fresh_courses and self.query._consecutive_html > 0:
                    sleep_time = random.uniform(*self.config.POLL_INTERVAL)
                    time.sleep(sleep_time)
                    continue

                # 遍历每个目标课程，有余量则提交选课
                all_done = True
                for course in targets:
                    if self._is_course_done(course):
                        continue
                    all_done = False

                    # 在最新课程数据中定位该目标课程
                    current = None
                    for fc in fresh_courses:
                        if fc.jxb_bh == course.jxb_bh:
                            current = fc
                            break

                    if current is None:
                        logger.warning(
                            f"目标课程 {course.course_name} 未出现在当前查询结果中，"
                            "可能已下线或不在查询范围内"
                        )
                        continue

                    if current.is_full:
                        logger.info(
                            f"课程 {current.course_name} 已满"
                            f"（{current.enrolled}/{current.capacity}），"
                            "继续等待..."
                        )
                        with self._lock:
                            self._stats["full_count"] += 1
                        continue

                    # 有余量 → 提交选课
                    with self._lock:
                        self._stats["total_attempts"] += 1

                    success = self.selector.select_course(current)
                    if success:
                        with self._lock:
                            self._stats["success_count"] += 1
                        note = self.selector.success_log[-1].get("note", "")
                        logger.info(f"选课成功！{current.course_name} - {note}")
                    else:
                        with self._lock:
                            self._stats["error_count"] += 1
                        logger.warning(f"选课失败：{current.course_name}")

                # 所有目标课程都已选完则退出
                if all_done:
                    logger.info("所有目标课程已选课成功，结束抢课")
                    break

                # 随机间隔
                sleep_time = random.uniform(*self.config.POLL_INTERVAL)
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("用户手动停止抢课")
        finally:
            self._print_summary()
            self.auth.logout()

    # ── 交互式课程展示与选择 ─────────────────────────────────────────────

    def _display_course_table(self, courses: List[CourseInfo]) -> None:
        """以清晰表格格式展示课程列表（统一排版方案）"""
        _COL_DEFS = [
            ("序号", 5, "right"),
            ("JxbBh", 12, "left"),
            ("课程名称", 36, "left"),
            ("教师", 8, "left"),
            ("学分", 4, "right"),
            ("已选/容量", 10, "right"),
            ("状态", 4, "center"),
            ("上课时间", 20, "left"),
        ]
        _TABLE_WIDTH = sum(w for _, w, _ in _COL_DEFS) + len(_COL_DEFS) + 1

        def _build_row(cols, row_type="data"):
            parts = []
            for i, col_def in enumerate(_COL_DEFS):
                _, w, align = col_def
                if row_type == "header":
                    parts.append(pad_str(col_def[0], w, "center"))
                elif row_type == "sep":
                    parts.append("━" * w)
                else:
                    parts.append(pad_str(str(cols[i]), w, align))
            return " " + " ".join(parts)

        print("")
        print("━" * _TABLE_WIDTH)
        print(f"  可选课程列表（共 {len(courses)} 门，按剩余名额降序）")
        print("━" * _TABLE_WIDTH)
        print(_build_row([cd[0] for cd in _COL_DEFS], "header"))
        print(_build_row([], "sep"))
        for i, c in enumerate(courses, 1):
            status = "满" if c.is_full else "可"
            cap_str = f"{c.enrolled}/{c.capacity}"
            row = [i, c.jxb_bh, c.course_name, c.teacher, c.credit,
                   cap_str, status, c.schedule]
            print(_build_row(row, "data"))
        print("━" * _TABLE_WIDTH)

    def _interactive_select_courses(self, courses: List[CourseInfo]) -> List[CourseInfo]:
        """
        展示课程列表，让用户交互式选择目标课程
        支持: 输入序号（1,3,5）、序号范围（1-10）、JxbBh 编号、关键词
        """
        self._display_course_table(courses)

        # 建立序号映射
        seq_map = {i + 1: c for i, c in enumerate(courses)}

        print("\n请选择目标课程（支持以下方式，多个用逗号分隔）：")
        print("  · 输入序号: 1,3,5 或 1-5")
        print("  · 输入 JxbBh 编号（精确匹配）")
        print("  · 输入课程名称关键词（模糊匹配）")
        print("  · 输入 'all' 选择全部    输入 'q' 退出")
        user_input = input("\n> ").strip()
        if not user_input or user_input.lower() in ("q", "quit", "exit"):
            return []

        if user_input.lower() == "all":
            logger.info(f"已选择全部 {len(courses)} 门课程")
            return list(courses)

        selected = []
        matched_ids = set()
        # 按逗号分隔各选项
        parts = [p.strip() for p in user_input.replace("，", ",").split(",") if p.strip()]

        for part in parts:
            # 序号范围: 1-10
            range_match = re.match(r'^(\d+)\s*-\s*(\d+)$', part)
            if range_match:
                lo, hi = int(range_match.group(1)), int(range_match.group(2))
                for seq in range(lo, hi + 1):
                    if seq in seq_map and seq_map[seq].jxb_bh not in matched_ids:
                        selected.append(seq_map[seq])
                        matched_ids.add(seq_map[seq].jxb_bh)
                continue

            # 纯数字: 序号
            if part.isdigit():
                seq = int(part)
                if seq in seq_map and seq_map[seq].jxb_bh not in matched_ids:
                    selected.append(seq_map[seq])
                    matched_ids.add(seq_map[seq].jxb_bh)
                    continue

            # JxbBh 精确匹配
            found = False
            for c in courses:
                if c.jxb_bh == part and c.jxb_bh not in matched_ids:
                    selected.append(c)
                    matched_ids.add(c.jxb_bh)
                    found = True
                    break
            if found:
                continue

            # 关键词模糊匹配
            for c in courses:
                if part in c.course_name and c.jxb_bh not in matched_ids:
                    selected.append(c)
                    matched_ids.add(c.jxb_bh)

        # 去重
        seen = set()
        unique_selected = []
        for c in selected:
            if c.jxb_bh not in seen:
                seen.add(c.jxb_bh)
                unique_selected.append(c)

        if not unique_selected:
            logger.warning("未匹配到任何课程，请重新输入")
            return self._interactive_select_courses(courses)

        logger.info(f"已选择 {len(unique_selected)} 门目标课程:")
        for c in unique_selected:
            logger.info(f"  · [{c.jxb_bh}] {c.course_name} - {c.teacher} "
                        f"(剩余 {c.remain}/{c.capacity})")
        return unique_selected

    def _check_session(self) -> bool:
        """检查会话，如果过期则尝试重新登录"""
        # GetKcInfo 连续返回 HTML（登录页）→ 会话已过期，触发重登
        if self.query._consecutive_html >= 2:
            logger.warning(
                f"检测到会话过期（连续 {self.query._consecutive_html} 次返回登录页），"
                "正在重新登录 ..."
            )
            if not self.auth.login():
                logger.error("重新登录失败，停止抢课")
                return False
            self.query._consecutive_html = 0
            logger.info(f"重新登录成功，新 session_guid: {self.auth.session_guid}")
            return True

        # 原有的 keep_alive 轻量检测
        if not self.auth.keep_alive():
            logger.warning("会话已过期，尝试重新登录 ...")
            if not self.auth.login():
                logger.error("重新登录失败，停止抢课")
                return False
            logger.info("重新登录成功，继续抢课")
        return True

    def _is_course_done(self, course: CourseInfo) -> bool:
        """检查课程是否已被当前会话选上（避免重复提交）"""
        for s in self.selector.success_log:
            if s["course_id"] == course.course_id:
                return True
        return False

    def _print_summary(self):
        """输出抢课结果汇总"""
        logger.info("")
        logger.info("=" * 55)
        logger.info("  抢课任务结束")
        logger.info(f"  运行时长: {time.time() - self._start_time:.1f} 秒")
        logger.info(f"  总计尝试: {self._stats['total_attempts']} 次")
        logger.info(f"  成功选课: {self._stats['success_count']} 门")
        logger.info(f"  课程已满: {self._stats['full_count']} 次")
        logger.info(f"  其他失败: {self._stats['error_count']} 次")
        logger.info("=" * 55)

        if self.selector.success_log:
            logger.info("  选课成功明细:")
            for rec in self.selector.success_log:
                note = f" [{rec.get('note', '')}]" if rec.get('note') else ""
                logger.info(f"    · {rec['course_name']} ({rec['course_id']}) "
                            f"– {rec.get('teacher', '未知教师')} {note}")
            logger.info("")
            logger.info("  请登录教务系统核对课表确认选课结果")
        else:
            logger.info("  未成功选到任何课程，请尝试手动选课")
        logger.info("=" * 55)


# =============================================================================
# 第八部分：主入口 & 配置示例
# =============================================================================

def _input_password(mode: str = "plain") -> str:
    """
    密码输入辅助函数，根据 mode 选择输入方式。

    Args:
        mode: "plain" — 明文 input（默认，输入内容可见，方便确认）
              "hidden" — getpass 隐式输入（无回显，适合公共场合）
              "auto" — 优先尝试 getpass，失败则回退 plain

    Returns:
        用户输入的密码字符串
    """
    if mode == "plain":
        print("  [明文输入] 密码将明文显示，请确保周围环境安全")
        return input("  请输入密码: ").strip()
    elif mode == "hidden":
        import getpass
        try:
            return getpass.getpass("  请输入密码: ").strip()
        except (Exception, KeyboardInterrupt) as e:
            logger.warning(f"getpass 输入失败 ({e})，降级为明文输入")
            return input("  请输入密码 (明文): ").strip()
    elif mode == "auto":
        import getpass
        try:
            return getpass.getpass("  请输入密码: ").strip()
        except (Exception, KeyboardInterrupt):
            logger.warning("getpass 不可用，降级为明文输入")
            return input("  请输入密码 (明文): ").strip()
    else:
        logger.warning(f"未知密码模式 '{mode}'，使用明文输入")
        return input("  请输入密码: ").strip()

def get_default_year_term(year: Optional[int] = None, month: Optional[int] = None) -> str:
    """
    根据当前日期自动推断最新学年学期。
    月份 ≥ 7 为第一学期（秋季），< 7 为第二学期（春季）。
    示例: 2026年7月 → "2026-2027-1"
    """
    now = datetime.now()
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    if m >= 7:
        return f"{y}-{y + 1}-1"
    else:
        return f"{y - 1}-{y}-2"


def interactive_setup() -> GrabberConfig:
    """
    交互式配置引导
    让用户输入学号密码，学年学期自动推断
    """
    config = GrabberConfig()

    print("\n===== 江西农业大学 · 抢课脚本 配置向导 =====")

    stu_id = os.getenv("JXAU_STU_ID", "")
    if not stu_id:
        stu_id = input("请输入学号: ").strip()
    config.STUDENT_ID = stu_id

    password = os.getenv("JXAU_PASS", "")
    if not password:
        password = _input_password(config.PASSWORD_MODE)
    config.PASSWORD = password

    default_year_term = get_default_year_term()
    year_term = input(f"请输入学年学期 (回车默认 {default_year_term}): ").strip()
    config.YEAR_TERM = year_term or default_year_term

    base_url = input(f"教务系统地址 (回车默认 {config.BASE_URL}): ").strip()
    if base_url:
        config.BASE_URL = base_url.rstrip("/")
        config.__post_init__()

    print("配置完成！开始登录并查询课程 ...\n")
    return config


def main():
    """主函数"""
    print("")
    print("╔══════════════════════════════════════════════════╗")
    print("║   江西农业大学 教务系统抢课脚本（学习研究版）    ║")
    print("║                                                  ║")
    print("║  作者: 许立鑫    侵权联系: xlx20050131 (QQ星)          ║")
    print("║  免责声明: 本脚本仅用于学习研究，禁止非法用途    ║")
    print("╚══════════════════════════════════════════════════╝")
    print("")

    # 判断运行模式: 由环境变量判定是否走非交互模式
    if os.getenv("JXAU_STU_ID") and os.getenv("JXAU_PASS"):
        config = GrabberConfig()
        if not config.YEAR_TERM:
            config.YEAR_TERM = get_default_year_term()
    else:
        config = interactive_setup()

    # 启动抢课引擎（登录→查询→选课流程全部在 run() 内完成）
    engine = GrabberEngine(config)
    engine.run()


if __name__ == '__main__':
    main()


def config_demo():
    """
    配置文件示例（非交互方式）
    使用方法：
        1. 复制本函数内容，创建一个 config_demo.py
        2. 修改其中的配置项
        3. 运行 python jxau_course_grabber.py
    """
    config = GrabberConfig(
        BASE_URL="https://jwgl.jxau.edu.cn",
        STUDENT_ID="2024xxxxxx",          # 替换为真实学号
        PASSWORD="your_password",          # 替换为真实密码
        YEAR_TERM=get_default_year_term(),
    )
    engine = GrabberEngine(config)
    engine.run()


if __name__ == '__main__':
    main()
