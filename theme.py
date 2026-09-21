# -*- coding: utf-8 -*-
"""Windows 11 Fluent light / dark QSS for the Qt build (rounded, frameless).

主题色（accent）机制
--------------------
界面主色不再硬编码，而是由「主题色」派生出一族色值：

    BLUE / BLUE_HOVER / BLUE_ACTIVE / BLUE_SOFT / BLUE_SOFT2 / NOTICE_TEXT

主题色来源三选一：
  · 跟随系统 —— 读 HKCU\\Software\\Microsoft\\Windows\\DWM\\AccentColor（默认）
  · ACCENT_PRESETS 里的常用色
  · 调色盘任选

换色只影响强调色族；性能曲线（CPU / 内存 / GPU / 磁盘 / 网络）仍各用独立色，
保证多条曲线在同一张图上依旧可区分。

圆角
----
统一由下面的 R_* 常量控制（比 Win11 默认更圆润），改这几个数就能整体调圆度。
"""

# ---------------- 圆角档位 ----------------
R_SHELL = 14        # 窗口外框 / 标题栏顶角
R_CARD = 12         # 卡片 / 表格 / 列表 / 标签页面板
R_CTL = 8           # 按钮 / 输入框 / 导航项 / 菜单项
R_NOTICE = 10       # 提示条
R_MENU = 8          # 菜单 / 工具提示
R_LOGO = 10         # 品牌方块
R_CHK = 5           # 勾选框指示器（按 16px 计）

# ---------------- 主题色预设（Win11 个性化常用色系） ----------------
DEFAULT_ACCENT = "#0067C0"          # Windows 蓝，也是「恢复默认」的目标
ACCENT_PRESETS = [
    ("Windows 蓝", "#0067C0"),
    ("天青", "#0099BC"),
    ("青绿", "#00B294"),
    ("翠绿", "#107C10"),
    ("琥珀", "#CA5010"),
    ("绯红", "#C42B1C"),
    ("紫罗兰", "#8764B8"),
    ("石墨", "#4A4A4A"),
]

# ---------------- light palette（Win11 层底：浅灰底 + 白卡片） ----------------
LIGHT = {
    "BG": "#F3F3F3",            # 窗口 / 页面层底（Win11 mica 近似）
    "PANEL": "#FFFFFF",         # 卡片 / 输入框 / 表格
    "PANEL2": "#FAFAFA",        # 次级面（悬停）
    "LINE": "#E5E5E5",
    "LINE2": "#D6D6D6",
    "TRACK": "#ECECEC",         # 进度条轨道（比 LINE 更淡，几乎隐形）
    "BORDER_BOTTOM": "#C8C8C8",  # 控件底边（Win11 按钮的立体感来源）
    "TEXT": "#1A1A1A",
    "TEXT2": "#5C5C5C",
    "TEXT3": "#8A8A8A",
    "OK": "#0F7B0F",
    "OK_BG": "#DFF6DD",
    "WARN": "#9D5D00",
    "WARN_BG": "#FFF4CE",
    "ERR": "#C42B1C",
    "ERR_BG": "#FDE7E9",
    "ACCENT_CPU": "#0078D4",
    "ACCENT_MEM": "#8B5CF6",
    "ACCENT_GPU": "#F2A900",
    "ACCENT_DISK": "#107C10",
    "ACCENT_NET": "#C42B1C",
    "ACCENT_CPU_SOFT": "#E5F1FB",
    "ACCENT_MEM_SOFT": "#EFE9FB",
    "ACCENT_GPU_SOFT": "#FFF4CE",
    "ACCENT_DISK_SOFT": "#DFF6DD",
    "ACCENT_NET_SOFT": "#FDE7E9",
    "ACCENT_GPU_TEXT": "#B07A00",
    "ACCENT_NET_TEXT": "#C42B1C",
    "TEMP": "#00A0A0",          # 温度进度条专用色
}

# ---------------- dark palette（Win11 深色：深灰底 + 略亮卡片） ----------------
DARK = {
    "BG": "#202020",
    "PANEL": "#2B2B2B",
    "PANEL2": "#333333",
    "LINE": "#3A3A3A",
    "LINE2": "#4A4A4A",
    "TRACK": "#3A3A3A",
    "BORDER_BOTTOM": "#161616",
    "TEXT": "#FFFFFF",
    "TEXT2": "#CFCFCF",
    "TEXT3": "#9A9A9A",
    "OK": "#6CCB5F",
    "OK_BG": "#214C1F",
    "WARN": "#FFCD75",
    "WARN_BG": "#5C4813",
    "ERR": "#FF99A4",
    "ERR_BG": "#5C1B1E",
    "ACCENT_CPU": "#4CC2FF",
    "ACCENT_MEM": "#B78DFF",
    "ACCENT_GPU": "#FFD166",
    "ACCENT_DISK": "#6CCB5F",
    "ACCENT_NET": "#FF99A4",
    "ACCENT_CPU_SOFT": "#1E3A5F",
    "ACCENT_MEM_SOFT": "#3A2E5C",
    "ACCENT_GPU_SOFT": "#4A3A14",
    "ACCENT_DISK_SOFT": "#214C1F",
    "ACCENT_NET_SOFT": "#5C1B1E",
    "ACCENT_GPU_TEXT": "#FFD166",
    "ACCENT_NET_TEXT": "#FF99A4",
    "TEMP": "#4CC9C9",
}


