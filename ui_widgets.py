# -*- coding: utf-8 -*-
"""从 app.py 拆出的「自绘 UI 小件 + 样式助手」。

这些符号对 app.py 的内部符号**零依赖**（只用到 PySide6 与 theme），
所以可以独立成模块而不产生循环导入。由 _extract_ui_widgets.py 机械切出，
内容与在 app.py 中逐字一致。
"""
import theme as T
from PySide6.QtCore import (QEasingCurve, QObject, QPointF, QPropertyAnimation, QVariantAnimation, Qt, Signal)
from PySide6.QtGui import (QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import QWidget

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
        """平滑过渡到 v（None 直接显示占位符）。

        流畅度不加钱的两个小诀窍：
        - 变化小到肉眼不可辨（<半个显示步长）就直接落值，不再空跑 280ms 动画；
        - 时长随变化幅度自适应：小变化快落定、大变化平滑滑过去，
          每秒刷新的指标不会出现「永远在赶路」的漂移感。
        """
        if v is None:
            self._a.stop()
            self._cur = None
            self.lb.setText("—")
            return
        v = float(v)
        start = self._cur if self._cur is not None else v
        delta = abs(v - start)
        self._a.stop()
        if delta < 0.5:                      # 显示上没有可感知的变化
            self._cur = v
            self.lb.setText(self.fmt(v))
            return
        self._a.setDuration(max(140, min(320, int(120 + delta * 3.0))))
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
    """进度条数值补间：宽度平滑生长，而不是每秒硬跳。

    兼容两种目标：QProgressBar（`value` 属性）与自绘 RingGauge（`pct` 属性）。
    自绘控件不用 `value` 当属性名，是为了保住 `value()` 这个与 QProgressBar
    同名的方法 —— 换控件时才不会把调用方炸掉。
    """

    def __init__(self, bar, dur=340, parent=None):
        super().__init__(parent or bar)
        self.bar = bar
        self._prop = b"pct" if hasattr(bar, "pct") else b"value"
        self._a = QPropertyAnimation(bar, self._prop, self)
        self._a.setDuration(dur)
        self._a.setEasingCurve(QEasingCurve.OutCubic)

    def _cur(self):
        return int(self.bar.pct) if self._prop == b"pct" else int(self.bar.value())

    def to(self, v):
        v = int(max(0, min(100, round(v or 0))))
        cur = self._cur()
        if cur == v:
            return
        self._a.stop()
        # 幅度小 → 快落定（少一串过渡帧）；幅度大 → 略拉长（生长感更顺）
        self._a.setDuration(max(150, min(360, int(140 + abs(v - cur) * 2.2))))
        self._a.setStartValue(cur)
        self._a.setEndValue(v)
        self._a.start()

class Sparkline(QWidget):
    """迷你折线（网络指标卡）：保存最近 N 个采样点，按窗口内最大值自适应缩放。

    只接 push()，不参与补间 —— 折线的意义就是"看见波动"，插值反而抹平了它。
    """

    def __init__(self, parent=None, color=None, dark=False, points=52, height=40,
                 accent="ACCENT_NET"):
        super().__init__(parent)
        self._buf = []
        self._cap = int(points)
        self._dark = dark
        self._accent = accent
        self._fixed_color = color
        self._color = color or T.get_color(accent, dark)
        self.setMinimumHeight(int(height))
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_dark(self, dark):
        self._dark = bool(dark)
        self._color = self._fixed_color or T.get_color(self._accent, self._dark)
        self.update()

    def push(self, v):
        try:
            v = float(v or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        v = max(0.0, v)
        self._buf.append(v)
        if len(self._buf) > self._cap:
            del self._buf[:len(self._buf) - self._cap]
        self.update()

    def clear(self):
        self._buf = []
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        if w < 8 or h < 8:
            return
        pad = 3
        # 基线（底部一条很淡的参考线，让空图不至于像坏了）
        pen = QPen(QColor(T.get_color("TRACK", self._dark)), 1.0)
        p.setPen(pen)
        p.drawLine(pad, h - pad, w - pad, h - pad)
        if len(self._buf) < 2:
            return
        lo, hi = 0.0, max(self._buf) or 1.0
        if hi <= 0:
            hi = 1.0
        # 点数还没攒够时按现有数量铺满整宽（否则起初只有右边一小段线，像坏了）；
        # 攒满容量后转为固定步长的滚动视图。
        n = len(self._buf)
        span = max(1, (self._cap if n >= self._cap else n) - 1)
        step = (w - 2 * pad) / float(span)
        off = 0.0 if n < self._cap else 0.0
        pts = []
        for i, v in enumerate(self._buf):
            x = pad + off + i * step
            y = (h - pad) - (v - lo) / (hi - lo) * (h - 2 * pad)
            pts.append(QPointF(x, y))
        # 面积：折线下方铺一层极淡的同色（效果图的"发光"感，成本只有一次填充）
        area = QPolygonF(pts + [QPointF(pts[-1].x(), h - pad), QPointF(pts[0].x(), h - pad)])
        g = QLinearGradient(0, 0, 0, h)
        c1 = QColor(self._color); c1.setAlpha(70)
        c2 = QColor(self._color); c2.setAlpha(0)
        g.setColorAt(0.0, c1); g.setColorAt(1.0, c2)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
        p.drawPolygon(area)
        pen = QPen(QColor(self._color), 1.8)
        pen.setJoinStyle(Qt.RoundJoin); pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawPolyline(QPolygonF(pts))

def page_active(page):
    """这个页面现在该不该跑 1 秒轮询。

    注意 `QWidget.isVisible()` 在「窗口被最小化」时**仍然是 True** ——
    只判断它的话，程序收进任务栏后后台照样每秒起线程、跑 nvidia-smi 与 PowerShell，
    老机器上就是"关了窗口还在吃 CPU"。这里额外看窗口是否最小化/隐藏。
    """
    try:
        if not page.isVisible():
            return False
        win = page.window()
        if win is None:
            return True
        return not (win.isMinimized() or win.isHidden())
    except Exception:
        return False

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

class HueBar(QWidget):
    """横向色相条（彩虹渐变）：单击 / 拖动选任意主题色。

    拖动过程只发 `preview(hex)` 做轻量预览（不动全局样式，避免反复重建 QSS 卡顿）；
    松开鼠标才发 `picked(hex)`，由设置页真正应用主题色。
    """

    preview = Signal(str)
    picked = Signal(str)

    def __init__(self, parent=None, sat=205, val=232):
        super().__init__(parent)
        self._sat, self._val = sat, val
        self._hue = 210
        self.setFixedHeight(28)
        self.setMinimumWidth(220)
        self.setCursor(Qt.PointingHandCursor)

    def hue(self):
        return self._hue

    def set_hue(self, h):
        """外部同步：当前主题色变化时把指示器移到对应色相。"""
        if h is None or h < 0:
            return
        self._hue = max(0, min(359, int(h)))
        self.update()

    def _hue_at(self, x):
        w = max(1, self.width())
        return max(0, min(359, int(round((x / float(w)) * 359.0))))

    def _apply_x(self, x, final):
        self._hue = self._hue_at(x)
        self.update()
        hexv = QColor.fromHsv(self._hue, self._sat, self._val).name()
        (self.picked if final else self.preview).emit(hexv)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._apply_x(e.position().x(), False)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._apply_x(e.position().x(), False)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._apply_x(e.position().x(), True)

    def paintEvent(self, _e):
        from PySide6.QtGui import QBrush
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        rad = h / 2.0
        clip = QPainterPath()
        clip.addRoundedRect(0.5, 0.5, w - 1.0, h - 1.0, rad, rad)
        p.setClipPath(clip)
        grad = QLinearGradient(0, 0, w, 0)
        for deg in range(0, 361, 15):          # 每 15° 一个色停，形成连续彩虹
            grad.setColorAt(min(1.0, deg / 360.0),
                            QColor.fromHsv(deg % 360, self._sat, self._val))
        p.fillRect(0, 0, w, h, QBrush(grad))
        p.setClipping(False)
        # 指示器：白圈 + 当前色圆点，位置对应色相
        x = (self._hue / 359.0) * (w - 1)
        p.setPen(QPen(QColor(255, 255, 255), 3))
        p.setBrush(QColor.fromHsv(self._hue, self._sat, self._val))
        p.drawEllipse(QPointF(x, h / 2.0), rad - 2.0, rad - 2.0)
        p.setPen(QPen(QColor(0, 0, 0, 55), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(x, h / 2.0), rad - 2.0, rad - 2.0)
        p.end()


# --- 富文本高度测量（fit_label 及其两个内部助手）---
# `_set()` 会调 `fit_label`，所以这几个必须和它在一起。
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
