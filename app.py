# -*- coding: utf-8 -*-
"""
WinToolbox — Windows 系统工具箱（桌面版 / PySide6）

无边框圆角窗口 + 自绘标题栏，左侧导航 + 右侧页面，蓝白 Windows 11 Fluent 风格。
所有系统操作直接调用 server.py 里已经验证过的服务层（进程内调用，无 HTTP）。
"""
import json
import os
import re
import sys
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QPointF, QRect, QFileInfo,
    QPropertyAnimation, QVariantAnimation, QEasingCurve, QObject,
)
from PySide6.QtGui import (QPainter, QColor, QPen, QFont, QIcon, QPixmap, QPolygonF,
                           QBrush, QRegion, QPainterPath, QAction, QRadialGradient,
                           QLinearGradient)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QComboBox, QCheckBox, QProgressBar, QScrollArea, QTabWidget,
    QAbstractItemView, QMessageBox, QStatusBar, QButtonGroup, QSystemTrayIcon,
    QMenu, QStackedWidget, QFileDialog, QGraphicsDropShadowEffect, QSizePolicy,
    QSlider, QSpinBox, QDialog, QDialogButtonBox, QListWidget,
    QGraphicsOpacityEffect,
)

import server as S
import theme as T

APP_NAME = "WinToolbox"
APP_VER = "1.2"
AUTHOR = "xixidan"
GITHUB_URL = "https://github.com/ncbsz/WinToolbox"
IS_ADMIN = S.is_admin()
CAT_NAME = {"system": "系统", "personal": "个性化", "privacy": "隐私和安全性",
            "security": "安全防护", "network": "网络和 Internet",
            "update": "Windows 更新", "gaming": "游戏", "apps": "应用",
            "explorer": "资源管理器", "input": "输入与键盘",
            "performance": "性能优化", "edge": "Edge 浏览器"}
CAT_ICON = {"system": "⚙", "personal": "🎨", "privacy": "🛡", "security": "🔒",
            "network": "🌍", "update": "🔄", "gaming": "🎮", "apps": "📦",
            "explorer": "📁", "input": "⌨", "performance": "⚡", "edge": "🌐"}


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


def choose_mode(argv):
    """恒返回 'admin'：已取消普通/管理员模式选择，启动即提权。

    保留 --normal-mode 作为**内部降级开关**（自动化测试/排障用），
    正常双击启动一律直接走管理员模式，不再弹窗询问。
    """
    if "--normal-mode" in argv:
        return "normal"
    return "admin"


def relaunch_as_admin():
    """Restart current executable with UAC elevation and exit this process."""
    import ctypes
    try:
        # Build args, preserving any existing ones, and add --admin-mode flag
        args = [a for a in sys.argv[1:] if a not in ("--admin-mode", "--normal-mode")]
        params = " ".join(args) + " --admin-mode"
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                              params, None, 1)
    except Exception as e:
        QMessageBox.critical(None, "提权失败", "无法请求管理员权限：\n%s" % e)
    sys.exit(0)


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
                 size=(1180, 780), screen_size=(1920, 1080), opacity=100):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("%s 正在启动…" % APP_NAME)
        self.setStyleSheet(T.build_qss(dark, scale, font_base, opacity))
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
        f = QFont("Microsoft YaHei UI", 34); f.setBold(True)
        p.setFont(f); p.setPen(QColor("white"))
        p.drawText(pm.rect(), Qt.AlignCenter, "W")
        p.end()
        return QIcon(pm)

    def _sys_text(self, screen_size):
        return ("正在检测显示器与缩放设置…"
                if not screen_size else
                "分辨率 %d × %d" % (screen_size[0], screen_size[1]))

    def _center_on(self, screen_size):
        w, h = self._size
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


class Tasks:
    _live = []

    @staticmethod
    def run(parent, fn, on_done, on_fail=None, *a, **kw):
        t = Task(fn, *a, **kw)
        Tasks._live.append(t)

        def _done(r):
            try:
                on_done(r)
            except RuntimeError:
                pass  # 窗口已销毁，积压回调打在已删控件上——静默丢弃
            finally:
                if t in Tasks._live:
                    Tasks._live.remove(t)

        def _fail(m):
            try:
                if on_fail:
                    on_fail(m)
                else:
                    warn(parent, m)
            except RuntimeError:
                pass  # 同上
            finally:
                if t in Tasks._live:
                    Tasks._live.remove(t)

        t.done.connect(_done)
        t.fail.connect(_fail)
        t.start()
        return t


# ==========================================================================
# 小工具
# ==========================================================================
# ==========================================================================
# 轻量动效：数值补间 / 进度条补间 / 入场淡入
#
# 只动「数值」与「透明度」—— 这两类属性不触发 relayout。每秒刷新的面板里，
# 任何引起重新布局的动画都会掉帧，流畅感的来源是数值平滑，而不是元素乱飞。
# ==========================================================================
class NumberTween(QObject):
    """把一个数值平滑过渡到目标（大号指标数字用，避免每秒硬切导致跳动）。"""

    def __init__(self, label, formatter, dur=280, parent=None):
        super().__init__(parent or label)
        self.lb = label
        self.fmt = formatter
        self._cur = None
        self._a = QVariantAnimation(self)
        self._a.setDuration(dur)
        self._a.setEasingCurve(QEasingCurve.OutCubic)   # 出场缓动
        self._a.valueChanged.connect(self._on_tick)

    def to(self, v):
        """平滑过渡到 v（None 直接显示占位符）。"""
        if v is None:
            self._a.stop()
            self._cur = None
            self.lb.setText("—")
            return
        v = float(v)
        start = self._cur if self._cur is not None else v
        self._a.stop()
        self._a.setStartValue(start)
        self._a.setEndValue(v)
        self._a.start()

    def set_now(self, v):
        """不做动画直接落值（首次进入页面时用，避免从 0 冲上去）。"""
        self._a.stop()
        self._cur = None if v is None else float(v)
        self.lb.setText("—" if v is None else self.fmt(float(v)))

    def _on_tick(self, v):
        self._cur = float(v)
        self.lb.setText(self.fmt(float(v)))


class BarTween(QObject):
    """进度条数值补间：宽度平滑生长，而不是每秒硬跳。"""

    def __init__(self, bar, dur=340, parent=None):
        super().__init__(parent or bar)
        self.bar = bar
        self._a = QPropertyAnimation(bar, b"value", self)
        self._a.setDuration(dur)
        self._a.setEasingCurve(QEasingCurve.OutCubic)

    def to(self, v):
        v = int(max(0, min(100, round(v or 0))))
        if self.bar.value() == v:
            return
        self._a.stop()
        self._a.setStartValue(self.bar.value())
        self._a.setEndValue(v)
        self._a.start()


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
    d, r = divmod(int(sec), 86400)
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


def warn(parent, msg, title="操作失败"):
    QMessageBox.warning(parent, title, str(msg))


def info(parent, msg, title="提示"):
    QMessageBox.information(parent, title, str(msg))


def confirm(parent, title, text, danger=False):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.button(QMessageBox.Yes).setText("确定")
    box.button(QMessageBox.No).setText("取消")
    _center_dialog(box)
    return box.exec() == QMessageBox.Yes


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
    return dlg.ok()


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
        h.addWidget(lb)
        h.addStretch(1)
        if right is not None:
            h.addWidget(right)
        v.addLayout(h)
    return f, v


# ==========================================================================
# 字体度量校正：QSS 的 font-size 不会刷新 QFontMetrics 的布局缓存，
# 导致 QLabel 的 sizeHint 偏小、文字被裁。应用 QSS 后调用本函数修正。
# ==========================================================================
def _rich_text_lines(lb, width):
    """用 QTextDocument 测量富文本在给定宽度下占多少行。"""
    try:
        from PySide6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setDefaultFont(lb.font())
        doc.setHtml(lb.text())
        doc.setTextWidth(max(40, width))
        n, blk = 0, doc.begin()
        while blk.isValid():
            n += 1
            blk = blk.next()
        return max(1, n)
    except Exception:
        return 0


def _rich_text_height(lb, width):
    """富文本 QLabel 的目标高度 = 行数 × 实际字体行高。

    QTextDocument 的默认行距大于 QFontMetrics.height()，直接用它的高度会高估，
    因此只取「行数」，行高用控件真实字体度量。
    """
    lines = _rich_text_lines(lb, width)
    if lines <= 0:
        return 0
    return lines * lb.fontMetrics().height() + 1


def fit_label(lb):
    """按当前实际字体度量，给 QLabel 设定不低于字体高度的最小高度。"""
    try:
        txt = lb.text()
        if not txt:
            return
        fm = lb.fontMetrics()
        if lb.wordWrap():
            w = lb.width() if lb.width() > 0 else lb.sizeHint().width()
            need = _rich_text_height(lb, w)
            if need <= 0:
                need = fm.height() + 2
        else:
            need = fm.height() * (txt.count("\n") + 1) + 2
        if lb.minimumHeight() < need:
            lb.setMinimumHeight(need)
        lb.updateGeometry()
    except Exception:
        pass


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