# ==========================================================================
# 颜色工具
# ==========================================================================
def hex_to_rgb(h):
    """'#RRGGBB' -> (r, g, b)"""
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return 255, 255, 255


def rgb_to_hex(r, g, b):
    return "#%02X%02X%02X" % (max(0, min(255, round(r))),
                              max(0, min(255, round(g))),
                              max(0, min(255, round(b))))


def mix(c1, c2, t):
    """线性混色：t=0 返回 c1，t=1 返回 c2。"""
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)


def lighten(c, t):
    return mix(c, "#FFFFFF", t)


def darken(c, t):
    return mix(c, "#000000", t)


def luminance(c):
    """相对亮度 0~1，用来决定压在这个颜色上的文字该用黑还是白。"""
    r, g, b = hex_to_rgb(c)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def contrast_text(c, dark_on_light=True):
    """在颜色 c 上放文字时，返回可读的文字色。"""
    return "#0A0A0A" if luminance(c) > 0.62 else "#FFFFFF"


def is_valid_hex(c):
    h = (c or "").lstrip("#")
    if len(h) == 3:
        h = "".join(x * 2 for x in h)
    if len(h) != 6:
        return False
    try:
        int(h, 16)
        return True
    except ValueError:
        return False


def bg_rgba(hex_color, bg_alpha):
    """把背景色转成 rgba()，用于「背景半透明、文字不透明」。"""
    if bg_alpha >= 0.999:
        return hex_color
    r, g, b = hex_to_rgb(hex_color)
    return "rgba(%d, %d, %d, %d)" % (r, g, b, max(0, min(255, round(bg_alpha * 255))))


# ==========================================================================
# 主题色：跟随系统 / 手动指定
# ==========================================================================
_accent_state = {"value": None}      # None 表示「跟随系统」
_sys_cache = {}
_palette_cache = {}                  # (dark, accent) -> 色表，避免每秒重绘时反复构造


def system_accent():
    """读取 Windows 系统强调色（HKCU DWM\\AccentColor，ABGR DWORD）。

    读不到就回退 Windows 蓝。结果缓存，避免每次取色都开注册表。
    """
    if "v" in _sys_cache:
        return _sys_cache["v"]
    val = DEFAULT_ACCENT
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\DWM") as k:
            raw, _t = winreg.QueryValueEx(k, "AccentColor")
        # 0xAABBGGRR
        val = rgb_to_hex(raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF)
    except Exception:
        pass
    _sys_cache["v"] = val
    return val


def set_accent(value):
    """设置主题色。传 None / '' 表示跟随系统。"""
    v = (value or "").strip()
    if not is_valid_hex(v):
        v = None
    else:
        v = rgb_to_hex(*hex_to_rgb(v))
    if v != _accent_state["value"]:
        _accent_state["value"] = v
        _ICON_CACHE.clear()          # 勾选图标依赖主题色，必须重画
    return v


def get_accent():
    """当前生效的主题色（手动指定优先，否则跟随系统）。"""
    return _accent_state["value"] or system_accent()


def is_following_system():
    return _accent_state["value"] is None


def accent_family(accent, dark=False):
    """从主题色派生整套强调色值。

    浅色主题：主色直接用原色；hover 提亮、pressed 压暗；soft 是叠在白底上的浅色。
    深色主题：主色需要提亮才够醒目（Win11 深色下的强调色就是亮版）；soft 是叠在
    深色面板上的暗色低饱和块。
    """
    a = accent if is_valid_hex(accent) else DEFAULT_ACCENT
    if dark:
        panel = DARK["PANEL"]
        bg = DARK["BG"]
        return {
            "BLUE": lighten(a, 0.45),
            "BLUE_HOVER": lighten(a, 0.58),
            "BLUE_ACTIVE": lighten(a, 0.32),
            "BLUE_SOFT": mix(a, panel, 0.80),
            "BLUE_SOFT2": mix(a, bg, 0.88),
            "NOTICE_TEXT": lighten(a, 0.62),
            "ON_ACCENT": contrast_text(lighten(a, 0.45)),
        }
    return {
        "BLUE": a,
        "BLUE_HOVER": lighten(a, 0.12),
        "BLUE_ACTIVE": darken(a, 0.16),
        "BLUE_SOFT": lighten(a, 0.84),
        "BLUE_SOFT2": lighten(a, 0.92),
        "NOTICE_TEXT": darken(a, 0.30),
        "ON_ACCENT": contrast_text(a),
    }


