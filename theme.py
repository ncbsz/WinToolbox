# -*- coding: utf-8 -*-
"""Windows 11 Fluent light / dark QSS for the Qt build (rounded, frameless)."""

# ---------------- light palette (纯净：全白底 / 深色文字) ----------------
LIGHT = {
    "BLUE": "#0067C0",
    "BLUE_HOVER": "#1976D2",
    "BLUE_ACTIVE": "#005A9E",
    "BLUE_SOFT": "#E5F1FB",
    "BLUE_SOFT2": "#F3F9FE",
    "BG": "#FFFFFF",
    "PANEL": "#FFFFFF",
    "LINE": "#E5E5E5",
    "LINE2": "#D6D6D6",
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
    "NOTICE_TEXT": "#134B78",
}

# ---------------- dark palette (纯净：全黑底 / 浅色文字) ----------------
DARK = {
    "BLUE": "#4CC2FF",
    "BLUE_HOVER": "#5ACBFF",
    "BLUE_ACTIVE": "#3BB4E8",
    "BLUE_SOFT": "#2C3E5C",
    "BLUE_SOFT2": "#22354A",
    "BG": "#000000",
    "PANEL": "#000000",
    "LINE": "#333333",
    "LINE2": "#484848",
    "TEXT": "#FFFFFF",
    "TEXT2": "#CCCCCC",
    "TEXT3": "#A0A0A0",
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
    "NOTICE_TEXT": "#9CD3FF",
}


def get_color(name, dark=False):
    return (DARK if dark else LIGHT).get(name, "#000000")


def hex_to_rgb(h):
    """'#RRGGBB' -> (r, g, b)"""
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return 255, 255, 255


def bg_rgba(hex_color, bg_alpha):
    """把背景色转成 rgba()，用于「背景半透明、文字不透明」。

    bg_alpha: 0.0~1.0。100% 时直接返回 hex（避免无谓的 rgba）。
    """
    if bg_alpha >= 0.999:
        return hex_color
    r, g, b = hex_to_rgb(hex_color)
    return "rgba(%d, %d, %d, %d)" % (r, g, b, max(0, min(255, round(bg_alpha * 255))))


# 透明度映射：滑块 20~100 → 实际背景 alpha。
# 下限 20% 若直接映射成 0.20，浅色白底会过淡、深字对比不足；
# 故把 20% 映射到 0.55，保证最低档也能清楚阅读（文字本身始终实心）。
_ALPHA_MIN = 0.55


def _map_alpha(op):
    """滑块值(20~100) → 背景 alpha(0.55~1.0)。"""
    op = max(20, min(100, int(op)))
    if op >= 100:
        return 1.0
    return _ALPHA_MIN + (1.0 - _ALPHA_MIN) * (op - 20) / 80.0


# ---------------- 勾选框指示器图标（程序化生成 PNG，QSS 用 url() 引用） ----------------
# 为什么不用系统默认：全局 QSS 一旦接管 QCheckBox/QTableView，原生指示器就不再
# 画方框，只剩一个"✓"字形。这里自己画：空框 / 悬停框 / 选中（蓝底白勾）。
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
    radius = size * 0.26
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