def _header_menu(table, pos, from_body=False):
    """列宽右键菜单：自动调整 / 适应窗口。表头和表格内容区都能唤出。

    `pos` — 表头坐标系（from_body=False）或表格视口坐标系（from_body=True）。
    """
    hh = table.horizontalHeader()
    if from_body:
        col = table.columnAt(pos.x())
        spot = table.viewport().mapToGlobal(pos)
    else:
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
        lambda pos, tb=t: _header_menu(tb, pos))
    # 内容区右键同样可以调列宽（位置换算成视口坐标）
    t.setContextMenuPolicy(Qt.CustomContextMenu)
    t.customContextMenuRequested.connect(
        lambda pos, tb=t: _header_menu(tb, pos, True))
    hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    t.setSortingEnabled(sortable)          # 点表头按列排序
    return t


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
                  self.findChild(QFrame, "Nav"), self.statusBar()):
            if w is not None:
                w.setProperty("maximized", on)
                w.style().unpolish(w)
                w.style().polish(w)
        self._clip()


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
        pm.fill(QColor(T.get_color("BG", dark)))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r, g, b = T.hex_to_rgb(accent)
        # 主光：左上角溢出。透明度压得很低 —— Mica 的要点是「几乎察觉不到的色彩倾向」，
        # 浓了就成了廉价渐变（深色下尤其明显）。
        halo = QRadialGradient(QPointF(w * 0.04, -h * 0.22), max(w, h) * 0.85)
        halo.setColorAt(0.00, QColor(r, g, b, 22 if dark else 13))
        halo.setColorAt(0.55, QColor(r, g, b, 8 if dark else 5))
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
        # 页面自身承载「纯色底」：QScrollArea 的 viewport 会取容器的背景，
        # 不给这里显式背景就会露出 Qt 默认 palette 的浅灰。
        self.setObjectName("Page")
        self.setAttribute(Qt.WA_StyledBackground, True)

    def refresh(self):
        pass

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
class OverviewPage(Page):
    title = "概览"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 20)
        root.setSpacing(12)

        self.cards = {}          # 兼容旧接口 {key: (val, sub, bar|None)}
        self.metrics = {}        # 主指标（大号数字）
        self._anim_parts = []    # 参与入场动画的容器

        # ---- 主指标条：四项横排、竖发丝线分隔 ----
        # 大号等宽数字是主角，单位与副信息退到次要层级（对齐任务管理器的信息层级）
        strip = QFrame(); strip.setObjectName("Card")
        sv = QHBoxLayout(strip)
        sv.setContentsMargins(18, 14, 18, 16)
        sv.setSpacing(0)
        for i, (key, label, st, cls) in enumerate(
                (("cpu", "处理器", 1.35, "cpu"), ("mem", "内存", 1.15, "mem"),
                 ("gpu", "显卡", 1.0, "gpu"), ("net", "网络", 1.0, "net"))):
            if i:
                sp = QFrame(); sp.setObjectName("VSep"); sp.setFixedWidth(1)
                sv.addSpacing(16); sv.addWidget(sp); sv.addSpacing(16)
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0); cl.setSpacing(3)
            k = QLabel(label); k.setObjectName("MetricK")
            num = QHBoxLayout(); num.setContentsMargins(0, 0, 0, 0); num.setSpacing(4)
            v = QLabel("—"); v.setObjectName("MetricV")
            u = QLabel("Mbps" if key == "net" else "%"); u.setObjectName("MetricU")
            u.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
            num.addWidget(v); num.addWidget(u); num.addStretch(1)
            sub = QLabel(""); sub.setObjectName("MetricSub"); sub.setWordWrap(True)
            bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0)
            bar.setTextVisible(False)
            cl.addWidget(k); cl.addLayout(num)
            cl.addWidget(bar)          # 四格都给条，保证副信息基线对齐
            cl.addWidget(sub)
            sv.addWidget(cell, st)
            if key == "net":
                fmt = lambda x: ("%.2f" % x) if x < 10 else ("%.1f" % x)
            else:
                fmt = lambda x: "%.0f" % x
            self.metrics[key] = {
                "val": v, "sub": sub, "bar": bar, "cls": cls,
                "tween": NumberTween(v, fmt, parent=self),
                "bar_tween": BarTween(bar, parent=self),
            }
        root.addWidget(strip)
        self._anim_parts.append(strip)

        # ---- 实时曲线：整行，图例放曲线下方单行展开（放标题右侧会挤成三行）----
        chart_card, cv = card("实时曲线")
        self.chart = Chart(self.win)
        cv.addWidget(self.chart)
        self.legend = QLabel(); self.legend.setObjectName("MetricSub")
        self.legend.setWordWrap(True)
        cv.addWidget(self.legend)
        root.addWidget(chart_card)
        self._anim_parts.append(chart_card)

        # ---- 系统信息（键值行） + 存储，并排 ----
        mid = QHBoxLayout(); mid.setSpacing(12)
        sys_card, syv = card("系统信息")
        g = QGridLayout(); g.setHorizontalSpacing(14); g.setVerticalSpacing(11)
        g.setColumnMinimumWidth(0, 58)
        g.setColumnStretch(1, 1)
        for i, (key, label) in enumerate((("os", "操作系统"), ("cpu", "处理器"),
                                          ("up", "运行时长"), ("host", "当前用户"))):
            k = QLabel(label); k.setObjectName("KVKey")
            k.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            v = QLabel("—"); v.setObjectName("KVVal"); v.setWordWrap(True)
            s = QLabel(""); s.setObjectName("KVSub"); s.setWordWrap(True)
            box = QVBoxLayout(); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(2)
            box.addWidget(v); box.addWidget(s)
            g.addWidget(k, i, 0); g.addLayout(box, i, 1)
            self.cards[key] = (v, s, None)
        syv.addLayout(g)
        syv.addStretch(1)
        mid.addWidget(sys_card, 3)

        disk_card, dv = card("存储")
        self.disk_box = QVBoxLayout(); self.disk_box.setSpacing(10)
        dv.addLayout(self.disk_box)
        dv.addStretch(1)
        mid.addWidget(disk_card, 4)
        root.addLayout(mid)
        self._anim_parts += [sys_card, disk_card]

        # ---- 设备区：GPU 详情 / 磁盘活动 ----
        dev_row = QHBoxLayout(); dev_row.setSpacing(12)
        self.diskio_val = QLabel("—"); self.diskio_val.setObjectName("MetricSub")
        gpu_card, gv = card("GPU 详情")
        sel_row = QHBoxLayout(); sel_row.setSpacing(8)
        sel_row.addWidget(QLabel("GPU："))
        self.gpu_pick = QComboBox()
        self.gpu_pick.addItem("全部")
        self.gpu_pick.currentTextChanged.connect(self._on_gpu_pick)
        sel_row.addWidget(self.gpu_pick, 1)
        gv.addLayout(sel_row)
        self.gpu_box = QVBoxLayout(); self.gpu_box.setSpacing(8)
        gv.addLayout(self.gpu_box)
        dev_row.addWidget(gpu_card, 1)

        diskact_card, dav = card("磁盘活动", right=self.diskio_val)
        self.diskact_box = QVBoxLayout(); self.diskact_box.setSpacing(8)
        dav.addLayout(self.diskact_box)
        dev_row.addWidget(diskact_card, 1)
        root.addLayout(dev_row)
        self._anim_parts += [gpu_card, diskact_card]

        # ---- 网络 / 快速操作 ----
        row = QHBoxLayout(); row.setSpacing(12)
        net_card, nav2 = card("网络")
        self.net_box = QVBoxLayout(); self.net_box.setSpacing(8)
        nav2.addLayout(self.net_box)
        row.addWidget(net_card, 3)

        act_card, av = card("快速操作")
        for text, slot in [("一键整理内存", self.do_trim),
                           ("网络快速诊断", lambda: self.win.goto("network")),
                           ("扫描桌面是否被感染", lambda: self.win.goto("security"))]:
            av.addWidget(btn(text, "Primary" if text.startswith("一键") else None, on_click=slot))
        av.addWidget(notice("本工具整合自 ZyperWin++ / HiBit Uninstaller / Sunlight 内存整理 / "
                            "360 断网急救箱 的功能思路，全部重新实现，"
                            "<b>未复用任何被感染文件中的代码</b>。"))
        av.addStretch(1)
        row.addWidget(act_card, 2)
        root.addLayout(row)
        self._anim_parts += [net_card, act_card]
        root.addStretch(1)

        self.mem_pct = 0.0
        self._net = {}
        self._net_rx = None
        self._net_tx = None
        self._last_gpu = None
        self._last_disk = None
        self._cpu_base = ""      # 「12 核 / 16 线程」，refresh 填充
        self._cpu_model = ""     # CPU 型号简称，refresh 填充
        self._cpu_cores = ""     # 「12核/16线程」（紧凑版，指标条用），refresh 填充
        self._cpu_mhz = 0        # CPU 标称基准频率(MHz)，refresh 填充
        self._cpu_temp = None    # 实时 CPU 温度，tick_perf 填充
        self.gpu_sel = ""        # 当前选中的显卡名（"" = 全部）
        self._gpu_sig = None     # 上次下拉框里的显卡名单，变了才重建
        # 控件复用缓存：结构不变时只更新数值，避免每秒重建导致的闪烁
        self._gpu_rows = []
        self._gpu_rows_sig = None
        self._disk_rows = []
        self._disk_rows_sig = None
        self._net_refs = None
        self._net_sig = None
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
        self._update_legend()

    def first_show(self):
        """首次进入：一次编排好的入场序列（卡片错峰 70ms 淡入）。

        只在首次播放 —— 之后切回本页不再重播，否则每次切页都闪一遍很烦。
        """
        if not self.loaded:
            self.loaded = True
            self.refresh()
            for i, w in enumerate(self._anim_parts):
                fade_in(w, i * 70)

    def _on_gpu_pick(self, _text):
        """切换 GPU 详情里显示哪块显卡（空 = 全部）。
        显示文本带 GPU0/GPU1 前缀，所以真实显卡名存在 item data 里。"""
        self.gpu_sel = self.gpu_pick.currentData() or ""
        self.tick_perf()          # 立即按新筛选重画，不必等下一个心跳

    def _update_legend(self, cpu=None, mem=None, gpu=None, disk=None, net=None):
        """曲线图例：色点 + 名称 + 当前值（任务管理器的图例写法）。"""
        vals = {"cpu": cpu, "mem": mem, "gpu": gpu, "disk": disk, "net": net}
        parts = []
        for key, ckey, label in Chart.SERIES:
            v = vals.get(key)
            col = T.get_color(ckey, self.win.dark)
            if v is None:
                txt = "—"
            elif key == "net":
                txt = fmt_rate(v)
            else:
                txt = "%.0f%%" % v
            parts.append('<span style="color:%s;">●</span> %s <b>%s</b>'
                         % (col, label, txt))
        self.legend.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    def tick(self):
        """1s 心跳：CPU / 内存 / 网络 —— 数值全部走补间，避免每秒硬跳。"""
        if not self.isVisible():
            return
        try:
            m = S.mem_status()
            cpu = S.cpu_percent()
            pct = (m.ullTotalPhys - m.ullAvailPhys) * 100.0 / m.ullTotalPhys
            self.mem_pct = pct

            # 网络：上下行合计（Mbps）
            net = None
            if self._net_rx is not None or self._net_tx is not None:
                net = (self._net_rx or 0.0) + (self._net_tx or 0.0)

            mt = self.metrics["cpu"]
            mt["tween"].to(cpu)
            mt["bar_tween"].to(cpu)
            _cls(mt["bar"], cpu, "Cpu")

            mt = self.metrics["mem"]
            mt["tween"].to(pct)
            mt["bar_tween"].to(pct)
            _cls(mt["bar"], pct, "Mem")
            mt["sub"].setText("%s / %s" % (fmt_bytes(m.ullTotalPhys - m.ullAvailPhys),
                                           fmt_bytes(m.ullTotalPhys)))

            if net is not None:
                mt = self.metrics["net"]
                mt["tween"].to(net)
                # 进度条 = 带宽占用率（相对网卡协商速率），无速率信息时留空
                link = 0.0
                prim = (self._net or {}).get("primary") or {}
                for src in (prim.get("link_mbps"), (self._net or {}).get("speed_mbps")):
                    try:
                        if src:
                            link = float(src)
                            break
                    except Exception:
                        pass
                if link > 0:
                    mt["bar_tween"].to(net * 100.0 / link)
                    mt["bar"].setToolTip("带宽占用（协商速率 %s）" % fmt_linkspeed(link))
                # 副信息显示「网卡名称 · 协商速率」，实时收发放 tooltip
                rx, tx = self._net_rx or 0.0, self._net_tx or 0.0
                nm = short_hw_name(prim.get("name") or prim.get("kind") or "网络")
                mt["sub"].setText("%s · %s" % (nm, fmt_linkspeed(link) if link else "速率未知"))
                mt["sub"].setToolTip("%s\n↓ %s   ↑ %s"
                                     % (prim.get("desc") or prim.get("name") or "",
                                        fmt_rate(rx), fmt_rate(tx)))
            elif (self._net or {}).get("warming"):
                self.metrics["net"]["sub"].setText("采样准备中…")

            # 缓存最近一次的 GPU / 磁盘值，避免只有 tick_perf 才刷曲线
            self.chart.push(cpu, pct, self._last_gpu, self._last_disk, net)
            self._update_legend(cpu, pct, self._last_gpu, self._last_disk, net)
        except Exception:
            pass

    def tick_perf(self):
        """1s 心跳：温度 / GPU / 磁盘（服务端 PDH 实时采样）+ 刷新右侧设备卡片。"""
        if not self.isVisible():
            return

        def clear_box(box):
            while box.count():
                it = box.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()

        def make_dev_bar(box, name, obj_name, pct_text="--"):
            """创建一行设备占用条，返回引用供后续只改数值（不重建控件）。"""
            row = QWidget()
            rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)
            lb = QLabel(name); lb.setObjectName("Muted")
            pb = QProgressBar(); pb.setRange(0, 100); pb.setValue(0)
            pb.setTextVisible(False); pb.setObjectName(obj_name)
            val = QLabel(pct_text)
            val.setObjectName("Mono")
            val.setMinimumWidth(52)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rl.addWidget(lb, 0); rl.addWidget(pb, 1); rl.addWidget(val, 0)
            box.addWidget(row)
            return {"row": row, "bar": pb, "val": val}

        def make_disk_row(box, name):
            """磁盘行：名称 + 利用率条 + 「读 x / 写 y MB/s」，同样复用。"""
            row = QWidget()
            rl = QVBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(3)
            top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.setSpacing(8)
            lb = QLabel(name); lb.setObjectName("Muted")
            pb = QProgressBar(); pb.setRange(0, 100)
            pb.setValue(0)
            pb.setTextVisible(False); pb.setObjectName("Disk")
            top.addWidget(lb, 0); top.addWidget(pb, 1)
            rl.addLayout(top)
            io = QLabel("读 —   写 —")
            io.setObjectName("Mono")
            rl.addWidget(io)
            box.addWidget(row)
            return {"row": row, "bar": pb, "io": io}

        def got(d):
            gpu_agg, disk_agg, ct, gt = d["gpu"], d["disk"], d["cpu_temp"], d["gpu_temp"]
            # ---- 主指标：显卡（优先独显，无独显才显示占用最高的卡；型号 + 温度）----
            mt = self.metrics["gpu"]
            mt["tween"].to(gpu_agg)
            mt["bar_tween"].to(gpu_agg or 0)
            _cls(mt["bar"], gpu_agg, "Gpu")
            named = [g for g in (d.get("gpus") or []) if g.get("name")]
            gname = ""
            if named:
                dgs = [g for g in named if is_dgpu(g["name"])]
                top = max(dgs or named, key=lambda g: (g.get("util") or 0))
                gname = short_hw_name(top["name"])
                tip = ("独显：%s" if dgs else "占用最高：%s") % top["name"]
                if len(named) > 1:
                    tip += "\n共 %d 块显卡" % len(named)
                mt["sub"].setToolTip(tip)
            mt["sub"].setText(" · ".join(
                [x for x in (gname, fmt_temp(gt) if gt is not None else "温度不可读") if x]))

            # ---- 主指标：处理器（型号简称 · 核线程 · 基准频率 · 温度）----
            self._cpu_temp = ct
            _mhz = getattr(self, "_cpu_mhz", 0)
            self.metrics["cpu"]["sub"].setText(" · ".join(
                [x for x in (getattr(self, "_cpu_model", ""),
                             getattr(self, "_cpu_cores", ""),
                             fmt_ghz(_mhz) if _mhz else "",
                             ("%.0f°C" % ct) if ct is not None else "温度不可读")
                 if x]))

            # ---- 磁盘活动聚合值挂到卡片标题右侧 ----
            self.diskio_val.setText("读写活跃 %s" % (fmt_pct(disk_agg)
                                                if disk_agg is not None else "—"))

            # per-GPU bars：所有显卡 + 右侧具体百分比 + 按下拉选择过滤
            # 关键：显卡名单不变时只更新数值，绝不重建控件（重建会闪）
            gpus = d.get("gpus") or []
            sig = "|".join(g.get("name") or ("GPU%d" % g.get("index", i))
                           for i, g in enumerate(gpus))
            if sig != self._gpu_sig:
                self._gpu_sig = sig
                self.gpu_pick.blockSignals(True)
                self.gpu_pick.clear()
                self.gpu_pick.addItem("全部")
                for g in gpus:
                    nm = g.get("name") or ("GPU%d" % g.get("index", 0))
                    # 显示「GPU0 · 显卡全名」，真实名字放 data 供过滤用
                    self.gpu_pick.addItem("GPU%d · %s" % (g.get("index", 0), nm), nm)
                if self.gpu_sel:
                    ix = self.gpu_pick.findData(self.gpu_sel)
                    if ix >= 0:
                        self.gpu_pick.setCurrentIndex(ix)
                self.gpu_pick.blockSignals(False)
            shown = [g for g in gpus
                     if not self.gpu_sel or g.get("name") == self.gpu_sel]
            spec = []
            for g in shown:
                tag = "GPU%d" % g.get("index", 0)      # 行首统一用 GPU0 / GPU1 标注
                full = g.get("name") or tag            # 全名放进悬停提示
                spec.append(("u", tag, full, g.get("util")))
                # 温度行同样带进度条（按 0~100℃ 填充）；读不到温度就留空
                spec.append(("t", tag + " 温度", full, g.get("temp")))
            rsig = tuple((k, n) for k, n, _f, _v in spec)
            if rsig != self._gpu_rows_sig:
                self._gpu_rows_sig = rsig
                clear_box(self.gpu_box)
                self._gpu_rows = []
                for k, n, full, _v in spec:
                    ref = make_dev_bar(self.gpu_box, n, "Gpu" if k == "u" else "Temp")
                    ref["row"].setToolTip(full)
                    self._gpu_rows.append(ref)
                if not spec:
                    empty = QLabel("未检测到可显示的 GPU")
                    empty.setObjectName("Muted")
                    self.gpu_box.addWidget(empty)
            for ref, (k, _n, _f, v) in zip(self._gpu_rows, spec):
                ref["bar"].setValue(int(min(100, v)) if v is not None else 0)
                if k == "u":
                    _cls(ref["bar"], v, "Gpu")
                    ref["val"].setText("--" if v is None else "%d%%" % round(v))
                else:
                    # 温度条平时用温度色，≥85℃ 转红
                    restyle(ref["bar"],
                            "TempErr" if (v is not None and v >= 85) else "Temp")
                    ref["val"].setText("--" if v is None else "%d°C" % round(v))
            self.chart.gpu_on = bool(spec)

            # per-disk activity bars（含读写速率）—— 同样复用控件
            disks = d.get("disks") or []
            dsig = tuple(dk.get("name") or "Disk%d" % i for i, dk in enumerate(disks))
            if dsig != self._disk_rows_sig:
                self._disk_rows_sig = dsig
                clear_box(self.diskact_box)
                self._disk_rows = [make_disk_row(self.diskact_box, dk.get("name") or "磁盘%d" % i)
                                   for i, dk in enumerate(disks)]
            for ref, dk in zip(self._disk_rows, disks):
                u = dk.get("util")
                ref["bar"].setValue(int(u) if u is not None else 0)
                _cls(ref["bar"], u, "Disk")
                ref["io"].setText("读 %s   写 %s" % (fmt_mbps(dk.get("read_mbps")),
                                                   fmt_mbps(dk.get("write_mbps"))))
            self.chart.disk_on = bool(disks)

            # 网络：网卡类型 / 协商速率 / 实时收发
            self._render_net(d)

            # 缓存聚合值，供 1s 心跳推入曲线（避免只有本回调刷新曲线）
            self._last_gpu = gpu_agg
            self._last_disk = disk_agg

            # CPU 温度并入「处理器」卡片副标题（GPU 温度在 GPU 卡片副标题里，见上）
            base = getattr(self, "_cpu_base", "")
            if base:
                sub_cpu = self.cards["cpu"][1]
                if ct is None:
                    sub_cpu.setText(base)
                    sub_cpu.setStyleSheet("")
                else:
                    sub_cpu.setText("%s · 温度 %s" % (base, fmt_temp(ct)))
                    col = (T.get_color("ERR", self.win.dark) if ct >= 85 else
                           T.get_color("WARN", self.win.dark) if ct >= 75 else "")
                    sub_cpu.setStyleSheet(("color: %s;" % col) if col else "")
        # 1s 一次，失败不能弹框（否则每秒一个警告）
        Tasks.run(self, S.perf_stats, got, on_fail=lambda *_: None)

    def refresh(self):
        def got(d):
            # ---- 主指标条：处理器副标题（型号，核线程/基准频率/温度由 tick_perf 每秒补）----
            self._cpu_base = "%d 核 / %d 线程" % (d["cores"], d["logical"])
            self._cpu_cores = "%d核/%d线程" % (d["cores"], d["logical"])
            self._cpu_mhz = d.get("base_mhz") or 0
            self._cpu_model = short_cpu_name(d["cpu"] or "")
            self._cpu_temp = None
            cpu_mt = self.metrics["cpu"]
            cpu_mt["sub"].setText(self._cpu_model or self._cpu_base)
            cpu_mt["sub"].setToolTip(
                "%s\n%s · 基准 %s\n温度取决于硬件与驱动支持（CPU 走 ACPI/WMI 热区，N 卡走 nvidia-smi）"
                % (d["cpu"] or "", self._cpu_base, fmt_ghz(self._cpu_mhz)))
            self.metrics["gpu"]["sub"].setText("温度不可读")

            # ---- 系统信息：键值行（标签灰、值实、小字补充）----
            _os = d["os"]
            _rel = _os.get("release") or ""
            _full = _os.get("build_full") or _os.get("version") or ""
            _bits = "64 位" if "64" in (_os.get("arch") or "") else "32 位"
            _set(self.cards["os"], (_os["caption"] or "—"),
                 ("%s · 版本 %s · %s" % (_rel, _full, _bits)) if _rel
                 else ("版本 %s · %s" % (_os.get("version") or "", _bits)))
            _set(self.cards["cpu"], (d["cpu"] or "—"), self._cpu_base)
            _set(self.cards["up"], fmt_uptime(d["uptime"]), "自上次开机起持续运行")
            _set(self.cards["host"], d["user"] or "—", d["host"] or "")

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

        def _info():
            b = S.sys_basic()
            return {"os": {"caption": b["caption"], "version": b["version"], "arch": b["arch"],
                           "release": b.get("release") or "",      # 24H2 / 25H2
                           "build_full": b.get("build_full") or ""},  # 26200.9168
                    "cpu": b["cpu"], "cores": b["cores"], "logical": b["logical"],
                    "base_mhz": b.get("base_mhz") or 0,            # CPU 标称基准频率
                    "disks": S.disks(), "uptime": S.uptime_seconds(),
                    "host": S.socket.gethostname(), "user": os.environ.get("USERNAME", "")}
        Tasks.run(self, _info, got)

    def do_trim(self):
        if not confirm(self, "整理内存",
                       "将对所有占用 >8MB 的进程执行 SetProcessWorkingSetSize(-1,-1)，"
                       "把工作集换出到页面文件。\n\n"
                       "说明：内存占用数字会立刻下降，但被换出的数据下次访问要从磁盘读回，"
                       "可能反而变慢。系统本身会按需管理内存。"):
            return
        Tasks.run(self, S.trim_all, lambda r: info(self, "已整理 %d / %d 个进程" % r))

    # ---- 网络卡片 ----
    def _load_net(self):
        """后台取一次网卡信息（协商速率 / 类型 / 收发速率）。"""
        def got_net(d):
            d = d or {}
            self._net = d
            rx = d.get("rx_mbps")
            tx = d.get("tx_mbps")
            # 首轮 PDH 建立期间（warming）没有样本，此时保持 None，
            # 让曲线和 pill 显示「准备中」而不是误导性的 0.00 Mbps。
            if rx is None and tx is None and d.get("warming"):
                self._net_rx = None
                self._net_tx = None
            else:
                self._net_rx = rx if rx is not None else 0.0
                self._net_tx = tx if tx is not None else 0.0
            # 网络曲线：拿到数据就开，速率同时用来定标
            self.chart.net_on = bool(d.get("primary"))
            prim = d.get("primary") or {}
            link = prim.get("link_mbps") or d.get("speed_mbps")
            if link:
                try:
                    self.chart.net_link = float(link)
                except Exception:
                    pass
            self._render_net(d)

        Tasks.run(self, S.net_info, got_net, on_fail=lambda *_: None)

    def _render_net(self, _unused=None):
        """网络卡片：结构不变时只刷新数值，避免每秒重建控件导致的闪烁。"""
        d = getattr(self, "_net", None) or {}
        box = getattr(self, "net_box", None)
        if box is None:
            return
        prim = d.get("primary")
        link = (prim or {}).get("link_mbps") or d.get("speed_mbps")
        sig = (None if not prim else
               (prim.get("name"), prim.get("kind"), prim.get("desc") or "",
                bool(link), bool(prim.get("desc"))))
        if sig != self._net_sig:
            self._net_sig = sig
            while box.count():
                it = box.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            self._net_refs = None
            if not prim:
                lb = QLabel("未检测到活动网卡"); lb.setObjectName("Muted")
                box.addWidget(lb)
                return
            # 第一行：类型标签 + 网卡名
            h1 = QHBoxLayout(); h1.setSpacing(8)
            kind = prim.get("kind") or "网络"
            icon = "📶" if kind == "WiFi" else "🔌"
            tag = QLabel("%s %s" % (icon, kind))
            tag.setObjectName("PillBlue")
            tag.setMinimumHeight(22)
            nm = QLabel(prim.get("name") or "—"); nm.setObjectName("Muted")
            nm.setWordWrap(True)
            h1.addWidget(tag, 0); h1.addWidget(nm, 1)
            w1 = QWidget(); w1.setLayout(h1); box.addWidget(w1)

            ls = QLabel("协商速率：%s" % fmt_linkspeed(link))
            ls.setObjectName("Mono"); ls.setWordWrap(True)
            box.addWidget(ls)

            io = QLabel("↓ 下载 —    ↑ 上传 —")
            io.setObjectName("Mono"); io.setWordWrap(True)
            box.addWidget(io)

            bar = None
            if link:
                row = QWidget()
                rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)
                lb2 = QLabel("带宽占用"); lb2.setObjectName("Muted")
                bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0)
                bar.setTextVisible(False); bar.setObjectName("Net")
                rl.addWidget(lb2, 0); rl.addWidget(bar, 1)
                box.addWidget(row)

            if prim.get("desc"):
                de = QLabel(prim["desc"]); de.setObjectName("Muted3")
                de.setWordWrap(True)
                box.addWidget(de)
            box.addStretch(0)
            self._net_refs = {"io": io, "bar": bar}
            fit_labels(box)

        refs = self._net_refs
        if not refs:
            return
        rx = d.get("rx_mbps") or 0.0
        tx = d.get("tx_mbps") or 0.0
        refs["io"].setText("↓ 下载 %s    ↑ 上传 %s"
                           % (fmt_rate(d.get("rx_mbps")), fmt_rate(d.get("tx_mbps"))))
        if refs.get("bar") is not None:
            pct = min(100, int(round((rx + tx) * 100.0 / max(1.0, float(link)))))
            refs["bar"].setValue(pct)


