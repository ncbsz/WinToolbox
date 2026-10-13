# -*- coding: utf-8 -*-
"""
WinToolbox — Windows 系统工具箱（桌面版 / PySide6）

无边框圆角窗口 + 自绘标题栏，左侧导航 + 右侧页面，蓝白 Windows 11 Fluent 风格。
所有系统操作直接调用 server.py 里已经验证过的服务层（进程内调用，无 HTTP）。
"""
import json
import os
import queue
import re
import subprocess
import sys
import time
import threading
import weakref
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QPointF, QRect, QFileInfo, QEvent,
    QPropertyAnimation, QVariantAnimation, QEasingCurve, QObject, Property,
    QSize,
)
from PySide6.QtGui import (QPainter, QColor, QPen, QFont, QIcon, QPixmap, QPolygonF,
                           QBrush, QRegion, QPainterPath, QAction, QRadialGradient,
                           QLinearGradient)
from urllib.parse import quote
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QComboBox, QCheckBox, QProgressBar, QScrollArea, QTabWidget,
    QAbstractItemView, QMessageBox, QButtonGroup, QSystemTrayIcon,
    QMenu, QStackedWidget, QFileDialog, QGraphicsDropShadowEffect, QSizePolicy,
    QSlider, QSpinBox, QDialog, QDialogButtonBox, QListWidget, QListWidgetItem,
    QGraphicsOpacityEffect, QInputDialog,
)

import server as S
import theme as T
from ui_widgets import NumberTween, BarTween, Sparkline, HueBar, page_active, restyle, _cls, _set, _rich_text_lines, _rich_text_height, fit_label

APP_NAME = "WinToolbox"
APP_VER = "1.4"
AUTHOR = "xixidan"
GITHUB_URL = "https://github.com/ncbsz/WinToolbox"
IS_ADMIN = S.is_admin()
CAT_NAME = {"system": "系统", "personal": "个性化", "privacy": "隐私和安全性",
            "security": "安全防护", "network": "网络和 Internet",
            "update": "Windows 更新", "gaming": "游戏", "apps": "应用",
            "explorer": "资源管理器", "input": "输入与键盘",
            "performance": "性能优化", "edge": "Edge 浏览器",
            "appperm": "应用权限", "cortana": "Cortana 与搜索"}
CAT_ICON = {"system": "⚙", "personal": "🎨", "privacy": "🛡", "security": "🔒",
            "network": "🌍", "update": "🔄", "gaming": "🎮", "apps": "📦",
            "explorer": "📁", "input": "⌨", "performance": "⚡", "edge": "🌐",
            "appperm": "🔐", "cortana": "🔍"}


# ==========================================================================
# 设置持久化（退出自动保存 / 启动自动恢复 / 手动导入导出）
# ==========================================================================
SETTINGS_FILE = os.path.join(S.APP_DIR, "settings.json")


def settings_load():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def settings_save(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


# ==========================================================================
# 启动模式
# ==========================================================================
# 已移除「管理员/普通模式」选择对话框：现在启动即提权并以管理员模式运行。
# 若需要只读模式排障，可用命令行参数 --normal-mode（内部用）。


# 提权后由子进程带上这个哨兵：万一「runas 报告成功但子进程仍非管理员」（极端
# 环境），没有它就会无限重启自己、疯狂弹 UAC。哨兵保证最多只提权一次。
_ELEVATED_FLAG = "--wtb-elevated"


def choose_mode(argv):
    """恒返回 'admin'：已取消普通/管理员模式选择，启动即提权。

    保留 --normal-mode 作为**内部降级开关**（自动化测试/排障用），
    正常双击启动一律直接走管理员模式，不再弹窗询问。
    """
    if "--normal-mode" in argv:
        return "normal"
    return "admin"


def relaunch_as_admin():
    """以管理员身份重启自己（UAC 由系统弹出）。

    返回 True = 已成功发起提权（调用方应退出当前进程，交给新进程）；
    返回 False = 提权未发起（用户在 UAC 上点了「否」/ 系统拒绝 / 调用异常），
    调用方应**继续以当前权限运行**并降级为普通模式 —— 绝不能静默退出，
    否则用户看到的现象是"双击了但软件打不开"。

    说明：打包版 exe 的 manifest 已声明 requireAdministrator，Windows 会在创建
    进程前就弹 UAC，正常双击根本走不到这里；本函数是**兜底**（源码直跑
    `python app.py`、或 manifest 未生效的场景）。
    """
    import ctypes
    if getattr(sys, "frozen", False):
        exe, extra = sys.executable, []            # 打包版：exe 自己就是入口
    else:
        exe, extra = sys.executable, [os.path.abspath(sys.argv[0])]  # python app.py
    args = [a for a in sys.argv[1:]
            if a not in ("--admin-mode", "--normal-mode", _ELEVATED_FLAG)]
    # 参数逐项加引号，避免路径含空格被拆断；末尾补哨兵（子进程见它不再二次提权）
    parts = " ".join('"%s"' % a if " " in a else a
                     for a in (extra + args + [_ELEVATED_FLAG]))
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, parts, None, 1)
    except Exception as e:
        QMessageBox.critical(None, "提权失败", "无法请求管理员权限：\n%s" % e)
        return False
    # ShellExecuteW 返回值 <=32 表示失败（5 = 用户取消 / 拒绝提权）
    return int(rc) > 32


def exe_path_for_startup():
    """Path used for startup entry. PyInstaller onefile -> sys.executable."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def set_startup(enable):
    """Add/remove HKCU Run key for this executable."""
    try:
        import winreg
        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                  '"%s"' % exe_path_for_startup())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        return False


# ==========================================================================
# 系统信息探测：分辨率 / DPI 缩放 / 系统字号
# ==========================================================================
def probe_system_info(app=None):
    """读取系统缩放相关信息，供启动窗口展示并决定窗口尺寸与字号。

    返回 dict：
      screen_w/screen_h  — 屏幕物理分辨率
      avail_w/avail_h    — 可用工作区（不含任务栏）
      dpi                — 系统 DPI（96 为 100%）
      scale              — 系统缩放倍数，如 1.0 / 1.25 / 1.5
      font_base          — 基准字号 px（跟随系统缩放；额外叠加「文本大小」设置）
      sys_font_pt        — 系统 UI 字体磅值（仅展示用）
    """
    import ctypes
    dpi = 96
    # 首选 GetDpiForSystem（与系统「缩放」设置一致）
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        try:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
        except Exception:
            dpi = 96
    if dpi <= 0:
        dpi = 96

    scale = dpi / 96.0

    # 分辨率（优先用 Qt，逻辑像素 × dpr 得到物理像素）
    sw = sh = aw = ah = 0
    try:
        scr = QApplication.primaryScreen()
        if scr is not None:
            dpr = scr.devicePixelRatio() or scale
            g, a = scr.geometry(), scr.availableGeometry()
            sw, sh = int(g.width() * dpr), int(g.height() * dpr)
            aw, ah = int(a.width() * dpr), int(a.height() * dpr)
    except Exception:
        pass
    if sw <= 0 or sh <= 0:
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            aw, ah = sw, sh
        except Exception:
            sw, sh, aw, ah = 1920, 1080, 1920, 1040

    # 系统 UI 字体磅值（展示用）
    sys_font_pt = 9.0
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\Desktop\WindowMetrics", 0,
                            winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, "CaptionFont")
            sys_font_pt = abs(int(v[0])) / 20.0
    except Exception:
        pass

    # 基准字号：13px。QSS px 是逻辑像素，Qt 渲染时已按系统缩放(dpr)自动放大物理尺寸，
    # 这里**不能**再乘 scale —— 否则高缩放屏上字号双重放大（画面整体偏大的根因之一）
    font_base = 13.0

    return {"screen_w": sw, "screen_h": sh, "avail_w": aw or sw, "avail_h": ah or sh,
            "dpi": dpi, "scale": round(scale, 2),
            "font_base": round(font_base, 2), "sys_font_pt": round(sys_font_pt, 2)}


# ==========================================================================
# 启动加载窗口（与主窗口同尺寸，避免出现后跳变）
# ==========================================================================
class SplashWindow(QWidget):
    """启动时的加载窗口：外观尺寸与主窗口一致，显示系统信息与进度。"""

    def __init__(self, dark=False, scale=1.0, font_base=13.0,
                 size=(1180, 780), screen_size=(1920, 1080)):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("%s 正在启动…" % APP_NAME)
        self.setStyleSheet(T.build_qss(dark, scale, font_base))
        self._dark = dark
        self._size = size
        self._center_on(screen_size)

        # 圆角外壳
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        shell = QFrame(); shell.setObjectName("Shell")
        outer.addWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏（同主窗口高度，保证两窗口视觉一致）
        tb = QWidget(); tb.setObjectName("TitleBar"); tb.setFixedHeight(max(30, int(44 * scale)))
        tl = QHBoxLayout(tb); tl.setContentsMargins(16, 0, 16, 0); tl.setSpacing(8)
        ico = QLabel(); ico.setPixmap(self._make_icon().pixmap(20, 20))
        tl.addWidget(ico)
        ttext = QLabel("%s — 系统工具箱 v%s" % (APP_NAME, APP_VER))
        ttext.setObjectName("TitleText")
        tl.addWidget(ttext); tl.addStretch(1)
        root.addWidget(tb)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(40, 0, 40, 0)
        bv.addStretch(1)

        logo = QLabel("W"); logo.setObjectName("Logo")
        logo.setFixedSize(int(64 * scale), int(64 * scale))
        lg = QHBoxLayout(); lg.addStretch(1); lg.addWidget(logo); lg.addStretch(1)
        logo.setStyleSheet(
            'QLabel#Logo { background: %s; color: #fff; font-size: %dpx; font-weight: 700;'
            ' border-radius: %dpx; }'
            % (T.get_color("BLUE", dark), int(34 * scale), int(14 * scale)))
        bv.addLayout(lg)
        bv.addSpacing(int(16 * scale))

        name = QLabel(APP_NAME); name.setObjectName("PageTitle")
        name.setAlignment(Qt.AlignCenter)
        ng = QHBoxLayout(); ng.addStretch(1); ng.addWidget(name); ng.addStretch(1)
        bv.addLayout(ng)
        sub = QLabel("系统工具箱 v%s" % APP_VER); sub.setObjectName("Muted3")
        sub.setAlignment(Qt.AlignCenter)
        sg = QHBoxLayout(); sg.addStretch(1); sg.addWidget(sub); sg.addStretch(1)
        bv.addLayout(sg)
        bv.addSpacing(int(26 * scale))

        # 系统信息行
        self.info_lb = QLabel(self._sys_text(screen_size))
        self.info_lb.setObjectName("Muted")
        self.info_lb.setAlignment(Qt.AlignCenter)
        self.info_lb.setWordWrap(True)
        bv.addWidget(self.info_lb)
        bv.addSpacing(int(18 * scale))

        # 进度条
        bar_wrap = QWidget()
        bw = QVBoxLayout(bar_wrap); bw.setContentsMargins(0, 0, 0, 0)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100); self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(max(6, int(6 * scale)))
        bw.addWidget(self.bar)
        wrap_h = QHBoxLayout(); wrap_h.addStretch(1)
        wrap_h.addWidget(bar_wrap, 6); wrap_h.addStretch(1)
        bv.addLayout(wrap_h)
        bv.addSpacing(int(10 * scale))

        self.step_lb = QLabel("正在读取系统信息…")
        self.step_lb.setObjectName("Muted3")
        self.step_lb.setAlignment(Qt.AlignCenter)
        bv.addWidget(self.step_lb)

        bv.addStretch(1)
        root.addWidget(body, 1)

    # ---- helpers ----
    def _make_icon(self):
        pm = QPixmap(64, 64); pm.fill(Qt.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(QColor(T.get_color("BLUE", self._dark)))
        p.drawRoundedRect(2, 2, 60, 60, 14, 14)
        f = QFont("Segoe UI Variable Display", 34); f.setBold(True)
        p.setFont(f); p.setPen(QColor("white"))
        p.drawText(pm.rect(), Qt.AlignCenter, "W")
        p.end()
        return QIcon(pm)

    def _sys_text(self, screen_size):
        return ("正在检测显示器与缩放设置…"
                if not screen_size else
                "分辨率 %d × %d" % (screen_size[0], screen_size[1]))

    def _center_on(self, screen_size):
        """把启动窗口居中到主屏（两边都按**逻辑像素**算）。

        `screen_size` 早先传进来的是物理像素（`geometry()*dpr`），而窗口尺寸是逻辑
        像素 —— 150% 缩放下两者不是一个坐标系，窗口会被推到右下甚至跑出屏幕；
        100% 缩放时凑巧正确，所以只在换屏幕/改缩放后才暴露。
        """
        w, h = self._size
        try:
            g = QApplication.primaryScreen().availableGeometry()
            sw, sh = g.width(), g.height()
        except Exception:
            sw, sh = screen_size
        self.setGeometry(max(0, (sw - w) // 2), max(0, (sh - h) // 2), w, h)

    def clip_round(self):
        path = QPainterPath()
        path.addRoundedRect(QRect(0, 0, self.width(), self.height()), 12, 12)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, e):
        self.clip_round()
        super().resizeEvent(e)

    # ---- 由主线程按步推进 ----
    def step(self, pct, text, sysinfo=None):
        self.bar.setValue(int(max(0, min(100, pct))))
        if text:
            self.step_lb.setText(text)
        if sysinfo:
            self.info_lb.setText(
                "分辨率 %d × %d　·　系统缩放 %d%%　·　DPI %d"
                % (sysinfo["screen_w"], sysinfo["screen_h"],
                   round(sysinfo["scale"] * 100), sysinfo["dpi"]))
        QApplication.processEvents()


# ==========================================================================
# 异步任务：把耗时的系统调用挪出 UI 线程
# ==========================================================================
class Task(QThread):
    done = Signal(object)
    fail = Signal(str)

    def __init__(self, fn, *a, **kw):
        super().__init__()
        self.fn, self.a, self.kw = fn, a, kw

    def run(self):
        try:
            self.done.emit(self.fn(*self.a, **self.kw))
        except Exception as e:
            self.fail.emit("%s: %s" % (type(e).__name__, e))


class ScanCtl:
    """扫描按钮的「开始 / 暂停 / 继续」三态控制器。

    这类扫描动辄十几秒到一分钟，点了以后既看不到进度也没法停。统一成三态后
    随时能停。

    暂停的语义**如实区分**，不含糊：
      · `resumable=True`（目前只有整盘占用扫描）：真接着扫，不重复累加；
      · `resumable=False`：停住并**保留已扫到的结果**，「继续」会**重新扫一遍**
        （tooltip 里写明）。

    扫描器要配合就用 `ctl.stop_flag()`（返回一个函数，回 True 表示该收工）；
    不支持协作中断的扫描器，点了暂停也会很快自己跑完 —— 那时 `finish()` 收到
    `stopped=False` 会回到「开始扫描」，而不是骗你说还在暂停中。
    """

    def __init__(self, btn, on_start, start_text="开始扫描", resumable=False):
        self.btn = btn
        self.on_start = on_start
        self.start_text = start_text
        self.resumable = resumable
        self.state = "idle"                  # idle / running / pausing / paused
        self._stop = threading.Event()
        btn.clicked.connect(self.clicked)

    def clicked(self):
        if self.state == "idle":
            self.start()
        elif self.state == "running":
            self.pause()
        elif self.state == "paused":
            self.start(resume=True)
        # pausing：等后台线程真的收工，忽略重复点击

    def start(self, resume=False):
        self._stop.clear()
        self.state = "running"
        self._paint()
        try:
            self.on_start(resume)
        except TypeError:
            self.on_start()

    def stop_flag(self):
        """给扫描器当 `should_stop` 用：**调用它**返回 True 表示该收工。

        ⚠️ 必须写成 `return self._stop.is_set()` —— 早先写成 `return
        self._stop.is_set`（漏了括号），返回的是方法对象（永远为真），
        于是扫描一启动就自己停了，结果一条都扫不出来。
        """
        return self._stop.is_set()

    def pause(self):
        if self.state != "running":
            return
        self._stop.set()
        self.state = "pausing"
        self.btn.setText("正在暂停…")
        self.btn.setEnabled(False)

    def finish(self, stopped=False):
        """扫描线程结束。`stopped=True` 表示扫描器确认是被暂停中断的。"""
        self._stop.clear()
        self.state = "paused" if stopped else "idle"
        self.btn.setEnabled(True)
        self._paint()

    def _paint(self):
        if self.state == "running":
            self.btn.setText("暂停扫描")
            self.btn.setToolTip("点一下暂停（已经扫到的结果会保留）")
        elif self.state == "paused":
            self.btn.setText("继续扫描")
            self.btn.setToolTip("接着扫（不重复累加）" if self.resumable
                                else "这个扫描不支持从断点续扫，会重新扫一遍")
        elif self.state == "pausing":
            self.btn.setToolTip("正在等后台收工…")
        else:
            self.btn.setText(self.start_text)
            self.btn.setToolTip("")


class Tasks:
    _live = []

    @staticmethod
    def run(parent, fn, on_done, on_fail=None, ctl=None, *a, **kw):
        """跑一个后台任务。`ctl` 传 ScanCtl 时，任务一结束就自动复位按钮状态
        （`stopped=True` 表示扫描器确认是被暂停中断的 → 按钮留在「继续扫描」）。"""
        t = Task(fn, *a, **kw)
        Tasks._live.append(t)

        def _done(r):
            try:
                on_done(r)
            except RuntimeError:
                pass  # 窗口已销毁，积压回调打在已删控件上——静默丢弃
            finally:
                if ctl is not None:
                    try:
                        ctl.finish(stopped=bool(isinstance(r, dict)
                                                and r.get("stopped")))
                    except RuntimeError:
                        pass
                Tasks._retire(t)

        def _fail(m):
            try:
                if on_fail:
                    on_fail(m)
                else:
                    warn(parent, m)
            except RuntimeError:
                pass  # 同上
            finally:
                if ctl is not None:
                    try:
                        ctl.finish()
                    except RuntimeError:
                        pass
                Tasks._retire(t)

        t.done.connect(_done)
        t.fail.connect(_fail)
        t.start()
        return t

    @staticmethod
    def _retire(t):
        """任务回调收尾：把「线程已结束」钉死，再让它可被销毁。

        ⚠️ `done` / `fail` 是**在 run() 内部** emit 的，此刻线程还没退出；而
        QThread 的 C++ 对象会在 Python 包装被回收时销毁 —— 若那一瞬间线程仍在
        跑，就是 Qt 点名的 UB（"QThread: Destroyed while thread is still
        running"），轻则告警、重则踩坏堆。这里 `wait()` 等它真的退出（`fn` 早已
        返回，通常毫秒级），`deleteLater()` 让对象在真实事件循环里随即释放。
        ⚠️ 测试 harness 全程 processEvents 驱动，deferred delete 不执行 —— 那只
        多留一个**已退出**的线程对象，无害。
        """
        if t in Tasks._live:
            Tasks._live.remove(t)
        try:
            t.wait(3000)
        except Exception:
            pass
        try:
            t.deleteLater()
        except Exception:
            pass

    @staticmethod
    def run_cached(parent, key, fn, on_done, on_fail=None, *a, **kw):
        '''会话内只读一次：同一个 key 第二次直接复用上次结果。

        数据在本次运行里只读一遍 —— 切页、切分类、重新排序都不会再碰系统
        （老机器上那批 PowerShell / WMI 子进程就是这么攒起来的）。要让数据
        更新，走 page.reload()：手动刷新或写操作之后由它清缓存重读。
        '''
        box = getattr(parent, '_sess', None)
        if box is not None and key in box:
            try:
                on_done(box[key])    # 命中缓存：主线程同步回调，界面不闪加载态
            except RuntimeError:
                pass
            return None

        def _finish(r):
            if box is not None:
                box[key] = r
            on_done(r)
        return Tasks.run(parent, fn, _finish, on_fail, *a, **kw)


class ProgressSpin:
    """把**后台线程**写的进度文本打到主窗口 spinner 上。

    ⚠️ 不让 worker 直接碰 Qt —— 跨线程动控件是最隐蔽的一类崩溃。
    做法：worker 只往一个 dict 写字符串（GIL 下单值读写安全），
    主线程用 120ms 的 QTimer 读出来显示（`busy()` 只改一个 QLabel）。
    定时器随操作结束立刻 stop，不常驻。
    """

    def __init__(self, win, state, initial="处理中…"):
        self.win, self.state = win, state
        # ⚠️ 初始文案也要写进 state —— 否则 120ms 后的第一次 tick 会拿空 state
        #    把用户刚看到的「正在关闭 Defender…」覆盖成光秃秃的「处理中…」。
        state.setdefault("msg", initial)
        self._t = QTimer(win)
        self._t.setInterval(120)
        self._t.timeout.connect(self._tick)
        try:
            win.busy(initial)
        except RuntimeError:
            pass
        self._t.start()

    def _tick(self):
        try:
            self.win.busy(self.state.get("msg") or "处理中…")
        except RuntimeError:
            self.stop()          # 窗口已销毁

    def stop(self):
        try:
            self._t.stop()
            self.win.idle()
        except RuntimeError:
            pass


# ==========================================================================
# 小工具
# ==========================================================================
# ==========================================================================
# 轻量动效：数值补间 / 进度条补间 / 入场淡入
#
# 只动「数值」与「透明度」—— 这两类属性不触发 relayout。每秒刷新的面板里，
# 任何引起重新布局的动画都会掉帧，流畅感的来源是数值平滑，而不是元素乱飞。
# ==========================================================================


def fade_in(widget, delay_ms=0, dur=320):
    """入场淡入。错峰调用即得到一次编排好的入场序列。

    动画结束会摘掉 opacity effect —— 常驻 effect 会让子控件走离屏合成，
    文字抗锯齿变糊，且每秒刷新的面板会白白多一层合成开销。
    """
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", widget)
    a.setDuration(dur)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve.OutCubic)
    a.finished.connect(lambda w=widget: w.setGraphicsEffect(None))
    if delay_ms:
        QTimer.singleShot(delay_ms, a.start)
    else:
        a.start()
    widget._fade_anim = a          # 保引用，否则动画被 GC 会中断
    return a


def fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def fmt_uptime(sec):
    # 读不到（None）/ 负数一律显示「—」，不要让一个字段把整张卡片带崩
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "—"
    if sec < 0:
        return "—"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return ("%d 天 " % d if d else "") + "%d 小时 %d 分" % (h, m)


def fmt_temp(v):
    return ("%.0f°C" % v) if v is not None else "不支持"


def fmt_ghz(mhz):
    """MHz → '3.10 GHz'（不足 1000 时按 MHz 显示，读不到时 '—'）。"""
    try:
        mhz = float(mhz)
    except (TypeError, ValueError):
        return "—"
    if mhz <= 0:
        return "—"
    return ("%.2f GHz" % (mhz / 1000.0)) if mhz >= 1000 else ("%.0f MHz" % mhz)


# ---------------- 硬件名简称（主指标条一行放得下的具体信息） ----------------
def short_cpu_name(n):
    """'12th Gen Intel(R) Core(TM) i5-12500H' → 'i5-12500H'。

    保留有辨识力的型号段，去掉代际/厂商/Core 等前缀与 'CPU @ 2.50GHz' 尾巴。
    """
    s = re.sub(r"\((?:R|TM|C)\)", "", n or "")
    s = re.sub(r"^\s*\d+(?:th|st|nd|rd)\s+Gen\s+", "", s, flags=re.I)
    s = re.sub(r"^(?:Intel|AMD)\s+", "", s, flags=re.I)
    s = re.sub(r"^Core\s+", "", s, flags=re.I)
    s = re.sub(r"\s+CPU\s*@.*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= 26 else s[:25] + "…"


def short_hw_name(n, limit=24):
    """硬件名通用简称：去 (R)/(TM)、压空格，过长截断（显卡 / 网卡共用）。"""
    s = re.sub(r"\((?:R|TM|C)\)", "", n or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def gpu_short(n):
    """显卡简称：去掉 NVIDIA GeForce / AMD Radeon 这类厂商前缀。

    指标条那一行窄，「RTX 3050 Laptop」比「NVIDIA GeForce RTX 3050 Laptop」好读；
    全名在悬停提示里。
    """
    s = (n or "").strip()
    for pre in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "AMD ", "Intel(R) ",
                "Intel "):
        if s.startswith(pre):
            s = s[len(pre):].strip()
            break
    return short_hw_name(s)


def is_dgpu(n):
    """名称启发式判断独显：N 卡全是独显；Intel 只有 Arc 是独显；
    AMD 看 RX / 数字型号（APU 核显叫「Radeon 680M」/「Radeon(TM) Graphics」）。"""
    s = (n or "").lower()
    if "nvidia" in s or "geforce" in s or "quadro" in s:
        return True
    if "intel" in s:
        return "arc" in s
    if "radeon" in s or "amd" in s:
        return bool(re.search(r"\b(rx|vega|vii|fury)\b", s)
                    or re.search(r"\bhd\s*\d{4}\b", s)
                    or re.search(r"\br[5-9]\s*\d{3}", s))
    return False


def fmt_mbps(v):
    """读写 / 收发速率：MB/s。"""
    if v is None:
        return "—"
    if v < 0.01:
        return "0.0 MB/s"
    if v >= 1024:
        return "%.2f GB/s" % (v / 1024.0)
    return "%.1f MB/s" % v


def fmt_rate(v):
    """网络速率：Mbps（性能计数器原生的比特速率单位）。"""
    if v is None:
        return "—"
    v = float(v)
    if v < 0.01:
        return "0.00 Mbps"
    if v >= 1000:
        return "%.2f Gbps" % (v / 1000.0)
    if v >= 10:
        return "%.1f Mbps" % v
    return "%.2f Mbps" % v


def fmt_linkspeed(mbps):
    """网卡协商速率：1000 Mbps -> 1.0 Gbps。"""
    try:
        v = float(mbps)
    except Exception:
        return "未知"
    if v <= 0:
        return "未知"
    if v >= 1000:
        return "%.1f Gbps" % (v / 1000.0)
    return "%.0f Mbps" % v


def fmt_pct(v):
    return ("%.0f%%" % v) if v is not None else "—"


def _msg_pair(a, b, default_title):
    """兼容两种调用写法，返回 (标题, 正文)。

        info(parent, "正文")             —— 只有正文，标题用默认值
        info(parent, "标题", "正文")     —— 第 2 个参数是标题

    历史上这里定义成 `info(parent, msg, title)`，但所有三参数调用点写的都是
    `(标题, 正文)`，于是「标题栏显示正文、正文显示标题」—— 检查更新失败时
    把 `ImportError: DLL load failed ...` 顶到标题栏上就是这么来的。
    """
    if b is None:
        return default_title, a
    return a, b


def warn(parent, a, b=None):
    title, text = _msg_pair(a, b, "操作失败")
    QMessageBox.warning(parent, str(title), str(text))


def info(parent, a, b=None):
    title, text = _msg_pair(a, b, "提示")
    QMessageBox.information(parent, str(title), str(text))


def _friendly_net_err(m):
    """把底层异常翻译成一句人话（打包版少东西时尤其需要）。"""
    s = str(m)
    if "DLL load failed" in s or "_ssl" in s:
        return ("程序缺少 HTTPS 组件（%s）。\n\n"
                "打包版出现这条，通常是构建时误删了 OpenSSL 动态库；"
                "请换用最新版 exe，或直接用源码运行。" % s)
    return s


def confirm(parent, title, text, danger=False):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.button(QMessageBox.Yes).setText("确定")
    box.button(QMessageBox.No).setText("取消")
    _center_dialog(box)
    ok = box.exec() == QMessageBox.Yes
    # 用完即走：QMessageBox 带 parent，关掉后只是隐藏，C++ 对象会一直挂在
    # 页面子树里，之后每次 setStyleSheet 重抛光整棵树都会扫到它（实测与
    # 「累积状态 + 主题色连切」组合会崩在 setStyleSheet 里，0xC0000005）。
    box.deleteLater()
    return ok


def confirm_ex(parent, title, text, backup_label=None, backup_default=True,
               backup_note="", danger=True):
    """带「是否备份」勾选的确认框。返回 **(是否继续, 是否备份)**。

    为什么要有这个：清理动作以前是**静默决定**处置方式（有的直接删、有的送回收站），
    用户看不到也选不了。这里把处置方式摆到确认框里让用户当场选。

    `backup_label=None` 时不画勾选框，退回普通 `confirm`。
    ⚠️ 传了 `backup_label` 时**不走** `A.confirm`，会创建真实 QMessageBox ——
    无人值守的测试要么桩 `A.QMessageBox.exec`，要么直接桩 `A.confirm_ex`
    （regress_all 用后者；只桩前者会让从未显示的对话框留在控件树里）。
    """
    if backup_label is None:
        return bool(confirm(parent, title, text, danger=danger)), False
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    body = text + (("\n\n" + backup_note) if backup_note else "")
    box.setText(body)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.button(QMessageBox.Yes).setText("确定")
    box.button(QMessageBox.No).setText("取消")
    cb = QCheckBox(backup_label)
    cb.setChecked(bool(backup_default))
    box.setCheckBox(cb)
    _center_dialog(box)
    ok = box.exec() == QMessageBox.Yes
    # `setCheckBox` 在某些平台/样式下可能没生成勾选框（返回 None），
    # 这时退回 backup_default，别让调用方拿到 None 当布尔用。
    try:
        checked = bool(cb.isChecked())
    except Exception:
        checked = bool(backup_default)
    # 同 confirm()：不把隐藏的模态框留在页面子树里（重抛光整棵树会扫到它）
    box.deleteLater()
    return ok, checked


def _center_dialog(dlg):
    """把对话框移到屏幕正中（多屏时用所在屏幕的可视区域）。"""
    try:
        scr = dlg.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        dlg.adjustSize()
        fg = dlg.frameGeometry()
        fg.moveCenter(scr.availableGeometry().center())
        dlg.move(fg.topLeft())
    except Exception:
        pass


class SafetyConfirmDialog(QDialog):
    """修改系统设置前的安全提示（屏幕正中，列出将被改动的项）。

    比 QMessageBox 更醒目：标题、风险说明、逐条列出待改动项，
    并要求用户**主动勾选**「我已了解」后才允许确认，避免误触直接改注册表。
    """

    def __init__(self, parent, dark, title, summary, items,
                 risky=0, warn_text=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("SafetyDialog")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self._ok = False

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 18)
        v.setSpacing(12)

        head = QHBoxLayout(); head.setSpacing(10)
        icon = QLabel("⚠")
        icon.setObjectName("SdIcon")
        icon.setAlignment(Qt.AlignTop)
        head.addWidget(icon)
        box = QVBoxLayout(); box.setSpacing(4)
        t = QLabel(title)
        t.setObjectName("SdTitle")
        t.setWordWrap(True)
        box.addWidget(t)
        s = QLabel(summary)
        s.setObjectName("SdDesc")
        s.setWordWrap(True)
        box.addWidget(s)
        head.addLayout(box, 1)
        v.addLayout(head)

        if warn_text:
            wn = QLabel(warn_text)
            wn.setObjectName("SdWarn")
            wn.setWordWrap(True)
            v.addWidget(wn)

        # 待改动项清单（限高，超出可滚动）
        if items:
            lb = QLabel("即将修改以下 %d 项：" % len(items))
            lb.setObjectName("SDLabel")
            v.addWidget(lb)
            lst = QListWidget()
            lst.setObjectName("SdList")
            lst.setSelectionMode(QListWidget.NoSelection)
            lst.setFocusPolicy(Qt.NoFocus)
            for it in items:
                lst.addItem(it)
            rows = min(len(items), 7)
            lst.setFixedHeight(min(210, 26 * rows + 12))
            v.addWidget(lst)

        if risky:
            rk = QLabel("其中 %d 项为【策略】项，改后 Windows 会显示“某些设置由你的组织管理”。"
                        % risky)
            rk.setObjectName("SdWarn")
            rk.setWordWrap(True)
            v.addWidget(rk)

        self.cb_ack = QCheckBox("我已了解以上改动，并确认继续")
        self.cb_ack.toggled.connect(self._on_ack)
        v.addWidget(self.cb_ack)

        bb = QDialogButtonBox()
        self.btn_ok = bb.addButton("确认修改", QDialogButtonBox.AcceptRole)
        self.btn_cancel = bb.addButton("取消", QDialogButtonBox.RejectRole)
        self.btn_ok.setObjectName("Primary")
        self.btn_ok.setEnabled(False)          # 必须先勾选「我已了解」
        self.btn_ok.clicked.connect(self._accept)
        self.btn_cancel.clicked.connect(self.reject)
        v.addWidget(bb)

        # 屏幕正中显示
        self._center_on_screen()

    def _on_ack(self, on):
        self.btn_ok.setEnabled(bool(on))

    def _accept(self):
        self._ok = True
        self.accept()

    def _center_on_screen(self):
        """在**屏幕正中**弹出（而不是跟随父窗口偏移）。"""
        _center_dialog(self)

    def ok(self):
        return self._ok


def safety_confirm(parent, dark, title, summary, items, risky=0, warn_text=None):
    """在屏幕正中弹出安全提示；返回 True 表示用户已确认。"""
    dlg = SafetyConfirmDialog(parent, dark, title, summary, items,
                              risky=risky, warn_text=warn_text)
    dlg.exec()
    ok = dlg.ok()
    dlg.deleteLater()      # 同 confirm()：不把隐藏的模态框留在控件树里
    return ok


class ServiceDialog(QDialog):
    """服务详情（只读）+ 常用操作。

    做这个的原因：`services.msc` 不支持「跳到某个服务」，以前只能把它打开再让用户
    自己去列表里翻。这个弹窗直接把注册表里的事实摆出来 —— 显示名 / 当前状态 /
    启动类型 / 登录身份 / 镜像路径 / 依赖关系 / 描述，并附上启停与改启动类型的按钮。
    """

    ACTIONS = (("启动", "start"), ("停止", "stop"), ("改为自动", "auto"),
               ("改为手动", "manual"), ("禁用", "disable"))

    def __init__(self, parent, name, on_action=None):
        super().__init__(parent)
        self.win = parent
        self.name = name
        self.on_action = on_action
        self.detail = {}
        self.setWindowTitle("服务详情 — %s" % name)
        self.setObjectName("SafetyDialog")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 18)
        v.setSpacing(12)

        head = QHBoxLayout(); head.setSpacing(10)
        icon = QLabel("⚙")
        icon.setObjectName("SdIcon")
        icon.setAlignment(Qt.AlignTop)
        head.addWidget(icon)
        box = QVBoxLayout(); box.setSpacing(3)
        self.lb_title = QLabel(name)
        self.lb_title.setObjectName("SdTitle")
        self.lb_title.setWordWrap(True)
        box.addWidget(self.lb_title)
        self.lb_sub = QLabel("读取中…")
        self.lb_sub.setObjectName("SdDesc")
        self.lb_sub.setWordWrap(True)
        box.addWidget(self.lb_sub)
        head.addLayout(box, 1)
        v.addLayout(head)

        self.grid = QGridLayout(); self.grid.setSpacing(6)
        self.grid.setColumnStretch(1, 1)
        v.addLayout(self.grid)

        dl = QLabel("描述")
        dl.setObjectName("SDLabel")
        v.addWidget(dl)
        self.lb_desc = QLabel("—")
        self.lb_desc.setObjectName("Muted")
        self.lb_desc.setWordWrap(True)
        self.lb_desc.setMinimumHeight(50)
        self.lb_desc.setAlignment(Qt.AlignTop)
        self.lb_desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.lb_desc)

        self.lb_warn = QLabel("")
        self.lb_warn.setObjectName("SdWarn")
        self.lb_warn.setWordWrap(True)
        v.addWidget(self.lb_warn)
        v.addStretch(1)

        admin = bool(getattr(parent, "admin_mode", False))
        ab = QHBoxLayout(); ab.setSpacing(6)
        for label, act in self.ACTIONS:
            b = btn(label, "Danger" if act == "disable" else "",
                    on_click=lambda _c=False, a=act: self._do(a))
            b.setEnabled(admin)
            if not admin:
                b.setToolTip("当前不是管理员模式，无法修改服务")
            ab.addWidget(b)
        ab.addStretch(1)
        v.addLayout(ab)

        bb = QDialogButtonBox()
        b_loc = bb.addButton("打开文件所在位置", QDialogButtonBox.ActionRole)
        b_cp = bb.addButton("复制服务名", QDialogButtonBox.ActionRole)
        b_msc = bb.addButton("在服务管理器中打开", QDialogButtonBox.ActionRole)
        b_close = bb.addButton("关闭", QDialogButtonBox.RejectRole)
        b_close.setObjectName("Primary")
        b_loc.clicked.connect(self._open_location)
        b_cp.clicked.connect(lambda: copy_text(self.name))
        b_msc.clicked.connect(self._open_msc)
        b_close.clicked.connect(self.reject)
        v.addWidget(bb)

        _center_dialog(self)
        self.reload()

    # ---------- 数据 ----------
    def reload(self):
        """重新读一遍详情（改完启停状态后也调这里）。"""
        d = S.service_detail(self.name) or {}
        self.detail = d
        if not d:
            self.lb_sub.setText("读不到这个服务的信息（可能已被删除或改名）。")
            return
        st = d.get("state_text") or "未知"
        self.lb_title.setText(d.get("display_text") or self.name)
        self.lb_sub.setText("服务名 %s ｜ 当前 %s ｜ 启动类型 %s"
                            % (self.name, st, d.get("start_text") or "?"))
        rows = (("服务名", self.name),
                ("显示名", d.get("display_text") or "—"),
                ("当前状态", st),
                ("启动类型", d.get("start_text") or "—"),
                ("运行方式", d.get("type_text") or "—"),
                ("登录身份", d.get("account") or "—"),
                ("可执行文件", d.get("img") or "—"),
                ("依赖的服务", "、".join(d.get("depend") or []) or "无"),
                ("被这些服务依赖", "、".join(d.get("dependents") or []) or "无"))
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for i, (k, val) in enumerate(rows):
            kl = QLabel(k); kl.setObjectName("KVKey")
            kl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            vl = QLabel(str(val)); vl.setObjectName("KVVal")
            vl.setWordWrap(True)
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.grid.addWidget(kl, i, 0)
            self.grid.addWidget(vl, i, 1)
        self.lb_desc.setText(d.get("desc_text") or "（该系统未提供描述）")
        n_dep = len(d.get("dependents") or [])
        warn_txt = ("停止或禁用前留意：有 %d 个服务依赖它，停掉可能连带影响这些功能。" % n_dep
                    if n_dep else "")
        self.lb_warn.setText(warn_txt)
        # SdWarn 自带底色与边框，空着不隐藏会留下一个空框
        self.lb_warn.setVisible(bool(warn_txt))

    # ---------- 操作 ----------
    def _do(self, action):
        if self.on_action is None:
            return
        self.on_action(action, self.name)
        # 用 parent 为 self 的定时器：对话框先被关掉时定时器会随之销毁，
        # 不会在 1.2 秒后回调到一个已删除的对象上。
        QTimer.singleShot(1200, self, self.reload)

    def _open_location(self):
        exe = exe_from_cmd(self.detail.get("img") or "")
        if not exe or not os.path.exists(exe):
            return info(self, "找不到该文件",
                        self.detail.get("img") or "（这个服务没有可定位的程序路径）")
        if not reveal_in_explorer(exe):
            warn(self, "无法在资源管理器中定位：\n%s" % exe)

    def _open_msc(self):
        copy_text(self.name)
        if not open_with_default("services.msc"):
            return warn(self, "打开服务管理器失败", "服务名已复制到剪贴板：%s" % self.name)
        info(self, "已打开服务管理器",
             "服务名「%s」已复制到剪贴板。services.msc 不支持跳到指定服务，"
             "粘贴（Ctrl+V）到列表里按首字母即可定位。" % self.name)


def card(title=None, right=None):
    f = QFrame()
    f.setObjectName("Card")
    v = QVBoxLayout(f)
    v.setContentsMargins(16, 14, 16, 14)
    v.setSpacing(10)
    if title:
        h = QHBoxLayout()
        h.setSpacing(8)
        lb = QLabel(title)
        lb.setObjectName("CardTitle")
        lb.setWordWrap(True)
        # 标题占满剩余宽度（stretch=1）而不是"贴着 sizeHint 再顶一个弹簧"：
        # 自动换行的 QLabel 的 sizeHint 会按启发式给一个偏窄的宽度，于是
        # 「大小核调度（Intel 混合架构）」这类稍长的标题会莫名折成两行。
        h.addWidget(lb, 1)
        if right is not None:
            h.addWidget(right)
        v.addLayout(h)
    return f, v


# ==========================================================================
# 字体度量校正：QSS 的 font-size 不会刷新 QFontMetrics 的布局缓存，
# 导致 QLabel 的 sizeHint 偏小、文字被裁。应用 QSS 后调用本函数修正。
# ==========================================================================
def fit_labels(root):
    """递归修正 root 下所有 QLabel 的最小高度（wordWrap 用富文本精确测量）。"""
    if root is None:
        return
    for lb in root.findChildren(QLabel):
        try:
            txt = lb.text()
            if not txt:
                continue
            fm = lb.fontMetrics()
            if lb.wordWrap():
                w = lb.width() if lb.width() > 0 else lb.sizeHint().width()
                w = max(40, w)
                need = _rich_text_height(lb, w)
                if need <= 0:
                    need = fm.height() * (txt.count("\n") + 1) + 2
                # 上限保护：避免极端宽度下高度估算过大
                max_h = fm.height() * (txt.count("\n") + 14) + 6
                need = min(need, max_h)
            else:
                need = fm.height() * (txt.count("\n") + 1) + 2
            if lb.minimumHeight() < need:
                lb.setMinimumHeight(need)
            # 单行不换行文本还需保证最小宽度，避免被压扁省略
            if not lb.wordWrap() and "\n" not in txt:
                need_w = fm.horizontalAdvance(txt) + 2
                if lb.minimumWidth() < need_w:
                    lb.setMinimumWidth(need_w)
            lb.updateGeometry()
        except Exception:
            pass


def pill(text, kind="", parent=None):
    lb = QLabel(text, parent)
    lb.setObjectName({"ok": "PillOk", "warn": "PillWarn", "err": "PillErr",
                      "blue": "PillBlue", "cpu": "PillCpu", "mem": "PillMem",
                      "gpu": "PillGpu", "disk": "PillDisk",
                      "net": "PillNet"}.get(kind, "Pill"))
    # pill 内容会动态变化，禁止被压扁导致文字省略号
    lb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return lb


def notice(text, kind="info"):
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setObjectName({"info": "Notice", "warn": "NoticeWarn",
                      "err": "NoticeErr", "ok": "NoticeOk"}.get(kind, "Notice"))
    # ⚠️ QLabel 默认是 **垂直居中**。notice 有 12px 上下 padding，当布局给的
    #    高度略大于文字所需时，文字会被居中 → 上下各多一截留白；不同 notice
    #    行数不同、多出的量也不同，看起来就是「行距/留白不统一」。
    #    固定成「左上」后，文字一律从 padding 之后开始，行距就一致了。
    lb.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    return lb


_GPU_TAG_RE = re.compile(r"<[^>]+>")


def plain_name(text):
    """把用户可能粘进来的富文本标记去掉，只留纯文本名字。

    ⚠️ 输入框旁边的预览是 `lb_preview`，那里用 `<b>…</b>` 做加粗。如果用户
    （或复制粘贴）把预览里的文本原样填回输入框，尖括号会**原样写进注册表**，
    设备管理器 / 任务管理器就会显示 `<b>NVIDIA …</b>` 这种带符号的字面。
    这里统一清洗一次，HTML 实体（&amp; 等）也还原。
    """
    t = _GPU_TAG_RE.sub("", text or "")
    if "&" in t:
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                     ("&#39;", "'"), ("&nbsp;", " "), ("&amp;", "&")):
            t = t.replace(a, b)
    return " ".join(t.split()).strip()


def _profile_reg_count(tree):
    """配置文件案里「注册表状态」有多少个键（含根）。用于给用户报个数。"""
    if not isinstance(tree, dict) or not tree:
        return 0
    n, stack = 0, [tree]
    while stack and n < 100000:
        node = stack.pop()
        n += 1
        stack += [v for v in ((node or {}).get("k") or {}).values()
                  if isinstance(v, dict)]
    return n


def notice_html(text, kind="info", on_link=None):
    """像 `notice()` 一样，但支持**可点击的 `<a href>`**。

    `linkActivated` 自己接，不用 `setOpenExternalLinks` —— 后者走
    QDesktopServices，对 `windowsdefender:` 这类**自定义协议**不一定起得来，
    而且起不来时**没有任何反馈**（按钮点了没反应，用户以为程序坏了）。
    """
    lb = notice(text, kind)
    lb.setTextInteractionFlags(Qt.TextBrowserInteraction)
    lb.setCursor(Qt.PointingHandCursor)
    if on_link is not None:
        lb.linkActivated.connect(on_link)
    return lb


class SortItem(QTableWidgetItem):
    """支持数值排序的单元格：填充时把可解析的数字存进 UserRole+1。

    "12.3 MB"、"30 ms"、"1,024 KB"、"97%"、"2025/6/1" 这类自带单位的值
    都能按数值大小排，而不是按字符串排。
    """

    def __lt__(self, other):
        a = self.data(Qt.UserRole + 1)
        b = other.data(Qt.UserRole + 1)
        if a is not None and b is not None:
            return a < b
        # 注意：不能调 super().__lt__()——PySide6 会递归回调本重载导致栈溢出
        return self.text() < other.text()


def _sort_num(v):
    """从单元格文本里提取可排序的数值；不是数值型内容返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    # 纯数字 / 日期（2025/6/1、20260601）
    if re.match(r"^[\d./:\-]+$", s):
        digits = re.sub(r"\D", "", s)
        return float(digits) if digits else None
    # 数字 + 单位（12.3 MB / 30 ms / 97% / 1.5 GB）
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB|TB|MB/s|KB/s|ms|%|个)?$",
                 s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").upper()
    val *= {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}.get(unit, 1)
    return val


def style_menu(menu, dark=False):
    """统一菜单配色（否则深色主题下白字白底看不见）。"""
    bg = T.get_color("PANEL", dark)
    fg = T.get_color("TEXT", dark)
    ln = T.get_color("LINE", dark)
    menu.setStyleSheet(
        "QMenu { background: %s; color: %s; border: 1px solid %s; padding: 4px; }"
        "QMenu::item { color: %s; padding: 6px 22px 6px 14px; background: transparent; }"
        "QMenu::item:selected { background: %s; color: %s; }"
        "QMenu::item:disabled { color: %s; }"
        "QMenu::separator { height: 1px; background: %s; margin: 4px 8px; }"
        % (bg, fg, ln, fg, ln, fg, T.get_color("TEXT3", dark), ln))


# ---------------- 表格列宽：可拖动 + 右键自动适应 ----------------
COL_MIN_W = 56          # 列宽下限（防止拖到 0 宽后找不回来）
COL_PAD = 18            # 按内容自适应时留的内边距


def _autofit_column(table, col):
    """把某一列调到「刚好放下内容」的宽度（含表头文字），并限制上下限。"""
    if col < 0 or col >= table.columnCount():
        return
    table.resizeColumnToContents(col)
    table.setColumnWidth(col, max(COL_MIN_W, min(table.columnWidth(col) + COL_PAD, 760)))


def _autofit_all(table):
    """所有列按内容自适应。"""
    hh = table.horizontalHeader()
    keep = hh.stretchLastSection()
    hh.setStretchLastSection(False)     # 开启时会干扰按内容测量
    for c in range(table.columnCount()):
        _autofit_column(table, c)
    hh.setStretchLastSection(keep)


def _fit_window(table):
    """先按内容量宽，再整体缩放，让列宽合计刚好填满可视区域。"""
    n = table.columnCount()
    avail = table.viewport().width()
    if n == 0 or avail <= 0:
        return
    hh = table.horizontalHeader()
    keep = hh.stretchLastSection()
    hh.setStretchLastSection(False)
    sizes = []
    for c in range(n):
        table.resizeColumnToContents(c)
        sizes.append(max(COL_MIN_W, table.columnWidth(c) + COL_PAD))
    total = float(sum(sizes))
    if total <= 0:
        hh.setStretchLastSection(keep)
        return
    scale = avail / total
    for c, w in enumerate(sizes):
        table.setColumnWidth(c, max(COL_MIN_W, int(w * scale)))
    used = sum(table.columnWidth(c) for c in range(n))
    if used < avail:                    # 取整误差补给末列
        table.setColumnWidth(n - 1, table.columnWidth(n - 1) + (avail - used))
    hh.setStretchLastSection(keep)


def _column_menu(table, pos):
    """表头右键：只放列调整项。

    和资源管理器一致 —— 这套菜单**只在最上方的表头**上唤出，
    点内容区不会再弹出「自动调整列宽」这类东西。
    """
    hh = table.horizontalHeader()
    col = hh.logicalIndexAt(pos)
    spot = hh.mapToGlobal(pos)
    dark = bool(getattr(table.window(), "dark", False))
    m = QMenu(table)
    style_menu(m, dark)

    a_this = m.addAction("自动调整此列宽度")
    a_this.setEnabled(col >= 0)
    a_all = m.addAction("自动调整所有列")
    m.addSeparator()
    a_fit = m.addAction("适应窗口宽度")
    m.addSeparator()
    a_movable = m.addAction("允许拖动调整列顺序")
    a_movable.setCheckable(True)
    a_movable.setChecked(hh.sectionsMovable())
    a_stretch = m.addAction("末列自动填满剩余空间")
    a_stretch.setCheckable(True)
    a_stretch.setChecked(hh.stretchLastSection())

    act = m.exec(spot)
    if act is None:
        return
    if act is a_this:
        _autofit_column(table, col)
    elif act is a_all:
        _autofit_all(table)
    elif act is a_fit:
        _fit_window(table)
    elif act is a_movable:
        hh.setSectionsMovable(a_movable.isChecked())
    elif act is a_stretch:
        hh.setStretchLastSection(a_stretch.isChecked())


def _row_menu(table, pos):
    """内容区右键：只放「对这一行做什么」。

    表头那一套列宽菜单（自动调整 / 适应窗口…）只在表头上唤出 —— 和资源管理器
    一样，点内容区不会再蹦出列相关的东西。

    页面可以用 attach_row_menu() 把自己的动作（卸载 / 打开注册表 / 定位服务…）
    挂上来；standalone=True 时通用项（定位文件夹 / 复制路径）不再追加。
    """
    reveal = _row_reveal_path(table, pos)
    dark = bool(getattr(table.window(), "dark", False))
    m = QMenu(table)
    style_menu(m, dark)

    builder = _ROW_MENUS.get(table)
    row = table.rowAt(pos.y())
    if builder is not None and row >= 0:
        try:
            builder(m, table, row)
        except Exception:
            pass
    a_rev = a_copy = None
    if table not in _ROW_MENUS_FULL:              # 页面自己给全了菜单就不追加通用项
        reveal = _row_reveal_path(table, pos)
        if m.actions():
            m.addSeparator()
        a_rev = m.addAction("定位文件夹")
        a_rev.setEnabled(bool(reveal))
        a_copy = m.addAction("复制路径")
        a_copy.setEnabled(bool(reveal))
    if not m.actions():
        return                                   # 没有可做的动作就不弹空菜单

    act = m.exec(table.viewport().mapToGlobal(pos))
    if act is None:
        return
    if act is a_rev:
        reveal = _row_reveal_path(table, pos)
        if not reveal_in_explorer(reveal):
            info(table.window(), "找不到该路径（可能已被删除）：\n%s" % reveal)
    elif act is a_copy:
        copy_text(_row_reveal_path(table, pos))


def make_table(headers, stretch_first=True, wrap=True, sortable=False):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setShowGrid(False)
    t.setWordWrap(wrap)                    # 问题2：单元格自动换行
    t.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    t.setMinimumHeight(160)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.Interactive)   # 每一列都能拖动改宽
    hh.setMinimumSectionSize(COL_MIN_W)                # 防止拖到看不见
    hh.setStretchLastSection(True)                     # 末列吃掉剩余空间，其余列自由拖动
    hh.setContextMenuPolicy(Qt.CustomContextMenu)
    hh.customContextMenuRequested.connect(
        lambda pos, tb=t: _column_menu(tb, pos))
    # 内容区右键只给行操作（定位文件夹 / 复制路径 / 页面自定义）
    t.setContextMenuPolicy(Qt.CustomContextMenu)
    t.customContextMenuRequested.connect(
        lambda pos, tb=t: _row_menu(tb, pos))
    hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    t.setSortingEnabled(sortable)          # 点表头按列排序
    return t


# ---------------- 右键「定位文件夹」 ----------------
# 每个表格注册一个「行 → 路径」解析器；右键某一行时菜单里就有「定位文件夹」。
# 用弱引用字典：对话框/页面销毁后自动摘掉，不会因为注册器持有引用而泄漏。
_REVEAL_GETTERS = weakref.WeakKeyDictionary()
_ROW_MENUS = weakref.WeakKeyDictionary()
# 标记「该表格的右键菜单完全由页面自己给」：不再追加通用的定位/复制路径，
# 免得跟页面自己的同名项重复成两行。
_ROW_MENUS_FULL = weakref.WeakSet()


def attach_reveal(table, getter):
    """给表格注册「行 → 路径」解析器（getter(table, row) -> str | None）。"""
    _REVEAL_GETTERS[table] = getter


def attach_row_menu(table, builder, standalone=False):
    """给表格注册内容区右键的动作：builder(menu, table, row)。

    `standalone=False`（默认）：页面动作排在通用项（定位文件夹 / 复制路径）之前。
    `standalone=True`：菜单完全由 builder 决定，通用项不再追加。

    builder 内用 `action.triggered.connect(...)` 绑定处理函数即可，无需返回值。
    """
    _ROW_MENUS[table] = builder
    if standalone:
        _ROW_MENUS_FULL.add(table)
    else:
        _ROW_MENUS_FULL.discard(table)


def _shell_select(path):
    """用 Shell API `SHOpenFolderAndSelectItems` 在资源管理器里选中目标。

    为什么不用命令行：`Popen(["explorer", "/select," + path])` 在路径含空格时
    （`C:\\Program Files\\...` 这类占大多数）Python 会给整个参数补引号，
    explorer 收到 `"/select,C:\\Program Files\\a b"` 后解析失败 —— 打开的不是目标
    位置（GitHub / StackOverflow 上的经典坑）。Shell API 直接吃 PIDL，没有
    命令行解析这一步。
    """
    try:
        import ctypes
        # SHParseDisplayName 不认正斜杠（返回 E_INVALIDARG），统一成反斜杠
        path = os.path.normpath(path)
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        try:
            ole32.CoInitialize(None)        # 主线程通常已初始化，重复调用无害
        except Exception:
            pass
        pidl = ctypes.c_void_p()
        if shell32.SHParseDisplayName(ctypes.c_wchar_p(path), None,
                                      ctypes.byref(pidl), 0, None) != 0:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:
        return False


def _explorer_select(path):
    """定位文件/目录（单独成函数：测试可安全打桩）。

    优先 Shell API；失败则退回命令行形式（字符串 + 只给路径加引号 —— explorer
    自己能正确解析 `explorer /select,"C:\\a b\\c.exe"`）。
    """
    if _shell_select(path):
        return
    try:
        subprocess.Popen('explorer /select,"%s"' % path)
    except Exception:
        pass


def reveal_in_explorer(path):
    """在资源管理器里定位文件/目录（目录 → 打开其父目录并高亮它）。

    路径不存在时退回定位它的父目录；父目录也没有则返回 False（由调用方提示）。
    """
    try:
        p = os.path.abspath(os.path.expandvars((path or "").strip().strip('"')))
    except Exception:
        p = ""
    if p and os.path.exists(p):
        _explorer_select(p)
        return True
    parent = os.path.dirname(p) if p else ""
    if parent and os.path.isdir(parent):
        _explorer_select(parent)
        return True
    return False


def copy_text(text):
    QApplication.clipboard().setText(text or "")


def open_with_default(target):
    """用系统默认程序打开（.msc / .exe / 文件夹都能），失败返回 False。"""
    try:
        os.startfile(target)                 # noqa: S606  Windows 专有
        return True
    except Exception:
        return False


def show_file_properties(path):
    """弹出文件的「属性」对话框（ShellExecute 的 properties 谓词）。"""
    if not path or not os.path.exists(path):
        return False
    return _shell_exec("properties", path)


def _shell_exec(verb, path):
    """`ShellExecuteW` 的薄封装。

    **单独成函数是为了测试能安全打桩** —— 否则测试一跑就会真的弹「属性」对话框
    或真去删文件。返回值 > 32 才算成功（<= 32 是 ShellExecute 的错误码，
    5 = 用户取消）。
    """
    import ctypes
    r = ctypes.windll.shell32.ShellExecuteW(None, verb, path, None, None, 1)
    return int(r) > 32


def open_power_control(plan_settings_page=True):
    """打开控制面板的电源计划界面。

    `control.exe powercfg.cpl,,1` = 「更改计划设置」（只作用于**当前生效**的计划）；
    不带页号 = 「电源选项」主页（能看到所有计划并逐个修改）。
    """
    arg = "powercfg.cpl,,1" if plan_settings_page else "powercfg.cpl"
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "control.exe")
    for cmd in ([exe, arg], ["control", arg]):
        try:
            subprocess.Popen(cmd)
            return True
        except Exception:
            continue
    return False


def open_folder_of(path):
    """打开 path 所在的文件夹（路径不存在时退回它的父目录）。"""
    return reveal_in_explorer(path)


# ---------------- 纯 Windows 原生的文件动作 ----------------
#
# 为什么要自己列一遍、而不直接把 Shell 的右键菜单拉出来（IContextMenu）：
# 那会把用户机器上装的所有 Shell 扩展（网盘客户端、压缩工具、编辑器、聊天软件）
# 一起塞进菜单里 —— 用户明确要求「不要第三方应用所带的选项，只要系统原生的」。
# 下面每一项都是资源管理器里能做到的**同一件事**，用系统自带的方式实现。

def shell_verb(path, verb):
    """用 Shell 的原生谓词做事（copy / cut / delete / properties …）。

    为什么用谓词而不是自己实现：
      · `copy` / `cut` 会把文件按**资源管理器认识的格式**放进剪贴板
        （CF_HDROP + Preferred DropEffect）。自己拼很容易做出「看着是复制、
        粘到资源管理器里变成移动」这种错。
      · `delete` 走的是系统自己的「确认 → 移到回收站」流程，尊重用户的
        回收站设置，不用我们另造一套删除逻辑。
    """
    if not path or not os.path.exists(path):
        return False
    try:
        return _shell_exec(verb, path)
    except Exception:
        return False


def open_terminal(path, kind="cmd"):
    """在 path 所在目录打开**系统自带**的终端（cmd / PowerShell）。

    ⚠️ 不能带 CREATE_NO_WINDOW —— 那样会起一个没有窗口的 cmd，
    等于什么都没发生（用户看不到任何东西）。
    """
    d = path if os.path.isdir(path) else os.path.dirname(path)
    if not d or not os.path.isdir(d):
        return False
    try:
        if kind == "ps":
            subprocess.Popen(["powershell", "-NoExit", "-Command",
                              "Set-Location -LiteralPath '%s'"
                              % d.replace("'", "''")])
        else:
            subprocess.Popen(["cmd", "/k", "cd", "/d", d])
        return True
    except Exception:
        return False


def create_shortcut_on_desktop(path):
    """在桌面建一个快捷方式（.lnk，走系统的 WScript.Shell）。返回 (ok, 说明)。"""
    if not path or not os.path.exists(path):
        return False, "路径不存在（可能已被移动或删除）"
    desktop = S.known_folder("desktop") or os.path.join(os.path.expanduser("~"),
                                                        "Desktop")
    if not os.path.isdir(desktop):
        return False, "找不到桌面目录：%s" % desktop
    base = os.path.basename(path.rstrip("\\/")) or "快捷方式"
    lnk = os.path.join(desktop, base + " - 快捷方式.lnk")
    if os.path.exists(lnk):
        return False, "桌面上已有同名快捷方式：%s" % os.path.basename(lnk)
    script = ("$w = New-Object -ComObject WScript.Shell;"
              "$s = $w.CreateShortcut('%s');"
              "$s.TargetPath = '%s';$s.Save()"
              % (lnk.replace("'", "''"), path.replace("'", "''")))
    rc, out = S.ps("$ErrorActionPreference='Stop';" + script, timeout=40)
    if rc == 0 and os.path.exists(lnk):
        return True, lnk
    return False, (out or "").strip().splitlines()[0][:120] if out else "创建失败"


def rename_path(win, path):
    """重命名（等同资源管理器按 F2）。

    ⚠️ 同名已存在时**直接拒绝**，不弹「要不要覆盖」—— 覆盖别人的文件是不可逆的，
    而这个入口不值得冒那个险。
    """
    if not os.path.exists(path):
        return warn(win, "找不到该路径", path)
    old = os.path.basename(path.rstrip("\\/"))
    new, ok = QInputDialog.getText(win, "重命名", "新名称：",
                                   QLineEdit.Normal, old)
    new = (new or "").strip()
    if not ok or not new or new == old:
        return
    if any(c in new for c in '\\/:*?"<>|'):
        return warn(win, "名称里有非法字符", '不能包含 \\ / : * ? " < > |')
    tgt = os.path.join(os.path.dirname(path.rstrip("\\/")), new)
    if os.path.exists(tgt):
        return warn(win, "同名已存在，未改动",
                    "目标位置已经有叫「%s」的东西了。换个名字，或先处理掉那个。"
                    % new)
    try:
        os.rename(path, tgt)
    except OSError as e:
        return warn(win, "重命名失败", str(e)[:160])
    info(win, "已重命名为「%s」" % new,
         "列表里还是**上次扫描时**的数据，重新扫描后才会反映这次改名。")


def _shell_guard(win, ok, what):
    """给「复制 / 剪切」这类由系统接管的动作补一句失败提示。

    系统谓词失败时**不会**有返回值告诉用户，静默失败会让人以为已经复制好了 ——
    所以失败必须自己喊出来。
    """
    if not ok:
        warn(win, "%s 失败" % what,
             "系统没有接受这个操作（可能是文件已被占用或权限不足）。")


def _shortcut_ui(win, path):
    ok, note = create_shortcut_on_desktop(path)
    if ok:
        info(win, "已在桌面创建快捷方式", note)
    else:
        warn(win, "创建快捷方式失败", note)


def _delete_ui(win, path, name, is_dir):
    """删除到回收站。**必须二次确认**（本项目铁律：不可逆操作先确认再动手）。

    系统随后还会弹一次自己的回收站确认框 —— 双确认在这里是刻意的：
    这是唯一一个会把文件从磁盘上挪走的入口。
    """
    if not confirm(win, "删除到回收站",
                   "即将把%s「%s」移到回收站。\n\n"
                   "系统随后还会再确认一次。回收站清空后就真的没了，"
                   "确认要删除吗？" % ("文件夹" if is_dir else "文件", name),
                   danger=True):
        return
    if not shell_verb(path, "delete"):
        warn(win, "删除失败", "系统没有接受这个操作（可能已只读或权限不足）。")


def native_file_menu(menu, path, win):
    """把「纯原生」的文件 / 文件夹动作加进 `menu`（就地连好处理函数）。

    分组顺序照着资源管理器：打开 → 路径 → 剪贴板 → 新建 → 改名/删除 → 终端 → 属性。
    不适用的项（比如对文件「在此打开命令提示符」）会置灰而不是消失 ——
    置灰能让人知道「有这个能力、只是当前对象不适用」。
    """
    exists = bool(path) and os.path.exists(path)
    is_dir = exists and os.path.isdir(path)
    name = os.path.basename(path.rstrip("\\/")) or path

    def add(label, fn, enabled=True):
        a = menu.addAction(label)
        a.setEnabled(bool(enabled))
        a.triggered.connect(fn)
        return a

    add("打开", lambda: open_with_default(path), exists)
    add("在资源管理器中定位", lambda: reveal_in_explorer(path) or
        info(win, "找不到该路径（可能已被删除）", path), exists)
    menu.addSeparator()
    add("复制完整路径", lambda: copy_text(path), exists)
    add("复制名称", lambda: copy_text(name), exists)
    menu.addSeparator()
    add("复制", lambda: _shell_guard(win, shell_verb(path, "copy"), "复制"), exists)
    add("剪切", lambda: _shell_guard(win, shell_verb(path, "cut"), "剪切"), exists)
    add("创建快捷方式到桌面", lambda: _shortcut_ui(win, path), exists)
    menu.addSeparator()
    add("重命名…", lambda: rename_path(win, path), exists)
    add("删除（移到回收站）…", lambda: _delete_ui(win, path, name, is_dir), exists)
    menu.addSeparator()
    add("在此打开命令提示符", lambda: open_terminal(path, "cmd"), is_dir)
    add("在此打开 PowerShell", lambda: open_terminal(path, "ps"), is_dir)
    menu.addSeparator()
    add("属性", lambda: show_file_properties(path), exists)


def _row_reveal_path(table, pos):
    """当前右键行的可定位路径（没有注册器 / 行无效时返回 ""）。"""
    getter = _REVEAL_GETTERS.get(table)
    if getter is None:
        return ""
    row = table.rowAt(pos.y())
    if row < 0:
        return ""
    try:
        return getter(table, row) or ""
    except Exception:
        return ""


# ---------------- 程序图标（按 exe 路径取系统图标，全局缓存） ----------------
_ICON_CACHE = {}


def file_icon(path):
    """按可执行文件路径取系统图标（带缓存）。路径无效时返回 None。"""
    key = (path or "").strip().lower()
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    icon = None
    if key and os.path.exists(path):
        try:
            from PySide6.QtWidgets import QFileIconProvider
            icon = QFileIconProvider().icon(QFileInfo(path))
        except Exception:
            icon = None
    _ICON_CACHE[key] = icon
    return icon


def exe_from_cmd(cmd):
    """从命令行里提取可执行文件路径（取不到返回 ''）。"""
    c = (cmd or "").strip()
    if not c:
        return ""
    if c.startswith('"'):
        end = c.find('"', 1)
        p = c[1:end] if end > 0 else c.strip('"')
    else:
        p = c.split(" ")[0]
    p = os.path.expandvars(p).strip().strip('"')
    return p if os.path.isfile(p) else ""


def fill_table(table, rows, tooltips=None, icons=None, keys=None):
    """填充表格。

    `icons` — 可选，按行给第一列设置图标（元素为 QIcon 或 None）。
    `keys`  — 可选，按行把「该行的唯一标识」存进第一列单元格的 UserRole。
              表格一旦开启排序，行号就不再对应原始数据列表，取当前选中项
              **必须**读 UserRole，不能用 currentRow() 当下标。
    """
    was_sorted = table.isSortingEnabled()
    if was_sorted:
        table.setSortingEnabled(False)     # 填充期间必须关掉，否则行会乱跳
    table.setRowCount(0)
    for r_i, r in enumerate(rows):
        i = table.rowCount()
        table.insertRow(i)
        tallest = 20
        for c, v in enumerate(r):
            it = SortItem("" if v is None else str(v))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            num = _sort_num(v)
            if num is not None:
                it.setData(Qt.UserRole + 1, num)
            it.setToolTip("" if v is None else str(v))   # 悬停可看全文
            if (c == 0 and icons is not None and r_i < len(icons)
                    and icons[r_i] is not None):
                it.setIcon(icons[r_i])
            if c == 0 and keys is not None and r_i < len(keys):
                it.setData(Qt.UserRole, keys[r_i])
            table.setItem(i, c, it)
            tallest = max(tallest, it.sizeHint().height())
        table.setRowHeight(i, max(30, min(tallest + 10, 120)))
    if tooltips:
        for i, tips in enumerate(tooltips):
            for c, tip in enumerate(tips):
                it = table.item(i, c)
                if it is not None:
                    it.setToolTip(str(tip))
    if was_sorted:
        table.setSortingEnabled(True)


def picked_key(table, col=0):
    """取当前选中行的标识（fill_table 的 keys 写进 UserRole 的那个值）。

    表格开启排序后行号会重排，必须用这个而不是 currentRow() 当下标。
    """
    r = table.currentRow()
    if r < 0:
        return None
    it = table.item(r, col)
    return it.data(Qt.UserRole) if it is not None else None


def btn(text, kind=None, on_click=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if on_click:
        b.clicked.connect(on_click)
    return b


def select_all_button(table, on_changed=None, text="全选"):
    """给「第 0 列是勾选框」的表格配一个全选/全不选切换按钮。

    行为：有未勾选的就全选，已全选则清空；按钮文案与 tooltip 跟着状态走。
    表格填完后调一次 `btn._refresh()` 让文案与计数对齐。
    """
    b = btn(text)

    def _scan():
        n = table.rowCount()
        k = 0
        for r in range(n):
            it = table.item(r, 0)
            if it is not None and (it.flags() & Qt.ItemIsUserCheckable) \
                    and it.checkState() == Qt.Checked:
                k += 1
        return k, n

    def _refresh():
        k, n = _scan()
        b.setEnabled(n > 0)
        b.setText("全不选" if (n and k == n) else "全选")
        b.setToolTip("已选 %d / %d 项" % (k, n))

    def _toggle():
        k, n = _scan()
        target = Qt.Unchecked if (n and k == n) else Qt.Checked
        was = table.blockSignals(True)
        for r in range(n):
            it = table.item(r, 0)
            if it is not None and (it.flags() & Qt.ItemIsUserCheckable):
                it.setCheckState(target)
        table.blockSignals(was)          # 避免 N 次 itemChanged 回调
        _refresh()
        if on_changed:
            on_changed()

    b.clicked.connect(_toggle)
    b._refresh = _refresh
    _refresh()
    return b


class MultiPick:
    """「多选下拉」按钮 —— 用一个按钮 + 弹出菜单代替 `QComboBox`。

    为什么不用 QComboBox：它天生**单选**。18 个分类时，想同时看「COM + 文件
    类型关联」就得把其余16 个取消勾选，交互成本高得离谱。这里换成一个按钮：
    按钮上写当前状态（「全部」/「3 类已选」/「全部（18）」），点开是一个
    **可勾选的菜单**，外加「全选 / 全不选 / 反选」三个快捷动作。

    语义：**空集合 = 全部**（与「全选」等价，但更好认—— 保留用户「我还没
    挑过」和「我明确要全看」这两种不同的状态）。

    用法：
        mp = MultiPick(win.dark, on_change=self._render_issues)
        mp.set_items([(cid, 名称, 条数), ...])   # 每次扫描后重建
        mp.picked()          # -> set，**空集表示全部**
        mp.picked_text()     # -> 「全部」/「3 类已选」
    """

    def __init__(self, dark=False, on_change=None):
        self.dark = dark
        self.on_change = on_change
        self._items = []                # [(key, 文本, 副文本), ...]
        self._sel = set()
        self.btn = QPushButton("全部")
        self.btn.setObjectName("Primary")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setToolTip("点开可以选择多个分类（留空 = 显示全部）")
        self.btn.clicked.connect(self._popup)
        self._paint()

    # ---------- 对外 ----------
    def set_items(self, items):
        """重建选项。`items` = [(key, 文本, 副文本或"")...]，key 为 None 表示「全部」。

        **会清空已选** —— 因为旧选项的 key 可能已经不存在了。
        """
        self._items = list(items)
        self._sel = set(k for k in self._sel
                        if any(k == it[0] for it in self._items) and k is not None)
        self._paint()

    def picked(self):
        return set(self._sel)

    def picked_text(self):
        n = len(self._sel)
        if n == 0:
            return "全部"
        if n == len([it for it in self._items if it[0] is not None]):
            return "全部（%d）" % n
        return "%d 类已选" % n

    def clear(self):
        self._sel = set()
        self._paint()

    def is_all(self):
        """当前是否等价于「不过滤」。"""
        return len(self._sel) == 0

    # ---------- 内部 ----------
    def _paint(self):
        self.btn.setText(self.picked_text())
        keys = [it[0] for it in self._items if it[0] is not None]
        self.btn.setEnabled(True)
        self.btn.setToolTip("当前：%s\n点开可多选；不勾任何一项 = 显示全部"
                            % self.picked_text()
                            if keys else "还没有扫描结果")

    def _fire(self):
        self._paint()
        if self.on_change:
            self.on_change()

    def _popup(self):
        if not self._items:
            return
        m = QMenu(self.btn)
        style_menu(m, self.dark)
        acts = {}
        for key, text, sub in self._items:
            a = m.addAction(text)
            a.setCheckable(True)
            # 空集合 = 全部，所以「全部」这一项在「没选任何具体项」时打勾
            a.setChecked(True if key is None else key in self._sel)
            if sub:
                a.setToolTip(sub)
            acts[key] = a
        m.addSeparator()
        a_all = m.addAction("全选")
        a_none = m.addAction("清空选择")
        a_inv = m.addAction("反选")
        keys = [it[0] for it in self._items if it[0] is not None]
        a_inv.setEnabled(len(keys) >= 2)

        act = m.exec(self.btn.mapToGlobal(self.btn.rect().bottomLeft()))
        if act is None:
            return
        if act is a_all:
            self._sel = set(keys)
        elif act is a_none:
            self._sel = set()
        elif act is a_inv:
            self._sel = keys - self._sel
        else:
            hit = next((k for k, a in acts.items() if a is act), "?")
            if hit is None:
                # 「全部」这一项被勾选 = 不筛任何具体分类
                self._sel = set()
            elif hit != "?":
                # ⚠️ 不能写成「遍历 + break」：那会把 `else` 吃掉，
                # 导致「全部」那项点了没反应（而且不报错）。
                if act.isChecked():
                    self._sel.add(hit)
                else:
                    self._sel.discard(hit)
        self._fire()


# ==========================================================================
# 无边框圆角窗口（问题6）
# ==========================================================================
class FramelessWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._dragging = False
        self._drag_off = None

    # ---- 裁剪圆角 ----
    def _clip(self):
        if self.isMaximized() or self.isFullScreen():
            self.clearMask()
        else:
            r = QRect(0, 0, self.width(), self.height())
            path = QPainterPath()
            path.addRoundedRect(r, 12, 12)
            self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, e):
        self._clip()
        super().resizeEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            pos = e.position().toPoint()
            if self.isFullScreen():
                return
            if self._on_draggable_area(pos):
                self._dragging = True
                self._drag_off = e.globalPosition().toPoint() - \
                    self.frameGeometry().topLeft()

    def _on_draggable_area(self, pos):
        """精准判断：按住标题栏（按钮以外）即可拖动窗口。

        从 childAt 命中的最深层控件沿父链上溯：
        - 命中窗口按钮 -> 交给按钮，不拖动；
        - 命中标题栏本身或其内部非按钮控件 -> 可拖动；
        - 命中其它区域 -> 不拖动。
        """
        tb = self.findChild(QWidget, "TitleBar")
        if tb is None:
            return False
        if not tb.geometry().contains(pos):
            return False
        w = self.childAt(pos)
        while w is not None and w is not self:
            if w.objectName() in ("TitleBtn", "TitleBtnClose"):
                return False          # 点在窗口按钮上 -> 交给按钮
            if w is tb:
                return True           # 位于标题栏内 -> 可拖动
            w = w.parentWidget()
        # 父链未到达标题栏（命中的是标题栏的直接背景）视为可拖动
        return True

    def mouseMoveEvent(self, e):
        if self._dragging and self._drag_off is not None:
            if self.isFullScreen():
                return
            self.move(e.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, e):
        self._dragging = False
        self._drag_off = None

    def mouseDoubleClickEvent(self, e):
        if self._on_draggable_area(e.position().toPoint()):
            self.toggle_maximized()

    def toggle_maximized(self):
        """最大化 / 还原。

        用 showMaximized 而不是 showFullScreen：前者只占「工作区」
        （自动避开任务栏），后者会盖住任务栏。
        """
        if self._is_max or self.isMaximized():
            self.showNormal()
            self._is_max = False
        else:
            self.showMaximized()
            self._is_max = True
        self._sync_max_flag()

    def _sync_max_flag(self):
        on = bool(self._is_max or self.isMaximized())
        if getattr(self, "btn_full", None) is not None:
            self.btn_full.setText("❐" if on else "□")
        sh = self.findChild(QFrame, "Shell")
        for w in (sh, self.findChild(QWidget, "TitleBar"),
                  self.findChild(QFrame, "Nav")):
            if w is not None:
                w.setProperty("maximized", on)
                w.style().unpolish(w)
                w.style().polish(w)
        self._clip()


# ==========================================================================
# 自绘仪表控件（概览页效果图版式：环形进度 / 迷你折线 / 五轴雷达）
# --------------------------------------------------------------------------
# 全部是"只画不存"的轻量控件：不建常驻定时器、不装特效层，
# 有数据更新时 update() 一次，动画交给外层的 BarTween（QPropertyAnimation）。
# ==========================================================================
class RingGauge(QWidget):
    """环形进度（CPU 指标卡右侧的圆环）。

    暴露 `value` 属性 → 直接复用 BarTween（`QPropertyAnimation(bar, b"value")`）。
    颜色随占用率分档：常规=主题色 / >65% 警告 / >85% 危险。
    """

    def __init__(self, parent=None, size=58, color=None, dark=False, warn=None, err=None,
                 accent="ACCENT_CPU"):
        super().__init__(parent)
        self._v = 0
        self._size = int(size)
        self._dark = dark
        self._accent = accent
        self._fixed_color = color          # 显式指定就固定（主题切换也不动它）
        self._color = color or T.get_color(accent, dark)
        self._warn = warn or T.get_color("WARN", dark)
        self._err = err or T.get_color("ERR", dark)
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_dark(self, dark):
        """主题切换：颜色是构造时取好的，这里按原色键重取并重画。"""
        self._dark = bool(dark)
        self._color = self._fixed_color or T.get_color(self._accent, self._dark)
        self._warn = T.get_color("WARN", self._dark)
        self._err = T.get_color("ERR", self._dark)
        self.update()

    def get_pct(self):
        return self._v

    def set_pct(self, v):
        v = int(max(0, min(100, round(v or 0))))
        if v != self._v:
            self._v = v
            self.update()

    # 补间属性走 `pct`；`value()`/`setValue()` 保留成 QProgressBar 同名的普通方法，
    # 这样把进度条换成环形时，调用方（_cls、测试、别的页面）都不用改。
    pct = Property(int, get_pct, set_pct)

    def value(self):
        return self._v

    def setValue(self, v):
        self.set_pct(v)

    def _stroke(self):
        if self._v > 85:
            return self._err
        if self._v > 65:
            return self._warn
        return self._color

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = min(self.width(), self.height())
        pen_w = max(3.0, w * 0.115)                 # 环宽随尺寸走
        pad = pen_w / 2.0 + 0.5
        rect = QRect(int(pad), int(pad), int(w - 2 * pad), int(w - 2 * pad))
        pen = QPen(QColor(T.get_color("TRACK", self._dark)), pen_w)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)
        if self._v <= 0:
            return
        pen.setColor(QColor(self._stroke()))
        p.setPen(pen)
        # 12 点方向起、顺时针（Qt 的起始角逆时针计数，故用 -span）
        p.drawArc(rect, 90 * 16, int(-360 * 16 * (self._v / 100.0)))


class CardScroll(QScrollArea):
    """卡片内部的垂直滚动区：让"内容很高"的卡不再把整页顶出视口。

    为什么需要：概览「详细信息」的 8 行值是 wordWrap 多行文本（显卡名 / 显示器
    型号 / 网卡名都换行），卡片 sizeHint 会随文本长度涨到 400+。而 QVBoxLayout
    在"各 item 的 sizeHint 之和已超过可用高度"时**不再按 stretch 分配**，
    于是整页溢出一屏（实测 1366×728 溢出 58px、1280×690 溢出 87px）。

    为什么不能只靠 QSizePolicy.Ignored：Ignored 只影响 widget 能否伸缩，
    QWidgetItem::sizeHint() 仍然返回内容高度，布局照样按那个值分配（实测无效）。

    做法：**把 sizeHint 的高度按比例压小**（宽度仍跟随内容）。这样
      · 小屏：布局的总需求变小 → mid 能被压缩 → 内容在卡内滚动，页面不溢出；
      · 大屏：省下来的空间会按 stretch 分回给 mid → 卡片照样拿够高度、
        完整显示全部行、不出现滚动条。
    """

    def __init__(self, parent=None, hint_ratio=0.55, hint_min=96):
        super().__init__(parent)
        self._ratio = hint_ratio
        self._hint_min = hint_min

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(s.width(), max(self._hint_min, int(s.height() * self._ratio)))


class RadarChart(QWidget):
    """五轴雷达（性能状态卡）：处理器 / 内存 / 显卡 / 磁盘 / 网络。

    set_values([...5 个 0-100]) 即可，未就绪的点按 0 画。
    """

    def __init__(self, parent=None, dark=False, color=None, labels=None, size=200,
                 accent="BLUE"):
        super().__init__(parent)
        self._vals = [0.0] * 5
        self._dark = dark
        self._accent = accent
        self._fixed_color = color
        self._color = color or T.get_color(accent, dark)
        self._labels = labels or ["处理器", "内存", "显卡", "磁盘", "网络"]
        self.setMinimumSize(int(size), int(size))
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_dark(self, dark):
        self._dark = bool(dark)
        self._color = self._fixed_color or T.get_color(self._accent, self._dark)
        self.update()

    def set_values(self, vals):
        out = []
        for i in range(5):
            try:
                v = float(vals[i]) if i < len(vals) else 0.0
            except (TypeError, ValueError):
                v = 0.0
            out.append(max(0.0, min(100.0, v)))
        if [round(x, 1) for x in out] != [round(x, 1) for x in self._vals]:
            self._vals = out
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        side = min(w, h)
        if side < 40:
            return
        cx, cy = w / 2.0, h / 2.0
        R = side * 0.34                       # 留出标签的位置
        import math
        ang = [-math.pi / 2 + i * 2 * math.pi / 5 for i in range(5)]

        def pt(i, r):
            return QPointF(cx + math.cos(ang[i]) * r, cy + math.sin(ang[i]) * r)

        grid = QColor(T.get_color("LINE2", self._dark))
        grid.setAlpha(150)
        for frac in (0.25, 0.5, 0.75, 1.0):
            poly = QPolygonF([pt(i, R * frac) for i in range(5)])
            p.setPen(QPen(grid, 1.0)); p.setBrush(Qt.NoBrush)
            p.drawPolygon(poly)
        for i in range(5):                    # 轴线
            p.setPen(QPen(grid, 1.0))
            p.drawLine(QPointF(cx, cy), pt(i, R))

        c = QColor(self._color)
        fill = QColor(c); fill.setAlpha(70)
        p.setPen(QPen(c, 2.0)); p.setBrush(QBrush(fill))
        p.drawPolygon(QPolygonF([pt(i, R * (self._vals[i] / 100.0)) for i in range(5)]))

        p.setPen(QColor(T.get_color("TEXT3", self._dark)))
        f = QFont(); f.setPointSizeF(8.0); p.setFont(f)
        for i in range(5):
            label = self._labels[i]
            anchor = pt(i, R * 1.22)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label)
            x = anchor.x() - tw / 2.0
            y = anchor.y() + fm.height() * 0.3
            p.drawText(QPointF(x, y), label)


class AtmosphereArea(QWidget):

    """内容区底：Mica 风格氛围层（替代纯平底色）。

    三层叠加，一次性画进缓存的 QPixmap：
      1. 基底色（Win11 层底）
      2. 左上主光 + 右下副光（跟随强调色，透明度仅 6~18%）→ 材质方向感
      3. 顶部一道极淡受光高光
    paintEvent 只贴图，尺寸 / 主题 / 强调色变化时才重画，无每秒开销。
    """

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("ContentArea")
        self._pm = None
        self._key = None

    def _render(self):
        w, h = max(1, self.width()), max(1, self.height())
        dark = bool(getattr(self.win, "dark", False))
        accent = T.get_color("BLUE", dark)
        pm = QPixmap(w, h)
        # 基底色 = 实心主题色。这里是**内容区真正的底色**（QSS 里
        # `QWidget#ContentArea` 是 transparent，靠这层自绘）。
        pm.fill(QColor(T.get_color("BG", dark)))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r, g, b = T.hex_to_rgb(accent)
        # 主光：左上角溢出。透明度压得很低 —— Mica 的要点是「几乎察觉不到的色彩倾向」，
        # 浓了就成了廉价渐变（深色下尤其明显）。
        halo = QRadialGradient(QPointF(w * 0.04, -h * 0.22), max(w, h) * 0.85)
        halo.setColorAt(0.00, QColor(r, g, b, 22 if dark else 8))
        halo.setColorAt(0.55, QColor(r, g, b, 8 if dark else 3))
        halo.setColorAt(1.00, QColor(r, g, b, 0))
        p.fillRect(0, 0, w, h, QBrush(halo))
        # 副光：右下角反向，制造纵深
        halo2 = QRadialGradient(QPointF(w * 1.04, h * 1.08), max(w, h) * 0.7)
        halo2.setColorAt(0.00, QColor(r, g, b, 12 if dark else 7))
        halo2.setColorAt(1.00, QColor(r, g, b, 0))
        p.fillRect(0, 0, w, h, QBrush(halo2))
        # 顶部受光高光（模拟窗口迎光面）
        top = QLinearGradient(0, 0, 0, 130)
        top.setColorAt(0.0, QColor(255, 255, 255, 8 if dark else 22))
        top.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(0, 0, w, 130, QBrush(top))
        p.end()
        self._pm = pm
        self._key = (w, h, dark, accent)

    def paintEvent(self, e):
        dark = bool(getattr(self.win, "dark", False))
        key = (self.width(), self.height(), dark, T.get_color("BLUE", dark))
        if self._pm is None or self._key != key:
            self._render()
        if self._pm is not None:
            QPainter(self).drawPixmap(0, 0, self._pm)

    def resizeEvent(self, e):
        self._pm = None                     # 尺寸变了，重画氛围层
        super().resizeEvent(e)


# ==========================================================================
# 页面基类
# ==========================================================================
class Page(QWidget):
    title = "页面"

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.loaded = False
        # 会话缓存：一次运行内每份数据只真正读一次。
        # 切页 / 切分类 / 排序 / 搜索一律复用，不再去碰系统；
        # 只有「手动点刷新」和「刚刚做过写操作」才会清掉它（见 reload）。
        self._sess = {}
        # 页面自身承载「纯色底」：QScrollArea 的 viewport 会取容器的背景，
        # 不给这里显式背景就会露出 Qt 默认 palette 的浅灰。
        self.setObjectName("Page")
        self.setAttribute(Qt.WA_StyledBackground, True)

    def sess_invalidate(self, *keys):
        '''让会话缓存失效：不带参数 = 全部失效。'''
        if keys:
            for k in keys:
                self._sess.pop(k, None)
        else:
            self._sess.clear()
    
    def reload(self):
        '''手动刷新 / 写操作之后：丢掉会话缓存，真的重读一遍。
    
        数据的有效期就等于本进程的生命周期 —— 打开软件时读一遍，之后不再重读；
        要让数据更新，只有两条路：点右上角刷新，或者刚做了一次写操作。
        '''
        self.sess_invalidate()
        self.refresh()
    
    def refresh(self):
        pass

    def load_fail(self, label, what="数据", msg=""):
        """在 `label` 上显示「读取失败：…」并标警示色。

        ⚠️ 项目铁律「**失败 ≠ 没有 / 读不到 ≠ 正常**」。后台读取失败时若静默
        留空，用户看到的是「—」或一张空表，**分不清是「确实没有」还是
        「读不出来」** —— 这个坑犯过好几次（备份列表、计划任务、服务状态）。

        例外只有 **1 秒级轮询**：那里弹提示会变成每秒一次骚扰。那几处已在
        代码里用 `# 刻意静默` 显式标注，审计断言要求「所有静默点都必须有标记」。
        """
        if label is None:
            return
        label.setText("读取失败：%s%s" % (what, ("（%s）" % msg) if msg else ""))
        label.setStyleSheet("color: %s;" % T.get_color("WARN", self.win.dark))

    def first_show(self):
        if not self.loaded:
            self.loaded = True
            self.refresh()

    def on_exit(self):
        """窗口关闭前回调（保存状态用）。"""
        pass


# ==========================================================================
# 1. 概览（问题5：加 GPU / 磁盘 / 温度）
# ==========================================================================
# 概览「详细信息」表的行 —— 顺序即显示顺序（对齐效果图）
DETAIL_ROWS = (("cpu", "处理器"), ("board", "主板"), ("mem", "内存"),
               ("gpu", "显卡"), ("mon", "显示器"), ("disk", "磁盘"),
               ("sound", "声卡"), ("net", "网卡"))


class OverviewPage(Page):
    title = "概览"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        # 一屏版式：所有间距按「刚好放得下」收紧（内容高度 ≈ 视口高度）
        # 1280×690 这类小窗也要一屏放下：再压一档留白，给「数据到达后行高变化」留余量。
        root.setContentsMargins(18, 2, 18, 2)
        root.setSpacing(4)

        self.cards = {}          # 兼容旧接口 {key: (val, sub, bar|None)}
        self.metrics = {}        # 主指标（大号数字）
        self._anim_parts = []    # 参与入场动画的容器

        # ---- 主指标：四张独立指标卡（大数字 + 胶囊进度条 + 小字说明） ----
        # 效果图版式：每项一张卡，数字是绝对主角；卡片间距 12，与下方卡片网格对齐。
        # 「显卡」「网络」两张卡标题行带下拉：有几张显卡就有几个选项 / 可切本机在用的网卡。
        strip_row = QHBoxLayout(); strip_row.setSpacing(12)
        metric_frames = []
        for key, label, st, cls in (
                ("cpu", "CPU", 1.0, "cpu"), ("mem", "内存", 1.0, "mem"),
                ("gpu", "GPU", 1.0, "gpu"), ("net", "网络", 1.0, "net")):
            f = QFrame(); f.setObjectName("StatCard")
            cl = QVBoxLayout(f)
            cl.setContentsMargins(16, 10, 16, 11); cl.setSpacing(5)
            head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 0); head.setSpacing(6)
            k = QLabel(label); k.setObjectName("MetricK")
            head.addWidget(k); head.addStretch(1)
            pick = None
            if key in ("gpu", "net"):
                pick = QComboBox(); pick.setObjectName("MetricPick")
                # 选项是运行时才填进去的 → 必须显式声明「随内容调整宽度」。
                # 默认的 AdjustToContentsOnFirstShow 会按**空列表**算一次宽度就不再变，
                # 于是「🔌 以太网」被截成「🔌 以太」。设上限防止超长网卡名撑破卡片。
                pick.setSizeAdjustPolicy(QComboBox.AdjustToContents)
                pick.setMaximumWidth(int(168 * (getattr(win, "scale", 1.0) or 1.0)))
                pick.setToolTip("切换要显示的显卡" if key == "gpu"
                                else "切换要显示的本机网卡")
                head.addWidget(pick, 0)
            cl.addLayout(head)

            # 数字区（左） + 图形区（右）：CPU 环形、网络折线、内存/GPU 无右侧图形
            body = QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(10)
            left = QVBoxLayout(); left.setContentsMargins(0, 0, 0, 0); left.setSpacing(1)
            num = QHBoxLayout(); num.setContentsMargins(0, 0, 0, 0); num.setSpacing(4)
            v = QLabel("—"); v.setObjectName("MetricV")
            u = QLabel("Mbps" if key == "net" else "%"); u.setObjectName("MetricU")
            u.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
            num.addWidget(v); num.addWidget(u); num.addStretch(1)
            sub = QLabel(""); sub.setObjectName("MetricSub"); sub.setWordWrap(True)
            left.addLayout(num)
            left.addWidget(sub)
            left.addStretch(1)
            body.addLayout(left, 1)

            bar = spark = None
            if key == "cpu":
                bar = RingGauge(f, size=int(62 * (getattr(win, "scale", 1.0) or 1.0)),
                                accent="ACCENT_CPU", dark=win.dark)
                bar.setToolTip("CPU 占用率")
                body.addWidget(bar, 0)
                cl.addLayout(body)
            elif key == "net":
                spark = Sparkline(f, accent="ACCENT_NET", dark=win.dark, height=38)
                spark.setMinimumHeight(int(38 * (getattr(win, "scale", 1.0) or 1.0)))
                spark.setToolTip("实时速率曲线")
                cl.addLayout(body)
                cl.addWidget(spark)        # 折线走整卡宽度，别跟数字挤在右半边
            else:
                bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0)
                bar.setTextVisible(False)
                cl.addLayout(body)
                cl.addWidget(bar)
            strip_row.addWidget(f, st)
            metric_frames.append(f)
            if key == "net":
                fmt = lambda x: ("%.2f" % x) if x < 10 else ("%.1f" % x)
            else:
                fmt = lambda x: "%.0f" % x
            self.metrics[key] = {
                "val": v, "sub": sub, "bar": bar, "spark": spark, "cell": f,
                "cls": cls, "pick": pick,
                "tween": NumberTween(v, fmt, parent=self),
                "bar_tween": BarTween(bar, parent=self) if bar is not None else None,
            }
            if pick is not None:
                pick.currentIndexChanged.connect(
                    lambda _i, kk=key: self._on_metric_pick(kk))
        root.addLayout(strip_row, 1)
        self._anim_parts += metric_frames

        # ---- 详细信息（键值行：多显卡 / 多磁盘 / 多声卡 / 多网卡 逐条列出） + 存储，并排 ----
        self.gpu_metric_sel = ""     # 显卡指标卡选中的显卡名（"" = 默认独显）
        self.net_sel = ""            # 网络指标卡选中的网卡名（"" = 第一张在线网卡）
        self._detail = {}            # {key: QLabel}
        self._sys_line = None        # 操作系统 · 运行时长 · 当前用户（合一行）
        self._gpu_names_sig = None
        self._net_names_sig = None
        self._net_cur = None
        self._gpus = []
        self._mons = []
        self._ex = {}
        self._vram = {}
        self._oem = ""

        mid = QHBoxLayout(); mid.setSpacing(12)
        det_card, dy = card("详细信息")
        g = QGridLayout(); g.setHorizontalSpacing(14); g.setVerticalSpacing(4)
        g.setColumnMinimumWidth(0, 52)
        g.setColumnStretch(1, 1)
        self._sys_line = QLabel("—"); self._sys_line.setObjectName("KVSub")
        self._sys_line.setWordWrap(True)
        g.addWidget(self._sys_line, 0, 0, 1, 2)
        for i, (key, label) in enumerate(DETAIL_ROWS, start=1):
            k = QLabel(label); k.setObjectName("KVKey")
            k.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            v = QLabel("—"); v.setObjectName("KVVal"); v.setWordWrap(True)
            g.addWidget(k, i, 0)
            g.addWidget(v, i, 1)
            self._detail[key] = v
        # 末尾留一行「弹簧」：卡片变高时多余高度落在表格下面，
        # 否则 QGridLayout 会把 8 行平摊拉开（行距越往下越大，很难看）
        g.setRowStretch(len(DETAIL_ROWS) + 1, 1)
        dy.setContentsMargins(16, 6, 16, 6)
        # ⚠️ 这 8 行的值是 wordWrap 的多行文本（显卡名、显示器型号、网卡名都换行），
        # 于是卡片的 sizeHint 会随文本长度涨到 400+（实测 1366×728 下 413px），
        # 把整个概览页顶出视口（滚动条 max=58~87），而其余大尺寸都装得下。
        # 包一层 QScrollArea：卡片可以被布局压到任意高度，内容装不下就**在卡内滚动**，
        # 页面本身永不溢出；大屏内容装得下时不显示滚动条，视觉与以前完全一致。
        # ⚠️ 这 8 行的值是 wordWrap 的多行文本（显卡名、显示器型号、网卡名都换行），
        # 于是卡片的 sizeHint 会随文本长度涨到 400+（实测 1366×728 下 413px），
        # 而 QVBoxLayout 在"各区块 sizeHint 之和已超过可用高度"时**不再按 stretch
        # 分配**，整页就被顶出视口（滚动条 max=58~87），其余大尺寸却都装得下。
        # CardScroll 把自身 sizeHint 高度压小 → 小屏时它愿意被压缩、内容在卡内滚动，
        # 页面永不溢出；大屏省下的空间按 stretch 分回来，卡片照样完整显示。
        det_scroll = CardScroll()
        det_scroll.setObjectName("CardScroll")
        det_scroll.setWidgetResizable(True)
        det_scroll.setFrameShape(QFrame.NoFrame)
        det_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        det_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        det_inner = QWidget()
        det_inner.setObjectName("CardScrollInner")
        det_inner.setLayout(g)
        det_scroll.setWidget(det_inner)
        dy.addWidget(det_scroll)
        # 硬件信息读取失败时显示（默认隐藏，成功时不占视觉位置）
        self.lb_hw_fail = QLabel("")
        self.lb_hw_fail.setObjectName("Muted")
        self.lb_hw_fail.setWordWrap(True)
        self.lb_hw_fail.setVisible(False)
        dy.addWidget(self.lb_hw_fail)
        mid.addWidget(det_card, 2)

        # 右：性能状态 —— 五轴雷达（处理器/内存/显卡/磁盘/网络）
        st_card, sv = card("性能状态")
        sv.setContentsMargins(16, 8, 16, 8); sv.setSpacing(6)
        # 雷达默认 setMinimumSize(200,200)，会把卡片的最小高度锁在 244px —— 那是
        # 小屏溢出的第二个来源（1280×690 视口只有 570）。这里给一个更小的下限，
        # 并允许垂直方向被压缩：雷达按实际尺寸自适应绘制，小窗下小一点但仍是完整五轴。
        self.radar = RadarChart(st_card, dark=win.dark, accent="BLUE",
                                labels=["处理器", "内存", "显卡", "磁盘", "网络"],
                                size=int(124 * (getattr(win, "scale", 1.0) or 1.0)))
        self.radar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.radar.setToolTip("五项占用率雷达图：处理器 / 内存 / 显卡 / 磁盘 / 网络")
        sv.addWidget(self.radar, 1)
        self._stat_rows = {}
        st_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        mid.addWidget(st_card, 1)
        root.addLayout(mid, 2)
        self._anim_parts += [det_card, st_card]

        # ---- 设备区：磁盘活动（左，占 2/3） / 快速操作（右） ----
        # 「GPU 详情」卡已删：显卡的实时占用与温度在顶部「显卡」指标卡（下拉可切
        # GPU0/GPU1…），型号与显存在「详细信息」表里，这里不必再单开一张卡。
        dev_row = QHBoxLayout(); dev_row.setSpacing(12)
        self.diskio_val = QLabel("—"); self.diskio_val.setObjectName("MetricSub")

        # 左：存储分析 —— 各分区占用（彩色条）+ 物理磁盘读写活动
        store_card, stv = card("存储分析", right=self.diskio_val)
        self.disk_box = QVBoxLayout(); self.disk_box.setSpacing(5)
        self.diskact_box = QVBoxLayout(); self.diskact_box.setSpacing(3)
        stv.setContentsMargins(16, 8, 16, 8); stv.setSpacing(6)
        stv.addLayout(self.disk_box)
        stv.addLayout(self.diskact_box)
        dev_row.addWidget(store_card, 2)

        act_card, av = card("快速优化")
        av.setContentsMargins(16, 8, 16, 8); av.setSpacing(8)
        self.btn_quick = btn("内存整理", "Primary", on_click=self.do_trim)
        self.btn_quick.setToolTip(
            "<b>内存整理</b><br>"
            "把所有占用超过 8&nbsp;MB 的进程的工作集换出到页面文件，<br>"
            "让任务管理器里的「内存占用」立刻降下来。<br><br>"
            "适合内存吃紧、开着一堆浏览器标签时临时腾空间。<br>"
            "注意：被换出的数据下次要用还得从磁盘读回来，<br>"
            "所以整理完头几秒会略卡；系统本身也会按需管理内存，<br>"
            "这不是必须定期做的操作。")
        av.addWidget(self.btn_quick)
        b_net = btn("网络诊断", on_click=lambda: self.win.goto("network"))
        b_net.setToolTip("<b>网络诊断</b><br>跳到「网络修复」页，逐项检查 DNS、网关、"
                         "代理与连通性，<br>并给出可一键执行的修复动作。")
        av.addWidget(b_net)
        av.addStretch(1)
        dev_row.addWidget(act_card, 1)
        root.addLayout(dev_row, 1)
        self._anim_parts += [store_card, act_card]

        self.mem_pct = 0.0
        self._net = {}
        self._net_rx = None
        self._net_tx = None
        self._cpu_base = ""      # 「12 核 / 16 线程」，refresh 填充
        self._cpu_model = ""     # CPU 型号简称，refresh 填充
        self._cpu_full = ""      # CPU 全名（详细信息表用），refresh 填充
        self._cpu_cores = ""     # 「12核/16线程」（紧凑版，指标条用），refresh 填充
        self._cpu_mhz = 0        # CPU 标称基准频率(MHz)，refresh 填充
        self._cpu_temp = None    # 实时 CPU 温度，tick_perf 填充
        # 控件复用缓存：结构不变时只更新数值，避免每秒重建导致的闪烁
        self._disk_rows = []
        self._disk_rows_sig = None
        self._mem_mods = []      # 内存条明细，refresh 填充
        self._os_txt = ""        # 操作系统一行
        self._sys_tip = ""
        self._user = ""
        self._host = ""
        self._up_base = None     # 运行时长基准：(读到时的秒数, 读到的本地时刻)
        self._up_txt = ''        # 上一次渲染的运行时长文本（变了才重绘）
        # 1s：CPU / 内存（纯 ctypes，零开销）
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        # 1s：温度 / GPU / 磁盘（服务端 PDH 实时采样，开销 <1ms）
        self._temp_timer = QTimer(self)
        self._temp_timer.setInterval(1000)
        self._temp_timer.timeout.connect(self.tick_perf)
        self._temp_timer.start()
        # 1s：网络速率（服务端常驻采样线程）
        self._net_timer = QTimer(self)
        self._net_timer.setInterval(1000)
        self._net_timer.timeout.connect(self._load_net)
        self._net_timer.start()
        self.tick()
        self.tick_perf()
        self._load_net()

    def first_show(self):
        """首次进入：一次编排好的入场序列（卡片错峰 70ms 淡入）。

        只在首次播放 —— 之后切回本页不再重播，否则每次切页都闪一遍很烦。
        """
        if not self.loaded:
            self.loaded = True
            self.refresh()
            for i, w in enumerate(self._anim_parts):
                fade_in(w, i * 70)

    def _on_metric_pick(self, key):
        """指标卡上的下拉：显卡选 GPU0/GPU1…，网络选本机某张网卡。"""
        pick = self.metrics.get(key, {}).get("pick")
        if pick is None:
            return
        val = pick.currentData() or ""
        if key == "gpu":
            self.gpu_metric_sel = val
            self.tick_perf()          # 立即按新选显卡重画
        else:
            self.net_sel = val
            self._apply_net_selection()

    def _tick_uptime(self):
        """运行时长本地推算：不再向系统重读，数值依旧在走（写在「详细信息」首行）。"""
        if self._up_base is None:
            return
        txt = fmt_uptime(self._up_base[0] + (time.time() - self._up_base[1]))
        if txt and txt != self._up_txt:
            self._up_txt = txt
            self._render_sys_line()

    def _render_sys_line(self):
        """详细信息表首行：操作系统 · 运行时长 · 当前用户。"""
        if self._sys_line is None:
            return
        up = self._up_txt or ""
        parts = [self._os_txt,
                 ("已运行 %s" % up) if up else "",
                 ("用户 %s" % self._user) if self._user else "",
                 self._host]
        self._sys_line.setText(" · ".join([p for p in parts if p]) or "—")
        self._sys_line.setToolTip(self._sys_tip)
    
    def tick(self):
        """1s 心跳：CPU / 内存 / 网络 —— 数值全部走补间，避免每秒硬跳。"""
        if not page_active(self):
            return
        try:
            self._tick_uptime()      # 运行时长本地推算（放在 try 内防止意外抛出中断心跳）
            m = S.mem_status()
            cpu = S.cpu_percent()
            pct = (m.ullTotalPhys - m.ullAvailPhys) * 100.0 / m.ullTotalPhys
            self.mem_pct = pct
            self._v_cpu, self._v_mem = cpu, pct          # 雷达图用

            # 网络：上下行合计（Mbps）
            net = None
            if self._net_rx is not None or self._net_tx is not None:
                net = (self._net_rx or 0.0) + (self._net_tx or 0.0)

            mt = self.metrics["cpu"]
            mt["tween"].to(cpu)
            mt["bar_tween"].to(cpu)          # 环形同样走 value 属性，复用同一套补间

            mt = self.metrics["mem"]
            mt["tween"].to(pct)
            mt["bar_tween"].to(pct)
            _cls(mt["bar"], pct, "Mem")
            mt["sub"].setText("%s / %s" % (fmt_bytes(m.ullTotalPhys - m.ullAvailPhys),
                                           fmt_bytes(m.ullTotalPhys)))
            _mtip = self._mem_tip(m)
            for _w in (mt["val"], mt["sub"], mt["bar"], mt.get("cell")):
                if _w is not None:
                    _w.setToolTip(_mtip)

            if net is not None:
                mt = self.metrics["net"]
                mt["tween"].to(net)
                cur = self._net_cur or {}
                try:
                    link = float(cur.get("link_mbps") or 0)
                except (TypeError, ValueError):
                    link = 0.0
                # 折线记的是实时速率本身（占用率看不出走势，曲线才有意义）
                if mt.get("spark") is not None:
                    mt["spark"].push(net)
                nm = short_hw_name(cur.get("name") or cur.get("kind") or "网络")
                state = "" if cur.get("up", True) else "未连接"
                tail = fmt_linkspeed(link) if link else (state or "速率未知")
                mt["sub"].setText("%s · %s" % (nm, tail))
                mt["sub"].setToolTip("%s\n↓ %s   ↑ %s"
                                     % (cur.get("desc") or cur.get("name") or "",
                                        fmt_rate(self._net_rx or 0.0),
                                        fmt_rate(self._net_tx or 0.0)))
            elif (self._net or {}).get("warming"):
                self.metrics["net"]["sub"].setText("采样准备中…")
            self._update_radar()
        except Exception:
            pass
    def _update_radar(self):
        """把五项占用率喂给「性能状态」雷达，并刷新三条结论式状态。

        数值全部来自已有采样（tick 给 CPU/内存，tick_perf 给 GPU/磁盘），
        这里只做一次映射与一次 update()，不新增任何采样开销。
        """
        radar = getattr(self, "radar", None)
        if radar is None:
            return
        cpu = float(getattr(self, "_v_cpu", 0) or 0)
        mem = float(getattr(self, "_v_mem", 0) or 0)
        gpu = getattr(self, "_v_gpu", None)
        dsk = getattr(self, "_v_disk", None)
        cur = self._net_cur or {}
        try:
            link = float(cur.get("link_mbps") or 0)
        except (TypeError, ValueError):
            link = 0.0
        rx, tx = (self._net_rx or 0.0), (self._net_tx or 0.0)
        if link > 0:
            netpct = min(100.0, (rx + tx) * 100.0 / link)
        else:
            netpct = 30.0 if (rx + tx) > 0 else 0.0        # 无协商速率时按「有流量」兜底
        radar.set_values([cpu, mem, gpu or 0, dsk or 0, netpct])

        # 卡片里的三行状态文字已删，这里只负责雷达

    def _mem_tip(self, m=None):
        """内存格子的悬停参数：物理内存 / 提交 / 虚拟内存 / 内存条明细。"""
        try:
            m = m or S.mem_status()
        except Exception:
            return ""
        tot, avail = m.ullTotalPhys, m.ullAvailPhys
        rows = [("物理内存", "%s / %s" % (fmt_bytes(tot - avail), fmt_bytes(tot))),
                ("可用", "%s （使用率 %d%%）" % (fmt_bytes(avail), m.dwMemoryLoad))]
        if m.ullTotalPageFile:
            rows.append(("提交", "%s / %s" % (
                fmt_bytes(m.ullTotalPageFile - m.ullAvailPageFile),
                fmt_bytes(m.ullTotalPageFile))))
        agg = {}                       # 同型号的条子合并成一行 ×N
        for x in (getattr(self, "_mem_mods", None) or []):
            key = (x.get("capacity"), x.get("speed"), x.get("type"), x.get("manufacturer"))
            agg[key] = agg.get(key, 0) + 1
        for (cap, spd, typ, man), cnt in agg.items():
            rows.append(("内存条", "%s %s %s（%s）%s"
                         % (fmt_bytes(cap) if cap else "", typ or "",
                            ("%d MHz" % spd) if spd else "",
                            S.vendor_cn(man) or "未知品牌",
                            (" × %d" % cnt) if cnt > 1 else "")))
        w = max(len(k) for k, _ in rows)
        return "\n".join("%s  %s" % (k.ljust(w), v) for k, v in rows)

    def tick_perf(self):
        """1s 心跳：温度 / GPU / 磁盘（服务端 PDH 实时采样）+ 刷新右侧设备卡片。"""
        if not page_active(self):
            return

        def clear_box(box):
            while box.count():
                it = box.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()

        def make_disk_row(box, name):
            """磁盘行：名称 + 利用率条 + 「读 x / 写 y」，一行放完。"""
            row = QWidget()
            rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)
            lb = QLabel(name); lb.setObjectName("Muted")
            pb = QProgressBar(); pb.setRange(0, 100)
            pb.setValue(0)
            pb.setTextVisible(False); pb.setObjectName("Disk")
            io = QLabel("读 —   写 —"); io.setObjectName("Mono")
            io.setMinimumWidth(168)
            io.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rl.addWidget(lb, 0); rl.addWidget(pb, 1); rl.addWidget(io, 0)
            box.addWidget(row)
            return {"row": row, "bar": pb, "io": io}

        def got(d):
            gpu_agg, disk_agg, ct, gt = d["gpu"], d["disk"], d["cpu_temp"], d["gpu_temp"]
            # ---- 主指标：显卡（下拉可选 GPU0 / GPU1 …；默认独显）----
            gpus = d.get("gpus") or []
            named = [g for g in gpus if g.get("name")]
            self._gpus = named
            gsig = tuple(g.get("name") for g in named)
            if gsig != self._gpu_names_sig:
                self._gpu_names_sig = gsig
                pick = self.metrics["gpu"].get("pick")
                if pick is not None:
                    pick.blockSignals(True)
                    pick.clear()
                    for i, g in enumerate(named):
                        pick.addItem("GPU%d" % i, g.get("name"))
                    ix = pick.findData(self.gpu_metric_sel)
                    if ix < 0:                     # 没选过 → 默认独显（其次第一块）
                        dgs = [i for i, g in enumerate(named) if is_dgpu(g["name"])]
                        ix = dgs[0] if dgs else 0
                        if named:
                            self.gpu_metric_sel = named[ix].get("name")
                    if named:
                        pick.setCurrentIndex(ix)
                    pick.blockSignals(False)
                self._render_detail_gpu()

            mt = self.metrics["gpu"]
            shown = next((g for g in named if g.get("name") == self.gpu_metric_sel), None)
            if shown is None and named:
                dgs = [g for g in named if is_dgpu(g["name"])]
                shown = max(dgs or named, key=lambda g: (g.get("util") or 0))
            if shown is None:
                # 一块都没枚举出来：退回聚合值（老行为）
                mt["tween"].to(gpu_agg)
                mt["bar_tween"].to(gpu_agg or 0)
                _cls(mt["bar"], gpu_agg, "Gpu")
                mt["sub"].setText("未检测到显卡")
            else:
                u = shown.get("util")
                mt["tween"].to(u)
                mt["bar_tween"].to(u or 0)
                _cls(mt["bar"], u, "Gpu")
                tp = shown.get("temp")
                mt["sub"].setToolTip("%s%s" % (shown["name"],
                                               ("\n共 %d 块显卡" % len(named))
                                               if len(named) > 1 else ""))
                mt["sub"].setText(" · ".join(
                    [x for x in (gpu_short(shown["name"]),
                                 fmt_temp(tp) if tp is not None else "温度不可读") if x]))

            # ---- 主指标：处理器（型号简称 · 核线程 · 基准频率 · 温度）----
            self._cpu_temp = ct
            # 基准频率是静态值（悬停提示里有），不占这一行
            self.metrics["cpu"]["sub"].setText(" · ".join(
                [x for x in (getattr(self, "_cpu_model", ""),
                             getattr(self, "_cpu_cores", ""),
                             ("%.0f°C" % ct) if ct is not None else "")
                 if x]))

            # ---- 磁盘活动聚合值挂到卡片标题右侧 ----
            self.diskio_val.setText("读写活跃 %s" % (fmt_pct(disk_agg)
                                                if disk_agg is not None else "—"))

            # per-disk activity bars（含读写速率）—— 同样复用控件
            disks = d.get("disks") or []
            dsig = tuple(dk.get("name") or "Disk%d" % i for i, dk in enumerate(disks))
            if dsig != self._disk_rows_sig:
                self._disk_rows_sig = dsig
                clear_box(self.diskact_box)
                self._disk_rows = []
                for i, dk in enumerate(disks):
                    full = dk.get("name") or ("磁盘%d" % i)
                    # 服务端给的名字形如「0 C: D:」——行首只留序号，盘符已在「存储」里
                    ref = make_disk_row(self.diskact_box,
                                        "磁盘%s" % (full.split(" ")[0] or i))
                    ref["row"].setToolTip("物理磁盘 %s" % full)
                    self._disk_rows.append(ref)
            for ref, dk in zip(self._disk_rows, disks):
                u = dk.get("util")
                ref["bar"].setValue(int(u) if u is not None else 0)
                _cls(ref["bar"], u, "Disk")
                ref["io"].setText("读 %s   写 %s" % (fmt_mbps(dk.get("read_mbps")),
                                                   fmt_mbps(dk.get("write_mbps"))))

            self._v_gpu, self._v_disk = gpu_agg, disk_agg
            self._update_radar()

            # CPU 温度并入「处理器」指标卡副标题（GPU 温度在显卡卡副标题里，见上）
            base = getattr(self, "_cpu_base", "")
            if base:
                sub_cpu = self.metrics["cpu"]["sub"]
                if ct is None:
                    sub_cpu.setText(base)
                    sub_cpu.setStyleSheet("")
                else:
                    sub_cpu.setText("%s · 温度 %s" % (base, fmt_temp(ct)))
                    col = (T.get_color("ERR", self.win.dark) if ct >= 85 else
                           T.get_color("WARN", self.win.dark) if ct >= 75 else "")
                    sub_cpu.setStyleSheet(("color: %s;" % col) if col else "")
        # 1s 一次，失败不能弹框（否则每秒一个警告）
        Tasks.run(self, S.perf_stats, got,
                  on_fail=lambda *_: None)  # 刻意静默：1 秒轮询，不能弹框

    def refresh(self):
        def got(d):
            # ---- 处理器（详细信息「处理器」行 + 指标卡副标题）----
            self._cpu_base = "%d 核 / %d 线程" % (d["cores"], d["logical"])
            self._cpu_cores = "%d核/%d线程" % (d["cores"], d["logical"])
            self._cpu_mhz = d.get("base_mhz") or 0
            self._cpu_model = short_cpu_name(d["cpu"] or "")
            self._cpu_full = d["cpu"] or ""
            self._cpu_temp = None
            self._set_detail("cpu", "%s （%s）" % (self._cpu_full or self._cpu_model or "—",
                                                  self._cpu_base))
            cpu_mt = self.metrics["cpu"]
            cpu_mt["sub"].setText(self._cpu_model or self._cpu_base)
            cpu_mt["sub"].setToolTip(
                "%s\n%s · 基准 %s\n温度取决于硬件与驱动支持（CPU 走 ACPI/WMI 热区，N 卡走 nvidia-smi）"
                % (d["cpu"] or "", self._cpu_base, fmt_ghz(self._cpu_mhz)))
            self.metrics["gpu"]["sub"].setText("温度不可读")

            # ---- 系统行：操作系统 · 运行时长 · 当前用户（合一行，省高度）----
            _os = d["os"]
            _rel = _os.get("release") or ""
            _bits = "64 位" if "64" in (_os.get("arch") or "") else "32 位"
            _cap = _os["caption"] or "—"
            if _cap.lower().startswith("microsoft "):
                _cap = _cap[10:]
            self._os_txt = " · ".join([x for x in (_cap, _rel, _bits) if x])
            self._sys_tip = _os.get("caption") or ""
            self._user = d["user"] or ""
            self._host = d["host"] or ""
            try:
                self._up_base = (float(d["uptime"]), time.time())
            except (TypeError, ValueError):
                self._up_base = None
            self._up_txt = ""
            self._tick_uptime()

            # ---- 存储：细条 + 统一强调色（只在快满时转警示，避免一排彩虹条）----
            while self.disk_box.count():
                it = self.disk_box.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            for x in d["disks"]:
                row = QWidget()
                rl = QVBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)
                h = QHBoxLayout(); h.setSpacing(8)
                a = QLabel(x["drive"]); a.setObjectName("KVVal")
                b = QLabel("余 %s / %s" % (fmt_bytes(x["free"]), fmt_bytes(x["total"])))
                b.setObjectName("KVSub")
                h.addWidget(a); h.addStretch(1); h.addWidget(b)
                pb = QProgressBar(); pb.setRange(0, 100); pb.setValue(int(x["percent"]))
                pb.setTextVisible(False)
                # 统一强调色；只有真的快满（>90%）才转警示红 —— 一排彩虹条最像 AI 生成
                if x["percent"] > 90:
                    pb.setObjectName("Err")
                rl.addLayout(h); rl.addWidget(pb)
                self.disk_box.addWidget(row)
            self.disk_box.addStretch(1)   # 盘少的机器：行贴顶，别让卡片中段空一大块

        def _info():
            b = S.sys_basic()
            return {"os": {"caption": b["caption"], "version": b["version"], "arch": b["arch"],
                           "release": b.get("release") or "",      # 24H2 / 25H2
                           "build_full": b.get("build_full") or ""},  # 26200.9168
                    "cpu": b["cpu"], "cores": b["cores"], "logical": b["logical"],
                    "base_mhz": b.get("base_mhz") or 0,            # CPU 标称基准频率
                    "disks": S.disks(), "uptime": S.uptime_seconds(),
                    "boot": S.boot_time(),
                    "host": S.socket.gethostname(), "user": os.environ.get("USERNAME", "")}
        # 读一遍就够：系统信息 / 磁盘清单第一次进来时读全，之后怎么切页都不再重读
        Tasks.run_cached(self, "info", _info, got)
        # 主板 / 显示器 / 内存条 / 物理磁盘 / 声卡都要起 WMI 查询，延后 2.5 秒再跑：
        # 首屏那批已经有 5~7 个子进程，再挤进来一批，弱机上就是"一打开整机卡住"
        QTimer.singleShot(2500, self._load_hw_info)

    # ---- 硬件标识：延后加载，避开启动那一波并发 ----
    def _load_hw_info(self):
        """主板 / 显示器 / 内存条 / 物理磁盘 / 声卡 / 显存 —— 都是"读一次就够"的静态信息。

        单独放到页面显示 2.5 秒之后再跑：首屏那批（系统信息 + 性能采样 + 网卡枚举）
        本身已经有 5~7 个子进程，再挤进来一批，弱机上会出现"一打开整机卡住"。
        这些数据全部只取一次并缓存，挪后不影响任何体验。
        """
        def _hw():
            return {"board": S.baseboard(), "monitors": S.monitors(),
                    "modules": S.memory_modules(), "extra": S.hw_extra(),
                    "vram": S.gpu_vram()}
        Tasks.run_cached(self, "hw", _hw, self._fill_hw,
                         on_fail=lambda m: self.load_fail(
                             self.lb_hw_fail, "硬件信息", m))

    # ---------- 「详细信息」表：一行一个主题，值可多行 ----------
    def _refit_details(self):
        """硬件信息到齐后再整体校正一次「详细信息」各行高度。

        `fit_labels` 只在窗口构建时跑（那时这些标签还是空文本，量不到东西），
        所以数据到了必须自己再量一次 —— 此时布局已稳定、宽度是真实值。
        """
        try:
            fit_labels(self)
        except Exception:
            pass

    def _set_detail(self, key, text):
        lb = self._detail.get(key)
        if lb is None:
            return
        text = text or "—"
        if lb.text() == text:       # 没变就不写，避免每秒重排
            return
        lb.setText(text)
        fit_label(lb)               # 按当前宽度重算高度：折行后网格若按 sizeHint
                                    # 分配会把行压扁（实测 14px 一行），文字被裁

    def _fill_hw(self, d):
        d = d or {}
        _bb = d.get("board") or {}
        self._oem = S.vendor_cn(_bb.get("manufacturer") or "") or ""
        self._detail["board"].setToolTip(
            "%s\n%s" % (_bb.get("manufacturer") or "", _bb.get("product") or ""))
        self._set_detail("board", _bb.get("label") or "—")

        # 显示器：真屏逐条列出；虚拟显示器（型号被伪造成 HDMI2.0 / DP 这类接口名）滤掉
        mons = d.get("monitors") or []
        self._mons = mons
        real = [m for m in mons
                if not (m.get("name") or "").strip().lower().startswith(
                    ("hdmi", "dp", "dvi", "vga", "displayport", "generic"))]
        self._set_detail("mon", "\n".join(
            self._mon_line(m) for m in (real or mons[:1])) or "—")

        self._mem_mods = d.get("modules") or []
        self._vram = d.get("vram") or {}
        self._ex = d.get("extra") or {}

        QTimer.singleShot(0, lambda: self._refit_details())
        self._set_detail("mem", self._mem_line())
        self._set_detail("disk", self._disk_line())
        self._set_detail("sound", self._sound_line())
        self._render_detail_gpu()
        self._render_detail_net()

    @staticmethod
    def _mon_line(m):
        """显示器一行：型号 [面板厂 PNP] （尺寸英寸），对齐效果图排版。"""
        nm = m.get("name") or m.get("pnp") or "—"
        pnp, ven = m.get("pnp") or "", m.get("vendor") or ""
        tag = ("[%s %s]" % (ven, pnp)).strip() if (ven or pnp) else ""
        inch = ("（%s 英寸）" % m["inch"]) if m.get("inch") else ""
        return " ".join([x for x in (nm, tag, inch) if x])

    def _mem_line(self):
        """内存一行：品牌 总容量 类型 频率 （单条 + 单条）。"""
        mods = self._mem_mods or []
        if not mods:
            return "—"
        total = sum((x.get("capacity") or 0) for x in mods)
        x0 = mods[0]
        sticks = [fmt_bytes(x.get("capacity") or 0) for x in mods]
        parts = [S.vendor_cn(x0.get("manufacturer") or "") or "",
                 fmt_bytes(total) if total else "",
                 x0.get("type") or "",
                 ("%sMHz" % x0["speed"]) if x0.get("speed") else "",
                 ("（%s）" % " + ".join(sticks)) if len(sticks) > 1 else ""]
        return " ".join([p for p in parts if p]) or "—"

    @staticmethod
    def _cap(n):
        """磁盘容量按「整 GB / TB」显示（512GB / 1024GB）—— 对齐系统信息工具的惯例。"""
        try:
            n = float(n or 0)
        except (TypeError, ValueError):
            return ""
        if n <= 0:
            return ""
        return ("%.0f TB" % (n / 1e12)) if n >= 1e12 else ("%.0f GB" % (n / 1e9))

    def _disk_line(self):
        """磁盘：物理盘型号 + 容量，一块一行（对齐效果图）。"""
        ds = (self._ex or {}).get("disks") or []
        if not ds:
            return "—"
        out = []
        for x in ds:
            sz = self._cap(x.get("size"))
            iface = (x.get("iface") or "").upper()
            pre = "USB " if iface == "USB" else ""
            out.append("%s%s%s" % (pre, (x.get("model") or "").strip(),
                                   ("（%s）" % sz) if sz else ""))
        return "\n".join(out) or "—"

    def _sound_line(self):
        """声卡：滤掉虚拟音频设备（Steam / 网易 / 虚拟声卡），一块一行。"""
        ss = (self._ex or {}).get("sounds") or []
        skip = ("virtual", "steam streaming", "wave extensible", "voice",
                "vb-audio", "broadcast", "智音技术", "虚拟")
        real = [x for x in ss if not any(k in (x.get("name") or "").lower()
                                         or k in (x.get("name") or "")
                                         for k in skip)]
        use = real or ss[:2]
        return "\n".join((x.get("name") or "").strip() for x in use) or "—"

    def _render_detail_gpu(self):
        """显卡：每块卡一行 —— 型号（显存 / 整机厂商）。"""
        gpus = self._gpus or []
        if not gpus:
            return
        out = []
        for g in gpus:
            nm = g.get("name") or ""
            mem = (self._vram or {}).get(nm) or 0
            if not mem:      # 核显在注册表 qwMemorySize 上常是 0 → 退回 WMI AdapterRAM
                low = nm.lower()
                for vc in (self._ex or {}).get("vcs") or []:
                    vn = (vc.get("name") or "").lower()
                    if vn and (vn in low or low in vn):
                        mem = vc.get("ram") or 0
                        break
            bits = []
            if mem:
                g = mem / 1073741824.0
                bits.append(("%.1f GB" % g) if abs(g - round(g)) > 0.05
                            else ("%.0f GB" % round(g)) if g >= 1
                            else ("%.0f MB" % (mem / 1048576.0)))
            if self._oem:
                bits.append(self._oem)
            out.append("%s%s" % (nm, ("（%s）" % " / ".join(bits)) if bits else ""))
        self._set_detail("gpu", "\n".join(out) or "—")

    def _render_detail_net(self):
        """网卡：真实网卡逐条列出（虚拟网卡 / 蓝牙 PAN / 加速器 TAP 不算）。"""
        d = self._net or {}
        items = d.get("items") or []
        if not items:
            return
        out = []
        for x in items:
            nm = (x.get("desc") or x.get("name") or "").strip()
            out.append("%s%s" % (nm, "" if x.get("up") else "（未连接）"))
        self._set_detail("net", "\n".join(out) or "—")

    def do_trim(self):
        if not confirm(self, "整理内存",
                       "将对所有占用 >8MB 的进程执行 SetProcessWorkingSetSize(-1,-1)，"
                       "把工作集换出到页面文件。\n\n"
                       "说明：内存占用数字会立刻下降，但被换出的数据下次访问要从磁盘读回，"
                       "可能反而变慢。系统本身会按需管理内存。"):
            return
        Tasks.run(self, S.trim_all,
                  lambda r: info(self, "已整理 %d / %d 个进程。\n\n"
                                       "（其余为系统进程或权限不足，已跳过）" % r))

    # ---- 网络指标卡：可切换本机在用的不同网卡 ----
    def _load_net(self):
        """后台取一次网卡信息（含每张网卡各自的实时速率）。"""
        if not page_active(self):
            return          # 停在别的页面 / 窗口最小化时不必每秒起线程
        Tasks.run(self, S.net_info, self._got_net,
                  on_fail=lambda *_: None)  # 刻意静默：1 秒轮询，不能弹框

    @staticmethod
    def _net_pick_label(x):
        icon = "📶" if (x.get("kind") == "WiFi") else "🔌"
        return "%s %s" % (icon, x.get("name") or "网络")

    def _got_net(self, d):
        d = d or {}
        self._net = d
        items = d.get("items") or []
        # 网卡名单变了才重建下拉（每秒重建会闪）
        sig = tuple(x.get("name") for x in items)
        if sig != self._net_names_sig:
            self._net_names_sig = sig
            pick = self.metrics["net"].get("pick")
            if pick is not None:
                pick.blockSignals(True)
                pick.clear()
                for x in items:
                    pick.addItem(self._net_pick_label(x), x.get("name"))
                ix = pick.findData(self.net_sel)
                if ix < 0:
                    ix = 0
                if items:
                    pick.setCurrentIndex(ix)
                    self.net_sel = pick.itemData(ix)
                pick.blockSignals(False)
        self._render_detail_net()
        self._apply_net_selection()

    def _apply_net_selection(self):
        """把选中的那张网卡的速率算出来（不再单独起线程）。"""
        d = self._net or {}
        items = d.get("items") or []
        cur = next((x for x in items if x.get("name") == self.net_sel), None)
        primary = d.get("primary") or {}
        if cur is None:
            cur = primary
        self._net_cur = cur
        rx, tx = cur.get("rx_mbps"), cur.get("tx_mbps")
        if not items:                      # 没有 items（老路径）：退回主网卡的聚合值
            rx, tx = d.get("rx_mbps"), d.get("tx_mbps")
        # 首轮 PDH 建立期间（warming）没有样本，此时保持 None，
        # 让指标卡显示「准备中」而不是误导性的 0.00 Mbps
        if rx is None and tx is None and d.get("warming"):
            self._net_rx = None
            self._net_tx = None
        else:
            self._net_rx = rx if rx is not None else 0.0
            self._net_tx = tx if tx is not None else 0.0
        self.tick()


# ==========================================================================
# 2. 系统优化（问题4：两级结构；问题3：勾选状态持久化）
# ==========================================================================
class OptimizePage(Page):
    title = "系统优化"

    def __init__(self, win):
        super().__init__(win)
        self.rules = []
        self.sel = {}
        self.current_cat = None       # None=大类列表, 否则=分类key

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # 面包屑
        self.crumb = QHBoxLayout()
        self.lb_crumb = QLabel("全部大类")
        self.lb_crumb.setObjectName("CardTitle")
        self.crumb.addWidget(self.lb_crumb)
        self.crumb.addStretch(1)
        self.btn_back = btn("‹ 返回大类", on_click=self.go_root)
        self.btn_back.setVisible(False)
        self.crumb.addWidget(self.btn_back)
        root.addLayout(self.crumb)

        # ---- 一键开关（更新 / Defender，普通模式灰显） ----
        self._upd_enabled = None
        self._upd_blocked = False
        self._upd_reasons = []
        self._upd_status = {}          # update_status() 的完整返回
        self._def_enabled = None
        self.toggle_row = QHBoxLayout()
        self.toggle_row.setSpacing(12)
        f_upd, v_upd = card("🔄 Windows 更新")
        self.upd_pill = pill("检测中…", "blue")
        v_upd.addWidget(self.upd_pill)
        self.btn_upd = btn("暂停更新", "Danger", on_click=self.toggle_update)
        self.btn_upd.setEnabled(self.win.admin_mode)
        v_upd.addWidget(self.btn_upd)
        # 检测到「暂停 / WSUS 接管 / 策略关闭 / 服务被禁」时在这里如实说明原因
        self.upd_hint = QLabel("")
        self.upd_hint.setObjectName("Muted")
        self.upd_hint.setWordWrap(True)
        self.upd_hint.setVisible(False)
        v_upd.addWidget(self.upd_hint)
        v_upd.addWidget(notice(
            "「暂停更新」会把 Windows 更新<b>暂停到 2099 年</b>，"
            "和「设置 → Windows 更新 → 暂停更新」是同一个做法 —— "
            "<b>不影响更新服务</b>；随时点「恢复更新」立即解除。", "info"))
        # 两张卡被等高拉伸时，多余空间落到**底部**，别把中间的 notice 撑高
        # （撑高后文字被居中，就和右边那张卡的行距对不齐了）
        v_upd.addStretch(1)
        self.toggle_row.addWidget(f_upd, 1)
        f_def, v_def = card("🛡 Windows Defender")
        self.def_pill = pill("检测中…", "blue")
        v_def.addWidget(self.def_pill)
        self.btn_def = btn("全关闭 Defender", "Danger", on_click=self.toggle_defender)
        self.btn_def.setEnabled(self.win.admin_mode)
        v_def.addWidget(self.btn_def)
        # 篡改防护开着时在这里如实说明（它会把我们的写入改回去）
        self.def_hint = QLabel("")
        self.def_hint.setObjectName("Muted")
        self.def_hint.setWordWrap(True)
        self.def_hint.setVisible(False)
        v_def.addWidget(self.def_hint)
        # 组件明细：把「Windows 会自行改回」与「真的没禁成」**分开写清楚**。
        # 用户反馈「关了 Defender 却像没生效」—— 根因就是这里只报「7/8 已禁」，
        # 那 1 个恰恰是系统自己会改回的 WaaSMedicSvc。
        self.def_prog = QLabel("")
        self.def_prog.setObjectName("Muted")
        self.def_prog.setWordWrap(True)
        self.def_prog.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.def_prog.setVisible(False)
        v_def.addWidget(self.def_prog)
        # ⚠️ 这段是用户点名要的提示（他参考了别的工具那张图）：
        #    必须**先手动关掉「实时保护」和「篡改防护」**，全关闭才做得成；
        #    两个方向都要重启。链接直接跳到「病毒和威胁防护 → 设置」那一页。
        self.def_note = notice_html(
            "注意：<br>"
            "禁用前请点击 <a href=\"#\" style=\"color:%s;\">此处</a> "
            "打开「病毒和威胁防护」设置，手动关闭「<b>实时保护</b>」和"
            "「<b>篡改防护</b>」两项，全关闭才能成功！<br>"
            "无论全关闭还是恢复，都必须<b>重新启动计算机</b>后才能完全生效。"
            % T.get_color("BLUE", self.win.dark),
            "warn", on_link=lambda _u: self.open_def_settings())
        v_def.addWidget(self.def_note)
        # 完整清单（8 个服务各自的作用 + 两个前置的细节）放弹窗里 ——
        # 常驻的话卡片会长得压过左边那张卡，页面失衡。
        self.btn_def_list = btn("查看完整服务清单", on_click=self.show_def_list)
        v_def.addWidget(self.btn_def_list)
        v_def.addStretch(1)          # 同上：多余空间落底部，不撑高文字块
        self.toggle_row.addWidget(f_def, 1)
        root.addLayout(self.toggle_row)

        self.policy_note = notice("", "warn")
        root.addWidget(self.policy_note)

        self.lb_preread = QLabel("")
        self.lb_preread.setObjectName("Muted3")
        self.lb_preread.setWordWrap(True)
        root.addWidget(self.lb_preread)

        # 二级工具条（包一层 widget 才能整体显隐）
        self.bar2 = QWidget()
        bl2 = QHBoxLayout(self.bar2)
        bl2.setContentsMargins(0, 0, 0, 0)
        bl2.setSpacing(8)
        self.btn_apply = btn("应用所选", "Primary", on_click=self.apply_sel)
        self.btn_restore = btn("还原所选", on_click=self.restore_sel)
        self.btn_undo = btn("撤销上次优化", on_click=self.undo)
        self.btn_apply.setEnabled(self.win.admin_mode)
        self.btn_restore.setEnabled(self.win.admin_mode)
        self.btn_undo.setEnabled(self.win.admin_mode)
        bl2.addWidget(self.btn_apply)
        bl2.addWidget(self.btn_restore)
        bl2.addWidget(self.btn_undo)
        bl2.addStretch(1)
        bl2.addWidget(btn("全选本类", on_click=self.select_all_cat))
        bl2.addWidget(btn("全不选", on_click=self.clear_sel))
        self.bar2.setVisible(False)
        root.addWidget(self.bar2)

        # 手动选择提示行（已移除「只显示安全项」过滤开关：
        # 由用户自行勾选想要修改的项，策略项在列表里用【策略】标出）
        self.safe_row = QWidget()
        sl = QHBoxLayout(self.safe_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)
        self.lb_safe_hint = QLabel("")
        self.lb_safe_hint.setObjectName("Muted3")
        sl.addWidget(self.lb_safe_hint)
        sl.addStretch(1)
        root.addWidget(self.safe_row)

        # 大类区
        self.cat_scroll = QScrollArea()
        self.cat_scroll.setWidgetResizable(True)
        self.cat_scroll.setFrameShape(QFrame.NoFrame)
        cat_holder = QWidget()
        self.cat_grid = QGridLayout(cat_holder)
        self.cat_grid.setContentsMargins(0, 0, 0, 0)
        self.cat_grid.setSpacing(12)
        self.cat_scroll.setWidget(cat_holder)
        root.addWidget(self.cat_scroll, 1)

        # 小类区
        self.item_scroll = QScrollArea()
        self.item_scroll.setWidgetResizable(True)
        self.item_scroll.setFrameShape(QFrame.NoFrame)
        self.holder = QWidget()
        self.hl = QVBoxLayout(self.holder)
        self.hl.setContentsMargins(0, 0, 0, 0)
        self.hl.setSpacing(12)
        self.item_scroll.setWidget(self.holder)
        self.item_scroll.setVisible(False)
        root.addWidget(self.item_scroll, 1)

    def refresh(self):
        def got(rules):
            self.rules = rules
            # 只读预读取：仅统计系统当前值与优化目标一致的项数用于展示。
            # 绝不自动勾选、绝不写任何系统设置——软件打开时一切保持原样，
            # 勾选完全由用户手动进行（也不再持久化上次的勾选）。
            done = len([r for r in rules if r.get("state") is True])
            self.lb_preread.setText(
                "只读检测完成：当前有 %d 项的系统值与优化目标一致（仅为状态显示，"
                "本软件打开时未修改任何设置；所有项默认不勾选，由你手动选择）。" % done)
            self._update_safe_hint()
            self.build_root()
        # 每次进页都重算状态：规则状态读的是注册表，用户可能在本软件之外
        # 改过（组策略 / 设置 App / powercfg），缓存旧状态就会显示「我明明
        # 禁用了它还说可以禁用」。131 条全量重读实测 <0.5s，值得。
        self.sess_invalidate("rules")
        Tasks.run_cached(self, "rules", S.load_rules_with_state, got)
        self._load_toggles()

    # ---------- 一键开关：更新 / Defender ----------
    def _load_toggles(self):
        Tasks.run_cached(self, "toggles",
                         lambda: (S.update_status(), S.defender_status()),
                         self._toggles_done)

    def _toggles_done(self, r):
        up, df = r
        self._upd_enabled = None if up.get("enabled") is None else bool(up.get("enabled"))
        self._upd_blocked = bool(up.get("blocked"))
        self._upd_reasons = list(up.get("reasons") or [])
        # ⚠️ 整个 status 原样留着：pill 与按钮都要读 paused_long / paused_until
        # 来区分「暂停到 2099」（本工具的关闭态，可一键恢复）和「被禁用/策略关闭」。
        self._upd_status = dict(up or {})
        self._def_enabled = None if df.get("enabled") is None else bool(df.get("enabled"))
        # 整个 status 留着：篡改防护状态要在提示里用（它会把我们的写入改回去）
        self._def_status = dict(df or {})
        if hasattr(self, "def_hint"):
            tp = df.get("tamper")
            if tp:
                self.def_hint.setText(
                    "⚠ 「篡改防护」正在开启 —— 系统的设置改动可能被自动改回。"
                    "要调整实时保护，需先在「Windows 安全中心 → 病毒和威胁防护 "
                    "→ 管理设置」里关闭篡改防护。")
                self.def_hint.setStyleSheet(
                    "color: %s;" % T.get_color("WARN", self.win.dark))
                self.def_hint.setVisible(True)
            else:
                self.def_hint.setVisible(False)
        self._paint_toggles()

    def _upd_suppressed(self):
        """更新当前是否处于「用不了」的状态（决定按钮该是暂停还是恢复）。

        ⚠️ **不能只看 `_upd_enabled`** —— 它是「服务与策略是否放行」。
        「暂停到 2099」时服务没被禁、策略也没写，`_upd_enabled` 仍是 True，
        于是 `target = not _upd_enabled` 会算出「再暂停一次」：**按钮写着
        「恢复更新」，点下去却又暂停一遍**（用户实测反馈的就是这个）。
        暂停也算「用不了」，必须一起算进来。
        """
        upd = getattr(self, "_upd_status", {}) or {}
        return (not self._upd_enabled) or bool(upd.get("paused"))

    def _def_suppressed(self):
        """Defender 实时保护当前是否关着（决定按钮该是关还是开）。

        与 `_upd_suppressed()` 同一套写法：**按钮文字与点击动作读同一个判据**，
        避免出现「按钮写着开启、点下去却关闭」这类背离。
        `_def_enabled` 现在直接就是「实时保护是否开启」（server 返回的
        realtime 字段），所以判据很短 —— 但要留着这层，日后 server 侧
        语义变了只改这里一处。
        """
        return not self._def_enabled

    def _paint_toggles(self):
        def repaint(pill_w, text, on):
            pill_w.setText(text)
            pill_w.setObjectName("PillOk" if on else "PillWarn")
            pill_w.style().unpolish(pill_w)
            pill_w.style().polish(pill_w)
        if self._upd_enabled is None:
            self.upd_pill.setText("检测中…")
            self.btn_upd.setText("切换中…")
        else:
            on = bool(self._upd_enabled)
            rs = getattr(self, "_upd_reasons", [])
            # 状态分四种，不能一律写成「已开启 / 已关闭」：
            #   ① 服务+策略放行且没暂停        → 正常绿灯
            #   ② 暂停到 2099（本工具的「关闭」）→ 这是**主动**暂停，不是被禁
            #   ③ 被禁服务 / 策略关闭 / WSUS   → 警示色
            # 旧实现只有「开 / 关」两态，于是「已经开启」与「根本收不到更新」
            # 会同时成立，用户完全没法理解为什么没打补丁（本机就被暂停到 11-07）。
            upd = getattr(self, "_upd_status", {}) or {}
            paused_long = bool(upd.get("paused_long"))
            tag = ""
            if on and not paused_long and rs:
                if any("暂停" in x for x in rs):
                    tag = "（已暂停）"
                elif any("WSUS" in x for x in rs):
                    tag = "（WSUS 接管）"
                else:
                    tag = "（受限）"
            if not on:
                state = "已禁用"
            elif paused_long:
                # ⚠️ 别写成「已开启（已暂停到…）」—— 自相矛盾，
                # 用户会以为更新在正常工作。暂停就是暂停，直说。
                state = "已暂停到 %s" % (upd.get("paused_until") or "远期")
            else:
                state = "已开启"
            repaint(self.upd_pill, "当前：%s%s" % (state, tag),
                    on and not rs)          # 有附加原因时用警示色，别一律绿灯
            # 按钮文字与点击后的动作**必须读同一个判据**（`_upd_suppressed`），
            # 否则又会出现「按钮说恢复、实际暂停」这种自相矛盾。
            self.btn_upd.setText("恢复更新" if self._upd_suppressed()
                                 else "暂停更新")
        if hasattr(self, "upd_hint"):
            rs = getattr(self, "_upd_reasons", [])
            if rs and self._upd_enabled is not None:
                self.upd_hint.setText("⚠ " + "；".join(rs))
                self.upd_hint.setStyleSheet(
                    "color: %s;" % T.get_color("WARN", self.win.dark))
                self.upd_hint.setVisible(True)
            else:
                self.upd_hint.setVisible(False)
        if self._def_enabled is None:
            self.def_pill.setText("检测中…")
            self.btn_def.setText("切换中…")
            if hasattr(self, "def_prog"):
                self.def_prog.setVisible(False)
        else:
            # 「全关闭」是 8 个组件的事，只报一个总数用户看不出还差哪几个。
            # ⚠️ 更关键的是**别把「Windows 会自己改回」的组件算成失败** ——
            #    WaaSMedicSvc 会被更新医疗定期恢复成手动，一直把它算进「还差 1 个」，
            #    用户就会以为工具点了没用（2026-10-12 的实际反馈）。
            ds = getattr(self, "_def_status", {}) or {}
            n_dis, n_tot = ds.get("n_disabled"), ds.get("n_total")
            heals = list(ds.get("self_heal") or [])
            sticky = list(ds.get("sticky_remaining") or [])
            eff = ds.get("effectively_off")
            prog = ""
            if n_dis is not None and n_tot:
                prog = "（服务 %d/%d 已禁）" % (n_dis, n_tot)
            if self._def_enabled:
                repaint(self.def_pill, "当前：实时保护已开启" + prog, True)
            elif eff:
                repaint(self.def_pill, "当前：已实际关闭" + prog, False)
            elif eff is False:
                # 有组件没禁成 —— 但仍要说清实时保护/防病毒的实际状态
                rt_av = []
                if ds.get("realtime") is False:
                    rt_av.append("实时保护已关")
                if ds.get("antivirus") is False:
                    rt_av.append("防病毒已关")
                repaint(self.def_pill,
                        "当前：已关闭（%s）%s" % ("·".join(rt_av) or "未关全", prog),
                        False)
            else:
                repaint(self.def_pill, "当前：已关闭" + prog, False)
            # 组件明细：分开说「系统会改回」与「真没禁成」
            if hasattr(self, "def_prog"):
                parts = []
                if n_dis is not None and n_tot:
                    parts.append("8 个服务/驱动已禁 %d 个" % n_dis)
                if heals:
                    parts.append("%s 会被 Windows 自行改回（<b>属正常</b>，"
                                 "不影响实际关闭）" % "、".join(heals))
                if sticky:
                    parts.append("⚠ 仍未禁用：%s —— 需进<b>安全模式</b>再执行一次"
                                 % "、".join(sticky))
                if parts:
                    self.def_prog.setText("；".join(parts) + "。")
                    self.def_prog.setStyleSheet(
                        "color: %s;" % T.get_color("WARN" if sticky else "TEXT2",
                                                   self.win.dark))
                    self.def_prog.setVisible(True)
                else:
                    self.def_prog.setVisible(False)
            self.btn_def.setText("恢复 Defender" if self._def_suppressed()
                                 else "全关闭 Defender")

    def _toggle_done(self, results, verb, kind):
        """切换完成：**逐条如实报告**，有失败就说失败，不弹「已xxx」了事。"""
        res = results or []
        bad = [x for x in res if not x.get("ok")]
        self.sess_invalidate("toggles", "rules")
        self._load_toggles()
        if not bad:
            return info(self, "已%s%s（需重启生效）。" % (verb, kind))
        lines = ["成功 %d / %d 项：" % (len(res) - len(bad), len(res))]
        for x in bad[:6]:
            lines.append("· %s：%s" % (x.get("name") or "?", (x.get("note") or "")[:60]))
        warn(self, "部分项目未成功", "\n".join(lines))

    def toggle_update(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if self._upd_enabled is None:
            return
        # ⚠️ 判据必须与按钮文字一致：`_upd_suppressed()` 把「暂停」也算进去，
        # 否则暂停到 2099 时这里会算出「再暂停一次」—— 按钮写着「恢复更新」
        # 却什么都没恢复（用户实测反馈的 bug）。
        target = self._upd_suppressed()
        rs = getattr(self, "_upd_reasons", [])
        if target:
            verb = "恢复"
            extra = "\n\n「恢复」会清掉暂停标记，立即生效（不用重启）。"
            risk = ""
        else:
            verb = "暂停"
            extra = "\n\n会把 Windows 更新暂停到 2099-12-31。"
            risk = "\n暂停期间收不到安全补丁。随时可点「恢复更新」解除。"
        if not confirm(self, "暂停 / 恢复 Windows 更新",
                       "即将%s Windows 更新。%s%s" % (verb, extra, risk),
                       danger=not target):
            return
        self.win.busy("正在%s更新…" % verb)
        Tasks.run(self, lambda: S.set_update(target),
                  lambda r: (self.win.idle(),
                             self._toggle_done(r, verb, "更新"))[1],
                  on_fail=lambda m: (self.win.idle(),
                                     warn(self, "操作失败", m))[1])

    def open_def_settings(self):
        """打开「病毒和威胁防护 → 设置」（实时保护 / 篡改防护 在那里手动关）。"""
        ok, msg = S.open_defender_settings()
        if not ok:
            warn(self, "打不开设置页", msg)

    def show_def_list(self):
        """展示「全关闭 Defender 要动哪些服务」的完整清单。

        用户明确要这份说明：他早先手工关过 Defender，已经不记得是哪些服务了。
        常驻在卡片里会把页面压失衡，所以放弹窗。
        """
        info(self, "全关闭 Defender 要动的服务/驱动",
             S.defender_prereq_text())

    def toggle_defender(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if self._def_enabled is None:
            return
        # 与按钮文字读同一判据（避免「按钮说开启、点下去却关闭」）
        target = self._def_suppressed()
        if target:
            verb = "恢复"
            extra = ("\n\n会把 8 个服务/驱动恢复成原值（优先用关闭前记录的快照）、"
                     "删掉禁用策略、重新启用 4 个计划任务。\n"
                     "**需要重启**才真正恢复。")
            risk = ""
        else:
            verb = "全关闭"
            extra = ("\n\n会**全关闭** Defender：\n"
                     "· 8 个服务/驱动设为禁用（WinDefend / WdNisSvc / WdNisDrv /"
                     " WdFilter / WdBoot / Sense / SecurityHealthService /"
                     " WaaSMedicSvc）\n"
                     "· 写禁用策略键 + 反间谍键\n"
                     "· 禁用 4 个 Windows Defender 计划任务\n\n"
                     "其中两个是关键前置：**SecurityHealthService**（服务键对"
                     "管理员只读，本工具改走 SCM）与 **WaaSMedicSvc**"
                     "（不禁它会被自动改回来）。")
            risk = ("\n\n⚠️ 请先点本页的「此处」链接打开「病毒和威胁防护 → 设置」，"
                    "**手动关闭「实时保护」和「篡改防护」**，否则关不干净。"
                    "\n关闭后本机将**完全没有杀毒防护**，请确认你清楚风险。"
                    "\n随时可点「恢复 Defender」还原。"
                    "\n⚠️ 普通模式下 WinDefend 有 PPL 自我保护，**要重启才生效**；"
                    "若写入被拒或重启后复活，需进**安全模式**再执行一次。")
        if not confirm(self, "全关闭 / 恢复 Defender",
                       "即将%s Defender。%s%s" % (verb, extra, risk),
                       danger=not target):
            return
        # 全关闭要写 8 个服务/驱动 + 4 个策略键 + 4 个计划任务，逐个回读复核要好几秒。
        # 只写一句「正在关闭…」用户会以为卡住了 —— 让 server 逐条上报进度。
        state = {}
        spin = ProgressSpin(self.win, state, "正在%s Defender…" % verb)
        Tasks.run(self, lambda: S.set_defender(
            target, progress=lambda m: state.__setitem__("msg", m)),
                  lambda r: (spin.stop(),
                             self._toggle_done(r, verb, "Defender"))[1],
                  on_fail=lambda m: (spin.stop(),
                                     warn(self, "操作失败", m))[1])

    def on_exit(self):
        self.save_state()

    def save_state(self):
        # 不再持久化勾选：每次启动全部不勾选，杜绝"上次勾选静默恢复后被一键应用"
        self.win.settings.pop("optimize_sel", None)

    # ---------- 大类列表 ----------
    def build_root(self):
        self.current_cat = None
        self.lb_crumb.setText("全部大类")
        self.btn_back.setVisible(False)
        self.bar2.setVisible(False)
        self.item_scroll.setVisible(False)
        self.cat_scroll.setVisible(True)

        pol = len([r for r in self.rules if r.get("risk") == "policy"])
        if pol:
            self.policy_note.setText(
                "在大类里点进去，自行勾选要修改的优化项。标有【策略】的 %d 项会写入 SOFTWARE\\Policies，"
                "应用后 Windows 会显示“某些设置由你的组织管理”，请确认后再勾选。"
                "**每次打开本软件都不会替你勾选任何项**，需要时自己选。" % pol)
        else:
            self.policy_note.setText(
                "在大类里点进去，自行勾选要修改的优化项。当前规则库里没有策略项。"
                "**每次打开本软件都不会替你勾选任何项**，需要时自己选。")

        while self.cat_grid.count():
            it = self.cat_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        cats = []
        for r in self.rules:
            if r["category"] not in cats:
                cats.append(r["category"])
        # 一行三个横排 —— 与「清理与诊断」hub 页同一套卡片写法：
        # 顶行放 图标 + 名称 + ›（一眼扫完），下面放「N 项 · 已生效 M」，
        # 有勾选时再加一枚 pill。竖排卡片比长条更适合三列并排。
        for i, cat in enumerate(cats):
            lst = [r for r in self.rules if r["category"] == cat]
            done = len([r for r in lst if r.get("state") is True])
            picked = len([r for r in lst if self.sel.get(r["id"])])
            f = QFrame()
            f.setObjectName("CatCard")
            f.setCursor(Qt.PointingHandCursor)
            f.setMinimumHeight(88)
            v = QVBoxLayout(f)
            v.setContentsMargins(16, 12, 12, 12)
            v.setSpacing(6)
            top = QHBoxLayout()
            top.setSpacing(8)
            icon = QLabel(CAT_ICON.get(cat, "•"))
            icon.setObjectName("CatIcon")
            icon.setAlignment(Qt.AlignCenter)
            top.addWidget(icon)
            name = QLabel(CAT_NAME.get(cat, cat))
            name.setObjectName("CatName")
            name.setWordWrap(True)
            top.addWidget(name)
            top.addStretch(1)
            arrow = QLabel("›")
            arrow.setObjectName("CatArrow")
            arrow.setAlignment(Qt.AlignCenter)
            top.addWidget(arrow)
            v.addLayout(top)
            stat = QLabel("%d 项 · 已生效 %d" % (len(lst), done))
            stat.setObjectName("CatSub")
            v.addWidget(stat)
            if picked:
                pr = QHBoxLayout()
                pr.setContentsMargins(0, 0, 0, 0)
                pr.addWidget(pill("已勾选 %d" % picked, "blue"))
                pr.addStretch(1)
                v.addLayout(pr)
            v.addStretch(1)
            def open_cat(_, c=cat):
                self.open_category(c)
            f.mousePressEvent = open_cat
            f.setToolTip("点击进入「%s」" % CAT_NAME.get(cat, cat))
            self.cat_grid.addWidget(f, i // 3, i % 3)
        # 三列等宽；行不参与拉伸（多出的空间留给末尾，否则卡片会被纵向摊开）
        for _c in range(3):
            self.cat_grid.setColumnStretch(_c, 1)
        _rows = (len(cats) + 2) // 3
        for _r in range(_rows):
            self.cat_grid.setRowStretch(_r, 0)
        self.cat_grid.setRowStretch(_rows, 1)
        # QSS 改字号后不会刷新字体度量缓存，布局需两轮才稳定 → 双次校正
        QTimer.singleShot(0, lambda: fit_labels(self.cat_scroll.widget() or self))
        QTimer.singleShot(120, lambda: fit_labels(self.cat_scroll.widget() or self))

    def open_category(self, cat):
        self.current_cat = cat
        self.lb_crumb.setText("%s%s  /  %s" % (CAT_ICON.get(cat, ""), CAT_NAME.get(cat, cat),
                                               "具体设置"))
        self.btn_back.setVisible(True)
        self.bar2.setVisible(True)
        self.cat_scroll.setVisible(False)
        self.item_scroll.setVisible(True)
        self.build_items()

    def go_root(self):
        self.save_state()
        self.build_root()

    # ---------- 小类明细 ----------
    def build_items(self):
        while self.hl.count():
            it = self.hl.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        cat = self.current_cat
        lst = [r for r in self.rules if r["category"] == cat]

        if not lst:
            self.hl.addWidget(notice("该分类下暂无优化项。"))
            self.hl.addStretch(1)
            return

        cnt = QLabel("已勾选 %d / %d" % (len([r for r in lst if self.sel.get(r["id"])]), len(lst)))
        cnt.setObjectName("Pill")
        f, v = card(CAT_NAME.get(cat, cat), cnt)
        for r in lst:
            row = QWidget()
            rl = QHBoxLayout(row); rl.setContentsMargins(0, 4, 0, 4); rl.setSpacing(10)
            cb = QCheckBox()
            cb.setChecked(bool(self.sel.get(r["id"])))
            cb.toggled.connect(lambda chk, rid=r["id"]: self.toggle(rid, chk))
            rl.addWidget(cb)
            col = QVBoxLayout(); col.setSpacing(2)
            t = QLabel(r["name"])
            t.setWordWrap(True)
            _warn_c = T.get_color("WARN", self.win.dark)
            _ok_c = T.get_color("OK", self.win.dark)
            _err_c = T.get_color("ERR", self.win.dark)
            if r.get("risk") == "policy":
                t.setText('<span style="color:%s;font-weight:600">%s</span>　'
                          '<span style="color:%s">【策略】</span>' % (_warn_c, r["name"], _warn_c))
            if r.get("danger"):
                t.setText(t.text() + '　<span style="color:%s;font-weight:600">【谨慎】</span>'
                          % _err_c)
            if r.get("state") is True:
                t.setText(t.text() + '　<span style="color:%s">已生效</span>' % _ok_c)
            d = QLabel(r["desc"]); d.setObjectName("Muted3"); d.setWordWrap(True)
            col.addWidget(t); col.addWidget(d)
            rl.addLayout(col, 1)
            v.addWidget(row)
        self.hl.addWidget(f)
        self.hl.addStretch(1)

    def _update_safe_hint(self):
        """提示行：说明所有项都要手动勾选，并标出策略项数量。"""
        lb = getattr(self, "lb_safe_hint", None)
        if lb is None:
            return
        if self.current_cat:
            lb.setText("")
            return
        pol = len([r for r in self.rules if r.get("risk") == "policy"])
        if pol:
            lb.setText("打开软件不会改动任何系统设置；手动勾选你要改的项再点“应用所选”才会生效，"
                       "其中 %d 项标有【策略】。" % pol)
        else:
            lb.setText("打开软件不会改动任何系统设置；手动勾选你要改的项再点“应用所选”才会生效。")

    def clear_sel(self):
        self.sel = {}
        if self.current_cat:
            self.build_items()
        else:
            self.build_root()

    def select_all_cat(self):
        """全选当前分类的优化项。

        注意：只是把勾选框填上，**不会写入任何系统设置** —— 仍需用户再点
        「应用所选」并确认才会生效（安全铁律：软件绝不自动改设置）。
        """
        if not self.current_cat:
            return
        for r in self.rules:
            if r["category"] == self.current_cat:
                self.sel[r["id"]] = True
        self.build_items()
        self.save_state()

    def toggle(self, rid, chk):
        self.sel[rid] = chk
        self.save_state()

    def _ids(self):
        return [k for k, v in self.sel.items() if v]

    def apply_sel(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        ids = self._ids()
        if not ids:
            return info(self, "还没有勾选任何项。")
        picked = [r for r in self.rules if r["id"] in ids]
        pol = [r for r in picked if r.get("risk") == "policy"]
        dgr = [r for r in picked if r.get("danger")]
        items = ["%s%s%s  —  %s" % (r["name"],
                                 "　【策略】" if r.get("risk") == "policy" else "",
                                 "　【谨慎】" if r.get("danger") else "",
                                 r.get("desc", ""))
                 for r in picked]
        hint = ("建议先关闭其他正在运行的程序。本工具会自动记录改动前的原值，"
                "可用「撤销上次优化」回退。")
        if dgr:
            hint = ("⚠️ 你勾选的项目里含 %d 项【谨慎】项（暂停更新 / 降低安全防护 / 进程劫持式拦截等），"
                    "可能影响系统防护或后续更新，请确认你真的需要。\n\n" % len(dgr)) + hint
        if not safety_confirm(
                self, self.win.dark, "确认修改系统设置",
                "即将对以下 %d 项写入注册表/系统服务。修改后可能需要重启才能生效。"
                % len(picked),
                items, risky=len(pol) + len(dgr),
                warn_text=hint):
            return
        self.win.busy("正在应用优化…")
        Tasks.run(self, lambda: S.snapshot_and_apply(self.rules, ids), self._applied,
                  on_fail=self._fail)

    def _fail(self, m):
        self.win.idle()
        warn(self, m)

    def _applied(self, res):
        self.win.idle()
        bad = [x for x in res if not x["ok"]]
        if not bad:
            info(self, "已完成 %d 项，系统设置已生效。" % len(res))
            self.reload()
            self.win.notify_changed("startup", "cputune")
            return
        # 逐条列出失败项，避免用户只看到"失败 N 项"却不知问题出在哪
        lines = []
        for x in bad[:8]:
            lines.append("· %s：%s" % (x.get("name", x.get("id")),
                                      x.get("msg") or "未能写入系统"))
        if len(bad) > 8:
            lines.append("· 其余 %d 项同样失败" % (len(bad) - 8))
        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Warning)
        dlg.setWindowTitle("部分设置未能生效")
        dlg.setText("成功 %d 项，失败 %d 项。" % (len(res) - len(bad), len(bad)))
        dlg.setInformativeText("失败明细：\n" + "\n".join(lines))
        dlg.setStandardButtons(QMessageBox.Ok)
        _center_dialog(dlg)
        dlg.exec()
        dlg.deleteLater()      # 不留在控件树里（重抛光整棵树会扫到隐藏对话框）
        self.reload()
        self.win.notify_changed("startup", "cputune")

    def restore_sel(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        ids = self._ids()
        if not ids:
            return info(self, "还没有勾选任何项。")
        picked = [r for r in self.rules if r["id"] in ids]
        items = ["%s  —  恢复为 Windows 默认值" % r["name"] for r in picked]
        if not safety_confirm(
                self, self.win.dark, "确认还原为默认值",
                "即将把以下 %d 项恢复为 Windows 默认设置（删除相关注册表值/恢复服务）。"
                % len(picked),
                items,
                warn_text="还原会移除这些优化项写入的值，系统行为将回到未优化状态。"):
            return
        self.win.busy("正在还原…")
        Tasks.run(self, lambda: S.restore_rules(self.rules, ids), self._applied, on_fail=self._fail)

    def undo(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not confirm(self, "撤销上次优化", "按操作记录把注册表值恢复到优化之前的状态。"):
            return

        def do():
            j = S.journal_load()
            if not j:
                raise RuntimeError("没有可撤销的记录")
            entry = j.pop(0)
            S.journal_save(j)
            n = 0
            for it in entry["items"]:
                try:
                    if it["existed"]:
                        S.reg_write(it["key"], it["value"], it["type"], it["data"])
                    else:
                        S.reg_delete(it["key"], it["value"])
                    n += 1
                except Exception:
                    pass
            return entry["time"], n
        self.win.busy("正在撤销…")
        Tasks.run(self, do, lambda r: (self.win.idle(),
                                       self.reload(),
                                       info(self, "已撤销 %s 的记录（恢复 %d 个值）" % r))[1],
                  on_fail=self._fail)


# ==========================================================================
# 4. 软件管理
# ==========================================================================
class SoftwarePage(Page):
    title = "软件管理"

    SORTS = [("名称", "name"), ("大小", "size"), ("安装日期", "date")]

    def __init__(self, win):
        super().__init__(win)
        self.all = []
        self.hotfix = None          # 已安装更新（切到「更新补丁」时按需读，不预加载）
        self._icons = {}
        # 三个页签：已安装软件 / 预装应用（UWP）/ 安装监视。
        # 不另开大类 —— 都是「这台机器上装了什么、装的时候动了什么」，放一起更顺。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        _w1 = QWidget()
        root = QVBoxLayout(_w1)          # 原来的 root 改挂到第一个页签上
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)
        self.tabs.addTab(_w1, "已安装软件")

        bar = QHBoxLayout()
        self.q = QLineEdit(); self.q.setPlaceholderText("搜索程序名 / 发布者")
        self.q.setMaximumWidth(260)
        self.q.textChanged.connect(self.filter)
        self.n = pill("共 0 个", "")
        bar.addWidget(self.q)
        bar.addWidget(self.n)
        # 分类切换：全部 / 自己安装 / 系统与商店（QButtonGroup 保证互斥）
        self._kind = "all"
        self._seg = QButtonGroup(self)
        self._seg.setExclusive(True)
        seg = QHBoxLayout(); seg.setSpacing(6)
        for _i, (_label, _key) in enumerate((("全部", "all"),
                                             ("我安装的", "user"),
                                             ("系统 / 商店", "system"),
                                             ("最近安装", "recent"),
                                             ("更新补丁", "update"))):
            b = QPushButton(_label)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setChecked(_key == "all")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=_key: self.set_kind(k))
            self._seg.addButton(b, _i)
            seg.addWidget(b)
        bar.addLayout(seg)
        bar.addStretch(1)
        self.btn_orphan = btn("扫描卸载残留")
        bar.addWidget(self.btn_orphan)
        self.ctl_orphan = ScanCtl(self.btn_orphan,
                                  lambda *_: self.orphan_scan())
        # ⚠️ 必须绑 `reload` 而不是 `refresh`：`refresh()` 走 `run_cached`，
        # 第二次点会**命中会话缓存直接回放旧数据** —— 按钮看着按了，列表纹丝不动。
        # `reload()` 先 `sess_invalidate()` 再读，才是真正的「重新读一遍系统」。
        bar.addWidget(btn("刷新", on_click=self.reload))
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("排序："))
        self.sort_combo = QComboBox()
        for label, key in self.SORTS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.setCurrentIndex(0)
        self.sort_combo.currentIndexChanged.connect(self.filter)
        bar2.addWidget(self.sort_combo)
        self.sort_desc = True
        self.btn_order = btn("降序", on_click=self.toggle_order)
        bar2.addWidget(self.btn_order)
        bar2.addStretch(1)
        root.addLayout(bar2)

        self.table = make_table(["程序名", "发布者", "版本", "大小", "安装日期", "架构"],
                                sortable=True)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)  # 支持多选（批量）
        attach_reveal(self.table, self._reveal_path)      # 右键「定位文件夹」
        attach_row_menu(self.table, self._row_menu)       # 右键软件动作
        root.addWidget(self.table, 1)

        act = QHBoxLayout()
        self.btn_uninstall = btn("卸载所选", "Danger", on_click=self.uninstall)
        self.btn_uninstall.setEnabled(self.win.admin_mode)
        act.addWidget(self.btn_uninstall)
        # 卸载方式（勾选「卸载字符串」）：默认调自带卸载程序、弹它
        # 自己的界面；勾上则走静默卸载（7-Zip 的 `Uninstall.exe /S` 那种），
        # 批量卸载时不用一个个点。没有静默命令的程序会自动退回交互式。
        self.cb_quiet = QCheckBox("静默卸载（不弹界面）")
        self.cb_quiet.setToolTip(
            "勾上后用程序自带的 QuietUninstallString 卸载，不弹任何界面。\n"
            "适合批量卸载；界面看不到进度，只能等日志里的结果。\n"
            "本机 348 个程序里约 19% 提供了静默命令，其余的会自动退回交互式。\n"
            "MSI 程序则切换 msiexec 的 /qn（全静默）与 /qb（显示进度条）。")
        self.cb_quiet.setChecked(bool(self.win.settings.get("uninstall_quiet")))
        self.cb_quiet.toggled.connect(self._on_quiet_toggle)
        act.addWidget(self.cb_quiet)
        act.addStretch(1)
        self.info_lb = QLabel(""); self.info_lb.setObjectName("Muted")
        self.info_lb.setWordWrap(True)
        act.addWidget(self.info_lb)
        root.addLayout(act)

        # 第二个页签：预装应用（首次点开才去查 —— 那要起一次 PowerShell，约 2 秒）
        self.uwp = UwpPanel(win)
        self.tabs.addTab(self.uwp, "预装应用")
        # 第三个页签：安装监视
        self.imon = InstallMonitorPanel(win)
        self.tabs.addTab(self.imon, "安装监视")
        self.tabs.currentChanged.connect(self._on_tab)

    def _on_tab(self, ix):
        """后两个页签：第一次点开才加载，别让它拖慢整个软件管理页。"""
        if ix == 1 and not self.uwp.loaded:
            self.uwp.first_show()
        elif ix == 2:
            self.imon.first_show()

    def refresh(self):
        Tasks.run_cached(self, "sw", S.list_software, self._got)
        # 预装应用只在自己被打开过之后才跟着刷新（没打开过就别白跑 PowerShell）
        if getattr(self, "uwp", None) is not None and self.uwp.loaded:
            self.uwp.refresh()

    def reload(self):
        """整页刷新：各页签的数据一起失效后重读。"""
        self.sess_invalidate()
        if getattr(self, "uwp", None) is not None:
            self.uwp.sess_invalidate()
        self.refresh()

    def _got(self, lst):
        self.all = lst
        self.filter()

    def _icon(self, it):
        """从 DisplayIcon 提取程序图标（QFileIconProvider 走系统外壳，带缓存）。"""
        name = it["name"]
        if name in self._icons:
            return self._icons[name]
        from PySide6.QtWidgets import QFileIconProvider
        icon = QFileIconProvider().icon(QFileIconProvider.File)
        raw = (it.get("icon") or "").strip()
        if raw:
            path = raw.split('",')[0].strip('"').strip()
            if "," in path:
                path = path.rsplit(",", 1)[0].strip().strip('"')
            if path and os.path.exists(path):
                try:
                    if path.lower().endswith(".ico"):
                        icon = QIcon(path)
                    else:
                        icon = QFileIconProvider().icon(QFileInfo(path))
                except Exception:
                    pass
        self._icons[name] = icon
        return icon

    def _date_key(self, s):
        d = "".join(ch for ch in (s or "") if ch.isdigit())
        return int(d) if d else 0

    def _reveal_path(self, table, row):
        """软件表右键「定位文件夹」：优先安装目录，其次图标 / 卸载程序所在位置。"""
        it = table.item(row, 0)
        idx = it.data(Qt.UserRole) if it is not None else None
        rows = getattr(self, "_rows", [])
        if idx is None or not (0 <= idx < len(rows)):
            return ""
        x = rows[idx]
        loc = str(x.get("location") or "").strip().strip('"')
        if loc and os.path.isdir(loc):
            return loc
        icon = str(x.get("icon") or "").split(",")[0].strip().strip('"')
        if icon and os.path.exists(icon):
            return icon
        for cmd in (x.get("uninstall"), x.get("quiet")):
            exe, _a = S.parse_uninstall_cmd(cmd or "")
            if exe and os.path.exists(exe):
                return exe
        return loc or icon

    def toggle_order(self):
        self.sort_desc = not self.sort_desc
        self.btn_order.setText("降序" if self.sort_desc else "升序")
        self.filter()

    def set_kind(self, k):
        """切换「全部 / 我安装的 / 系统与商店 / 最近安装 / 更新补丁」。"""
        self._kind = k
        if k == "update" and getattr(self, "hotfix", None) is None:
            # 已安装更新不在 ARP 名单里（Win10 累积更新早已搬走），必须单独查
            self.hotfix = []
            self.n.setText("正在读取已安装更新…")
            self.filter()                 # 立刻切到更新视图（此时为空），
                                          # 别让上一个分类的行留在表里冒充补丁
            Tasks.run(self, lambda: S.list_hotfix(), self._got_hotfix,
                      on_fail=lambda m: (setattr(self, "hotfix", []),
                                         self.n.setText("读取更新失败：%s" % m[:40]),
                                         self.filter()))
            return
        self.filter()

    def _got_hotfix(self, rows):
        """把 Get-HotFix 结果整形成与已装软件一致的形状，再走同一套填表/排序。"""
        self.hotfix = [{
            "name": x.get("name") or x.get("kb") or "",
            "kb": True, "hotfix": True, "kbno": x.get("kb") or "",
            "version": "", "publisher": x.get("by") or "Windows Update",
            "size": 0, "date": x.get("date") or "",
            "uninstall": "", "quiet": "", "location": "", "icon": "",
            "modify": "",
            # 补丁没有官网，但微软有一个公开的 KB 详情页可用
            "url": ("https://support.microsoft.com/help/%" + (x.get("kb") or ""))
                   if x.get("kb") else "",
            "bit": "",
            "kind": "update", "desc": x.get("desc") or "",
        } for x in (rows or [])]
        if getattr(self, "_kind", "all") == "update":
            self.n.setText("更新补丁 %d 条 · 点左侧其它分类可返回" % len(self.hotfix))
        self.filter()

    def _rows_for(self, kind, match):
        """按分类取行。「更新补丁」走独立的 Get-HotFix 结果，不混 ARP。"""
        if kind == "update":
            return [x for x in (getattr(self, "hotfix", None) or []) if match(x)]
        if kind == "recent":                              # 最近 30 天安装
            import datetime as _dt
            cutoff = (_dt.date.today() - _dt.timedelta(days=30)).strftime("%Y%m%d")
            return [x for x in self.all
                    if str(x.get("date") or "") >= cutoff and match(x)]
        if kind == "all":
            return [x for x in self.all if match(x)]
        return [x for x in self.all if x.get("kind") == kind and match(x)]

    def filter(self):
        kw = self.q.text().strip().lower()

        def match(x):
            return (not kw or kw in (x["name"] + " " + (x["publisher"] or "")).lower())

        kind = getattr(self, "_kind", "all")
        rows = self._rows_for(kind, match)
        key = self.sort_combo.currentData() or "name"
        if key == "size":
            rows.sort(key=lambda x: x["size"] or 0, reverse=self.sort_desc)
        elif key == "date":
            rows.sort(key=lambda x: self._date_key(x["date"]), reverse=self.sort_desc)
        else:
            rows.sort(key=lambda x: x["name"].lower(), reverse=self.sort_desc)
        n_user = len([x for x in self.all if x.get("kind") == "user"])
        if kind == "update":
            self.n.setText("已安装更新 %d 条 · 其余分类共自装 %d / 系统 %d"
                           % (len(rows), n_user, len(self.all) - n_user))
        else:
            self.n.setText("自装 %d · 系统 %d · 显示 %d"
                           % (n_user, len(self.all) - n_user, len(rows)))
        was_sorted = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for idx, x in enumerate(rows):
            i = self.table.rowCount()
            self.table.insertRow(i)
            it0 = SortItem(x["name"])
            it0.setFlags(it0.flags() & ~Qt.ItemIsEditable)
            it0.setIcon(self._icon(x))
            it0.setToolTip(x["name"])
            it0.setData(Qt.UserRole, idx)      # 记住原始行号，排序后仍能对上数据
            self.table.setItem(i, 0, it0)
            bit = x.get("bit") or ""
            for c, v in enumerate([x["publisher"], x["version"],
                                   fmt_bytes(x["size"] * 1024) if x["size"] else "",
                                   x["date"], (bit + " 位") if bit else ""], start=1):
                it = SortItem("" if v is None else str(v))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                num = _sort_num(v)
                if num is not None:
                    it.setData(Qt.UserRole + 1, num)
                it.setToolTip("" if v is None else str(v))
                self.table.setItem(i, c, it)
            self.table.setRowHeight(i, 30)
        if was_sorted:
            self.table.setSortingEnabled(True)
        self.table.setColumnWidth(0, 300)
        self._rows = rows

    def _current(self):
        r = self.table.currentRow()
        if r < 0 or r >= self.table.rowCount():
            return None
        it = self.table.item(r, 0)
        idx = it.data(Qt.UserRole) if it else None
        if idx is None:
            idx = r
        rows = getattr(self, "_rows", [])
        if idx < 0 or idx >= len(rows):
            return None
        return rows[idx]

    def _on_quiet_toggle(self, on):
        """「静默卸载」勾选：记住选择（下次打开还是这个状态）。"""
        self.win.settings["uninstall_quiet"] = bool(on)
        settings_save(self.win.settings)

    def uninstall(self):
        """按钮「卸载所选」：对当前行执行卸载。"""
        it = self._current()
        if not it:
            return info(self, "请先在列表中选择一个程序。")
        self._do_uninstall(it)

    # ---------- 右键菜单（动作与顺序） ----------
    def _row_item(self, row):
        """按表格行号取原始程序数据（排序后仍正确：读 UserRole 里记的原索引）。"""
        if row < 0 or row >= self.table.rowCount():
            return None
        it = self.table.item(row, 0)
        idx = it.data(Qt.UserRole) if it is not None else None
        rows = getattr(self, "_rows", [])
        if idx is None or not (0 <= idx < len(rows)):
            return None
        return rows[idx]

    def _selected_items(self):
        """当前选中的程序（去重，保持显示顺序）—— 供批量操作使用。"""
        out, seen = [], set()
        for r in sorted({i.row() for i in self.table.selectedIndexes()}):
            x = self._row_item(r)
            if x is None:
                continue
            sig = (x["name"], x.get("version"), x.get("publisher"))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(x)
        return out

    def _row_menu(self, m, table, row):
        """内容区右键菜单：卸载 / 强删条目 / 修改 / 打开目录·注册表·官网 / 搜索…"""
        it = self._row_item(row)
        if it is None:
            return
        admin = bool(self.win.admin_mode)
        has_cmd = bool(it.get("quiet") or it.get("uninstall"))
        sel = self._selected_items()

        def add(text, fn, enabled=True):
            a = m.addAction(text)
            a.setEnabled(bool(enabled))
            # 菜单关闭后再执行，避免在 exec() 内嵌套弹模态
            a.triggered.connect(lambda _c=False, f=fn: QTimer.singleShot(0, f))
            return a

        if it.get("hotfix"):
            # 补丁行不是普通程序：没有卸载命令、没有安装目录/注册表位置，
            # 给的是「查这个 KB」而不是一排点不动的灰项。
            add("在浏览器中查看该 KB 详情", lambda: self._do_open_url(it),
                bool(it.get("url")))
            add("复制 KB 编号", lambda: self._do_copy_kb(it), True)
            add("用 Google 搜索此补丁", lambda: self._do_google(it), True)
            return

        add("卸载", lambda: self._do_uninstall(it), admin and has_cmd)
        add("修改 / 修复安装", lambda: self._do_modify(it),
            admin and bool(it.get("modify")))
        m.addSeparator()
        add("强制删除此条目（仅注册表）", lambda: self._do_force_remove(it), admin)
        add("强制删除所选的 %d 个条目" % len(sel),
            lambda: self._do_force_remove_many(sel), admin and len(sel) > 1)
        m.addSeparator()
        add("打开安装目录", lambda: self._do_open_dir(it))
        add("打开注册表位置", lambda: self._do_open_reg(it))
        add("访问程序官网", lambda: self._do_open_url(it), bool(it.get("url")))
        add("用 Google 搜索", lambda: self._do_google(it))
        add("在 Microsoft Store 中打开", lambda: self._do_store(it),
            it.get("kind") == "system")

    def _do_copy_kb(self, it):
        kb = it.get("kbno") or ""
        if not kb:
            return
        QApplication.clipboard().setText(kb)
        self.info_lb.setText("已复制补丁编号 %s" % kb)

    def _do_uninstall(self, it):
        """卸载：记录快照 → 调原厂卸载程序并等它结束 → 询问后扫描对比残留。"""
        if it.get("hotfix"):
            return info(self, "已安装的更新补丁不能在这里卸载。\n\n"
                              "要移除某个更新补丁，请到「设置 → Windows 更新 → 高级选项 →"
                              "查看更新历史记录」，或用 DISM / wusa 单独卸载。")
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        # ⚠️ 默认必须**优先 UninstallString**。QuietUninstallString 是静默卸载
        # （典型：7-Zip 的 `Uninstall.exe /S`），**不弹任何界面** —— 用户点完卸载
        # 什么都没看到，只会以为"没能调用它自带的卸载程序"，还得干等它静默跑完。
        # 本机 348 个程序里 19% 带 quiet，命中面不小。
        # 只有**用户主动勾了「静默卸载」**、或确实没有交互式命令时才走静默
        # （勾选「卸载字符串」，批量卸载时省事）。
        _cu = (it.get("uninstall") or "").strip()
        _cq = (it.get("quiet") or "").strip()
        want_quiet = bool(getattr(self, "cb_quiet", None)
                          and self.cb_quiet.isChecked())
        if want_quiet and _cq:
            cmd, interactive = _cq, False
        else:
            cmd, interactive = (_cu or _cq), bool(_cu)
        # 勾了静默但该程序没有静默命令 → 如实说明会退回交互式，别让用户以为没生效
        quiet_fallback = want_quiet and not _cq
        if not cmd:
            return info(self, "该程序没有提供卸载命令，可能需要手动卸载。")
        if interactive:
            _how = ("将调用它自带的卸载程序（会弹出它的界面，按提示完成即可）。"
                    + ("\n\n（已勾选「静默卸载」，但该程序没有提供静默命令，"
                       "所以这次仍会弹出它的界面。）" if quiet_fallback else ""))
        else:
            _why = ("因为勾选了「静默卸载」。" if want_quiet
                    else "该程序只提供了静默卸载命令。")
            _how = ("将用**静默**方式卸载（不会弹界面），完成后自动结束，请耐心等待。"
                    "\n\n（%s）" % _why)
        if not confirm(self, "确认卸载",
                       "即将卸载：%s\n\n%s\n"
                       "本工具会先记录卸载前状态，等卸载结束后自动对比扫描残留。"
                       % (it["name"], _how),
                       danger=True):
            return

        _snap = {"dirs": 0, "files": 0}

        def after_snap(_r=None):
            # snapshot_before 除了名字匹配，还拍了**安装目录的精确快照** ——
            # 卸载后重扫对比，就能拿到「该删而没删」的硬证据（比名字猜准得多）。
            if isinstance(_r, dict):
                _snap["dirs"] = int(_r.get("install_dirs") or 0)
                _snap["files"] = int(_r.get("install_files") or 0)
            if interactive:
                self.info_lb.setText("正在等待卸载程序关闭…（请在它自己的窗口里完成操作）")
                self.win.busy("等待卸载程序关闭…")
            else:
                self.info_lb.setText("正在静默卸载，请稍候…（不会弹界面）")
                self.win.busy("正在静默卸载…")

            def done(r):
                self.win.idle()
                r = r or {}
                mode = r.get("mode") or ("completed" if r.get("ok") else "timeout")
                secs = r.get("secs", 0)
                self.info_lb.setText("卸载流程结束")
                self.reload()
                self.win.notify_changed("startup", "overview")
                _extra = ("\n已记录它安装目录里的 %d 个文件 —— 卸载没删掉的会被"
                          "精确列出（不再靠名字猜）。" % _snap["files"]
                          if _snap["files"] else "")
                if mode == "completed":
                    # 检测到自带卸载程序已关闭、且已从「已安装」列表消失 → 真卸载完了，
                    # 直接进入残留扫描（用户要的就是「关掉卸载程序之后才扫」）。
                    if confirm(self, "卸载完成 · 扫描残留",
                               "卸载已完成（用时 %d 秒）。\n\n"
                               "现在扫描 %s 留下的残余文件与注册表项？%s\n"
                               "（扫描只列出结果，删除前会让你逐项勾选）"
                               % (secs, it["name"], _extra)):
                        self._scan_residual(it)
                elif mode == "cancelled":
                    # 卸载程序关了，但程序还在列表里 → 多半是用户取消了。
                    # 这时候不该自动扫（文件都还在，扫出来全是"正常文件"）。
                    if confirm(self, "似乎取消了卸载",
                               "卸载程序已关闭，但 %s 仍在「已安装」列表中 ——"
                               "看起来是取消了卸载。\n\n"
                               "%s\n\n仍要扫描残留吗？（扫描只列出结果，不会删除）"
                               % (it["name"],
                                  "（如果它其实装完了、只是需要重启，"
                                  "扫描不会有副作用）")):
                        self._scan_residual(it)
                else:
                    # 超时：卸载窗口可能还开着（UAC 框 / 最小化）
                    if confirm(self, "未检测到卸载程序关闭",
                               r.get("msg") or
                               "没有检测到卸载程序关闭，可能它还在等待操作。\n\n"
                               "要现在扫描残留吗？（如果它还在运行，"
                               "扫到的多半是尚未删除的正常文件）"):
                        self._scan_residual(it)

            # 把安装目录一起传下去：卸载程序常自我复制到 %TEMP% 再重启，
            # 只按 exe 名匹配会漏，带上安装目录才能准确判断"窗口关了没有"。
            _dirs = []
            try:
                _dirs = S.install_dir_candidates(it.get("key", ""),
                                                it.get("location") or "", cmd)
            except Exception:
                _dirs = []
            Tasks.run(self, lambda: S.uninstall_software(
                it.get("key", ""), cmd, install_dirs=_dirs,
                quiet=(not interactive)), done)

        self.info_lb.setText("正在记录卸载前状态…")
        Tasks.run(self,
                  lambda: S.snapshot_before(it.get("key", ""), it["name"],
                                            it.get("publisher") or "",
                                            it.get("location") or "", cmd),
                  after_snap, on_fail=lambda *_: after_snap())

    def _do_force_remove(self, it):
        """强制移除条目：只删 Uninstall 注册表项，不动程序文件。"""
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not confirm(self, "强制删除条目",
                       "将从「已安装程序」列表中移除：\n%s\n\n"
                       "只删除它的 Uninstall 注册表条目（不动程序文件），"
                       "用于卸载程序已损坏、无法正常卸载的情况。\n"
                       "要清文件请改用「卸载」或「扫描卸载残留」。\n\n确认移除该条目？"
                       % it["name"], danger=True):
            return
        r = S.force_remove_entry(it.get("key", "")) or {}
        info(self, "已移除条目" if r.get("ok") else "移除失败", r.get("msg") or "")
        self.reload()
        self.win.notify_changed("startup", "overview")

    def _do_force_remove_many(self, items):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not items:
            return info(self, "没有选中任何程序。")
        names = "\n".join("· " + x["name"] for x in items[:12])
        more = "" if len(items) <= 12 else "\n…等共 %d 个" % len(items)
        if not confirm(self, "批量强制删除条目",
                       "将从「已安装程序」列表移除以下 %d 个条目：\n%s%s\n\n"
                       "只删注册表条目，不动程序文件。确认？" % (len(items), names, more),
                       danger=True):
            return
        ok = sum(1 for x in items
                 if (S.force_remove_entry(x.get("key", "")) or {}).get("ok"))
        info(self, "批量移除完成", "成功移除 %d / %d 个条目。" % (ok, len(items)))
        self.reload()
        self.win.notify_changed("startup", "overview")

    def _do_modify(self, it):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        ok, msg = S.run_detached(it.get("modify") or "")
        if ok:
            self.info_lb.setText(msg)
        else:
            warn(self, msg)

    def _do_open_dir(self, it):
        loc = (it.get("location") or "").strip().strip('"')
        if not loc:
            loc = str(it.get("icon") or "").split(",")[0].strip().strip('"')
        if not loc or not S.open_dir(loc):
            warn(self, "找不到该程序的安装目录。")

    def _do_open_reg(self, it):
        if not S.open_regedit(it.get("key", "")):
            warn(self, "无法打开注册表位置。")

    def _do_open_url(self, it):
        if not S.open_url(it.get("url")):
            warn(self, "该程序没有提供官网地址。")

    def _do_google(self, it):
        S.google_search("%s %s" % (it["name"], it.get("publisher") or ""))

    def _do_store(self, it):
        if not S.open_store(it["name"]):
            warn(self, "无法打开 Microsoft Store。")

    def _scan_residual(self, it):
        self.win.busy("正在扫描残留…")
        self.info_lb.setText("正在扫描残余文件与注册表项…")
        base = S.snapshot_get(it.get("key", ""), it["name"])

        def got(r):
            self.win.idle()
            n = len((r or {}).get("files") or []) + len((r or {}).get("reg") or [])
            self.info_lb.setText(("发现 %d 项残留，等待处理…" % n) if n
                                 else "未发现残留，系统很干净。")
            dlg = ResidualDialog(self, it["name"], r, on_done=self.reload)
            dlg.exec()
            dlg.deleteLater()      # 不把隐藏的模态框留在控件树里（重抛光会扫到）

        Tasks.run(self,
                  lambda: S.residual_scan(it["name"], it.get("publisher") or "",
                                          it.get("location") or "",
                                          it.get("uninstall") or it.get("quiet") or "",
                                          it.get("key", ""), base),
                  got,
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败：" + m))[1])

    def orphan_scan(self):
        """全机卸载残留扫描：不需要选中程序，直接找"失去主人"的东西。

        内容：卸载程序已消失的条目 / 失效快捷方式 / 指向不存在可执行文件的启动项
        与服务 / **安装根下的空壳目录**（无证据链可依，靠"完全不含文件"判定，
        标为低置信由用户确认）。
        """
        self.win.busy("正在扫描全机卸载残留（孤儿条目 / 失效快捷方式 / "
                      "孤儿服务 / 空残留目录）…")
        def _orphan_got(r):
            self.win.idle()
            dlg = ResidualDialog(self, "全机扫描", r, on_done=self.reload)
            dlg.exec()
            dlg.deleteLater()      # 同上：用完即走

        Tasks.run(self, lambda: S.orphan_scan(
            should_stop=self.ctl_orphan.stop_flag),
                  _orphan_got,
                  ctl=self.ctl_orphan,
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败：" + m))[1])


class ResidualDialog(QDialog):
    """残留处理窗：单张分类清单 + 工具栏 + 逐条删除进度日志。

    流程是「列出残留 → 勾选 → 逐条处理并实时显示日志 → 汇总」：
      * 单张表按「类型」分类（目录 / 文件 / 注册表 / 凭据 / 任务），不再拆两张表；
      * 工具栏：关键词搜索、类型筛选、全选 / 全不选 / 反选 / 只选高置信；
      * 底栏常驻「已选 N 项 · 约 X MB」；
      * 删除先过确认框（写明留了哪些退路）→ 切到进度页**逐条**处理，每条一行
        真实结果，跑完出汇总；中途关闭即中止剩余条目。
    """

    KIND_LABEL = {"dir": "目录", "file": "文件", "reg": "注册表",
                  "cred": "凭据", "task": "任务"}

    def __init__(self, parent, name, result, on_done=None):
        super().__init__(parent)
        self.setWindowTitle("残留处理 — %s" % name)
        self.resize(840, 620)
        self.on_done = on_done
        p = self.parent()
        self._dark = bool(getattr(p, "dark", None)
                          or getattr(getattr(p, "win", None), "dark", False))
        self._rows = []          # [{kind,label,size,weak,tag,evidence,payload}]
        self._queue = []
        self._ok_n = self._fail_n = 0
        self._backups = []
        self._busy = False
        result = result or {}
        files = result.get("files") or []
        regs = result.get("reg") or []
        total = sum(int(f.get("bytes") or 0) for f in files)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        head = QLabel("找到 %d 个文件/目录（约 %s）、%d 个注册表 / 凭据 / 计划任务项。\n"
                      "删除前自动留退路：文件移入回收站（可还原），注册表导出 .reg "
                      "备份到 backup\\uninstall。"
                      % (len(files), fmt_bytes(total), len(regs)))
        head.setObjectName("Muted")
        head.setWordWrap(True)
        root.addWidget(head)

        # --- 工具栏：关键词搜索 + 类型筛选（清单顶部的过滤条） ---
        bar = QHBoxLayout()
        self.ed_q = QLineEdit()
        self.ed_q.setPlaceholderText("搜索路径 / 注册表位置 / 关键词…")
        self.ed_q.setClearButtonEnabled(True)
        self.ed_q.textChanged.connect(self._refilter)
        bar.addWidget(self.ed_q, 1)
        self.cmb_kind = QComboBox()
        self.cmb_kind.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.cmb_kind.addItems(["全部类型", "文件/目录", "注册表", "凭据/任务"])
        self.cmb_kind.currentIndexChanged.connect(self._refilter)
        bar.addWidget(self.cmb_kind)
        root.addLayout(bar)

        # --- 清单：单张表，勾选状态跟行走 ---
        self.tbl = make_table(["", "类型", "位置", "大小", "置信"], sortable=True)
        for f in files:
            kind = f.get("kind") if f.get("kind") in ("dir", "file") else "file"
            # 带上来源标签（安装目录 / 空残留目录 / 桌面快捷方式…），
            # 否则一列路径看不出它是靠什么判定出来的；搜索也顺带能按来源筛。
            _lab = f.get("path") or ""
            if f.get("tag"):
                _lab = "[%s] %s" % (f["tag"], _lab)
            self._rows.append({"kind": kind, "label": _lab,
                               "size": int(f.get("bytes") or 0),
                               "weak": bool(f.get("weak")),
                               "checked": not bool(f.get("weak")),
                               "tag": f.get("tag") or "",
                               "evidence": f.get("evidence") or "",
                               "payload": {"path": f.get("path")}})
        for rg in regs:
            kind = rg.get("kind") if rg.get("kind") in ("cred", "task") else "reg"
            if kind in ("cred", "task"):
                label = rg.get("path") or ""       # 凭据/任务没有注册表路径
                if rg.get("value"):
                    label += "（%s）" % rg["value"]
            else:
                label = "%s\\%s" % (rg.get("hive"), rg.get("path"))
                if rg.get("value"):
                    label += "  →  %s" % rg["value"]
            if rg.get("tag"):
                label = "[%s] %s" % (rg["tag"], label)
            self._rows.append({"kind": kind, "label": label, "size": 0,
                               "weak": False, "checked": True,
                               "tag": rg.get("tag") or "",
                               "evidence": "", "payload": dict(rg)})
        for i, row in enumerate(self._rows):
            row["rid"] = i          # PySide 存 dict 进 UserRole 会被拷贝（取回不是
                                    # 同一对象），所以用整数 id 回索引 self._rows
        self._fill_table()
        attach_reveal(self.tbl, self._reveal)
        root.addWidget(self.tbl, 1)

        # --- 进度页（逐条处理日志，默认隐藏，删除时切换） ---
        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(8)
        self.lb_prog = QLabel("")
        self.lb_prog.setObjectName("Muted")
        self.log = QListWidget()
        self.prog.setVisible(False)
        self.lb_prog.setVisible(False)
        self.log.setVisible(False)
        root.addWidget(self.prog)
        root.addWidget(self.lb_prog)
        root.addWidget(self.log, 1)

        # --- 底栏：选择操作 + 删除 ---
        hb = QHBoxLayout()
        self.lb_sel = QLabel("已选 0 项")
        hb.addWidget(self.lb_sel)
        hb.addWidget(btn("全选", on_click=lambda: self._check_all(True)))
        hb.addWidget(btn("全不选", on_click=lambda: self._check_all(False)))
        hb.addWidget(btn("反选", on_click=self._invert))
        hb.addWidget(btn("只选高置信", on_click=self._only_strong))
        hb.addStretch(1)
        self.btn_backup = btn("打开备份文件夹", on_click=self._open_backup)
        self.btn_backup.setEnabled(
            os.path.isdir(os.path.join(S.BACKUP, "uninstall")))
        hb.addWidget(self.btn_backup)
        self.btn_del = btn("删除所选", "Danger", on_click=self._clean)
        self.btn_del.setEnabled(bool(files or regs))
        hb.addWidget(self.btn_del)
        self.btn_close = btn("关闭", on_click=self.reject)
        hb.addWidget(self.btn_close)
        root.addLayout(hb)
        self.tbl.itemChanged.connect(self._on_item_changed)
        self._sync_sel()

    def _on_item_changed(self, it):
        """勾选状态写回行数据 —— 这样搜索/筛选重建表格后不会丢勾选。"""
        if self.tbl.column(it) != 0 or not hasattr(self, "lb_sel"):
            return
        pos = self.tbl.item(self.tbl.row(it), 2)
        rid = pos.data(Qt.UserRole + 1) if pos else None
        if isinstance(rid, int) and 0 <= rid < len(self._rows):
            self._rows[rid]["checked"] = (it.checkState() == Qt.Checked)
        self._sync_sel()

    # ---------- 清单填充 / 过滤 / 选择 ----------

    def _fill_table(self):
        """按当前搜索词与类型筛选重建表格（过滤后看不见的行不会被删除）。"""
        t = self.tbl
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.setRowCount(0)
        kw = (self.ed_q.text() if hasattr(self, "ed_q") else "").strip().lower()
        flt = self.cmb_kind.currentIndex() if hasattr(self, "cmb_kind") else 0
        for row in self._rows:
            if kw and kw not in row["label"].lower():
                continue
            if flt == 1 and row["kind"] not in ("dir", "file"):
                continue
            if flt == 2 and row["kind"] != "reg":
                continue
            if flt == 3 and row["kind"] not in ("cred", "task"):
                continue
            i = t.rowCount()
            t.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked if row.get("checked", not row["weak"])
                              else Qt.Unchecked)
            # weak 的原因不同，提示语必须区分 —— 否则「空残留目录」会被写成
            # 「仅名称片段相同，可能是别的程序的文件」，与事实不符（它根本没有
            # 名称匹配，是靠"整个目录不含文件"判定的）。
            if row["weak"]:
                if row.get("tag") == "空残留目录":
                    chk.setToolTip("整个目录不含任何文件：删掉不会丢数据，但它也可能"
                                   "只是某个程序还没用到的空目录 —— 确认后再勾选")
                else:
                    chk.setToolTip("仅名称片段相同，可能是别的程序的文件 —— "
                                   "确认后再勾选")
            else:
                chk.setToolTip("")
            t.setItem(i, 0, chk)
            kind_it = QTableWidgetItem(self.KIND_LABEL.get(row["kind"], "?"))
            kind_it.setFlags(kind_it.flags() & ~Qt.ItemIsEditable)
            t.setItem(i, 1, kind_it)
            pos = QTableWidgetItem(row["label"])
            pos.setFlags(pos.flags() & ~Qt.ItemIsEditable)
            tip = row["evidence"] or row["label"]
            pos.setToolTip(tip)
            pos.setData(Qt.UserRole, dict(row["payload"]))   # 删除只认原始结构
            pos.setData(Qt.UserRole + 1, row.get("rid"))     # 勾选状态写回的索引
            t.setItem(i, 2, pos)
            sz = QTableWidgetItem(fmt_bytes(row["size"]) if row["size"] else "—")
            sz.setFlags(sz.flags() & ~Qt.ItemIsEditable)
            sz.setData(Qt.UserRole, int(row["size"]))        # 供「已选约 X MB」统计
            t.setItem(i, 3, sz)
            conf = QTableWidgetItem("低·需确认" if row["weak"] else "确定")
            conf.setFlags(conf.flags() & ~Qt.ItemIsEditable)
            conf.setForeground(QColor(T.get_color(
                "WARN" if row["weak"] else "OK", self._dark)))
            conf.setData(Qt.UserRole, row["weak"])
            conf.setToolTip("仅名称片段相同，可能是别的程序的文件" if row["weak"]
                            else (row["evidence"] or "位置与失效记录完全对得上"))
            t.setItem(i, 4, conf)
        t.setColumnWidth(0, 34)
        t.setColumnWidth(1, 86)
        t.setColumnWidth(3, 92)
        t.setColumnWidth(4, 96)
        t.setSortingEnabled(True)
        t.blockSignals(False)
        self._sync_sel()

    def _refilter(self, *_):
        self._fill_table()

    def _reveal(self, t, r):
        """右键定位：文件认路径；注册表项记录了失效 exe（孤儿服务/启动项）就过去。"""
        it = t.item(r, 2)
        d = it.data(Qt.UserRole) if it else None
        if isinstance(d, dict):
            if d.get("hive"):
                v = str(d.get("value") or "")
                return v if os.path.isabs(v) else ""
            return str(d.get("path") or "")          # 文件 / 目录
        return ""

    def _check_all(self, on):
        st = Qt.Checked if on else Qt.Unchecked
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it and (it.flags() & Qt.ItemIsUserCheckable):
                it.setCheckState(st)

    def _invert(self):
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it and (it.flags() & Qt.ItemIsUserCheckable):
                it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked
                                 else Qt.Checked)

    def _only_strong(self):
        """「只选确定的」：低置信项一键让位，避免顺手删错。"""
        for r in range(self.tbl.rowCount()):
            it, cf = self.tbl.item(r, 0), self.tbl.item(r, 4)
            if it and cf and (it.flags() & Qt.ItemIsUserCheckable):
                it.setCheckState(Qt.Unchecked if cf.data(Qt.UserRole)
                                 else Qt.Checked)

    def _sync_sel(self):
        if not hasattr(self, "lb_sel"):      # __init__ 里表格先建、底栏后建
            return
        n, sz = 0, 0
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                n += 1
                sz_it = self.tbl.item(r, 3)
                sz += int(sz_it.data(Qt.UserRole) or 0) if sz_it else 0
        self.lb_sel.setText("已选 %d 项 · 约 %s" % (n, fmt_bytes(sz))
                            if n else "未选择任何项")

    def _collect(self):
        """取勾选项：一律读单元格 UserRole 里的**原始结构**，不解析显示文本。

        （显示文本是「[标签] hive\\path」加工过的，解析它会拿到假 hive —— 旧 bug。）
        返回 (files, regs)。
        """
        files, regs = [], []
        for r in range(self.tbl.rowCount()):
            if self.tbl.item(r, 0).checkState() != Qt.Checked:
                continue
            it = self.tbl.item(r, 2)
            d = it.data(Qt.UserRole) if it else None
            if not isinstance(d, dict):
                continue
            if d.get("hive") or d.get("kind") in ("cred", "task"):
                regs.append(dict(d))
            else:
                files.append({"path": d.get("path")})
        return files, regs

    # ---------- 删除：逐条处理 + 实时日志 ----------

    def _clean(self):
        if self._busy:
            return
        files, regs = self._collect()
        if not files and not regs:
            return info(self, "没有勾选任何项。")
        if not confirm(self, "确认删除",
                       "即将处理 %d 个文件/目录 和 %d 个注册表项。\n\n"
                       "· 文件会移入回收站（可还原）；回收站不可用时才永久删除\n"
                       "· 注册表项删除前会自动导出 .reg 备份到 backup\\uninstall\n"
                       "· 系统目录（C:\\Users、C:\\ProgramData 等）会被直接拒绝\n\n"
                       "确认后逐条处理，随时可以中止。"
                       % (len(files), len(regs)), danger=True):
            return
        self._queue = ([("file", p) for p in files] + [("reg", g) for g in regs])
        self._ok_n = self._fail_n = 0
        self._backups = []
        self._busy = True
        # 切到进度页（处理时逐条显示日志）
        self.ed_q.setVisible(False)
        self.cmb_kind.setVisible(False)
        self.tbl.setVisible(False)
        self.prog.setVisible(True)
        self.lb_prog.setVisible(True)
        self.log.setVisible(True)
        self.prog.setRange(0, len(self._queue))
        self.prog.setValue(0)
        self.log.clear()
        self.btn_del.setEnabled(False)
        self.btn_backup.setEnabled(False)
        self.btn_close.setText("中止")
        self.lb_sel.setText("正在处理 0 / %d …" % len(self._queue))
        QTimer.singleShot(80, self._step)

    def _step(self):
        """逐条处理：每条单独调一次服务端，拿到真实结果写一行日志。"""
        if not self._busy or not self.isVisible():
            return                       # 已中止 / 窗口已关
        if not self._queue:
            return self._finish()
        kind, item = self._queue.pop(0)
        done = self.prog.value() + 1
        self.prog.setValue(done)
        if kind == "file":
            desc = str(item.get("path") or "")
        else:
            desc = "%s\\%s" % (item.get("hive"), item.get("path"))
        self.lb_prog.setText("正在处理 %d / %d：%s"
                             % (done, self.prog.maximum(), desc[:76]))
        QApplication.processEvents()     # 让进度先画出来（单条处理很快）
        if kind == "file":
            r = S.residual_clean([item], [])
        else:
            r = S.residual_clean([], [item])
        ok = bool(r.get("ok"))
        self._ok_n += 1 if ok else 0
        self._fail_n += 0 if ok else 1
        self._backups += list(r.get("backups") or [])
        msgs = list(r.get("msg") or [])[:3] or ["（无返回信息）"]
        for m in msgs:
            self._log(m, ok)
        self.lb_sel.setText("正在处理 %d / %d …（成功 %d · 失败 %d）"
                            % (done, self.prog.maximum(), self._ok_n, self._fail_n))
        QTimer.singleShot(20, self._step)

    def _log(self, text, ok=True):
        it = QListWidgetItem(text)
        it.setForeground(QColor(T.get_color("OK" if ok else "ERR", self._dark)))
        self.log.addItem(it)
        self.log.scrollToBottom()

    def _finish(self):
        self._busy = False
        self.prog.setValue(self.prog.maximum())
        if self._backups:
            self._log("注册表已备份 %d 个 .reg → %s（双击 .reg 即可还原）"
                      % (len(self._backups), os.path.join(S.BACKUP, "uninstall")), True)
        self._log("完成：%d 项成功，%d 项失败 / 被拒。"
                  % (self._ok_n, self._fail_n), self._fail_n == 0)
        self.lb_prog.setText("已全部处理完毕")
        self.lb_sel.setText("处理完成：成功 %d · 失败 %d" % (self._ok_n, self._fail_n))
        self.btn_close.setText("完成")
        self.btn_backup.setEnabled(
            os.path.isdir(os.path.join(S.BACKUP, "uninstall")))
        if self.on_done:
            self.on_done()               # 让底下的列表先刷新

    def _open_backup(self):
        d = os.path.join(S.BACKUP, "uninstall")
        if not os.path.isdir(d):
            return info(self, "还没有备份目录 —— 删除注册表项之后才会生成。")
        try:
            os.startfile(d)              # noqa: 打开资源管理器定位备份
        except OSError as e:
            warn(self, "打开备份文件夹失败：%s" % e)


# ==========================================================================
# 5.5 CPU 调试（大小核调度 / 进程亲和 / 优先级）
# ==========================================================================
HELP_CPU = (
    "① CPU 实时状态 —— 每颗核心的占用率与频率，每秒刷新，只读不改。\n"
    "② 大小核调度 —— 先在下面的进程表里选中一个进程（可先搜索名字），再点「绑到 P 核 / "
    "E 核 / 全核」：绑定后该进程只跑在指定核心上，适合把游戏钉在 P 核、把后台压在 E 核。\n"
    "③ 调度增强 —— 勾选任意核心组合后点「应用到选中进程」；勾上「强亲和」会同时作用于该进程"
    "的所有线程；再往下还能单独调内存 / IO 优先级、释放工作集。\n"
    "④ 电源计划（高性能 / 节能 / 处理器最小最大状态 / EPP）已移到左侧「电源管理」页。"
)


class CpuTunePage(Page):
    title = "CPU 调试"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        self.topo = None
        self._core_cells = {}

        # ---- 1. 实时状态 ----
        f1, v1 = card("CPU 实时状态")
        self.lb_name = QLabel("读取中…"); self.lb_name.setObjectName("Muted")
        self.lb_name.setWordWrap(True)
        self.pill_temp = pill("温度 —", "blue")
        row0 = QHBoxLayout()
        row0.addWidget(self.lb_name, 1)
        row0.addWidget(self.pill_temp)
        v1.addLayout(row0)
        self.core_grid = QGridLayout()
        self.core_grid.setSpacing(6)
        v1.addLayout(self.core_grid)
        root.addWidget(f1)

        # ---- 2. 使用说明（本页怎么用，一看就懂） ----
        f_help, v_help = card("使用说明")
        v_help.addWidget(notice(HELP_CPU, "info"))
        root.addWidget(f_help)

        # ---- 3. 大小核调度 ----
        f3, v3 = card("大小核调度（Intel 混合架构）")
        self.lb_topo = QLabel("读取中…"); self.lb_topo.setObjectName("Muted")
        self.lb_topo.setWordWrap(True)
        v3.addWidget(self.lb_topo)
        r4 = QHBoxLayout()
        self.proc_q = QLineEdit()
        self.proc_q.setPlaceholderText("搜索进程名")
        self.proc_q.setMaximumWidth(240)
        self.proc_q.textChanged.connect(self._paint_procs)
        r4.addWidget(self.proc_q)
        self.proc_n = pill("—", "")
        r4.addWidget(self.proc_n)
        r4.addStretch(1)
        r4.addWidget(btn("绑到 P 核", "Primary", on_click=lambda: self.set_affinity("P")))
        r4.addWidget(btn("绑到 E 核", on_click=lambda: self.set_affinity("E")))
        r4.addWidget(btn("全核", on_click=lambda: self.set_affinity("ALL")))
        self.btn_procs = btn("刷新进程", on_click=lambda: self.load_procs(True))
        r4.addWidget(self.btn_procs)
        v3.addLayout(r4)

        # 进程列表：图标 + 名称 / PID / 内存 / CPU / GPU（与内存性能页同款交互）
        self.proc_table = make_table(["进程", "PID", "内存", "CPU", "GPU"], sortable=True)
        attach_reveal(self.proc_table,
                      lambda t, r: t.item(r, 0).data(Qt.UserRole) or "")
        self.proc_table.setMaximumHeight(232)
        self.proc_icons = {}
        v3.addWidget(self.proc_table)

        self.lb_aff = QLabel(""); self.lb_aff.setObjectName("Muted")
        self.lb_aff.setWordWrap(True)
        v3.addWidget(self.lb_aff)
        root.addWidget(f3)

        # ---- 4. 调度增强（自定义核心 / 强亲和 / 优先级 / 工作集） ----
        f35, v35 = card("调度增强（自定义核心 · 优先级 · 工作集）")
        v35.addWidget(notice(
            "针对上表**选中的进程**生效：可绑定任意核心组合；「强亲和」会同时贯穿该进程的"
            "所有线程；还能单独调内存 / IO 优先级、释放工作集。", "info"))
        self.sel_grid = QGridLayout(); self.sel_grid.setSpacing(4)
        v35.addLayout(self.sel_grid)

        r6 = QHBoxLayout()
        r6.addWidget(btn("全选", on_click=lambda: self._sel_cores("all")))
        r6.addWidget(btn("仅 P 核", on_click=lambda: self._sel_cores("P")))
        r6.addWidget(btn("仅 E 核", on_click=lambda: self._sel_cores("E")))
        r6.addWidget(btn("清空", on_click=lambda: self._sel_cores("none")))
        r6.addSpacing(12)
        self.cb_strong = QCheckBox("强亲和（贯穿所有线程）")
        r6.addWidget(self.cb_strong)
        r6.addStretch(1)
        r6.addWidget(btn("应用到选中进程", "Primary", on_click=self.apply_core_sel))
        r6.addWidget(btn("恢复全部核心", on_click=self.restore_cores))
        v35.addLayout(r6)

        r7 = QHBoxLayout()
        r7.addWidget(QLabel("内存优先级："))
        self.combo_memprio = QComboBox()
        for _lv, _nm in sorted(S.MEM_PRIORITY_NAMES.items()):
            self.combo_memprio.addItem(_nm, _lv)
        self.combo_memprio.setCurrentIndex(3)
        r7.addWidget(self.combo_memprio)
        r7.addWidget(btn("应用", on_click=self.apply_mem_prio))
        r7.addSpacing(14)
        r7.addWidget(QLabel("IO 优先级："))
        self.combo_ioprio = QComboBox()
        for _lv, _nm in sorted(S.IO_PRIORITY_NAMES.items()):
            self.combo_ioprio.addItem(_nm, _lv)
        self.combo_ioprio.setCurrentIndex(2)
        r7.addWidget(self.combo_ioprio)
        r7.addWidget(btn("应用", on_click=self.apply_io_prio))
        r7.addStretch(1)
        v35.addLayout(r7)

        r8 = QHBoxLayout()
        r8.addWidget(btn("释放选中进程工作集", on_click=self.free_ws_sel))
        r8.addWidget(btn("释放全部进程工作集", on_click=self.free_ws_all))
        r8.addStretch(1)
        v35.addLayout(r8)

        self.lb_sched = QLabel(""); self.lb_sched.setObjectName("Muted")
        self.lb_sched.setWordWrap(True)
        v35.addWidget(self.lb_sched)
        root.addWidget(f35)


        root.addStretch(1)

        # ---- 定时器 ----
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    # ---------- 生命周期 ----------
    def refresh(self):
        # 加 _topo_loading 门闩：refresh 与 1 秒 tick 可能同时进来，
        # 没有它就会各起一次 CPU 拓扑查询（那要读注册表 + 计算，不该跑两遍）
        if self.topo is None and not getattr(self, "_topo_loading", False):
            self._topo_loading = True

            def _topo_fail(*_a):
                self._topo_loading = False
            Tasks.run(self, S.cpu_topology, self._got_topo, on_fail=_topo_fail)
        self.load_procs()

    def on_exit(self):
        pass

    # ---------- 拓扑 / 渲染 ----------
    def _got_topo(self, t):
        self._topo_loading = False
        self.topo = t
        if not t:
            self.lb_name.setText("无法读取 CPU 拓扑")
            return
        self.lb_name.setText("%s  ·  %d 逻辑处理器（P 核 %d / E 核 %d）  ·  基频 %d MHz"
                             % (t["name"], t["logical"], t["p_cores"], t["e_cores"],
                                t["base_mhz"]))
        pset = set(t["p_logicals"])
        # 每逻辑处理器一个小单元：P 核绿框、E 核灰框
        while self.core_grid.count():
            it = self.core_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._core_cells = {}
        for i in range(t["logical"]):
            f = QFrame(); f.setObjectName("CatCard")
            fl = QVBoxLayout(f); fl.setContentsMargins(8, 6, 8, 6); fl.setSpacing(2)
            tag = "P" if i in pset else "E"
            lb = QLabel("%s%d" % (tag, i)); lb.setObjectName("Muted")
            bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0)
            bar.setTextVisible(False)
            bar.setObjectName("Gpu" if tag == "P" else "Disk")
            val = QLabel("—"); val.setObjectName("Mono")
            fl.addWidget(lb); fl.addWidget(bar); fl.addWidget(val)
            self.core_grid.addWidget(f, i // 4, i % 4)
            self._core_cells[i] = (bar, val)
        self._build_core_sel(t)          # 同步「调度增强」里的核心勾选框

    def tick(self):
        if not page_active(self):
            return
        if self.topo is None and not getattr(self, "_topo_loading", False):
            self._topo_loading = True
            Tasks.run(self, S.cpu_topology, self._got_topo,
                      on_fail=lambda *_: setattr(self, "_topo_loading", False))
        Tasks.run(self, S.cpu_per_core, self._got_core,
                  on_fail=lambda *_: None)  # 刻意静默：1 秒轮询，不能弹框
        Tasks.run(self, S.cpu_temp, lambda v: self.pill_temp.setText(
            "温度 %s" % fmt_temp(v)), on_fail=lambda *_: None)  # 刻意静默：1 秒轮询，不能弹框

    def _got_core(self, d):
        if not d:
            return
        for c in d.get("cores", []):
            cell = self._core_cells.get(c["id"])
            if not cell:
                continue
            bar, val = cell
            u = c.get("util")
            m = c.get("mhz")
            bar.setValue(int(u or 0))
            val.setText("%d%% · %s" % (round(u or 0),
                                       ("%dMHz" % round(m)) if m else "—"))

    # ---------- 调度控制 ----------
    # ---------- 大小核 ----------
    def load_procs(self, force=False):
        '''进程列表：一次运行只读一遍；点「刷新进程」才重新枚举。'''
        if force:
            self.sess_invalidate("procs")
        Tasks.run_cached(self, "procs", lambda: S.list_processes(300),
                         self._got_procs,
                         on_fail=lambda *_: self.proc_n.setText("读取失败"))

    def _got_procs(self, procs):
        self._procs = procs or []
        self._paint_procs()

    def _paint_procs(self):
        """进程表：搜索过滤 + 程序图标（与软件管理页同款）。"""
        kw = self.proc_q.text().strip().lower()
        rows = [p for p in getattr(self, "_procs", [])
                if not kw or kw in (p["name"] or "").lower()]
        tbl = self.proc_table
        tbl.setSortingEnabled(False)
        tbl.setRowCount(0)
        for p in rows:
            i = tbl.rowCount()
            tbl.insertRow(i)
            it0 = QTableWidgetItem(p["name"])
            it0.setFlags(it0.flags() & ~Qt.ItemIsEditable)
            it0.setIcon(self._proc_icon(p))
            it0.setToolTip("%s\n%s" % (p["name"], p.get("exe") or "(无路径信息)"))
            it0.setData(Qt.UserRole, p.get("exe") or "")   # 右键「定位文件夹」用
            tbl.setItem(i, 0, it0)
            it1 = SortItem(str(p["pid"]))
            it1.setFlags(it1.flags() & ~Qt.ItemIsEditable)
            it1.setData(Qt.UserRole + 1, p["pid"])       # 排序后仍能取回真实 PID
            tbl.setItem(i, 1, it1)
            it2 = SortItem(fmt_bytes(p["mem"]))
            it2.setFlags(it2.flags() & ~Qt.ItemIsEditable)
            it2.setData(Qt.UserRole + 1, p["mem"])
            tbl.setItem(i, 2, it2)
            # CPU / GPU：数值存 UserRole+1 按数值排序；取不到显示「—」
            for c, key in ((3, "cpu"), (4, "gpu")):
                v = p.get(key)
                it = SortItem(("%.1f%%" % v) if v is not None else "—")
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if v is not None:
                    it.setData(Qt.UserRole + 1, float(v))
                tbl.setItem(i, c, it)
            tbl.setRowHeight(i, 28)
        tbl.setSortingEnabled(True)
        tbl.setColumnWidth(0, 230)
        tbl.setColumnWidth(1, 72)
        tbl.setColumnWidth(2, 92)
        tbl.setColumnWidth(3, 64)
        tbl.setColumnWidth(4, 64)
        self.proc_n.setText("%d 个" % len(rows))
        self._proc_rows = rows

    def _proc_icon(self, p):
        """按 exe 路径取系统图标（按路径缓存，同一程序的多进程共用）。"""
        exe = (p.get("exe") or "").strip()
        key = exe.lower() or ("pid_" + str(p.get("pid")))
        hit = self.proc_icons.get(key)
        if hit is not None:
            return hit
        from PySide6.QtWidgets import QFileIconProvider
        icon = QFileIconProvider().icon(QFileIconProvider.File)
        if exe and os.path.exists(exe):
            try:
                icon = QFileIconProvider().icon(QFileInfo(exe))
            except Exception:
                pass
        self.proc_icons[key] = icon
        return icon

    def _selected_pid(self):
        """当前选中进程的 PID。表格可排序，所以 PID 存在单元格 UserRole 里。"""
        tbl = self.proc_table
        r = tbl.currentRow()
        if r < 0:
            return None
        it = tbl.item(r, 1)
        return it.data(Qt.UserRole + 1) if it else None

    def set_affinity(self, target):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        pid = self._selected_pid()
        if not pid:
            return info(self, "请先在列表里选择一个进程。")
        rec = next((x for x in getattr(self, "_proc_rows", [])
                    if x["pid"] == pid), None)
        name = rec["name"] if rec else ("PID %s" % pid)
        what = {"P": "P 核", "E": "E 核"}.get(target, "全部核心")
        r = S.set_process_affinity(int(pid), target)
        if r.get("ok"):
            self.lb_aff.setText("已把 %s（PID %d）绑定到 %s（掩码 0x%X）"
                                % (name, pid, what, r.get("mask", 0)))
        else:
            self.lb_aff.setText("失败：%s" % r.get("err"))

    # ---------- 调度增强（对标 LaoYing 的 Scheduler / MemoryPriority） ----------
    def _build_core_sel(self, topo):
        """按拓扑重建「调度增强」里的核心勾选框（P / E 核用 tooltip 区分）。"""
        while self.sel_grid.count():
            it = self.sel_grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self.core_chk = []
        n = int((topo or {}).get("logical") or 0)
        pset = set((topo or {}).get("p_logicals") or [])
        for c in range(n):
            cb = QCheckBox(str(c))
            cb.setChecked(True)
            cb.setToolTip("P 核（性能核）" if c in pset else "E 核（能效核）")
            self.core_chk.append((c, cb))
            self.sel_grid.addWidget(cb, c // 8, c % 8)

    def _sel_cores(self, mode):
        topo = self.topo or {}
        pset = set(topo.get("p_logicals") or [])
        eset = set(topo.get("e_logicals") or [])
        for c, cb in getattr(self, "core_chk", []):
            cb.setChecked(mode == "all" or (mode == "P" and c in pset)
                          or (mode == "E" and c in eset))

    def _checked_cores(self):
        return [c for c, cb in getattr(self, "core_chk", []) if cb.isChecked()]

    def apply_core_sel(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        pid = self._selected_pid()
        if not pid:
            return info(self, "请先在上面的进程表里选中一个进程。")
        cores = self._checked_cores()
        if not cores:
            return info(self, "请至少勾选一个核心。")
        strong = self.cb_strong.isChecked()
        self.win.busy("正在设置亲和…")
        Tasks.run(self, lambda: S.set_process_affinity_cores(pid, cores, strong),
                  lambda r: (self.win.idle(), self._after_aff(r, pid)),
                  on_fail=lambda m: (self.win.idle(), warn(self, "设置失败：" + m))[1])

    def _after_aff(self, r, pid):
        r = r or {}
        if not r.get("ok"):
            return warn(self, r.get("err") or "设置失败")
        extra = ""
        if r.get("threads"):
            extra = "，同时绑定 %d 个线程%s" % (
                r["threads"], ("（%d 个失败）" % r["failed"]) if r.get("failed") else "")
        self.lb_sched.setText("已把 PID %s 绑定到核心 %s（掩码 0x%X）%s"
                              % (pid, r.get("cores"), r.get("mask", 0), extra))
        self._refresh_sched(pid)

    def restore_cores(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        pid = self._selected_pid()
        if not pid:
            return info(self, "请先在上面的进程表里选中一个进程。")
        Tasks.run(self, lambda: S.restore_process_affinity(pid),
                  lambda r: (self.win.idle(), self._after_aff(r, pid)),
                  on_fail=lambda m: (self.win.idle(), warn(self, "恢复失败：" + m))[1])

    def apply_mem_prio(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        pid = self._selected_pid()
        if not pid:
            return info(self, "请先在上面的进程表里选中一个进程。")
        lv = self.combo_memprio.currentData()
        Tasks.run(self, lambda: S.set_process_memory_priority(pid, lv),
                  lambda r: (self.win.idle(), self._after_prio("内存", r, pid)),
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def apply_io_prio(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        pid = self._selected_pid()
        if not pid:
            return info(self, "请先在上面的进程表里选中一个进程。")
        lv = self.combo_ioprio.currentData()
        Tasks.run(self, lambda: S.set_process_io_priority(pid, lv),
                  lambda r: (self.win.idle(), self._after_prio("IO", r, pid)),
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def _after_prio(self, kind, r, pid):
        r = r or {}
        if not r.get("ok"):
            return warn(self, r.get("err") or "设置失败")
        self.lb_sched.setText("已把 PID %s 的%s优先级设为 %s"
                              % (pid, kind, r.get("level")))
        self._refresh_sched(pid)

    def free_ws_sel(self):
        pid = self._selected_pid()
        if not pid:
            return info(self, "请先在上面的进程表里选中一个进程。")
        Tasks.run(self, lambda: S.release_working_set(pid),
                  lambda r: self.lb_sched.setText(
                      "已释放 PID %s 的工作集" % pid if (r or {}).get("ok")
                      else "释放失败：%s" % (r or {}).get("err")),
                  on_fail=lambda m: self.lb_sched.setText("释放失败：" + m))

    def free_ws_all(self):
        self.win.busy("正在释放全部进程工作集…")
        Tasks.run(self, S.release_working_set_all,
                  lambda r: (self.win.idle(), self.lb_sched.setText(
                      "已释放 %d 个进程的工作集（跳过 %d 个无权限进程）"
                      % ((r or {}).get("done", 0), (r or {}).get("skipped", 0))))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def _refresh_sched(self, pid):
        Tasks.run(self, lambda: S.process_sched_state(pid),
                  lambda r: self._show_sched(pid, r),
                  on_fail=lambda m: self.load_fail(
                      self.lb_sched, "该进程的调度状态", m))

    def _show_sched(self, pid, r):
        r = r or {}
        if not r.get("ok"):
            return
        self.lb_sched.setText(
            "PID %s 当前：亲和 0x%X（%d 个核心）· 优先级 %s · 内存优先级 %s"
            % (pid, r.get("affinity", 0), bin(r.get("affinity", 0)).count("1"),
               r.get("priority_name"), r.get("memory_name")))

# ==========================================================================
# 5. 显卡伪装（独立大类）
# ==========================================================================
class PowerPage(Page):
    """电源管理：电源计划的启用 / 删除 / 修改，以及处理器性能参数。

    单独成页的原因：电源计划相关的功能还会继续加（计划列表、参数、导入导出…），
    继续塞在「CPU 调试」里既挤又难找。
    """
    title = "电源管理"

    def __init__(self, win):
        super().__init__(win)
        self._plans = []
        self._pow_items = []          # 候选（本机 .pow + 已安装计划）
        self._pow_map = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(notice(
            "电源计划决定 CPU 在性能与省电之间怎么取舍：换计划、改参数都会立即写入系统。"
            "计划列表上右键 —— 第一项「删除电源计划」、第二项「修改电源计划」（走控制面板）。",
            "info"))

        # ---- 1. 内置模板一键启用 ----
        row_t = QHBoxLayout(); row_t.setSpacing(12)
        for kind, tip in (("high", "Windows 内置模板，适合游戏与多数生产力场景。"),
                          ("ultimate", "从 Windows 官方模板复制后启用（默认隐藏）。"),
                          ("saver", "压低功耗与发热，笔记本在外时用。")):
            label = (S.POWER_TEMPLATES.get(kind) or ("", ""))[0] or kind
            f, v = card(label, right=btn("启用", "Primary",
                                        on_click=lambda _=False, k=kind: self.use_template(k)))
            v.addWidget(notice(tip, "info"))
            row_t.addWidget(f, 1)
        root.addLayout(row_t)

        # ---- 2. 计划列表（右键：删除 / 修改）----
        f2, v2 = card("电源计划列表", right=btn("刷新", on_click=self.reload))
        self.plan_table = make_table(["计划名称", "状态", "计划 GUID"], sortable=True)
        self.plan_table.setMaximumHeight(196)
        attach_row_menu(self.plan_table, self._plan_menu, standalone=True)
        v2.addWidget(self.plan_table)
        # 「计划被外部改动」提示：默认隐藏，只有真发生了才显示
        self.lb_drift = QLabel("")
        self.lb_drift.setObjectName("NoticeWarn")
        self.lb_drift.setWordWrap(True)
        self.lb_drift.setVisible(False)
        v2.addWidget(self.lb_drift)
        # 本软件自己切过计划（用来区分「用户点的」和「外部程序改的」）
        self._switched_by_us = False
        v2.addWidget(notice(
            "当前生效的计划不能删除 —— 想删它，先用右键「设为当前计划」切到别的计划。", "info"))
        prow = QHBoxLayout()
        b_reset = btn("恢复默认电源计划", on_click=self.reset_defaults)
        b_reset.setToolTip("切回「平衡」计划，并把处理器参数恢复为 Windows 默认值"
                           "（只动这一个计划，不会删掉你自建的其他计划）")
        prow.addWidget(b_reset)
        prow.addStretch(1)
        v2.addLayout(prow)
        root.addWidget(f2)

        # ---- 3. 处理器性能参数 ----
        f3, v3 = card("处理器性能参数")
        v3.addWidget(notice("下面这些参数作用于上面「电源计划」下拉里选中的那个计划。", "info"))
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("电源计划："))
        self.plan_combo = QComboBox()
        self.plan_combo.currentIndexChanged.connect(self.on_plan_selected)
        r1.addWidget(self.plan_combo, 1)
        r1.addWidget(btn("设为当前计划", "Primary", on_click=self.apply_plan))
        v3.addLayout(r1)
        self.lb_plan_state = QLabel("")
        self.lb_plan_state.setObjectName("Muted")
        self.lb_plan_state.setWordWrap(True)
        v3.addWidget(self.lb_plan_state)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("处理器最小状态 %："))
        self.spin_min = QSpinBox(); self.spin_min.setRange(0, 100)
        r2.addWidget(self.spin_min)
        r2.addWidget(QLabel("最大状态 %："))
        self.spin_max = QSpinBox(); self.spin_max.setRange(0, 100)
        r2.addWidget(self.spin_max)
        r2.addWidget(btn("应用到该计划", "Primary", on_click=self.apply_states))
        r2.addStretch(1)
        v3.addLayout(r2)
        v3.addWidget(notice(
            "最小 / 最大状态是 CPU 频率的百分比区间：想让它一直跑满就都设 100，"
            "想压温度可以设「最大 80」。", "info"))

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("能效偏好 EPP（0=最高性能，100=最省电）："))
        self.slider_epp = QSlider(Qt.Horizontal)
        self.slider_epp.setRange(0, 100)
        self.lb_epp = QLabel("—"); self.lb_epp.setObjectName("Mono")
        self.slider_epp.valueChanged.connect(lambda v: self.lb_epp.setText(str(v)))
        r3.addWidget(self.slider_epp, 1)
        r3.addWidget(self.lb_epp)
        r3.addWidget(btn("应用到该计划", "Primary", on_click=self.apply_epp))
        v3.addLayout(r3)

        # EPP 快捷档位：不用自己去猜 0-100 该怎么填
        p_row = QHBoxLayout(); p_row.setSpacing(8)
        p_row.addWidget(QLabel("快捷档位："))
        for _lab, _val, _tip in (("省电", 80, "偏向省电：发热与风扇噪音更低"),
                                 ("均衡", 50, "Windows 平衡计划的默认值"),
                                 ("性能", 0, "偏向性能：插电使用或打游戏时用")):
            _b = btn(_lab, on_click=lambda _c=False, v=_val: self.apply_epp_preset(v))
            _b.setToolTip("EPP = %d，%s" % (_val, _tip))
            p_row.addWidget(_b)
        p_row.addStretch(1)
        v3.addLayout(p_row)
        root.addWidget(f3)

        # ---- 4. 导入 .pow 电源计划 ----
        self.btn_pow_scan = btn("重新扫描", on_click=self.scan_pow)
        f4, v4 = card("导入 .pow 电源计划", right=self.btn_pow_scan)
        v4.addWidget(notice(
            "自动扫描本机文件；导入由 Windows 完成，之后会回读计划列表验证是否真的成功。"
            "列表里同时列出本机 .pow 与已安装的计划，选中后点「导入并启用」。", "info"))
        self.pow_table = make_table(["本机文件 / 计划", "来源"], sortable=False, wrap=False)
        self.pow_table.setMaximumHeight(180)
        v4.addWidget(self.pow_table)
        self.lb_pow = QLabel("尚未扫描。")
        self.lb_pow.setObjectName("Muted")
        self.lb_pow.setWordWrap(True)
        v4.addWidget(self.lb_pow)
        r4 = QHBoxLayout()
        r4.addWidget(btn("选择其他文件", on_click=self.pick_pow))
        r4.addStretch(1)
        r4.addWidget(btn("导入并启用", "Primary", on_click=self.import_pow))
        v4.addLayout(r4)
        root.addWidget(f4)

        # ---- 5. 各计划参数对比 ----
        self.btn_cmp = btn("读取各计划参数", on_click=self.load_plan_states)
        f5, v5 = card("各计划参数对比", right=self.btn_cmp)
        v5.addWidget(notice(
            "把每个电源计划保存的处理器参数并排列出来，方便比较「我这台机器上到底有几套参数」。"
            "读取要逐个计划问一次系统（每个约 0.2 秒），所以做成点按钮再读 —— "
            "不想等就不点，不影响别的功能。", "info"))
        self.cmp_table = make_table(
            ["电源计划", "最小状态 %", "最大状态 %", "EPP", "状态"], sortable=True)
        self.cmp_table.setMaximumHeight(190)
        v5.addWidget(self.cmp_table)
        self.lb_cmp = QLabel("尚未读取。")
        self.lb_cmp.setObjectName("Muted")
        self.lb_cmp.setWordWrap(True)
        v5.addWidget(self.lb_cmp)
        root.addWidget(f5)
        root.addStretch(1)

    # ---------- 生命周期 ----------
    def refresh(self):
        self._cmp_rows = None          # 计划可能变了，参数对比要重读
        Tasks.run_cached(self, "plans", S.cpu_plans, self._got_plans,
                         on_fail=lambda m: self.load_fail(
                             self.lb_plan_state, "电源计划列表", m))
        if not self._pow_items:          # 只在首次进入时扫一次，之后用「重新扫描」
            self.scan_pow()

    def _got_plans(self, plans):
        self._plans = plans or []
        cur = self.plan_combo.currentData()
        self.plan_combo.blockSignals(True)
        self.plan_combo.clear()
        for p in self._plans:
            label = p["name"] or p["guid"]
            if p.get("active"):
                label += "（当前生效）"
            self.plan_combo.addItem(label, p["guid"])
        if cur:
            ix = self.plan_combo.findData(cur)
            self.plan_combo.setCurrentIndex(ix if ix >= 0 else 0)
        self.plan_combo.blockSignals(False)
        self._paint_plans()
        self.on_plan_selected()
        self._check_plan_drift()

    def _check_plan_drift(self):
        """比对「启动时的计划」与「现在的」—— 变了且不是本软件切的，如实说。

        ⚠️ 只提示、**不自动改回去**：切不切是用户的决定，替用户动系统状态
        是更糟的做法。用户想切回，用上面的「设为当前计划」。
        """
        cur = next((x for x in self._plans if x.get("active")), None)
        if not cur:
            self.lb_drift.setVisible(False)
            return
        now = (cur.get("guid") or "").lower()
        if self._switched_by_us:
            # 自己点的切换：把基准挪到当前，不提示
            self._switched_by_us = False
            self.win.plan_at_start = {"guid": now,
                                      "name": cur.get("name") or "", "error": ""}
            self.lb_drift.setVisible(False)
            return
        snap = getattr(self.win, "plan_at_start", None) or {}
        base = (snap.get("guid") or "").lower()
        if not base or base == now:            # 快照还没到 / 没变
            self.lb_drift.setVisible(False)
            return
        bn = snap.get("name") or base
        cn = cur.get("name") or now
        self.lb_drift.setText(
            "⚠️ 本软件启动时生效的是「%s」，现在是「%s」——<b>中间没有在本软件里"
            "点过切换</b>，说明是别的程序（厂商控制中心 / 系统自己等）改的。<br>"
            "要切回原计划：在上面选中它，点「设为当前计划」。"
            % (bn, cn))
        self.lb_drift.setVisible(True)

    def _paint_plans(self):
        rows, keys = [], []
        for p in self._plans:
            rows.append([p.get("name") or "(未命名)",
                         "当前生效" if p.get("active") else "未使用",
                         p.get("guid") or ""])
            keys.append(p.get("guid") or "")
        fill_table(self.plan_table, rows, keys=keys)
        self.plan_table.setColumnWidth(0, 260)
        self.plan_table.setColumnWidth(1, 90)

    def _plan_by_guid(self, guid):
        for p in self._plans:
            if (p.get("guid") or "").lower() == (guid or "").lower():
                return p
        return None

    # ---------- 快捷档位 / 参数对比 / 恢复默认 ----------
    def apply_epp_preset(self, val):
        """EPP 快捷档位：先动滑块，再走和「应用」完全相同的写入路径。"""
        self.slider_epp.setValue(int(val))
        self.apply_epp()

    def load_plan_states(self, force=False):
        """读取各计划的处理器参数（耗时操作，按需触发）。"""
        if getattr(self, "_cmp_rows", None) and not force:
            return
        self.btn_cmp.setEnabled(False)
        self.lb_cmp.setText("正在逐个读取各计划的处理器参数…")
        self.win.busy("正在读取各计划参数…")
        Tasks.run(self, S.power_plans_states, self._got_plan_states,
                  on_fail=lambda m: (self.win.idle(), self.btn_cmp.setEnabled(True),
                                     self.lb_cmp.setText("读取失败：%s" % m))[-1])

    def _got_plan_states(self, rows):
        self.win.idle()
        self.btn_cmp.setEnabled(True)
        self._cmp_rows = rows or []

        def fmt(v):
            return "—" if v is None else str(v)

        data, keys, n_ok = [], [], 0
        for r in self._cmp_rows:
            if r.get("min") is not None or r.get("max") is not None:
                n_ok += 1
            data.append([r.get("name") or "(未命名)", fmt(r.get("min")), fmt(r.get("max")),
                         fmt(r.get("epp")), "当前生效" if r.get("active") else ""])
            keys.append(r.get("guid") or "")
        fill_table(self.cmp_table, data, keys=keys)
        for c, w in ((0, 240), (1, 96), (2, 96), (3, 70), (4, 88)):
            self.cmp_table.setColumnWidth(c, w)
        self.lb_cmp.setText("共 %d 个计划，其中 %d 个读到了处理器参数；"
                            "「—」表示该系统/计划未提供该项。"
                            % (len(self._cmp_rows), n_ok))

    def reset_defaults(self):
        """恢复默认电源计划：切回平衡 + 处理器参数回默认。"""
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        label = (S.POWER_TEMPLATES.get("balanced") or ("平衡",))[0]
        if not confirm(self, "恢复默认电源计划",
                       "将把当前计划切回「%s」，并把它保存的处理器参数恢复为 Windows 默认值：\n"
                       "　最小状态 5%%　/　最大状态 100%%　/　能效偏好 EPP 50\n\n"
                       "只会改动这一个计划 —— 你自己创建的其他计划不会被删除。\n"
                       "（不会执行 powercfg -restoredefaultschemes，那会把自建计划一起清掉。）"
                       % label, danger=False):
            return
        self.win.busy("正在恢复默认电源计划…")
        Tasks.run(self, S.reset_power_defaults,
                  lambda r: (self.win.idle(), self._plan_done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "恢复失败", m))[1])

    # ---------- 计划列表右键 ----------
    def _plan_menu(self, menu, table, row):
        it = table.item(row, 0)
        guid = it.data(Qt.UserRole) if it is not None else None
        p = self._plan_by_guid(guid)
        if p is None:
            return
        active = bool(p.get("active"))
        # 第 1 项固定「删除」、第 2 项固定「修改」
        a_del = menu.addAction("删除电源计划")
        a_del.setEnabled(self.win.admin_mode and not active)
        a_del.triggered.connect(lambda: self.delete_plan(p))
        a_mod = menu.addAction("修改电源计划")
        a_mod.triggered.connect(lambda: self.modify_plan(p))
        menu.addSeparator()
        a_set = menu.addAction("设为当前计划")
        a_set.setEnabled(self.win.admin_mode and not active)
        a_set.triggered.connect(lambda: self.activate_plan(p))
        a_cp = menu.addAction("复制计划 GUID")
        a_cp.triggered.connect(lambda: copy_text(p.get("guid") or ""))
        a_cpn = menu.addAction("复制计划名称")
        a_cpn.triggered.connect(lambda: copy_text(p.get("name") or ""))

    def delete_plan(self, p):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if p.get("active"):
            return info(self, "不能删除当前生效的计划", "请先切换到别的计划，再删除它。")
        if not confirm(self, "删除电源计划",
                       "确定删除「%s」？\n\n该计划连同它保存的参数一起被移除，"
                       "操作不可撤销（Windows 自带的那几个还能重新启用回来）。"
                       % (p.get("name") or p.get("guid")), danger=True):
            return
        self.win.busy("正在删除电源计划…")
        Tasks.run(self, lambda: S.power_plan_delete(p.get("guid")),
                  lambda r: (self.win.idle(), self._plan_done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "删除失败", m))[1])

    def activate_plan(self, p):
        # 注意：切换电源计划**不需要管理员**（powercfg /setactive 对当前用户生效），
        # 所以这里不加 admin 闸 —— 加了反而把普通用户能做的事挡掉。
        r = S.cpu_set_plan(p.get("guid"))
        if r.get("ok"):
            self._switched_by_us = True
            self.reload()                 # 切换计划是写操作：丢缓存重读
            info(self, "已切换电源计划", "当前生效：%s" % (p.get("name") or ""))
        else:
            warn(self, "切换失败", r.get("err") or "")

    def modify_plan(self, p):
        """打开控制面板的电源计划修改界面。

        `control.exe powercfg.cpl,,1` 是「更改计划设置」，但它只认**当前生效**的计划；
        目标不是当前计划时退回「电源选项」主页，让用户自己点那一条的「更改计划设置」
        —— 比先偷偷切过去改完再切回来安全得多。
        """
        if p.get("active"):
            ok = open_power_control(True)
            tip = "已打开「更改计划设置」，改完点保存即可。"
        else:
            ok = open_power_control(False)
            tip = ("「%s」不是当前生效的计划，而控制面板只能直接修改当前计划，"
                   "所以打开了「电源选项」主页 —— 在该计划上点「更改计划设置」即可。"
                   % (p.get("name") or ""))
        if not ok:
            return warn(self, "打开控制面板失败", "也可以手动运行：control powercfg.cpl")
        info(self, "已打开电源计划设置", tip)

    # ---------- 处理器参数 ----------
    def on_plan_selected(self):
        """切换下拉时读取该计划存储的处理器参数（含未生效的计划）。"""
        guid = self.plan_combo.currentData()
        active = next((p for p in self._plans if p.get("active")), None)
        if active and guid == active.get("guid"):
            self.lb_plan_state.setText("该计划当前生效，修改参数立即生效。")
        else:
            self.lb_plan_state.setText("该计划未生效：显示的是它保存的参数，"
                                       "点「应用到该计划」写入后，切换到它时生效。")
        if not guid:
            return
        Tasks.run_cached(self, "pstates:" + guid,
                         lambda: S.cpu_proc_states(guid), self._got_states,
                         on_fail=lambda m: self.load_fail(
                             self.lb_plan_state, "该电源计划的参数", m))

    def apply_plan(self):
        guid = self.plan_combo.currentData()
        if not guid:
            return
        # 同上：切换计划不需要管理员（写「计划里的参数」才需要）
        r = S.cpu_set_plan(guid)
        if r.get("ok"):
            self._switched_by_us = True      # 是我们改的，别误报成「被外部改」
            self.reload()
            info(self, "电源计划已切换。")
        else:
            warn(self, "切换失败：%s" % r.get("err"))

    def _got_states(self, st):
        if not st:
            return
        if st.get("min") is not None:
            self.spin_min.setValue(int(st["min"]))
        if st.get("max") is not None:
            self.spin_max.setValue(int(st["max"]))
        if st.get("epp") is not None:
            self.slider_epp.setValue(int(st["epp"]))
            self.lb_epp.setText(str(int(st["epp"])))
        else:
            self.lb_epp.setText("未设置（用默认）")

    def _states_done(self, guid, errs):
        """写处理器参数后的统一收尾：**先清缓存回读，再弹提示**。

        不回读的话，切走再切回会命中旧缓存（`on_plan_selected` 的 `pstates:<guid>`），
        界面又把刚写进去的值显示回修改前的样子，用户以为根本没生效。
        """
        self.sess_invalidate("pstates:" + (guid or ""))
        self.on_plan_selected()          # 立刻重读，界面显示系统里的真实值
        if errs:
            warn(self, "应用失败", "；".join(errs))
        else:
            info(self, "已写入该计划", "已写入并回读确认。")

    def apply_states(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        mn, mx = self.spin_min.value(), self.spin_max.value()
        guid = self.plan_combo.currentData()
        if mn > mx:
            return warn(self, "最小状态不能大于最大状态。")
        Tasks.run(self,
                  lambda: (S.cpu_set_state("min", mn, guid), S.cpu_set_state("max", mx, guid)),
                  lambda r: self._states_done(
                      guid, [x.get("err", "") or "写入失败" for x in r if not x.get("ok")]),
                  on_fail=lambda m: (self.sess_invalidate("pstates:" + (guid or "")),
                                     warn(self, "失败：" + str(m)))[1])

    def apply_epp(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        v = self.slider_epp.value()
        guid = self.plan_combo.currentData()
        Tasks.run(self, lambda: S.cpu_set_state("epp", v, guid),
                  lambda r: self._states_done(guid, [] if r.get("ok") else [r.get("err") or ""]),
                  on_fail=lambda m: (self.sess_invalidate("pstates:" + (guid or "")),
                                     warn(self, "失败：" + str(m)))[1])

    # ---------- 内置模板 ----------
    def use_template(self, kind):
        label = (S.POWER_TEMPLATES.get(kind) or ("", ""))[0]
        if not label:
            return
        tip = ("该系统模板在多数机器上处于隐藏状态，会先用 Windows 官方模板"
               "复制一份再启用。" if kind == "ultimate"
               else "会把它设为当前生效的电源计划。")
        if not confirm(self, "切换电源计划", "即将启用「%s」。\n\n%s" % (label, tip)):
            return
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        self.win.busy("正在启用%s…" % label)
        Tasks.run(self, lambda: S.power_plan_activate_template(kind),
                  lambda r: (self.win.idle(), self._plan_done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def _plan_done(self, r):
        r = r or {}
        if r.get("ok"):
            self._switched_by_us = True      # 可能切了计划（模板 / 恢复默认）
            info(self, "操作完成", r.get("msg") or "已完成。")
        else:
            warn(self, "操作未完成", r.get("msg") or "操作未完成。")
        self.reload()

    # ---------- .pow 导入 ----------
    def scan_pow(self, extra=None):
        """扫描本机 .pow 文件 + 已安装计划，填充候选列表。"""
        self.btn_pow_scan.setEnabled(False)
        self.lb_pow.setText("正在查找本机 .pow 文件…")
        Tasks.run(self, lambda: S.power_plan_candidates(extra), self._got_pow,
                  on_fail=lambda m: (self.btn_pow_scan.setEnabled(True),
                                     self.lb_pow.setText("扫描失败：%s" % m))[1])

    def _got_pow(self, d):
        d = d or {}
        self._pow_items = d.get("items") or []
        self.btn_pow_scan.setEnabled(True)
        self._paint_pow()
        n_pow, n_plan = int(d.get("pow_count") or 0), int(d.get("plan_count") or 0)
        if not n_pow and not n_plan:
            self.lb_pow.setText("未在本机找到 .pow 文件，也未能枚举到已安装的电源计划。")
        else:
            self.lb_pow.setText("已发现 %d 个本机电源计划文件，另有 %d 个已安装计划。"
                                % (n_pow, n_plan))

    def _paint_pow(self):
        """候选表：本机文件 / 来源；键存 UserRole（排序后取数必须用它）。"""
        self._pow_map = {}
        rows, keys = [], []
        for x in self._pow_items:
            k = x.get("path") or x.get("guid") or ""
            self._pow_map[k] = x
            rows.append([x.get("name") or k, x.get("source") or ""])
            keys.append(k)
        fill_table(self.pow_table, rows, keys=keys)
        self.pow_table.setColumnWidth(0, 320)

    def pick_pow(self):
        """手动选一个 .pow：加入列表并选中，随后可点「导入并启用」。"""
        p, _ = QFileDialog.getOpenFileName(
            self, "选择电源计划文件", "", "电源计划 (*.pow);;所有文件 (*.*)")
        if not p:
            return
        low = p.lower()
        if not any((x.get("path") or "").lower() == low for x in self._pow_items):
            self._pow_items = [{"kind": "pow",
                                "name": os.path.splitext(os.path.basename(p))[0],
                                "source": "手动选择", "guid": "", "path": p,
                                "active": False}] + list(self._pow_items)
            self._paint_pow()
        for r in range(self.pow_table.rowCount()):
            it = self.pow_table.item(r, 0)
            if it is not None and (it.data(Qt.UserRole) or "").lower() == low:
                self.pow_table.selectRow(r)
                break

    def import_pow(self):
        """对选中项执行「导入并启用」：.pow 走导入 + 回读验证；已安装计划直接启用。"""
        key = picked_key(self.pow_table)
        if not key:
            return info(self, "请先在列表中选择一个电源计划或 .pow 文件。")
        it = self._pow_map.get(key) or {}
        if it.get("kind") == "plan":
            # 启用「已安装的计划」同样不需要管理员（只有导入 .pow 才需要）
            r = S.cpu_set_plan(it.get("guid") or key)
            if r.get("ok"):
                info(self, "已启用", "已启用「%s」。" % (it.get("name") or key))
            else:
                warn(self, "启用失败", r.get("err") or "")
            return self.reload()
        if not os.path.isfile(key):
            return warn(self, "文件不存在", key)
        if not confirm(self, "导入电源计划",
                       "将导入并立即启用：\n\n%s\n\n"
                       "导入由 Windows 完成，之后会回读计划列表验证是否真的导入成功。"
                       % os.path.basename(key)):
            return
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        self.win.busy("正在由 Windows 导入并回读验证…")
        Tasks.run(self, lambda: S.power_plan_import_pow(key, activate=True),
                  lambda r: (self.win.idle(), self._import_done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "导入失败", m))[1])

    def _import_done(self, r):
        r = r or {}
        if r.get("ok"):
            self._switched_by_us = True      # 导入时 activate=True 会切计划
            info(self, "导入完成", r.get("msg") or "已导入并启用。")
        else:
            warn(self, "导入未完成", r.get("msg") or "")
        self.sess_invalidate("plans")     # 新导入的计划要立刻出现在下拉里
        self.scan_pow()


class GpuPage(Page):
    title = "显卡伪装"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        self._gpus = []
        self._backup_file = ""

        # ---- 1. 改写显示名 ----
        # 「刷新设备」要真的重读硬件：用 `reload`（丢缓存），不是 `refresh`
        # （run_cached 第二次会直接回放旧列表，按了跟没按一样）。
        f1, v1 = card("改写显示名", right=btn("刷新设备", on_click=self.reload))
        v1.addWidget(notice(
            "改的是注册表里的<b>显示名</b>：设备管理器读的那一项"
            "（<code>Enum\\设备实例\\DeviceDesc</code>）以及驱动实例里的 "
            "<code>DriverDesc</code> 等，都会一起改，<b>设备管理器 / 任务管理器</b>才会显示新名字。\n"
            "<b>只改名字，不改变任何实际性能</b>；部分反作弊 / 跑分软件会做校验，改完可能报错。"
            "原值会自动备份、可一键还原。\n"
            "改完点「<b>重启显卡</b>」即可立刻生效（只重新加载这一块显卡的驱动，"
            "<b>不用重启电脑</b>，也不改动任何设置）。"
            "GPU-Z 之类<b>直接问显卡驱动</b>的软件不一定跟着变。", "warn"))

        # 大号预览块：横贯整卡，「现在叫什么 → 会变成什么」一眼看清
        self.lb_preview = QLabel("—")
        self.lb_preview.setObjectName("GpuPreview")
        self.lb_preview.setAlignment(Qt.AlignCenter)
        self.lb_preview.setWordWrap(True)
        self.lb_preview.setMinimumHeight(104)
        v1.addWidget(self.lb_preview)

        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(12)
        g.setColumnStretch(1, 1)

        g.addWidget(QLabel("目标显卡"), 0, 0)
        self.gpu_pick = QComboBox()
        self.gpu_pick.setMinimumHeight(40)
        self.gpu_pick.currentIndexChanged.connect(self._on_pick)
        g.addWidget(self.gpu_pick, 0, 1)
        self.lb_count = QLabel("")
        self.lb_count.setObjectName("Muted3")
        g.addWidget(self.lb_count, 0, 2)

        g.addWidget(QLabel("当前状态"), 1, 0)
        self.lb_state = QLabel("—")
        self.lb_state.setObjectName("Muted")
        self.lb_state.setWordWrap(True)
        g.addWidget(self.lb_state, 1, 1, 1, 2)

        g.addWidget(QLabel("预设"), 2, 0)
        self.preset = QComboBox()
        self.preset.setMinimumHeight(40)
        self.preset.addItem("（自定义 / 手填）", "")
        for _gi, (_grp, _names) in enumerate(S.GPU_ALIAS_GROUPS):
            if _gi:                       # 分组之间插一条分隔线（不可选中）
                self.preset.insertSeparator(self.preset.count())
            for _nm in _names:
                self.preset.addItem(_nm, _nm)
        self.preset.currentIndexChanged.connect(self._on_preset)
        g.addWidget(self.preset, 2, 1, 1, 2)

        g.addWidget(QLabel("新显示名"), 3, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setMinimumHeight(44)
        self.name_edit.setPlaceholderText("选预设或直接输入要显示的名称")
        self.name_edit.textChanged.connect(lambda _t: self._refresh_preview())
        # ⚠️ 预览里用了 `<b>…</b>` 做加粗，用户可能把预览文本原样粘回来 →
        #    尖括号会被写进注册表。粘贴时自动清洗一次。
        self.name_edit.textChanged.connect(self._clean_pasted)
        g.addWidget(self.name_edit, 3, 1, 1, 2)
        v1.addLayout(g)

        r1 = QHBoxLayout()
        r1.setSpacing(10)
        b_apply = btn("应用伪装", "Primary", on_click=self.apply_alias)
        b_apply.setMinimumHeight(40)
        b_apply.setMinimumWidth(170)
        b_rev = btn("还原原名", on_click=self.restore_alias)
        b_rev.setMinimumHeight(40)
        b_rev.setMinimumWidth(170)
        r1.addWidget(b_apply)
        r1.addWidget(b_rev)
        # 「重启显卡」：只重启这一个显示设备（不用重启电脑）
        b_gpu = btn("重启显卡", on_click=self.restart_gpu)
        b_gpu.setMinimumHeight(40)
        b_gpu.setMinimumWidth(150)
        b_gpu.setToolTip("只重启所选的这一个显示设备（会重新加载它的驱动，"
                         "屏幕闪一下；不需要重启电脑）")
        r1.addWidget(b_gpu)
        b_live = btn("系统现在报的名字", on_click=self.show_live_names)
        b_live.setMinimumHeight(40)
        b_live.setToolTip("让系统自己报一遍显卡名，用来核对称装到底生效没")
        r1.addWidget(b_live)
        r1.addStretch(1)
        v1.addLayout(r1)
        root.addWidget(f1, 3)

        # ---- 2. 备份与还原 ----
        f2, v2 = card("备份与还原", right=btn("打开备份所在目录", on_click=self.open_backup_dir))
        v2.addWidget(notice(
            "改写前会把该适配器的原显示名写入备份文件，还原成功后对应记录会被移除。", "info"))
        self.lb_backup = QLabel("")
        self.lb_backup.setObjectName("Muted")
        self.lb_backup.setWordWrap(True)
        v2.addWidget(self.lb_backup)
        self.lb_bkpath = QLabel("")
        self.lb_bkpath.setObjectName("Mono")
        self.lb_bkpath.setWordWrap(True)
        v2.addWidget(self.lb_bkpath)
        r2 = QHBoxLayout()
        b_all = btn("全部还原为原名", "Danger", on_click=self.restore_all)
        b_all.setMinimumHeight(38)
        b_all.setMinimumWidth(190)
        r2.addWidget(b_all)
        r2.addStretch(1)
        v2.addLayout(r2)
        root.addWidget(f2, 1)

    # ---------- 生命周期 ----------
    def refresh(self):
        Tasks.run_cached(self, "gpus", S.gpu_adapters, self._got_gpus,
                         on_fail=lambda m: self.load_fail(
                             self.lb_state, "显卡列表", m))

    # ---------- 渲染 ----------
    def _got_gpus(self, lst):
        self._gpus = lst or []
        self._backup_file = getattr(S, "_GPU_BACKUP_FILE", "")
        cur = self._cur_gpu()
        keep = cur["index"] if cur else None
        self.gpu_pick.blockSignals(True)
        self.gpu_pick.clear()
        for a in self._gpus:
            self.gpu_pick.addItem("[%s] %s%s" % (a["index"], a["name"],
                                                 "（已伪装）" if a.get("alias") else ""),
                                  a["index"])
        self.gpu_pick.blockSignals(False)
        if keep is not None:                      # 刷新后保持原选中
            ix = self.gpu_pick.findData(keep)
            if ix >= 0:
                self.gpu_pick.setCurrentIndex(ix)
        elif self._gpus:                          # 首次进入默认选独显（虚拟显示适配器排最后）
            ix = next((i for i, a in enumerate(self._gpus)
                       if is_dgpu(a["name"])), 0)
            self.gpu_pick.setCurrentIndex(ix)
        n_alias = len([a for a in self._gpus if a.get("alias")])
        n_bk = len([a for a in self._gpus if a.get("backup")])
        self.lb_count.setText("共 %d 个显示设备%s"
                              % (len(self._gpus),
                                 "，其中 %d 个已伪装" % n_alias if n_alias else ""))
        self.lb_backup.setText("已备份 %d 个适配器的原名。" % n_bk)
        self.lb_bkpath.setText(self._backup_file or "—")
        self._on_pick()

    def _cur_gpu(self):
        """当前选中的显卡（下拉项的 data 里存适配器序号）。"""
        if not self._gpus:
            return None
        idx = self.gpu_pick.currentData()
        if idx is None:
            return None
        return next((a for a in self._gpus if str(a["index"]) == str(idx)), None)

    def _on_pick(self, *_a):
        a = self._cur_gpu()
        if a is None:
            self.lb_state.setText("未枚举到显卡驱动实例。")
        else:
            self.lb_state.setText("%s%s%s"
                                  % (("厂商 %s · " % a["vendor"]) if a["vendor"] else "",
                                     "已改写过显示名" if a.get("alias") else "未被改写",
                                     " · 已备份原名，可还原" if a.get("backup") else ""))
        self._refresh_preview()

    def _clean_pasted(self, text):
        """把粘进来的 HTML 标记/实体清掉，但**不打���用户正在输入**。"""
        if "<" not in text and "&" not in text:
            return                      # 绝大多数输入直接返回，不打扰
        clean = plain_name(text)
        if clean == text:
            return
        self.name_edit.blockSignals(True)
        self.name_edit.setText(clean)
        self.name_edit.blockSignals(False)
        self._refresh_preview()

    def _on_preset(self, index):
        """下拉选预设 → 填进名称框。落在分组分隔线上时保持原值不动。"""
        d = self.preset.itemData(index)
        if d:
            self.name_edit.setText(plain_name(d))

    def _refresh_preview(self):
        """大号预览：「当前显示名 → 新显示名」。"""
        a = self._cur_gpu()
        if a is None:
            self.lb_preview.setText("未枚举到显卡驱动实例")
            return
        new = plain_name(self.name_edit.text())
        if not new:
            self.lb_preview.setText("%s\n↓  在上方填写或选择新显示名" % a["name"])
        else:
            self.lb_preview.setText("%s\n↓\n<b>%s</b>" % (a["name"], new))

    # ---------- 动作 ----------
    def apply_alias(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        a = self._cur_gpu()
        if a is None:
            return info(self, "请先选择要伪装的显卡。")
        name = plain_name(self.name_edit.text())
        if not name:
            return info(self, "请选择预设或直接填写要显示的名称。")
        if len(name) > 80:
            return info(self, "名字太长了（%d 字），设备管理器会显示不下。" % len(name))
        if not confirm(self, "确认显卡伪装",
                       "将把 [%s] %s 的显示名改为：\n\n%s\n\n"
                       "只改注册表里的显示名，不改变硬件能力；"
                       "部分反作弊/跑分软件可能因此报错。确认执行？"
                       % (a["index"], a["name"], name), danger=True):
            return
        self.win.busy("正在写入显卡显示名…")
        Tasks.run(self, lambda: S.gpu_alias_apply(a["index"], name),
                  lambda r: (self.win.idle(), self._done(r)),
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def restart_gpu(self):
        """「重启显卡」：只重启所选的这一个显示设备（重新加载它的驱动）。

        底层是 `pnputil /restart-device <实例ID>`（Win10 2004+），**不用重启电脑**。
        """
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        a = self._cur_gpu()
        if a is None:
            return info(self, "请先选择要伪装的显卡。")
        if not confirm(self, "重启显卡",
                       "将重启 [%s] %s 这一个显示设备。\n\n"
                       "它会重新加载这块显卡的驱动：屏幕会黑一下 / 闪一两秒，"
                       "属正常现象，不会改动任何设置；"
                       "正在全屏游戏或录屏的话请先退出。\n\n继续？"
                       % (a["index"], a["name"]), danger=True):
            return
        self.win.busy("正在重启显卡驱动…")
        Tasks.run(self, lambda: S.gpu_restart_device(a["index"]),
                  lambda r: (self.win.idle(), self._done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def show_live_names(self):
        """核验用：让系统自己报一遍显卡名（设备管理器 / 任务管理器同源）。"""
        self.win.busy("正在读取系统当前报告的显卡名…")

        def done(lst):
            self.win.idle()
            if not lst:
                return warn(self, "读取失败", "没有读到任何显卡（可能权限不足）。")
            lines = "\n".join("· %s\n    %s" % (x["name"], x["pnp"]) for x in lst)
            want = plain_name(self.name_edit.text())
            if want and not any(want == x["name"] for x in lst):
                lines += ("\n\n目标名称「%s」**没有**出现在系统报告里 → 还没生效。"
                          "先点「重启显卡」再核验。" % want)
            info(self, "系统当前报告的显卡名", lines)

        Tasks.run(self, S.gpu_current_wmi_names, done,
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def restore_alias(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        a = self._cur_gpu()
        if a is None:
            return info(self, "请先选择显卡。")
        self.win.busy("正在还原显卡显示名…")
        Tasks.run(self, lambda: S.gpu_alias_restore(a["index"]),
                  lambda r: (self.win.idle(), self._done(r)),
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def restore_all(self):
        """把列表里所有已备份的适配器一次性还原。"""
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        todo = [a for a in self._gpus if a.get("backup")]
        if not todo:
            return info(self, "没有需要还原的适配器。")
        if not confirm(self, "全部还原",
                       "将把以下 %d 个适配器的显示名还原为原值：\n\n%s"
                       % (len(todo), "\n".join("· [%s] %s" % (a["index"], a["name"])
                                               for a in todo)), danger=True):
            return
        self.win.busy("正在还原 %d 个适配器…" % len(todo))

        def work():
            ok = 0
            for a in todo:
                if (S.gpu_alias_restore(a["index"]) or {}).get("ok"):
                    ok += 1
            return {"ok": ok == len(todo),
                    "msg": "已还原 %d / %d 个适配器。" % (ok, len(todo))}

        Tasks.run(self, work, lambda r: (self.win.idle(), self._done(r)),
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def open_backup_dir(self):
        p = self._backup_file or getattr(S, "_GPU_BACKUP_FILE", "")
        if not S.open_dir(p):
            warn(self, "找不到备份文件所在目录。")

    def _done(self, r):
        r = r or {}
        info(self, "完成" if r.get("ok") else "未完成", r.get("msg") or "")
        self.reload()


# ==========================================================================
# 5. 启动与服务
# ==========================================================================
class UwpPanel(Page):
    """预装应用（UWP / 商店应用）—— 「软件管理」页的第二个页签。

    为什么不做「一键全删预装应用」：那会把应用商店、winget 宿主、
    开始菜单宿主一起卸掉，删完开始菜单和搜索就废了。这里关键包与运行时框架
    **连勾都不给勾**，其余也必须手动点，卸载前列清单确认，卸载后回读复核。
    """
    title = "预装应用"

    def __init__(self, win):
        super().__init__(win)
        self.apps = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(notice(
            "这里列出当前用户已安装的 UWP / 商店 / 系统预置应用。\n"
            "• 卸载只对**当前用户**生效，之后随时能从 Microsoft Store 重新安装（应用数据会清掉）。\n"
            "• 勾上「同时从系统预置中移除」后，之后新建的用户账户不会再自动装上这些应用（需管理员）。\n"
            "• 标【关键】/【框架】的是商店、开始菜单宿主、运行时组件等，删了会出问题，已禁止勾选。", "info"))

        b = QHBoxLayout()
        self.btn_uwp_del = btn("卸载所选", "Danger", on_click=self.remove_sel)
        self.btn_uwp_del.setEnabled(False)
        b.addWidget(self.btn_uwp_del)
        self.btn_uwp_re = btn("重装全部商店应用", on_click=self.reinstall_all)
        self.btn_uwp_re.setToolTip("重新安装已注册的应用：对所有商店/UWP "
                                   "应用重新注册（不重新下载）。用于修复应用打不开、\n"
                                   "开始菜单点不动、图标全丢这类损坏。需要几分钟。")
        b.addWidget(self.btn_uwp_re)
        b.addWidget(btn("全选可卸载", on_click=lambda: self.check_all(True)))
        b.addWidget(btn("全不选", on_click=lambda: self.check_all(False)))
        b.addWidget(btn("刷新", on_click=self.reload))
        b.addStretch(1)
        self.cb_prov = QCheckBox("同时从系统预置中移除")
        self.cb_prov.setToolTip("需要管理员权限。作用于系统镜像：之后新建的用户账户"
                                "不会再自动装上这些应用")
        b.addWidget(self.cb_prov)
        self.uwp_n = pill("读取中…", "")
        b.addWidget(self.uwp_n)
        root.addLayout(b)

        q = QHBoxLayout()
        self.uwp_q = QLineEdit()
        self.uwp_q.setPlaceholderText("按名称 / 包名过滤")
        self.uwp_q.setMaximumWidth(300)
        self.uwp_q.textChanged.connect(self.paint_apps)
        q.addWidget(self.uwp_q)
        q.addStretch(1)
        root.addLayout(q)

        self.tbl = make_table(["", "名称", "包名", "版本", "类型", "发布者", "安装位置"])
        self.tbl.itemChanged.connect(self._on_item_changed)
        attach_row_menu(self.tbl, self._app_menu, standalone=True)
        root.addWidget(self.tbl, 1)

    # ---------- 数据 ----------
    def refresh(self):
        Tasks.run_cached(self, "uwp", S.list_uwp_apps, self._got_apps,
                         on_fail=lambda *_: self.uwp_n.setText("读取失败"))

    def _got_apps(self, rows):
        self.apps = rows or []
        self.paint_apps()

    def paint_apps(self):
        kw = self.uwp_q.text().strip().lower()
        # 过滤会整表重建 —— 先把已勾选项记下来，否则改一下搜索词勾选就全丢了
        keep = {a["full"] for a in self.checked()}
        rows = [a for a in self.apps
                if not kw or kw in (a["title"] + " " + a["name"]).lower()]
        t = self.tbl
        t.blockSignals(True)
        t.setRowCount(0)
        for a in rows:
            i = t.rowCount()
            t.insertRow(i)
            locked = bool(a["protected"] or a["framework"])
            chk = QTableWidgetItem()
            if locked:
                chk.setFlags(Qt.ItemIsEnabled)          # 连勾都不给勾
                chk.setToolTip("关键组件，禁止卸载：删了会导致商店 / 开始菜单 /"
                               "搜索 / 运行时不可用")
            else:
                chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                chk.setCheckState(Qt.Checked if a["full"] in keep else Qt.Unchecked)
                chk.setToolTip("勾选后点上方「卸载所选」")
            chk.setData(Qt.UserRole, a["full"])
            t.setItem(i, 0, chk)
            kind = "框架" if a["framework"] else ("关键" if a["protected"] else "应用")
            cells = (a["title"], a["name"], a["version"], kind,
                     a["publisher"], a["location"] or "—")
            for c, v in enumerate(cells, start=1):
                it = SortItem(str(v))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(v))
                if c == 1 and locked:
                    it.setForeground(QColor(T.get_color("WARN", self.win.dark)))
                t.setItem(i, c, it)
            t.setRowHeight(i, 30)
        t.blockSignals(False)
        # 列宽：前五列收窄，把剩余宽度让给「安装位置」（否则被截成 C:\Program …）
        for c, w in ((0, 28), (1, 175), (2, 225), (3, 80), (4, 46), (5, 110)):
            t.setColumnWidth(c, w)
        self._on_item_changed(None)

    def _on_item_changed(self, _it):
        n = len(self.checked())
        n_lock = sum(1 for a in self.apps if a["protected"] or a["framework"])
        if not self.apps:
            self.uwp_n.setText("未读到应用")
        else:
            self.uwp_n.setText("%d 可卸载 · 已选 %d" % (len(self.apps) - n_lock, n))
        self.btn_uwp_del.setEnabled(n > 0)

    def checked(self):
        """取勾选项（按包全名回查原始数据，不依赖行号）。"""
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is None or it.checkState() != Qt.Checked:
                continue
            full = it.data(Qt.UserRole)
            a = next((x for x in self.apps if x["full"] == full), None)
            if a is not None:
                out.append(a)
        return out

    def check_all(self, on):
        state = Qt.Checked if on else Qt.Unchecked
        self.tbl.blockSignals(True)
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None and (it.flags() & Qt.ItemIsUserCheckable):
                it.setCheckState(state)
        self.tbl.blockSignals(False)
        self._on_item_changed(None)

    # ---------- 卸载 ----------
    def remove_sel(self):
        sel = self.checked()
        if not sel:
            return info(self, "还没有勾选任何应用。")
        prov = self.cb_prov.isChecked()
        if prov and not self.win.admin_mode:
            return self.win._warn_readonly()
        warn_text = ("同时会从**系统预置镜像**里移除它们 —— 之后新建的用户账户"
                     "不会再自动装上这些应用。" if prov else
                     "卸载后可以从 Microsoft Store 重新安装，但应用里已保存的数据会一并清掉。")
        if not safety_confirm(
                self, self.win.dark, "卸载预装应用",
                "即将卸载以下 %d 个应用（只对当前用户生效）：" % len(sel),
                ["%s　—　%s" % (a["title"], a["name"]) for a in sel],
                risky=0, warn_text=warn_text):
            return
        self._do_remove(sel, prov)

    def _do_remove(self, sel, prov):
        self.win.busy("正在卸载 %d 个应用…" % len(sel))
        Tasks.run(self, lambda: S.remove_uwp_apps(sel, provisioned=prov),
                  lambda r: (self.win.idle(), self._remove_done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "卸载失败", m))[1])

    def reinstall_all(self):
        """重新安装已注册的应用：全部商店应用重新注册（修复用）。"""
        if not confirm(self, "重装全部商店应用",
                       "将对所有商店 / UWP 应用重新注册（不重新下载，只是重建注册）。\n\n"
                       "用于修复：应用打不开、开始菜单点不动、应用图标全丢这类损坏。\n"
                       "需要几分钟，期间本页面会显示进行中；框架包/受保护包失败属正常。"):
            return
        self.btn_uwp_re.setEnabled(False)
        self.uwp_n.setText("正在重装…（可能需要几分钟）")
        self.win.busy("正在重装商店应用…")
        Tasks.run(self, S.reinstall_store_apps, self._reinstalled,
                  on_fail=lambda m: (self.win.idle(), self.btn_uwp_re.setEnabled(True),
                                     warn(self, "重装失败", m))[1])

    def _reinstalled(self, r):
        r = r or {}
        self.win.idle()
        self.btn_uwp_re.setEnabled(True)
        self.sess_invalidate("uwp")
        self.refresh()
        info(self, r.get("msg") or "完成")

    def _remove_done(self, r):
        r = r or {}
        self.sess_invalidate("uwp")
        self.refresh()                      # 先刷新，再弹提示（模态会阻塞事件循环）
        lines = [r.get("msg") or ""]
        for f in (r.get("failed") or [])[:6]:
            lines.append("· %s：%s" % (f.get("package", "?"), (f.get("err") or "")[:80]))
        for f in (r.get("prov_failed") or [])[:3]:
            lines.append("· 预置移除失败：%s（%s）"
                         % (f.get("package", "?"), (f.get("err") or "")[:60]))
        body = "\n".join(x for x in lines if x)
        if r.get("ok"):
            info(self, "卸载完成", body)
        else:
            warn(self, "卸载未全部完成", body)

    # ---------- 右键 ----------
    def _app_menu(self, menu, table, row):
        it = table.item(row, 0)
        full = it.data(Qt.UserRole) if it is not None else None
        a = next((x for x in self.apps if x["full"] == full), None)
        if a is None:
            return
        locked = bool(a["protected"] or a["framework"])
        act = menu.addAction("卸载这个应用")
        act.setEnabled(not locked)
        if locked:
            act.setToolTip("关键组件 / 运行时框架，不能卸载")
        act.triggered.connect(lambda: self._remove_one(a))
        menu.addSeparator()
        a_loc = menu.addAction("打开安装位置")
        a_loc.setEnabled(bool(a["location"]) and os.path.isdir(a["location"]))
        a_loc.triggered.connect(lambda: self.open_location(a["location"]))
        a_cp = menu.addAction("复制包名")
        a_cp.triggered.connect(lambda: copy_text(a["name"]))
        a_cpf = menu.addAction("复制完整包名")
        a_cpf.triggered.connect(lambda: copy_text(a["full"]))
        a_web = menu.addAction("在线搜索")
        a_web.triggered.connect(lambda: S.open_url(
            "https://www.bing.com/search?q=" + quote(str(a["name"]))))

    def _remove_one(self, a):
        if a["protected"] or a["framework"]:
            return info(self, "这个是关键组件，不能卸载",
                        "「%s」属于系统关键包或运行时框架，删掉会导致商店 / 开始菜单 /"
                        "搜索等功能不可用。" % a["title"])
        if not safety_confirm(
                self, self.win.dark, "卸载预装应用",
                "即将卸载：%s（只对当前用户生效）" % a["title"],
                ["%s　—　%s" % (a["title"], a["name"])],
                warn_text="卸载后可以从 Microsoft Store 重新安装，"
                          "但应用里已保存的数据会一并清掉。"):
            return
        self._do_remove([a], False)

    def open_location(self, path):
        if not path or not os.path.isdir(path):
            return info(self, "找不到安装位置", path or "（该应用没有报告安装路径）")
        if not reveal_in_explorer(path):
            warn(self, "无法在资源管理器中定位：\n%s" % path)


class InstallMonitorPanel(Page):
    """安装监视 —— 「软件管理」页的第三个页签。

    用法：装软件**前**点「开始监视」→ 正常安装 → 装完点「停止并查看」。
    它只列出**这次安装动了什么**（新增/改动的文件与注册表项），新增的能一键清掉。
    这是「事前记录」，比事后靠名字猜残留准得多。
    """
    title = "安装监视"

    def __init__(self, win):
        super().__init__(win)
        self.res = None
        self._rows = []                  # 与表格行一一对应（行内存下标，防排序错位）
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(notice(
            "「事前记录」：装软件<b>前</b>点「开始监视」，装完点「停止并查看」，"
            "它会列出这次安装<b>新增和改动了什么</b>（文件 + 注册表），新增的可以一键清掉。\n"
            "• 监视期间<b>别的程序也在写文件</b>（日志、输入法词库、显卡驱动等），"
            "这类标成灰色的「仅供参考」，默认不勾、也不建议删。\n"
            "• 默认只盯安装常碰的位置（Program Files / ProgramData / "
            "%LOCALAPPDATA%\\Programs / 桌面 / 开始菜单）；\n"
            "  装绿色软件或想把配置也一起记录，勾上「含用户数据目录」把整个 "
            "%LOCALAPPDATA% 与 %APPDATA% 纳入。", "info"))

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_start = btn("开始监视", "Primary", on_click=self.start)
        bar.addWidget(self.btn_start)
        self.btn_stop = btn("停止并查看", "Danger", on_click=self.stop)
        bar.addWidget(self.btn_stop)
        self.btn_cancel = btn("放弃", on_click=self.cancel)
        bar.addWidget(self.btn_cancel)
        self.cb_ud = QCheckBox("含用户数据目录")
        self.cb_ud.setToolTip(
            "额外监视 %LOCALAPPDATA% 与 %APPDATA%（本机各 12 万 / 3.7 万个文件，"
            "多花约 7 / 3 秒）。\n装绿色软件、或想连配置一起记录时才需要。")
        bar.addWidget(self.cb_ud)
        bar.addStretch(1)
        self.pill_st = pill("未监视", "")
        bar.addWidget(self.pill_st)
        root.addLayout(bar)

        f, v = card("这次安装动了什么")
        self.head = QLabel("先点「开始监视」，再去安装软件。")
        self.head.setObjectName("Muted")
        self.head.setWordWrap(True)
        v.addWidget(self.head)

        self.table = make_table(["", "类型", "位置", "大小"])
        self.table.itemChanged.connect(lambda *_: self._sum())
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_clean = btn("清理新增项", "Danger", on_click=self.clean)
        self.btn_clean.setEnabled(False)
        row.addWidget(self.btn_clean)
        self.btn_all = select_all_button(self.table, on_changed=self._sum)
        row.addWidget(self.btn_all)
        row.addStretch(1)
        self.sum_lb = QLabel("")
        self.sum_lb.setObjectName("Muted")
        row.addWidget(self.sum_lb)
        v.addLayout(row)
        root.addWidget(f, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ---------- Page 接口 ----------
    def refresh(self):
        st = S.install_monitor_status()
        self._paint(st)
        if st.get("active"):
            self._timer.start()
        else:
            self._timer.stop()

    def first_show(self):
        """首次点开页签才读状态（避免拖慢整个软件管理页）。"""
        if not getattr(self, "loaded", False):
            self.loaded = True
            self.refresh()

    def _paint(self, st):
        act = bool((st or {}).get("active"))
        if act:
            el = int(st.get("elapsed") or 0)
            self.pill_st.setText("监视中 %d:%02d · 基线 %s 个文件"
                                 % (el // 60, el % 60, st.get("files", 0)))
            self.pill_st.setObjectName("PillOk")
        else:
            self.pill_st.setText("未监视")
            self.pill_st.setObjectName("Pill")
        self.pill_st.style().unpolish(self.pill_st)
        self.pill_st.style().polish(self.pill_st)
        self.btn_start.setEnabled(not act)
        self.btn_stop.setEnabled(act)
        self.btn_cancel.setEnabled(act)
        self.cb_ud.setEnabled(not act)

    def _tick(self):
        st = S.install_monitor_status()
        if not st.get("active"):
            self._timer.stop()
        self._paint(st)

    # ---------- 操作 ----------
    def start(self):
        # 监视是只读的，普通模式也能用；只有「清理」需要管理员
        self.win.busy("正在记录当前状态（约 2 秒）…")
        ud = self.cb_ud.isChecked()
        Tasks.run(self, lambda: S.install_monitor_start(ud), self._started,
                  on_fail=lambda m: (self.win.idle(),
                                     warn(self, "开始监视失败", m))[1])

    def _started(self, r):
        self.win.idle()
        r = r or {}
        if not r.get("ok"):
            return warn(self, "开始监视失败", r.get("msg") or "未知原因")
        self.res = None
        self._rows = []
        self.table.setRowCount(0)
        self.sum_lb.setText("")
        self.btn_clean.setEnabled(False)
        self.head.setText("已记录基线：%s 个文件 / %s 个目录（用时 %s 秒）。\n"
                          "现在去装你的软件 —— 装完回来点「停止并查看」。"
                          % (r.get("files"), r.get("dirs"), r.get("secs")))
        self.refresh()

    def stop(self):
        self.win.busy("正在对比（约 3~5 秒）…")
        Tasks.run(self, S.install_monitor_stop, self._stopped,
                  on_fail=lambda m: (self.win.idle(),
                                     warn(self, "对比失败", m))[1])

    def _stopped(self, r):
        self.win.idle()
        r = r or {}
        if not r.get("ok"):
            return warn(self, "停止失败", r.get("msg") or "未知原因")
        self.res = r
        self._fill()
        self.refresh()

    def cancel(self):
        if not confirm(self, "放弃监视",
                       "放弃本次监视？已记录的基线会被丢掉，之后无法对比。",
                       danger=False):
            return
        S.install_monitor_cancel()
        self._timer.stop()
        self.head.setText("已放弃。想重新开始就再点「开始监视」。")
        self.refresh()

    def _fill(self):
        r = self.res or {}
        rows = []
        for f in (r.get("files") or []):
            rows.append({"kind": f.get("kind") or "file", "tag": f.get("tag") or "",
                         "path": f.get("path") or "", "hive": "", "sub": "",
                         "size": int(f.get("bytes") or 0),
                         "deletable": bool(f.get("deletable")), "why": ""})
        for k in (r.get("reg") or []):
            rows.append({"kind": "reg", "tag": k.get("tag") or "",
                         "path": "%s\\%s" % (k.get("hive"), k.get("path")),
                         "hive": k.get("hive"), "sub": k.get("path") or "",
                         "size": 0, "deletable": bool(k.get("deletable")),
                         "why": k.get("why") or ""})
        rows.sort(key=lambda x: 0 if x["deletable"] else 1)   # 可清理的排前面
        self._rows = rows

        dim = QColor(T.get_color("TEXT3", self.win.dark))
        t = self.table
        t.blockSignals(True)
        t.setRowCount(0)
        for i, row in enumerate(rows):
            t.insertRow(i)
            cb = QTableWidgetItem()
            if row["deletable"]:
                cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                cb.setCheckState(Qt.Checked)
                cb.setToolTip("这次安装**新增**的，可以清掉：\n"
                              "文件移入回收站（可还原），注册表先导出 .reg 备份。")
            else:
                # 只报告、不给勾：它覆盖了原有内容，删掉只会弄坏原来的程序
                cb.setFlags(Qt.NoItemFlags)
                cb.setToolTip(row["why"] or "仅供参考，不可清理")
            cb.setData(Qt.UserRole, i)          # 排序后行号会变，取数据认这个
            t.setItem(i, 0, cb)
            for col, val in ((1, row["tag"]), (2, row["path"]),
                             (3, fmt_bytes(row["size"]) if row["size"] else "")):
                it = QTableWidgetItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                if not row["deletable"]:
                    it.setForeground(QBrush(dim))
                t.setItem(i, col, it)
            t.setRowHeight(i, 28)
        t.blockSignals(False)
        t.setColumnWidth(0, 34)
        t.setColumnWidth(1, 88)
        t.setColumnWidth(2, 620)
        t.setColumnWidth(3, 80)

        self.head.setText(
            "监视了 %d 秒（对比用时 %s 秒）。\n"
            "· 新增 %d 个文件 / %d 个目录，新建 %d 个注册表根 —— 这些可以清掉\n"
            "· 另有 %d 个原有文件被覆盖、%d 个注册表键被写入 —— 只报告，不能撤销\n"
            "· 期间被删掉 %d 个文件（无法还原，只记录）"
            % (r.get("secs", 0), r.get("scan_secs", 0),
               r.get("n_created", 0), r.get("n_new_dirs", 0), r.get("n_new_top", 0),
               r.get("n_modified", 0), r.get("n_reg_hit", 0), r.get("n_deleted", 0)))
        if r.get("truncated"):
            self.head.setText(self.head.text() +
                              "\n（列表太长，只显示前 %d 条）" % len(rows))
        if not rows:
            self.head.setText(self.head.text() + "\n\n没有检测到任何改动。")
        self._sum()

    def _sum(self):
        n = sum(1 for r in range(self.table.rowCount())
                if (self.table.item(r, 0) is not None
                    and self.table.item(r, 0).checkState() == Qt.Checked))
        self.btn_clean.setEnabled(bool(self.win.admin_mode) and n > 0)
        self.sum_lb.setText("已选 %d 项" % n if n else "")
        if getattr(self, "btn_all", None) is not None:
            self.btn_all._refresh()

    def clean(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        files, regs = [], []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is None or it.checkState() != Qt.Checked:
                continue
            row = self._rows[it.data(Qt.UserRole)]
            if not row.get("deletable"):
                continue                     # 只报告的项一律不动
            if row["kind"] == "reg":
                regs.append({"hive": row["hive"], "path": row["sub"]})
            else:
                files.append({"path": row["path"], "kind": row["kind"]})
        if not files and not regs:
            return info(self, "没有勾选任何可清理的项。")
        if not confirm(self, "清理新增项",
                       "将清理这次安装新增的 %d 个文件/目录、%d 个注册表键。\n\n"
                       "文件移入回收站（可还原），注册表删除前导出 .reg 备份到 "
                       "backup\\uninstall。\n\n⚠️ 请确认这些确实是这次安装带来的 —— "
                       "勾错的项会连带影响原有程序。"
                       % (len(files), len(regs)), danger=True):
            return
        self.win.busy("正在清理…")

        def done(res):
            self.win.idle()
            res = res or {}
            self.win.notify_changed("overview", "startup")
            if res.get("ok"):
                info(self, "清理完成。")
            else:
                warn(self, "部分未清理成功",
                     "\n".join((res.get("msg") or [])[:8]) or "见备份目录")
        Tasks.run(self, lambda: S.residual_clean(files, regs), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "清理失败", m))[1])


class StartupPage(Page):
    title = "启动与服务"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        self.startup = []
        self.services = []
        self.tasks = []

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---------------- 开机启动项 ----------------
        w1 = QWidget(); l1 = QVBoxLayout(w1)
        l1.setContentsMargins(14, 14, 14, 14); l1.setSpacing(10)
        l1.addWidget(notice(
            "「禁用」只关掉自启、不删条目：被禁用的项会留在列表里（状态显示「已禁用」），"
            "随时可以再启用 —— 与 Windows 任务管理器同一套机制。"
            "右键任意一项可打开文件所在位置、定位到对应服务或注册表项。", "info"))
        b1 = QHBoxLayout()
        self.btn_su_on = btn("启用所选", on_click=lambda: self.startup_switch(True))
        self.btn_su_off = btn("禁用所选", on_click=lambda: self.startup_switch(False))
        self.btn_su_rm = btn("移除所选", "Danger", on_click=self.remove_startup)
        for _b in (self.btn_su_on, self.btn_su_off, self.btn_su_rm):
            _b.setEnabled(self.win.admin_mode)
            _b.setToolTip("禁用只关闭自启，条目保留在列表里；移除才是彻底删除注册表项")
            b1.addWidget(_b)
        b1.addWidget(btn("刷新", on_click=self.reload))
        b1.addStretch(1)
        b1.addWidget(QLabel("显示："))
        self.su_filter = QComboBox()
        self.su_filter.addItems(["全部", "仅已启用", "仅已禁用"])
        self.su_filter.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.su_filter.setMaximumWidth(130)
        self.su_filter.currentIndexChanged.connect(lambda *_: self.paint_startup())
        b1.addWidget(self.su_filter)
        self.s_n = pill("0 项", "")
        b1.addWidget(self.s_n)
        l1.addLayout(b1)
        self.t1 = make_table(["名称", "发布者", "状态", "位置", "命令"], sortable=True)
        attach_reveal(self.t1, self._reveal_startup)
        attach_row_menu(self.t1, self._startup_menu, standalone=True)
        l1.addWidget(self.t1, 1)
        self.tabs.addTab(w1, "开机启动项")

        # ---------------- 系统服务 ----------------
        w2 = QWidget(); l2 = QVBoxLayout(w2)
        l2.setContentsMargins(14, 14, 14, 14); l2.setSpacing(10)
        b2 = QHBoxLayout()
        self.sq = QLineEdit(); self.sq.setPlaceholderText("按名称过滤"); self.sq.setMaximumWidth(240)
        self.sq.textChanged.connect(self.paint_services)
        b2.addWidget(self.sq)
        self.sv_n = pill("0", "")
        b2.addWidget(self.sv_n)
        b2.addStretch(1)
        self.svc_btns = []
        for label, act in [("启动", "start"), ("停止", "stop"), ("禁用", "disable"),
                           ("改自动", "auto"), ("改手动", "manual")]:
            b = btn(label, on_click=lambda _=False, a=act: self.svc(a))
            b.setEnabled(self.win.admin_mode)
            self.svc_btns.append(b)
            b2.addWidget(b)
        l2.addLayout(b2)
        self.t2 = make_table(["服务名", "显示名", "状态", "启动类型"], sortable=True)
        attach_reveal(self.t2, self._reveal_service)
        attach_row_menu(self.t2, self._service_menu, standalone=True)
        l2.addWidget(self.t2, 1)
        self.tabs.addTab(w2, "系统服务")

        # ---------------- 计划任务 ----------------
        w3 = QWidget(); l3 = QVBoxLayout(w3)
        l3.setContentsMargins(14, 14, 14, 14); l3.setSpacing(10)
        b3 = QHBoxLayout()
        self.tq = QLineEdit(); self.tq.setPlaceholderText("按任务名 / 路径过滤")
        self.tq.setMaximumWidth(240)
        self.tq.textChanged.connect(self.paint_tasks)
        b3.addWidget(self.tq)
        self.tk_n = pill("0", "")
        b3.addWidget(self.tk_n)
        b3.addStretch(1)
        self.task_btns = []
        for label, act in (("启用", "enable"), ("禁用", "disable")):
            b = btn(label, on_click=lambda _=False, a=act: self.task(a))
            b.setEnabled(self.win.admin_mode)
            self.task_btns.append(b)
            b3.addWidget(b)
        l3.addLayout(b3)
        self.t3 = make_table(["任务名", "路径", "状态"], sortable=True)
        attach_reveal(self.t3, self._reveal_task)
        l3.addWidget(self.t3, 1)
        self.tabs.addTab(w3, "计划任务")

    def refresh(self):
        Tasks.run_cached(self, "su", S.list_startup, self._got_startup)
        Tasks.run_cached(self, "svc", S.list_services, self._got_services)
        Tasks.run_cached(self, "task", S.list_tasks, self._got_tasks)

    # ==================== 通用：行 → 数据 ====================
    def _row_item(self, table, row):
        """按填入时存的 UserRole 取回原始数据（排序后行号不可信）。"""
        it = table.item(row, 0)
        idx = it.data(Qt.UserRole) if it is not None else None
        if isinstance(idx, int) and 0 <= idx < len(self.startup):
            return self.startup[idx]
        return None

    def _startup_path(self, x):
        """该项对应的可执行文件（定位 / 属性 / 图标都用它）。"""
        exe = (x.get("exe") or "").strip()
        if exe and os.path.exists(exe):
            return exe
        cmd = str(x.get("cmd") or "").strip().strip('"')
        return cmd if os.path.exists(cmd) else (exe or cmd)

    def _reveal_startup(self, t, r):
        x = self._row_item(t, r)
        return self._startup_path(x) if x else ""

    # ==================== 打开 / 定位 ====================
    def open_location(self, path):
        if not path or not os.path.exists(path):
            return info(self, "找不到该文件", "可能已被删除或卸载：\n%s" % path)
        if not reveal_in_explorer(path):
            warn(self, "无法在资源管理器中定位：\n%s" % path)

    def locate_service(self, name):
        """打开服务管理器并把服务名复制到剪贴板。

        services.msc 不支持按名字直接跳转（微软没提供深链），所以这里把名字
        放进剪贴板 —— 在列表里 Ctrl+V 即可跳到目标。
        """
        if not name:
            return info(self, "没找到与之对应的服务",
                        "这个启动项不是由服务拉起的，可以改用「打开文件所在位置」"
                        "或「定位到注册表项」。")
        copy_text(name)
        if not open_with_default("services.msc"):
            return warn(self, "打开服务管理器失败",
                        "服务名已复制到剪贴板：\n%s" % name)
        info(self, "已打开服务管理器",
             "服务名「%s」已复制到剪贴板。\n\n"
             "services.msc 不支持直接跳转，粘贴（Ctrl+V）到列表里按首字母即可定位。" % name)

    def open_service_detail(self, name):
        """打开自绘的服务详情弹窗。

        `services.msc` 不支持跳到指定服务，所以「定位到服务」改成先看这个弹窗 ——
        里面直接有显示名、状态、启动类型、依赖关系和描述，想改再从这里改。
        """
        if not name:
            return info(self, "没找到与之对应的服务",
                        "这个启动项不是由服务拉起的，可以改用「打开文件所在位置」"
                        "或「定位到注册表项」。")
        dlg = ServiceDialog(self.win, name, on_action=self.svc)
        dlg.exec()
        dlg.deleteLater()      # 不把隐藏的模态框留在控件树里（重抛光会扫到）

    def locate_registry(self, x):
        if x.get("folder"):
            return info(self, "启动文件夹项没有注册表位置", "它位于：\n%s" % x.get("path"))
        key = "%s\\%s" % (x["hive"], x["path"])
        copy_text("%s   →   %s" % (key, x["name"]))
        S.set_regedit_lastkey(x["hive"], x["path"])
        if not open_with_default("regedit.exe"):
            return warn(self, "打开注册表编辑器失败", "位置已复制到剪贴板：\n%s" % key)
        info(self, "已打开注册表编辑器",
             "位置「%s」已写入 regedit 的上次访问位置，并已复制到剪贴板。\n"
             "如果 regedit 已经开着没有跳过去，关掉再打开一次即可。" % key)

    def open_props(self, path):
        if not show_file_properties(path):
            warn(self, "无法打开属性对话框", path or "（没有可用的文件路径）")

    # ==================== 开机启动项 ====================
    def _got_startup(self, lst):
        self.startup = lst or []
        self.paint_startup()

    def paint_startup(self):
        mode = self.su_filter.currentIndex()          # 0 全部 / 1 仅已启用 / 2 仅已禁用
        rows, keys, icons = [], [], []
        for i, x in enumerate(self.startup):
            if (mode == 1 and not x.get("enabled")) or (mode == 2 and x.get("enabled")):
                continue
            loc = ("启动文件夹" if x.get("folder") else ("%s\\%s" % (x["hive"], x["path"])))
            rows.append([x.get("name") or "", x.get("publisher") or "—",
                         "已启用" if x.get("enabled") else "已禁用",
                         loc, x.get("cmd") or ""])
            keys.append(i)
            icons.append(file_icon(self._startup_path(x)))
        fill_table(self.t1, rows, icons=icons, keys=keys)
        n_off = sum(1 for x in self.startup if not x.get("enabled"))
        self.s_n.setText("%d 项 · 禁用 %d" % (len(self.startup), n_off))
        # 列宽：前四列收窄，把剩下的宽度尽量留给「命令」（它最长且最常被看）
        for c, w in ((0, 218), (1, 148), (2, 66), (3, 186)):
            self.t1.setColumnWidth(c, w)

    def startup_switch(self, on):
        """启用 / 禁用所选启动项（写 StartupApproved 标志，不删条目）。"""
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        idx = picked_key(self.t1)
        if not isinstance(idx, int) or not (0 <= idx < len(self.startup)):
            return info(self, "请先选择一个启动项。")
        x = self.startup[idx]
        if not x.get("managed"):
            return info(self, "该项不支持启用/禁用",
                        "「%s」位于 RunOnce（只运行一次）键下，Windows 不维护它的启停状态。"
                        "不需要的话请用「移除所选」。" % x["name"])
        if bool(x.get("enabled")) == bool(on):
            return info(self, "该项已经是「%s」状态。" % ("启用" if on else "禁用"))
        act = "启用" if on else "禁用"
        tip = ("它会恢复随系统启动。" if on else
               "它仍会留在列表里（显示「已禁用」），随时可以再启用，不会删除任何东西。")
        if not confirm(self, "%s启动项" % act, "确定%s「%s」？\n\n%s" % (act, x["name"], tip)):
            return
        self.win.busy("正在%s…" % act)
        Tasks.run(self, lambda: S.set_startup_enabled(
                      x["hive"], x["path"], x["name"], x.get("folder", False), on),
                  lambda r: (self.win.idle(), self._after_switch(r, act))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "%s失败" % act, m))[1])

    def _after_switch(self, r, act):
        r = r or {}
        self.reload()                 # 先启动刷新，再弹提示（模态框会阻塞事件循环）
        if r.get("ok"):
            info(self, "已%s" % act, r.get("msg") or "")
        else:
            warn(self, "%s失败" % act, r.get("msg") or "操作未完成。")

    def remove_startup(self):
        """彻底删除自启动条目（仅注册表项；文件夹项请手动处理）。"""
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        idx = picked_key(self.t1)
        if not isinstance(idx, int) or not (0 <= idx < len(self.startup)):
            return info(self, "请先选择一个启动项。")
        x = self.startup[idx]
        if x.get("folder"):
            return info(self, "启动文件夹中的项请手动处理",
                        "它位于：\n%s\n\n只想关掉自启的话，用右键「禁用」更安全。" % x.get("path"))
        if not confirm(self, "移除启动项",
                       "删除 %s 的自启动注册表项？\n\n"
                       "删除后该项不再出现在列表里。若只是想关掉自启，"
                       "请改用「禁用所选」（条目会保留）。" % x["name"], danger=True):
            return
        Tasks.run(self, lambda: S.reg_delete(x["hive"] + "\\" + x["path"], x["name"]),
                  lambda r: (self.reload(), info(self, "已移除", x["name"]))[0])

    def _startup_menu(self, menu, table, row):
        """启动项右键菜单（对齐任务管理器）：启用/禁用 · 打开位置 · 定位服务/注册表 · 搜索 · 属性。"""
        x = self._row_item(table, row)
        if x is None:
            return
        path = self._startup_path(x)
        has_path = bool(path and os.path.exists(path))

        act = menu.addAction("禁用" if x.get("enabled") else "启用")
        act.setEnabled(self.win.admin_mode and bool(x.get("managed")))
        act.triggered.connect(lambda: self.startup_switch(not x.get("enabled")))
        menu.addSeparator()

        # 能定位文件夹就定位文件夹；定位不了还有「服务 / 注册表」两条退路
        a_loc = menu.addAction("打开文件所在位置")
        a_loc.setEnabled(has_path)
        a_loc.triggered.connect(lambda: self.open_location(path))
        a_svc = menu.addAction("服务详情…")
        a_svc.setEnabled(bool(x.get("service")))
        a_svc.triggered.connect(lambda: self.open_service_detail(x.get("service")))
        a_msc = menu.addAction("在服务管理器中打开")
        a_msc.setEnabled(bool(x.get("service")))
        a_msc.triggered.connect(lambda: self.locate_service(x.get("service")))
        a_reg = menu.addAction("定位到注册表项")
        a_reg.setEnabled(not x.get("folder"))
        a_reg.triggered.connect(lambda: self.locate_registry(x))
        menu.addSeparator()

        a_web = menu.addAction("在线搜索")
        a_web.triggered.connect(lambda: S.open_url(
            "https://www.bing.com/search?q=" + quote(str(x.get("name") or ""))))
        a_cp = menu.addAction("复制命令")
        a_cp.setEnabled(bool(x.get("cmd")))
        a_cp.triggered.connect(lambda: copy_text(str(x.get("cmd") or "")))
        a_prop = menu.addAction("属性")
        a_prop.setEnabled(has_path)
        a_prop.triggered.connect(lambda: self.open_props(path))

    # ==================== 系统服务 ====================
    def _got_services(self, lst):
        self.services = lst
        self.paint_services()

    def paint_services(self):
        kw = self.sq.text().strip().lower()
        lst = [s for s in getattr(self, "services", [])
               if not kw or kw in (s["name"] + s["display"]).lower()]
        self.sv_n.setText(str(len(lst)))
        fill_table(self.t2, [[s["name"], s["display"],
                              "运行中" if s["status"] == "Running" else "已停止", s["start"]]
                             for s in lst],
                   icons=[file_icon(exe_from_cmd(s.get("exe"))) for s in lst],
                   keys=[s["name"] for s in lst])   # 排序后靠服务名找回
        self.t2.setColumnWidth(0, 260)
        self.t2.setColumnWidth(2, 90)
        self.t2.setColumnWidth(3, 90)
        self._svc_rows = lst

    def _svc_by_name(self, name):
        for s in getattr(self, "services", []):
            if s.get("name") == name:
                return s
        return None

    def _service_menu(self, menu, table, row):
        """服务右键菜单：启停 / 改启动类型 + 在服务中打开 / 定位文件 / 复制服务名。"""
        it = table.item(row, 0)
        name = it.data(Qt.UserRole) if it is not None else None
        s = self._svc_by_name(name)
        if s is None:
            return
        admin = self.win.admin_mode
        a_det = menu.addAction("服务详情…")
        a_det.triggered.connect(lambda: self.open_service_detail(name))
        menu.addSeparator()
        for label, act in (("启动", "start"), ("停止", "stop"),
                           ("改为自动启动", "auto"), ("改为手动启动", "manual"),
                           ("禁用服务", "disable")):
            a = menu.addAction(label)
            a.setEnabled(admin)
            a.triggered.connect(lambda _=False, x=act: self.svc(x, name=name))
        menu.addSeparator()
        a_open = menu.addAction("在服务中打开（services.msc）")
        a_open.triggered.connect(lambda: self.locate_service(name))
        exe = exe_from_cmd(s.get("exe") or "")
        a_loc = menu.addAction("打开文件所在位置")
        a_loc.setEnabled(bool(exe and os.path.exists(exe)))
        a_loc.triggered.connect(lambda: self.open_location(exe))
        a_cp = menu.addAction("复制服务名")
        a_cp.triggered.connect(lambda: copy_text(name or ""))

    def _reveal_service(self, t, r):
        it = t.item(r, 0)
        name = it.data(Qt.UserRole) if it is not None else None
        if not name:
            return ""
        s = self._svc_by_name(name)
        return (s.get("exe") or "") if s else ""

    def svc(self, action, name=None):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        name = name or picked_key(self.t2)   # 排序后行号会变，服务名存在单元格里
        if not name:
            return info(self, "请先选择一个服务。")
        if action == "disable" and not confirm(self, "禁用服务",
                                               "确定禁用 %s？可能影响依赖它的功能。" % name,
                                               danger=True):
            return

        def done(res):
            ok, note = res
            self.reload()
            if not ok:
                warn(self, note or "操作失败")
        self.win.busy("正在操作服务…")
        Tasks.run(self, lambda: S.set_service(name, action),
                  lambda res: (self.win.idle(), done(res))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    # ==================== 计划任务 ====================
    def _got_tasks(self, lst):
        self.tasks = lst or []
        self.paint_tasks()

    def paint_tasks(self):
        kw = self.tq.text().strip().lower()
        lst = [t for t in getattr(self, "tasks", [])
               if not kw or kw in (t["name"] + " " + t["path"]).lower()]
        self.tk_n.setText(str(len(lst)))

        def _full(t):
            return (t["path"].rstrip("\\") + "\\" + t["name"]) if t["path"] != "\\" \
                else ("\\" + t["name"])

        fill_table(self.t3, [[t["name"], t["path"], t["state"]] for t in lst],
                   icons=[file_icon(exe_from_cmd(t.get("exe"))) for t in lst],
                   keys=[_full(t) for t in lst])   # 排序后靠全路径找回
        self.t3.setColumnWidth(0, 300)
        self.t3.setColumnWidth(1, 330)
        self.t3.setColumnWidth(2, 90)
        self._task_rows = lst

    def _reveal_task(self, t, r):
        it = t.item(r, 0)
        full = it.data(Qt.UserRole) if it is not None else None
        if not full:
            return ""
        for x in getattr(self, "tasks", []):
            cur = ((x["path"].rstrip("\\") + "\\" + x["name"])
                   if x["path"] != "\\" else ("\\" + x["name"]))
            if cur == full:
                return x.get("exe") or ""
        return ""

    def task(self, action):
        """启用 / 禁用计划任务。"""
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        full = picked_key(self.t3)          # 排序后行号会变，全路径存在单元格里
        if not full:
            return info(self, "请先选择一个计划任务。")
        if action == "disable" and not confirm(
                self, "禁用计划任务",
                "确定禁用？\n\n%s\n\n依赖它的功能（系统更新、驱动检查、备份等）"
                "将不再自动运行；随时可在本页重新启用。" % full, danger=True):
            return
        self.win.busy("正在操作计划任务…")
        def _on_task(res):
            self.win.idle()
            self.reload()
            # `set_task` 内部已做**回读复核**，返回的 msg 是实测结果而不是
            # 「命令返回成功」—— 直接转述它，别自己编一句「已提交」。
            ok, msg = res[0], (res[1] or "")
            if ok:
                info(self, msg or "操作完成")
            else:
                warn(self, msg or "操作失败")
        Tasks.run(self, lambda: S.set_task(full, action == "enable"), _on_task,
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])


# ==========================================================================
# 6. 内存与性能
# ==========================================================================
class PerfPage(Page):
    title = "内存与性能"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        top = QHBoxLayout(); top.setSpacing(12)
        defs = [("CPU", "cpu"), ("内存", "mem"), ("GPU", "gpu"),
                ("磁盘", "disk"), ("网络", "net")]
        self.bars = {}
        for label, key in defs:
            f, v = card()
            k = QLabel(label + "占用" if key != "net" else "网络速率"); k.setObjectName("StatK")
            val = QLabel("—"); val.setObjectName("StatV")
            bar = QProgressBar(); bar.setRange(0, 100); bar.setTextVisible(False)
            bar.setObjectName({"cpu": "Cpu", "mem": "Mem", "gpu": "Gpu",
                               "disk": "Disk", "net": "Net"}[key])
            sub = QLabel(""); sub.setObjectName("Mono"); sub.setWordWrap(True)
            v.addWidget(k); v.addWidget(val); v.addWidget(bar); v.addWidget(sub)
            top.addWidget(f)
            self.bars[key] = (val, sub, bar)
        root.addLayout(top)

        f4, v4 = card("占用最高的进程")
        b = QHBoxLayout()
        b.addStretch(1)
        b.addWidget(btn("整理所选进程", on_click=self.trim_one))
        # 进程表已每秒自动刷新（见 _auto_procs），手动刷新按钮换成「一键整理」
        b.addWidget(btn("一键整理", "Primary", on_click=self.trim_all))
        v4.addLayout(b)
        self.table = make_table(["进程", "PID", "内存占用", "CPU", "GPU"], sortable=True)
        attach_reveal(self.table,
                      lambda t, r: t.item(r, 0).data(Qt.UserRole) or "")
        v4.addWidget(self.table, 1)
        root.addWidget(f4, 1)

        # 全部 1s 实时：CPU/内存纯 ctypes，其余走服务端 PDH（单次 <1ms）
        self.timer = QTimer(self); self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self._ptimer = QTimer(self); self._ptimer.setInterval(1000)
        self._ptimer.timeout.connect(self.tick_perf)
        self._ptimer.start()
        self._ntimer = QTimer(self); self._ntimer.setInterval(1000)
        self._ntimer.timeout.connect(self.tick_net)
        self._ntimer.start()
        # 进程表每秒自动刷新：procs_fast 是纯 ctypes 快照（<10ms），
        # 不再像 list_processes 那样每次起一个 PowerShell（~1.4s）
        self._procs_busy = False
        self._procs_timer = QTimer(self); self._procs_timer.setInterval(1000)
        self._procs_timer.timeout.connect(self._auto_procs)
        self._procs_timer.start()
        self.tick()
        self.tick_perf()
        self.tick_net()

    def tick(self):
        if not page_active(self):
            return
        try:
            m = S.mem_status()
            cpu = S.cpu_percent()
            pct = (m.ullTotalPhys - m.ullAvailPhys) * 100.0 / m.ullTotalPhys
            _set(self.bars["cpu"], "%.0f%%" % cpu, "1 秒实时采样", size="20px")
            self.bars["cpu"][2].setValue(int(cpu))
            _cls(self.bars["cpu"][2], cpu, "Cpu")
            _set(self.bars["mem"], "%.0f%%" % pct,
                 "%s / %s" % (fmt_bytes(m.ullTotalPhys - m.ullAvailPhys),
                              fmt_bytes(m.ullTotalPhys)),
                 size="20px")
            self.bars["mem"][2].setValue(int(pct))
            _cls(self.bars["mem"][2], pct, "Mem")
        except Exception:
            pass

    def tick_perf(self):
        if not page_active(self):
            return

        def got(d):
            gpu = d.get("gpu")
            _set(self.bars["gpu"], fmt_pct(gpu),
                 "温度 " + fmt_temp(d.get("gpu_temp")), size="20px")
            if gpu is not None:
                self.bars["gpu"][2].setValue(int(gpu))
                _cls(self.bars["gpu"][2], gpu, "Gpu")
            disk = d.get("disk")
            rd = sum((x.get("read_mbps") or 0) for x in (d.get("disks") or []))
            wr = sum((x.get("write_mbps") or 0) for x in (d.get("disks") or []))
            _set(self.bars["disk"], fmt_pct(disk),
                 "读 %s / 写 %s" % (fmt_mbps(rd), fmt_mbps(wr)), size="20px")
            if disk is not None:
                self.bars["disk"][2].setValue(int(disk))
                _cls(self.bars["disk"][2], disk, "Disk")
        Tasks.run(self, S.perf_stats, got,
                  on_fail=lambda *_: None)  # 刻意静默：1 秒轮询，不能弹框

    def tick_net(self):
        """网络速率卡片：1 秒实时（服务端常驻采样）。"""
        if not page_active(self):
            return

        def got(d):
            prim = (d or {}).get("primary") or {}
            link = prim.get("link_mbps") or (d or {}).get("speed_mbps")
            rx = (d or {}).get("rx_mbps")
            tx = (d or {}).get("tx_mbps")
            total = (rx or 0.0) + (tx or 0.0)
            _set(self.bars["net"], fmt_rate(total),
                 "↓ %s  ↑ %s · %s" % (fmt_rate(rx), fmt_rate(tx),
                                      fmt_linkspeed(link)),
                 size="20px")
            if link:
                pct = min(100, int(round(total * 100.0 / max(1.0, float(link)))))
                self.bars["net"][2].setValue(pct)
        Tasks.run(self, S.net_info, got,
                  on_fail=lambda *_: None)  # 刻意静默：1 秒轮询，不能弹框

    def refresh(self):
        self.load_procs()

    def load_procs(self, force=False):
        '''进程列表首帧：走会话缓存（CIM 全量）；之后每秒由 _auto_procs 自动刷新。'''
        if force:
            self.sess_invalidate("procs")
        Tasks.run_cached(self, "procs", lambda: S.list_processes(60),
                         self._got_procs)

    def _auto_procs(self):
        """每秒自动刷新进程表（页面可见才刷，见 page_active）。

        _procs_busy 防止上一帧还没回来就再起线程（快照虽快，也得兜底）。"""
        if not page_active(self) or self._procs_busy:
            return
        self._procs_busy = True

        def done(lst):
            self._procs_busy = False
            self._got_procs(lst)

        def fail(*_a):
            self._procs_busy = False

        Tasks.run(self, lambda: S.procs_fast(60), done, on_fail=fail)

    def trim_all(self):
        """一键整理：对所有占用 >8MB 的进程释放工作集（同概览页「一键整理内存」）。"""
        if not confirm(self, "一键整理内存",
                       "将对所有占用 >8MB 的进程执行 SetProcessWorkingSetSize(-1,-1)，"
                       "把工作集换出到页面文件。\n\n"
                       "说明：内存占用数字会立刻下降，但被换出的数据下次访问要从磁盘读回，"
                       "可能反而变慢。系统本身会按需管理内存。"):
            return

        def done(r):
            self.load_procs(True)      # 先启动刷新，再弹提示（模态对话框会阻塞事件循环）
            info(self, "已整理 %d / %d 个进程。\n\n"
                       "（其余为系统进程或权限不足，已跳过）" % r)

        Tasks.run(self, S.trim_all, done, on_fail=lambda m: warn(self, m))

    def _got_procs(self, lst):
        """进程表：进程 / PID / 内存 / CPU / GPU。
        CPU·GPU 列按数值排序（UserRole+1 存原始值），不是按字符串。
        每秒整表重建：先记住选中 PID 与滚动位置，重建后恢复，避免视觉跳动。"""
        self.procs = lst
        tbl = self.table
        sel_pid = None
        _r = tbl.currentRow()
        _it = tbl.item(_r, 1) if _r >= 0 else None
        if _it is not None:
            sel_pid = _it.data(Qt.UserRole + 1)
        _scroll = tbl.verticalScrollBar().value()
        tbl.setSortingEnabled(False)
        tbl.setRowCount(0)
        for p in lst:
            i = tbl.rowCount()
            tbl.insertRow(i)
            cells = [(p["name"], None),
                     (str(p["pid"]), p["pid"]),
                     (fmt_bytes(p["mem"]), p["mem"]),
                     ((("%.1f%%" % p["cpu"]) if p.get("cpu") is not None else "—"),
                      p.get("cpu")),
                     ((("%.1f%%" % p["gpu"]) if p.get("gpu") is not None else "—"),
                      p.get("gpu"))]
            for c, (txt, num) in enumerate(cells):
                it = SortItem(txt)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(txt)
                if num is not None:
                    it.setData(Qt.UserRole + 1, float(num))
                if c == 0:
                    ic = file_icon(p.get("exe"))
                    if ic is not None:
                        it.setIcon(ic)
                    it.setData(Qt.UserRole, p.get("exe") or "")   # 右键「定位文件夹」
                tbl.setItem(i, c, it)
            tbl.setRowHeight(i, 30)
        tbl.setSortingEnabled(True)
        tbl.setColumnWidth(0, 300)
        tbl.setColumnWidth(1, 76)
        tbl.setColumnWidth(2, 100)
        tbl.setColumnWidth(3, 70)
        # 恢复选中（PID 从单元格 UserRole+1 取，排序后行号已变）与滚动位置
        if sel_pid is not None:
            for i in range(tbl.rowCount()):
                c = tbl.item(i, 1)
                if c is not None and c.data(Qt.UserRole + 1) == sel_pid:
                    tbl.selectRow(i)
                    break
        _sb = tbl.verticalScrollBar()
        _sb.setValue(min(_scroll, _sb.maximum()))

    def trim_one(self):
        # 表格可排序 → 行号与 self.procs 不再对应，PID 从单元格里取
        r = self.table.currentRow()
        it = self.table.item(r, 1) if r >= 0 else None
        pid = it.data(Qt.UserRole + 1) if it else None
        if not pid:
            return info(self, "请先选择一个进程。")
        Tasks.run(self, lambda: S.trim_process(int(pid)),
                  lambda ok: (self.load_procs(True),            # 先刷新，再弹提示
                              info(self, "已整理" if ok else "整理失败（可能需要管理员权限）"))[0])


# ==========================================================================
# 7. 网络修复
# ==========================================================================
class NetworkPage(Page):
    title = "网络修复"

    REPAIRS = [("flushdns", "刷新 DNS 解析缓存", "最快最安全，先试这个"),
               ("registerdns", "重新注册 DNS 记录", "DNS 解析异常"),
               ("release", "释放 IP 地址", "配合重新获取使用"),
               ("renew", "重新获取 IP 地址", "DHCP 拿不到地址"),
               ("arpr", "清除 ARP 缓存", "局域网/ARP 冲突"),
               ("route", "重置路由表", "路由表被写乱"),
               ("winsock", "重置 Winsock 目录", "LSP 被劫持、浏览器打不开网页"),
               ("tcpip", "重置 TCP/IP 协议栈", "疑难断网，需重启"),
               ("ipv4", "重置 IPv4 协议栈", "TCP/IP 重置的补充"),
               ("firewall", "重置防火墙规则", "规则冲突，慎用")]

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        f1, v1 = card("全面诊断")
        h = QHBoxLayout(); h.addStretch(1)
        self.score = pill("—", "")
        h.addWidget(self.score)
        h.addWidget(btn("开始诊断", "Primary", on_click=self.diagnose))
        v1.addLayout(h)
        self.d_table = make_table(["检查项", "结果", "详情"])
        v1.addWidget(self.d_table)
        root.addWidget(f1)

        f2, v2 = card("一键修复")
        v2.addWidget(notice("重置 Winsock / 协议栈 / 路由表属于强力修复，会改写系统网络配置，"
                            "执行后需要重启。修复前建议先创建系统还原点。", "warn"))
        self.boxes = {}
        grid = QGridLayout(); grid.setSpacing(8)
        for i, (key, name, desc) in enumerate(self.REPAIRS):
            cb = QCheckBox("%s —— %s" % (name, desc))
            cb.setChecked(i < 5)
            self.boxes[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        v2.addLayout(grid)
        hb = QHBoxLayout(); hb.addStretch(1)
        self.repair_btn = btn("执行所选修复", "Danger", on_click=self.repair)
        self.repair_btn.setEnabled(self.win.admin_mode)
        hb.addWidget(self.repair_btn)
        v2.addLayout(hb)
        root.addWidget(f2)
        root.addStretch(1)

    def diagnose(self):
        self.win.busy("正在诊断网络（约 10~25 秒）…")
        Tasks.run(self, S.net_diagnose, lambda r: (self.win.idle(), self._got_diag(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def _got_diag(self, r):
        fill_table(self.d_table, [[s["name"], "正常" if s["ok"] else "异常", s["detail"]]
                                  for s in r["steps"]])
        self.d_table.setColumnWidth(0, 260)
        self.d_table.setColumnWidth(1, 70)
        p = self.score
        p.setText("正常 %d/%d" % (r["score"], r["total"]))
        p.setObjectName("PillOk" if r["score"] == r["total"] else
                        "PillWarn" if r["score"] >= r["total"] - 2 else "PillErr")
        p.style().unpolish(p); p.style().polish(p)

    def repair(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        steps = [k for k, cb in self.boxes.items() if cb.isChecked()]
        if not steps:
            return info(self, "没有选择任何修复项。")
        if not confirm(self, "确认修复",
                       "将执行 %d 项网络修复。\n\n部分操作会重置网络配置，需要重启电脑后生效。"
                       % len(steps), danger=True):
            return
        self.win.busy("正在修复…")
        Tasks.run(self, lambda: S.net_repair(steps), self._repaired,
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def _repaired(self, r):
        self.win.idle()
        fill_table(self.d_table, [[x["name"], "已完成" if x["ok"] else "失败", ""] for x in r])
        self.d_table.setColumnWidth(0, 300)
        info(self, "完成 %d / %d 项。\n\n若做了 Winsock / 协议栈 / 路由表重置，请重启电脑。"
             % (len([x for x in r if x["ok"]]), len(r)))


class PingWorker(QThread):
    """Continuous / fixed-count ping loop (one `ping -n 1` per tick)."""
    tick = Signal(dict)
    done = Signal()

    def __init__(self, host, count):
        super().__init__()
        self.host = host
        self.count = count   # -1 = 一直 ping，直到 requestInterruption()

    def run(self):
        sent = 0
        try:
            while not self.isInterruptionRequested():
                r = S.ping_once(self.host)
                sent += 1
                self.tick.emit(r)
                if 0 < self.count <= sent:
                    break
        finally:
            self.done.emit()


# ==========================================================================
# 9. DNS 检测
# ==========================================================================
class DNSPage(Page):
    title = "DNS 检测"

    MODES = [("国内", "domestic"), ("国外", "foreign"), ("混合", "mixed"),
             ("IPv6", "ipv6"), ("本机 DNS", "local")]

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # ---------- 1. Ping 测试（调用 Windows cmd，隐藏窗口） ----------
        f_ping, vp = card("Ping 测试（网络连通性）")
        vp.addWidget(notice(
            "调用 Windows ping 命令（隐藏窗口）检查连通性与丢包率。可选 ping 固定个数，"
            "或勾选“持续 Ping”一直测到手动停止。", "info"))
        bar_p = QHBoxLayout()
        bar_p.addWidget(QLabel("目标："))
        self.ping_host = QLineEdit("www.baidu.com")
        self.ping_host.setPlaceholderText("例如 www.baidu.com 或 114.114.114.114")
        bar_p.addWidget(self.ping_host, 1)
        bar_p.addWidget(QLabel("包数："))
        self.ping_count = QSpinBox()
        self.ping_count.setRange(1, 100)
        self.ping_count.setValue(4)
        bar_p.addWidget(self.ping_count)
        self.ping_cont = QCheckBox("持续 Ping（直到停止）")
        self.ping_cont.toggled.connect(self._on_cont)
        bar_p.addWidget(self.ping_cont)
        self.ping_btn = btn("开始 Ping", "Primary", on_click=self.do_ping)
        bar_p.addWidget(self.ping_btn)
        vp.addLayout(bar_p)
        self.ping_status = QLabel("尚未测试"); self.ping_status.setObjectName("Muted")
        self.ping_status.setWordWrap(True)
        vp.addWidget(self.ping_status)
        self.ping_table = make_table(["目标", "IP 地址", "发送/接收", "丢失率", "最短 / 最长 / 平均", "状态"])
        vp.addWidget(self.ping_table)
        # 持续 Ping 延迟明细：每次结果一行，新的在最上，向下依次是历史
        vd_hint = QLabel("延迟明细（新结果在上，向下为历史）：")
        vd_hint.setObjectName("Muted3")
        vp.addWidget(vd_hint)
        self.ping_detail = make_table(["序号", "时间", "延迟", "状态"])
        self.ping_detail.setMinimumHeight(140)
        self.ping_detail.setMaximumHeight(240)
        vp.addWidget(self.ping_detail)
        root.addWidget(f_ping)

        # ---------- 2. DNS 测试（解析速度） ----------
        f_dns, vd = card("DNS 测试（解析速度）")
        vd.addWidget(notice(
            "用 <b>ICMP ping</b> 测各 DNS 服务器的往返延迟（与 DnsTools 同口径）；"
            "同时做一次 DNS 查询显示解析结果 —— 禁 ping 但能正常解析的服务器会标注为"
            "「禁 ping · 可解析」。", "info"))
        bar = QHBoxLayout()
        self.combo = QComboBox()
        for name, key in self.MODES:
            self.combo.addItem(name, key)
        self.combo.setCurrentIndex(2)
        self.domain = QLineEdit("www.baidu.com")
        self.domain.setPlaceholderText("要解析的域名，例如 www.baidu.com")
        self.status = QLabel("尚未测试"); self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        bar.addWidget(QLabel("模式："))
        bar.addWidget(self.combo)
        bar.addWidget(QLabel("域名："))
        bar.addWidget(self.domain, 1)
        self.test_btn = btn("开始测试", "Primary", on_click=self.test)
        bar.addWidget(self.test_btn)
        vd.addLayout(bar)
        vd.addWidget(self.status)
        # 模式提示：随下拉框切换更新（本机 DNS 模式说明它的特殊之处）
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Muted3")
        self.mode_hint.setWordWrap(True)
        vd.addWidget(self.mode_hint)
        self.combo.currentIndexChanged.connect(self._on_mode)
        self._on_mode(self.combo.currentIndex())
        self.table = make_table(["DNS 服务器", "分类", "解析结果", "Ping 延迟", "状态"], sortable=True)
        # 固定列宽（结果到位前就摆好，避免边测边跳）
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 56)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 80)
        vd.addWidget(self.table)
        root.addWidget(f_dns)
        root.addStretch(0)

        self._worker = None
        self._ping_stats = {}
        # ---- DNS 流式显示：工作线程把结果塞进队列，UI 定时器取出来逐条上屏 ----
        self._dns_q = queue.Queue()
        self._dns_busy = False
        self._dns_count = 0
        self._dns_ok = 0
        self._dns_timer = QTimer(self)
        self._dns_timer.setInterval(60)
        self._dns_timer.timeout.connect(self._drain_dns)

    def _on_cont(self, v):
        self.ping_count.setEnabled(not v)

    # ---------- Ping ----------
    def do_ping(self):
        if self._worker is not None and self._worker.isRunning():
            # 停止
            self._worker.requestInterruption()
            self.ping_btn.setText("停止中…")
            self.ping_btn.setEnabled(False)
            return
        host = self.ping_host.text().strip() or "www.baidu.com"
        cont = self.ping_cont.isChecked()
        count = -1 if cont else self.ping_count.value()
        self._ping_stats = {"sent": 0, "recv": 0, "min": None, "max": None,
                            "sum": 0, "ip": None, "ok_once": False}
        self.ping_table.setRowCount(0)
        self.ping_detail.setRowCount(0)
        self.ping_btn.setText("停止")
        self.ping_btn.setObjectName("Danger")
        self.ping_btn.style().unpolish(self.ping_btn); self.ping_btn.style().polish(self.ping_btn)
        self.win.busy("正在 Ping…")
        self._worker = PingWorker(host, count)
        self._worker.tick.connect(self._ping_tick)
        self._worker.done.connect(self._ping_done)
        self._worker.start()

    def _ping_tick(self, r):
        s = self._ping_stats
        s["sent"] += 1
        if r.get("ip"):
            s["ip"] = r["ip"]
        if r.get("ok"):
            s["recv"] += 1
            s["ok_once"] = True
            if r.get("ms") is not None:
                s["sum"] += r["ms"]
                s["min"] = r["ms"] if s["min"] is None else min(s["min"], r["ms"])
                s["max"] = r["ms"] if s["max"] is None else max(s["max"], r["ms"])
        # 延迟明细：新结果插到第 0 行（最上面），向下依次是历史；最多留 200 条
        self.ping_detail.insertRow(0)
        row = [str(s["sent"]), time.strftime("%H:%M:%S"),
               ("%d ms" % r["ms"]) if (r.get("ok") and r.get("ms") is not None) else "—",
               "成功" if r.get("ok") else "超时"]
        for c, v in enumerate(row):
            it = QTableWidgetItem(v)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.ping_detail.setItem(0, c, it)
        if self.ping_detail.rowCount() > 200:
            self.ping_detail.removeRow(self.ping_detail.rowCount() - 1)
        self._repaint_ping()

    def _repaint_ping(self):
        s = self._ping_stats
        sent, recv = s["sent"], s["recv"]
        loss = (100 * (sent - recv) // sent) if sent else 0
        avg = (s["sum"] // recv) if recv else None
        mm = "—"
        if s["min"] is not None:
            mm = "%d / %d / %d ms" % (s["min"], s["max"], avg if avg is not None else 0)
        state = "可达" if s["ok_once"] else "不可达"
        fill_table(self.ping_table, [[
            self.ping_host.text().strip() or "www.baidu.com",
            s["ip"] or "—",
            "%d / %d" % (sent, recv),
            "%d%%" % loss,
            mm,
            state]])
        self.ping_table.setColumnWidth(0, 150)
        self.ping_table.setColumnWidth(1, 120)
        self.ping_table.setColumnWidth(2, 80)
        self.ping_table.setColumnWidth(3, 70)
        self.ping_status.setText("已发送 %d，接收 %d，丢失 %d%%，平均 %s" %
                                 (sent, recv, loss, (("%d ms" % avg) if avg is not None else "—")))

    def _ping_done(self):
        self.win.idle()
        self.ping_btn.setText("开始 Ping")
        self.ping_btn.setObjectName("Primary")
        self.ping_btn.setEnabled(True)
        self.ping_btn.style().unpolish(self.ping_btn); self.ping_btn.style().polish(self.ping_btn)
        s = self._ping_stats
        if s.get("sent"):
            self.ping_status.setText(self.ping_status.text() + "　（已停止）")

    # ---------- DNS ----------
    @staticmethod
    def _dns_row(x):
        """一台 DNS 的表格行（流式追加与最终重排共用，保证格式一致）。"""
        if x["ok"]:                        # ping 通
            state = "可用"
        elif x.get("dns_ok"):              # 禁 ICMP 但能正常解析（如 114）
            state = "禁 ping · 可解析"
        else:
            state = "不可用"
        return [
            "%s (%s)" % (x["name"], x["ip"]),
            x.get("cat") or "—",
            x.get("answer") or "—",
            ("%d ms" % x["ms"]) if x["ok"] and x["ms"] is not None else "—",
            state,
        ]

    def _append_dns_row(self, x):
        """追加一行（主线程执行）。追加期间必须关排序，否则行会乱跳。"""
        self.table.setSortingEnabled(False)
        i = self.table.rowCount()
        self.table.insertRow(i)
        for c, v in enumerate(self._dns_row(x)):
            it = SortItem(str(v))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setToolTip(str(v))
            num = _sort_num(v)
            if num is not None:
                it.setData(Qt.UserRole + 1, num)
            self.table.setItem(i, c, it)
        self.table.setRowHeight(i, 30)

    def _drain_dns(self):
        """把工作线程推送的结果逐条上屏（定时器在主线程调用）。"""
        got = 0
        while True:
            try:
                x = self._dns_q.get_nowait()
            except queue.Empty:
                break
            self._append_dns_row(x)
            self._dns_count += 1
            if x.get("ok"):
                self._dns_ok += 1
            got += 1
        if got:
            self.status.setText("正在测试… 已测出 %d 个（可用 %d）"
                                % (self._dns_count, self._dns_ok))

    def _on_mode(self, idx):
        """下拉框切换时更新模式提示。"""
        mode = self.combo.itemData(idx)
        if mode == "local":
            self.mode_hint.setText(
                "本机 DNS 模式：用 ICMP ping 测各 DNS 服务器的往返延迟（与 DnsTools 同口径）。"
                "服务器集 = 本机网卡当前配置的 DNS（标「本机在用」）+ 公共 DNS，"
                "可直接看出换哪个对本机更快；「解析结果」为顺带做的一次 DNS 查询。")
        else:
            self.mode_hint.setText(
                "用 ICMP ping 测各 DNS 服务器的往返延迟（与 DnsTools 同口径）；"
                "同时做一次 DNS 查询显示解析结果；禁 ping 但能解析的会单独标注。")

    def test(self):
        if self._dns_busy:
            return
        mode = self.combo.currentData()
        domain = self.domain.text().strip() or "www.baidu.com"
        if mode == "local":
            # 本机模式即使没配 DNS 也有公共 DNS 可测，这里只是把「本机在用」个数报出来
            n = len(S.local_dns_servers())
            self.status.setText("正在 Ping 各 DNS 服务器…" +
                                ("（本机在用 %d 个）" % n if n else ""))
        else:
            self.status.setText("正在 Ping 各 DNS 服务器…")
        self.table.setRowCount(0)
        self._dns_count = self._dns_ok = 0
        self._dns_q = queue.Queue()
        self._dns_busy = True
        self.test_btn.setEnabled(False)
        self._dns_timer.start()
        self.win.busy("正在测试 DNS…")
        # 工作线程每测完一台就把结果 put 进队列，UI 由定时器取出来立刻上屏
        Tasks.run(self,
                  lambda: S.dns_test(mode, domain, on_each=self._dns_q.put),
                  self._done, on_fail=self._dns_fail)

    def _dns_stop(self):
        self._dns_timer.stop()
        self._dns_busy = False
        self.test_btn.setEnabled(True)
        self.win.idle()

    def _dns_fail(self, m):
        self._dns_stop()
        self.status.setText("失败：" + m)

    def _done(self, r):
        # 收尾：停表 → 把队列里最后几条也上屏 → 再按「可用优先、延迟升序」重排
        self._dns_timer.stop()
        self._drain_dns()
        self._dns_busy = False
        self.test_btn.setEnabled(True)
        self.win.idle()
        if not r:
            self.status.setText("共 0 个 DNS")
            return
        fill_table(self.table, [self._dns_row(x) for x in r])
        # 流式追加期间排序是关着的，收尾必须显式打开 —— 否则这个表头从此点不动
        # （fill_table 只看「当时开着没开」，它看到的是关）。再按「延迟」升序排，
        # 刚好就是「Ping 最快」的展示顺序，不可用的 "—" 自然落在最后。
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(3, Qt.AscendingOrder)
        ok = [x for x in r if x["ok"]]
        lead = "Ping 最快"
        if ok and ok[0].get("ms") is not None:
            self.status.setText("%s：%s（%d ms），共 %d / %d 个可用，解析到 %s" %
                                (lead, ok[0]["name"], ok[0]["ms"], len(ok), len(r),
                                 ok[0].get("answer") or "—"))
        else:
            self.status.setText("共 %d 个 DNS，%d 个可用" % (len(r), len(ok)))


# ==========================================================================
# 9. 垃圾清理
# ==========================================================================
class CleanupPage(Page):
    title = "垃圾清理"

    def __init__(self, win):
        super().__init__(win)
        self.items = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        bar = QHBoxLayout()
        self.btn_scan = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_scan)
        self.ctl_clean = ScanCtl(self.btn_scan, lambda *_: self.scan())
        self.btn_clean = btn("清理所选", "Danger", on_click=self.clean)
        self.btn_clean.setEnabled(self.win.admin_mode)
        bar.addWidget(self.btn_clean)
        bar.addStretch(1)
        # 文件年龄阈值：只清「够旧」的文件，程序正在用（刚生成）的临时文件不动。
        # 之前是无差别删目录下所有条目，程序刚创建、稍后才读的临时文件会被误删。
        bar.addWidget(QLabel("文件年龄："))
        self.combo_age = QComboBox()
        for _k in ("normal", "strict", "off"):
            self.combo_age.addItem(S.JUNK_AGE_LABELS[_k], _k)
        self.combo_age.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        _m = self.win.settings.get("junk_age_mode", S.JUNK_AGE_DEFAULT)
        _ix = self.combo_age.findData(_m)
        self.combo_age.setCurrentIndex(_ix if _ix >= 0 else 0)
        self.combo_age.setToolTip(
            "只清理「修改时间」早于该阈值的文件。<br>"
            "临时文件默认 24 小时、内存/崩溃转储 48 小时 —— "
            "这样程序正在写、稍后才读的临时文件不会被误删。<br>"
            "「不限制」会连刚生成的文件一起清，可能让某些程序出错。")
        self.combo_age.currentIndexChanged.connect(self.on_age_mode)
        bar.addWidget(self.combo_age)
        self.sum = pill("—", "")
        bar.addWidget(self.sum)
        root.addLayout(bar)
        root.addWidget(notice(
            "扫描只统计体积，不会删除东西。清理时正在被占用的文件会自动跳过。<br>"
            "<b>建议清理</b>（绿色）＝删了没有任何影响；<b>可以清理</b>（蓝色）＝删了会重新生成，"
            "首次打开可能稍慢；<b>谨慎清理</b>（橙色）＝涉及回收站或系统缓存，默认不勾选，"
            "请确认后再勾。每项的「说明」列写明了这些文件是什么。<br>"
            "右侧「文件年龄」决定多新的文件会被留下来 —— "
            "默认只清 1 天前的临时文件、2 天前的转储，避免误删程序正在用的文件。", "info"))

        # 扫描进度（带百分比）：扫描期间不列数据，扫完一次性列出
        self.prog_row = QWidget()
        pr = QHBoxLayout(self.prog_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(10)
        self.prog_lb = QLabel(""); self.prog_lb.setObjectName("Muted")
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setValue(0)
        self.prog.setTextVisible(True)
        self.prog.setFormat("%p%")
        self.prog.setMinimumWidth(240)
        pr.addWidget(self.prog_lb, 0)
        pr.addWidget(self.prog, 1)
        self.prog_row.setVisible(False)
        root.addWidget(self.prog_row)

        f, v = card("可清理项目")
        # 分类：常规清理 / 浏览器缓存 / 系统缓存
        self.table = make_table(["", "分类", "项目", "建议", "路径", "体积", "文件数", "说明"],
                                sortable=True)
        attach_reveal(self.table, lambda t, r: t.item(r, 4).text())   # 第 4 列就是路径
        self.table.setColumnWidth(0, 34)
        self.table.itemChanged.connect(lambda *_: self.update_sum())
        v.addWidget(self.table, 1)
        root.addWidget(f, 1)

        # 全选 / 全不选（挂在顶部工具条「清理所选」右侧）
        self.btn_all = select_all_button(self.table, on_changed=self.update_sum)
        bar.insertWidget(bar.indexOf(self.btn_clean) + 1, self.btn_all)

    def refresh(self):
        """不再自动扫描：进入页面只重置为待扫描状态，由用户点「开始扫描」触发。"""
        self._scan_token = getattr(self, "_scan_token", 0) + 1   # 作废进行中的扫描
        self.items = []
        self.table.setRowCount(0)
        self.prog_row.setVisible(False)
        self.sum.setText("未扫描")
        self.sum.setObjectName("Pill")
        self.sum.style().unpolish(self.sum)
        self.sum.style().polish(self.sum)
        if hasattr(self, "btn_all"):
            self.btn_all._refresh()          # 无数据时全选按钮自动置灰

    def _age_scale(self):
        """当前「文件年龄」档位对应的缩放系数（1.0 = 推荐档）。"""
        if hasattr(self, "combo_age"):
            return S.junk_age_scale(self.combo_age.currentData())
        return S.junk_age_scale(self.win.settings.get("junk_age_mode"))

    def on_age_mode(self, _i):
        """切换档位：持久化，并按新阈值重扫 —— 阈值会改变每一项的体积。"""
        if not hasattr(self, "combo_age"):
            return
        mode = self.combo_age.currentData() or S.JUNK_AGE_DEFAULT
        self.win.settings["junk_age_mode"] = mode
        settings_save(self.win.settings)
        if self.items:
            self.scan()          # 已有结果 → 直接按新阈值重扫，避免数字对不上实际
        else:
            self.refresh()

    _SCAN_MAX_PAR = 4        # 同时最多跑几个后台扫描（垃圾项彼此独立，4 路足够）

    def scan(self):
        """并行扫描所有垃圾目录（**限并发**）：起 N 个后台任务，完成一个补一个。"""
        self._scan_token = getattr(self, "_scan_token", 0) + 1
        token = self._scan_token
        scale = self._age_scale()
        self.table.setRowCount(0)
        self.items = []
        targets = [t["id"] for t in S.junk_targets(scale)] + ["recyclebin"]
        total = len(targets)
        self.prog_row.setVisible(True)
        self.prog.setRange(0, total)
        self.prog.setValue(0)
        self.prog_lb.setText("正在扫描 0/%d…" % total)
        self.sum.setText("扫描中…")
        self.win.busy("正在扫描垃圾文件…")
        self._res = {}
        self._done_n = 0
        # 限并发：早先是把 20 个目标一次性全丢出去 = 同时 20 个 QThread 一起跑、
        # 一起收尾（线程抖动大、资源尖峰）。改成「起 N 个，完成一个补一个」。
        self._pend = list(targets)
        self._scan_total = total
        for _ in range(min(self._SCAN_MAX_PAR, total)):
            self._start_one(token)

    def _start_one(self, token):
        """从待扫队列取一个目标起任务（限并发：完成一个补一个）。"""
        if token != getattr(self, "_scan_token", 0):
            return
        if not getattr(self, "_pend", None):
            return
        tid = self._pend.pop(0)
        scale = self._age_scale()
        total = getattr(self, "_scan_total", 0)
        Tasks.run(self, lambda t=tid, sc=scale: S.junk_item_size(t, sc),
                  lambda r, tk=token: self._one_done(tk, r, total),
                  on_fail=lambda _m, tk=token: self._one_step(tk, total))

    def _one_done(self, token, r, total):
        if token != getattr(self, "_scan_token", 0):
            return
        if r:
            self._res[r["id"]] = r
        self._one_step(token, total)

    def _one_step(self, token, total):
        if token != getattr(self, "_scan_token", 0):
            return
        self._done_n += 1
        self.prog.setValue(self._done_n)
        self.prog_lb.setText("正在扫描 %d/%d…" % (self._done_n, total))
        if self._done_n >= total:
            self._finish(token)
        else:
            self._start_one(token)      # 补位，保持最多 _SCAN_MAX_PAR 个在跑

    def _finish(self, token):
        if token != getattr(self, "_scan_token", 0):
            return
        self._pend = []                  # 收尾：清掉未起的队列（重扫会重建）
        if hasattr(self, "ctl_clean"):
            self.ctl_clean.finish()      # 每项都是独立小任务，收尾时统一复位按钮
        order = [t["id"] for t in S.junk_targets(self._age_scale())] + ["recyclebin"]
        items = [self._res[i] for i in order if i in self._res]
        self.items = items
        # 扫完一次性填表（填表期间关排序，避免行错位）
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for x in items:
            i = self.table.rowCount()
            self.table.insertRow(i)
            lv = x.get("level") or "ok"
            grp = x.get("group") or "常规清理"
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # 默认只勾「建议清理 / 可以清理」，谨慎项留空由用户自己决定
            cb.setCheckState(Qt.Unchecked if lv == "caution" else Qt.Checked)
            cb.setData(Qt.UserRole, x["id"])     # 排序后仍能对上数据
            self.table.setItem(i, 0, cb)

            it_grp = SortItem(grp)
            it_grp.setFlags(it_grp.flags() & ~Qt.ItemIsEditable)
            it_grp.setData(Qt.UserRole + 1,
                           {"常规清理": 0, "浏览器缓存": 1, "系统缓存": 2}.get(grp, 3))
            self.table.setItem(i, 1, it_grp)

            lv_txt, lv_color = {
                "recommend": ("建议清理", T.get_color("OK", self.win.dark)),
                "ok": ("可以清理", T.get_color("BLUE", self.win.dark)),
                "caution": ("谨慎清理", T.get_color("WARN", self.win.dark)),
            }.get(lv, ("可以清理", T.get_color("BLUE", self.win.dark)))
            it_lv = SortItem(lv_txt)
            it_lv.setFlags(it_lv.flags() & ~Qt.ItemIsEditable)
            it_lv.setForeground(QBrush(QColor(lv_color)))
            it_lv.setData(Qt.UserRole + 1, {"recommend": 0, "ok": 1, "caution": 2}[lv])
            it_lv.setToolTip(x.get("what") or "")
            self.table.setItem(i, 3, it_lv)

            what = x.get("what") or ""
            # 把该项目的实际年龄阈值写进说明列 —— 用户能直接看到"多新的会被保留"
            if x.get("min_age_hours"):
                what += "（只清理 %g 小时前的文件）" % x["min_age_hours"]
            for col, val in ((2, x["name"]), (4, x["path"]), (5, fmt_bytes(x["size"])),
                             (6, x["files"]), (7, what)):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                num = _sort_num(val)
                if num is not None:
                    it.setData(Qt.UserRole + 1, num)
                it.setToolTip(str(val))
                self.table.setItem(i, col, it)
            self.table.setRowHeight(i, 40)
        self.table.setSortingEnabled(True)
        self.table.setColumnWidth(1, 86)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 82)
        self.table.setColumnWidth(4, 240)
        self.table.setColumnWidth(5, 88)
        self.table.setColumnWidth(6, 64)
        self.table.setColumnWidth(7, 300)
        self.prog_lb.setText("扫描完成（%d 项）" % len(items))
        self.prog.setValue(self.prog.maximum())
        self.prog_row.setVisible(False)
        self.win.idle()
        self.update_sum()

    def update_sum(self):
        total = 0
        by_id = {x["id"]: x for x in self.items}
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and it.checkState() == Qt.Checked:
                x = by_id.get(it.data(Qt.UserRole))
                if x:
                    total += x["size"]
        self.sum.setText("可清理 " + fmt_bytes(total))
        b = getattr(self, "btn_all", None)
        if b is not None:
            b._refresh()                 # 全选按钮文案与计数跟着勾选状态走

    def clean(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        ids, total = [], 0
        by_id = {x["id"]: x for x in self.items}
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                tid = it.data(Qt.UserRole)
                if tid and by_id.get(tid, {}).get("size", 0) > 0:
                    ids.append(tid)
                    total += by_id[tid]["size"]
        if not ids:
            return info(self, "没有选择任何项。")
        caution = [by_id[i]["name"] for i in ids
                   if (by_id[i].get("level") == "caution")]
        tip = ""
        if caution:
            tip = ("\n\n⚠️ 其中包含谨慎项：\n  · " + "\n  · ".join(caution) +
                   "\n（回收站清空后不可恢复；Prefetch 删除后短期内程序启动会变慢）")
        # 「是否备份」摆到确认框里让用户当场选 —— 以前是静默决定处置方式，
        # 用户既看不到也选不了。回收站每个文件都要建记录，实测约 45 毫秒/个，
        # 所以把预计耗时也写出来，由用户自己权衡。
        _n_files = sum(int(x.get("files") or 0) for x in self.items
                       if x["id"] in ids)
        _est = max(1, int(_n_files * 0.045))
        _est_txt = ("约 %d 分钟" % round(_est / 60)) if _est >= 90 else ("约 %d 秒" % _est)
        _ok, _bak = confirm_ex(
            self, "确认清理",
            "将清理约 %s 的临时/缓存文件（%d 个文件）。\n\n"
            "「文件年龄」当前为：%s\n"
            "（比这个时间新的文件会原样保留，不参与清理）\n\n"
            "不勾选备份时：直接删除，速度最快。\n"
            "勾选备份时：先移入回收站（可还原），但预计慢 %s。%s"
            % (fmt_bytes(total), _n_files,
               S.JUNK_AGE_LABELS.get(self.combo_age.currentData(), ""),
               _est_txt, tip),
            backup_label="删除前移入回收站（可还原，但很慢）",
            backup_default=False,
            backup_note="提示：临时文件/缓存删了通常没有影响；"
                        "只有拿不准时才需要勾选。")
        if not _ok:
            return
        self.win.busy("正在清理…")
        scale = self._age_scale()

        def _on_cleaned(r):
            self.win.idle()
            freed = sum(x.get("freed", 0) for x in r)
            kept = sum(x.get("skipped", 0) for x in r)
            rec = sum(x.get("recycled", 0) for x in r)
            self.scan()                      # 先启动扫描（异步），对话框期间后台已在跑
            self.win.notify_changed("overview")   # 磁盘余量变了 → 概览页存储重读
            msg = "清理完成，释放 %s" % fmt_bytes(freed)
            if rec:
                msg += "\n\n其中 %d 个文件已移入回收站，可从回收站还原。" % rec
            if kept:
                # 如实报出保留数量：这正是"时间阈值"在起作用，别让用户以为漏清了
                msg += "\n\n另有 %d 个较新的文件按年龄阈值保留了下来。" % kept
            info(self, msg)
        Tasks.run(self, lambda: S.clean_junk(ids, scale, _bak), _on_cleaned,
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])


class DiskToolsPage(Page):
    """磁盘工具（空文件夹清理 / 重复文件查找 / 文件夹占用分析 / 文件粉碎）。

    四个页签共用同一套交互：**扫描只读 → 列表勾选 → 明确确认 → 执行**。
    删除类一律走回收站（可还原）；粉碎是唯一的不可逆操作，单独页签并二次确认。
    """

    title = "磁盘工具"

    def __init__(self, win):
        super().__init__(win)
        self.empty_rows = []
        self.dup_rows = []
        self.usage_rows = []
        self.shred_rows = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_empty(), "空文件夹清理")
        self.tabs.addTab(self._build_dup(), "重复文件查找")
        self.tabs.addTab(self._build_usage(), "文件夹占用分析")
        self.tabs.addTab(self._build_diskscan(), "磁盘占用扫描")
        self.tabs.addTab(self._build_shred(), "文件粉碎")

    # ---------------------------------------------------------------- 空文件夹
    def _build_empty(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_empty = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_empty)
        self.ctl_empty = ScanCtl(self.btn_empty, lambda *_: self.scan_empty())
        bar.addWidget(QLabel("仅列出空闲超过"))
        self.sp_empty_age = QSpinBox()
        self.sp_empty_age.setRange(0, 3650)
        self.sp_empty_age.setValue(30)
        self.sp_empty_age.setSuffix(" 天")
        self.sp_empty_age.setMaximumWidth(96)
        bar.addWidget(self.sp_empty_age)
        bar.addWidget(QLabel("最大深度"))
        self.sp_empty_depth = QSpinBox()
        self.sp_empty_depth.setRange(1, 8)
        self.sp_empty_depth.setValue(3)
        self.sp_empty_depth.setMaximumWidth(72)
        bar.addWidget(self.sp_empty_depth)
        bar.addStretch(1)
        self.pill_empty = pill("未扫描", "")
        bar.addWidget(self.pill_empty)
        v.addLayout(bar)
        v.addWidget(notice(
            "扫描范围：下载 / 文档 / 桌面 / 图片 / 视频 / 音乐（跟随系统的真实位置，"
            "重定向到其它盘的也能扫到）。<br>"
            "判定：整棵子树里没有任何文件才算空，且只列<b>最上层</b>那个 —— "
            "删掉父目录时子目录会一起消失。默认只报空闲超过 30 天的，"
            "避免删掉软件正在使用的临时目录。", "info"))
        f, box = card("空文件夹")
        self.tb_empty = make_table(["", "文件夹", "层级", "空闲时长"], sortable=True)
        attach_reveal(self.tb_empty, lambda t, r: t.item(r, 1).text())
        self.tb_empty.setColumnWidth(0, 34)
        self.tb_empty.itemChanged.connect(lambda *_: self._empty_sel())
        box.addWidget(self.tb_empty, 1)
        v.addWidget(f, 1)
        hb = QHBoxLayout()
        self.btn_empty_del = btn("删除所选（送回收站）", "Danger", on_click=self.del_empty)
        self.btn_empty_del.setEnabled(False)
        hb.addWidget(self.btn_empty_del)
        hb.addWidget(select_all_button(self.tb_empty, on_changed=self._empty_sel))
        hb.addStretch(1)
        self.lb_empty = QLabel("")
        self.lb_empty.setObjectName("Muted")
        hb.addWidget(self.lb_empty)
        v.addLayout(hb)
        return w

    def scan_empty(self):
        self.win.busy("正在扫描空文件夹…")
        self.tb_empty.setRowCount(0)
        self.empty_rows = []
        age, depth = self.sp_empty_age.value(), self.sp_empty_depth.value()

        def done(r):
            self.win.idle()
            self.empty_rows = r or []
            t = self.tb_empty
            t.blockSignals(True)
            t.setSortingEnabled(False)
            t.setRowCount(0)
            for x in self.empty_rows:
                i = t.rowCount()
                t.insertRow(i)
                cb = QTableWidgetItem()
                cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                cb.setCheckState(Qt.Unchecked)     # 空目录默认不勾
                cb.setData(Qt.UserRole, x["path"])
                t.setItem(i, 0, cb)
                p = SortItem(x["path"])
                p.setFlags(p.flags() & ~Qt.ItemIsEditable)
                p.setToolTip(x["path"])
                t.setItem(i, 1, p)
                d = SortItem(str(x["depth"]))
                d.setFlags(d.flags() & ~Qt.ItemIsEditable)
                d.setData(Qt.UserRole + 1, x["depth"])
                t.setItem(i, 2, d)
                a = SortItem("%d 小时" % x["age_hours"])
                a.setFlags(a.flags() & ~Qt.ItemIsEditable)
                a.setData(Qt.UserRole + 1, x["age_hours"])
                a.setForeground(QBrush(QColor(T.get_color("WARN", self.win.dark))))
                t.setItem(i, 3, a)
            t.setSortingEnabled(True)
            t.setColumnWidth(1, 520)
            t.blockSignals(False)
            self.pill_empty.setText("找到 %d 个" % len(self.empty_rows))
            self.btn_empty_del.setEnabled(bool(self.empty_rows))
            self._empty_sel()
            if not self.empty_rows:
                info(self, "没有符合条件的空文件夹。")

        Tasks.run(self, lambda: S.empty_dirs_scan(
            min_age_hours=age * 24, max_depth=depth,
            should_stop=self.ctl_empty.stop_flag), done, ctl=self.ctl_empty,
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败", m))[1])

    def _empty_sel(self):
        n = 0
        for r in range(self.tb_empty.rowCount()):
            it = self.tb_empty.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                n += 1
        self.lb_empty.setText("已选 %d 个" % n if n else "未选择")

    def del_empty(self):
        paths = [self.tb_empty.item(r, 0).data(Qt.UserRole)
                 for r in range(self.tb_empty.rowCount())
                 if self.tb_empty.item(r, 0)
                 and self.tb_empty.item(r, 0).checkState() == Qt.Checked]
        paths = [p for p in paths if p]
        if not paths:
            return info(self, "没有勾选任何目录。")
        if not confirm(self, "删除空文件夹",
                       "将删除 %d 个空文件夹（送回收站，可还原）。\n"
                       "只删空目录，非空的会自动跳过。" % len(paths), danger=True):
            return
        self.win.busy("正在删除…")

        def done(r):
            self.win.idle()
            ok = [x for x in (r or []) if x.get("ok")]
            bad = [x for x in (r or []) if not x.get("ok")]
            self.scan_empty()
            msg = "已删除 %d 个" % len(ok)
            if bad:
                msg += "，%d 个未成功：\n" % len(bad) + "\n".join(
                    "· %s（%s）" % (os.path.basename(x["path"]), x["note"]) for x in bad[:8])
            info(self, msg)

        Tasks.run(self, lambda: S.delete_paths(paths), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "删除失败", m))[1])

    # ---------------------------------------------------------------- 重复文件
    def _build_dup(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_dup = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_dup)
        self.ctl_dup = ScanCtl(self.btn_dup, lambda *_: self.scan_dup())
        bar.addWidget(QLabel("最小文件"))
        self.cb_dup_size = QComboBox()
        self.cb_dup_size.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for label, val in (("100 KB", 100 * 1024), ("1 MB", 1024 * 1024),
                           ("10 MB", 10 * 1024 * 1024), ("100 MB", 100 * 1024 * 1024)):
            self.cb_dup_size.addItem(label, val)
        self.cb_dup_size.setCurrentIndex(1)
        bar.addWidget(self.cb_dup_size)
        self.ck_dup_hash = QCheckBox("校验内容（SHA-256，慢但准）")
        self.ck_dup_hash.setChecked(True)
        self.ck_dup_hash.setToolTip("不勾则只按文件大小判重：更快，"
                                     "但可能把「同名不同内容」的文件误判成重复")
        bar.addWidget(self.ck_dup_hash)
        bar.addStretch(1)
        self.pill_dup = pill("未扫描", "")
        bar.addWidget(self.pill_dup)
        v.addLayout(bar)
        # 查找范围：常用位置（默认，保持原行为） / 指定文件夹 / 整个磁盘
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("查找范围"))
        self.cb_dup_scope = QComboBox()
        self.cb_dup_scope.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.cb_dup_scope.addItem("常用位置（桌面 / 下载 / 文档…）", "default")
        self.cb_dup_scope.addItem("指定文件夹…", "folder")
        self.cb_dup_scope.addItem("整个磁盘", "drive")
        self.cb_dup_scope.currentIndexChanged.connect(self._dup_scope_changed)
        row2.addWidget(self.cb_dup_scope)
        # 文件夹模式：显示已选目录 + 「选择…」
        self._dup_dir = ""
        self.lb_dup_dir = QLabel("未选目录")
        self.lb_dup_dir.setObjectName("Muted")
        self.lb_dup_dir.setMaximumWidth(300)
        self.btn_dup_dir = btn("选择…", on_click=self._dup_pick_dir)
        row2.addWidget(self.lb_dup_dir)
        row2.addWidget(self.btn_dup_dir)
        # 磁盘模式：选盘
        self.cb_dup_drive = QComboBox()
        self.cb_dup_drive.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        row2.addWidget(self.cb_dup_drive)
        row2.addStretch(1)
        v.addLayout(row2)
        v.addWidget(notice(
            "按「大小分组 → SHA-256 校验」两段判定，确认内容完全一致才算重复。<br>"
            "<b>每组会自动保留最早修改的那一份（不勾）</b>，只勾其余的 —— "
            "删重复文件时至少要留一份，这是硬规则。<br>"
            "范围可选「常用位置 / 指定文件夹 / 整个磁盘」：整盘会遍历该盘所有目录"
            "（系统保护目录会跳过），文件多时要几分钟，期间可随时点「暂停扫描」。",
            "info"))
        f, box = card("重复文件")
        self.tb_dup = make_table(["", "文件名", "完整路径", "大小", "修改时间"],
                                 sortable=True)
        attach_reveal(self.tb_dup, lambda t, r: t.item(r, 2).text())
        self.tb_dup.setColumnWidth(0, 34)
        self.tb_dup.itemChanged.connect(lambda *_: self._dup_sel())
        box.addWidget(self.tb_dup, 1)
        v.addWidget(f, 1)
        hb = QHBoxLayout()
        self.btn_dup_del = btn("删除勾选的副本", "Danger", on_click=self.del_dup)
        self.btn_dup_del.setEnabled(False)
        hb.addWidget(self.btn_dup_del)
        hb.addWidget(select_all_button(self.tb_dup, on_changed=self._dup_sel))
        hb.addStretch(1)
        self.lb_dup = QLabel("")
        self.lb_dup.setObjectName("Muted")
        hb.addWidget(self.lb_dup)
        v.addLayout(hb)
        self._dup_scope_changed()        # 初始化「选目录 / 选盘」的显隐
        return w

    def _dup_scope_changed(self, *_a):
        """按「查找范围」切换控件：文件夹模式给选目录，磁盘模式给选盘。"""
        mode = self.cb_dup_scope.currentData()
        is_folder = (mode == "folder")
        is_drive = (mode == "drive")
        self.lb_dup_dir.setVisible(is_folder)
        self.btn_dup_dir.setVisible(is_folder)
        self.cb_dup_drive.setVisible(is_drive)
        if is_drive and self.cb_dup_drive.count() == 0:
            self.refresh_dup_drives()

    def _dup_pick_dir(self):
        """选要查找重复文件的文件夹（记住上次选的）。"""
        d = QFileDialog.getExistingDirectory(
            self, "选择要查找重复文件的文件夹",
            self._dup_dir or os.path.expanduser("~"))
        if d:
            self._dup_dir = d
            self.lb_dup_dir.setText(d if len(d) <= 38 else "…" + d[-36:])
            self.lb_dup_dir.setToolTip(d)

    def refresh_dup_drives(self):
        """刷新重复查找用的磁盘下拉（默认选第一个固定盘）。"""
        keep = self.cb_dup_drive.currentData() if self.cb_dup_drive.count() else None
        self.cb_dup_drive.blockSignals(True)
        self.cb_dup_drive.clear()
        for d in S.list_drives():
            self.cb_dup_drive.addItem("%s  %s可用 %s / 共 %s" % (
                d["root"], ("「%s」 " % d["label"]) if d["label"] else "",
                fmt_bytes(d["free"]), fmt_bytes(d["total"])), d["root"])
        if keep:
            i = self.cb_dup_drive.findData(keep)
            if i >= 0:
                self.cb_dup_drive.setCurrentIndex(i)
        self.cb_dup_drive.blockSignals(False)

    def scan_dup(self):
        mode = self.cb_dup_scope.currentData()
        roots = None                     # None = 常用位置（走 server 默认根）
        if mode == "folder":
            if not self._dup_dir:
                return info(self, "请先点「选择…」挑一个要查找的文件夹。")
            roots = [self._dup_dir]
        elif mode == "drive":
            drive = self.cb_dup_drive.currentData()
            if not drive:
                return info(self, "请先选择要查找的磁盘。")
            roots = [drive]
        self.win.busy("正在整盘查找重复文件（可能要几分钟）…" if mode == "drive"
                      else "正在查找重复文件（文件多时需几十秒）…")
        self.tb_dup.setRowCount(0)
        self.dup_rows = []
        min_size = self.cb_dup_size.currentData()
        deep = self.ck_dup_hash.isChecked()

        def done(groups):
            self.win.idle()
            self.dup_rows = []
            t = self.tb_dup
            t.blockSignals(True)
            t.setSortingEnabled(False)
            t.setRowCount(0)
            wasted = 0
            for gi, g in enumerate(groups or []):
                # 组内按修改时间升序：第一份当"保留"（不勾），其余勾上
                items = sorted(g.get("files") or [],
                               key=lambda x: x.get("mtime") or 0)
                for k, it in enumerate(items):
                    row = {"gid": gi, "path": it["path"], "size": g["size"],
                           "mtime": it.get("mtime") or 0, "keep": (k == 0)}
                    self.dup_rows.append(row)
                    i = t.rowCount()
                    t.insertRow(i)
                    cb = QTableWidgetItem()
                    cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    cb.setCheckState(Qt.Unchecked if k == 0 else Qt.Checked)
                    cb.setData(Qt.UserRole, row)
                    if k == 0:
                        cb.setToolTip("这一份是组内最早的，默认保留")
                    t.setItem(i, 0, cb)
                    nm = SortItem(os.path.basename(it["path"]))
                    nm.setFlags(nm.flags() & ~Qt.ItemIsEditable)
                    nm.setToolTip(it["path"])
                    t.setItem(i, 1, nm)
                    p = SortItem(it["path"])
                    p.setFlags(p.flags() & ~Qt.ItemIsEditable)
                    p.setToolTip(it["path"])
                    t.setItem(i, 2, p)
                    for col, val in ((3, g["size"]), (4, int(it.get("mtime") or 0))):
                        c = SortItem(fmt_bytes(val) if col == 3
                                     else time.strftime("%Y-%m-%d %H:%M",
                                                        time.localtime(val or 0)))
                        c.setFlags(c.flags() & ~Qt.ItemIsEditable)
                        c.setData(Qt.UserRole + 1, val)
                        c.setForeground(QBrush(QColor(
                            T.get_color("WARN" if k == 0 else "ERR", self.win.dark))))
                        t.setItem(i, col, c)
                if k:                      # 只统计副本
                    wasted += g["size"]
            t.setSortingEnabled(True)
            t.setColumnWidth(1, 190)
            t.setColumnWidth(2, 380)
            t.setColumnWidth(3, 92)
            t.setColumnWidth(4, 140)
            t.blockSignals(False)
            self._dup_wasted = wasted
            self.pill_dup.setText("%d 组 / %d 个文件" % (len(groups or []), len(self.dup_rows)))
            self.btn_dup_del.setEnabled(bool(self.dup_rows))
            self._dup_sel()
            if not self.dup_rows:
                info(self, "没有找到重复文件。")

        self._dup_wasted = 0
        Tasks.run(self, lambda: S.duplicate_files_scan(
            roots=roots, min_size=min_size, deep=deep,
            should_stop=self.ctl_dup.stop_flag),
                  done, ctl=self.ctl_dup, on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败", m))[1])

    def _dup_sel(self):
        n = 0
        freed = 0
        for r in range(self.tb_dup.rowCount()):
            it = self.tb_dup.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                n += 1
                d = it.data(Qt.UserRole)
                if isinstance(d, dict):
                    freed += int(d.get("size") or 0)   # 按选中项精确求和，不猜
        self.lb_dup.setText("已选 %d 个副本 · 可释放 %s" % (n, fmt_bytes(freed))
                            if n else "未选择")

    def del_dup(self):
        picked = []
        for r in range(self.tb_dup.rowCount()):
            it = self.tb_dup.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                d = it.data(Qt.UserRole)
                if d:
                    picked.append(d)
        if not picked:
            return info(self, "没有勾选任何文件。")
        paths = [d["path"] for d in picked]
        if not confirm(self, "删除重复副本",
                       "将删除 %d 个重复文件的副本（送回收站，可还原）。\n"
                       "每组至少保留一份 —— 勾选的会被删，未勾的保留。" % len(paths),
                       danger=True):
            return
        self.win.busy("正在删除…")

        def done(r):
            self.win.idle()
            ok = [x for x in (r or []) if x.get("ok")]
            bad = [x for x in (r or []) if not x.get("ok")]
            self.scan_dup()
            msg = "已删除 %d 个副本" % len(ok)
            if bad:
                msg += "，%d 个未成功（见回收站/占用）" % len(bad)
            info(self, msg)

        Tasks.run(self, lambda: S.delete_paths(paths), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "删除失败", m))[1])

    # ---------------------------------------------------------------- 占用分析
    def _build_usage(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_usage = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_usage)
        self.ctl_usage = ScanCtl(self.btn_usage, lambda *_: self.scan_usage())
        bar.addWidget(QLabel("只显示 ≥"))
        self.cb_usage_min = QComboBox()
        self.cb_usage_min.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for label, val in (("10 MB", 10 * 1024 * 1024), ("100 MB", 100 * 1024 * 1024),
                           ("1 GB", 1024 ** 3)):
            self.cb_usage_min.addItem(label, val)
        self.cb_usage_min.setCurrentIndex(1)
        bar.addWidget(self.cb_usage_min)
        bar.addStretch(1)
        self.pill_usage = pill("未扫描", "")
        bar.addWidget(self.pill_usage)
        v.addLayout(bar)
        v.addWidget(notice(
            "一次遍历把每个文件的大小累加到它的各层上级目录，回答"
            "「<b>空间到底被谁吃了</b>」。范围：下载 / 文档 / 桌面 / 图片 / 视频 / 音乐。",
            "info"))
        f, box = card("占用 Top 目录")
        self.tb_usage = make_table(["目录", "占用", "占比", "文件数"], sortable=True)
        attach_reveal(self.tb_usage, lambda t, r: t.item(r, 0).text())
        self.tb_usage.setColumnWidth(0, 34)
        box.addWidget(self.tb_usage, 1)
        v.addWidget(f, 1)
        return w

    def scan_usage(self):
        self.win.busy("正在统计目录占用…")
        self.tb_usage.setRowCount(0)
        mn = self.cb_usage_min.currentData()

        def done(r):
            self.win.idle()
            rows, total = r or ([], 0)
            t = self.tb_usage
            t.blockSignals(True)
            t.setSortingEnabled(False)
            t.setRowCount(0)
            for x in rows:
                i = t.rowCount()
                t.insertRow(i)
                p = SortItem(x["path"])
                p.setFlags(p.flags() & ~Qt.ItemIsEditable)
                p.setToolTip(x["path"])
                t.setItem(i, 0, p)
                sz = SortItem(fmt_bytes(x["bytes"]))
                sz.setFlags(sz.flags() & ~Qt.ItemIsEditable)
                sz.setData(Qt.UserRole + 1, x["bytes"])
                t.setItem(i, 1, sz)
                pc = SortItem("%.1f%%" % x["pct"])
                pc.setFlags(pc.flags() & ~Qt.ItemIsEditable)
                pc.setData(Qt.UserRole + 1, x["pct"])
                pc.setForeground(QBrush(QColor(T.get_color("BLUE", self.win.dark))))
                t.setItem(i, 2, pc)
                fc = SortItem(str(x["files"]))
                fc.setFlags(fc.flags() & ~Qt.ItemIsEditable)
                fc.setData(Qt.UserRole + 1, x["files"])
                t.setItem(i, 3, fc)
            t.setSortingEnabled(True)
            t.setColumnWidth(0, 560)
            t.setColumnWidth(1, 100)
            t.setColumnWidth(2, 80)
            t.setColumnWidth(3, 90)
            t.blockSignals(False)
            self.pill_usage.setText("扫描范围合计 %s · Top %d" % (fmt_bytes(total), len(rows)))

        Tasks.run(self, lambda: S.folder_usage(
            top=60, min_size=mn, should_stop=self.ctl_usage.stop_flag),
                  done, ctl=self.ctl_usage,
                  on_fail=lambda m: (self.win.idle(), warn(self, "统计失败", m))[1])

    # ---------------------------------------------------------------- 整盘占用扫描
    def _build_diskscan(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("选择磁盘"))
        self.cb_drive = QComboBox()
        self.cb_drive.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.cb_drive.setMinimumWidth(280)
        self.cb_drive.currentIndexChanged.connect(self._dk_drive_changed)
        bar.addWidget(self.cb_drive)
        self.btn_dk = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_dk)
        bar.addWidget(QLabel("只显示 ≥"))
        self.cb_dk_min = QComboBox()
        self.cb_dk_min.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        # 默认「全部」——**不丢任何文件夹**，按大小排序自己看哪几个大
        for label, val in (("全部", 0), ("100 MB", 100 * 1024 * 1024),
                           ("1 GB", 1024 ** 3), ("10 GB", 10 * 1024 ** 3)):
            self.cb_dk_min.addItem(label, val)
        self.cb_dk_min.setCurrentIndex(0)
        bar.addWidget(self.cb_dk_min)
        bar.addStretch(1)
        self.pill_dk = pill("未扫描", "")
        bar.addWidget(self.pill_dk)
        v.addLayout(bar)

        # ---- 搜索（扫完/扫描中都能搜；回车即搜）----
        srow = QHBoxLayout()
        srow.addWidget(QLabel("搜索"))
        self.ed_dk = QLineEdit()
        self.ed_dk.setPlaceholderText("文件夹名，或 ≥%d MB 的文件名"
                                      % (S._BIG_FILE_MIN // 1024 // 1024))
        self.ed_dk.setMaximumWidth(340)
        self.ed_dk.returnPressed.connect(self.dk_search)
        srow.addWidget(self.ed_dk)
        srow.addWidget(btn("搜索", on_click=self.dk_search))
        srow.addWidget(btn("清除搜索", on_click=self.dk_search_clear))
        srow.addSpacing(10)
        self.lb_dk_search = QLabel("")
        self.lb_dk_search.setObjectName("Muted")
        srow.addWidget(self.lb_dk_search, 1)
        v.addLayout(srow)

        # 扫描进度单独一行：塞进右上角的药丸会被挤断（「已发现 N 个文件夹 / N 个
        # 文件 / 已扫到 X」根本放不下），而且扫描时它是用户最想看的信息。
        self.lb_dk_stat = QLabel("")
        self.lb_dk_stat.setObjectName("Muted")
        self.lb_dk_stat.setWordWrap(True)
        self.lb_dk_stat.setVisible(False)
        v.addWidget(self.lb_dk_stat)

        v.addWidget(notice(
            "直接遍历目录统计，<b>不依赖 Windows 搜索索引</b>"
            "（索引对系统盘常是关闭的，也常常没建完）。<br>"
            "<b>只列主文件夹</b>：点一行（或行首的 ▸）才展开它的子文件夹，"
            "一层一层往下看 —— 这样整盘几十万个目录也不会把界面淹掉。"
            "同级按<b>占用大小降序</b>排（树形表格不能全局排序，否则父子会错位）。<br>"
            "<b>扫描时就在长</b>：每扫到一个文件夹立刻显示，大小随扫描进度实时变大"
            "（还没扫完的数字是「目前已发现」的，扫完即为准值）。<br>"
            "<b>搜索</b>：按文件夹名找，也能按名字找 <b>≥%d MB 的大文件</b>"
            "（整盘每个文件都存下来会撑爆内存，所以只记大文件）。<br>"
            "右键菜单<b>只给 Windows 自带的项</b>（打开 / 复制 / 剪切 / 重命名 / "
            "删除到回收站 / 命令行 / 属性…）—— 不拉第三方软件塞进来的那些。<br>"
            "只读扫描，不删任何东西。中途可以点「暂停扫描」，已扫到的会留着。"
            % (S._BIG_FILE_MIN // 1024 // 1024), "info"))
        f, box = card("文件夹占用（点目录展开子级）")
        # ⚠️ 树形结构下**不能**开表头全局排序：一排序父子行就错位了。
        # 排序改为「同级按占用降序」，在 _dk_render 里自己做。
        self.tb_dk = make_table(["目录", "占用", "占整盘", "文件数"],
                                sortable=False)
        # 展开/收起：点目录名或行首的箭头
        self.tb_dk.cellClicked.connect(self._dk_toggle)
        # 「在资源管理器里定位」要用真实路径 —— col0 的文字带了缩进和箭头，
        # 所以路径单独存在 UserRole 里
        attach_reveal(self.tb_dk,
                      lambda t, r: (t.item(r, 0).data(Qt.UserRole) or "")
                      if t.item(r, 0) else "")
        # 右键菜单：整份由 native_file_menu 给（standalone=True → 不再追加通用项）
        attach_row_menu(self.tb_dk, self._dk_menu, standalone=True)
        box.addWidget(self.tb_dk, 1)
        v.addWidget(f, 1)
        # 后台线程不能碰控件：扫描时只往这个列表里塞 (path, bytes, files, depth)，
        # 由定时器每 400ms 取一批并进树索引
        self._dk_inbox = []
        self._dk_n = 0
        # 父子索引：父目录 -> {子路径: (字节, 文件数)}
        # ⚠️ 值是**字典**不是列表：扫描期间同一个目录会先被 on_live 报一次
        #    （实时大小）、再被 on_dir 报一次（最终值），列的话会出现两行同名目录。
        self._dk_tree = {}
        self._dk_flat = {}           # 全量平表（搜索用）：路径 -> (字节, 文件数)
        self._dk_files = []          # 扫描顺手记下的大文件 [(路径, 字节)]
        self._dk_query = ""          # 当前搜索词（空 = 树视图）
        self._dk_hits = []           # 搜索结果 [(路径, 字节, 文件数, 是否文件)]
        self._dk_colw = None         # 进搜索前的列宽快照（退出搜索时还原）
        self._dk_expanded = set()
        self._dk_root = ""
        self._dk_last_paint = 0.0
        self._dk_live = {"files": 0, "dirs": 0, "bytes": 0, "t0": time.time()}
        self._dk_timer = QTimer(self)
        self._dk_timer.setInterval(400)
        self._dk_timer.timeout.connect(self._dk_drain)
        self.ctl_dk = ScanCtl(self.btn_dk, self.scan_disk, resumable=True)
        self.refresh_drives()
        return w

    def _dk_menu(self, menu, table, row):
        """磁盘占用表的右键菜单：**纯 Windows 原生项**（见 native_file_menu）。"""
        it = table.item(row, 0)
        path = it.data(Qt.UserRole) if it else None
        if path:
            native_file_menu(menu, path, self)

    def _dk_cap_update(self):
        """「占整盘」的分母取**该盘总容量** —— 扫描期间一直有值，不会像
        「已扫到多少」那样刚开始是 0（那会让所有行的占比都显示 0.0%）。"""
        i = self.cb_drive.currentIndex()
        caps = getattr(self, "_dk_caps", [])
        self._dk_cap = caps[i] if 0 <= i < len(caps) else 0

    def refresh_drives(self):
        """列出所有磁盘（含移动盘，默认选第一个）。"""
        keep = self.cb_drive.currentData() if self.cb_drive.count() else None
        self.cb_drive.blockSignals(True)
        self.cb_drive.clear()
        self._dk_caps = []
        for d in S.list_drives():
            self.cb_drive.addItem("%s  %s可用 %s / 共 %s" % (
                d["root"], ("「%s」 " % d["label"]) if d["label"] else "",
                fmt_bytes(d["free"]), fmt_bytes(d["total"])), d["root"])
            self._dk_caps.append(d["total"])
        if keep:
            i = self.cb_drive.findData(keep)
            if i >= 0:
                self.cb_drive.setCurrentIndex(i)
        self.cb_drive.blockSignals(False)
        self._dk_cap_update()

    def _dk_drive_changed(self, _ix):
        """换盘：暂停中/已扫的结果都属于旧盘，直接丢掉重来。"""
        S.disk_scan_reset()
        self._dk_inbox = []
        self._dk_n = 0
        self._dk_tree = {}           # 旧盘的父子索引整棵丢掉
        self._dk_flat = {}
        self._dk_files = []
        self._dk_expanded = set()
        self._dk_root = self.cb_drive.currentData() or ""
        self._dk_clear_search(repaint=False)      # 换盘后旧搜索词没有意义了
        self._dk_cap_update()
        self.tb_dk.setRowCount(0)
        self.pill_dk.setText("未扫描")
        self._dk_stat("")
        if getattr(self, "ctl_dk", None):
            self.ctl_dk.finish(stopped=False)

    def dk_search(self, *_a):
        """按名字搜索：文件夹名，或扫描时顺手记下的**大文件**名。

        为什么文件只在 ≥ `_BIG_FILE_MIN`：整盘每个文件都存下来要几百万条
        （内存撑不住），而「哪个文件这么大」恰恰是清理磁盘时最想知道的。
        所以只留大文件，并在界面上写明这个口径 —— 别让人以为「搜不到 = 没有」。
        """
        q = (self.ed_dk.text() or "").strip()
        if not q:
            return self.dk_search_clear()
        key = q.lower()
        hits = []
        for path, (nb, fc) in self._dk_flat.items():
            if key in os.path.basename(path.rstrip("\\/")).lower():
                hits.append((path, nb, fc, False))
        for path, nb in self._dk_files:
            if key in os.path.basename(path).lower():
                hits.append((path, nb, 0, True))
        hits.sort(key=lambda x: -x[1])
        self._dk_query = key
        self._dk_hits = hits[:300]
        n_d = sum(1 for h in hits if not h[3])
        n_f = len(hits) - n_d
        more = "（只显示前 300 条，按占用降序）" if len(hits) > 300 else ""
        self._dk_search_label = ("搜索「%s」：文件夹 %d 个 · 大文件 %d 个%s"
                                 % (q, n_d, n_f, more))
        self.lb_dk_search.setText(self._dk_search_label)
        self._dk_render()
        self._dk_colw_apply(True)

    def _dk_colw_apply(self, searching):
        """搜索时把第一列加宽。

        搜索结果就是一条条**完整路径** —— 列太窄会被省略号吃掉（
        `C:\\Program ...`），那这功能等于白做。
        """
        t = self.tb_dk
        if t.columnCount() < 4:
            return
        if searching:
            if self._dk_colw is None:
                self._dk_colw = [t.columnWidth(i) for i in range(t.columnCount())]
            t.setColumnWidth(0, max(420, int(t.viewport().width() * 0.58)))
        else:
            if self._dk_colw:
                for i, wd in enumerate(self._dk_colw):
                    t.setColumnWidth(i, wd)
            self._dk_colw = None

    def dk_search_clear(self, *_a):
        """清掉搜索，回到树视图。"""
        self._dk_clear_search()

    def _dk_clear_search(self, repaint=True):
        self._dk_query = ""
        self._dk_hits = []
        self._dk_search_label = ""
        if hasattr(self, "ed_dk"):
            self.ed_dk.clear()
        if hasattr(self, "lb_dk_search"):
            self.lb_dk_search.setText("")
        self._dk_colw_apply(False)
        if repaint:
            self._dk_render()

    def _dk_row(self, path, nbytes, files, depth, flat=False):
        t = self.tb_dk
        i = t.rowCount()
        t.insertRow(i)
        kids = self._dk_tree.get(path) or {}
        if flat:
            # 搜索结果：一行就是一个绝对路径，不带箭头和缩进（点它也不展开）
            name = path
        else:
            exp = path in self._dk_expanded
            # 有子目录才给箭头；已展开用 ▾，未展开用 ▸
            mark = ("\u25be " if exp else "\u25b8 ") if kids else "\u00b7 "
            name = mark + "\u2003" * depth + (os.path.basename(path.rstrip("\\/"))
                                              or path)
        p = SortItem(name)
        p.setFlags(p.flags() & ~Qt.ItemIsEditable)
        p.setData(Qt.UserRole, path)          # 真实路径（供展开/定位用）
        tip = path
        if kids and not flat:
            tip += "\n（点击展开 %d 个子文件夹）" % len(kids)
        if S._is_protected_dir(path):
            tip += "\n系统目录：这里只统计占用，本工具不会去动它"
        p.setToolTip(tip)
        t.setItem(i, 0, p)
        sz = SortItem(fmt_bytes(nbytes))
        sz.setFlags(sz.flags() & ~Qt.ItemIsEditable)
        sz.setData(Qt.UserRole + 1, nbytes)
        t.setItem(i, 1, sz)
        pc = SortItem("%.1f%%" % (nbytes * 100.0 / self._dk_cap
                                  if self._dk_cap else 0.0))
        pc.setFlags(pc.flags() & ~Qt.ItemIsEditable)
        pc.setData(Qt.UserRole + 1, nbytes)
        pc.setForeground(QBrush(QColor(T.get_color("BLUE", self.win.dark))))
        t.setItem(i, 2, pc)
        fc = SortItem(str(files))
        fc.setFlags(fc.flags() & ~Qt.ItemIsEditable)
        fc.setData(Qt.UserRole + 1, files)
        t.setItem(i, 3, fc)

    def _dk_render(self):
        """按「当前展开到哪一层」重建可见行；有搜索词时改画搜索结果。

        只画 main 文件夹 + 已展开节点的子级 —— 整盘几十万个目录也不会淹掉界面。
        同级按占用降序（树形表格不能全局排序，否则父子行会错位）。
        """
        t = self.tb_dk
        t.blockSignals(True)
        t.setRowCount(0)
        if self._dk_query:
            for path, nb, fc, _is_file in self._dk_hits:
                self._dk_row(path, nb, fc, 0, flat=True)
            t.blockSignals(False)
            return

        def walk(parent, depth):
            kids = self._dk_tree.get(parent) or {}
            for path, (nb, fc) in sorted(kids.items(), key=lambda x: -x[1][0]):
                self._dk_row(path, nb, fc, depth)
                if path in self._dk_expanded:
                    walk(path, depth + 1)

        walk(self._dk_root, 0)
        t.blockSignals(False)

    def _dk_toggle(self, row, col):
        """点一行展开 / 收起它的子文件夹（只看 col0/col1，避免误触其它列）。"""
        if col not in (0, 1):
            return
        if self._dk_query:
            return                  # 搜索结果是平表，没有层级可展开
        it = self.tb_dk.item(row, 0)
        path = it.data(Qt.UserRole) if it else None
        if not path or not (self._dk_tree.get(path)):
            return                      # 没有子目录：点了不做事（不是错）
        if path in self._dk_expanded:
            self._dk_expanded.discard(path)
        else:
            self._dk_expanded.add(path)
        self._dk_render()

    def _dk_drain(self, final=False):
        """把后台塞进 inbox 的目录并进**父子索引**，再按需重绘。

        ⚠️ 不再是「每来一条就插一行」：树形视图只画展开到的层级，
        扫描期间通常一个都没展开 → 每次重绘就画那几个主文件夹，很便宜。

        ⚠️ 用**赋值**（字典）而不是往列表里 append：扫描期间同一个目录会先被
        `on_live` 报一次（实时大小）、扫完再被 `on_dir` 报一次（最终值）。
        列表的话会出现两行同名目录（一个数字还在长、一个是准值）。
        """
        n = len(self._dk_inbox)
        if n:
            root_key = (self._dk_root or "").rstrip("\\/")
            for path, nbytes, files, _depth in self._dk_inbox:
                if path.rstrip("\\/") == root_key:
                    continue             # 根目录本身不是一行
                par = os.path.dirname(path.rstrip("\\/")) or self._dk_root
                self._dk_tree.setdefault(par, {})[path] = (nbytes, files)
                self._dk_flat[path] = (nbytes, files)
            del self._dk_inbox[:]
            self._dk_n = len(self._dk_flat)
        # 扫描中重绘要节流：主文件夹的顺序会随实时大小变化，画太勤会闪。
        # 0.6s 是折中 —— 数字看得出在长，又不至于每帧都在重排。
        now = time.time()
        if final or now - self._dk_last_paint > 0.6:
            self._dk_last_paint = now
            self._dk_render()
            if not final:
                lv = self._dk_live
                self._dk_stat(
                    "扫描中… 已发现 %d 个文件夹 / %d 个文件 / 已扫到 %s / %d 秒"
                    % (self._dk_n, lv.get("files", 0), fmt_bytes(lv.get("bytes", 0)),
                       int(time.time() - lv.get("t0", time.time()))))

    def _dk_stat(self, text, color=None):
        """扫描进度那行（空串 = 隐藏）。"""
        if not hasattr(self, "lb_dk_stat"):
            return
        self.lb_dk_stat.setText(text or "")
        self.lb_dk_stat.setVisible(bool(text))
        if color:
            self.lb_dk_stat.setStyleSheet("color: %s;" % color)

    def scan_disk(self, resume=False):
        drive = self.cb_drive.currentData()
        if not drive:
            return info(self, "没有可扫描的磁盘", "请确认磁盘已连接。")
        mn = self.cb_dk_min.currentData() or 0
        self._dk_live = {"files": 0, "dirs": 0, "bytes": 0, "t0": time.time()}
        self._dk_root = drive            # 树的根：这个盘的根目录
        if not resume:
            S.disk_scan_reset()          # 从头扫：丢掉上次的断点
            self._dk_inbox = []
            self._dk_n = 0
            self._dk_tree = {}
            self._dk_flat = {}
            self._dk_files = []
            self._dk_expanded = set()
            self._dk_clear_search(repaint=False)
            self.tb_dk.setRowCount(0)
        self.pill_dk.setText("扫描中…")
        self._dk_stat("扫描中… 正在读目录，数字会随扫描进度变大")
        self.win.busy("正在扫描 %s 的占用…" % drive)
        self._dk_timer.start()
        ctl = self.ctl_dk

        def done(r):
            self._dk_timer.stop()
            r = r or {}
            # 大文件列表（搜索用）只有扫完才拿得到 —— 它是随扫描顺手记的
            if r.get("big"):
                self._dk_files = list(r["big"])
            self._dk_drain(final=True)
            self.win.idle()
            stopped = bool(r.get("stopped"))
            ctl.finish(stopped=stopped)
            # 渲染交给树：同级按占用降序（_dk_render 里做），**不做全局排序**
            self._dk_drain(final=True)
            if self._dk_query:
                # 搜索中：把结果按最终数据重算一遍（扫描期间的数字是实时值）
                self.dk_search()
            if stopped:
                self.pill_dk.setText("已暂停")
                self._dk_stat("已暂停 · 已发现 %d 个文件夹 · 点「继续扫描」接着扫"
                              % self._dk_n,
                              T.get_color("WARN", self.win.dark))
            elif r.get("truncated"):
                # 目前 max_dirs 没传，走不到这里；留着是为了以后真加了上限时
                # 用户能一眼看出「没扫完」，而不是以为这就是全部。
                self.pill_dk.setText("⚠ 未扫完")
                self._dk_stat("⚠ 未扫完（到了目录数上限）· 已列出 %d 个文件夹"
                              % self._dk_n)
            else:
                # 「知道全部扫完才停止」：完成态必须说明白，别让用户猜
                self.pill_dk.setText("✓ 扫描完成")
                self._dk_stat(
                    "✓ 扫描完成 · 共 %d 个文件夹 / %d 个文件 / 合计 %s / %s 秒"
                    "（点目录展开子级）"
                    % (self._dk_n, r.get("files", 0),
                       fmt_bytes(r.get("total", 0)), r.get("secs", 0)),
                    T.get_color("OK", self.win.dark))
                if not self._dk_n:
                    warn(self, "这个盘上没扫到任何文件夹",
                         "确认盘符选对了。另外系统还原点/休眠文件不属于普通文件，"
                         "不计入这里。")

        Tasks.run(self, lambda: S.disk_usage_scan(
            drive, min_size=mn, resume=resume,
            on_dir=lambda p, b, f, d: self._dk_inbox.append((p, b, f, d)),
            # 实时大小：回报的是「当前目录 + 各级祖先」这条链，每 0.4s 一次。
            # 走**同一个** inbox —— 界面那边是按键覆盖，不会多出一行。
            on_live=lambda p, b, f, d: self._dk_inbox.append((p, b, f, d)),
            on_tick=self._dk_got_tick,
            should_stop=ctl.stop_flag),
            done,
            on_fail=lambda m: (self._dk_timer.stop(), ctl.finish(),
                               self.win.idle(), warn(self, "扫描失败", m))[3])

    def _dk_got_tick(self, nf, nd, tot):
        """扫描线程经 `on_tick` 回报总量（**在工作线程里被调**，只碰普通变量）。

        只写这两个数字，不碰任何控件 —— 控件由 400ms 的 `_dk_drain` 在主线程改。
        """
        self._dk_live["files"] = nf
        self._dk_live["dirs"] = nd
        self._dk_live["bytes"] = tot

    # ---------------------------------------------------------------- 文件粉碎
    def _build_shred(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        v.addWidget(notice(
            "<b>不可恢复</b>：文件会被多轮随机数据覆写后再删除，"
            "任何恢复软件都找不回来。<br>"
            "只处理<b>单个文件</b>（目录请先自行展开）；文件需管理员才能覆写，"
            "普通权限下会如实报失败。<br>"
            "敏感文件（证书密钥、密码库等）用完请立即清理，不要留在磁盘上。", "warn"))
        bar = QHBoxLayout()
        bar.addWidget(btn("添加文件…", on_click=self.add_shred))
        bar.addWidget(btn("清空列表", on_click=self.clear_shred))
        bar.addWidget(QLabel("覆写轮数"))
        self.sp_shred = QSpinBox()
        self.sp_shred.setRange(1, 35)
        self.sp_shred.setValue(3)
        self.sp_shred.setMaximumWidth(72)
        bar.addWidget(self.sp_shred)
        bar.addStretch(1)
        self.btn_shred = btn("开始粉碎", "Danger", on_click=self.do_shred)
        self.btn_shred.setEnabled(False)
        bar.addWidget(self.btn_shred)
        v.addLayout(bar)
        f, box = card("待粉碎文件")
        self.tb_shred = make_table(["", "文件名", "完整路径", "大小", "修改时间"],
                                   sortable=True)
        attach_reveal(self.tb_shred, lambda t, r: t.item(r, 2).text())
        self.tb_shred.setColumnWidth(0, 34)
        box.addWidget(self.tb_shred, 1)
        v.addWidget(f, 1)
        return w

    def add_shred(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择要粉碎的文件")
        if not files:
            return
        t = self.tb_shred
        t.blockSignals(True)
        for p in files:
            if any(x["path"] == p for x in self.shred_rows):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            self.shred_rows.append({"path": p, "size": st.st_size, "mtime": st.st_mtime})
            i = t.rowCount()
            t.insertRow(i)
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            cb.setCheckState(Qt.Checked)
            cb.setData(Qt.UserRole, p)
            t.setItem(i, 0, cb)
            for col, val in ((1, os.path.basename(p)), (2, p),
                             (3, fmt_bytes(st.st_size)),
                             (4, time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(st.st_mtime)))):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                t.setItem(i, col, it)
        t.blockSignals(False)
        self.btn_shred.setEnabled(bool(self.shred_rows))

    def clear_shred(self):
        self.shred_rows = []
        self.tb_shred.setRowCount(0)
        self.btn_shred.setEnabled(False)

    def do_shred(self):
        paths = [self.tb_shred.item(r, 0).data(Qt.UserRole)
                 for r in range(self.tb_shred.rowCount())
                 if self.tb_shred.item(r, 0)
                 and self.tb_shred.item(r, 0).checkState() == Qt.Checked]
        paths = [p for p in paths if p]
        if not paths:
            return info(self, "没有勾选任何文件。")
        if not confirm(self, "确认粉碎（不可恢复）",
                       "即将对 %d 个文件做多轮覆写并删除，<b>无法恢复</b>。\n\n%s\n\n确认继续？"
                       % (len(paths), "\n".join("· " + os.path.basename(p) for p in paths[:10])),
                       danger=True):
            return
        self.win.busy("正在粉碎…")
        passes = self.sp_shred.value()

        def done(r):
            self.win.idle()
            ok = [x for x in (r or []) if x.get("ok")]
            bad = [x for x in (r or []) if not x.get("ok")]
            left = {p for p in paths if os.path.exists(p)}
            self.shred_rows = [x for x in self.shred_rows if x["path"] in left]
            self.clear_shred()
            msg = "已粉碎 %d 个文件" % len(ok)
            if bad:
                msg += "\n%d 个失败：\n" % len(bad) + "\n".join(
                    "· %s（%s）" % (os.path.basename(x["path"]), x["note"]) for x in bad[:8])
            info(self, msg)

        Tasks.run(self, lambda: S.shred_files(paths, passes=passes), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "粉碎失败", m))[1])

    def refresh(self):
        """进页不自动扫描（扫磁盘很慢），由用户点「开始扫描」。"""
        for t in (self.tb_empty, self.tb_dup, self.tb_usage, self.tb_shred, self.tb_dk):
            t.setRowCount(0)
        self.empty_rows, self.dup_rows, self.usage_rows = [], [], []
        self.pill_empty.setText("未扫描")
        self.pill_dup.setText("未扫描")
        self.pill_usage.setText("未扫描")
        self.pill_dk.setText("未扫描")
        self.btn_empty_del.setEnabled(False)
        self.btn_dup_del.setEnabled(False)
        self._empty_sel()
        self._dup_sel()
        self.refresh_drives()      # 期间可能插了 U 盘 / 换了盘
        if getattr(self, "cb_dup_drive", None) is not None:
            self.refresh_dup_drives()      # 重复查找的选盘同理
        # 整盘扫描的会话状态与定时器一起收掉，别留着上一次的断点
        S.disk_scan_reset()
        self._dk_inbox = []
        self._dk_n = 0
        if getattr(self, "_dk_timer", None) is not None:
            self._dk_timer.stop()
        for c in ("ctl_dk", "ctl_empty", "ctl_dup", "ctl_usage"):
            ctl = getattr(self, c, None)
            if ctl is not None:
                ctl.finish()


class RegistryToolsPage(Page):
    """注册表工具（注册表清理 / 右键菜单管理 / 备份与还原 / 快捷方式修复）。

    铁律与残留清理一致：**扫描只读**；任何删除都先导出 .reg 备份，备份失败直接拒绝；
    删除后回读复核。低置信项（weak）默认不勾。

    「备份与还原」页放**系统还原点**（系统级快照）；本工具产生的 .reg 操作备份
    挪到了设置页（那是配置存档，和系统快照不是一回事）。
    """

    title = "注册表清理"

    KIND_CN = {
        "invalid_filetype": "无效文件关联",
        "invalid_filetype_openwith": "候选打开方式（未注册）",
        "invalid_appid": "无效 AppID 引用",
        "invalid_typelib": "无效 TypeLib 引用",
        "invalid_proxystub": "无效代理存根",
        "invalid_com_server": "COM 组件文件丢失",
        "invalid_shellex": "无效菜单扩展",
        "invalid_apppath": "无效应用路径",
        "invalid_sound": "无效声音事件",
        "invalid_font": "无效字体登记",
        "invalid_shareddll": "无效共享 DLL",
        "invalid_installer": "无效安装信息",
        "invalid_service": "无效服务",
        "invalid_startup": "无效启动项",
        "invalid_autostart": "无效自启动位置",
        "invalid_com_cat": "无效组件类别",
        "invalid_protocol": "无效协议处理器",
        "invalid_interface": "无效 COM 接口",
        "invalid_history": "无效历史记录",
        "invalid_firewall": "无效防火墙规则",
        "invalid_muicache": "无效显示名缓存",
    }

    def __init__(self, win):
        super().__init__(win)
        self.issues = []
        self.cm_rows = []
        self.cm_off = []
        self.lnk_rows = []
        self.rps = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_issues(), "无效项清理")
        self.tabs.addTab(self._build_cm(), "右键菜单管理")
        self.tabs.addTab(self._build_restore(), "备份与还原")
        self.tabs.addTab(self._build_lnk(), "快捷方式修复")
        self._bk_tabs_loaded = set()
        # 切到「备份与还原」就重读一次 —— 还原点列表只跑一条 PowerShell（~1s），
        # 不重读的话用户切页签看到的可能是上次留下的空表。
        self.tabs.currentChanged.connect(self._on_tab)
        self._on_tab(0)

    def _on_tab(self, ix):
        """首次切到某个页签时才加载（扫描都不快，别在构造时全跑）。

        ⚠️ `addTab` 会**立刻**触发 `currentChanged`，早于 `__init__` 末尾 ——
        所以 `_bk_tabs_loaded` 要提前建好，且不能假设属性已存在。
        """
        loaded = getattr(self, "_bk_tabs_loaded", None)
        if loaded is None:
            loaded = self._bk_tabs_loaded = set()
        if ix == 2 and 2 not in loaded:
            loaded.add(2)
            self.scan_rp()

    def _reload_tab(self):
        """强制重扫**当前页签**。

        与「系统组件」页的同名方法一个套路：`refresh()` 走的是「首次加载」逻辑，
        **操作完成后不会重读** —— 新建/删除还原点后列表还是老样子，用户会以为
        「操作没生效」。这里先清掉已加载标记再触发。
        """
        loaded = getattr(self, "_bk_tabs_loaded", None)
        if loaded is not None:
            loaded.discard(self.tabs.currentIndex())
        self._on_tab(self.tabs.currentIndex())

    # ---------------------------------------------------------------- 无效项
    def _build_issues(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_issue_scan = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_issue_scan)
        self.ctl_issue = ScanCtl(self.btn_issue_scan,
                                 lambda *_: self.scan_issues())
        bar.addWidget(QLabel("每类最多列出"))
        self.cb_issue_per = QComboBox()
        self.cb_issue_per.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for label, val in (("200 条", 200), ("400 条", 400), ("1000 条", 1000),
                           ("不限", 0)):
            self.cb_issue_per.addItem(label, val)
        self.cb_issue_per.setCurrentIndex(1)
        self.cb_issue_per.setToolTip(
            "按类限量，避免某一类刷屏把别的类挤掉。选「不限」则不截断；"
            "只勾一个小类时也会**自动放开**上限（选了某一类就该看到它的全部）。"
            "真的触顶时，结果区会明确写出来。")
        bar.addWidget(self.cb_issue_per)
        bar.addStretch(1)
        self.pill_issue = pill("未扫描", "")
        bar.addWidget(self.pill_issue)
        v.addLayout(bar)
        # 「读没读全」的如实提示：截断了 / 枚举被上限卡住，都在这里写明。
        # （旧版把上限藏在上面那个下拉的 tooltip 里，结果区毫无提示 ——
        #   用户指着 400 条问「怎么不是全部」。）
        self.lb_issue_trunc = QLabel("")
        self.lb_issue_trunc.setObjectName("Muted")
        self.lb_issue_trunc.setWordWrap(True)
        self.lb_issue_trunc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lb_issue_trunc.setVisible(False)
        v.addWidget(self.lb_issue_trunc)

        # 扫描范围：先勾要查的小类，再扫 —— 全量一把梭既慢又会让某一类刷屏
        f0, b0 = card("扫描范围")
        g = QGridLayout()
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(6)
        self.issue_cats = {}
        for k, (cid, name, desc, dflt, risk) in enumerate(S.REG_CATEGORIES):
            low = (risk == "weak")
            cb = QCheckBox(name + ("（低置信）" if low else ""))
            cb.setChecked(bool(dflt))
            cb.setToolTip(desc + ("\n\n低置信：默认不勾，确认用途后再选" if low else ""))
            # 手动勾选也要更新计数，否则「已勾选 N / 18」会停在预设那一刻的数字
            cb.toggled.connect(lambda _v: self._issue_cats_sync())
            g.addWidget(cb, k // 2, k % 2)
            self.issue_cats[cid] = cb
        b0.addLayout(g)
        rc = QHBoxLayout()
        for label, mode in (("只勾推荐的", "recommend"), ("全选", "all"),
                            ("全不选", "none"), ("反选", "invert")):
            rc.addWidget(btn(label, on_click=lambda _=0, m=mode:
                             self.issue_cat_preset(m)))
        rc.addStretch(1)
        self.lb_issue_cat = QLabel("")
        self.lb_issue_cat.setObjectName("Muted")
        rc.addWidget(self.lb_issue_cat)
        b0.addLayout(rc)
        v.addWidget(f0)

        # 结果区：可按分类过滤，清理只作用于**当前列表里勾选**的项
        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("只显示分类"))
        # ⚠️ 用 MultiPick（多选下拉）而不是 QComboBox —— 后者天生单选，
        # 18 个分类时想同时看「COM + 文件类型」就得把其余 16 个取消选择，
        # 交互成本高得离谱。留空 = 全部。
        self.mp_issue_cat = MultiPick(self.win.dark,
                                      on_change=self._render_issues)
        bar2.addWidget(self.mp_issue_cat.btn)
        bar2.addStretch(1)
        self.lb_issue_show = QLabel("")
        self.lb_issue_show.setObjectName("Muted")
        bar2.addWidget(self.lb_issue_show)
        v.addLayout(bar2)
        v.addWidget(notice(
            "先在上面勾要查的<b>小类</b>再扫描（默认已勾推荐类；"
            "<b>反选</b>可以一次换到另一组，逐个点 18 次太累）。"
            "扫完可以<b>同时看多个分类</b> —— 「只显示分类」是可多选的下拉，"
            "留空即全部。<br>"
            "「清理勾选项」只处理<b>当前列表</b>里勾上的项 —— "
            "所以想按类清理，把过滤切到那一类、再点清理即可。<br>"
            "每一项删除前都会自动导出 <b>.reg 备份</b>；<b>备份失败就不删</b>，"
            "删完再回读复核。带「低置信」的分类是数量大或影响面广的，默认不勾。",
            "info"))
        f, box = card("无效注册表项")
        self.tb_issue = make_table(["", "分类", "类型", "位置", "值", "说明"],
                                   sortable=True)
        attach_reveal(self.tb_issue, lambda t, r: t.item(r, 3).text())
        self.tb_issue.setColumnWidth(0, 34)
        self.tb_issue.itemChanged.connect(self._on_issue_check)
        box.addWidget(self.tb_issue, 1)
        v.addWidget(f, 1)
        hb = QHBoxLayout()
        self.btn_issue_clean = btn("清理勾选项", "Danger", on_click=self.clean_issues)
        self.btn_issue_clean.setEnabled(False)
        hb.addWidget(self.btn_issue_clean)
        self.btn_issue_all = select_all_button(self.tb_issue,
                                              on_changed=self._issue_sel)
        hb.addWidget(self.btn_issue_all)
        hb.addStretch(1)
        self.lb_issue = QLabel("")
        self.lb_issue.setObjectName("Muted")
        hb.addWidget(self.lb_issue)
        v.addLayout(hb)
        self._issue_per = 400
        self._issue_stats = {}         # 上一次扫描的「读没读全」实情（server 回填）
        self._issue_auto_wide = False  # 上次扫描是否因「只勾 1 类」自动放开了上限
        self._issue_cats_sync()
        return w

    def issue_cat_preset(self, mode):
        """扫描范围的快捷勾选：只勾推荐 / 全选 / 全不选 / 反选。

        「反选」是为了配合「默认勾了 13 个推荐类」这个起点 —— 想换成另一组
        分类时，逐个点 18 次不现实；先全选再反选只要两次点击。
        """
        cur = {cid: bool(cb.isChecked()) for cid, cb in self.issue_cats.items()}
        if mode == "invert":
            want = {cid: not v for cid, v in cur.items()}
            # 反选后一个都没勾 → 扫不了，直接把「推荐」那组填回去更有用
            if not any(want.values()):
                want = {cid: bool(dflt) for cid, _n, _d, dflt, _r
                        in S.REG_CATEGORIES}
        elif mode == "all":
            want = {cid: True for cid in self.issue_cats}
        elif mode == "none":
            want = {cid: False for cid in self.issue_cats}
        else:                                          # recommend（默认）
            want = {cid: bool(dflt) for cid, _n, _d, dflt, _r
                    in S.REG_CATEGORIES}
        for cid in self.issue_cats:
            cb = self.issue_cats[cid]
            if cb:
                cb.setChecked(want[cid])
        self._issue_cats_sync()

    def _issue_cats_sync(self):
        # ⚠️ 现在由每个勾选框的 `toggled` 直接触发，而勾选框是在
        # `lb_issue_cat` **创建之前**就setChecked 的 —— 顺序上安全，
        # 但加个 getattr 兜底更稳（构造中途真被调到也不会 AttributeError）。
        n = sum(1 for cb in self.issue_cats.values() if cb.isChecked())
        lb = getattr(self, "lb_issue_cat", None)
        if lb is None:
            return
        lb.setText("已勾选 %d / %d 类" % (n, len(self.issue_cats)))
        if n == 0:
            lb.setText("⚠ 一个都没勾 —— 点「开始扫描」会提示先选几类")
        elif n == len(self.issue_cats):
            lb.setText("已勾选 全部 %d 类（会比较慢，且低置信类也在内）" % n)

    def scan_issues(self):
        picks = [cid for cid, cb in self.issue_cats.items() if cb.isChecked()]
        if not picks:
            return info(self, "没有勾选任何扫描范围",
                        "至少勾一个小类再点「开始扫描」。")
        per = self.cb_issue_per.currentData()
        per = 400 if per is None else int(per)
        # 只勾一个小类 → 自动放开上限。选了某一类就该看到它的**全部**，
        # 而不是被 400 条截掉（用户 2026-10-12 的原话就是这个）。
        # 自动放宽会在结果区写明，不是偷偷改。
        auto_wide = len(picks) == 1 and per > 0
        if auto_wide:
            per = 0
        self._issue_per = per
        self._issue_stats = {}
        self._issue_auto_wide = auto_wide
        lb_t = getattr(self, "lb_issue_trunc", None)
        if lb_t is not None:
            lb_t.setVisible(False)
        self.win.busy("正在扫描 %d 类无效注册表项…" % len(picks))
        self.tb_issue.setRowCount(0)
        self.issues = []
        self.pill_issue.setText("扫描中…")
        self._issue_cats_sync()

        stats = {}                     # 出参：回填「读没读全」的实情

        def done(r):
            self.win.idle()
            self.issues = list(r or [])
            for x in self.issues:
                x["_checked"] = not bool(x.get("weak"))
            self._issue_stats = dict(stats)
            self._fill_issue_cat()
            self._render_issues()
            self._show_issue_trunc(stats)
            if not self.issues:
                info(self, "这几类里没有发现无效项。",
                     "可以换几类再扫，或勾上低置信的分类（它们数量多、风险也高）。")

        Tasks.run(self, lambda: S.registry_issues_scan(
            kinds=picks, per_cat=per, should_stop=self.ctl_issue.stop_flag,
            stats=stats),
            done, ctl=self.ctl_issue, on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败", m))[1])

    def _show_issue_trunc(self, stats):
        """把「没读全」如实写在结果区 —— 截断了绝不能让用户以为这就是全部。

        三种要说话的情况：
          ① 有分类正好卡在「每类最多列出」上（数量只多不少）；
          ② 总数超过全局上限，后面的被丢掉了；
          ③ 枚举阶段就被上限卡住（连候选都没看完）—— 这是最隐蔽的一种。
        """
        lb = getattr(self, "lb_issue_trunc", None)
        if lb is None:
            return
        s = stats or {}
        msgs = []
        if getattr(self, "_issue_auto_wide", False):
            msgs.append("本次只勾了 1 类 → 已<b>自动放开</b>「每类最多列出」上限，"
                        "列出该类的全部条目。")
        caps = s.get("capped_cats") or {}
        if caps:
            names = "、".join("%s(%d)" % (S.REG_CAT_NAMES.get(c, c), n)
                              for c, n in list(caps.items())[:6])
            # 上限值可能为 0（不限）—— 那种情况下 server 不会报 capped，
            # 但这里也兜一手，免得印出「正好卡在上限 0」这种怪话。
            lim_txt = s.get("per_cat") if s.get("per_cat") else "不限"
            msgs.append("⚠ 以下分类<b>正好卡在上限 %s</b>，实际数量只多不少：%s —— "
                        "把「每类最多列出」设为「不限」再扫一次可看全。"
                        % (lim_txt, names))
        if s.get("truncated"):
            msgs.append("⚠ 共扫到 %s 条，超过全局上限 %s，只显示了前 %s 条 —— "
                        "建议少勾几类再扫。" % (s.get("total"), s.get("cap"),
                                             s.get("shown")))
        en = s.get("enum_trunc") or []
        if en:
            where = "、".join(str(x.get("where")) for x in en[:3])
            msgs.append("⚠ 有 %d 处枚举被读取上限卡住（如 %s…），"
                        "这些位置里可能还有没读到的项。" % (len(en), where))
        if msgs:
            lb.setText("<br>".join(msgs))
            lb.setStyleSheet("color: %s;" % T.get_color("WARN", self.win.dark))
            lb.setVisible(True)
        else:
            lb.setVisible(False)

    def _fill_issue_cat(self):
        """按本次结果重建「分类」多选下拉（带各类条数）。

        ⚠️ `registry_issue_stats` 返回的是
        `(分类 id, 中文名, 条数, 其中可勾选条数)` —— 第 4 项是**可勾选数**
        （低置信项默认不勾），不是描述文案，别拿它当副文本。
        """
        self.mp_issue_cat.set_items(
            [(None, "全部", "不过滤（= 不勾任何具体分类）")]
            + [(cid, "%s（%d）" % (name, cnt),
                "%s\n共 %d 项，其中 %d 项默认已勾选（低置信项需自行确认）"
                % (S.REG_CAT_DESC.get(cid, ""), cnt, strong))
               for cid, name, cnt, strong in S.registry_issue_stats(self.issues)])

    def _render_issues(self):
        # MultiPick 语义：**空集合 = 全部**（与「不勾任何一项」等价）
        want = self.mp_issue_cat.picked()
        t = self.tb_issue
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.setRowCount(0)
        shown = 0
        for idx, x in enumerate(self.issues):
            if want and (x.get("cat") or "") not in want:
                continue
            weak = bool(x.get("weak"))
            i = t.rowCount()
            t.insertRow(i)
            # ⚠️ UserRole 只存**下标整数**：SortItem 排序后行号会变，
            # 存 dict 取回来还是拷贝（改不动原数据），状态会静默丢失。
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked if x.get("_checked") else Qt.Unchecked)
            chk.setData(Qt.UserRole, idx)
            chk.setToolTip("低置信项：默认不勾，确认它确实没用了再勾"
                           if weak else "")
            t.setItem(i, 0, chk)
            loc = "%s%s" % (x["hive"], x["path"])
            for col, val in ((1, S.REG_CAT_NAMES.get(x.get("cat"), x.get("cat") or "")),
                             (2, self.KIND_CN.get(x["kind"], x["kind"])),
                             (3, loc), (4, x.get("value") or "(默认)"),
                             (5, x.get("why") or "")):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                if col == 1 and weak:
                    it.setForeground(QBrush(QColor(T.get_color("WARN", self.win.dark))))
                t.setItem(i, col, it)
            shown += 1
        t.setSortingEnabled(True)
        for col, wd in ((0, 34), (1, 160), (2, 130), (3, 320), (4, 140), (5, 360)):
            t.setColumnWidth(col, wd)
        t.blockSignals(False)
        # ⚠️ 别用 `cnt >= self._issue_per` 判「触顶」—— 选「不限」时 _issue_per=0，
        #    那样每个分类都会被误判成触顶。触顶实情由 server 的 stats 给出。
        caps = (getattr(self, "_issue_stats", {}) or {}).get("capped_cats") or {}
        over = [S.REG_CAT_NAMES.get(c, c) for c in caps]
        self.pill_issue.setText(
            "共 %d 项" % len(self.issues) +
            (" · %s 已达单类上限" % "、".join(over[:2]) if over else ""))
        self.lb_issue_show.setText("当前显示 %d 项%s"
                                   % (shown, "（已按分类过滤）" if want else ""))
        self.btn_issue_clean.setEnabled(bool(shown))
        if hasattr(self, "btn_issue_all"):
            self.btn_issue_all._refresh()
        self._issue_sel()

    def _on_issue_check(self, item):
        """勾选状态回写到 self.issues —— 筛选/排序重建表格后不会丢。"""
        if item.column() != 0:
            return
        idx = item.data(Qt.UserRole)
        if isinstance(idx, int) and 0 <= idx < len(self.issues):
            self.issues[idx]["_checked"] = (item.checkState() == Qt.Checked)
        self._issue_sel()

    def _issue_sel(self):
        if not hasattr(self, "lb_issue"):
            return
        n = 0
        for r in range(self.tb_issue.rowCount()):
            it = self.tb_issue.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                n += 1
        self.lb_issue.setText("当前列表已选 %d 项（每项都会先备份）" % n if n
                              else "当前列表未选择")

    def clean_issues(self):
        sel = []
        for r in range(self.tb_issue.rowCount()):
            it = self.tb_issue.item(r, 0)
            if not it or it.checkState() != Qt.Checked:
                continue
            idx = it.data(Qt.UserRole)
            if isinstance(idx, int) and 0 <= idx < len(self.issues):
                sel.append(self.issues[idx])
        if not sel:
            return info(self, "没有勾选任何项。")
        by = {}
        for x in sel:
            nm = S.REG_CAT_NAMES.get(x.get("cat"), "其它")
            by[nm] = by.get(nm, 0) + 1
        detail = "\n".join("· %s：%d 项" % (k, n)
                           for k, n in sorted(by.items(), key=lambda p: -p[1]))
        low = [x for x in sel if x.get("weak")]
        extra = ""
        if low:
            extra = ("\n\n⚠️ 其中 %d 项属于<b>低置信</b>项（本来默认没勾）—— "
                     "删掉可能影响仍在使用的程序，请确认清楚。" % len(low))
        if not confirm(self, "清理无效注册表项",
                       "将处理 %d 项，按分类：\n%s\n\n"
                       "每一项都会先导出 .reg 备份到 backup 目录，"
                       "<b>备份失败则不删除</b>，删完还会回读复核。%s"
                       % (len(sel), detail, extra), danger=True):
            return
        self.win.busy("正在清理（逐项备份并复核）…")

        def done(r):
            self.win.idle()
            res = r or []
            ok = [x for x in res if x.get("ok")]
            bad = [x for x in res if not x.get("ok")]
            self.scan_issues()
            msg = "已清理 %d 项" % len(ok)
            if bad:
                msg += "，%d 项未处理：\n" % len(bad) + "\n".join(
                    "· %s（%s）" % ((x.get("why") or "")[:40], x.get("note"))
                    for x in bad[:8])
            info(self, msg + "\n\n备份可在「备份还原中心」一键还原。")

        Tasks.run(self, lambda: S.registry_issue_clean(sel), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "清理失败", m))[1])
    # ---------------------------------------------------------------- 右键菜单
    def _build_cm(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_cm = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_cm)
        self.ctl_cm = ScanCtl(self.btn_cm, lambda *_: self.scan_cm())
        self.ck_cm_sys = QCheckBox("显示系统自带项")
        bar.addWidget(self.ck_cm_sys)
        bar.addStretch(1)
        self.pill_cm = pill("未扫描", "")
        bar.addWidget(self.pill_cm)
        v.addLayout(bar)
        v.addWidget(notice(
            "禁用<b>不是删除</b>：整棵子树会先搬到 <code>HKCU\\Software\\WinToolbox\\"
            "DisabledContextMenu</code> 备份区并记录到 <code>backup\\context_menu.json</code>，"
            "随时可在下面「已禁用」区一键还原。<br>"
            "系统自带项默认不显示 —— 禁用它们没有意义。", "info"))
        f, box = card("已启用的右键菜单项")
        self.tb_cm = make_table(["", "范围", "名称", "显示名", "处理器 CLSID"], sortable=True)
        self.tb_cm.setColumnWidth(0, 34)
        self.tb_cm.itemChanged.connect(lambda *_: self._cm_sel())
        box.addWidget(self.tb_cm, 1)
        v.addWidget(f, 1)
        hb = QHBoxLayout()
        self.btn_cm_off = btn("禁用勾选项", "Danger", on_click=self.disable_cm)
        self.btn_cm_off.setEnabled(False)
        hb.addWidget(self.btn_cm_off)
        hb.addWidget(select_all_button(self.tb_cm, on_changed=self._cm_sel))
        hb.addStretch(1)
        self.lb_cm = QLabel("")
        self.lb_cm.setObjectName("Muted")
        hb.addWidget(self.lb_cm)
        v.addLayout(hb)
        f2, box2 = card("已禁用（可还原）")
        self.tb_cm_off = make_table(["", "范围", "名称", "显示名"], sortable=False)
        self.tb_cm_off.setColumnWidth(0, 34)
        self.tb_cm_off.itemChanged.connect(lambda *_: self._cm_off_sel())
        box2.addWidget(self.tb_cm_off)
        hb2 = QHBoxLayout()
        self.btn_cm_on = btn("还原勾选项", "Primary", on_click=self.enable_cm)
        self.btn_cm_on.setEnabled(False)
        hb2.addWidget(self.btn_cm_on)
        hb2.addWidget(btn("刷新已禁用列表", on_click=self.load_cm_off))
        self.lb_cm_off = QLabel("")
        self.lb_cm_off.setObjectName("Muted")
        hb2.addWidget(self.lb_cm_off)
        hb2.addStretch(1)
        v.addWidget(f2, 1)
        v.addLayout(hb2)
        return w

    def scan_cm(self):
        self.win.busy("正在枚举右键菜单项…")
        show_sys = self.ck_cm_sys.isChecked()

        def done(r):
            self.win.idle()
            self.cm_rows = r or []
            self._fill_cm()
            self.load_cm_off()
            self.pill_cm.setText("已启用 %d 项" % len(self.cm_rows))
            if not self.cm_rows:
                info(self, "没有找到第三方右键菜单项。")

        Tasks.run(self, lambda: S.context_menu_list(show_system=show_sys), done,
                  ctl=self.ctl_cm,
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败", m))[1])

    def _fill_cm(self):
        t = self.tb_cm
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for x in self.cm_rows:
            i = t.rowCount()
            t.insertRow(i)
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            cb.setCheckState(Qt.Unchecked)
            cb.setData(Qt.UserRole, x)
            t.setItem(i, 0, cb)
            for col, val in ((1, x["scope"]), (2, x["name"]),
                             (3, x.get("display") or x["name"]),
                             (4, x.get("clsid") or "")):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                if col == 4 and x.get("system"):
                    it.setForeground(QBrush(QColor(T.get_color("TEXT3", self.win.dark))))
                t.setItem(i, col, it)
        t.setSortingEnabled(True)
        t.setColumnWidth(1, 92)
        t.setColumnWidth(2, 220)
        t.setColumnWidth(3, 240)
        t.setColumnWidth(4, 300)
        t.blockSignals(False)
        self._cm_sel()

    def _cm_sel(self):
        if not hasattr(self, "lb_cm"):
            return
        n = sum(1 for r in range(self.tb_cm.rowCount())
                if self.tb_cm.item(r, 0) and self.tb_cm.item(r, 0).checkState() == Qt.Checked)
        self.lb_cm.setText("已选 %d 项" % n if n else "未选择")
        self.btn_cm_off.setEnabled(n > 0)

    def _cm_off_sel(self):
        n = sum(1 for r in range(self.tb_cm_off.rowCount())
                if self.tb_cm_off.item(r, 0) and self.tb_cm_off.item(r, 0).checkState() == Qt.Checked)
        if hasattr(self, "btn_cm_on"):
            self.btn_cm_on.setEnabled(n > 0)

    def load_cm_off(self):
        def done(r):
            self.cm_off = r or []
            t = self.tb_cm_off
            t.blockSignals(True)
            t.setRowCount(0)
            for x in self.cm_off:
                i = t.rowCount()
                t.insertRow(i)
                cb = QTableWidgetItem()
                cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                cb.setCheckState(Qt.Unchecked)
                cb.setData(Qt.UserRole, "%s|%s" % (x["scope"], x["name"]))
                t.setItem(i, 0, cb)
                for col, val in ((1, x["scope"]), (2, x["name"]), (3, x["display"])):
                    it = SortItem(str(val))
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    t.setItem(i, col, it)
            t.blockSignals(False)
            self._cm_off_sel()

        Tasks.run(self, S.context_menu_disabled_list, done,
                  on_fail=lambda m: self.load_fail(
                      self.lb_cm_off, "已禁用项列表", m))

    def disable_cm(self):
        sel = [self.tb_cm.item(r, 0).data(Qt.UserRole)
               for r in range(self.tb_cm.rowCount())
               if self.tb_cm.item(r, 0) and self.tb_cm.item(r, 0).checkState() == Qt.Checked]
        sel = [x for x in sel if x]
        if not sel:
            return info(self, "没有勾选任何项。")
        if not confirm(self, "禁用右键菜单项",
                       "将禁用 %d 个右键菜单项：\n%s\n\n"
                       "只是搬到备份区（可一键还原），不会删除任何程序。"
                       % (len(sel), "\n".join("· " + x["name"] for x in sel[:12])), danger=True):
            return
        self.win.busy("正在禁用…")

        def done(r):
            self.win.idle()
            res = r or []
            ok = [x for x in res if x.get("ok")]
            self.scan_cm()
            info(self, "已禁用 %d 项%s" % (len(ok),
                                          "，%d 项失败" % (len(res) - len(ok)) if len(res) > len(ok) else ""))

        Tasks.run(self, lambda: S.context_menu_disable(sel), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "禁用失败", m))[1])

    def enable_cm(self):
        keys = [self.tb_cm_off.item(r, 0).data(Qt.UserRole)
                for r in range(self.tb_cm_off.rowCount())
                if self.tb_cm_off.item(r, 0) and self.tb_cm_off.item(r, 0).checkState() == Qt.Checked]
        keys = [k for k in keys if k]
        if not keys:
            return info(self, "没有勾选要还原的项。")
        self.win.busy("正在还原…")

        def done(r):
            self.win.idle()
            res = r or []
            ok = [x for x in res if x.get("ok")]
            self.scan_cm()
            info(self, "已还原 %d 项%s" % (len(ok),
                                          "，%d 项失败" % (len(res) - len(ok)) if len(res) > len(ok) else ""))

        Tasks.run(self, lambda: S.context_menu_enable(keys), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "还原失败", m))[1])

    # -------------------------------------------------- 系统还原点（备份还原）
    def _build_restore(self):
        """系统还原点列表。

        为什么在这里而不是「系统组件」页：还原点**就是**系统级的备份/还原手段，
        放在「备份还原」语义最贴；操作备份（.reg 文件）挪去了设置页 ——
        两者一个是「系统快照」，一个是「配置存档」，不是一回事。
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_rp_refresh = btn("刷新列表", "Primary", on_click=self.scan_rp)
        bar.addWidget(self.btn_rp_refresh)
        bar.addWidget(btn("新建还原点", on_click=self.make_rp))
        bar.addStretch(1)
        self.pill_rp = pill("未扫描", self.win)
        bar.addWidget(self.pill_rp)
        v.addLayout(bar)
        v.addWidget(notice(
            "读取与新建都需要<b>管理员权限</b>，且系统保护必须已开启"
            "（Win10/11 默认关闭，<code>系统属性 → 系统 → 系统保护</code> 里打开）。<br>"
            "还原点默认<b>不勾选</b> —— 删除不可撤销，且最早的那个不允许删。<br>"
            "本工具产生的注册表 <code>.reg</code> 操作备份不在这儿，已移到<b>设置页</b>。",
            "info"))
        f, box = card("系统还原点")
        self.tb_rp = make_table(["", "描述", "类型", "创建时间"], sortable=True)
        self.tb_rp.setColumnWidth(0, 34)
        self.tb_rp.setColumnWidth(1, 420)
        self.tb_rp.setColumnWidth(2, 72)
        box.addWidget(self.tb_rp, 1)
        v.addWidget(f, 1)
        hb = QHBoxLayout()
        self.lb_rp = QLabel("还原点默认不勾选 —— 删除不可撤销，且不能删最早的那个")
        self.lb_rp.setObjectName("Muted")
        hb.addWidget(self.lb_rp)
        hb.addStretch(1)
        self.btn_rp_del = btn("删除所选", "Danger", on_click=self.del_rp)
        self.btn_rp_del.setEnabled(False)
        hb.addWidget(self.btn_rp_del)
        hb.addWidget(select_all_button(self.tb_rp, on_changed=self._rp_sel))
        v.addLayout(hb)
        return w

    def scan_rp(self):
        self.pill_rp.setText("读取中…")
        Tasks.run(self, S.list_restore_points, self._got_rp,
                  on_fail=lambda m: self.pill_rp.setText("失败：%s" % m[:28]))

    def _got_rp(self, rows):
        self.rps = rows or []
        self.tb_rp.setSortingEnabled(False)
        self.tb_rp.setRowCount(0)
        for p in self.rps:
            i = self.tb_rp.rowCount()
            self.tb_rp.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Unchecked)      # 删除不可撤销 → 默认全不勾
            chk.setData(Qt.UserRole, p["seq"])
            self.tb_rp.setItem(i, 0, chk)
            for col, val in ((1, p.get("desc") or ""), (2, p.get("type") or ""),
                             (3, p.get("raw_date") or "")):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                self.tb_rp.setItem(i, col, it)
            self.tb_rp.setRowHeight(i, 28)
        self.tb_rp.setSortingEnabled(True)
        if self.rps:
            self.pill_rp.setText("%d 个还原点" % len(self.rps))
            self.pill_rp.setObjectName("Pill")
        elif not self.win.admin_mode:
            # 非管理员下 Get-ComputerRestorePoint 返回 Access denied，服务端拿不到
            # 结果就返回空 —— 直接显示「0 个还原点」是误导（其实是有、但读不到）。
            self.pill_rp.setText("需要管理员权限才能读取")
            self.pill_rp.setObjectName("PillWarn")
        else:
            self.pill_rp.setText("未发现还原点（系统保护可能未开启）")
            self.pill_rp.setObjectName("PillWarn")
        self.pill_rp.style().unpolish(self.pill_rp)
        self.pill_rp.style().polish(self.pill_rp)
        self._rp_sel()

    def _rp_sel(self):
        if not hasattr(self, "btn_rp_del"):
            return
        n = sum(1 for r in range(self.tb_rp.rowCount())
                if self.tb_rp.item(r, 0) and self.tb_rp.item(r, 0).checkState() == Qt.Checked)
        self.lb_rp.setText("已选 %d 个（删除不可撤销）" % n if n
                           else "还原点默认不勾选 —— 删除不可撤销，且不能删最早的那个")
        self.btn_rp_del.setEnabled(n > 0)

    def make_rp(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not confirm(self, "新建还原点",
                       "即将创建一个系统还原点。\n\n"
                       "· 需要管理员权限\n· 需要系统保护已开启（默认关闭）\n"
                       "· 创建前会提示「正在创建还原点，请稍候」", danger=False):
            return
        self.win.busy("正在创建还原点…")
        Tasks.run(self, S.create_restore_point, self._rp_done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "创建失败：" + m))[1])

    def del_rp(self):
        seqs = [it.data(Qt.UserRole) for it in
                (self.tb_rp.item(r, 0) for r in range(self.tb_rp.rowCount()))
                if it is not None and it.checkState() == Qt.Checked]
        if not seqs:
            return info(self, "请先勾选要删除的还原点。")
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not confirm(self, "确认删除还原点",
                       "即将删除 %d 个还原点：\n\n%s\n\n"
                       "删除后无法恢复到那个时间点，系统还原功能本身仍保留。"
                       % (len(seqs), "\n".join("· 序列号 " + str(s) for s in seqs[:8])),
                       danger=True):
            return
        self.win.busy("正在删除 %d 个还原点…" % len(seqs))
        Tasks.run(self, lambda: [S.remove_restore_point(s) for s in seqs],
                  lambda r: (self.win.idle(), self._rp_done(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "删除失败：" + m))[1])

    def _rp_done(self, r):
        self.win.idle()
        r = r if isinstance(r, dict) else {"ok": bool(r), "msg": str(r)}
        # 强制重读：新建/删除后必须看到最新的列表，否则用户会以为「操作没生效」
        self.scan_rp()
        info(self, "还原点操作完成", r.get("msg") or "")

    # ---------------------------------------------------------------- 快捷方式
    def _build_lnk(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        bar = QHBoxLayout()
        self.btn_lnk = btn("开始扫描", "Primary")
        bar.addWidget(self.btn_lnk)
        self.ctl_lnk = ScanCtl(self.btn_lnk, lambda *_: self.scan_lnk())
        bar.addStretch(1)
        self.pill_lnk = pill("未扫描", "")
        bar.addWidget(self.pill_lnk)
        v.addLayout(bar)
        v.addWidget(notice(
            "范围：桌面 + 开始菜单（当前用户 + 全部用户）。<br>"
            "<b>修复</b>：在已装程序的安装目录与 Program Files 里找<b>同名</b>可执行文件，"
            "找到就重建快捷方式指向它（适合程序挪了目录 / 升级后路径变了）；"
            "找不到会如实报告，不会乱指。", "info"))
        f, box = card("快捷方式")
        self.tb_lnk = make_table(["", "快捷方式", "目标", "状态"], sortable=True)
        attach_reveal(self.tb_lnk, lambda t, r: t.item(r, 1).text())
        self.tb_lnk.setColumnWidth(0, 34)
        self.tb_lnk.itemChanged.connect(lambda *_: self._lnk_sel())
        box.addWidget(self.tb_lnk, 1)
        v.addWidget(f, 1)
        hb = QHBoxLayout()
        self.btn_lnk_fix = btn("修复勾选（找新目标并重建）", "Primary", on_click=self.fix_lnk)
        self.btn_lnk_fix.setEnabled(False)
        hb.addWidget(self.btn_lnk_fix)
        self.btn_lnk_del = btn("删除勾选的快捷方式", "Danger", on_click=self.del_lnk)
        self.btn_lnk_del.setEnabled(False)
        hb.addWidget(self.btn_lnk_del)
        hb.addStretch(1)
        self.lb_lnk = QLabel("")
        self.lb_lnk.setObjectName("Muted")
        hb.addWidget(self.lb_lnk)
        v.addLayout(hb)
        return w

    def scan_lnk(self):
        self.win.busy("正在扫描快捷方式…")
        self.tb_lnk.setRowCount(0)
        self.lnk_rows = []

        def done(r):
            self.win.idle()
            self.lnk_rows = r or []
            t = self.tb_lnk
            t.blockSignals(True)
            t.setSortingEnabled(False)
            t.setRowCount(0)
            for x in self.lnk_rows:
                i = t.rowCount()
                t.insertRow(i)
                bad = not x.get("ok")
                cb = QTableWidgetItem()
                cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                cb.setCheckState(Qt.Unchecked if x.get("ok") else Qt.Checked)
                cb.setData(Qt.UserRole, x)
                t.setItem(i, 0, cb)
                for col, val in ((1, x["path"]), (2, x["target"]),
                                 (3, "失效" if bad else "正常")):
                    it = SortItem(str(val))
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    it.setToolTip(str(val))
                    if col == 3:
                        it.setForeground(QBrush(QColor(T.get_color(
                            "ERR" if bad else "OK", self.win.dark))))
                    t.setItem(i, col, it)
            t.setSortingEnabled(True)
            t.setColumnWidth(1, 420)
            t.setColumnWidth(2, 420)
            t.setColumnWidth(3, 70)
            t.blockSignals(False)
            n_bad = len([x for x in self.lnk_rows if not x.get("ok")])
            self.pill_lnk.setText("%d 个 / 失效 %d" % (len(self.lnk_rows), n_bad))
            self._lnk_sel()
            if not n_bad:
                info(self, "没有失效的快捷方式。")

        Tasks.run(self, S.shortcut_scan, done, ctl=self.ctl_lnk,
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败", m))[1])

    def _lnk_sel(self):
        if not hasattr(self, "lb_lnk"):
            return
        sel = [self.tb_lnk.item(r, 0).data(Qt.UserRole)
               for r in range(self.tb_lnk.rowCount())
               if self.tb_lnk.item(r, 0) and self.tb_lnk.item(r, 0).checkState() == Qt.Checked]
        sel = [x for x in sel if x]
        n_bad = len([x for x in sel if not x.get("ok")])
        self.lb_lnk.setText("已选 %d 个（其中失效 %d）" % (len(sel), n_bad) if sel else "未选择")
        self.btn_lnk_fix.setEnabled(n_bad > 0)
        self.btn_lnk_del.setEnabled(bool(sel))

    def fix_lnk(self):
        sel = [self.tb_lnk.item(r, 0).data(Qt.UserRole)
               for r in range(self.tb_lnk.rowCount())
               if self.tb_lnk.item(r, 0)
               and self.tb_lnk.item(r, 0).checkState() == Qt.Checked]
        sel = [x for x in sel if x and not x.get("ok")]
        if not sel:
            return info(self, "没有勾选失效的快捷方式。")
        self.win.busy("正在查找新目标并重建…")

        def done(r):
            self.win.idle()
            self.scan_lnk()
            info(self, (r or {}).get("msg") or "完成")

        Tasks.run(self, lambda: S.shortcut_rebuild(sel), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "修复失败", m))[1])

    def del_lnk(self):
        paths = [self.tb_lnk.item(r, 0).data(Qt.UserRole) for r in range(self.tb_lnk.rowCount())
                 if self.tb_lnk.item(r, 0) and self.tb_lnk.item(r, 0).checkState() == Qt.Checked]
        paths = [x["path"] for x in paths if x]
        if not paths:
            return info(self, "没有勾选任何项。")
        if not confirm(self, "删除快捷方式",
                       "将删除 %d 个快捷方式（送回收站，可还原）。\n不会删除它指向的程序。"
                       % len(paths), danger=True):
            return
        self.win.busy("正在删除…")

        def done(r):
            self.win.idle()
            res = r or []
            self.scan_lnk()
            info(self, "已删除 %d 个" % len([x for x in res if x.get("ok")]))

        Tasks.run(self, lambda: S.delete_paths(paths), done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "删除失败", m))[1])

    def refresh(self):
        """进页不自动扫（枚举注册表 / 快捷方式都偏慢），由用户点按钮。"""
        for t in (self.tb_issue, self.tb_cm, self.tb_cm_off, self.tb_rp, self.tb_lnk):
            t.setRowCount(0)
        self.issues, self.cm_rows, self.lnk_rows, self.rps = [], [], [], []
        if hasattr(self, "mp_issue_cat"):        # 分类筛选跟着旧结果清空
            self.mp_issue_cat.clear()
        for lb in ("lb_issue", "lb_issue_show"):
            obj = getattr(self, lb, None)
            if obj is not None:
                obj.setText("")
        for pill_obj, txt in ((getattr(self, "pill_issue", None), "未扫描"),
                              (getattr(self, "pill_cm", None), "未扫描"),
                              (getattr(self, "pill_rp", None), "未扫描"),
                              (getattr(self, "pill_lnk", None), "未扫描")):
            if pill_obj:
                pill_obj.setText(txt)
        if hasattr(self, "btn_issue_clean"):
            self.btn_issue_clean.setEnabled(False)
        if hasattr(self, "btn_cm_off"):
            self.btn_cm_off.setEnabled(False)
        if hasattr(self, "btn_lnk_fix"):
            self.btn_lnk_fix.setEnabled(False)
            self.btn_lnk_del.setEnabled(False)
        if hasattr(self, "btn_rp_del"):
            self.btn_rp_del.setEnabled(False)
        for f in (self._issue_sel, self._cm_sel, self._rp_sel, self._lnk_sel):
            try:
                f()
            except RuntimeError:
                pass
        # 还原点列表**必须在这里重载**：上面把 tb_rp 清空了，而这一类页签
        # （无效项 / 右键菜单 / 快捷方式）都要用户点按钮才开始扫，只有还原点
        # 读取是自动的 —— 不重读的话用户切进来看到空列表，以为「读取不到」。
        # 代价是每次进页跑一条 PowerShell（约 1s），换来「进来就有内容」。
        if hasattr(self, "_bk_tabs_loaded"):
            self._bk_tabs_loaded.discard(2)
        self._on_tab(self.tabs.currentIndex())


# ==========================================================================
# 10. 系统组件（程序组件管理器 / 浏览器扩展 / 文件校验）
# ==========================================================================
class SysToolsPage(Page):
    """3 个页签：程序组件(COM) / 浏览器扩展 / 文件校验。

    统一「只读扫描 → 勾选 → 确认 → 执行」。组件禁用是写操作，走二次确认，
    且搬到影子区（可一键还原）而非删除。

    系统还原点已迁到「注册表清理 → 备份与还原」（那里才是备份/还原语义）。
    """
    title = "系统组件"

    def __init__(self, win):
        super().__init__(win)
        self.comp = []
        self.exts = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        t = T.get_color("TEXT", win.dark)
        dim = T.get_color("TEXT3", win.dark)
        root.addWidget(QLabel("程序组件、浏览器扩展与文件签名校验。"
                              "扫描全部只读；禁用组件会先备份到 HKCU 影子区，可一键还原。"))

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(lambda _i: self._on_tab())
        root.addWidget(self.tabs, 1)

        # ---------- 页签 1：程序组件 ----------
        w1 = QWidget(); v1 = QVBoxLayout(w1)
        v1.setContentsMargins(0, 10, 0, 0); v1.setSpacing(10)
        r1 = QHBoxLayout()
        self.cb_comp_sys = QCheckBox("显示系统自带组件（数千个，不建议禁用）")
        self.cb_comp_sys.toggled.connect(lambda *_: self._fill_comp())
        r1.addWidget(self.cb_comp_sys)
        r1.addStretch(1)
        self.pill_comp = pill("未扫描", self.win)
        r1.addWidget(self.pill_comp)
        self.btn_comp = btn("扫描组件")
        r1.addWidget(self.btn_comp)
        self.ctl_comp = ScanCtl(self.btn_comp, lambda *_: self.scan_comp())
        v1.addLayout(r1)
        self.tb_comp = make_table(["", "名称", "类型", "实现文件"], sortable=True)
        attach_reveal(self.tb_comp, lambda t, r: t.item(r, 3).text())
        self.tb_comp.setColumnWidth(0, 34)
        self.tb_comp.setColumnWidth(1, 230)
        self.tb_comp.setColumnWidth(2, 62)
        self.tb_comp.itemChanged.connect(self._on_comp_item_changed)
        v1.addWidget(self.tb_comp, 1)
        h1 = QHBoxLayout()
        self.lb_comp = QLabel("未选择任何项")
        self.lb_comp.setObjectName("Muted")
        h1.addWidget(self.lb_comp)
        h1.addStretch(1)
        h1.addWidget(btn("全选可禁用项", on_click=lambda: self._check_comp(True)))
        h1.addWidget(btn("全不选", on_click=lambda: self._check_comp(False)))
        h1.addWidget(btn("禁用所选", "Danger", on_click=self.disable_comp))
        h1.addWidget(btn("还原所选", on_click=self.enable_comp))
        v1.addLayout(h1)
        v1.addWidget(notice("系统自带组件默认不显示，第三方组件的「实现文件」指向已"
                            "不存在的 DLL/EXE 时最该清；禁用只是把注册项搬到影子区，"
                            "不会卸载程序本身。", "info"))
        self.tabs.addTab(w1, "程序组件")

        # ---------- 页签 2：浏览器扩展 ----------
        w2 = QWidget(); v2 = QVBoxLayout(w2)
        v2.setContentsMargins(0, 10, 0, 0); v2.setSpacing(10)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("只读列出各浏览器已安装的扩展与权限（不代你改浏览器设置）"))
        r2.addStretch(1)
        self.pill_ext = pill("未扫描", self.win)
        r2.addWidget(self.pill_ext)
        self.btn_ext = btn("扫描扩展")
        r2.addWidget(self.btn_ext)
        self.ctl_ext = ScanCtl(self.btn_ext, lambda *_: self.scan_ext())
        v2.addLayout(r2)
        self.tb_ext = make_table(["", "浏览器", "名称", "版本", "状态", "大小", "权限"],
                                 sortable=True)
        self.tb_ext.setColumnWidth(0, 34)
        self.tb_ext.setColumnWidth(1, 76)
        self.tb_ext.setColumnWidth(2, 190)
        self.tb_ext.setColumnWidth(3, 78)
        self.tb_ext.setColumnWidth(4, 66)
        self.tb_ext.setColumnWidth(5, 78)
        self.tb_ext.setColumnWidth(6, 220)
        v2.addWidget(self.tb_ext, 1)
        h2 = QHBoxLayout()
        self.lb_ext = QLabel("双击一行可打开它的安装目录")
        self.lb_ext.setObjectName("Muted")
        h2.addWidget(self.lb_ext)
        h2.addStretch(1)
        h2.addWidget(btn("打开所在目录", on_click=self.open_ext_dir))
        v2.addLayout(h2)
        v2.addWidget(notice("Chromium 系浏览器的扩展状态存在带哈希校验的配置里，"
                            "外部改动会被浏览器立刻还原；因此这里只提供清单与定位，"
                            "停用请在浏览器扩展页里操作。", "info"))
        self.tabs.addTab(w2, "浏览器扩展")

        # ---------- 页签 3：文件校验 ----------
        # （原页签 3「系统还原点」已迁到「注册表清理 → 备份与还原」——
        #   还原点是系统级快照，和注册表备份放一起语义更对；
        #   本页签从 4 个减为 3 个，后面所有索引都要跟着变。）
        w4 = QWidget(); v4 = QVBoxLayout(w4)
        v4.setContentsMargins(0, 10, 0, 0); v4.setSpacing(10)
        r4 = QHBoxLayout()
        r4.addWidget(btn("添加文件…", on_click=self.add_files))
        r4.addWidget(btn("添加当前选中的文件", on_click=self.add_cur_exe))
        r4.addWidget(btn("清空", on_click=self._clear_files))
        r4.addStretch(1)
        self.cb_algo = QComboBox()
        self.cb_algo.addItem("SHA-256", "sha256")
        self.cb_algo.addItem("SHA-1", "sha1")
        self.cb_algo.addItem("MD5", "md5")
        self.cb_algo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        r4.addWidget(self.cb_algo)
        self.pill_fv = pill("未校验", self.win)
        r4.addWidget(self.pill_fv)
        r4.addWidget(btn("开始校验", "Primary", on_click=self.do_verify))
        v4.addLayout(r4)
        self.tb_fv = make_table(["文件", "大小", "哈希", "签名状态", "签名者"],
                                sortable=True)
        self.tb_fv.setColumnWidth(0, 330)
        self.tb_fv.setColumnWidth(1, 84)
        self.tb_fv.setColumnWidth(2, 300)
        self.tb_fv.setColumnWidth(3, 96)
        v4.addWidget(self.tb_fv, 1)
        v4.addWidget(notice("哈希在本地计算，文件不会被上传。签名状态来自 Windows 的"
                            "Authenticode 校验：「签名无效」说明文件在签名后被改动过。",
                            "info"))
        self.tabs.addTab(w4, "文件校验")
        self._loaded = set()
        self._fv_files = []
        self._on_tab()

    # ---------- 通用 ----------
    def _on_tab(self):
        """第一次切到某个页签时才加载（扫描都不快，别在构造时全跑）。"""
        # ⚠️ addTab 会立刻触发 currentChanged，比 __init__ 末尾那两行更早 ——
        #    所以用 getattr 兜住，别假设 _loaded 已经建好。
        loaded = getattr(self, "_loaded", None)
        if loaded is None:
            loaded = self._loaded = set()
        i = self.tabs.currentIndex()
        if i == 0 and 0 not in loaded:
            loaded.add(0)
            self.scan_comp()
        elif i == 1 and 1 not in loaded:
            loaded.add(1)
            self.scan_ext()
        # 页签 2 = 文件校验：文件由用户自己添加，没有「扫描」这回事，不用懒加载

    # ---------- 程序组件 ----------
    def _reload_tab(self):
        """强制重扫**当前页签**。

        ⚠️ 不能用 `refresh()` 代替：它走的是「每个页签只加载一次」的懒加载逻辑
        （`_on_tab` 里查 `_loaded`），**操作完成后不会重读** —— 于是注册/注销
        组件后列表也不变，用户会以为「操作没生效」。这里先清标记再重扫。
        """
        loaded = getattr(self, "_loaded", None)
        if loaded is not None:
            loaded.discard(self.tabs.currentIndex())
        self._on_tab()

    def scan_comp(self):
        self.pill_comp.setText("扫描中…")
        include_sys = self.cb_comp_sys.isChecked()

        def job():
            return S.list_components(cap=2500, include_system=include_sys)
        Tasks.run(self, job, self._got_comp, ctl=self.ctl_comp,
                  on_fail=lambda m: self.pill_comp.setText("失败：%s" % m[:28]))

    def _got_comp(self, rows):
        self.comp = rows or []
        self._fill_comp()

    def _fill_comp(self):
        dark = self.win.dark
        self.tb_comp.setSortingEnabled(False)
        self.tb_comp.setRowCount(0)
        for c in self.comp:
            i = self.tb_comp.rowCount()
            self.tb_comp.insertRow(i)
            sysc = bool(c.get("sys"))
            chk = QTableWidgetItem()
            if sysc:
                # 系统组件不给勾 —— 误禁会搞坏 Office/浏览器/Shell
                chk.setFlags(Qt.ItemIsEnabled)
                chk.setToolTip("系统自带组件，不提供禁用（误禁会导致程序无法启动）")
            else:
                chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                chk.setCheckState(Qt.Unchecked)
            chk.setData(Qt.UserRole, c["clsid"])
            self.tb_comp.setItem(i, 0, chk)
            impl = c.get("impl") or ""
            exists = os.path.exists(os.path.expandvars(impl.split('"')[0])) if impl else False
            nm = SortItem(c.get("name") or "")
            nm.setFlags(nm.flags() & ~Qt.ItemIsEditable)
            nm.setForeground(QBrush(QColor(
                T.get_color("TEXT3" if sysc else "TEXT", dark))))
            tip = c["clsid"]
            if impl and not exists:
                tip += "\n实现文件已不存在：%s" % impl
            nm.setToolTip(tip)
            self.tb_comp.setItem(i, 1, nm)
            kt = SortItem({"inproc": "DLL", "local": "EXE"}.get(c.get("kind"), "?"))
            kt.setFlags(kt.flags() & ~Qt.ItemIsEditable)
            self.tb_comp.setItem(i, 2, kt)
            it = SortItem(impl or "（无实现路径）")
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setForeground(QBrush(QColor(T.get_color("WARN" if impl and not exists
                                                      else "TEXT", dark))))
            it.setToolTip(impl)
            self.tb_comp.setItem(i, 3, it)
            self.tb_comp.setRowHeight(i, 28)
        self.tb_comp.setSortingEnabled(True)
        n_sys = len([x for x in self.comp if x.get("sys")])
        n_dead = len([x for x in self.comp
                      if x.get("impl") and not os.path.exists(
                          os.path.expandvars(x["impl"].split('"')[0]))])
        self.pill_comp.setText("%d 个%s · 实现文件已丢失 %d"
                               % (len(self.comp),
                                  "（含系统 %d）" % n_sys if n_sys else "", n_dead))
        self._sync_comp()

    def _on_comp_item_changed(self, it):
        if it.column() == 0:
            self._sync_comp()

    def _sel_comp(self):
        out = []
        for r in range(self.tb_comp.rowCount()):
            it = self.tb_comp.item(r, 0)
            if it is not None and it.checkState() == Qt.Checked:
                cid = it.data(Qt.UserRole)
                c = next((x for x in self.comp if x["clsid"] == cid), None)
                if c:
                    out.append(c)
        return out

    def _check_comp(self, on):
        state = Qt.Checked if on else Qt.Unchecked
        for r in range(self.tb_comp.rowCount()):
            it = self.tb_comp.item(r, 0)
            if it is not None and (it.flags() & Qt.ItemIsUserCheckable):
                it.setCheckState(state)
        self._sync_comp()

    def _sync_comp(self):
        n = len(self._sel_comp())
        self.lb_comp.setText("已选 %d 个组件" % n if n else "未选择任何项")

    def disable_comp(self):
        sel = self._sel_comp()
        if not sel:
            return info(self, "请先勾选要禁用的组件。")
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not confirm(self, "确认禁用组件",
                       "即将禁用 %d 个组件：\n\n%s\n\n"
                       "禁用方式是把注册项整棵搬到 HKCU 影子区（不是删除），"
                       "随时可在同一页点「还原所选」恢复。\n"
                       "禁用后如果发现某程序打不开，先还原它。"
                       % (len(sel), "\n".join("· " + x["name"][:40] for x in sel[:8])),
                       danger=True):
            return
        self.win.busy("正在禁用 %d 个组件…" % len(sel))
        Tasks.run(self, lambda: [S.disable_component(x["clsid"]) for x in sel],
                  lambda r: (self.win.idle(), self._done_comp(r, "禁用"))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "禁用失败：" + m))[1])

    def enable_comp(self):
        sel = self._sel_comp()
        if not sel:
            return info(self, "请先勾选要还原的组件。")
        self.win.busy("正在还原 %d 个组件…" % len(sel))
        Tasks.run(self, lambda: [S.disable_component(x["clsid"], enable=True)
                                 for x in sel],
                  lambda r: (self.win.idle(), self._done_comp(r, "还原"))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "还原失败：" + m))[1])

    def _done_comp(self, results, verb):
        ok = sum(1 for x in (results or []) if x.get("ok"))
        bad = [x for x in (results or []) if not x.get("ok")]
        self.sess_invalidate("comp")
        self.win.idle()
        lines = ["成功 %d / %d" % (ok, len(results or []))]
        for x in bad[:6]:
            lines.append("失败：%s" % (x.get("msg") or "")[:70])
        # 先刷新再弹窗：模态会阻塞事件循环。
        # 用 _reload_tab 而不是 refresh —— 后者不会重扫（见 _reload_tab 的说明）。
        self._reload_tab()
        info(self, "%s完成" % verb, "\n".join(lines))

    # ---------- 浏览器扩展 ----------
    def scan_ext(self):
        self.pill_ext.setText("扫描中…")
        Tasks.run(self, S.list_browser_extensions, self._got_ext,
                  ctl=self.ctl_ext,
                  on_fail=lambda m: self.pill_ext.setText("失败：%s" % m[:28]))

    def _got_ext(self, rows):
        self.exts = rows or []
        dark = self.win.dark
        self.tb_ext.setSortingEnabled(False)
        self.tb_ext.setRowCount(0)
        for e in self.exts:
            i = self.tb_ext.rowCount()
            self.tb_ext.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)      # 只作标记用，本页无批量写操作
            self.tb_ext.setItem(i, 0, chk)
            for col, val in ((1, e.get("browser") or ""), (2, e.get("name") or ""),
                             (3, e.get("version") or ""),
                             (5, fmt_bytes(e.get("size") or 0))):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                self.tb_ext.setItem(i, col, it)
            en = e.get("enabled")
            st = "已启用" if en else ("已停用" if en is False else "未知")
            si = SortItem(st)
            si.setFlags(si.flags() & ~Qt.ItemIsEditable)
            si.setForeground(QBrush(QColor(
                T.get_color("OK" if en else ("TEXT3" if en is None else "WARN"),
                            dark))))
            self.tb_ext.setItem(i, 4, si)
            perms = "、".join(e.get("perm") or []) or "—"
            pi = SortItem(perms)
            pi.setFlags(pi.flags() & ~Qt.ItemIsEditable)
            pi.setToolTip((e.get("desc") or "") + "\n\n权限：" + perms)
            self.tb_ext.setItem(i, 6, pi)
            self.tb_ext.item(i, 2).setData(Qt.UserRole, e.get("path") or "")
            self.tb_ext.setRowHeight(i, 30)
        self.tb_ext.setSortingEnabled(True)
        by = {}
        for e in self.exts:
            by[e.get("browser") or "?"] = by.get(e.get("browser") or "?", 0) + 1
        self.pill_ext.setText("%d 个 · %s" % (
            len(self.exts), " ".join("%s %d" % kv for kv in sorted(by.items()))))

    def _cur_ext(self):
        r = self.tb_ext.currentRow()
        if r < 0 or r >= self.tb_ext.rowCount():
            return None
        it = self.tb_ext.item(r, 2)
        return (it.data(Qt.UserRole) if it else "") or ""

    def open_ext_dir(self):
        p = self._cur_ext()
        if not p or not os.path.isdir(p):
            return info(self, "请先在列表里选中一个扩展。")
        S.open_dir(p)

    # ---------- 文件校验 ----------
    def add_files(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "选择要校验的文件")
        if not fs:
            return
        for f in fs:
            if f not in self._fv_files:
                self._fv_files.append(f)
        self._fill_fv()

    def add_cur_exe(self):
        """把当前正在运行的程序加进来（常用于自查可疑进程）。"""
        self._fv_files = [sys.executable]
        self._fill_fv()

    def _clear_files(self):
        self._fv_files = []
        self._fill_fv()

    def _fill_fv(self):
        self.tb_fv.setSortingEnabled(False)
        self.tb_fv.setRowCount(0)
        dark = self.win.dark
        for p in self._fv_files:
            i = self.tb_fv.rowCount()
            self.tb_fv.insertRow(i)
            nm = os.path.basename(p)
            for col, val in ((0, nm), (1, ""), (2, ""), (3, "待校验"), (4, "")):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(p if col == 0 else str(val))
                self.tb_fv.setItem(i, col, it)
            self.tb_fv.item(i, 0).setData(Qt.UserRole, p)
            self.tb_fv.setRowHeight(i, 30)
        self.tb_fv.setSortingEnabled(True)
        self.pill_fv.setText("%d 个待校验" % len(self._fv_files))

    def do_verify(self):
        if not self._fv_files:
            return info(self, "请先添加要校验的文件。")
        algo = self.cb_algo.currentData() or "sha256"
        files = list(self._fv_files)
        self.win.busy("正在计算哈希并校验签名…")
        Tasks.run(self, lambda: S.file_verify(files, algo),
                  lambda rows: (self.win.idle(), self._got_fv(rows))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "校验失败：" + m))[1])

    def _got_fv(self, rows):
        dark = self.win.dark
        for i, r in enumerate(rows or []):
            if i >= self.tb_fv.rowCount():
                break
            self.tb_fv.item(i, 1).setText(fmt_bytes(r.get("size") or 0))
            self.tb_fv.item(i, 2).setText((r.get("hash") or "")[:32])
            st = r.get("sign") or ""
            self.tb_fv.item(i, 3).setText(st)
            self.tb_fv.item(i, 3).setForeground(QBrush(QColor(
                T.get_color({"已签名": "OK", "签名无效": "ERR",
                             "不受信任": "ERR", "未签名": "WARN"}.get(st, "TEXT3"),
                            dark))))
            self.tb_fv.item(i, 4).setText((r.get("signer") or "")[:40])
            self.tb_fv.item(i, 4).setToolTip(r.get("signer") or "")
        n = len(rows or [])
        bad = len([r for r in (rows or []) if r.get("sign") == "签名无效"])
        self.pill_fv.setText("已校验 %d 个%s" % (n, " · 签名异常 %d" % bad if bad else ""))

    def refresh(self):
        """进页时不自动重扫（枚举注册表/磁盘都慢），由各页签自己触发。"""
        self._on_tab()


# ==========================================================================
# 9. 策略诊断
# ==========================================================================
class PoliciesPage(Page):
    title = "策略诊断"

    def __init__(self, win):
        super().__init__(win)
        self.found = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(notice(
            "<b>为什么会出现“由你的组织管理”？</b><br>"
            "优化工具为了禁用 Defender、关闭更新、关掉 SmartScreen，会把设置写进组策略分支 "
            "SOFTWARE\\Policies\\…。Windows 读到这里有值，就认为“有组织在统一管控本机”，"
            "于是安全中心 / Windows 更新 / Edge 显示“由你的组织管理”。本页就是清掉这些残留。", "info"))

        bar = QHBoxLayout()
        self.btn_pol = btn("扫描策略残留", "Primary")
        bar.addWidget(self.btn_pol)
        self.ctl_pol = ScanCtl(self.btn_pol, lambda *_: self.scan())
        self.btn_fix = btn("清除所选", "Danger", on_click=self.fix)
        self.btn_fix.setEnabled(self.win.admin_mode)
        bar.addWidget(self.btn_fix)
        bar.addStretch(1)
        self.sum = pill("—", "")
        bar.addWidget(self.sum)
        root.addLayout(bar)

        self.prog_row = QWidget()
        pr = QHBoxLayout(self.prog_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(10)
        self.prog_lb = QLabel("正在扫描注册表策略项…")
        self.prog_lb.setObjectName("Muted")
        self.prog = QProgressBar()
        self.prog.setRange(0, 0)          # 不确定进度：策略项很少，扫描通常 <1s
        self.prog.setTextVisible(False)
        self.prog.setMinimumWidth(240)
        pr.addWidget(self.prog_lb, 0)
        pr.addWidget(self.prog, 1)
        self.prog_row.setVisible(False)
        root.addWidget(self.prog_row)

        f, v = card("检测结果")
        self.table = make_table(["", "注册表路径", "影响", "值数量"], sortable=True)
        self.table.setColumnWidth(0, 34)
        self.table.itemChanged.connect(lambda *_: self._sync_all_btn())
        v.addWidget(self.table, 1)
        root.addWidget(f, 1)

        # 全选 / 全不选（挂在顶部工具条「清除所选」右侧）
        self.btn_all = select_all_button(self.table, on_changed=self._sync_all_btn)
        bar.insertWidget(bar.indexOf(self.btn_fix) + 1, self.btn_all)

    def _sync_all_btn(self):
        b = getattr(self, "btn_all", None)
        if b is not None:
            b._refresh()                 # 全选按钮文案与计数跟着勾选状态走

    def refresh(self):
        """不再自动扫描：进入页面只重置为待扫描状态，由用户点按钮触发。"""
        self.found = []
        self.table.setRowCount(0)
        self.sum.setText("未扫描")
        self.sum.setObjectName("Pill")
        self.sum.style().unpolish(self.sum); self.sum.style().polish(self.sum)
        self._sync_all_btn()

    def scan(self):
        self.win.busy("正在扫描策略残留…")
        self.prog_row.setVisible(True)
        Tasks.run(self, S.policies_scan,
                  lambda r: (self.win.idle(), self.prog_row.setVisible(False),
                             self._got(r))[-1],
                  ctl=self.ctl_pol,
                  on_fail=lambda m: (self.win.idle(), self.prog_row.setVisible(False),
                                     warn(self, m))[-1])

    def _got(self, found):
        self.found = found
        # 填表期间关排序（否则行会错位/留空行），同时屏蔽 itemChanged ——
        # 不屏蔽的话每插一行都会触发一次 _sync_all_btn，几百行就是 O(n²)。
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for x in found:
            i = self.table.rowCount()
            self.table.insertRow(i)
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb.setCheckState(Qt.Checked)
            cb.setData(Qt.UserRole, x["path"])   # 排序后仍能对上路径
            self.table.setItem(i, 0, cb)
            for c, val in enumerate([x["path"], x["why"], x["count"]], start=1):
                it = SortItem(str(val))          # SortItem：数值列按数值排
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                if c == 3:
                    # 「值数量」不这么做的话 10 会排在 2 前面（字符串排序）
                    it.setData(Qt.UserRole + 1, float(x["count"] or 0))
                self.table.setItem(i, c, it)
            self.table.setRowHeight(i, 34)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(True)
        self._sync_all_btn()
        self.table.setColumnWidth(1, 420)
        self.table.setColumnWidth(2, 300)
        if not found:
            self.sum.setText("未发现残留")
            self.sum.setObjectName("PillOk")
        else:
            self.sum.setText("%d 处残留" % len(found))
            self.sum.setObjectName("PillErr")
        self.sum.style().unpolish(self.sum); self.sum.style().polish(self.sum)

    def fix(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not self.found:
            return info(self, "请先点“扫描策略残留”。")
        paths = []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                p = it.data(Qt.UserRole)
                if p:
                    paths.append(p)
        if not paths:
            return info(self, "没有选择任何项。")
        if not confirm(self, "清除策略残留",
                       "将删除 %d 处组策略分支，并解除更新暂停。\n\n"
                       "清理前会先把每一项导出成 .reg 备份，存放在：\n%s\n"
                       "出问题双击对应备份即可还原。\n\n"
                       "清除后需要重启，“由你的组织管理”才会消失。"
                       % (len(paths), S.BACKUP), danger=True):
            return
        self.win.busy("正在清除…")
        Tasks.run(self, lambda: S.policies_fix(paths),
                  lambda r: (self.win.idle(), self._fixed(r))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    def _fixed(self, r):
        r = r or {}
        results = r.get("results") or []
        backups = r.get("backups") or []
        ok = len([x for x in results if x.get("ok")])
        self.scan()
        if backups:
            info(self, "已清除 %d / %d 处。\n\n已备份 %d 个 .reg 文件到：\n%s\n\n请重启电脑。"
                 % (ok, len(results), len(backups), r.get("backup_dir") or S.BACKUP))
        else:
            info(self, "已清除 %d / %d 处（未生成备份文件）。\n\n请重启电脑。" % (ok, len(results)))


# ==========================================================================
# 10. 设置（问题3：备份 / 恢复 / 导入导出）
# ==========================================================================


class SettingsPage(Page):
    title = "设置"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        f1, v1 = card("配置备份与迁移")
        v1.addWidget(notice(
            "关闭软件时会<b>自动保存</b>当前设置到程序目录的 settings.json，下次启动<b>自动恢复</b>。"
            "<b>换成新电脑</b>用「导出配置…」生成一个 .json 档案拷过去导入即可，包含：<br>"
            "　· 外观与偏好（主题 / 强调色 / 开机启动 / 关闭行为 / 更新检查等）<br>"
            "　· 右键菜单禁用记录、系统组件禁用记录（存在注册表里，旧版导出<b>不带</b>这部分）<br>"
            "　· 系统优化<b>已生效项清单</b>（导入时按本机实测算出「还差哪些」，"
            "再问你一次要不要补齐）<br>"
            "　· <b>在「启动与服务」页被禁用的</b>启动项 / 服务 / 计划任务"
            "（系统里没有「谁禁的」这种线索，靠这份记录才能在新机器上照做）<br>"
            "<b>不含</b>显卡改名备份、Defender 原始状态快照、操作日志 —— 那些都是<b>本机</b>的，"
            "换台机器没有意义。<br>"
            "导入是<b>叠加</b>：档案里有的键覆盖，没有的保持本机原样；"
            "导入前会把当前配置另存一份到 backup 目录。<br>"
            "（「系统优化」页的<b>勾选</b>不在此列 —— 那边每次打开都不预勾，由你自己选；"
            "档案带走的是<b>当时真正生效</b>的项。）", "info"))
        self.lb_path = QLabel("配置文件：—")
        self.lb_path.setObjectName("Mono"); self.lb_path.setWordWrap(True)
        v1.addWidget(self.lb_path)
        h = QHBoxLayout()
        h.addWidget(btn("立即备份", "Primary", on_click=self.save_now))
        h.addWidget(btn("恢复上次设置", on_click=self.restore_now))
        h.addWidget(btn("导出配置…", on_click=self.export_cfg))
        h.addWidget(btn("导入配置…", on_click=self.import_cfg))
        h.addStretch(1)
        v1.addLayout(h)
        root.addWidget(f1)

        # ---- 操作备份（.reg）----
        # 从「注册表清理」页迁过来的：那边现在是「系统还原点」，而 .reg 备份是
        # **本工具自己的操作存档**（误删了可以写回来），跟系统快照不是一回事，
        # 放设置页和「配置备份与迁移」是同一类东西。
        f_bk, v_bk = card("操作备份（.reg）")
        v_bk.addWidget(notice(
            "本工具每次<b>删除注册表项之前</b>都会先导出一份 <code>.reg</code>："
            "卸载残留清理、注册表无效项清理、策略清理都会留下备份。"
            "误删了就在这里勾选<b>覆盖写回</b>。\n"
            "还原会把该项<b>退回备份时的状态</b> —— 如果你后来又改过同一项，"
            "那个改动会被覆盖。只允许还原 <code>backup</code> 目录内的文件（防路径穿越）。\n"
            "（<b>系统还原点不在这里</b> —— 那是 Windows 的系统级快照，"
            "在「注册表清理 → 备份与还原」页。）", "warn"))
        hb_bk = QHBoxLayout()
        hb_bk.addWidget(btn("刷新列表", "Primary", on_click=self.load_backups))
        hb_bk.addWidget(btn("打开 backup 目录", on_click=self.open_backup_dir))
        hb_bk.addStretch(1)
        self.pill_bk = pill("—", self.win)
        hb_bk.addWidget(self.pill_bk)
        v_bk.addLayout(hb_bk)
        self.tb_bk = make_table(["", "文件", "来源", "大小", "时间"], sortable=True)
        self.tb_bk.setColumnWidth(0, 34)
        self.tb_bk.setColumnWidth(1, 380)
        self.tb_bk.setColumnWidth(2, 116)
        self.tb_bk.setColumnWidth(3, 86)
        self.tb_bk.setColumnWidth(4, 148)
        # 固定一个高度区间：设置页底部还有「外观 / 偏好 / 关于」三张卡，
        # 表格如果按内容无限长会把它们顶出视口（页面在滚动区里，会多出一屏滚动）。
        self.tb_bk.setMinimumHeight(150)
        self.tb_bk.setMaximumHeight(240)
        self.tb_bk.itemChanged.connect(lambda *_: self._bk_sel())
        v_bk.addWidget(self.tb_bk)
        hbk2 = QHBoxLayout()
        self.btn_bk_restore = btn("还原勾选的备份", "Danger", on_click=self.restore_bk)
        self.btn_bk_restore.setEnabled(False)
        hbk2.addWidget(self.btn_bk_restore)
        hbk2.addWidget(select_all_button(self.tb_bk, on_changed=self._bk_sel))
        hbk2.addStretch(1)
        self.lb_bk = QLabel("")
        self.lb_bk.setObjectName("Muted")
        hbk2.addWidget(self.lb_bk)
        v_bk.addLayout(hbk2)
        root.addWidget(f_bk)

        # ---- 外观：主题色（Win11 风格：常用色块 + 调色盘） ----
        f_ac, av = card("外观")
        av.addWidget(notice(
            "界面强调色，作用于按钮、选中行、开关、进度条、导航高亮等。"
            "点色块快速选取，或拖动下面的色相条任选颜色。"
            "CPU / 内存 / GPU 等性能曲线各自使用独立颜色，不受此处影响。", "info"))

        # 第一行：全部默认色块（自定义色直接拖下面的色相条，无需额外入口）
        srow = QHBoxLayout(); srow.setSpacing(7)
        self._swatches = []
        self._sw_group = QButtonGroup(self)
        self._sw_group.setExclusive(True)
        for _name, _hexv in T.ACCENT_PRESETS:
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip("%s  %s" % (_name, _hexv))
            b.clicked.connect(lambda _c=False, h=_hexv: self.win.set_accent(h))
            self._sw_group.addButton(b)
            self._swatches.append((_hexv, b))
            srow.addWidget(b)
        srow.addStretch(1)
        av.addLayout(srow)

        # 第二行：色相条（彩虹渐变），单击 / 拖动选任意色
        self.hue_bar = HueBar()
        self.hue_bar.preview.connect(self._preview_accent)
        self.hue_bar.picked.connect(lambda h: self.win.set_accent(h))
        av.addWidget(self.hue_bar)

        crow = QHBoxLayout(); crow.setSpacing(10)
        self.chip_accent = QLabel(); self.chip_accent.setObjectName("AccentChip")
        self.lb_accent = QLabel(""); self.lb_accent.setObjectName("Muted")
        self.lb_accent.setWordWrap(True)
        crow.addWidget(self.chip_accent)
        crow.addWidget(self.lb_accent, 1)
        self.btn_follow = btn("跟随系统", on_click=lambda: self.win.set_accent(""))
        crow.addWidget(self.btn_follow)
        av.addLayout(crow)

        # 深浅色：跟随系统 / 浅色 / 深色
        trow = QHBoxLayout(); trow.setSpacing(8)
        trow.addWidget(QLabel("界面深浅色："))
        self.combo_theme = QComboBox()
        for _lab, _key in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
            self.combo_theme.addItem(_lab, _key)
        self.combo_theme.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo_theme.setMaximumWidth(140)
        self.combo_theme.setToolTip("选「跟随系统」后，Windows 切换深/浅色时本程序会自动跟随"
                                    "（不需要重启，也没有常驻轮询）")
        _ix = self.combo_theme.findData(self.win.theme_mode)
        self.combo_theme.setCurrentIndex(_ix if _ix >= 0 else 0)
        self.combo_theme.currentIndexChanged.connect(
            lambda _i: self.win.set_theme_mode(self.combo_theme.currentData()))
        trow.addWidget(self.combo_theme)
        trow.addStretch(1)
        av.addLayout(trow)

        root.addWidget(f_ac)

        f_pref, pv = card("偏好设置")
        g = QGridLayout(); g.setSpacing(12)
        self.cb_startup = QCheckBox("开机自动启动 WinToolbox")
        self.cb_startup.setChecked(bool(self.win.settings.get("startup", False)))
        self.cb_startup.toggled.connect(self.on_startup)
        g.addWidget(self.cb_startup, 0, 0)

        # 关闭窗口时的行为：每次询问 / 收进托盘（后台监测暂停）/ 直接退出
        g.addWidget(QLabel("关闭窗口时："), 0, 1)
        self.combo_close = QComboBox()
        for _cl, _cv in (("每次询问", "ask"),
                         ("最小化到托盘（监测暂停）", "tray"),
                         ("直接退出程序", "exit")):
            self.combo_close.addItem(_cl, _cv)
        self.combo_close.setToolTip(
            "最小化到托盘：窗口收进托盘、性能监测暂停，双击托盘图标即可恢复。\n"
            "直接退出：完全关闭本程序。")
        _cix = self.combo_close.findData(self.win._close_action())
        self.combo_close.setCurrentIndex(_cix if _cix >= 0 else 0)
        self.combo_close.currentIndexChanged.connect(self.on_close_action)
        g.addWidget(self.combo_close, 0, 2)

        g.addWidget(QLabel("启动时检查更新："), 1, 0)
        self.cb_update = QCheckBox("有新版时提示一次（推荐；没有新版不打扰）")
        self.cb_update.setChecked(bool(self.win.settings.get("check_update", True)))
        self.cb_update.toggled.connect(self.on_check_update)
        self.cb_update.setToolTip("启动后联网查询 GitHub 最新版本，每天最多一次；"
                                  "网络不通时静默跳过。")
        g.addWidget(self.cb_update, 1, 1, 1, 2)
        pv.addLayout(g)
        root.addWidget(f_pref)

        # ---- 系统修复：重启进「高级启动」（Windows 修复菜单） ----
        # 从「CPU 调试」页迁过来的：它是系统级修复入口，不是 CPU 功能，
        # 放「设置」页和「关于」挨着更合理。
        f4, v4 = card("Windows 修复菜单（「高级启动」）")
        v4.addWidget(notice(
            "重启后直接进入 <b>Windows 恢复环境</b> —— "
            "和「电脑多次启动失败后自动跳出来的那个修复界面」是同一个。\n"
            "里面有：<b>启动修复</b>、<b>启动设置</b>（可选安全模式）、"
            "<b>系统还原</b>、<b>命令提示符</b>、<b>UEFI 固件设置</b>。", "info"))
        rb = QHBoxLayout()
        rb.addWidget(btn("重启进入「高级启动」", "Primary",
                         on_click=self.go_recovery))
        # 保留取消按钮：倒数 30 秒里这是唯一能在应用内反悔的入口
        self.btn_reboot_cancel = btn("取消重启", on_click=self.do_cancel_reboot)
        self.btn_reboot_cancel.setEnabled(False)
        rb.addWidget(self.btn_reboot_cancel)
        rb.addStretch(1)
        v4.addLayout(rb)
        self.lb_reboot = QLabel("")
        self.lb_reboot.setObjectName("Muted")
        self.lb_reboot.setVisible(False)
        v4.addWidget(self.lb_reboot)
        root.addWidget(f4)

        f3, v3 = card("关于")
        rows = [("软件名称", APP_NAME), ("版本", "v%s" % APP_VER),
                ("作者", "by：%s" % AUTHOR),
                ("权限", "管理员 ✓" if IS_ADMIN else "普通（功能受限）"),
                ("数据目录", S.APP_DIR),
                ("功能来源", "参考同类工具的功能思路，全部重新实现，"
                             "未复用任何第三方代码")]
        g = QGridLayout(); g.setSpacing(8)
        for i, (k, val) in enumerate(rows):
            a = QLabel(k); a.setObjectName("Muted")
            b = QLabel(str(val)); b.setWordWrap(True)
            g.addWidget(a, i, 0); g.addWidget(b, i, 1)
        # GitHub：文本链接 + 一键按钮
        a = QLabel("GitHub"); a.setObjectName("Muted")
        self.link_gh = QLabel('<a href="%s" style="color:%s; text-decoration:none;">%s</a>'
                              % (GITHUB_URL, T.get_color("BLUE", self.win.dark), GITHUB_URL))
        link = self.link_gh
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        link.setCursor(Qt.PointingHandCursor)
        link.setWordWrap(True)
        link.setToolTip("点击在浏览器中打开项目主页")
        g.addWidget(a, len(rows), 0); g.addWidget(link, len(rows), 1)
        hb = QHBoxLayout()
        hb.addWidget(btn("检查更新", on_click=self.on_check_update_now))
        hb.addWidget(btn("打开 GitHub 项目主页", "Primary",
                         on_click=lambda: self.open_github()))
        hb.addStretch(1)
        v3.addLayout(g)
        v3.addLayout(hb)
        root.addWidget(f3)
        root.addStretch(1)


    # ---------- 重启进「高级启动」（本卡片唯一入口） ----------
    def go_recovery(self):
        self._reboot_ui()

    def _reboot_ui(self):
        """安排「重启进「高级启动」」。

        ⚠️ 刻意留 %d 秒（不是 `/t 0`）：这是会打断手头事情的操作，必须给保存
        文件的时间，而且倒计时里随时能点「取消重启」反悔。
        """
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        body = ("电脑将在 %d 秒后重启，并进入「高级启动」。\n\n"
                "进去后依次选：疑难解答 → 高级选项，里面有\n"
                "  · 启动修复、系统还原；\n"
                "  · 启动设置 —— 可选是否进安全模式；\n"
                "  · 命令提示符、UEFI 固件设置。\n\n"
                "请先保存所有工作。" % S.REBOOT_DELAY_SECS)
        if not confirm(self, "重启进入「高级启动」", body, danger=True):
            return
        Tasks.run(self, S.reboot_to, self._reboot_done,
                  on_fail=lambda m: warn(self, "提交重启失败", m))

    def _reboot_done(self, r):
        r = r or {}
        if not r.get("ok"):
            return warn(self, "重启没能安排成功",
                        "%s\n\n手动执行同样效果的命令：\n  shutdown /r %s /t %d"
                        % (r.get("err") or "原因未知", S.REBOOT_FLAG,
                           S.REBOOT_DELAY_SECS))
        self._reboot_left = int(r.get("delay") or S.REBOOT_DELAY_SECS)
        self._reboot_name = r.get("name") or ""
        self.btn_reboot_cancel.setEnabled(True)
        self.lb_reboot.setVisible(True)
        self._paint_reboot()
        if getattr(self, "timer_reboot", None) is None:
            self.timer_reboot = QTimer(self)
            self.timer_reboot.setInterval(1000)
            self.timer_reboot.timeout.connect(self._reboot_tick)
        self.timer_reboot.start()

    def _paint_reboot(self):
        self.lb_reboot.setText(
            "⏱ 已安排重启进入%s —— %d 秒后执行；点「取消重启」可立即中止。"
            % (getattr(self, "_reboot_name", ""), self._reboot_left))

    def _reboot_tick(self):
        self._reboot_left -= 1
        if self._reboot_left <= 0:
            self.timer_reboot.stop()
            self.lb_reboot.setText("正在重启…")
            self.btn_reboot_cancel.setEnabled(False)
            return
        self._paint_reboot()

    def do_cancel_reboot(self):
        Tasks.run(self, S.cancel_reboot, self._cancel_done,
                  on_fail=lambda m: warn(self, "取消失败", m))

    def _cancel_done(self, r):
        r = r or {}
        if getattr(self, "timer_reboot", None) is not None:
            self.timer_reboot.stop()
        self.btn_reboot_cancel.setEnabled(False)
        if r.get("ok"):
            self.lb_reboot.setText("已取消重启。")
        else:
            # 取消失败最常见的原因就是**本来就没有待执行的重启**（比如已被取消过）
            self.lb_reboot.setText("没有取消成功：%s" % (r.get("err") or "原因未知"))
    def open_github(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(GITHUB_URL))

    def on_check_update(self, on):
        """启动检查更新开关（写回设置，退出时统一保存）。"""
        self.win.settings["check_update"] = bool(on)

    def on_check_update_now(self):
        """手动检查更新：不受每日一次限制。"""
        self.win.check_update_now()

    def _sync_from_settings(self):
        """把 settings 里的值同步到本页所有控件。

        ⚠️ 导入 / 恢复设置后**必须**调 —— 只改 `win.settings` 字典、不碰界面的话，
        用户看到的是「提示导入成功，但界面上什么都没变」。
        """
        st = self.win.settings
        self.cb_startup.setChecked(bool(st.get("startup", False)))
        self.cb_update.setChecked(bool(st.get("check_update", True)))
        ix = self.combo_close.findData(self.win._close_action())
        if ix >= 0:
            self.combo_close.blockSignals(True)      # 别触发 on_close_action 回写
            self.combo_close.setCurrentIndex(ix)
            self.combo_close.blockSignals(False)
        self.win._sync_theme_controls()               # 主题下拉
        self.refresh()                                # 色块选中态 / 备份列表
        # 其它页里由 settings 驱动的控件：不同步的话，用户导入后切过去还是旧值，
        # 会以为「导入没生效」（那正是本次要修的现象）。
        sw = self.win.pages.get("software")
        cbq = getattr(sw, "cb_quiet", None)
        if cbq is not None:
            cbq.blockSignals(True)                    # 别触发回写
            cbq.setChecked(bool(st.get("uninstall_quiet")))
            cbq.blockSignals(False)
        cl = self.win.pages.get("cleanup")
        ca = getattr(cl, "combo_age", None)
        if ca is not None:
            ix2 = ca.findData(st.get("junk_age_mode"))
            if ix2 >= 0:
                ca.blockSignals(True)
                ca.setCurrentIndex(ix2)
                ca.blockSignals(False)
                cl.on_age_mode(ix2)                   # 按新阈值刷新（含保存）

    def refresh(self):
        self.lb_path.setText("配置文件：%s" % SETTINGS_FILE)
        self._paint_swatches()
        # 备份列表进页就重读：只是列目录 + stat（毫秒级），不重读的话用户切到
        # 设置页看到的是空表，会以为「读不到上一版备份」。
        if hasattr(self, "tb_bk"):
            self.load_backups()

    # ---------------------------------------------------------- 操作备份（.reg）
    def load_backups(self):
        def done(r):
            rows = r or []
            t = self.tb_bk
            t.blockSignals(True)
            t.setSortingEnabled(False)
            t.setRowCount(0)
            for x in rows:
                i = t.rowCount()
                t.insertRow(i)
                cb = QTableWidgetItem()
                cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                cb.setCheckState(Qt.Unchecked)          # 还原是覆盖式，默认不勾
                cb.setData(Qt.UserRole, x["path"])
                t.setItem(i, 0, cb)
                for col, val in ((1, x["name"]), (2, x["kind"]),
                                 (3, fmt_bytes(x["size"])), (4, x["mtime"])):
                    it = SortItem(str(val))
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    it.setToolTip(str(val))
                    t.setItem(i, col, it)
            t.setSortingEnabled(True)
            t.blockSignals(False)
            if rows:
                self.pill_bk.setText("%d 个备份" % len(rows))
                self.pill_bk.setObjectName("Pill")
            elif not os.path.isdir(S.BACKUP):
                # 目录都没建过 → 说明还没做过任何会产出备份的操作，说清楚免得
                # 被当成「读取失败」
                self.pill_bk.setText("当前无备份（备份目录尚未创建）")
                self.pill_bk.setObjectName("PillWarn")
            else:
                # 空列表不能只显示「0 个备份」—— 用户看不出是「没有备份」还是
                # 「读取失败」。加上目录路径，让两种情况可以自行区分。
                self.pill_bk.setText("当前无备份（目录：%s）" % S.BACKUP)
                self.pill_bk.setObjectName("PillWarn")
            self.pill_bk.style().unpolish(self.pill_bk)
            self.pill_bk.style().polish(self.pill_bk)
            self._bk_sel()

        Tasks.run(self, S.reg_backups_list, done, on_fail=self._bk_fail)

    def _bk_fail(self, m):
        """读取失败要**明说**，不能安静地留一张空表。"""
        self.pill_bk.setText("读取失败：%s" % str(m)[:40])
        self.pill_bk.setObjectName("PillWarn")
        self.pill_bk.style().unpolish(self.pill_bk)
        self.pill_bk.style().polish(self.pill_bk)

    def _bk_sel(self):
        if not hasattr(self, "lb_bk"):
            return
        n = sum(1 for r in range(self.tb_bk.rowCount())
                if self.tb_bk.item(r, 0) and self.tb_bk.item(r, 0).checkState() == Qt.Checked)
        self.lb_bk.setText("已选 %d 个" % n if n else "未选择（还原会覆盖当前设置）")
        self.btn_bk_restore.setEnabled(n > 0)

    def restore_bk(self):
        paths = [p for p in (self.tb_bk.item(r, 0).data(Qt.UserRole)
                             for r in range(self.tb_bk.rowCount())
                             if self.tb_bk.item(r, 0)
                             and self.tb_bk.item(r, 0).checkState() == Qt.Checked) if p]
        if not paths:
            return info(self, "没有勾选备份。")
        if not confirm(self, "还原注册表备份",
                       "将把 %d 个备份<b>覆盖写回</b>注册表：\n%s\n\n"
                       "如果这些键后来被改过，会退回备份时的状态。确认继续？"
                       % (len(paths), "\n".join("· " + os.path.basename(p) for p in paths[:10])),
                       danger=True):
            return
        self.win.busy("正在还原…")

        def done(r):
            self.win.idle()
            self.load_backups()      # 铁律：成功回调必须 reload，否则看不到变化
            info(self, (r or {}).get("msg") or "完成")

        Tasks.run(self, lambda: [S.reg_backup_restore(p) for p in paths], done,
                  on_fail=lambda m: (self.win.idle(), warn(self, "还原失败", m))[1])

    def open_backup_dir(self):
        try:
            os.startfile(S.BACKUP)                   # noqa: 打开资源管理器
        except OSError as e:
            warn(self, "打开失败：%s" % e)

    def pick_accent(self):
        """调色盘任选主题色（系统取色器）。当前界面无入口，保留备用。"""
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor(T.get_accent()), self, "选择主题色")
        if c.isValid():
            self.win.set_accent(c.name())

    def _preview_accent(self, hexv):
        """拖动色相条时的轻量预览：只改色块与文字，不重建全局样式。"""
        chip = getattr(self, "chip_accent", None)
        if chip is not None:
            chip.setStyleSheet("QLabel#AccentChip { background: %s; }" % hexv)
        lb = getattr(self, "lb_accent", None)
        if lb is not None:
            lb.setText("预览：%s（松开即应用）" % hexv.upper())

    def _paint_swatches(self):
        """重画色块：当前色打勾 + 外圈高亮，并更新「当前」提示。"""
        cur = T.get_accent().lower()
        following = T.is_following_system()
        muted = T.get_color("TEXT3", self.win.dark)
        ring = T.get_color("TEXT", self.win.dark)
        # 互斥组不允许「全部取消选中」，画之前先临时关掉互斥
        grp = getattr(self, "_sw_group", None)
        if grp is not None:
            grp.setExclusive(False)
        for hexv, b in getattr(self, "_swatches", []):
            sel = (not following) and hexv.lower() == cur
            b.setChecked(sel)
            b.setText("✓" if sel else "")
            b.setStyleSheet(
                "QPushButton { border: 2px solid transparent; border-radius: 15px;"
                " background: %s; color: %s; font-weight: 700; }"
                "QPushButton:hover { border-color: %s; }"
                "QPushButton:checked { border-color: %s; }"
                % (hexv, T.contrast_text(hexv), muted, ring))
        if grp is not None:
            grp.setExclusive(True)
        chip = getattr(self, "chip_accent", None)
        if chip is not None:
            chip.setStyleSheet("QLabel#AccentChip { background: %s; }" % cur)
        hb = getattr(self, "hue_bar", None)
        if hb is not None:
            c0 = QColor(cur)
            if c0.hue() >= 0:              # 灰色无有效色相 → 指示器保持原位
                hb.set_hue(c0.hue())
        lb = getattr(self, "lb_accent", None)
        if lb is not None:
            lb.setText("当前：跟随系统 · %s（你 Windows 的强调色）" % cur.upper()
                       if following else "当前：自定义 · %s" % cur.upper())
        lk = getattr(self, "link_gh", None)
        if lk is not None:
            lk.setText('<a href="%s" style="color:%s; text-decoration:none;">%s</a>'
                       % (GITHUB_URL, T.get_color("BLUE", self.win.dark), GITHUB_URL))

    def save_now(self):
        if settings_save(self.win.settings):
            info(self, "已保存到：\n%s" % SETTINGS_FILE)
        else:
            warn(self, "保存失败（目录不可写？）")

    def restore_now(self):
        data = settings_load()
        if not data:
            return info(self, "还没有保存过的设置。")
        self.win.settings = data
        opt = self.win.pages.get("optimize")
        if opt:
            # 不恢复「优化项勾选」：本软件的设计是打开时不预勾任何项，
            # 恢复配置时静默勾上会绕过那条安全约定。其它设置照常恢复。
            opt.sel = {}
            opt._update_safe_hint()
            opt.build_root()
        # 主题 / 强调色要重新应用（它们靠 QSS 重画，光改字典看不见）
        self.win.set_theme_mode(self.win.settings.get("theme") or self.win.theme_mode)
        self.win.set_accent(self.win.settings.get("accent"))
        self.win.notify_all_changed()          # 已加载的页全部重读
        self._sync_from_settings()             # 本页控件跟上
        settings_save(self.win.settings)
        info(self, "已恢复上次保存的设置。")

    def export_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置档案", "WinToolbox配置.json",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            prof = S.profile_build(self.win.settings, APP_VER)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(prof, f, ensure_ascii=False, indent=1)
            n = _profile_reg_count(prof.get("own_reg") or {})
            n_opt = len(prof.get("optimize_applied") or [])
            extra = [("本软件注册表状态：%d 个键。" % n) if n
                     else "（本机没有本软件的注册表状态，档案里这部分为空。）",
                     ("系统优化：记下本机**已生效**的 %d 项，导入时可一键补齐。"
                      % n_opt) if n_opt
                     else "系统优化：本机当前没有已生效的优化项。"]
            info(self, "已导出配置档案",
                 "文件：%s\n\n包含：\n· %s\n\n%s"
                 % (path, "\n· ".join(prof.get("_includes") or []),
                    "\n".join(extra)))
        except Exception as e:
            warn(self, "导出失败", str(e))

    def import_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入配置档案", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            return warn(self, "导入失败", "读不了这个文件：%s" % e)
        # ① 先**整份校验**，不合格就一个字节都不动
        prof = S.profile_normalize(raw)
        if prof is None:
            return warn(self, "导入失败",
                        "这不是 WinToolbox 的配置档案。\n\n"
                        "（档案里带 _app 标记；旧版导出的 settings.json 也认得。）")
        data = prof["settings"]
        reg = prof.get("own_reg") or {}
        n_reg = _profile_reg_count(reg)
        tip = ["将导入 %d 项设置。" % len(data)]
        if n_reg:
            tip.append("并写回本软件的注册表状态（%d 个键）：右键菜单禁用 / 系统组件禁用。"
                       % n_reg)
        if prof.get("_legacy"):
            tip.append("（这是**旧版**导出的文件 —— 只含设置，不含注册表状态。）")
        n_opt = len(prof.get("optimize_applied") or [])
        if n_opt:
            tip.append("档案里还记着 %d 项「导出时已生效的系统优化」—— 本机没生效的"
                       "会**单独再问你一次**要不要补齐（不会顺手改掉）。" % n_opt)
        tip.append("\n导入前会把**当前**配置另存一份到 backup 目录，出问题能退回来。")
        if not confirm(self, "导入配置档案", "\n".join(tip)):
            return
        try:
            # ② 先留恢复副本（TweakHub 的做法：先能回来，再改东西）
            bk = S.profile_backup_current(self.win.settings, APP_VER)
            # ③ 应用设置：先把值写进字典（少了这句，除了 theme/accent 之外的键都不会生效）
            self.win.settings.update(data)
            opt = self.win.pages.get("optimize")
            if opt:
                # 同「恢复设置」：不把配置里的优化项勾选带进来（那会绕过
                # 「打开软件不预勾任何项」的安全约定），其它设置照常生效
                opt.sel = {}
                opt._update_safe_hint()
                opt.build_root()
            # ⚠️ 光改 settings 字典，界面上什么都不会变 —— 用户反馈的
            #    「导入后没有任何改动效果」就是这么来的。要真的应用一次：
            #    只应用**这份文件确实带了的键**，免得把本机原有的主题/色重置掉。
            if "theme" in data:
                self.win.set_theme_mode(data.get("theme"))
            if "accent" in data:
                self.win.set_accent(data.get("accent"))
            self.win.notify_all_changed()          # 已加载的页全部重读
            self._sync_from_settings()             # 本页控件跟上
            settings_save(self.win.settings)       # 应用过程会回写字典，最后统一存
            # ④ 写回注册表状态（档案里没有就跳过 —— 那不是失败）
            reg_msg = ""
            if n_reg:
                r = S.own_state_import(reg)
                reg_msg = "\n\n注册表状态：%s" % r.get("msg")
                self.win.notify_all_changed()      # 菜单 / 组件状态可能变了
            # ⑤ 系统优化项：档案里记的是「导出时**真正生效**的项」，
            #    不是勾选状态 —— 所以按本机实测算出「还差哪些」，再问一次是否补齐。
            #    （用户反馈「用备份文件一键调系统，覆盖不了已优化项」就是这个缺口。）
            opt_msg = ""
            if n_opt:
                opt_ids = [str(x) for x in (prof.get("optimize_applied") or [])]
                miss = S.optimize_missing(opt_ids) if opt_ids else []
                if not miss:
                    opt_msg = ("\n\n系统优化：档案里的 %d 项在本机都已生效，无需补齐。"
                               % len(opt_ids))
                else:
                    lines = ["%s%s" % (r.get("name", r["id"]),
                                       "　【策略】" if r.get("risk") == "policy" else "")
                             for r in miss[:14]]
                    more = ("\n…还有 %d 项" % (len(miss) - 14)) if len(miss) > 14 else ""
                    if confirm(self, "顺带补齐系统优化？",
                               "档案里记着 %d 项已生效的优化，其中 **%d 项在本机还没生效**：\n\n%s%s"
                               "\n\n现在一并应用吗？（会先自动记录原值，可用「撤销上次优化」回退）"
                               % (len(opt_ids), len(miss), "\n".join(lines), more)):
                        self.win.busy("正在补齐系统优化…")
                        rr = S.snapshot_and_apply(S.load_rules(),
                                                  [r["id"] for r in miss])
                        self.win.idle()
                        bad = [x for x in (rr or []) if not x.get("ok")]
                        opt_msg = "\n\n系统优化：补应用 %d 项%s" % (
                            len(miss) - len(bad),
                            ("，**%d 项失败**（详见「撤销上次优化」处的记录）"
                             % len(bad)) if bad else "，全部成功")
                        self.win.notify_all_changed()
                    else:
                        opt_msg = ("\n\n系统优化：**没有补齐**（你在提示里选了「取消」）"
                                   "—— 现有设置未改动。")
            # ⑥ 启动项 / 服务 / 计划任务：档案里只有「被本工具禁用过」的**凭据** ——
            #    那些禁用动作改的是 Windows 自己的状态（StartupApproved / Start / Enabled），
            #    换台机器那边只会看到「都是启用的」。这里按本机实况算差集，只列还没禁的。
            rec_msg = ""
            if n_reg:
                pend = S.disabled_records_pending()
                if pend:
                    kind_cn = {"startup": "开机启动项", "service": "服务",
                               "task": "计划任务"}
                    lines = ["[%s] %s" % (kind_cn.get(d.get("kind"), d.get("kind")),
                                          S.disabled_record_label(d.get("kind"), d))
                             for d in pend[:14]]
                    more = ("\n…还有 %d 项" % (len(pend) - 14)) if len(pend) > 14 else ""
                    if confirm(self, "顺带恢复「被禁用」的项？",
                               "档案里记着 %d 项曾在本工具里被**禁用**，其中 %d 项"
                               "在本机还是启用状态：\n\n%s%s\n\n"
                               "现在按记录把它们也禁掉吗？（只动这几项，可随时在"
                               "「启动与服务」页重新启用）"
                               % (sum(len(v) for v in
                                      (S.disabled_records().values())),
                                  len(pend), "\n".join(lines), more)):
                        self.win.busy("正在按记录禁用…")
                        rr = S.disabled_records_apply(pend)
                        self.win.idle()
                        bad = [x for x in rr if not x.get("ok")]
                        rec_msg = "\n\n启动项/服务/任务：补禁用 %d 项%s" % (
                            len(rr) - len(bad),
                            ("，**%d 项失败**（可能本机没有这一项）" % len(bad))
                            if bad else "，全部成功")
                    else:
                        rec_msg = ("\n\n启动项/服务/任务：**没有**按记录禁用"
                                   "（你在提示里选了「取消」）。")
                elif any(S.disabled_records().values()):
                    rec_msg = ("\n\n启动项/服务/任务：档案里记的项，本机都已是禁用状态。")
            info(self, "导入完成",
                 "设置已应用。" + ("\n\n当前配置已另存到：\n%s" % bk if bk
                                   else "\n\n（当前配置没能写成恢复副本）")
                 + reg_msg + opt_msg + rec_msg
                 # 旧版文件不带注册表状态 —— 完成提示里也说一句，
                 # 免得用户以为「怎么右键菜单还是没恢复」
                 + ("\n\n（这是**旧版**导出的文件 —— 只含设置，没带注册表状态。）"
                    if prof.get("_legacy") else ""))
        except Exception as e:
            warn(self, "导入失败", str(e))

    def on_startup(self, v):
        ok = set_startup(bool(v))
        self.win.settings["startup"] = bool(v)
        if not ok:
            self.cb_startup.setChecked(False)
            warn(self, "设置开机启动失败（需要管理员权限写入注册表）")

    def on_close_action(self, _ix):
        """关闭窗口时的行为：每次询问 / 最小化到托盘 / 直接退出。"""
        act = self.combo_close.currentData() or "ask"
        self.win.settings["close_action"] = act
        self.win.settings["minimize_to_tray"] = (act == "tray")

# ==========================================================================
# 主窗口
# ==========================================================================
def resolve_dark(mode, fallback=False):
    """把主题模式解析成具体深浅。

    `system` —— 先问 Qt 的 `styleHints().colorScheme()`（Qt 6.5+ 在 Windows 上
    由 WM_SETTINGCHANGE 驱动），拿不到再读注册表的 `AppsUseLightTheme` 兜底；
    两者都拿不到就用 `fallback`（本次启动原来的样子）。
    """
    if mode == "dark":
        return True
    if mode == "light":
        return False
    try:
        cs = QApplication.instance().styleHints().colorScheme()
        if cs == Qt.ColorScheme.Dark:
            return True
        if cs == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    v = S.system_uses_dark()
    return bool(v) if v is not None else bool(fallback)


class ToolHubPage(Page):
    """「清理与诊断」大类：一行三个格子，点进去用对应的工具。

    垃圾清理 / 注册表清理 / 系统组件 / 策略诊断这 4 个页都还在，只是
    **不再单独占侧栏一项** —— 侧栏 16 项太长，它们又都是「偶尔用一次」的，
    收进一个大类里更短也更好找。点格子进去之后，侧栏高亮会停在本大类上。
    """
    title = "清理与诊断"

    # (页 key, 图标, 名称, 一句话说明) —— 顺序即格子顺序，一行三个
    TOOLS = (
        ("cleanup", "\U0001f9f9", "垃圾清理",
         "临时文件、浏览器缓存、系统缓存、回收站、Windows 更新残留 —— "
         "先只读扫描再列清单，可送回收站"),
        ("regtool", "\U0001f5c2", "注册表清理",
         "无效项清理、右键菜单管理、备份与还原、快捷方式修复；"
         "删之前先导 .reg 备份"),
        ("systool", "\U0001f9e9", "系统组件",
         "程序组件、浏览器扩展、文件签名校验；"
         "禁用会先备份到影子区，可一键还原"),
        ("policies", "\U0001f6e1", "策略诊断",
         "组策略 / 注册表策略体检：哪些策略被改过、被谁改的、当前生效值"),
    )

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)
        root.addWidget(notice(
            "这四类工具都收在这里，<b>点任意一个格子进去</b>。<br>"
            "进去之后左侧栏的高亮会停在本大类上，点「清理与诊断」就能回到这一页。",
            "info"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        for i, (key, icon, name, desc) in enumerate(self.TOOLS):
            grid.addWidget(self._hub_card(key, icon, name, desc), i // 3, i % 3)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        root.addLayout(grid)
        root.addStretch(1)

    def _hub_card(self, key, icon, name, desc):
        f = QFrame()
        f.setObjectName("HubCard")
        f.setCursor(Qt.PointingHandCursor)
        f.setMinimumHeight(116)
        v = QVBoxLayout(f)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        ic = QLabel(icon)
        ic.setObjectName("HubIcon")
        top.addWidget(ic)
        t = QLabel(name)
        t.setObjectName("HubTitle")
        top.addWidget(t)
        top.addStretch(1)
        ar = QLabel("\u203a")
        ar.setObjectName("CatArrow")
        top.addWidget(ar)
        v.addLayout(top)
        d = QLabel(desc)
        d.setObjectName("HubDesc")
        d.setWordWrap(True)
        v.addWidget(d)
        v.addStretch(1)
        # 本项目的既有写法：直接挂 mousePressEvent（分类卡片也是这么做的）
        f.mousePressEvent = lambda _e, k=key: self.win.goto(k)
        f.setToolTip("点击进入「%s」" % name)
        return f


class MainWindow(FramelessWindow):
    NAV = [("overview", "概览", OverviewPage),
           ("optimize", "系统优化", OptimizePage),
           ("cputune", "CPU 调试", CpuTunePage),
           ("power", "电源管理", PowerPage),
           ("gpu", "显卡伪装", GpuPage),
           ("software", "软件管理", SoftwarePage),
           ("startup", "启动与服务", StartupPage),
           ("performance", "内存与性能", PerfPage),
           ("network", "网络修复", NetworkPage),
           ("dns", "DNS 检测", DNSPage),
           ("cleanup", "垃圾清理", CleanupPage),
           ("tools", "磁盘工具", DiskToolsPage),
           ("hub", "清理与诊断", ToolHubPage),
           ("regtool", "注册表清理", RegistryToolsPage),
           ("systool", "系统组件", SysToolsPage),
           ("policies", "策略诊断", PoliciesPage),
           ("settings", "设置", SettingsPage)]

    # 这些页**不单独占侧栏一项**，从「清理与诊断」(hub) 进去。
    # ⚠️ 但它们**必须留在 NAV 里**：stack 的下标与 NAV 下标一一对应
    # （侧栏按钮 id → stack 页），`goto(key)` 也是按 NAV 找目标页的。
    # 所以做法是「保留在 NAV、建侧栏按钮时跳过」，而不是从 NAV 删掉。
    HIDDEN_NAV = ("cleanup", "regtool", "systool", "policies")
    HUB_KEY = "hub"
    # 从隐藏页进去时侧栏高亮停在哪（否则会出现「没有任何一项高亮」的空状态）
    HUB_OF = {"cleanup": "hub", "regtool": "hub",
              "systool": "hub", "policies": "hub"}

    def __init__(self, settings=None, admin_mode=False, dark=False, sysinfo=None):
        super().__init__()
        self.admin_mode = admin_mode
        _st = settings if settings is not None else {}
        # 主题模式：system（跟随系统）/ light / dark。
        # 旧版只有一个 dark 布尔键，这里做向后兼容 —— 没写 theme 就按它推。
        self.theme_mode = _st.get("theme") or ("dark" if dark else "light")
        self.dark = resolve_dark(self.theme_mode, fallback=bool(dark))
        # 主题色："" = 跟随系统强调色。必须在 _apply_qss 之前写入全局状态。
        self.accent = _st.get("accent") or ""
        T.set_accent(self.accent)
        self.sysinfo = sysinfo or probe_system_info()
        # 字号固定跟随系统缩放，不再随窗口面积变化（避免缩放后文字显示不全）
        self.scale = 1.0
        self.font_base = self.sysinfo["font_base"]
        self.setWindowTitle("%s — 系统工具箱 v%s" % (APP_NAME, APP_VER))
        # 固定窗口尺寸：自动适配屏幕可用区域（逻辑像素，Qt 按系统缩放渲染）
        sg = QApplication.primaryScreen().availableGeometry()
        self._base_w = min(self._base_w_for(sg.width()), int(sg.width() * 0.86))
        self._base_h = min(self._base_h_for(sg.height()), int(sg.height() * 0.88))
        self.resize(self._base_w, self._base_h)
        self.setMinimumSize(min(760, self._base_w), min(540, self._base_h))
        self.setWindowIcon(self._make_icon())
        self.settings = _st
        self._closing = False
        self._mon_paused = None   # None=未判定；窗口不可见时 True（后台监测已停）
        self._is_max = False
        self._init_tray()

        # 外壳（圆角卡片）
        shell = QFrame()
        shell.setObjectName("Shell")
        self.setCentralWidget(shell)
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- 自绘标题栏 ----
        # 标题栏与按钮尺寸随系统缩放变化，保证任意缩放下点击热区与文字都完整
        _bs = self.sysinfo.get("scale", 1.0)
        _btn_w = max(34, int(42 * _bs))
        _btn_h = max(24, int(30 * _bs))
        tb = QWidget()
        tb.setObjectName("TitleBar")
        tb.setFixedHeight(max(32, int(44 * _bs)))
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(16, 0, 8, 0)
        tbl.setSpacing(8)
        ticon = QLabel()
        ticon.setPixmap(self.windowIcon().pixmap(20, 20))
        self._title_icon = ticon          # 换主题色后要重画，见 _refresh_skin
        tbl.addWidget(ticon)
        ttext = QLabel("%s — 系统工具箱 v%s" % (APP_NAME, APP_VER))
        ttext.setObjectName("TitleText")
        tbl.addWidget(ttext)
        tbl.addStretch(1)
        self.btn_theme = QPushButton("🌙" if not self.dark else "☀")
        self.btn_theme.setObjectName("TitleBtn")
        self.btn_theme.setToolTip("切换浅色 / 深色主题")
        self.btn_theme.setFixedSize(_btn_w, _btn_h)
        self.btn_theme.clicked.connect(self.toggle_theme)
        tbl.addWidget(self.btn_theme)
        b_min = QPushButton("—")
        b_min.setObjectName("TitleBtn")
        b_min.clicked.connect(lambda: self.showMinimized())
        self.btn_full = QPushButton("□")
        self.btn_full.setObjectName("TitleBtn")
        self.btn_full.setToolTip("最大化 / 还原（保留任务栏）")
        self.btn_full.clicked.connect(self.toggle_maximized)
        b_cls = QPushButton("✕")
        b_cls.setObjectName("TitleBtnClose")
        b_cls.clicked.connect(self.close)
        for b in (b_min, self.btn_full, b_cls):
            b.setFixedSize(_btn_w, _btn_h)
            tbl.addWidget(b)
        outer.addWidget(tb)

        # ---- 主体 ----
        body = QWidget()
        h = QHBoxLayout(body); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        outer.addWidget(body, 1)

        nav = QFrame(); nav.setObjectName("Nav")
        nav.setFixedWidth(max(190, int(228 * _bs)))
        nv = QVBoxLayout(nav); nv.setContentsMargins(10, 12, 10, 12); nv.setSpacing(2)
        brand = QHBoxLayout(); brand.setSpacing(10)
        logo = QLabel("W"); logo.setObjectName("Logo")
        bt = QVBoxLayout(); bt.setSpacing(0)
        t1 = QLabel(APP_NAME); t1.setObjectName("Brand")
        t2 = QLabel("系统工具箱 v" + APP_VER); t2.setObjectName("BrandSub")
        bt.addWidget(t1); bt.addWidget(t2)
        brand.addWidget(logo); brand.addLayout(bt); brand.addStretch(1)
        bw = QWidget(); bw.setLayout(brand)
        nv.addWidget(bw)
        nv.addSpacing(12)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.stack = QStackedWidget()
        self.pages = {}
        self.scrolls = {}
        for i, (key, name, cls) in enumerate(self.NAV):
            if key not in self.HIDDEN_NAV:
                b = QPushButton(name)
                b.setObjectName("NavItem")
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                # 按钮 id = NAV 下标（跳过隐藏项后 id 不连续，QButtonGroup 允许）
                self.group.addButton(b, i)
                nv.addWidget(b)
            page = cls(self)
            self.pages[key] = page
            # 每个页面套滚动区：窗口变小时内容保持自然尺寸，超出部分滚动，
            # 避免布局把标签压到 0 高度导致文字消失（窗口缩放适配的关键）
            sa = QScrollArea()
            sa.setObjectName("PageScroll")
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.NoFrame)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            # 关键：viewport 也要吃 QSS 背景，否则露出 Qt 默认浅灰
            sa.viewport().setObjectName("PageViewport")
            sa.viewport().setAttribute(Qt.WA_StyledBackground, True)
            sa.setWidget(page)
            self.scrolls[key] = sa
            self.stack.addWidget(sa)
        nv.addStretch(1)
        self.admin_lb = QLabel("权限检测中…"); self.admin_lb.setObjectName("Muted3")
        self.admin_lb.setWordWrap(True)
        nv.addWidget(self.admin_lb)
        h.addWidget(nav)

        right = AtmosphereArea(self)      # 内容区底：Mica 氛围层（所有页面共用）
        right.setAttribute(Qt.WA_StyledBackground, True)
        rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(0)
        top = QFrame(); top.setObjectName("TopBar"); top.setFixedHeight(52)
        tv = QHBoxLayout(top); tv.setContentsMargins(20, 0, 20, 0); tv.setSpacing(10)
        # 二级页（从大类进去的）在这里给一个「← 返回 …」。
        # 放在顶部栏而不是各页自己加：一处实现，所有二级页（含以后新增的）都有，
        # 样式也统一；各页自己加迟早会漏一两个。
        self.btn_back_page = btn("← 返回", on_click=self.go_back)
        self.btn_back_page.setVisible(False)
        self.btn_back_page.setToolTip("回到上一个层级")
        tv.addWidget(self.btn_back_page)
        self.title_lb = QLabel("概览"); self.title_lb.setObjectName("PageTitle")
        tv.addWidget(self.title_lb); tv.addStretch(1)
        self.spinner = QLabel(""); self.spinner.setObjectName("Muted")
        tv.addWidget(self.spinner)
        self.pill_admin = pill("—", "")
        tv.addWidget(self.pill_admin)
        tv.addWidget(btn("刷新", on_click=self.on_refresh))
        rv.addWidget(top)
        rv.addWidget(self.stack, 1)
        h.addWidget(right, 1)

        self.group.idClicked.connect(self.goto_index)

        self._admin_label()
        self._apply_qss()
        self.goto("overview")   # 每次打开都从概览页开始，不再恢复上次的页面
        # 预热系统信息（首次要起一次 PowerShell，约 1.8s）：进概览页时立刻有内容
        Tasks.run(self, S.sys_basic, lambda *_: None,
                  on_fail=lambda *_: None)  # 刻意静默：预热，结果本就丢弃
        # 启动检查更新：延后 3.5s（让首屏那批请求先跑完），有新版才提示
        QTimer.singleShot(3500, self._startup_update_check)
        # 只读快照当前电源计划：**同步读一次注册表**（微秒级，不起进程），
        # 不是异步任务 —— 免得为了它多一个静默失败点。
        # 用途：运行期间若计划被换掉、又不是在本软件里点的切换，电源页会如实提示。
        try:
            self.plan_at_start = S.power_active_snapshot()
        except Exception:
            self.plan_at_start = None
        # 「跟随系统」时监听系统深浅色变化（事件驱动，无常驻轮询）
        self._watch_system_theme()
        self._sync_theme_controls()

    # ---------- 初始窗口尺寸（逻辑像素恒定，物理占比由 dpr 统一缩放） ----------
    def _base_w_for(self, avail_w):
        """逻辑像素下的目标宽度：基准不乘系统缩放 —— QSS 逻辑 px 会被 dpr 自动
        放大成物理尺寸，再乘 scale 会让窗口物理占屏暴涨（2K+150% 时 86%→近全屏）。"""
        sc = self.sysinfo.get("scale", 1.0)
        ratio = 0.92 if sc <= 1.0 else (0.86 if sc <= 1.5 else 0.80)
        return min(1180, int(avail_w * ratio))

    def _base_h_for(self, avail_h):
        sc = self.sysinfo.get("scale", 1.0)
        ratio = 0.92 if sc <= 1.0 else (0.86 if sc <= 1.5 else 0.82)
        return min(780, int(avail_h * ratio))

    # ---------- helpers ----------
    def _make_icon(self):
        pm = QPixmap(64, 64); pm.fill(Qt.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(QColor(T.get_color("BLUE", self.dark)))
        p.drawRoundedRect(2, 2, 60, 60, 14, 14)
        f = QFont("Segoe UI Variable Display", 34); f.setBold(True)
        p.setFont(f); p.setPen(QColor("white"))
        p.drawText(pm.rect(), Qt.AlignCenter, "W")
        p.end()
        return QIcon(pm)

    def _init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.windowIcon())
        self.tray.setToolTip("%s v%s" % (APP_NAME, APP_VER))
        menu = QMenu()
        menu.addAction("设置", self._open_settings)
        menu.addSeparator()
        menu.addAction("退出", self._real_close)
        # 菜单文字/背景跟随主题（否则深色主题下白字白底看不见）
        style_menu(menu, self.dark)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _open_settings(self):
        self.showNormal()
        self._is_max = False
        self._sync_max_flag()
        self.raise_()
        self.goto("settings")

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()
            self._is_max = False
            self._sync_max_flag()
            self.raise_()

    def _real_close(self):
        self._closing = True
        self.close()

    def _admin_label(self):
        if self.admin_mode:
            self.admin_lb.setText("权限：管理员 ✓")
            self.pill_admin.setText("管理员权限")
            self.pill_admin.setObjectName("PillOk")
        else:
            self.admin_lb.setText("权限：普通（写入类操作已禁用）")
            self.pill_admin.setText("普通模式")
            self.pill_admin.setObjectName("PillWarn")
        self.pill_admin.style().unpolish(self.pill_admin)
        self.pill_admin.style().polish(self.pill_admin)

    # ---------- 主题模式：跟随系统 / 浅色 / 深色 ----------
    def set_theme_mode(self, mode):
        """设置主题模式：system / light / dark（未知值按 system 处理）。"""
        self.theme_mode = mode if mode in ("system", "light", "dark") else "system"
        self.settings["theme"] = self.theme_mode
        self._apply_theme_mode()

    def toggle_theme(self):
        """标题栏的快捷按钮：在浅 / 深之间直接切（会退出「跟随系统」）。"""
        self.set_theme_mode("light" if self.dark else "dark")

    def _apply_theme_mode(self):
        """按当前模式解析深浅并应用；系统换色时也走这里。"""
        d = resolve_dark(self.theme_mode, fallback=self.dark)
        self.settings["dark"] = d          # 兼容旧键
        if d != self.dark:
            self.dark = d
            self._apply_qss()              # QSS + 勾选图标重画
            self._refresh_skin()           # 窗口图标 / 托盘
            self._reskin_tree()            # 自绘控件按新主题重取颜色
        self._sync_theme_controls()

    def _reskin_tree(self):
        """把「代码画的」控件切到当前主题。

        环形表 / 迷你折线 / 雷达这些在构造时就把颜色取好了，只换 QSS 不会更新它们。
        这里逐个 set_dark，而不是整页重建 —— 页面可能正在异步加载数据，
        重建会打到已销毁的控件上（Qt 里表现为崩溃）。
        """
        for page in getattr(self, "pages", {}).values():
            for w in page.findChildren(QWidget):
                fn = getattr(w, "set_dark", None)
                if callable(fn):
                    try:
                        fn(self.dark)
                    except Exception:
                        pass

    def _sync_theme_controls(self):
        """标题栏按钮与设置页下拉同步到当前模式。"""
        bt = getattr(self, "btn_theme", None)
        if bt is not None:
            cur = "深色" if self.dark else "浅色"
            bt.setText("☀" if self.dark else "🌙")
            bt.setToolTip({"system": "跟随系统（当前 %s）—— 点击切到浅色" % cur
                           if self.dark else
                           "跟随系统（当前 %s）—— 点击切到深色" % cur,
                           "light": "浅色主题 —— 点击切到深色",
                           "dark": "深色主题 —— 点击切到浅色"}.get(self.theme_mode, ""))
        sp = getattr(self, "pages", {}).get("settings")
        combo = getattr(sp, "combo_theme", None)
        if combo is not None:
            ix = combo.findData(self.theme_mode)
            if ix >= 0 and ix != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(ix)
                combo.blockSignals(False)

    def _watch_system_theme(self):
        """跟随系统时监听深浅色变化 —— 事件驱动，不轮询、不占资源。"""
        try:
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                self._on_system_scheme_changed)
        except Exception:
            pass

    def _on_system_scheme_changed(self, *_a):
        if self.theme_mode == "system":
            self._apply_theme_mode()

    def set_accent(self, value):
        """设置界面主题色。value 为 '' / None 时表示跟随系统强调色。"""
        v = T.set_accent(value)
        self.accent = v or ""
        self.settings["accent"] = self.accent
        # _apply_qss 会重画勾选图标（文件名带主题色指纹）并重新 polish 全部控件
        self._apply_qss()
        self._refresh_skin()
        sp = getattr(self, "pages", {}).get("settings")
        if sp is not None:
            # 只重画色块选中态。**不要**整页 refresh —— 那会顺带
            # `load_backups()`（每次都起一个后台任务），连续切主题色时纯属堆积。
            if hasattr(sp, "_paint_swatches"):
                sp._paint_swatches()
            elif hasattr(sp, "refresh"):
                sp.refresh()

    def _refresh_skin(self):
        """浅/深色 或 主题色变更后，刷新所有「用代码画的」皮肤元素。"""
        self.setWindowIcon(self._make_icon())
        ti = getattr(self, "_title_icon", None)
        if ti is not None:
            ti.setPixmap(self.windowIcon().pixmap(20, 20))
        if getattr(self, "tray", None):
            self.tray.setIcon(self.windowIcon())

    def _apply_qss(self):
        # ⚠️ 拆成两条：faulthandler 只能报到行，合成一行时分不清崩在
        #    `build_qss`（里面要画 PNG：QImage/QPainter/save）还是 `setStyleSheet`。
        _qss = T.build_qss(self.dark, self.scale, self.font_base)
        self.setStyleSheet(_qss)
        # QSS 的 font-size 不会刷新布局缓存，样式重建后要重新校正标签度量；
        # 布局需要两轮才稳定（首轮 width 可能为 0），所以分两轮做。
        #
        # ⚠️ 必须**防抖**：早先是每次直接排两个 `QTimer.singleShot`。
        #    连续调用时（比如逐个试 10 个主题色）会堆 20 个回调，
        #    而 `_refit_fonts` 会改控件树（setMinimumHeight /
        #    updateGeometry），与 `setStyleSheet` 正在进行的样式重画交错 →
        #    **实测崩在 setStyleSheet 里（0xC0000005 访问冲突）**。
        #    改成「停止再启动」的单次定时器，把突发调用合并成一组。
        #    现在**两轮都走单次定时器**：第二轮以前用 `QTimer.singleShot` 排，
        #    连续换肤时仍会叠出多次全树 `fit_labels`（3545 控件 / 943 标签）。
        self._arm_refit()

    def _arm_refit(self):
        """排两轮字体校正（首轮控件 width 可能还是 0）。两轮都防抖。"""
        pair = getattr(self, "_refit_pair", None)
        if pair is None:
            pair = []
            for delay in (40, 160):
                t = QTimer(self)
                t.setSingleShot(True)
                t.timeout.connect(self._refit_fonts)
                pair.append((t, delay))
            self._refit_pair = pair
        for t, delay in pair:
            t.stop()
            t.start(delay)

    def _refit_fonts(self):
        """样式重建后按真实字体度量修正所有标签，避免文字被裁。"""
        try:
            fit_labels(self)
            for page in getattr(self, "pages", {}).values():
                fit_labels(page)
        except Exception:
            pass

    # ---------- 窗口缩放：字号跟随系统缩放，不随窗口面积变化 ----------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_scale()

    def _update_scale(self):
        """字号只与系统缩放有关：窗口化 = 系统字号，最大化可略微放大（上限 1.25）。"""
        if self.isMaximized():
            new_scale = min(1.25, 1.0 + 0.1 * max(0.0, self.sysinfo.get("scale", 1.0) - 1.0))
        else:
            new_scale = 1.0
        if abs(new_scale - self.scale) > 0.01:
            self.scale = new_scale
            self._apply_qss()
        # 同步最大化/还原按钮图标与外壳圆角
        self._sync_max_flag()

    def _warn_readonly(self):
        warn(self, "当前为普通模式，写入系统设置的选项已被禁用。\n\n"
                  "如需修改服务 / 注册表 / 策略 / 网络修复，请在启动时选择「管理员模式」并允许 UAC 提权。")

    # ---------- 检查更新（有新版才提示，否则完全安静） ----------
    def _startup_update_check(self):
        """启动自动检查：每天最多一次；设置里可关；失败零打扰。"""
        if not self.settings.get("check_update", True):
            return
        today = time.strftime("%Y-%m-%d")
        if self.settings.get("update_checked_on") == today:
            return
        self.settings["update_checked_on"] = today

        def done(r):
            r = r or {}
            if r.get("ok") and r.get("has_new"):
                self._show_update_dialog(r)     # 没新版 / 网络失败：什么都不做

        Tasks.run(self, lambda: S.check_update(APP_VER), done,
                  on_fail=lambda *_: None)  # 刻意静默：检查更新失败零打扰（有意为之）

    def _show_update_dialog(self, r):
        """新版提示：去下载（打开 GitHub Release）/ 以后再说。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        dlg = QMessageBox(self)
        dlg.setWindowTitle("发现新版本")
        dlg.setIcon(QMessageBox.Information)
        dlg.setText("WinToolbox v%s 已发布（当前 v%s）。"
                    % (r.get("latest") or "?", APP_VER))
        notes = (r.get("notes") or "").strip()
        dlg.setInformativeText(notes[:600] if notes else "可到 GitHub 下载新版本。")
        b_go = dlg.addButton("去下载", QMessageBox.AcceptRole)
        dlg.addButton("以后再说", QMessageBox.RejectRole)
        _center_dialog(dlg)
        dlg.exec()
        _go = dlg.clickedButton() is b_go      # 先取值，再释放对话框
        dlg.deleteLater()
        if _go:
            QDesktopServices.openUrl(QUrl(r.get("url") or GITHUB_URL))

    def check_update_now(self, manual=True):
        """手动检查（设置页按钮）：不受每日一次限制，结果如实反馈。

        多通道回退最多要跑 20 秒左右，所以先给个忙碌提示，否则用户会以为没反应。
        """
        self.busy("正在检查更新…")

        def done(r):
            self.idle()
            r = r or {}
            if not r.get("ok"):
                tried = r.get("tried") or []
                detail = "\n".join("· " + t for t in tried[-5:]) or "· （没有可用通道）"
                info(self, "检查更新失败",
                     "所有通道都没能连上 GitHub。\n\n已尝试：\n%s\n\n"
                     "如果你能用浏览器打开 GitHub，说明本机有代理，但本程序没走到 ——\n"
                     "请把代理软件的「系统代理」打开，或把它的 HTTP 端口告诉我。\n\n"
                     "详细过程：%s"
                     % (detail, os.path.join("data", "update_check.log")))
            elif r.get("has_new"):
                self._show_update_dialog(r)
            else:
                info(self, "已是最新版本",
                     "当前 v%s 已是最新版本。\n\n（本条通过「%s」获取）"
                     % (APP_VER, r.get("via") or "GitHub"))

        Tasks.run(self, lambda: S.check_update(APP_VER), done,
                  on_fail=lambda m: (self.idle(),
                                     info(self, "检查更新失败", _friendly_net_err(m)))[1])

    def goto_index(self, i):
        self.goto(self.NAV[i][0])

    def goto(self, key):
        for i, (k, _, _) in enumerate(self.NAV):
            if k == key:
                # 隐藏页从大类进去：侧栏高亮停在它所属的大类上，
                # 否则会出现「整列没有一项高亮」的空状态（用户会以为跑丢了）
                hl = self.HUB_OF.get(k, k)
                for j, (k2, _, _) in enumerate(self.NAV):
                    if k2 == hl:
                        b = self.group.button(j)
                        if b is not None:      # 隐藏页没有按钮
                            b.setChecked(True)
                        break
                self.stack.setCurrentIndex(i)
                self.title_lb.setText(self.NAV[i][1])
                # 二级页：显示「← 返回 <大类名>」
                _par = self.HUB_OF.get(k)
                if _par:
                    _pn = dict((_x[0], _x[1]) for _x in self.NAV).get(_par, "上一级")
                    self.btn_back_page.setText("← 返回%s" % _pn)
                    self.btn_back_page.setVisible(True)
                else:
                    self.btn_back_page.setVisible(False)
                break
        page = self.pages.get(key)
        if page:
            def _show():
                page.first_show()
                fit_labels(page)
            QTimer.singleShot(0, _show)

    def go_back(self):
        """回到当前页所属的大类（二级页专用）。"""
        key = self.NAV[self.stack.currentIndex()][0]
        self.goto(self.HUB_OF.get(key, "overview"))

    def on_refresh(self):
        page = self.pages.get(self.NAV[self.stack.currentIndex()][0])
        if page:
            page.reload()          # 手动刷新 = 丢掉会话缓存，真的重读一遍

    def notify_changed(self, *keys):
        """某页做了写操作后，让受影响的大类丢缓存重读系统状态。

        只刷新已经加载过的页：没打开过的页会话缓存本来就是空的，
        首次进入自然读到最新值，提前 reload 只会白起一串子进程。
        """
        for k in keys:
            p = self.pages.get(k)
            if p is not None and getattr(p, "loaded", False):
                try:
                    p.reload()
                except Exception:
                    pass

    def notify_all_changed(self):
        """恢复设置这类全局操作：所有已加载页全部重读。"""
        self.notify_changed(*self.pages.keys())

    def busy(self, msg="处理中…"):
        self.spinner.setText("⏳ " + msg)

    def idle(self):
        self.spinner.setText("")

    # ---------- 退出时自动保存 / 最小化到托盘 ----------
    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            self._sync_monitor_pause()
        elif e.type() == QEvent.ThemeChange and self.theme_mode == "system":
            # Qt 在系统换深浅色时也会发 ThemeChange（比 colorSchemeChanged 更早），
            # 两条都接上：哪条先到都行，重复触发也只是「值没变 → 什么都不做」。
            self._apply_theme_mode()
        super().changeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        # 首次显示时确保样式（字号/DPI）已按本窗口的 scale/font_base 应用。
        # ⚠️ 这个类里以前 showEvent 定义了两次（构造函数后面还有一份），
        # Python 只保留最后一个 → 那段 _qss_ready 逻辑一直是死代码，已合并到这里。
        if not getattr(self, "_qss_ready", False):
            self._qss_ready = True
            self._apply_qss()
        self._sync_monitor_pause()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_monitor_pause()

    # ---------- 关闭行为 / 窗口可见性 → 后台监测开关 ----------
    def _close_action(self):
        """关闭窗口时怎么做：ask（每次询问）/ tray / exit。

        兼容早期只有一个「关闭时最小化到系统托盘」复选框时存下的设置。
        """
        act = self.settings.get("close_action")
        if act in ("ask", "tray", "exit"):
            return act
        return "tray" if self.settings.get("minimize_to_tray") else "ask"

    @staticmethod
    def _close_pick(clicked, b_tray, b_exit, b_cancel=None):
        """把「点了哪个按钮」翻译成动作：tray / exit / ask（ask = 不关闭）。
    
        点右上角 ×、按 Esc、点「取消」三种情况都算「不关闭」：Qt 在**没有** 
        RejectRole 按钮时给 clickedButton()=None，**有**的时候则把 × 映射到那个按钮，
        所以这里用「只认两个主动作，其余一律不关闭」的写法，任何意外都落到取消上。
        """
        if clicked is b_tray:
            return "tray"
        if clicked is b_exit:
            return "exit"
        return "ask"
    
    def _ask_close_action(self):
        """问一次：最小化到托盘，还是直接退出。返回 ask 表示用户取消了关闭。"""
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Question)
        m.setWindowTitle("关闭 %s" % APP_NAME)
        m.setText("要最小化到系统托盘，还是直接退出程序？")
        m.setInformativeText(
            "最小化到托盘 —— 窗口收进托盘，<b>性能监测暂停</b>"
            "（核心调度等已经写入系统的设置不受影响），双击托盘图标即可恢复。\n"
            "直接退出 —— 完全关闭本程序。\n"
            "（点右上角 ×、按 Esc 或点「取消」都只是关掉本对话框，不会关闭软件。）")
        b_tray = m.addButton("最小化到托盘", QMessageBox.AcceptRole)
        b_exit = m.addButton("退出程序", QMessageBox.DestructiveRole)
        # 「取消」既是显式选项，也是 Esc 与右上角 × 的落点 —— 三者都表示「不关闭」。
        # （Qt 只有在没有 RejectRole 按钮时，点 × 才会给出 clickedButton()=None；
        #   一旦有，它就把 × 映射到那个按钮上，所以不能用 None 来判「取消」。）
        b_cancel = m.addButton("取消", QMessageBox.RejectRole)
        m.setEscapeButton(b_cancel)
        m.setDefaultButton(b_tray)
        cb = QCheckBox("记住我的选择，下次不再询问")
        m.setCheckBox(cb)
        _center_dialog(m)
        m.exec()
        act = self._close_pick(m.clickedButton(), b_tray, b_exit, b_cancel)
        remember = cb.isChecked()      # 先取值：下面 deleteLater 之后不再碰控件
        m.deleteLater()                # 不把隐藏的模态框留在控件树里
        if act == "ask":      # 取消（含 Esc / 右上角 ×）：不记住、不关闭
            return "ask"
        if remember:
            self.settings["close_action"] = act
            self.settings["minimize_to_tray"] = (act == "tray")
            try:
                settings_save(self.settings)
            except Exception:
                pass
        return act

    def _sync_monitor_pause(self):
        """窗口被最小化 / 收进托盘 → 暂停后台监测；重新显示 → 恢复。

        页面的 1 秒轮询靠 page_active() 自己就停了，这里管的是服务端那两个常驻
        采样线程（温度 / GPU / 网络速率）—— 让它们真正停下，而不是降频空转。
        """
        try:
            paused = bool(self.isMinimized() or self.isHidden())
        except Exception:
            return
        prev = getattr(self, "_mon_paused", None)
        if paused == prev:
            return
        self._mon_paused = paused
        try:
            S.set_monitor_paused(paused)
        except Exception:
            pass
        if not paused and prev is True:
            self._kick_visible_page()

    def _kick_visible_page(self):
        """恢复窗口时立刻把当前页的实时数据补一次，别让用户盯着暂停前的旧数字。"""
        try:
            page = self.pages.get(self.NAV[self.stack.currentIndex()][0])
        except Exception:
            return
        if page is None:
            return
        for name in ("tick", "tick_perf", "tick_net"):
            fn = getattr(page, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def closeEvent(self, e):
        if self._closing:
            self._quit_now(e)
            return
        act = self._close_action()
        if act == "ask":
            act = self._ask_close_action()
            if act == "ask":              # 用户取消了关闭
                e.ignore()
                return
        if act == "tray":
            e.ignore()
            self.hide()
            self._sync_monitor_pause()    # 收进托盘 → 停掉后台监测
            tray = getattr(self, "tray", None)
            if tray is not None:
                tray.showMessage(
                    APP_NAME,
                    "已最小化到系统托盘，性能监测已暂停；双击图标即可恢复。",
                    QSystemTrayIcon.Information, 1800)
            return
        self._quit_now(e)

    def _quit_now(self, e):
        """真正退出：保存状态、通知各页、收起托盘图标。"""
        try:
            for page in self.pages.values():
                if hasattr(page, "on_exit"):
                    page.on_exit()
            self.settings.pop("last_page", None)   # 打开固定显示概览页
            self.settings["window"] = {"w": self.width(), "h": self.height()}
            settings_save(self.settings)
            if getattr(self, "tray", None):
                self.tray.hide()
        except Exception:
            pass
        super().closeEvent(e)


def main():
    # high-DPI：PassThrough 让 dpr 保持真实值（如 150% → 1.5）。
    # 本应用的缩放模型是「手动缩放」：窗口尺寸与 QSS 字号都已按系统缩放（scale/font_base）
    # 预先放大，逻辑空间必须等于 物理/真实缩放 才能比例正确。
    # 旧版用 Round（150% → dpr=2）：字号物理偏大 33% + 逻辑空间少 25%，
    # 叠加后所有接近满宽的单行文本（指标条副标题等）尾部被裁 —— 已修复。
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI Variable Text", 9))

    # ---- 阶段0：权限 —— 启动就提权（尽量最靠前） ----
    # 打包版由 manifest（requireAdministrator）在进程创建前弹 UAC；
    # 源码 `python app.py` 走这里的兜底：非管理员 → 以管理员重启自己。
    # 放在读设置 / 探针 / 建界面**之前**：那些工作在注定被丢弃的非提权进程里
    # 白跑一遍（探针会起子进程，慢机器上白等一两秒）。QApplication 已建，
    # 提权失败时 relaunch_as_admin 里的 QMessageBox 才有父可挂。
    mode = choose_mode(sys.argv)
    if mode == "admin" and not S.is_admin() and _ELEVATED_FLAG not in sys.argv:
        if relaunch_as_admin():
            sys.exit(0)                  # 已发起提权 → 当前进程退出，交给新进程
        # 用户在 UAC 上点了「否」/ 系统拒绝 → 不退出，降级为普通模式继续运行
    # 一律以**实际权限**为准：提权失败时若还当成管理员，界面按钮全可点，
    # 但真实写入全部失败 —— 那才是最坑的状态。
    admin_mode = (mode == "admin") and S.is_admin()

    settings = settings_load()
    dark = bool(settings.get("dark", False))

    # ---- 阶段1：读取系统信息（分辨率 / DPI 缩放 / 系统字号） ----
    sysinfo = probe_system_info(app)
    app.setStyleSheet(T.build_qss(dark, 1.0, sysinfo["font_base"]))


    # ---- 阶段2：先算出主窗口尺寸，用同尺寸启动窗口展示加载过程 ----
    # 窗口逻辑基准不乘系统缩放：QSS px 由 dpr 自动放大物理尺寸，再乘会双重放大
    # （2K+150% 下窗口占屏 86% 的"画面很大"即此造成）。_ratio 只约束小屏。
    sg = app.primaryScreen().availableGeometry()
    sc = sysinfo["scale"]
    _ratio = 0.92 if sc <= 1.0 else (0.86 if sc <= 1.5 else 0.80)
    win_w = min(1180, int(sg.width() * _ratio))
    _hratio = 0.92 if sc <= 1.0 else (0.86 if sc <= 1.5 else 0.82)
    win_h = min(780, int(sg.height() * _hratio))
    screen_size = (sysinfo["screen_w"], sysinfo["screen_h"])

    splash = SplashWindow(dark=dark, scale=1.0, font_base=sysinfo["font_base"],
                          size=(win_w, win_h), screen_size=screen_size)
    splash.show()
    splash.step(15, "正在读取系统信息…", sysinfo)
    time.sleep(0.25)
    splash.step(45, "正在应用系统缩放 %d%%（DPI %d）…"
                % (round(sysinfo["scale"] * 100), sysinfo["dpi"]))
    time.sleep(0.25)
    splash.step(70, "正在初始化界面…")

    w = MainWindow(settings=settings, admin_mode=admin_mode, dark=dark,
                   sysinfo=sysinfo)
    splash.step(92, "正在加载功能页面…")

    # ---- 阶段3：同尺寸无跳变切换到主窗口 ----
    w.show()
    splash.step(100, "启动完成")
    QApplication.processEvents()
    splash.close()
    QTimer.singleShot(120, lambda: (w.raise_(), w.activateWindow()))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