def indicator_icons(dark=False, scale=1.0):
    """返回 {'off','off_hover','on'} 三个 PNG 的绝对路径（带缓存，按主题+缩放生成）。"""
    import os
    import tempfile
    size = max(14, int(round(16 * max(1.0, scale))))
    key = (bool(dark), size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    P = DARK if dark else LIGHT
    d = os.path.join(tempfile.gettempdir(), "wintoolbox_ui")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    tag = "d" if dark else "l"
    on_fill, on_check = P["BLUE"], ("#0B1A26" if dark else "#FFFFFF")
    off_border = "#6E6E6E" if dark else "#8A8A8A"
    ic = {
        "off": _draw_indicator(os.path.join(d, "chk_%s_%d_off.png" % (tag, size)),
                               size, off_border),
        "off_hover": _draw_indicator(os.path.join(d, "chk_%s_%d_hover.png" % (tag, size)),
                                     size, P["BLUE"]),
        "on": _draw_indicator(os.path.join(d, "chk_%s_%d_on.png" % (tag, size)),
                              size, on_fill, fill=on_fill, check=on_check),
    }
    _ICON_CACHE[key] = ic
    return ic


def build_qss(dark=False, scale=1.0, font_base=13, opacity=100):
    """Return the full QSS string.

    `scale`     — UI 尺寸倍数（= 系统 DPI 缩放，仅在「窗口化」时人为收一点）。
    `font_base` — 基准字号（px），默认 13px，等价于 Windows 100% 缩放下 9pt。
    `opacity`   — 窗口背景不透明度（20~100）。**只作用于背景**，文字始终保持实心，
                  因此即使调到最低也能看清内容（避免整体 setWindowOpacity 把字也淡掉）。
    """
    P = DARK if dark else LIGHT
    if not font_base or font_base <= 0:
        font_base = 13
    try:
        op = int(opacity)
    except Exception:
        op = 100
    alpha = _map_alpha(op)

    def s(v):
        # 尺寸：按 DPI 倍数缩放
        try:
            return "%dpx" % round(float(v) * scale)
        except Exception:
            return str(v)

    def f(v):
        # 字号：一律以 font_base 为基准等比缩放，保证任意缩放下字号一致且完整显示
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
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    font-size: {f(13)};
    color: {P["TEXT"]};
}}
QWidget#Root       {{ background: transparent; }}

/* ---------- 纯色底：所有内容容器必须显式指定背景 ----------
   Qt 默认 palette 的窗口色是浅灰(#F0F0F0)，页面被 QScrollArea 包住后
   viewport/QStackedWidget/裸 QWidget 若不指定背景就会露出这层灰。
   这里统一按主题底色刷成纯白/纯黑，保证「要么全白、要么全黑」。 */
QMainWindow, QDialog, QScrollArea, QStackedWidget, QSplitter {{
    background: {bg("BG")};
}}
QScrollArea > QWidget > QWidget {{ background: {bg("BG")}; }}
QScrollArea QWidget#qt_scrollarea_viewport {{ background: {bg("BG")}; }}
QScrollArea {{ border: none; }}
QStackedWidget > QWidget {{ background: {bg("BG")}; }}
QWidget#Page {{ background: {bg("BG")}; }}
QScrollArea#PageScroll {{ background: {bg("BG")}; }}
QWidget#PageViewport {{ background: {bg("BG")}; }}
QWidget#ContentArea {{ background: {bg("BG")}; }}

/* ---------- frameless shell ---------- */
QFrame#Shell {{
    background: {bg("BG")};
    border-radius: {s(12)};
    border: {s(1)} solid {bgl("LINE")};
}}
QFrame#Shell[maximized="true"] {{ border-radius: 0px; border: {s(1)} solid {bgl("LINE")}; }}

QWidget#TitleBar {{
    background: {bg("PANEL")};
    border-top-left-radius: {s(12)}; border-top-right-radius: {s(12)};
    border-bottom: {s(1)} solid {bgl("LINE")};
}}
QWidget#TitleBar[maximized="true"] {{
    border-top-left-radius: 0px; border-top-right-radius: 0px;
}}
QLabel#TitleText {{ font-size: {f(12.5)}; color: {P["TEXT2"]}; background: transparent; }}

/* 尺寸由代码 setFixedSize 控制，QSS 只负责外观，避免 min-width 被拉伸 */
QPushButton#TitleBtn {{
    background: transparent; border: none; border-radius: {s(6)};
    font-size: {f(11)}; color: {P["TEXT"]};
}}
QPushButton#TitleBtn:hover  {{ background: {bg("LINE")}; }}
QPushButton#TitleBtn:pressed {{ background: {bg("LINE2")}; }}
QPushButton#TitleBtnClose {{
    background: transparent; border: none; border-radius: {s(6)};
    font-size: {f(11)}; color: {P["TEXT"]};
}}
QPushButton#TitleBtnClose:hover {{ background: {P["ERR"]}; color: #FFFFFF; }}
QPushButton#TitleBtnClose:pressed {{ background: #A8261A; color: #FFFFFF; }}