def restyle(w, obj_name):
    """只在 objectName 真的变化时才 unpolish/polish。

    旧版每秒都无条件重刷样式，会让整行控件重绘 —— 页面刷新时"闪一下"的主因之一。
    """
    if getattr(w, "_wt_style_name", None) == obj_name:
        return
    w._wt_style_name = obj_name
    w.setObjectName(obj_name)
    w.style().unpolish(w)
    w.style().polish(w)


def _cls(bar, v, base=""):
    if v is not None and v > 85:
        nm = "Err"
    elif v is not None and v > 65:
        nm = "Warn"
    else:
        nm = base
    restyle(bar, nm)


def _set(card_tuple, val_text, sub_text, size=None):
    val, sub, bar = card_tuple
    val.setText(val_text)
    sub.setText(sub_text)
    # 动态文本可能换行行数变化，重新校正高度避免被裁
    fit_label(val)
    fit_label(sub)


class Chart(QWidget):
    """实时占用曲线：CPU / 内存 / GPU / 磁盘 / 网络。

    前四条是百分比，直接用 0~100 的纵轴。
    网络速率是 Mbps，量级完全不同，所以单列一条曲线并按「协商速率」自动定标：
    纵轴上限 = max(100, 当前最大观测值 * 1.25) 且不超过协商带宽，
    这样千兆网卡跑满会贴近顶部，而空闲时的细微抖动也能看见。
    """

    # 曲线 -> (配色键, 中文名)
    SERIES = [("cpu", "ACCENT_CPU", "CPU"),
              ("mem", "ACCENT_MEM", "内存"),
              ("gpu", "ACCENT_GPU", "GPU"),
              ("disk", "ACCENT_DISK", "磁盘"),
              ("net", "ACCENT_NET", "网络")]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.cpu = []
        self.mem = []
        self.gpu = []
        self.disk = []
        self.net = []
        self.gpu_on = False
        self.disk_on = False
        self.net_on = False
        self.net_max = 100.0          # 网络纵轴上限（Mbps）
        self.net_link = 0.0           # 协商速率（Mbps），用于定标
        self.setMinimumHeight(110)

    def push(self, c, m, g=None, d=None, n=None):
        self.cpu.append(float(c))
        self.mem.append(float(m))
        self.gpu.append(float(g) if g is not None else None)
        self.disk.append(float(d) if d is not None else None)
        self.net.append(float(n) if n is not None else None)
        if len(self.cpu) > 60:
            for arr in (self.cpu, self.mem, self.gpu, self.disk, self.net):
                arr.pop(0)
        # 网络纵轴自适应：取近期峰值留 25% 余量，最低 100 Mbps
        vals = [v for v in self.net[-30:] if v is not None]
        if vals:
            target = max(vals) * 1.25
            target = max(100.0, target)
            if self.net_link > 0:
                target = min(target, float(self.net_link))
            # 平滑变化，避免曲线抖动
            self.net_max += (target - self.net_max) * 0.25
            self.net_max = max(10.0, self.net_max)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, pad = self.width(), self.height(), 4
        p.fillRect(0, 0, w, h, QColor(T.get_color("PANEL", self.win.dark)))
        grid = T.get_color("TRACK", self.win.dark)      # 网格用最淡的轨道色，只做参考不做装饰
        p.setPen(QPen(QColor(grid), 1))
        for i in range(5):
            y = pad + (h - pad * 2) * i / 4.0
            p.drawLine(0, int(y), w, int(y))

        def series(arr, color, fill, scale=100.0):
            pts = [(i, v) for i, v in enumerate(arr) if v is not None]
            if len(pts) < 2:
                return
            n = len(arr)
            qpts = [QPointF(w * i / (n - 1.0),
                            h - pad - (h - pad * 2) * min(scale, v) / scale)
                    for i, v in pts]
            p.setPen(Qt.NoPen)
            if fill:
                # 渐变填充（顶实底虚）比纯色块干净，也不会糊住网格
                gr = QLinearGradient(0, 0, 0, h)
                c0 = QColor(color); c0.setAlpha(60)
                c1 = QColor(color); c1.setAlpha(4)
                gr.setColorAt(0.0, c0)
                gr.setColorAt(1.0, c1)
                p.setBrush(QBrush(gr))
                p.drawPolygon(QPolygonF(qpts + [QPointF(w, h), QPointF(0, h)]))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(color), 2))
            for i in range(1, len(qpts)):
                p.drawLine(qpts[i - 1], qpts[i])

        series(self.mem, T.get_color("ACCENT_MEM", self.win.dark), True)
        series(self.cpu, T.get_color("ACCENT_CPU", self.win.dark), False)
        if self.gpu_on:
            series(self.gpu, T.get_color("ACCENT_GPU", self.win.dark), False)
        if self.disk_on:
            series(self.disk, T.get_color("ACCENT_DISK", self.win.dark), False)
        if self.net_on:
            series(self.net, T.get_color("ACCENT_NET", self.win.dark), False,
                   scale=self.net_max)
        p.end()


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
        self._def_enabled = None
        self.toggle_row = QHBoxLayout()
        self.toggle_row.setSpacing(12)
        f_upd, v_upd = card("🔄 Windows 更新")
        self.upd_pill = pill("检测中…", "blue")
        v_upd.addWidget(self.upd_pill)
        self.btn_upd = btn("关闭更新", "Danger", on_click=self.toggle_update)
        self.btn_upd.setEnabled(self.win.admin_mode)
        v_upd.addWidget(self.btn_upd)
        v_upd.addWidget(notice("一键禁用/恢复 Windows Update 服务，改完需重启生效。", "info"))
        self.toggle_row.addWidget(f_upd, 1)
        f_def, v_def = card("🛡 Windows Defender")
        self.def_pill = pill("检测中…", "blue")
        v_def.addWidget(self.def_pill)
        self.btn_def = btn("关闭 Defender", "Danger", on_click=self.toggle_defender)
        self.btn_def.setEnabled(self.win.admin_mode)
        v_def.addWidget(self.btn_def)
        v_def.addWidget(notice("一键禁用/恢复 Defender 服务。关闭后本机失去实时防护，请谨慎。", "warn"))
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
        Tasks.run(self, S.load_rules_with_state, got)
        self._load_toggles()

    # ---------- 一键开关：更新 / Defender ----------
    def _load_toggles(self):
        Tasks.run(self, lambda: (S.update_status(), S.defender_status()), self._toggles_done)

    def _toggles_done(self, r):
        up, df = r
        self._upd_enabled = bool(up.get("enabled"))
        self._def_enabled = bool(df.get("enabled"))
        self._paint_toggles()

    def _paint_toggles(self):
        def repaint(pill_w, text, on):
            pill_w.setText(text)
            pill_w.setObjectName("PillOk" if on else "PillWarn")
            pill_w.style().unpolish(pill_w)
            pill_w.style().polish(pill_w)
        if self._upd_enabled is None:
            self.upd_pill.setText("检测中…")
        else:
            repaint(self.upd_pill, "当前：已开启" if self._upd_enabled else "当前：已关闭",
                    self._upd_enabled)
            self.btn_upd.setText("一键关闭更新" if self._upd_enabled else "一键开启更新")
        if self._def_enabled is None:
            self.def_pill.setText("检测中…")
        else:
            repaint(self.def_pill, "当前：已开启" if self._def_enabled else "当前：已关闭",
                    self._def_enabled)
            self.btn_def.setText("一键关闭 Defender" if self._def_enabled else "一键开启 Defender")

    def toggle_update(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if self._upd_enabled is None:
            return
        target = not self._upd_enabled
        verb = "关闭" if not target else "开启"
        if not confirm(self, "切换 Windows 更新",
                       "即将%s Windows Update 相关服务（wuauserv / UsoSvc / WaaSMedicSvc）。\n\n"
                       "改完后需重启电脑生效；关闭更新会让系统长期不打补丁，请确认风险。" % verb,
                       danger=not target):
            return
        self.win.busy("正在%s更新…" % verb)
        Tasks.run(self, lambda: S.set_update(target),
                  lambda r: (self.win.idle(), self._load_toggles(),
                             info(self, "已%s更新（部分需重启生效）。" % verb))[1],
                  on_fail=self._fail)

    def toggle_defender(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if self._def_enabled is None:
            return
        target = not self._def_enabled
        verb = "关闭" if not target else "开启"
        if not confirm(self, "切换 Windows Defender",
                       "即将%s Defender 相关服务（WinDefend / WdNisSvc / SecurityHealthService）。\n\n"
                       "关闭后本机将失去实时防护，且若开启了“篡改防护”可能无法生效，需先关闭篡改防护。\n"
                       "改完后需重启电脑生效。" % verb, danger=not target):
            return
        self.win.busy("正在%s Defender…" % verb)
        Tasks.run(self, lambda: S.set_defender(target),
                  lambda r: (self.win.idle(), self._load_toggles(),
                             info(self, "已%s Defender（部分需重启生效）。" % verb))[1],
                  on_fail=self._fail)

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
                "勾选状态会自动保存，下次打开自动恢复。" % pol)
        else:
            self.policy_note.setText(
                "在大类里点进去，自行勾选要修改的优化项。当前规则库里没有策略项。"
                "勾选状态会自动保存，下次打开自动恢复。")

        while self.cat_grid.count():
            it = self.cat_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        cats = []
        for r in self.rules:
            if r["category"] not in cats:
                cats.append(r["category"])
        for i, cat in enumerate(cats):
            lst = [r for r in self.rules if r["category"] == cat]
            done = len([r for r in lst if r.get("state") is True])
            picked = len([r for r in lst if self.sel.get(r["id"])])
            # 单列紧凑分组行（Win11 设置页写法）：图标 + 名称 + 状态 + 计数 + ›
            # 原先 3x3 等大方块是典型「AI 生成感」布局，改成列表后信息密度更高、更原生
            f = QFrame()
            f.setObjectName("CatCard")
            f.setCursor(Qt.PointingHandCursor)
            h = QHBoxLayout(f)
            h.setContentsMargins(16, 10, 12, 10)
            h.setSpacing(12)
            icon = QLabel(CAT_ICON.get(cat, "•"))
            icon.setObjectName("CatIcon")
            icon.setFixedWidth(22)
            icon.setAlignment(Qt.AlignCenter)
            name = QLabel(CAT_NAME.get(cat, cat))
            name.setObjectName("CatName")
            h.addWidget(icon)
            h.addWidget(name)
            h.addStretch(1)
            stat = QLabel("%d 项 · 已生效 %d" % (len(lst), done))
            stat.setObjectName("CatSub")
            h.addWidget(stat)
            if picked:
                h.addWidget(pill("已勾选 %d" % picked, "blue"))
            arrow = QLabel("›")
            arrow.setObjectName("CatArrow")
            arrow.setFixedWidth(14)
            arrow.setAlignment(Qt.AlignCenter)
            h.addWidget(arrow)
            def open_cat(_, c=cat):
                self.open_category(c)
            f.mousePressEvent = open_cat
            self.cat_grid.addWidget(f, i, 0)
        self.cat_grid.setRowStretch(self.cat_grid.rowCount(), 1)
        self.cat_grid.setColumnStretch(0, 1)
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
            if r.get("risk") == "policy":
                t.setText('<span style="color:%s;font-weight:600">%s</span>　'
                          '<span style="color:%s">【策略】</span>' % (_warn_c, r["name"], _warn_c))
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
        items = ["%s%s  —  %s" % (r["name"],
                                 "　【策略】" if r.get("risk") == "policy" else "",
                                 r.get("desc", ""))
                 for r in picked]
        if not safety_confirm(
                self, self.win.dark, "确认修改系统设置",
                "即将对以下 %d 项写入注册表/系统服务。修改后可能需要重启才能生效。"
                % len(picked),
                items, risky=len(pol),
                warn_text="建议先关闭其他正在运行的程序。本工具会自动记录改动前的原值，"
                          "可用「撤销上次优化」回退。"):
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
            self.refresh()
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
        self.refresh()

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
                                       self.refresh(),
                                       info(self, "已撤销 %s 的记录（恢复 %d 个值）" % r))[1],
                  on_fail=self._fail)