def palette(dark=False, accent=None):
    """完整色表 = 基础主题色 + 由 accent 派生的强调色族。"""
    out = dict(DARK if dark else LIGHT)
    out.update(accent_family(accent if accent is not None else get_accent(), dark))
    return out


def get_color(name, dark=False):
    """取单个色值（供自绘控件 / 动态字符串用）。"""
    return palette(dark).get(name, "#000000")


# ==========================================================================
# 透明度：滑块 20~100 → 背景 alpha
# ==========================================================================
# 下限 20% 若直接映射成 0.20，浅色底会过淡、深字对比不足；
# 故把 20% 映射到 0.55，保证最低档也能清楚阅读（文字本身始终实心）。
_ALPHA_MIN = 0.55


def _map_alpha(op):
    op = max(20, min(100, int(op)))
    if op >= 100:
        return 1.0
    return _ALPHA_MIN + (1.0 - _ALPHA_MIN) * (op - 20) / 80.0


# ==========================================================================
# 勾选框指示器图标（程序化生成 PNG，QSS 用 url() 引用）
# ==========================================================================
# 为什么不用系统默认：全局 QSS 一旦接管 QCheckBox/QTableView，原生指示器就不再
# 画方框，只剩一个"✓"字形。这里自己画：空框 / 悬停框 / 选中（主题色底 + 对勾）。
_ICON_CACHE = {}


def _draw_indicator(path, size, border, fill=None, check=None):
    """画一枚勾选框指示器：border=边框色；fill=填充色(None 则空心)；check=勾色。"""
    from PySide6.QtCore import Qt, QRectF, QPointF
    from PySide6.QtGui import QImage, QPainter, QPen, QColor

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    r = QRectF(size * 0.09, size * 0.09, size * 0.82, size * 0.82)
    radius = size * (R_CHK / 16.0)           # 更圆润的指示器
    if fill:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(fill))
        p.drawRoundedRect(r, radius, radius)
    else:
        pen = QPen(QColor(border))
        pen.setWidthF(max(1.2, size * 0.095))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, radius, radius)
    if check:
        pen = QPen(QColor(check))
        pen.setWidthF(max(1.6, size * 0.145))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(size * 0.28, size * 0.53),
                        QPointF(size * 0.44, size * 0.70),
                        QPointF(size * 0.74, size * 0.32)])
    p.end()
    img.save(path, "PNG")
    return path


def indicator_icons(dark=False, scale=1.0, accent=None):
    """返回 {'off','off_hover','on'} 三个 PNG 的绝对路径（按主题色+缩放缓存）。"""
    import os
    import tempfile
    size = max(14, int(round(16 * max(1.0, scale))))
    acc = accent if is_valid_hex(accent) else get_accent()
    key = (bool(dark), size, acc)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    P = palette(dark, acc)
    d = os.path.join(tempfile.gettempdir(), "wintoolbox_ui")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    # 文件名带主题色指纹：换色后 url 变化，Qt 才会重新加载图片
    tag = "%s_%s_%d" % ("d" if dark else "l", acc.lstrip("#").lower(), size)
    off_border = "#6E6E6E" if dark else "#8A8A8A"
    on_fill = P["BLUE"]
    on_check = P["ON_ACCENT"]
    ic = {
        "off": _draw_indicator(os.path.join(d, "chk_%s_off.png" % tag),
                               size, off_border),
        "off_hover": _draw_indicator(os.path.join(d, "chk_%s_hover.png" % tag),
                                     size, P["BLUE"]),
        "on": _draw_indicator(os.path.join(d, "chk_%s_on.png" % tag),
                              size, on_fill, fill=on_fill, check=on_check),
    }
    _ICON_CACHE[key] = ic
    return ic