/* ---------- layout ---------- */
QFrame#Nav         {{ background: {bg("PANEL")}; border-top-left-radius: {s(12)}; border-bottom-left-radius: {s(12)}; border-right: {s(1)} solid {bgl("LINE")}; }}
QFrame#Nav[maximized="true"] {{ border-top-left-radius: 0px; }}
QFrame#TopBar      {{ background: {bg("PANEL")}; border-bottom: {s(1)} solid {bgl("LINE")}; }}

QLabel#Brand       {{ font-size: {f(15)}; font-weight: 600; background: transparent; }}
QLabel#BrandSub    {{ font-size: {f(11)}; color: {P["TEXT3"]}; background: transparent; }}
QLabel#Logo        {{
    background: {P["BLUE"]}; color: #fff; font-size: {f(15)}; font-weight: 700;
    border-radius: {s(8)}; min-width: {s(30)}; max-width: {s(30)};
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

/* ---------- category cards (optimize level 0) ---------- */
QFrame#CatCard {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")};
    border-radius: {s(10)}; padding: {s(6)};
}}
QFrame#CatCard:hover {{ border-color: {P["BLUE"]}; background: {bg("BLUE_SOFT2")}; }}
QLabel#CatName  {{ font-size: {f(14.5)}; font-weight: 600; background: transparent; }}
QLabel#CatSub   {{ font-size: {f(11.5)}; color: {P["TEXT3"]}; background: transparent; }}
QLabel#CatArrow {{ font-size: {f(16)}; color: {P["TEXT3"]}; background: transparent; }}

/* nav buttons */
QPushButton#NavItem {{
    text-align: left; padding: {s(9)} {s(14)}; border: none; border-radius: {s(6)};
    background: transparent; color: {P["TEXT"]}; font-size: {f(13.5)};
}}
QPushButton#NavItem:hover   {{ background: {bg("LINE")}; }}
QPushButton#NavItem:checked {{
    background: {bg("BLUE_SOFT")}; color: {P["BLUE"]}; font-weight: 600;
}}

/* generic buttons */
QPushButton {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")}; border-radius: {s(6)};
    padding: {s(6)} {s(14)}; color: {P["TEXT"]};
}}
QPushButton:hover    {{ background: {bg("LINE")}; }}
QPushButton:pressed  {{ background: {bg("LINE2")}; }}
QPushButton:disabled {{ color: {P["TEXT3"]}; border-color: {bgl("LINE")}; }}
QPushButton#Primary {{
    background: {P["BLUE"]}; border: {s(1)} solid {P["BLUE"]}; color: #FFFFFF; font-weight: 600;
}}
QPushButton#Primary:hover   {{ background: {P["BLUE_HOVER"]}; border-color: {P["BLUE_HOVER"]}; }}
QPushButton#Primary:pressed {{ background: {P["BLUE_ACTIVE"]}; }}
QPushButton#Primary:disabled{{ background: #A9CBEA; border-color: #A9CBEA; color: #fff; }}
QPushButton#Danger {{
    background: {P["ERR"]}; border: {s(1)} solid {P["ERR"]}; color: #fff; font-weight: 600;
}}
QPushButton#Danger:hover {{ background: #D1342A; }}

/* cards */
QFrame#Card {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(10)};
}}
QFrame#StatCard {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(10)};
}}

/* inputs */
QLineEdit, QComboBox, QSpinBox {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")}; border-radius: {s(6)};
    padding: {s(5)} {s(9)}; min-height: {s(18)};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: {s(1)} solid {P["BLUE"]}; }}
QComboBox::drop-down {{ border: none; width: {s(20)}; }}
/* QSpinBox 被 QSS 样式化后默认上下按钮会失效（上键点不动），
   必须显式定义 up/down-button 及箭头，热区才可靠 */
QSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: {s(20)}; border: none; background: transparent;
}}
QSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: {s(20)}; border: none; background: transparent;
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
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")}; selection-background-color: {bg("BLUE_SOFT")};
    selection-color: {P["BLUE"]}; outline: none;
}}
QCheckBox {{ spacing: {s(7)}; background: transparent; }}
{_chk_qss}
QCheckBox::indicator {{
    width: {s(16)}; height: {s(16)}; border: {s(1)} solid {bgl("LINE2")};
    border-radius: {s(5)}; background: {bg("PANEL")};
}}
QCheckBox::indicator:hover   {{ border-color: {P["BLUE"]}; }}
QCheckBox::indicator:checked {{ background: {P["BLUE"]}; border-color: {P["BLUE"]}; }}