# ==========================================================================
# 3. 安全检测
# ==========================================================================
class SecurityPage(Page):
    title = "安全检测"

    PRESETS = [("桌面", "__desktop"), ("下载", "__downloads"), ("文档", "__docs"),
               ("D:\\", "D:\\"), ("C:\\", "C:\\"), ("自定义…", "__custom")]

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(notice(
            "<b>Synaptics / XRed 感染型病毒检测</b><br>"
            "只读扫描 PE 文件，比对病毒壳特征（CODE 节 629760 字节、MD5 "
            "33fbe30e…6542）以及 xred.mooo.com 等特征字符串。<b>不执行、不修改任何文件。</b>", "err"))

        f, v = card("扫描范围")
        bar = QHBoxLayout()
        self.combo = QComboBox()
        for name, _ in self.PRESETS:
            self.combo.addItem(name)
        self.combo.currentIndexChanged.connect(self.on_preset)
        self.path = QLineEdit()
        self.path.setPlaceholderText("例如 D:\\桌面\\桌面文件")
        self.path.setVisible(False)
        self.deep = QCheckBox("深度扫描（较慢）")
        bar.addWidget(self.combo)
        bar.addWidget(self.path, 1)
        bar.addWidget(self.deep)
        bar.addWidget(btn("开始扫描", "Primary", on_click=self.scan))
        v.addLayout(bar)
        self.status = QLabel("尚未扫描")
        self.status.setObjectName("Muted"); self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.table = make_table(["文件路径", "大小", "判定"])
        v.addWidget(self.table, 1)
        root.addWidget(f, 1)

    def on_preset(self, i):
        self.path.setVisible(self.PRESETS[i][1] == "__custom")

    def _base(self):
        key = self.PRESETS[self.combo.currentIndex()][1]
        home = os.path.expanduser("~")
        return {"__desktop": os.path.join(home, "Desktop"),
                "__downloads": os.path.join(home, "Downloads"),
                "__docs": os.path.join(home, "Documents")}.get(key, key)

    def scan(self):
        key = self.PRESETS[self.combo.currentIndex()][1]
        base = self.path.text().strip() if key == "__custom" else self._base()
        if not base or not os.path.isdir(base):
            return info(self, "目录不存在：%s" % base)
        self.status.setText("扫描中…")
        self.table.setRowCount(0)
        deep = self.deep.isChecked()
        self.win.busy("正在扫描…")
        Tasks.run(self, lambda: S.scan_path_for_infection(base, deep=deep),
                  self._done, on_fail=self._fail)

    def _fail(self, m):
        self.win.idle()
        self.status.setText("失败：" + m)

    def _done(self, r):
        self.win.idle()
        hits = r["hits"]
        fill_table(self.table, [[h["path"], fmt_bytes(h["size"]),
                                 "确认感染" if h["verdict"] == "infected-loader" else "可疑"]
                                for h in sorted(hits, key=lambda x: x["path"])])
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 90)
        if not hits:
            self.status.setText("扫描完成：检查 %d 个 PE 文件，未发现感染特征。" % r["scanned"])
        else:
            self.status.setText("扫描完成：检查 %d 个 PE，发现 %d 个异常（其中确认感染 %d 个）"
                                % (r["scanned"], len(hits),
                                   len([h for h in hits if h["verdict"] == "infected-loader"])))