# ==========================================================================
# QSS 主体
# ==========================================================================
def build_qss(dark=False, scale=1.0, font_base=13, opacity=100, accent=None):
    """Return the full QSS string.

    `scale`     — UI 尺寸倍数（= 系统 DPI 缩放，仅在「窗口化」时人为收一点）。
    `font_base` — 基准字号（px），默认 13px，等价于 Windows 100% 缩放下 9pt。
    `opacity`   — 窗口背景不透明度（20~100）。**只作用于背景**，文字始终保持实心。
    `accent`    — 主题色；不传则用当前全局主题色（手动指定 / 跟随系统）。
    """
    P = palette(dark, accent if accent is not None else get_accent())
    if not font_base or font_base <= 0:
        font_base = 13
    try:
        op = int(opacity)
    except Exception:
        op = 100
    alpha = _map_alpha(op)

    def s(v):
        try:
            return "%dpx" % round(float(v) * scale)
        except Exception:
            return str(v)

    def f(v):
        try:
            return "%dpx" % max(9, round(float(v) / 13.0 * font_base))
        except Exception:
            return str(v)

    def bg(name):
        """背景类颜色：随透明度变化。"""
        return bg_rgba(P[name], alpha)

    def bgl(name):
        """线条/分隔类颜色：同样随透明度淡化，但保留最低可见度。"""
        return bg_rgba(P[name], max(0.35, alpha))

    # 勾选框指示器：生成 PNG 并拼出 QSS 片段（失败则留空，退回系统默认样式）
    _chk_qss = ""
    try:
        _ic = indicator_icons(dark, scale)
        _u = lambda p: p.replace("\\", "/")
        _chk_qss = (
            "QCheckBox::indicator {{ width: {w}; height: {w}; image: url({off}); }}\n"
            "QCheckBox::indicator:hover {{ image: url({hov}); }}\n"
            "QCheckBox::indicator:checked, QCheckBox::indicator:checked:hover "
            "{{ image: url({on}); }}\n"
            "QTableView::indicator, QTreeView::indicator, QListView::indicator "
            "{{ width: {w}; height: {w}; image: url({off}); }}\n"
            "QTableView::indicator:hover, QTreeView::indicator:hover, "
            "QListView::indicator:hover {{ image: url({hov}); }}\n"
            "QTableView::indicator:checked, QTreeView::indicator:checked, "
            "QListView::indicator:checked {{ image: url({on}); }}\n"
        ).format(w=s(16), off=_u(_ic["off"]), hov=_u(_ic["off_hover"]), on=_u(_ic["on"]))
    except Exception:
        _chk_qss = ""

    return f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: {f(13)};
    color: {P["TEXT"]};
}}
QWidget#Root       {{ background: transparent; }}

/* ---------- 层底：Win11 是「浅灰底 + 白卡片」两层结构 ----------
   BG 刷在窗口 / 页面 / 滚动区底层，PANEL 刷在卡片与控件上。
   注意：QScrollArea 的 viewport、QStackedWidget、裸 QWidget 都必须显式指定背景，
   否则会露出 Qt 默认 palette 的浅灰。 */
QMainWindow, QDialog, QScrollArea, QStackedWidget, QSplitter {{
    background: {bg("BG")};
}}
QScrollArea > QWidget > QWidget {{ background: {bg("BG")}; }}
QScrollArea QWidget#qt_scrollarea_viewport {{ background: {bg("BG")}; }}
QScrollArea {{ border: none; }}
QStackedWidget > QWidget {{ background: {bg("BG")}; }}
QWidget#Page {{ background: transparent; }}
QScrollArea#PageScroll {{ background: transparent; border: none; }}
QWidget#PageViewport {{ background: transparent; }}
QWidget#ContentArea {{ background: transparent; }}   /* 由 AtmosphereArea.paintEvent 画 Mica 氛围层 */

/* ---------- frameless shell ---------- */
QFrame#Shell {{
    background: {bg("BG")};
    border-radius: {s(R_SHELL)};
    border: {s(1)} solid {bgl("LINE")};
}}
QFrame#Shell[maximized="true"] {{ border-radius: 0px; border: {s(1)} solid {bgl("LINE")}; }}

QWidget#TitleBar {{
    background: {bg("PANEL")};
    border-top-left-radius: {s(R_SHELL)}; border-top-right-radius: {s(R_SHELL)};
    border-bottom: {s(1)} solid {bgl("LINE")};
}}
QWidget#TitleBar[maximized="true"] {{
    border-top-left-radius: 0px; border-top-right-radius: 0px;
}}
QLabel#TitleText {{ font-size: {f(12.5)}; color: {P["TEXT2"]}; background: transparent; }}

/* 尺寸由代码 setFixedSize 控制，QSS 只负责外观，避免 min-width 被拉伸 */
QPushButton#TitleBtn {{
    background: transparent; border: none; border-radius: {s(R_CTL)};
    font-size: {f(11)}; color: {P["TEXT"]};
}}
QPushButton#TitleBtn:hover  {{ background: {bg("LINE")}; }}
QPushButton#TitleBtn:pressed {{ background: {bg("LINE2")}; }}
QPushButton#TitleBtnClose {{
    background: transparent; border: none; border-radius: {s(R_CTL)};
    font-size: {f(11)}; color: {P["TEXT"]};
}}
QPushButton#TitleBtnClose:hover {{ background: {P["ERR"]}; color: #FFFFFF; }}
QPushButton#TitleBtnClose:pressed {{ background: #A8261A; color: #FFFFFF; }}