/* sliders */
QSlider::groove:horizontal {{
    background: {bg("LINE2")}; height: {s(4)}; border-radius: {s(2)};
}}
QSlider::sub-page:horizontal {{
    background: {P["BLUE"]}; height: {s(4)}; border-radius: {s(2)};
}}
QSlider::handle:horizontal {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE2")};
    width: {s(16)}; height: {s(16)}; margin: {s(-6)} 0; border-radius: {s(8)};
}}
QSlider::handle:horizontal:hover {{ border-color: {P["BLUE"]}; }}

/* tables */
QTableWidget, QTreeWidget {{
    background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(10)};
    gridline-color: {bgl("LINE")}; outline: none;
    selection-background-color: {bg("BLUE_SOFT")}; selection-color: {P["TEXT"]};
}}
QTableWidget::item, QTreeWidget::item {{ padding: {s(6)} {s(5)}; border-bottom: {s(1)} solid {bgl("LINE")}; }}
QHeaderView::section {{
    background: {bg("PANEL")}; border: none; border-bottom: {s(1)} solid {bgl("LINE")};
    padding: {s(7)} {s(6)}; font-weight: 600; color: {P["TEXT2"]};
}}
QTableCornerButton::section {{ background: {bg("PANEL")}; border: none; }}

/* scrollbars */
QScrollBar:vertical {{ background: transparent; width: {s(10)}; margin: 0; }}
QScrollBar::handle:vertical {{ background: {bgl("LINE2")}; border-radius: {s(5)}; min-height: {s(30)}; }}
QScrollBar::handle:vertical:hover {{ background: {bgl("TEXT3")}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: {s(10)}; }}
QScrollBar::handle:horizontal {{ background: {bgl("LINE2")}; border-radius: {s(5)}; min-width: {s(30)}; }}

/* progress bars */
QProgressBar {{
    background: {bg("LINE")}; border: none; border-radius: {s(3)};
    min-height: {s(6)}; max-height: {s(6)}; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {P["BLUE"]}; border-radius: {s(3)}; }}
QProgressBar#Warn::chunk {{ background: {P["WARN"]}; }}
QProgressBar#Err::chunk  {{ background: {P["ERR"]}; }}
QProgressBar#Cpu::chunk {{ background: {P["ACCENT_CPU"]}; }}
QProgressBar#Mem::chunk {{ background: {P["ACCENT_MEM"]}; }}
QProgressBar#Gpu::chunk {{ background: {P["ACCENT_GPU"]}; }}
QProgressBar#Disk::chunk {{ background: {P["ACCENT_DISK"]}; }}
QProgressBar#Net::chunk {{ background: {P["ACCENT_NET"]}; }}

/* pills */
QLabel#Pill      {{ background: {bg("LINE")}; color: {P["TEXT2"]}; border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillOk    {{ background: {bg("OK_BG")};   color: {P["OK"]};   border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillWarn  {{ background: {bg("WARN_BG")}; color: {P["WARN"]}; border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillErr   {{ background: {bg("ERR_BG")};  color: {P["ERR"]};  border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillBlue  {{ background: {bg("BLUE_SOFT")}; color: {P["BLUE"]}; border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillCpu   {{ background: {bg("ACCENT_CPU_SOFT")};  color: {P["ACCENT_CPU"]};  border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillMem   {{ background: {bg("ACCENT_MEM_SOFT")};  color: {P["ACCENT_MEM"]};  border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillGpu   {{ background: {bg("ACCENT_GPU_SOFT")};  color: {P["ACCENT_GPU_TEXT"]};  border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillDisk  {{ background: {bg("ACCENT_DISK_SOFT")}; color: {P["ACCENT_DISK"]}; border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}
QLabel#PillNet   {{ background: {bg("ACCENT_NET_SOFT")};  color: {P["ACCENT_NET_TEXT"]};  border-radius: {s(11)}; padding: {s(3)} {s(11)}; font-size: {f(11.5)}; }}