# ==========================================================================
# 4. 软件管理
# ==========================================================================
class SoftwarePage(Page):
    title = "软件管理"

    SORTS = [("名称", "name"), ("大小", "size"), ("安装日期", "date")]

    def __init__(self, win):
        super().__init__(win)
        self.all = []
        self._icons = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

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
                                             ("系统 / 商店", "system"))):
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
        bar.addWidget(btn("残留扫描", on_click=self.leftover))
        bar.addWidget(btn("刷新", on_click=self.refresh))
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

        self.table = make_table(["程序名", "发布者", "版本", "大小", "安装日期"],
                                sortable=True)
        root.addWidget(self.table, 1)

        act = QHBoxLayout()
        self.btn_uninstall = btn("卸载所选", "Danger", on_click=self.uninstall)
        self.btn_uninstall.setEnabled(self.win.admin_mode)
        act.addWidget(self.btn_uninstall)
        act.addStretch(1)
        self.info_lb = QLabel(""); self.info_lb.setObjectName("Muted")
        self.info_lb.setWordWrap(True)
        act.addWidget(self.info_lb)
        root.addLayout(act)

    def refresh(self):
        Tasks.run(self, S.list_software, self._got)

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

    def toggle_order(self):
        self.sort_desc = not self.sort_desc
        self.btn_order.setText("降序" if self.sort_desc else "升序")
        self.filter()

    def set_kind(self, k):
        """切换「全部 / 我安装的 / 系统与商店」。"""
        self._kind = k
        self.filter()

    def filter(self):
        kw = self.q.text().strip().lower()
        kind = getattr(self, "_kind", "all")
        rows = [x for x in self.all
                if (kind == "all" or x.get("kind") == kind)
                and (not kw or kw in (x["name"] + " " + (x["publisher"] or "")).lower())]
        key = self.sort_combo.currentData() or "name"
        if key == "size":
            rows.sort(key=lambda x: x["size"] or 0, reverse=self.sort_desc)
        elif key == "date":
            rows.sort(key=lambda x: self._date_key(x["date"]), reverse=self.sort_desc)
        else:
            rows.sort(key=lambda x: x["name"].lower(), reverse=self.sort_desc)
        n_user = len([x for x in self.all if x.get("kind") == "user"])
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
            for c, v in enumerate([x["publisher"], x["version"],
                                   fmt_bytes(x["size"] * 1024) if x["size"] else "",
                                   x["date"]], start=1):
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

    def uninstall(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        it = self._current()
        if not it:
            return info(self, "请先在列表中选择一个程序。")
        cmd = it.get("quiet") or it.get("uninstall")
        if not cmd:
            return info(self, "该程序没有提供卸载命令，可能需要手动卸载。")
        if not confirm(self, "确认卸载",
                       "即将卸载：%s\n\n将调用它自带的卸载程序，可能弹出交互窗口。" % it["name"],
                       danger=True):
            return

        def do():
            if cmd.lower().startswith("msiexec"):
                return S.run(["cmd", "/c", cmd + " /qn /norestart"], timeout=600)[1][:300]
            return S.run(["cmd", "/c", "start", "", cmd], timeout=60)[1][:300]

        def done(_r):
            self.info_lb.setText("卸载命令已执行")
            if confirm(self, "残留扫描",
                       "卸载程序已运行。\n\n是否现在扫描 %s 留下的残余文件与注册表项？"
                       "\n（扫描只列出结果，删除前会再让你逐项勾选）" % it["name"]):
                self._scan_residual(it)

        self.info_lb.setText("已启动卸载程序…")
        Tasks.run(self, do, done)

    def _scan_residual(self, it):
        self.win.busy("正在扫描残留…")
        Tasks.run(self,
                  lambda: S.residual_scan(it["name"], it.get("publisher") or "",
                                          it.get("location") or ""),
                  lambda r: (self.win.idle(), ResidualDialog(self, it["name"], r,
                                                             on_done=self.refresh).exec())[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败：" + m))[1])

    def leftover(self):
        from PySide6.QtWidgets import QInputDialog
        kw, ok = QInputDialog.getText(self, "残留扫描", "输入已卸载程序的关键词（≥3 字符）：")
        if not ok or len(kw.strip()) < 3:
            return
        self.win.busy("正在扫描残留…")
        Tasks.run(self,
                  lambda: S.residual_scan(kw.strip(), "", ""),
                  lambda r: (self.win.idle(),
                             ResidualDialog(self, kw.strip(), r, on_done=self.refresh).exec())[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, "扫描失败：" + m))[1])


class ResidualDialog(QDialog):
    """卸载残留结果：逐项勾选后删除。"""

    def __init__(self, parent, name, result, on_done=None):
        super().__init__(parent)
        self.setWindowTitle("残留扫描 — %s" % name)
        self.resize(760, 560)
        self.on_done = on_done
        result = result or {}
        files = result.get("files") or []
        regs = result.get("reg") or []
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel("找到 %d 个文件/目录、%d 个注册表项。\n勾选要删除的项（已全选），确认后点下方按钮。"
                      % (len(files), len(regs)))
        head.setObjectName("Muted")
        head.setWordWrap(True)
        root.addWidget(head)

        root.addWidget(QLabel("残留文件 / 目录"))
        self.tbl_files = make_table(["", "路径", "类型", "大小"])
        for f in files:
            i = self.tbl_files.rowCount()
            self.tbl_files.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)
            self.tbl_files.setItem(i, 0, chk)
            for c, v in enumerate([f["path"],
                                   "目录" if f["kind"] == "dir" else "文件",
                                   fmt_bytes(f.get("bytes") or 0)], start=1):
                it = QTableWidgetItem(str(v))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(v))
                self.tbl_files.setItem(i, c, it)
        self.tbl_files.setColumnWidth(0, 34)
        self.tbl_files.setColumnWidth(1, 420)
        root.addWidget(self.tbl_files, 1)

        root.addWidget(QLabel("残留注册表项"))
        self.tbl_reg = make_table(["", "位置"])
        for rg in regs:
            i = self.tbl_reg.rowCount()
            self.tbl_reg.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)
            self.tbl_reg.setItem(i, 0, chk)
            it = QTableWidgetItem("%s\\%s" % (rg["hive"], rg["path"]))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setToolTip("%s\\%s" % (rg["hive"], rg["path"]))
            self.tbl_reg.setItem(i, 1, it)
        self.tbl_reg.setColumnWidth(0, 34)
        self.tbl_reg.setColumnWidth(1, 560)
        root.addWidget(self.tbl_reg, 1)

        hb = QHBoxLayout()
        self.btn_del = btn("删除勾选项", "Danger", on_click=self._clean)
        self.btn_del.setEnabled(bool(files or regs))
        hb.addWidget(self.btn_del)
        hb.addWidget(btn("全不选", on_click=lambda: self._check_all(False)))
        hb.addWidget(btn("全选", on_click=lambda: self._check_all(True)))
        hb.addStretch(1)
        hb.addWidget(btn("关闭", on_click=self.reject))
        root.addLayout(hb)
        self._result = None

    def _check_all(self, on):
        state = Qt.Checked if on else Qt.Unchecked
        for t in (self.tbl_files, self.tbl_reg):
            for r in range(t.rowCount()):
                t.item(r, 0).setCheckState(state)

    def _collect(self, table, n_extra):
        out = []
        for r in range(table.rowCount()):
            if table.item(r, 0).checkState() == Qt.Checked:
                if n_extra == 2:      # files 表：路径列在 1
                    kind = table.item(r, 2).text()
                    out.append({"path": table.item(r, 1).text(),
                                "kind": "dir" if kind == "目录" else "file"})
                else:                 # reg 表：hive\path 在 1
                    full = table.item(r, 1).text()
                    hive, _, path = full.partition("\\")
                    out.append({"hive": hive, "path": path})
        return out

    def _clean(self):
        files = self._collect(self.tbl_files, 2)
        regs = self._collect(self.tbl_reg, 1)
        if not files and not regs:
            return info(self, "没有勾选任何项。")
        if not confirm(self, "确认删除",
                       "即将删除 %d 个文件/目录 和 %d 个注册表项。\n\n此操作不可撤销，请确认没有勾错。" %
                       (len(files), len(regs)), danger=True):
            return
        self.btn_del.setEnabled(False)
        Tasks.run(self, lambda: S.residual_clean(files, regs), self._cleaned,
                  on_fail=lambda m: (self._enable(), warn(self, "删除失败：" + m)))

    def _enable(self):
        self.btn_del.setEnabled(True)

    def _cleaned(self, r):
        self._enable()
        ok = (r or {}).get("ok")
        msgs = (r or {}).get("msg") or []
        if self.on_done:
            self.on_done()              # 先触发列表刷新（异步），再弹模态对话框
        QMessageBox.information(self, "清理完成" if ok else "清理完成（部分失败）",
                                "\n".join(msgs[:30]) or "完成")
        self.accept()