/* ---------- layout ---------- */
QFrame#Nav         {{ background: {bg("PANEL")}; border-top-left-radius: {s(R_SHELL)}; border-bottom-left-radius: {s(R_SHELL)}; border-right: {s(1)} solid {bgl("LINE")}; }}
QFrame#Nav[maximized="true"] {{ border-top-left-radius: 0px; }}
QFrame#TopBar      {{ background: {bg("PANEL")}; border-bottom: {s(1)} solid {bgl("LINE")}; }}

QLabel#Brand       {{ font-size: {f(15)}; font-weight: 600; background: transparent; }}
QLabel#BrandSub    {{ font-size: {f(11)}; color: {P["TEXT3"]}; background: transparent; }}
QLabel#Logo        {{
    background: {P["BLUE"]}; color: {P["ON_ACCENT"]}; font-size: {f(15)}; font-weight: 700;
    border-radius: {s(R_LOGO)}; min-width: {s(30)}; max-width: {s(30)};
    min-height: {s(30)}; max-height: {s(30)}; qproperty-alignment: AlignCenter;
}}
QLabel#PageTitle   {{ font-size: {f(16)}; font-weight: 600; background: transparent; }}
QLabel#CardTitle   {{ font-size: {f(14)}; font-weight: 600; background: transparent; }}
QLabel#Muted       {{ color: {P["TEXT2"]}; background: transparent; }}
QLabel#Muted3      {{ color: {P["TEXT3"]}; font-size: {f(11.5)}; background: transparent; }}
QLabel#StatK       {{ color: {P["TEXT2"]}; font-size: {f(12)}; background: transparent; }}
QLabel#StatV       {{ font-size: {f(20)}; font-weight: 600; background: transparent; }}
QLabel#StatVSmall  {{ font-size: {f(14)}; font-weight: 600; background: transparent; }}
QLabel#Mono        {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: {f(11.5)}; color: {P["TEXT2"]}; background: transparent; }}

/* ---------- 仪表盘：主指标（大数字是主角，标签退为配角） ----------
   参考 Win11 任务管理器「性能」页：等宽数字 + 小写单位 + 极小的全大写标签。 */
QLabel#MetricK {{
    font-size: {f(10.5)}; font-weight: 700; color: {P["TEXT3"]};
    background: transparent;
}}
QLabel#MetricV {{
    font-family: "Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace;
    font-size: {f(30)}; font-weight: 600; color: {P["TEXT"]};
    background: transparent;
}}
QLabel#MetricU {{
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: {f(12)}; color: {P["TEXT2"]}; background: transparent;
}}
QLabel#MetricSub {{ font-size: {f(11.5)}; color: {P["TEXT3"]}; background: transparent; }}

/* 键值行：左侧灰标签，右侧实值，中间留白 —— 设置页「关于」的写法 */
QLabel#KVKey  {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; background: transparent; }}
QLabel#KVVal  {{ font-size: {f(13)}; font-weight: 600; background: transparent; }}
QLabel#KVSub  {{ color: {P["TEXT3"]}; font-size: {f(11)}; background: transparent; }}

/* 发丝分隔线：比普通边框更淡，只做结构提示不做装饰 */
QFrame#VSep {{ background: {bgl("LINE")}; max-width: {s(1)}; border: none; }}
QFrame#HSep {{ background: {bgl("LINE")}; max-height: {s(1)}; border: none; }}

/* ---------- category cards (optimize level 0) ---------- */
QFrame#CatCard {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")};
    border-radius: {s(R_CTL)}; padding: 0;
}}
QFrame#CatCard:hover {{ border-color: {P["BLUE"]}; background: {bg("BLUE_SOFT2")}; }}
QLabel#CatIcon {{ font-size: {f(15)}; background: transparent; }}
QLabel#CatName  {{ font-size: {f(13.5)}; font-weight: 600; background: transparent; }}
QLabel#CatSub   {{ font-size: {f(11.5)}; color: {P["TEXT3"]}; background: transparent; }}
QLabel#CatArrow {{ font-size: {f(16)}; color: {P["TEXT3"]}; background: transparent; }}

/* nav buttons：选中态用主题色（Win11 导航惯例） */
QPushButton#NavItem {{
    text-align: left; padding: {s(9)} {s(14)}; border: none; border-radius: {s(R_CTL)};
    background: transparent; color: {P["TEXT"]}; font-size: {f(13.5)};
}}
QPushButton#NavItem:hover   {{ background: {bg("LINE")}; }}
QPushButton#NavItem:checked {{
    background: {bg("BLUE_SOFT")}; color: {P["BLUE"]}; font-weight: 600;
}}