/* notices：颜色全部取自调色板，深色主题下同样是「浅字深底」 */
QLabel#Notice {{
    border-radius: {s(8)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("BLUE_SOFT2")}; border: {s(1)} solid {bgl("BLUE")}; color: {P["NOTICE_TEXT"]};
}}
QLabel#NoticeWarn {{ border-radius: {s(8)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("WARN_BG")}; border: {s(1)} solid {bgl("WARN")}; color: {P["WARN"]}; }}
QLabel#NoticeErr  {{ border-radius: {s(8)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("ERR_BG")}; border: {s(1)} solid {bgl("ERR")}; color: {P["ERR"]}; }}
QLabel#NoticeOk   {{ border-radius: {s(8)}; padding: {s(12)} {s(15)}; font-size: {f(12.5)};
    background: {bg("OK_BG")}; border: {s(1)} solid {bgl("OK")}; color: {P["OK"]}; }}

QStatusBar {{ background: {bg("PANEL")}; border-bottom-left-radius: {s(12)}; border-bottom-right-radius: {s(12)}; border-top: {s(1)} solid {bgl("LINE")}; color: {P["TEXT2"]}; }}
QStatusBar[maximized="true"] {{ border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; }}
QTabWidget::pane {{ border: {s(1)} solid {bgl("LINE")}; border-radius: {s(10)}; background: {bg("PANEL")}; top: {s(-1)}; }}
QTabBar::tab {{
    background: transparent; padding: {s(7)} {s(16)}; border: {s(1)} solid transparent;
    border-top-left-radius: {s(8)}; border-top-right-radius: {s(8)}; color: {P["TEXT2"]};
}}
QTabBar::tab:selected {{ background: {bg("PANEL")}; border-color: {bgl("LINE")}; border-bottom-color: {bg("PANEL")}; color: {P["BLUE"]}; font-weight: 600; }}
QListWidget {{ background: {bg("PANEL")}; border: {s(1)} solid {bgl("LINE")}; border-radius: {s(10)}; outline: none; }}
QListWidget::item {{ padding: {s(7)} {s(6)}; }}
QListWidget::item:selected {{ background: {bg("BLUE_SOFT")}; color: {P["TEXT"]}; }}
QToolTip {{ background: {bg("PANEL")}; color: {P["TEXT"]}; border: {s(1)} solid {bgl("LINE2")}; border-radius: {s(6)}; padding: {s(5)} {s(8)}; }}

QMessageBox {{ background: {bg("PANEL")}; }}
QMessageBox QLabel {{ color: {P["TEXT"]}; }}

/* dialogs (startup mode chooser etc.) must follow the active theme */
QDialog {{ background: {bg("BG")}; }}
QDialog QLabel {{ color: {P["TEXT"]}; background: transparent; }}
QDialog QLabel#DlgTitle {{ font-size: {f(15)}; font-weight: 600; }}
QDialog QLabel#DlgDesc  {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; }}

/* 安全确认对话框（屏幕正中） */
QDialog#SafetyDialog {{ background: {bg("PANEL")}; }}
QLabel#SdIcon {{ font-size: {f(26)}; color: {P["WARN"]}; background: transparent; }}
QLabel#SdTitle {{ font-size: {f(15.5)}; font-weight: 600; }}
QLabel#SdDesc {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; }}
QLabel#SDLabel {{ color: {P["TEXT2"]}; font-size: {f(12.5)}; font-weight: 600; }}
QLabel#SdWarn {{
    background: {bg("WARN_BG")}; color: {P["WARN"]};
    border: {s(1)} solid {bgl("WARN")}; border-radius: {s(8)};
    padding: {s(9)} {s(11)}; font-size: {f(12.5)};
}}
QListWidget#SdList {{
    background: {bg("BG")}; border: {s(1)} solid {bgl("LINE")};
    border-radius: {s(8)}; outline: none; font-size: {f(12.5)};
}}
QListWidget#SdList::item {{ padding: {s(6)} {s(8)}; color: {P["TEXT"]}; }}
"""