# ==========================================================================
# 5.5 CPU 调试（大小核调度 / 电源 / EPP / 进程亲和）
# ==========================================================================
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

        # ---- 2. 调度控制 ----
        f2, v2 = card("调度控制（当前电源计划）")
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("电源计划："))
        self.plan_combo = QComboBox()
        self.plan_combo.currentIndexChanged.connect(self.on_plan_selected)
        r1.addWidget(self.plan_combo, 1)
        self.btn_plan = btn("切换", "Primary", on_click=self.apply_plan)
        r1.addWidget(self.btn_plan)
        v2.addLayout(r1)
        self.lb_plan_state = QLabel("")
        self.lb_plan_state.setObjectName("Muted")
        v2.addWidget(self.lb_plan_state)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("处理器最小状态 %："))
        self.spin_min = QSpinBox(); self.spin_min.setRange(0, 100)
        r2.addWidget(self.spin_min)
        r2.addWidget(QLabel("最大状态 %："))
        self.spin_max = QSpinBox(); self.spin_max.setRange(0, 100)
        r2.addWidget(self.spin_max)
        self.btn_states = btn("应用", "Primary", on_click=self.apply_states)
        r2.addWidget(self.btn_states)
        r2.addStretch(1)
        v2.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("能效偏好 EPP（0=最高性能，100=最省电）："))
        self.slider_epp = QSlider(Qt.Horizontal)
        self.slider_epp.setRange(0, 100)
        self.lb_epp = QLabel("—"); self.lb_epp.setObjectName("Mono")
        self.slider_epp.valueChanged.connect(lambda v: self.lb_epp.setText(str(v)))
        r3.addWidget(self.slider_epp, 1)
        r3.addWidget(self.lb_epp)
        self.btn_epp = btn("应用", "Primary", on_click=self.apply_epp)
        r3.addWidget(self.btn_epp)
        v2.addLayout(r3)
        root.addWidget(f2)

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
        self.btn_procs = btn("刷新进程", on_click=self.load_procs)
        r4.addWidget(self.btn_procs)
        v3.addLayout(r4)

        # 进程列表：图标 + 名称 / PID / 内存 / CPU / GPU（与内存性能页同款交互）
        self.proc_table = make_table(["进程", "PID", "内存", "CPU", "GPU"], sortable=True)
        self.proc_table.setMaximumHeight(232)
        self.proc_icons = {}
        v3.addWidget(self.proc_table)

        self.lb_aff = QLabel(""); self.lb_aff.setObjectName("Muted")
        self.lb_aff.setWordWrap(True)
        v3.addWidget(self.lb_aff)
        root.addWidget(f3)

        # ---- 4. E-core 全局开关 ----
        f4, v4 = card("E-core 全局开关（高级）")
        v4.addWidget(notice(
            "通过 bcdedit numproc 让 Windows 只使用 P 核，需重启生效。\n"
            "风险：个别驱动/授权校验依赖完整核心数；恢复同样在这里点“恢复全部核心”。", "warn"))
        self.lb_ecore = QLabel("当前：读取中…")
        r5 = QHBoxLayout()
        r5.addWidget(self.lb_ecore, 1)
        r5.addWidget(btn("禁用 E-core（重启生效）", "Danger", on_click=self.ecore_off))
        r5.addWidget(btn("恢复全部核心", on_click=self.ecore_on))
        v4.addLayout(r5)
        root.addWidget(f4)

        root.addStretch(1)

        # ---- 定时器 ----
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    # ---------- 生命周期 ----------
    def refresh(self):
        if self.topo is None:
            Tasks.run(self, S.cpu_topology, self._got_topo)
        Tasks.run(self, S.cpu_plans, self._got_plans, on_fail=lambda *_: None)
        Tasks.run(self, S.cpu_proc_states, self._got_states, on_fail=lambda *_: None)
        Tasks.run(self, S.ecore_status, self._got_ecore, on_fail=lambda *_: None)
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

    def tick(self):
        if not self.isVisible():
            return
        if self.topo is None and not getattr(self, "_topo_loading", False):
            self._topo_loading = True
            Tasks.run(self, S.cpu_topology, self._got_topo,
                      on_fail=lambda *_: setattr(self, "_topo_loading", False))
        Tasks.run(self, S.cpu_per_core, self._got_core, on_fail=lambda *_: None)
        Tasks.run(self, S.cpu_temp, lambda v: self.pill_temp.setText(
            "温度 %s" % fmt_temp(v)), on_fail=lambda *_: None)

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
        self.on_plan_selected()          # 选定计划后立即读取它的参数

    def on_plan_selected(self):
        """切换下拉时读取该计划存储的处理器参数（含未生效的计划）。"""
        guid = self.plan_combo.currentData()
        active = next((p for p in getattr(self, "_plans", []) if p.get("active")), None)
        if active and guid == active.get("guid"):
            self.lb_plan_state.setText("该计划当前生效，修改参数立即生效。")
        else:
            self.lb_plan_state.setText("该计划未生效：显示的是它保存的参数，"
                                       "点“应用”写入后，切换到它时生效。")
        if not guid:
            return
        Tasks.run(self, lambda: S.cpu_proc_states(guid), self._got_states,
                  on_fail=lambda *_: None)

    def apply_plan(self):
        guid = self.plan_combo.currentData()
        if not guid:
            return
        r = S.cpu_set_plan(guid)
        if r.get("ok"):
            self.refresh()
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
            self.lb_epp.setText("不支持")

    def apply_states(self):
        mn, mx = self.spin_min.value(), self.spin_max.value()
        guid = self.plan_combo.currentData()
        if mn > mx:
            return warn(self, "最小状态不能大于最大状态。")
        Tasks.run(self,
                  lambda: (S.cpu_set_state("min", mn, guid), S.cpu_set_state("max", mx, guid)),
                  lambda r: info(self, "已写入该计划。")
                  if all(x.get("ok") for x in r) else
                  warn(self, "应用失败：%s" % "; ".join(x.get("err", "") for x in r if not x.get("ok"))),
                  on_fail=lambda m: warn(self, "失败：" + m))

    def apply_epp(self):
        v = self.slider_epp.value()
        guid = self.plan_combo.currentData()
        Tasks.run(self, lambda: S.cpu_set_state("epp", v, guid),
                  lambda r: info(self, "EPP=%d 已写入该计划。" % v)
                  if r.get("ok") else warn(self, "应用失败：%s" % r.get("err")),
                  on_fail=lambda m: warn(self, "失败：" + m))

    # ---------- 大小核 ----------
    def load_procs(self):
        Tasks.run(self, lambda: S.list_processes(300), self._got_procs,
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

    # ---------- E-core 全局 ----------
    def _got_ecore(self, st):
        if not st:
            self.lb_ecore.setText("当前：读取失败")
            return
        if st.get("set"):
            self.lb_ecore.setText("当前：numproc=%d（已限制核心数，重启后生效中）" % st["numproc"])
        else:
            self.lb_ecore.setText("当前：未限制（全部核心启用）")

    def ecore_off(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        if not confirm(self, "禁用 E-core",
                       "将执行 bcdedit 设置 numproc 只保留 P 核，重启电脑后生效。\n\n"
                       "副作用：多线程性能下降；个别软件授权校验可能异常。\n"
                       "随时可在本页点“恢复全部核心”撤销。继续？", danger=True):
            return
        Tasks.run(self, S.ecore_disable,
                  lambda r: (self.refresh(),
                             info(self, "已设置 numproc=%d，重启后生效。" % r["numproc"])
                             if r.get("ok") else
                             warn(self, "失败：%s" % r.get("err")))[1],
                  on_fail=lambda m: warn(self, "失败：" + m))

    def ecore_on(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        Tasks.run(self, S.ecore_restore,
                  lambda r: (self.refresh(),
                             info(self, "已恢复全部核心，重启后生效。") if r.get("ok")
                             else warn(self, "失败：%s（可能本来就没限制）" % r.get("err")))[1],
                  on_fail=lambda m: warn(self, "失败：" + m))


# ==========================================================================
# 5. 启动与服务
# ==========================================================================
class StartupPage(Page):
    title = "启动与服务"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        w1 = QWidget(); l1 = QVBoxLayout(w1); l1.setContentsMargins(14, 14, 14, 14); l1.setSpacing(10)
        b1 = QHBoxLayout()
        self.btn_rm_startup = btn("移除所选启动项", "Danger", on_click=self.remove_startup)
        self.btn_rm_startup.setEnabled(self.win.admin_mode)
        b1.addWidget(self.btn_rm_startup)
        b1.addWidget(btn("刷新", on_click=self.refresh))
        b1.addStretch(1)
        self.s_n = pill("0 项", "")
        b1.addWidget(self.s_n)
        l1.addLayout(b1)
        self.t1 = make_table(["名称", "位置", "命令"], sortable=True)
        l1.addWidget(self.t1, 1)
        self.tabs.addTab(w1, "开机启动项")

        w2 = QWidget(); l2 = QVBoxLayout(w2); l2.setContentsMargins(14, 14, 14, 14); l2.setSpacing(10)
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
        l2.addWidget(self.t2, 1)
        self.tabs.addTab(w2, "系统服务")

        w3 = QWidget(); l3 = QVBoxLayout(w3); l3.setContentsMargins(14, 14, 14, 14); l3.setSpacing(10)
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
        l3.addWidget(self.t3, 1)
        self.tabs.addTab(w3, "计划任务")

    def refresh(self):
        Tasks.run(self, S.list_startup, self._got_startup)
        Tasks.run(self, S.list_services, self._got_services)
        Tasks.run(self, S.list_tasks, self._got_tasks)

    def _got_startup(self, lst):
        self.startup = lst
        self.s_n.setText("%d 项" % len(lst))
        fill_table(self.t1, [[x["name"], x["hive"] + "\\" + x["path"] if x["hive"] != "DIR" else "启动文件夹",
                              x["cmd"]] for x in lst],
                   icons=[file_icon(exe_from_cmd(x.get("exe"))) for x in lst],
                   keys=[i for i in range(len(lst))])   # 排序后靠它找回原始项
        self.t1.setColumnWidth(0, 220)
        self.t1.setColumnWidth(1, 240)

    def remove_startup(self):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        idx = picked_key(self.t1)
        if idx is None or not (0 <= idx < len(getattr(self, "startup", []))):
            return info(self, "请先选择一个启动项。")
        x = self.startup[idx]
        if x["hive"] == "DIR":
            return info(self, "启动文件夹中的项请手动处理。")
        if not confirm(self, "移除启动项", "删除 %s 的自启动注册表项？" % x["name"], danger=True):
            return
        Tasks.run(self, lambda: S.reg_delete(x["hive"] + "\\" + x["path"], x["name"]),
                  lambda r: (self.refresh(), info(self, "已移除"))[0])

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

    def svc(self, action):
        if not self.win.admin_mode:
            return self.win._warn_readonly()
        name = picked_key(self.t2)          # 排序后行号会变，服务名存在单元格里
        if not name:
            return info(self, "请先选择一个服务。")
        if action == "disable" and not confirm(self, "禁用服务",
                                               "确定禁用 %s？可能影响依赖它的功能。" % name,
                                               danger=True):
            return

        def done(res):
            ok, note = res
            self.refresh()                 # 先刷新，再处理提示
            if not ok:
                warn(self, note or "操作失败")
        self.win.busy("正在操作服务…")
        Tasks.run(self, lambda: S.set_service(name, action),
                  lambda res: (self.win.idle(), done(res))[1],
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])

    # ---------- 计划任务 ----------
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
            self.refresh()                  # 先启动刷新，再弹对话框
            if res[0]:
                info(self, "已提交，正在刷新状态…")
            else:
                warn(self, res[1] or "操作失败")
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
        b.addWidget(btn("刷新进程列表", on_click=self.load_procs))
        v4.addLayout(b)
        self.table = make_table(["进程", "PID", "内存占用", "CPU", "GPU"], sortable=True)
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
        self.tick()
        self.tick_perf()
        self.tick_net()

    def tick(self):
        if not self.isVisible():
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
        if not self.isVisible():
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
        Tasks.run(self, S.perf_stats, got, on_fail=lambda *_: None)

    def tick_net(self):
        """网络速率卡片：1 秒实时（服务端常驻采样）。"""
        if not self.isVisible():
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
        Tasks.run(self, S.net_info, got, on_fail=lambda *_: None)

    def refresh(self):
        self.load_procs()

    def load_procs(self):
        Tasks.run(self, lambda: S.list_processes(60), self._got_procs)

    def _got_procs(self, lst):
        """进程表：进程 / PID / 内存 / CPU / GPU。
        CPU·GPU 列按数值排序（UserRole+1 存原始值），不是按字符串。"""
        self.procs = lst
        tbl = self.table
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
                tbl.setItem(i, c, it)
            tbl.setRowHeight(i, 30)
        tbl.setSortingEnabled(True)
        tbl.setColumnWidth(0, 300)
        tbl.setColumnWidth(1, 76)
        tbl.setColumnWidth(2, 100)
        tbl.setColumnWidth(3, 70)

    def trim_one(self):
        # 表格可排序 → 行号与 self.procs 不再对应，PID 从单元格里取
        r = self.table.currentRow()
        it = self.table.item(r, 1) if r >= 0 else None
        pid = it.data(Qt.UserRole + 1) if it else None
        if not pid:
            return info(self, "请先选择一个进程。")
        Tasks.run(self, lambda: S.trim_process(int(pid)),
                  lambda ok: (self.load_procs(),            # 先刷新，再弹提示
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
# 8. 内网测速（iperf3）
# ==========================================================================
class SpeedTestPage(Page):
    title = "内网测速"

    def __init__(self, win):
        super().__init__(win)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        # ---------- 0. 环境 / 网卡信息 ----------
        f_env, ve = card("本机网络")
        self.env_box = QVBoxLayout(); self.env_box.setSpacing(6)
        ve.addLayout(self.env_box)
        self.lan_lb = QLabel("检测中…"); self.lan_lb.setObjectName("Mono")
        self.lan_lb.setWordWrap(True)
        ve.addWidget(self.lan_lb)
        root.addWidget(f_env)

        # ---------- 1. 本机做服务端 ----------
        f_srv, vs = card("① 本机做服务端")
        vs.addWidget(notice(
            "启动后，同一局域网内的手机 / 电脑 / NAS 可用 iperf3 对本机测速：<br>"
            "<code>iperf3 -c &lt;本机IP&gt; -p 5201</code>（下载：加 <code>-R</code>）<br>"
            "需确保 Windows 防火墙允许 iperf3 通过。", "info"))
        row1 = QHBoxLayout(); row1.setSpacing(10)
        self.srv_port = QSpinBox(); self.srv_port.setRange(1024, 65535); self.srv_port.setValue(5201)
        row1.addWidget(QLabel("端口："))
        row1.addWidget(self.srv_port)
        self.srv_btn = btn("启动服务端", "Primary", on_click=self.toggle_server)
        row1.addWidget(self.srv_btn)
        row1.addStretch(1)
        vs.addLayout(row1)
        self.srv_status = QLabel("服务端未启动"); self.srv_status.setObjectName("Muted")
        self.srv_status.setWordWrap(True)
        vs.addWidget(self.srv_status)
        root.addWidget(f_srv)

        # ---------- 2. 本机做客户端 ----------
        f_cli, vc = card("② 本机做客户端")
        vc.addWidget(notice(
            "目标设备需先运行 <code>iperf3 -s</code>（如路由器 / NAS / 另一台电脑）。"
            "下载方向用 <code>-R</code> 反向测试。", "info"))
        row2 = QHBoxLayout(); row2.setSpacing(10)
        self.host = QLineEdit("")
        self.host.setPlaceholderText("目标 IP，例如 192.168.1.1")
        self.cli_port = QSpinBox(); self.cli_port.setRange(1024, 65535); self.cli_port.setValue(5201)
        self.cli_secs = QSpinBox(); self.cli_secs.setRange(3, 60); self.cli_secs.setValue(8)
        row2.addWidget(QLabel("目标："))
        row2.addWidget(self.host, 1)
        row2.addWidget(QLabel("端口："))
        row2.addWidget(self.cli_port)
        row2.addWidget(QLabel("时长(秒)："))
        row2.addWidget(self.cli_secs)
        vc.addLayout(row2)

        row3 = QHBoxLayout(); row3.setSpacing(10)
        self.dir_combo = QComboBox()
        self.dir_combo.addItem("下载（服务端 → 本机）", "down")
        self.dir_combo.addItem("上传（本机 → 服务端）", "up")
        self.proto_combo = QComboBox()
        self.proto_combo.addItem("TCP", "tcp")
        self.proto_combo.addItem("UDP", "udp")
        self.par_spin = QSpinBox(); self.par_spin.setRange(1, 16); self.par_spin.setValue(1)
        row3.addWidget(QLabel("方向："))
        row3.addWidget(self.dir_combo)
        row3.addWidget(QLabel("协议："))
        row3.addWidget(self.proto_combo)
        row3.addWidget(QLabel("并发流："))
        row3.addWidget(self.par_spin)
        self.cli_btn = btn("开始测速", "Primary", on_click=self.do_client)
        row3.addWidget(self.cli_btn)
        row3.addStretch(1)
        vc.addLayout(row3)

        self.cli_status = QLabel("尚未测速"); self.cli_status.setObjectName("Muted")
        self.cli_status.setWordWrap(True)
        vc.addWidget(self.cli_status)
        self.cli_tbl = make_table(["方向", "带宽", "字节", "耗时", "结果"])
        vc.addWidget(self.cli_tbl)
        root.addWidget(f_cli)

        # ---------- 3. 历史结果 ----------
        f_his, vh = card("测速记录")
        self.his_tbl = make_table(["时间", "目标", "方向", "协议", "带宽"])
        vh.addWidget(self.his_tbl)
        root.addWidget(f_his)

        root.addStretch(0)
        self._srv_running = False
        self._history = []

    # ---------- 环境 ----------
    def refresh(self):
        self.load_env()

    def load_env(self):
        def got(d):
            d = d or {}
            prim = d.get("primary") or {}
            kind = prim.get("kind") or "未知"
            nm = prim.get("name") or "—"
            desc = prim.get("desc") or ""
            ls = prim.get("link_mbps")
            self.lan_lb.setText(
                "本机 IP：%s    网卡：%s（%s）    协商速率：%s"
                % (d.get("lan_ip") or "—", nm, kind, fmt_linkspeed(ls)))
            if desc:
                self.lan_lb.setText(self.lan_lb.text() + "    " + desc)
        def both():
            d = S.net_info() or {}
            d["lan_ip"] = S.lan_ip()
            return d
        Tasks.run(self, both, got, on_fail=lambda *_: None)
        self._sync_server_status()

    def _sync_server_status(self):
        st = S.iperf_server_status()
        running = bool(st.get("running"))
        self._srv_running = running
        if running:
            self.srv_btn.setText("停止服务端")
            self.srv_btn.setObjectName("Danger")
            self.srv_status.setText("服务端运行中 · 端口 %d · 本机 IP %s"
                                    % (st.get("port", 5201), S.lan_ip()))
        else:
            self.srv_btn.setText("启动服务端")
            self.srv_btn.setObjectName("Primary")
            self.srv_status.setText("服务端未启动")
        self.srv_btn.style().unpolish(self.srv_btn)
        self.srv_btn.style().polish(self.srv_btn)
        fit_label(self.srv_status)

    # ---------- 服务端 ----------
    def toggle_server(self):
        if self._srv_running:
            S.iperf_server_stop()
            self._sync_server_status()
            return
        port = self.srv_port.value()
        r = S.iperf_server_start(port)
        if not (r or {}).get("ok"):
            info(self, "启动失败：%s" % ((r or {}).get("err") or "未知错误"))
        self._sync_server_status()

    # ---------- 客户端 ----------
    def do_client(self):
        host = self.host.text().strip()
        if not host:
            info(self, "请先填写目标设备的 IP 地址。")
            return
        direction = self.dir_combo.currentData()
        proto = self.proto_combo.currentData()
        port = self.cli_port.value()
        secs = self.cli_secs.value()
        par = self.par_spin.value()
        reverse = (direction == "down")
        self.cli_btn.setEnabled(False)
        self.cli_status.setText("测速中… （约 %d 秒）" % secs)
        fit_label(self.cli_status)

        def job():
            return S.iperf_client_run(host, port, secs, reverse=reverse,
                                      parallel=par, udp=(proto == "udp"))

        def done(r):
            self.cli_btn.setEnabled(True)
            r = r or {}
            self.cli_tbl.setRowCount(0)
            if not r.get("ok"):
                self.cli_status.setText("测速失败：%s" % (r.get("err") or "未知错误"))
                fit_label(self.cli_status)
                return
            mbps = r.get("rx_mbps") if direction == "down" else r.get("tx_mbps")
            label = "下载" if direction == "down" else "上传"
            speed = "%.1f Mbps" % (mbps or 0)
            if (mbps or 0) >= 1000:
                speed = "%.2f Gbps" % ((mbps or 0) / 1000.0)
            fill_table(self.cli_tbl, [[label, speed, "—", "%ds" % secs,
                                       "%.1f MB/s" % ((mbps or 0) / 8.0)]])
            self.cli_status.setText("完成：%s %s" % (label, speed))
            fit_label(self.cli_status)
            self._history.insert(0, (time.strftime("%H:%M:%S"), host,
                                     label, proto.upper(), speed))
            fill_table(self.his_tbl, self._history[:20])
        Tasks.run(self, job, done,
                  on_fail=lambda e: (self.cli_btn.setEnabled(True),
                                     self.cli_status.setText("测速出错：%s" % e)))


# ==========================================================================
# 9. DNS 检测
# ==========================================================================
class DNSPage(Page):
    title = "DNS 检测"

    MODES = [("国内", "domestic"), ("国外", "foreign"), ("混合", "mixed"),
             ("IPv6", "ipv6")]

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
            "用原生 UDP DNS 查询直接测各 DNS 服务器解析该域名的延迟，并显示解析到的 IP。", "info"))
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
        bar.addWidget(btn("开始测试", "Primary", on_click=self.test))
        vd.addLayout(bar)
        vd.addWidget(self.status)
        self.table = make_table(["DNS 服务器", "分类", "解析结果", "延迟", "状态"], sortable=True)
        vd.addWidget(self.table)
        root.addWidget(f_dns)
        root.addStretch(0)

        self._worker = None
        self._ping_stats = {}

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
    def test(self):
        mode = self.combo.currentData()
        domain = self.domain.text().strip() or "www.baidu.com"
        self.status.setText("正在测试…")
        self.table.setRowCount(0)
        self.win.busy("正在测试 DNS…")
        Tasks.run(self, lambda: S.dns_test(mode, domain), self._done,
                  on_fail=lambda m: (self.win.idle(), self.status.setText("失败：" + m)))

    def _done(self, r):
        self.win.idle()
        rows = []
        for x in r:
            rows.append([
                "%s (%s)" % (x["name"], x["ip"]),
                x.get("cat") or "—",
                x.get("answer") or "—",
                ("%d ms" % x["ms"]) if x["ok"] and x["ms"] is not None else "—",
                "可用" if x["ok"] else "不可用"
            ])
        fill_table(self.table, rows)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 56)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 80)
        ok = [x for x in r if x["ok"]]
        if ok and ok[0].get("ms") is not None:
            self.status.setText("最快：%s（%d ms），共 %d / %d 个可用，解析到 %s" %
                                (ok[0]["name"], ok[0]["ms"], len(ok), len(r),
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
        bar.addWidget(btn("开始扫描", "Primary", on_click=self.scan))
        self.btn_clean = btn("清理所选", "Danger", on_click=self.clean)
        self.btn_clean.setEnabled(self.win.admin_mode)
        bar.addWidget(self.btn_clean)
        bar.addStretch(1)
        self.sum = pill("—", "")
        bar.addWidget(self.sum)
        root.addLayout(bar)
        root.addWidget(notice(
            "扫描只统计体积，不会删除东西。清理时正在被占用的文件会自动跳过。<br>"
            "<b>建议清理</b>（绿色）＝删了没有任何影响；<b>可以清理</b>（蓝色）＝删了会重新生成，"
            "首次打开可能稍慢；<b>谨慎清理</b>（橙色）＝涉及回收站或系统缓存，默认不勾选，"
            "请确认后再勾。每项的「说明」列写明了这些文件是什么。", "info"))

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
        self.table = make_table(["", "项目", "建议", "路径", "体积", "文件数", "说明"],
                                sortable=True)
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

    def scan(self):
        """并行扫描所有垃圾目录：期间显示进度条与百分比，扫完一次性列出结果。"""
        self._scan_token = getattr(self, "_scan_token", 0) + 1
        token = self._scan_token
        self.table.setRowCount(0)
        self.items = []
        targets = [t["id"] for t in S.junk_targets()] + ["recyclebin"]
        total = len(targets)
        self.prog_row.setVisible(True)
        self.prog.setRange(0, total)
        self.prog.setValue(0)
        self.prog_lb.setText("正在扫描 0/%d…" % total)
        self.sum.setText("扫描中…")
        self.win.busy("正在扫描垃圾文件…")
        self._res = {}
        self._done_n = 0

        for tid in targets:
            Tasks.run(self, lambda t=tid: S.junk_item_size(t),
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

    def _finish(self, token):
        if token != getattr(self, "_scan_token", 0):
            return
        order = [t["id"] for t in S.junk_targets()] + ["recyclebin"]
        items = [self._res[i] for i in order if i in self._res]
        self.items = items
        # 扫完一次性填表（填表期间关排序，避免行错位）
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for x in items:
            i = self.table.rowCount()
            self.table.insertRow(i)
            lv = x.get("level") or "ok"
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # 默认只勾「建议清理 / 可以清理」，谨慎项留空由用户自己决定
            cb.setCheckState(Qt.Unchecked if lv == "caution" else Qt.Checked)
            cb.setData(Qt.UserRole, x["id"])     # 排序后仍能对上数据
            self.table.setItem(i, 0, cb)

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
            self.table.setItem(i, 2, it_lv)

            what = x.get("what") or ""
            for col, val in ((1, x["name"]), (3, x["path"]), (4, fmt_bytes(x["size"])),
                             (5, x["files"]), (6, what)):
                it = SortItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                num = _sort_num(val)
                if num is not None:
                    it.setData(Qt.UserRole + 1, num)
                it.setToolTip(str(val))
                self.table.setItem(i, col, it)
            self.table.setRowHeight(i, 40)
        self.table.setSortingEnabled(True)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 82)
        self.table.setColumnWidth(3, 280)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 70)
        self.table.setColumnWidth(6, 330)
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
        if not confirm(self, "确认清理",
                       "将删除约 %s 的临时/缓存文件。\n\n⚠️ 删除后无法恢复。%s"
                       % (fmt_bytes(total), tip), danger=True):
            return
        self.win.busy("正在清理…")
        def _on_cleaned(r):
            self.win.idle()
            freed = sum(x.get("freed", 0) for x in r)
            self.scan()                      # 先启动扫描（异步），对话框期间后台已在跑
            info(self, "清理完成，释放 %s" % fmt_bytes(freed))
        Tasks.run(self, lambda: S.clean_junk(ids), _on_cleaned,
                  on_fail=lambda m: (self.win.idle(), warn(self, m))[1])


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
        bar.addWidget(btn("扫描策略残留", "Primary", on_click=self.scan))
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
                  on_fail=lambda m: (self.win.idle(), self.prog_row.setVisible(False),
                                     warn(self, m))[-1])

    def _got(self, found):
        self.found = found
        # 填表期间必须关排序，否则行会错位/留空行
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
                it = QTableWidgetItem(str(val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setToolTip(str(val))
                self.table.setItem(i, c, it)
            self.table.setRowHeight(i, 34)
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

        f1, v1 = card("自动备份")
        v1.addWidget(notice(
            "关闭软件时会<b>自动保存</b>当前设置（优化项勾选、过滤开关等）到程序目录的 "
            "settings.json，下次启动<b>自动恢复</b>，不怕重新调整后找不回。", "info"))
        self.lb_path = QLabel("配置文件：—")
        self.lb_path.setObjectName("Mono"); self.lb_path.setWordWrap(True)
        v1.addWidget(self.lb_path)
        h = QHBoxLayout()
        h.addWidget(btn("立即备份", "Primary", on_click=self.save_now))
        h.addWidget(btn("恢复上次设置", on_click=self.restore_now))
        h.addStretch(1)
        v1.addLayout(h)
        root.addWidget(f1)

        f2, v2 = card("导入 / 导出")
        v2.addWidget(notice("导出的 .json 可以拷到别的电脑导入，实现一套设置多机复用。", "info"))
        h2 = QHBoxLayout()
        h2.addWidget(btn("导出设置…", on_click=self.export_cfg))
        h2.addWidget(btn("导入设置…", on_click=self.import_cfg))
        h2.addStretch(1)
        v2.addLayout(h2)
        root.addWidget(f2)

        # ---- 外观：主题色（Win11 风格：常用色块 + 调色盘） ----
        f_ac, av = card("外观")
        av.addWidget(notice(
            "界面强调色，作用于按钮、选中行、开关、进度条、导航高亮等。"
            "CPU / 内存 / GPU 等性能曲线各自使用独立颜色，不受此处影响。", "info"))
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
        srow.addSpacing(12)
        srow.addWidget(btn("自定义颜色…", on_click=self.pick_accent))
        srow.addWidget(btn("跟随系统", on_click=lambda: self.win.set_accent("")))
        srow.addStretch(1)
        av.addLayout(srow)

        crow = QHBoxLayout(); crow.setSpacing(10)
        self.chip_accent = QLabel(); self.chip_accent.setObjectName("AccentChip")
        self.lb_accent = QLabel(""); self.lb_accent.setObjectName("Muted")
        self.lb_accent.setWordWrap(True)
        crow.addWidget(self.chip_accent)
        crow.addWidget(self.lb_accent, 1)
        av.addLayout(crow)
        root.addWidget(f_ac)

        f_pref, pv = card("偏好设置")
        g = QGridLayout(); g.setSpacing(12)
        self.cb_startup = QCheckBox("开机自动启动 WinToolbox")
        self.cb_tray = QCheckBox("关闭时最小化到系统托盘")
        self.cb_startup.setChecked(bool(self.win.settings.get("startup", False)))
        self.cb_tray.setChecked(bool(self.win.settings.get("minimize_to_tray", False)))
        self.cb_startup.toggled.connect(self.on_startup)
        self.cb_tray.toggled.connect(self.on_tray)
        g.addWidget(self.cb_startup, 0, 0)
        g.addWidget(self.cb_tray, 0, 1)

        g.addWidget(QLabel("窗口背景不透明度："), 1, 0)
        h_op = QHBoxLayout()
        self.slider_op = QSlider(Qt.Horizontal)
        self.slider_op.setRange(20, 100)
        self.slider_op.setToolTip("只影响窗口背景。文字始终保持实心，"
                                  "调到最低 20% 也能看清全部内容。")
        self.slider_op.setValue(int(max(20, min(100,
                                                self.win.settings.get("opacity", 100)))))
        self.spin_op = QSpinBox()
        self.spin_op.setRange(20, 100)
        self.spin_op.setSuffix("%")
        self.spin_op.setValue(self.slider_op.value())
        self.slider_op.valueChanged.connect(self.spin_op.setValue)
        self.spin_op.valueChanged.connect(self.slider_op.setValue)
        self.slider_op.valueChanged.connect(self.on_opacity)
        h_op.addWidget(self.slider_op, 1)
        h_op.addWidget(self.spin_op)
        g.addLayout(h_op, 1, 1)
        g.addWidget(QLabel("提示：透明度仅作用于背景色，文字不会跟着变淡。"),
                    2, 0, 1, 2)
        pv.addLayout(g)
        root.addWidget(f_pref)

        f3, v3 = card("关于")
        rows = [("软件名称", APP_NAME), ("版本", "v%s" % APP_VER),
                ("作者", "by：%s" % AUTHOR),
                ("权限", "管理员 ✓" if IS_ADMIN else "普通（功能受限）"),
                ("数据目录", S.APP_DIR)]
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
        hb.addWidget(btn("打开 GitHub 项目主页", "Primary",
                         on_click=lambda: self.open_github()))
        hb.addStretch(1)
        v3.addLayout(g)
        v3.addLayout(hb)
        root.addWidget(f3)
        root.addStretch(1)

    def open_github(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(GITHUB_URL))

    def refresh(self):
        self.lb_path.setText("配置文件：%s" % SETTINGS_FILE)
        self._paint_swatches()

    def pick_accent(self):
        """调色盘任选主题色。"""
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor(T.get_accent()), self, "选择主题色")
        if c.isValid():
            self.win.set_accent(c.name())

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
            opt.sel = {k: bool(v) for k, v in data.get("optimize_sel", {}).items()}
            opt._update_safe_hint()
            opt.build_root()
        info(self, "已恢复上次保存的设置。")

    def export_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出设置", "WinToolbox设置.json",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.win.settings, f, ensure_ascii=False, indent=1)
            info(self, "已导出到：\n%s" % path)
        except Exception as e:
            warn(self, str(e))

    def import_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入设置", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("不是有效的设置文件")
            self.win.settings.update(data)
            settings_save(self.win.settings)
            opt = self.win.pages.get("optimize")
            if opt:
                opt.sel = {k: bool(v) for k, v in data.get("optimize_sel", {}).items()}
                opt._update_safe_hint()
                opt.build_root()
            info(self, "导入成功。")
            # import 后同步偏好设置
            self.cb_startup.setChecked(bool(self.win.settings.get("startup", False)))
            self.cb_tray.setChecked(bool(self.win.settings.get("minimize_to_tray", False)))
            op = max(20, min(100, int(self.win.settings.get("opacity", 100))))
            self.slider_op.setValue(op)
            self.spin_op.setValue(op)
            self.win.set_opacity(op)
        except Exception as e:
            warn(self, "导入失败：%s" % e)

    def on_startup(self, v):
        ok = set_startup(bool(v))
        self.win.settings["startup"] = bool(v)
        if not ok:
            self.cb_startup.setChecked(False)
            warn(self, "设置开机启动失败（需要管理员权限写入注册表）")

    def on_tray(self, v):
        self.win.settings["minimize_to_tray"] = bool(v)

    def on_opacity(self, v):
        v = int(v)
        self.win.set_opacity(v)


# ==========================================================================
# 主窗口
# ==========================================================================
class MainWindow(FramelessWindow):
    NAV = [("overview", "概览", OverviewPage),
           ("optimize", "系统优化", OptimizePage),
           ("cputune", "CPU 调试", CpuTunePage),
           ("security", "安全检测", SecurityPage),
           ("software", "软件管理", SoftwarePage),
           ("startup", "启动与服务", StartupPage),
           ("performance", "内存与性能", PerfPage),
           ("network", "网络修复", NetworkPage),
           ("speedtest", "内网测速", SpeedTestPage),
           ("dns", "DNS 检测", DNSPage),
           ("cleanup", "垃圾清理", CleanupPage),
           ("policies", "策略诊断", PoliciesPage),
           ("settings", "设置", SettingsPage)]

    def __init__(self, settings=None, admin_mode=False, dark=False, sysinfo=None):
        super().__init__()
        self.admin_mode = admin_mode
        self.dark = dark
        _st = settings if settings is not None else {}
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
        # 背景不透明度（只作用于背景，文字始终实心）——见 set_opacity
        self.opacity = int(max(20, min(100, _st.get("opacity", 100))))
        self.setWindowIcon(self._make_icon())
        self.settings = _st
        self._closing = False
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
            b = QPushButton(name)
            b.setObjectName("NavItem")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
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

        self.sb = QStatusBar()
        self.setStatusBar(self.sb)
        self.group.idClicked.connect(self.goto_index)

        self._admin_label()
        self._apply_qss()
        self.goto("overview")   # 每次打开都从概览页开始，不再恢复上次的页面
        # 预热系统信息（首次要起一次 PowerShell，约 1.8s）：进概览页时立刻有内容
        Tasks.run(self, S.sys_basic, lambda *_: None, on_fail=lambda *_: None)

    def showEvent(self, e):
        super().showEvent(e)
        # 首次显示时确保样式（字号/DPI）已按本窗口的 scale/font_base 应用
        if not getattr(self, "_qss_ready", False):
            self._qss_ready = True
            self._apply_qss()

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
        f = QFont("Microsoft YaHei UI", 34); f.setBold(True)
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
        self.sb.showMessage("Python %s · %s" % (sys.version.split()[0], S.APP_DIR))

    def toggle_theme(self):
        self.dark = not self.dark
        self.btn_theme.setText("🌙" if not self.dark else "☀")
        self.settings["dark"] = self.dark
        self._apply_qss()
        self._refresh_skin()

    def set_accent(self, value):
        """设置界面主题色。value 为 '' / None 时表示跟随系统强调色。"""
        v = T.set_accent(value)
        self.accent = v or ""
        self.settings["accent"] = self.accent
        # _apply_qss 会重画勾选图标（文件名带主题色指纹）并重新 polish 全部控件
        self._apply_qss()
        self._refresh_skin()
        sp = getattr(self, "pages", {}).get("settings")
        if sp is not None and hasattr(sp, "refresh"):
            sp.refresh()

    def _refresh_skin(self):
        """浅/深色 或 主题色变更后，刷新所有「用代码画的」皮肤元素。"""
        self.setWindowIcon(self._make_icon())
        ti = getattr(self, "_title_icon", None)
        if ti is not None:
            ti.setPixmap(self.windowIcon().pixmap(20, 20))
        if getattr(self, "tray", None):
            self.tray.setIcon(self.windowIcon())
        chart = getattr(self, "pages", {}).get("overview")
        if chart is not None:
            chart.chart.update()
            if hasattr(chart, "_update_legend"):
                chart._update_legend()

    def _apply_qss(self):
        self.setStyleSheet(T.build_qss(self.dark, self.scale, self.font_base,
                                       self.opacity))
        # QSS 的 font-size 不会刷新布局缓存，必须重新校正一次标签度量。
        # 布局需要两轮才能稳定（首轮 width 可能为 0），故做二次校正。
        QTimer.singleShot(0, self._refit_fonts)
        QTimer.singleShot(120, self._refit_fonts)

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
            if hasattr(self, "pages"):
                chart = self.pages.get("overview")
                if chart:
                    chart.chart.update()
        # 同步最大化/还原按钮图标与外壳圆角
        self._sync_max_flag()

    def set_opacity(self, v):
        """设置窗口背景不透明度（20~100）。

        关键：**不**用 setWindowOpacity（那会把文字一起淡掉，调低后完全看不清）。
        改为把透明度写进 QSS 的背景色（rgba），文字始终保持实心不透明，
        因此即使调到最低 20% 也能清楚看到所有文字。
        """
        v = int(max(20, min(100, v)))
        self.opacity = v
        self.settings["opacity"] = v
        self._apply_qss()

    def _warn_readonly(self):
        warn(self, "当前为普通模式，写入系统设置的选项已被禁用。\n\n"
                  "如需修改服务 / 注册表 / 策略 / 网络修复，请在启动时选择「管理员模式」并允许 UAC 提权。")

    def goto_index(self, i):
        self.goto(self.NAV[i][0])

    def goto(self, key):
        for i, (k, _, _) in enumerate(self.NAV):
            if k == key:
                self.group.button(i).setChecked(True)
                self.stack.setCurrentIndex(i)
                self.title_lb.setText(self.NAV[i][1])
                break
        page = self.pages.get(key)
        if page:
            def _show():
                page.first_show()
                fit_labels(page)
            QTimer.singleShot(0, _show)

    def on_refresh(self):
        page = self.pages.get(self.NAV[self.stack.currentIndex()][0])
        if page:
            page.refresh()
        self.sb.showMessage("已刷新", 1500)

    def busy(self, msg="处理中…"):
        self.spinner.setText("⏳ " + msg)

    def idle(self):
        self.spinner.setText("")

    # ---------- 退出时自动保存 / 最小化到托盘 ----------
    def closeEvent(self, e):
        if self._closing or not self.settings.get("minimize_to_tray", False):
            try:
                # 停掉可能还在跑的 iperf3 服务端，避免残留进程
                try:
                    S.iperf_server_stop()
                except Exception:
                    pass
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
            return
        # minimize-to-tray mode: hide instead of close
        e.ignore()
        self.hide()
        if getattr(self, "tray", None):
            self.tray.showMessage(
                "WinToolbox", "已最小化到系统托盘，双击图标可恢复。",
                QSystemTrayIcon.Information, 1500)


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
    app.setFont(QFont("Microsoft YaHei UI", 9))

    settings = settings_load()
    dark = bool(settings.get("dark", False))

    # ---- 阶段1：读取系统信息（分辨率 / DPI 缩放 / 系统字号） ----
    sysinfo = probe_system_info(app)
    _op = int(max(20, min(100, settings.get("opacity", 100))))
    app.setStyleSheet(T.build_qss(dark, 1.0, sysinfo["font_base"], _op))

    # ---- 阶段2：模式（已取消选择弹窗：直接以管理员权限运行）----
    # 流程改为「直接跑进度条 → 进软件」，不再先弹模式选择框。
    # 若当前进程没有提权，先重新以管理员启动（UAC 由系统弹出），
    # 提权成功后再正常显示启动进度条。
    mode = choose_mode(sys.argv)
    if mode == "admin" and not S.is_admin():
        # 注意：这里在启动窗口之前，用户会先看到系统 UAC 弹窗，
        # 确认后新进程会重新走一遍本流程并直接进入加载条。
        relaunch_as_admin()
    admin_mode = (mode == "admin")

    # ---- 阶段3：先算出主窗口尺寸，用同尺寸启动窗口展示加载过程 ----
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
                          size=(win_w, win_h), screen_size=screen_size,
                          opacity=_op)
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

    # ---- 阶段4：同尺寸无跳变切换到主窗口 ----
    w.show()
    splash.step(100, "启动完成")
    QApplication.processEvents()
    splash.close()
    QTimer.singleShot(120, lambda: (w.raise_(), w.activateWindow()))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