/* generic buttons（Win11：浅底 + 细边框 + 略深的底边） */
QPushButton {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")};
    border-bottom-color: {bgl("BORDER_BOTTOM")};
    border-radius: {s(R_CTL)}; padding: {s(6)} {s(14)}; color: {P["TEXT"]};
}}
QPushButton:hover    {{ background: {bg("PANEL2")}; }}
QPushButton:pressed  {{ background: {bg("LINE")}; border-bottom-color: {bgl("LINE")}; }}
QPushButton:disabled {{ color: {P["TEXT3"]}; border-color: {bgl("LINE")}; background: {bg("BG")}; }}
QPushButton:focus {{ border-color: {P["BLUE"]}; }}
QPushButton#Primary {{
    background: {P["BLUE"]}; border: {s(1)} solid {P["BLUE"]};
    border-bottom-color: {darken(P["BLUE"], 0.18)};
    color: {P["ON_ACCENT"]}; font-weight: 600;
}}
QPushButton#Primary:hover   {{ background: {P["BLUE_HOVER"]}; border-color: {P["BLUE_HOVER"]}; }}
QPushButton#Primary:pressed {{ background: {P["BLUE_ACTIVE"]}; border-color: {P["BLUE_ACTIVE"]}; }}
QPushButton#Primary:disabled{{ background: {bg("LINE")}; border-color: {bgl("LINE")}; color: {P["TEXT3"]}; }}
QPushButton#Danger {{
    background: {bg("ERR_BG")}; border: {s(1)} solid {P["ERR"]};
    border-bottom-color: {darken(P["ERR"], 0.18)};
    color: {P["ERR"]}; font-weight: 600;
}}
QPushButton#Danger:hover   {{ background: {bg("ERR_BG")}; border-color: {P["ERR"]}; color: {P["ERR"]}; }}
QPushButton#Danger:pressed {{ background: {P["ERR"]}; border-color: {P["ERR"]}; color: #FFFFFF; }}

/* 分段切换按钮（软件管理 / 分类筛选） */
QPushButton#SegBtn {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")};
    border-bottom-color: {bgl("BORDER_BOTTOM")};
    border-radius: {s(R_CTL)}; padding: {s(4)} {s(14)}; color: {P["TEXT2"]};
}}
QPushButton#SegBtn:hover {{ background: {bg("PANEL2")}; color: {P["TEXT"]}; }}
QPushButton#SegBtn:checked {{
    background: {P["BLUE"]}; border-color: {P["BLUE"]};
    border-bottom-color: {darken(P["BLUE"], 0.18)};
    color: {P["ON_ACCENT"]}; font-weight: 600;
}}

/* cards */
QFrame#Card {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(R_CARD)};
}}
QFrame#StatCard {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(R_CARD)};
}}

/* inputs */
QLineEdit, QComboBox, QSpinBox {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")};
    border-bottom-color: {bgl("BORDER_BOTTOM")};
    border-radius: {s(R_CTL)}; padding: {s(5)} {s(9)}; min-height: {s(18)};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ background: {bg("PANEL2")}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: {s(1)} solid {P["BLUE"]}; background: {bg("PANEL")}; }}
QComboBox::drop-down {{ border: none; width: {s(20)}; }}
/* QSpinBox 被 QSS 样式化后默认上下按钮会失效（上键点不动），
   必须显式定义 up/down-button 及箭头，热区才可靠 */
QSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: {s(20)}; border: none; background: transparent;
    border-top-right-radius: {s(R_CTL)};
}}
QSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: {s(20)}; border: none; background: transparent;
    border-bottom-right-radius: {s(R_CTL)};
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {{
    background: {bg("BLUE_SOFT")};
}}
QSpinBox::up-arrow {{
    width: 0; height: 0;
    border-left: {s(4)} solid transparent;
    border-right: {s(4)} solid transparent;
    border-bottom: {s(5)} solid {P["TEXT"]};
}}
QSpinBox::up-arrow:hover, QSpinBox::up-arrow:pressed {{ border-bottom-color: {P["BLUE"]}; }}
QSpinBox::down-arrow {{
    width: 0; height: 0;
    border-left: {s(4)} solid transparent;
    border-right: {s(4)} solid transparent;
    border-top: {s(5)} solid {P["TEXT"]};
}}
QSpinBox::down-arrow:hover, QSpinBox::down-arrow:pressed {{ border-top-color: {P["BLUE"]}; }}
QComboBox QAbstractItemView {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")};
    border-radius: {s(R_MENU)};
    selection-background-color: {bg("BLUE_SOFT")};
    selection-color: {P["BLUE"]}; outline: none;
}}
QCheckBox {{ spacing: {s(7)}; background: transparent; }}
{_chk_qss}
/* 兜底：图标加载失败时仍能看到方框 */
QCheckBox::indicator {{
    width: {s(16)}; height: {s(16)}; border: {s(1)} solid {bgl("LINE2")};
    border-radius: {s(R_CHK)}; background: {bg("PANEL")};
}}
QCheckBox::indicator:hover   {{ border-color: {P["BLUE"]}; }}

/* sliders */
QSlider::groove:horizontal {{
    background: {bg("LINE2")}; height: {s(4)}; border-radius: {s(2)};
}}
QSlider::sub-page:horizontal {{
    background: {P["BLUE"]}; height: {s(4)}; border-radius: {s(2)};
}}
QSlider::handle:horizontal {{
    background: {bg("PANEL")}; border: {s(2)} solid {P["BLUE"]};
    width: {s(14)}; height: {s(14)}; margin: {s(-6)} 0; border-radius: {s(9)};
}}
QSlider::handle:horizontal:hover {{ background: {bg("BLUE_SOFT")}; }}

/* tables */
QTableWidget, QTreeWidget {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(R_CARD)};
    gridline-color: {bgl("LINE")}; outline: none;
    selection-background-color: {bg("BLUE_SOFT")}; selection-color: {P["TEXT"]};
}}
QTableWidget::item, QTreeWidget::item {{ padding: {s(6)} {s(5)}; border-bottom: {s(1)} solid {bgl("LINE")}; }}
QHeaderView::section {{
    background: {bg("PANEL")}; border: none; border-bottom: {s(1)} solid {bgl("LINE")};
    padding: {s(7)} {s(6)}; font-weight: 600; color: {P["TEXT2"]};
}}
QHeaderView::section:hover {{ color: {P["BLUE"]}; }}
QTableCornerButton::section {{ background: {bg("PANEL")}; border: none; }}

/* scrollbars */
QScrollBar:vertical {{ background: transparent; width: {s(10)}; margin: 0; }}
QScrollBar::handle:vertical {{ background: {bgl("LINE2")}; border-radius: {s(5)}; min-height: {s(30)}; }}
QScrollBar::handle:vertical:hover {{ background: {bgl("TEXT3")}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: {s(10)}; }}
QScrollBar::handle:horizontal {{ background: {bgl("LINE2")}; border-radius: {s(5)}; min-width: {s(30)}; }}

/* progress bars（细胶囊：轨道几乎隐形，只让填充说话） */
QProgressBar {{
    background: {bg("TRACK")}; border: none; border-radius: {s(4)};
    min-height: {s(7)}; max-height: {s(7)}; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {P["BLUE"]}; border-radius: {s(4)}; }}
QProgressBar#Warn::chunk {{ background: {P["WARN"]}; }}
QProgressBar#Err::chunk  {{ background: {P["ERR"]}; }}
QProgressBar#Cpu::chunk {{ background: {P["ACCENT_CPU"]}; }}
QProgressBar#Mem::chunk {{ background: {P["ACCENT_MEM"]}; }}
QProgressBar#Gpu::chunk {{ background: {P["ACCENT_GPU"]}; }}
QProgressBar#Disk::chunk {{ background: {P["ACCENT_DISK"]}; }}
QProgressBar#Net::chunk {{ background: {P["ACCENT_NET"]}; }}
QProgressBar#Temp::chunk {{ background: {P["TEMP"]}; }}
QProgressBar#TempErr::chunk {{ background: {P["ERR"]}; }}

/* pills（胶囊） */
QLabel#Pill      {{ background: {bg("LINE")}; color: {P["TEXT2"]}; border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillOk    {{ background: {bg("OK_BG")};   color: {P["OK"]};   border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillWarn  {{ background: {bg("WARN_BG")}; color: {P["WARN"]}; border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillErr   {{ background: {bg("ERR_BG")};  color: {P["ERR"]};  border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillBlue  {{ background: {bg("BLUE_SOFT")}; color: {P["BLUE"]}; border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillCpu   {{ background: {bg("ACCENT_CPU_SOFT")};  color: {P["ACCENT_CPU"]};  border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillMem   {{ background: {bg("ACCENT_MEM_SOFT")};  color: {P["ACCENT_MEM"]};  border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillGpu   {{ background: {bg("ACCENT_GPU_SOFT")};  color: {P["ACCENT_GPU_TEXT"]};  border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillDisk  {{ background: {bg("ACCENT_DISK_SOFT")}; color: {P["ACCENT_DISK"]}; border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillNet   {{ background: {bg("ACCENT_NET_SOFT")};  color: {P["ACCENT_NET_TEXT"]};  border-radius: {s(12)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}

/* 主题色预览块（设置页小色块） */
QLabel#AccentChip {{
    border-radius: {s(R_MENU)}; border: {s(1)} solid {bgl("LINE2")};
    min-width: {s(46)}; max-width: {s(46)}; min-height: {s(24)}; max-height: {s(24)};
}}

/* notices：颜色全部取自调色板 */
QLabel#Notice {{
    border-radius: {s(R_NOTICE)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("BLUE_SOFT2")}; border: {s(1)} solid {bgl("BLUE")}; color: {P["NOTICE_TEXT"]};
}}
QLabel#NoticeWarn {{ border-radius: {s(R_NOTICE)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("WARN_BG")}; border: {s(1)} solid {bgl("WARN")}; color: {P["WARN"]}; }}
QLabel#NoticeErr  {{ border-radius: {s(R_NOTICE)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("ERR_BG")}; border: {s(1)} solid {bgl("ERR")}; color: {P["ERR"]}; }}
QLabel#NoticeOk   {{ border-radius: {s(R_NOTICE)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("OK_BG")}; border: {s(1)} solid {bgl("OK")}; color: {P["OK"]}; }}

QStatusBar {{ background: {bg("PANEL")}; border-bottom-left-radius: {s(R_SHELL)}; border-bottom-right-radius: {s(R_SHELL)}; border-top: {s(1)} solid {bgl("LINE")}; color: {P["TEXT2"]}; }}
QStatusBar[maximized="true"] {{ border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; }}
QTabWidget::pane {{ border: {s(1)} solid {bgl("LINE")}; border-radius: {s(R_CARD)}; background: {bg("PANEL")}; top: {s(-1)}; }}
QTabBar::tab {{
    background: transparent; padding: {s(7)} {s(16)}; border: {s(1)} solid transparent;
    border-top-left-radius: {s(R_CTL)}; border-top-right-radius: {s(R_CTL)}; color: {P["TEXT2"]};
}}
QTabBar::tab:selected {{ background: {bg("PANEL")}; border-color: {bgl("LINE")}; border-bottom-color: {bg("PANEL")}; color: {P["BLUE"]}; font-weight: 600; }}
QListWidget {{ background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(R_CARD)}; outline: none; }}
QListWidget::item {{ padding: {s(7)} {s(6)}; border-radius: {s(R_CTL)}; }}
QListWidget::item:selected {{ background: {bg("BLUE_SOFT")}; color: {P["TEXT"]}; }}
QToolTip {{ background: {bg("PANEL")}; color: {P["TEXT"]}; border: {s(1)} solid {bgl("LINE2")}; border-radius: {s(R_MENU)}; padding: {s(5)} {s(8)}; }}

QMenu {{ background: {bg("PANEL")}; color: {P["TEXT"]}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(R_MENU)}; padding: {s(5)}; }}
QMenu::item {{ padding: {s(6)} {s(22)} {s(6)} {s(14)}; border-radius: {s(R_CTL)}; }}
QMenu::item:selected {{ background: {bg("BLUE_SOFT")}; color: {P["BLUE"]}; }}
QMenu::separator {{ height: {s(1)}; background: {bgl("LINE")}; margin: {s(4)} {s(8)}; }}

QMessageBox {{ background: {bg("PANEL")}; border-radius: {s(R_CARD)}; }}
QMessageBox QLabel {{ color: {P["TEXT"]}; }}

/* dialogs (startup mode chooser etc.) must follow the active theme */
QDialog {{ background: {bg("BG")}; }}
QDialog QLabel {{ color: {P["TEXT"]}; background: transparent; }}
QDialog QLabel#DlgTitle {{ font-size: {f(15)}; font-weight: 600; }}
QDialog QLabel#DlgDesc  {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; }}

/* 安全确认对话框（屏幕正中） */
QDialog#SafetyDialog {{ background: {bg("PANEL")}; border-radius: {s(R_CARD)}; }}
QLabel#SdIcon {{ font-size: {f(26)}; color: {P["WARN"]}; background: transparent; }}
QLabel#SdTitle {{ font-size: {f(15.5)}; font-weight: 600; }}
QLabel#SdDesc {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; }}
QLabel#SDLabel {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; font-weight: 600; }}
QLabel#SdWarn {{
    background: {bg("WARN_BG")}; color: {P["WARN"]};
    border: {s(1)} solid {bgl("WARN")}; border-radius: {s(R_NOTICE)};
    padding: {s(9)} {s(11)}; font-size: {f(12.5)};
}}
QListWidget#SdList {{
    background: {bg("BG")}; border: {s(1)} solid {bgl("LINE")};
    border-radius: {s(R_NOTICE)}; outline: none; font-size: {f(12.5)};
}}
QListWidget#SdList::item {{ padding: {s(6)} {s(8)}; color: {P["TEXT"]}; }}
"""
