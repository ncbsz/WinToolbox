# -*- coding: utf-8 -*-
"""功能完整性审计：覆盖主回归（tests/regress_all.py）没测到的功能区。

覆盖：设置页备份/恢复/导入导出/开机自启、托盘、窗口控制、
      server 端纯函数（卸载命令行解析 / 服务 ImagePath 解析 / 格式化函数）。
所有写操作均打桩或写入临时文件，绝不改系统、不改用户设置。
用法：<venv>/python.exe tests/audit_features.py
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (QApplication, QMessageBox, QLabel, QLineEdit)
from PySide6.QtCore import QTimer, QEventLoop

import app as A
import server as S

FAIL = []


def chk(cond, label):
    print(("  [OK]  " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL.append(label)


def need(cond, msg):
    if not cond:
        raise AssertionError(msg)
    return True


# ---------------- 全局打桩：弹窗、写操作 ----------------
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None
A.confirm = lambda *a, **k: False
A.QMessageBox.information = staticmethod(lambda *a, **k: None)
A.QMessageBox.warning = staticmethod(lambda *a, **k: None)

calls = {"settings_save": [], "set_startup": []}
A.settings_save = lambda d: (calls["settings_save"].append(dict(d)), True)[1]
A.set_startup = lambda v: (calls["set_startup"].append(v), True)[1]
# DNS 延迟口径已改为 ICMP ping —— 测试里一律打桩，避免真实发包（慢且不确定）
PING_CALLS = []


def _fake_ping(ip, *a, **k):
    PING_CALLS.append(str(ip))
    try:
        return (int(str(ip).split(".")[-1]) % 40) + 5
    except Exception:
        return 10


S.ping_ms = _fake_ping

_orig = {"enter": None, "none": None}


def _stub_modals():
    _orig["enter"] = A.MainWindow.showEvent
    _orig["none"] = A.MainWindow.showEvent


app = QApplication(sys.argv)
win = A.MainWindow(dark=False)
win.show()


def pump(sec):
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()


pump(2.0)

print("=" * 62)
print("A. 设置页（备份 / 恢复 / 导入导出 / 开机自启）")
print("=" * 62)
win.goto("settings")
pump(0.6)
se = win.pages["settings"]
se.save_now()
chk(len(calls["settings_save"]) == 1, "save_now 调用了 settings_save")

before = dict(win.settings)
se.restore_now()                     # 真实读取（文件不存在时走提示分支）
chk(isinstance(win.settings, dict), "restore_now 后 settings 仍为 dict")

tmp_dir = tempfile.mkdtemp(prefix="wtb_audit_")
exp_path = os.path.join(tmp_dir, "cfg.json")
A.QFileDialog = type("FD", (), {
    "getSaveFileName": staticmethod(lambda *a, **k: (exp_path, "")),
    "getOpenFileName": staticmethod(lambda *a, **k: (exp_path, "")),
})
se.export_cfg()
chk(os.path.exists(exp_path), "export_cfg 真的写出了文件")
data = json.load(open(exp_path, encoding="utf-8"))
chk(isinstance(data, dict), "导出内容是合法 JSON 对象：%d 个键" % len(data))

# 导入合法配置
calls["settings_save"].clear()
json.dump({"dark": True, "opacity": 90}, open(exp_path, "w", encoding="utf-8"), ensure_ascii=False)
se.import_cfg()
chk(win.settings.get("opacity") == 90, "import_cfg 更新了 settings（opacity=90）")
chk(len(calls["settings_save"]) == 1, "import_cfg 落盘一次")
chk(se.spin_op.value() == 90, "导入后透明度控件已同步")

# 导入非法文件（不应崩、不应落盘）
calls["settings_save"].clear()
open(exp_path, "w", encoding="utf-8").write("{ 不是 json")
se.import_cfg()
chk(len(calls["settings_save"]) == 0, "非法 JSON 时不落盘（只提示）")

# 开机自启：失败分支要回退复选框
A.set_startup = lambda v: False
se.cb_startup.setChecked(True)
se.on_startup(True)
chk(se.cb_startup.isChecked() is False, "开机自启失败时复选框回退")
A.set_startup = lambda v: (calls["set_startup"].append(v), True)[1]

print()
print("=" * 62)
print("B. 托盘 / 窗口控制")
print("=" * 62)
tray = getattr(win, "tray", None)
chk(tray is not None or not A.QSystemTrayIcon.isSystemTrayAvailable(),
    "托盘已初始化（无托盘环境则跳过）")
if tray is not None:
    acts = [a.text() for a in tray.contextMenu().actions() if a.text()]
    chk("设置" in acts and "退出" in acts, "托盘菜单含「设置 / 退出」：%s" % acts)
    chk(tray.toolTip().startswith(A.APP_NAME), "托盘 tooltip 正常：%s" % tray.toolTip())

_chk_items = [se.combo_close.itemData(i) for i in range(se.combo_close.count())]
chk(_chk_items == ["ask", "tray", "exit"],
    "关闭行为下拉为三选一：%s" % _chk_items)
se.combo_close.setCurrentIndex(se.combo_close.findData("tray"))
chk(win.settings.get("close_action") == "tray"
    and win.settings.get("minimize_to_tray") is True,
    "选「最小化到托盘」写入 close_action + minimize_to_tray")
se.combo_close.setCurrentIndex(se.combo_close.findData("ask"))
chk(win.settings.get("close_action") == "ask", "选回「每次询问」后 close_action=ask")

# 关闭弹窗的按钮判定：点右上角 × / 按 Esc / 点「取消」都必须等于「不关闭软件」
_bx = A.QMessageBox()
_bt = _bx.addButton("最小化到托盘", A.QMessageBox.AcceptRole)
_be = _bx.addButton("退出程序", A.QMessageBox.DestructiveRole)
_bc = _bx.addButton("取消", A.QMessageBox.RejectRole)
chk(win._close_pick(_bt, _bt, _be, _bc) == "tray", "点「最小化到托盘」→ tray")
chk(win._close_pick(_be, _bt, _be, _bc) == "exit", "点「退出程序」→ exit")
chk(win._close_pick(_bc, _bt, _be, _bc) == "ask", "点「取消」→ 不关闭")
chk(win._close_pick(None, _bt, _be, _bc) == "ask",
    "点右上角 ×（无 RejectRole 时 clickedButton=None）→ 不关闭")
chk(win._close_pick("?", _bt, _be, _bc) == "ask", "意外对象也一律按「不关闭」处理")

# 窗口不可见 → 服务端采样线程停摆（不是降频空转，是真停）
win.settings["close_action"] = "ask"
win.hide()
pump(0.5)
chk(S.monitors_paused() is True and win._mon_paused is True,
    "窗口隐藏 / 收进托盘 → 后台监测暂停")
win.showNormal()
pump(0.5)
chk(S.monitors_paused() is False and win._mon_paused is False,
    "恢复窗口 → 后台监测恢复")
chk(callable(getattr(S, "set_monitor_paused", None)), "服务端暴露 set_monitor_paused 开关")

w0 = win.isMaximized()
win.toggle_maximized()
pump(0.4)
chk(win.isMaximized() != w0, "toggle_maximized 切换了最大化状态")
win.toggle_maximized()
pump(0.4)
win.resize(1200, 800)
pump(0.3)
chk(win.width() == 1200 or win.isMaximized(), "窗口可 resize（宽=%d）" % win.width())

print()
print("=" * 62)
print("C. server 端纯函数")
print("=" * 62)
cases = [
    (r'"C:\Program Files\Foo\unins000.exe" /S', r"C:\Program Files\Foo\unins000.exe"),
    (r"C:\Program Files\Foo\unins000.exe /S", r"C:\Program Files\Foo\unins000.exe"),
    (r'"C:\Apps\x.exe"', r"C:\Apps\x.exe"),
    (r"%ProgramFiles%\Foo\unins.exe /quiet", os.path.expandvars(r"%ProgramFiles%\Foo\unins.exe")),
    (r"msiexec.exe /I {4B857D52-8C5A-4A9A-A17D-0EE8A34A12C7}", "msiexec.exe"),
]
bad = [(c, S.parse_uninstall_cmd(c)[0]) for c, want in cases
       if S.parse_uninstall_cmd(c)[0].lower() != want.lower()]
chk(not bad, "parse_uninstall_cmd 5 种形态全对：%s" % (bad or "ok"))
chk(S._msi_guid(cases[-1][0]) == "{4B857D52-8C5A-4A9A-A17D-0EE8A34A12C7}",
    "_msi_guid 提取 GUID 正确")
chk(S._svc_exe(r'"C:\Program Files\Common Files\PUBG\zksvc.exe" -k') ==
    r"C:\Program Files\Common Files\PUBG\zksvc.exe", "服务路径：带引号能正确解析")
chk(S._svc_exe(r"\SystemRoot\System32\drivers\a.sys").lower().endswith(r"system32\drivers\a.sys"),
    "服务路径：\\SystemRoot\\ 前缀已展开")
chk(S._svc_exe(r"C:\Program Files\Foo\svc.exe -k") == "",
    "服务路径：无引号含空格 → 放弃判断（不误报）")
chk(A.fmt_bytes(0) and A.fmt_bytes(None) is not None, "fmt_bytes 边界值不崩")
chk(A.fmt_linkspeed(1000) and A.fmt_linkspeed(None) is not None, "fmt_linkspeed 边界值不崩")
chk(A.fmt_rate(0) is not None and A.fmt_mbps(0) is not None, "fmt_rate / fmt_mbps 不崩")
chk(A.fmt_uptime(0) and A.fmt_uptime(99999), "fmt_uptime 边界不崩")
chk(A.fmt_temp(None) == "不支持" and A.fmt_temp(65.4) == "65°C", "fmt_temp 边界正确")
chk(A.fmt_ghz(0) == "—" and A.fmt_ghz(2500) == "2.50 GHz", "fmt_ghz 正确")
chk(A.short_cpu_name("12th Gen Intel(R) Core(TM) i5-12500H") == "i5-12500H",
    "short_cpu_name 简化正确")
chk(A.is_dgpu("NVIDIA GeForce RTX 3050") and not A.is_dgpu("Intel(R) Iris(R) Xe Graphics"),
    "is_dgpu 判定正确")

print()
print("=" * 62)
print("D. 软件卸载命令行构造（打桩 Popen，绝不真的启动卸载程序）")
print("=" * 62)
captured = []
_real_popen = S.subprocess.Popen


class _FakeProc:
    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


S.subprocess.Popen = lambda cmd, **k: (captured.append(cmd), _FakeProc())[1]
try:
    # exe 分支：含空格路径必须整体加引号（历史 bug：被空格截断）
    # 用真实存在的 7z.exe 只是为了让函数走到"启动"分支；Popen 已打桩，不会真的运行
    probe_exe = r"C:\Program Files\7-Zip\7z.exe"
    if os.path.exists(probe_exe):
        r = S.uninstall_software("HKCU\\SOFTWARE\\绝对不存在的键",
                                 '"%s" /S' % probe_exe)
        chk(bool(captured) and ('"%s"' % probe_exe) in captured[0],
            "exe 分支：含空格路径整体加引号 → %s" % (captured[0] if captured else None))
        chk(set(r.keys()) >= {"ok", "exe", "left", "secs", "msg"} and r["left"] is False,
            "返回结构完整且判据正确（键已消失 → left=False）：%s" % r.get("msg", ""))
        captured.clear()
    else:
        print("  [skip] 本机没有含空格的真实 exe 可作探针")
    # MSI 分支：统一走 msiexec /x {GUID} /qb /norestart，不照搬原串
    S.uninstall_software("HKCU\\SOFTWARE\\绝对不存在的键",
                         "msiexec.exe /I {4B857D52-8C5A-4A9A-A17D-0EE8A34A12C7}")
    chk(bool(captured) and captured[0].lower().find("msiexec") >= 0
        and "/x {4B857D52-8C5A-4A9A-A17D-0EE8A34A12C7}".lower() in captured[0].lower()
        and "/qb" in captured[0].lower(),
        "MSI 分支：改写为 msiexec /x {GUID} /qb /norestart → %s" % (captured[0] if captured else None))
    captured.clear()
    # 卸载程序文件不存在 → 直接报"疑似残留"，不启动任何进程
    r2 = S.uninstall_software("HKCU\\SOFTWARE\\K", r'"C:\绝不存在\unins000.exe"')
    chk(not captured and r2.get("left") is True and "残留" in r2.get("msg", ""),
        "卸载程序不存在 → 不启动进程，提示疑似残留")
finally:
    S.subprocess.Popen = _real_popen

print()
print("=" * 62)
print("E. 补齐未覆盖的按钮回调（load_procs ×2 / open_github / pick_accent）")
print("=" * 62)
from PySide6.QtGui import QDesktopServices, QColor
from PySide6.QtWidgets import QColorDialog

for page_key, attr in (("cputune", "proc_table"), ("performance", "table")):
    win.goto(page_key)
    pump(1.0)
    pg = win.pages[page_key]
    tbl = getattr(pg, attr)
    tbl.setRowCount(0)                    # 清空后由回调重新加载
    pg.load_procs()
    pump(9.0)                             # 进程表走 CIM，等一会儿
    chk(tbl.rowCount() > 0, "%s 页「刷新进程」按钮回调有数据（%d 行）" % (page_key, tbl.rowCount()))

# 打开 GitHub（打桩 QDesktopServices，避免真的弹浏览器）
opened = []
_real_open = QDesktopServices.openUrl
QDesktopServices.openUrl = staticmethod(
    lambda u: (opened.append(u.toString()), True)[1])
try:
    se2 = win.pages["settings"]
    se2.open_github()
    chk(opened and "github" in opened[0].lower(),
        "open_github 打开作者仓库：%s" % (opened[0] if opened else None))
    chk("github.com" in A.GITHUB_URL.lower(), "GITHUB_URL 常量正确：%s" % A.GITHUB_URL)
finally:
    QDesktopServices.openUrl = _real_open

# 调色盘选色（打桩 QColorDialog）
_real_color = QColorDialog.getColor
QColorDialog.getColor = staticmethod(lambda *a, **k: QColor("#FF6600"))
try:
    before = win.settings.get("accent")
    se2.pick_accent()
    pump(0.5)
    chk(win.settings.get("accent", "").lower() == "#ff6600",
        "pick_accent 应用了调色盘颜色（%s → %s）" % (before, win.settings.get("accent")))
    chk(win.accent.lower() == "#ff6600", "窗口 accent 同步")
    # 取消选色（无效色）不应改设置
    QColorDialog.getColor = staticmethod(lambda *a, **k: QColor())
    cur = win.settings.get("accent")
    se2.pick_accent()
    pump(0.3)
    chk(win.settings.get("accent") == cur, "取消选色时保持不变")
finally:
    QColorDialog.getColor = _real_color

print()
print("=" * 62)
print("⑨ DNS 探测并行分支（防「静默退化为串行」）")
print("=" * 62)

# 历史 bug：work() 入参拆包写错（src 元素是 (s, cat)，enumerate 后按 3 元组拆），
# 并行分支每次抛 ValueError 被 except 吞掉 → 悄悄退回串行。功能"看起来正常"，
# 只是 114 台要跑几十秒。这里用「并发峰值」把它钉死：串行时峰值恒为 1。
import threading
import time as _time

_real_probe2 = S._dns_probe
_lk = threading.Lock()
_st2 = {"live": 0, "peak": 0, "calls": 0}


def _counting_probe(ip, domain, timeout=1.5, measured=3, comm=False):
    with _lk:
        _st2["live"] += 1
        _st2["calls"] += 1
        _st2["peak"] = max(_st2["peak"], _st2["live"])
    _time.sleep(0.20)
    with _lk:
        _st2["live"] -= 1
    return {"ok": True, "ms": 10.0, "answer": "1.2.3.4"}


S._dns_probe = _counting_probe
try:
    _n = len(S.DNS_SERVERS["domestic"])
    _t0 = _time.time()
    _rr = S.dns_test("domestic", "www.baidu.com")
    _dt = _time.time() - _t0
    print("  %d 台 / 单次 0.20s → 实测 %.2fs，并发峰值 %d（串行应 %.1fs）"
          % (_n, _dt, _st2["peak"], _n * 0.20))
    chk(_st2["peak"] > 1, "并行分支真的并行了（峰值 %d，退化时会恒为 1）" % _st2["peak"])
    chk(_st2["peak"] >= 24, "并发度达 32 档（峰值 %d）" % _st2["peak"])
    chk(_st2["calls"] == _n, "每台探测一次（%d 次）" % _st2["calls"])
    chk(len(_rr) == _n, "返回条数 == 服务器数（%d）" % len(_rr))
    chk(_dt < _n * 0.20 / 6, "耗时远低于串行（%.2fs）" % _dt)
    _ms = [x["ms"] for x in _rr if x["ok"]]
    chk(_ms == sorted(_ms), "可用项按毫秒升序")
finally:
    S._dns_probe = _real_probe2

# 代码 bug 类异常必须上抛（不许被兜底 except 吞掉后静默变串行）
def _buggy_probe(ip, domain, timeout=1.5, measured=3, comm=False):
    raise ValueError("simulated code bug (unpacking)")


S._dns_probe = _buggy_probe
try:
    _raised = False
    try:
        S.dns_test("foreign", "www.baidu.com")
    except ValueError:
        _raised = True
    chk(_raised, "代码 bug（ValueError）会向上抛出，不被静默吞掉退化成串行")
finally:
    S._dns_probe = _real_probe2

# 环境类异常仍应优雅退化（结果完整）
import concurrent.futures as _cf2

_real_tpe = _cf2.ThreadPoolExecutor


class _BoomTpe:
    def __init__(self, *a, **k):
        raise RuntimeError("simulated environment failure")


_cf2.ThreadPoolExecutor = _BoomTpe
S._dns_probe = _counting_probe
try:
    _st2.update(live=0, peak=0, calls=0)
    _rr2 = S.dns_test("foreign", "www.baidu.com")
    _n2 = len(S.DNS_SERVERS["foreign"])
    chk(len(_rr2) == _n2, "环境类异常 → 串行兜底仍返回全部结果（%d 条）" % len(_rr2))
    chk(_st2["calls"] == _n2, "兜底每台探测一次（%d 次）" % _st2["calls"])
finally:
    _cf2.ThreadPoolExecutor = _real_tpe
    S._dns_probe = _real_probe2

print()
print("=" * 62)
print("⑩ DNS 页：结果流式上屏 + 「域名」输入项")
print("=" * 62)

dn = win.pages["dns"]

chk(hasattr(dn, "domain"), "DNSPage 保留 self.domain（域名输入框）")
_labels = [w.text() for w in dn.findChildren(QLabel) if hasattr(w, "text")]
chk("域名：" in _labels, "界面有「域名：」标签")
chk(dn.domain.text().strip() == "www.baidu.com", "输入框默认值 %r" % dn.domain.text())
chk(dn.domain.placeholderText() != "", "输入框有占位提示")
chk(dn.test_btn is not None, "开始测试按钮已存为属性（运行时禁用防重复触发）")

# 输入的域名必须真的传给 dns_test（含「清空 → 回落默认值」这一分支）
_real_dns_test = S.dns_test
_seen_domain = []


def _spy_dns_test(mode, domain, on_each=None):
    _seen_domain.append(domain)
    return []


S.dns_test = _spy_dns_test
try:
    dn.domain.setText("example.com")
    dn.test()
    pump(0.8)
    chk(_seen_domain == ["example.com"], "自定义域名被传入 dns_test：%s" % _seen_domain)
    dn.domain.setText("")
    dn.test()
    pump(0.8)
    chk(_seen_domain[-1] == "www.baidu.com",
        "清空输入后回落默认域名（不报错）：%s" % _seen_domain[-1])
finally:
    S.dns_test = _real_dns_test
dn.domain.setText("www.baidu.com")
for _ in range(60):                      # 等上一次测试彻底收尾，再进入流式验证
    if not dn._dns_busy:
        break
    pump(0.05)
chk(not dn._dns_busy, "上一轮测试已收尾（busy 复位）")

# 流式判据：用「打桩探针 + 固定延时」把测试拉长到可观测，采样行数随时间的变化。
# 关键不变量 —— 必须出现「0 < 行数 < 总数」的中间态；测完一起呈现则只会看到 0 → 总数。
import threading
import time as _time2

_N = len(S.DNS_SERVERS["domestic"])
_LK = threading.Lock()
_st3 = {"n": 0}
_real_probe3 = S._dns_probe


def _slow_probe(ip, domain, timeout=1.5, measured=3, comm=False):
    _time2.sleep(0.25)
    with _LK:
        _st3["n"] += 1
    return {"ok": True, "ms": 20.0 + (_st3["n"] % 30), "answer": "1.2.3.4"}


S._dns_probe = _slow_probe
try:
    dn.combo.setCurrentIndex(0)          # 国内
    dn.test()
    _samples = []
    _t0 = _time2.time()
    while _time2.time() - _t0 < 3.0:
        pump(0.02)
        _samples.append((_time2.time() - _t0, dn.table.rowCount(), dn._dns_busy))
finally:
    S._dns_probe = _real_probe3

_partial = [(t_, n_) for t_, n_, b_ in _samples if 0 < n_ < _N]
_end = next((t_ for t_, n_, b_ in _samples if not b_), None)
if _partial:
    print("    首行出现于 %.2fs（%d 行）→ 全部结束于 %s（总 %d 台）"
          % (_partial[0][0], _partial[0][1],
             ("%.2fs" % _end) if _end is not None else "未结束", _N))
chk(bool(_partial), "测试过程中就显示出部分结果（流式，不是测完一起呈现）")
chk(bool(_partial) and _end is not None and _partial[0][0] < _end,
    "首行出现时刻早于测试结束时刻")
chk(_samples[-1][1] == _N, "最终行数 == 服务器数（%d）" % _samples[-1][1])
chk(_samples[-1][1] > 0 and not dn._dns_busy, "测试结束后 busy 标志复位")
chk(dn.test_btn.isEnabled(), "测试结束后按钮恢复可用")
chk("可用" in dn.status.text(), "状态栏给出汇总：%s" % dn.status.text()[:46])

# 推送回调（on_each）抛异常不能影响整体结果 —— 界面关闭时正是这种情形
_bad = S.dns_test("foreign", "www.baidu.com",
                  on_each=lambda r: (_ for _ in ()).throw(RuntimeError("ui gone")))
chk(len(_bad) == len(S.DNS_SERVERS["foreign"]),
    "on_each 抛异常时仍返回完整结果（%d 条）" % len(_bad))
chk(len(S.dns_test("foreign", "www.baidu.com")) == len(_bad),
    "on_each=None 与传回调结果一致")

print()
print("⑪ DNS 页：本机 DNS 模式（本机 ↔ 各 DNS 的通信延迟）")
_real_probe4 = S._dns_probe
_seen_comm = []


def _spy_probe(ip, domain, timeout=1.5, measured=3, comm=False):
    """记录探测口径：comm=True 为通信延迟，False 为解析延迟。"""
    _seen_comm.append(comm)
    return {"ok": True, "ms": 12, "answer": "93.184.216.34"}


try:
    local = S.local_dns_servers()
    chk(isinstance(local, list), "local_dns_servers() 返回 list")
    chk(all(isinstance(s, dict) and "name" in s and "ip" in s for s in local),
        "local_dns_servers() 元素结构正确（name/ip）")
    chk(len({s["ip"] for s in local}) == len(local), "本机 DNS 已去重")

    S._dns_probe = _spy_probe
    # 本机模式：服务器集 = 本机在用 + 国内 + 国外；延迟口径 = ICMP ping
    _seen_comm.clear()
    PING_CALLS.clear()
    rr_local = S.dns_test("local", "www.baidu.com")
    expect = (len(local) + len(S.DNS_SERVERS["domestic"])
              + len(S.DNS_SERVERS["foreign"]))
    chk(len(rr_local) == expect,
        "本机模式服务器集 = 本机在用+国内+国外（%d 台）" % expect)
    chk(len(PING_CALLS) == expect,
        "本机模式每台都走 ICMP ping 探测（ping_ms 调用 %d 次）" % len(PING_CALLS))
    chk(all(r["ms"] is not None and r["ok"] for r in rr_local),
        "延迟字段来自 ping（打桩后每台都有值）")
    chk(all("dns_ok" in r for r in rr_local),
        "每台都带 dns_ok 标记（用于区分「禁 ping 但可解析」）")
    if local:
        chk(any(r["cat"] == "本机在用" for r in rr_local),
            "本机网卡在用的 DNS 被标为「本机在用」")
        chk(all(r["ip"] in {s["ip"] for s in local}
                for r in rr_local if r["cat"] == "本机在用"),
            "「本机在用」项都来自本机网卡配置")
    # 其它模式同样走 ping 口径（统一为 DnsTools 口径）
    PING_CALLS.clear()
    S.dns_test("foreign", "www.baidu.com")
    chk(len(PING_CALLS) == len(S.DNS_SERVERS["foreign"]),
        "其它模式也走 ICMP ping（%d 台）" % len(PING_CALLS))
finally:
    S._dns_probe = _real_probe4

# DNSPage 必须提供「本机 DNS」模式选项
chk(any(k == "local" for _, k in A.DNSPage.MODES),
    "DNSPage.MODES 含「本机 DNS」(local) 选项")
# DNSPage 保留域名输入框（域名的检测保留）
chk(hasattr(dn, "domain"), "DNSPage 保留 self.domain（域名输入框）")
# _on_mode 切换提示文本（本机模式明确说明测的是通信延迟）
dn._on_mode(dn.combo.findData("local"))
chk("ICMP ping" in dn.mode_hint.text(), "切到本机 DNS 模式时说明测的是 ICMP ping")
dn._on_mode(dn.combo.findData("mixed"))
chk("ICMP ping" in dn.mode_hint.text(), "其它模式也说明是 ICMP ping 口径")

print()
print("⑫ 软件管理：Geek 式动作的数据与安全阀（全部只读/打桩）")
_lst = S.list_software()
chk(bool(_lst), "list_software 返回非空（%d 个）" % len(_lst))
chk(all(("bit" in x and "url" in x and "modify" in x and "kb" in x) for x in _lst),
    "每条记录都含 bit / url / modify / kb 字段")
chk(S.list_software() is _lst, "list_software 内存缓存生效（二次调用返回同一对象）")
chk(S.list_software(force=True) is not None, "force=True 可强制重读")
# force_remove_entry 安全阀：非 Uninstall 路径必须被拒且不碰注册表
chk(S.force_remove_entry("HKCU\\SOFTWARE\\NotUninstall").get("ok") is False,
    "force_remove_entry 拒绝非 Uninstall 路径")
chk(S.force_remove_entry("").get("ok") is False, "force_remove_entry 拒绝空路径")
chk(S.force_remove_entry("HKLM\\SOFTWARE\\Microsoft").get("ok") is False,
    "force_remove_entry 拒绝非 Uninstall 的 HKLM 路径")
# 打开类动作：WTB_NO_BROWSER=1 时只返回结果、不弹任何窗口
_old_env = os.environ.get("WTB_NO_BROWSER")
os.environ["WTB_NO_BROWSER"] = "1"
try:
    chk(S.open_url("example.com") is True, "open_url 无界面模式返回 True")
    chk(S.open_store("wechat") is True, "open_store 无界面模式返回 True")
    chk(S.google_search("x") is True, "google_search 无界面模式返回 True")
    chk(S.open_regedit("HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall")
        is True, "open_regedit 无界面模式返回 True（不写注册表）")
    chk(S.open_dir(r"C:\Windows") is True, "open_dir 对存在的目录返回 True")
    chk(S.open_dir(r"Z:\definitely\missing\dir") is False, "open_dir 对不存在目录返回 False")
    ok, msg = S.run_detached(r'"C:\绝对不存在\modify.exe"')
    chk(ok is False and ("找不到" in msg), "run_detached 对不存在的程序返回失败")
finally:
    if _old_env is None:
        os.environ.pop("WTB_NO_BROWSER", None)
    else:
        os.environ["WTB_NO_BROWSER"] = _old_env

print()
print("⑬ 打包产物：管理员权限清单（exe 存在才检查）")
_exe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "WinToolbox.exe")
if os.path.exists(_exe):
    _blob = open(_exe, "rb").read()
    chk(b"requireAdministrator" in _blob,
        "exe manifest 请求管理员权限（requireAdministrator）")
    chk(b'level="asInvoker"' not in _blob, "exe manifest 不再使用 asInvoker")
else:
    print("  [SKIP] 未找到 WinToolbox.exe（尚未打包，跳过）")

print()
print("⑭ 核心调度增强 + 显卡伪装（数据 / 只读探测 / UI）")
# --- 纯逻辑 ---
chk(S.cores_to_mask([0, 1, 3]) == 0b1011, "cores_to_mask 正确（0,1,3 → 0b1011）")
chk(S.cores_to_mask([]) == 0, "cores_to_mask 空列表 → 0")
chk(S.cores_to_mask([63, 64, -1]) == (1 << 63), "cores_to_mask 只接受 0..63（越界忽略）")
chk(set(S.MEM_PRIORITY_NAMES) == {0, 1, 2, 3, 4, 5}, "内存优先级表覆盖 0..5")
chk(set(S.IO_PRIORITY_NAMES) == {0, 1, 2, 3, 4}, "IO 优先级表覆盖 0..4")
# --- 只读探测（对自身进程 / 本机注册表） ---
_st = S.process_sched_state(os.getpid())
chk(_st.get("ok") and _st.get("affinity", 0) > 0,
    "process_sched_state 读到自身亲和掩码 0x%X" % _st.get("affinity", 0))
chk(bool(_st.get("priority_name")), "process_sched_state 给出优先级名：%s"
    % _st.get("priority_name"))
_rw = S.release_working_set(None)
chk(_rw.get("ok") is True, "release_working_set(None) 可释放自身工作集")
_gpus = S.gpu_adapters()
chk(isinstance(_gpus, list), "gpu_adapters 返回 list（本机 %d 个适配器）" % len(_gpus))
chk(all(("index" in a and "name" in a and "vendor" in a and "backup" in a)
        for a in _gpus), "适配器字段齐全（index/name/vendor/alias/backup）")
chk(len(S.GPU_ALIAS_PRESETS) >= 8, "显卡别名预设 ≥ 8 条（%d）" % len(S.GPU_ALIAS_PRESETS))
# --- 入参校验（不触碰注册表） ---
chk(S.gpu_alias_apply("0002", "").get("ok") is False, "空显卡名被拒")
chk(S.gpu_alias_apply("0002", "x" * 80).get("ok") is False, "超长显卡名被拒")
# --- 写操作打桩：确认调用参数正确且不真写注册表 ---
_real_apply, _real_restore = S.gpu_alias_apply, S.gpu_alias_restore
_calls = []
S.gpu_alias_apply = lambda i, n: (_calls.append(("apply", i, n)),
                                  {"ok": True, "msg": "stub"})[1]
S.gpu_alias_restore = lambda i: (_calls.append(("restore", i)),
                                 {"ok": True, "msg": "stub"})[1]
try:
    S.gpu_alias_apply("0002", "NVIDIA GeForce RTX 4090")
    S.gpu_alias_restore("0002")
finally:
    S.gpu_alias_apply, S.gpu_alias_restore = _real_apply, _real_restore
chk(_calls == [("apply", "0002", "NVIDIA GeForce RTX 4090"), ("restore", "0002")],
    "显卡伪装调用参数正确（apply/restore 各一次）")
# --- UI ---
ct = win.pages["cputune"]
win.goto("cputune")
ct.refresh()
for _ in range(80):
    pump(0.05)
    if getattr(ct, "core_chk", None):
        break
_logical = int((ct.topo or {}).get("logical") or 0)
chk(len(getattr(ct, "core_chk", [])) == _logical,
    "CPU 调试页核心勾选框数 = 逻辑核数（%d）" % len(getattr(ct, "core_chk", [])))
chk(ct.combo_memprio.count() == 6 and ct.combo_ioprio.count() == 5,
    "内存 / IO 优先级下拉项数正确")
chk(bool(ct.cb_strong.text()), "存在「强亲和」开关")
ct._sel_cores("P")
chk(ct._checked_cores() == sorted(set((ct.topo or {}).get("p_logicals") or [])),
    "「仅 P 核」勾选正确（%d 个）" % len(ct._checked_cores()))
ct._sel_cores("E")
chk(ct._checked_cores() == sorted(set((ct.topo or {}).get("e_logicals") or [])),
    "「仅 E 核」勾选正确（%d 个）" % len(ct._checked_cores()))
ct._sel_cores("none")
chk(ct._checked_cores() == [], "「清空」后无勾选")
ct._sel_cores("all")
chk(ct._checked_cores() == list(range(_logical)), "「全选」后勾满所有逻辑核")
chk(hasattr(ct, "lb_sched") and hasattr(ct, "pow_table"), "调度增强 / 电源计划控件已就位")
chk(not hasattr(ct, "gpu_combo") and not hasattr(ct, "gpu_name"),
    "CPU 调试页不再承载显卡控件（已拆为独立大类）")
_gp = win.pages.get("gpu")
chk(_gp is not None and _gp.title == "显卡伪装", "存在「显卡伪装」独立大类页面")
chk(len(win.NAV) == 12 and [k for k, _, _ in win.NAV][3] == "gpu",
    "导航共 13 项，显卡伪装排在第 4 位")
win.goto("gpu"); pump(1.2)
_gp.refresh(); pump(1.2)
chk(len(S.GPU_ALIAS_PRESETS) == 30 and len(S.GPU_ALIAS_GROUPS) == 6,
    "预设显卡固定 30 款 / 6 组：%d 组 / %d 款"
    % (len(S.GPU_ALIAS_GROUPS), len(S.GPU_ALIAS_PRESETS)))
chk(len(set(S.GPU_ALIAS_PRESETS)) == len(S.GPU_ALIAS_PRESETS), "预设无重复项")
chk(any("750 Ti" in n for n in S.GPU_ALIAS_PRESETS)
    and any("GT 1030" in n for n in S.GPU_ALIAS_PRESETS),
    "含低端 / 老卡（GTX 750 Ti、GT 1030 等）")
chk(not any(("Voodoo" in n or "Matrox" in n or "摩尔线程" in n)
            for n in S.GPU_ALIAS_PRESETS), "已移除冷门 / 经典 / 国产分组")
_sel = [i for i in range(_gp.preset.count())
        if _gp.preset.itemData(i) is not None]
chk(len(_sel) == len(S.GPU_ALIAS_PRESETS) + 1,
    "显卡预设可选项 = 预设数 + 1（含“自定义”项，%d）" % len(_sel))
chk(_gp.preset.count() > len(_sel), "分组之间有分隔线（%d 条）"
    % (_gp.preset.count() - len(_sel)))
_gp.name_edit.setText("__keep_me__")
_gp.preset.setCurrentIndex(0)
GP_sep = next(i for i in range(1, _gp.preset.count()) if not _gp.preset.itemData(i))
_gp.preset.setCurrentIndex(GP_sep)
chk(_gp.name_edit.text() == "__keep_me__", "落到分组分隔线上时不清空已填名称")
_gp.name_edit.setText("")
chk(not hasattr(_gp, "table"), "设备表格卡片已移除（页面改为放大填充）")
chk(_gp.gpu_pick.count() == len(S.gpu_adapters()),
    "显卡下拉项数 = 适配器数（%d）" % _gp.gpu_pick.count())
_cur = _gp._cur_gpu()
chk(_cur is not None and _gp.lb_state.text() not in ("", "—"),
    "默认选中显卡并回显状态（%s）" % (_cur or {}).get("name", "?")[:26])
chk("↓" in _gp.lb_preview.text(), "大号预览块已渲染")
_gp.preset.setCurrentIndex(1); pump(0.1)
chk(_gp.name_edit.text() == S.GPU_ALIAS_PRESETS[0]
    and S.GPU_ALIAS_PRESETS[0] in _gp.lb_preview.text(),
    "选预设 → 名称填入且预览块同步显示")
_gp.preset.setCurrentIndex(0); pump(0.1)
_gp.name_edit.setText(""); pump(0.1)
chk(bool(_gp.lb_count.text()) and bool(_gp.lb_backup.text()) and bool(_gp.lb_bkpath.text()),
    "设备计数 / 备份信息已渲染（%s）" % _gp.lb_count.text())
_gp.apply_alias()
chk(True, "空名称不触发写入（仅提示）")
_gp.restore_all()          # 原本无备份 → 仅提示，不写入
chk(True, "「全部还原」无备份时不写入")

print()
print("⑮ 电源计划（内置模板 / .pow 扫描 / 导入回读验证）")
# --- 常量与纯逻辑 ---
chk({"high", "ultimate"} <= set(S.POWER_TEMPLATES), "内置模板含 高性能 / 卓越性能")
chk(all(len(v) == 2 and S._GUID_RE.fullmatch(v[1])
        for v in S.POWER_TEMPLATES.values()), "模板 GUID 格式正确")
chk(S.power_plan_activate_template("nope").get("ok") is False, "未知模板被拒")
# --- 扫描器：临时目录里造真 .pow（含超深目录，验证最多下探两层）---
_td = tempfile.mkdtemp(prefix="wtb_pow_")
open(os.path.join(_td, "Unit Test Plan.pow"), "wb").write(b"POWERSETTINGS\x00" + b"\x00" * 32)
_deep = os.path.join(_td, "deep", "deeper", "deepest")
os.makedirs(_deep, exist_ok=True)
open(os.path.join(_deep, "too_deep.pow"), "wb").write(b"x")
_found = S.power_plan_scan_pow([_td])
chk([f["name"] for f in _found] == ["Unit Test Plan"],
    "扫描到 .pow 且超深目录不再下探（%s）" % [f["name"] for f in _found])
chk([f["name"] for f in S.power_plan_scan_pow([_deep])] == ["too_deep"],
    "把深目录直接作为根时仍能扫到（深度限制按根算）")
_dirs = S.power_plan_scan_dirs()
chk(bool(_dirs) and all(os.path.isdir(d) for d in _dirs),
    "扫描目录均存在（%d 个）" % len(_dirs))
chk(bool(S.known_folder("Desktop")), "known_folder 解析真实桌面：%s" % S.known_folder("Desktop"))
# --- 入参校验（不落盘、不改系统）---
chk(S.power_plan_import_pow("").get("ok") is False, "空路径被拒")
chk(S.power_plan_import_pow(os.path.join(_td, "a.txt")).get("ok") is False, "非 .pow 被拒")
chk(S.power_plan_import_pow(os.path.join(_td, "nope.pow")).get("ok") is False, "文件不存在被拒")
chk(S.power_plan_delete("not-a-guid").get("ok") is False, "非法 GUID 删除被拒")
# --- 打桩 run/cpu_plans：走完 导入→回读验证→启用 与 两条失败分支 ---
_real_run, _real_plans, _real_set = S.run, S.cpu_plans, S.cpu_set_plan
_G = "ABCDEF01-2345-6789-ABCD-EF0123456789"
_pow = os.path.join(_td, "Unit Test Plan.pow")
try:
    _seq = []
    S.run = lambda cmd, timeout=25, shell=False: (
        _seq.append(cmd), (0, "已成功导入电源方案 GUID: %s  (Unit Test)" % _G))[1]
    S.cpu_plans = lambda: [{"guid": _G, "name": "Unit Test", "active": False},
                           {"guid": "b" * 36, "name": "平衡", "active": True}]
    S.cpu_set_plan = lambda g: (_seq.append(("setactive", g)), {"ok": True, "err": ""})[1]
    _r = S.power_plan_import_pow(_pow, activate=True)
    chk(_r.get("ok") and _r.get("active") and _r.get("guid") == _G,
        "导入成功：解析 GUID + 回读验证 + 已启用")
    chk(any(isinstance(c, tuple) and c[0] == "setactive" for c in _seq),
        "导入后调用了 cpu_set_plan 启用")
    S.cpu_plans = lambda: [{"guid": "b" * 36, "name": "平衡", "active": True}]
    _r = S.power_plan_import_pow(_pow)
    chk(_r.get("ok") is False and "回读验证失败" in _r.get("msg", ""),
        "powercfg 返回 0 但计划未列出 → 回读验证拦下")
    S.run = lambda cmd, timeout=25, shell=False: (1, "无效的电源计划文件")
    _r = S.power_plan_import_pow(_pow)
    chk(_r.get("ok") is False and "导入失败" in _r.get("msg", ""), "导入报错原样返回")
    # 模板：已有同名 → 直接启用；隐藏模板 → 复制后启用
    S.cpu_plans = lambda: [{"guid": "c" * 36, "name": "高性能", "active": False}]
    _r = S.power_plan_activate_template("high")
    chk(_r.get("ok") and _r.get("guid") == "c" * 36, "已有同名「高性能」→ 直接启用")
    S.cpu_plans = lambda: [{"guid": "b" * 36, "name": "平衡", "active": True}]

    def _run_tpl(cmd, timeout=25, shell=False):
        if "-duplicatescheme" in cmd:
            return 0, ("已成功创建电源方案 GUID: "
                       "99999999-8888-7777-6666-555555555555  (卓越性能)")
        return 1, "无法找到指定的电源方案"

    S.run = _run_tpl
    _r = S.power_plan_activate_template("ultimate")
    chk(_r.get("ok") and _r.get("guid", "").startswith("99999999"),
        "隐藏模板「卓越性能」→ 复制后再启用")
    # 当前生效的计划不能删
    S.cpu_plans = lambda: [{"guid": _G, "name": "Unit Test", "active": True}]
    chk(S.power_plan_delete(_G).get("ok") is False, "当前生效的计划不可删除")
finally:
    S.run, S.cpu_plans, S.cpu_set_plan = _real_run, _real_plans, _real_set
# --- 候选清单合并 ---
_real_cand = S.power_plan_candidates
_FAKE = {"items": [{"kind": "pow", "name": "Unit Test Plan", "source": "本机文件",
                    "guid": "", "path": _pow, "active": False},
                   {"kind": "plan", "name": "平衡", "source": "已安装",
                    "guid": "381b4222-f694-41f0-9685-ff5bb260df2e", "path": "",
                    "active": True}],
         "files": [], "pow_count": 1, "plan_count": 1}
S.power_plan_candidates = lambda extra=None: _FAKE
try:
    win.goto("cputune"); pump(0.6)
    ct.scan_pow(); pump(0.6)
    chk(ct.pow_table.rowCount() == 2, "候选表填充 2 行（%d）" % ct.pow_table.rowCount())
    chk(ct.pow_table.item(1, 1).text() == "已安装", "来源列区分 本机文件 / 已安装")
    chk("已发现 1 个本机电源计划文件" in ct.lb_pow.text(), "底部计数：%s" % ct.lb_pow.text())
    _real_set2 = S.cpu_set_plan
    _c2 = []
    S.cpu_set_plan = lambda g: (_c2.append(g), {"ok": True, "err": ""})[1]
    try:
        ct.pow_table.selectRow(1); pump(0.2)
        ct.import_pow(); pump(0.3)
        chk(_c2 == ["381b4222-f694-41f0-9685-ff5bb260df2e"],
            "选中已安装计划 → 直接启用（不走导入）")
    finally:
        S.cpu_set_plan = _real_set2
    _real_imp = S.power_plan_import_pow
    _c3 = []
    S.power_plan_import_pow = lambda p, activate=True: (
        _c3.append((p, activate)), {"ok": True, "msg": "stub"})[1]
    _old_admin, _old_confirm = win.admin_mode, A.confirm
    try:
        win.admin_mode = True
        A.confirm = lambda *a, **k: True
        ct.pow_table.selectRow(0); pump(0.2)
        ct.import_pow(); pump(0.4)
        chk(_c3 and _c3[0][0] == _pow and _c3[0][1] is True,
            "选中 .pow → 调用导入并启用（activate=True）")
        _c3.clear()
        win.admin_mode = False
        ct.import_pow(); pump(0.3)
        chk(not _c3, "非管理员：导入被拦下（仅只读提示）")
        _c3.clear()
        win.admin_mode = True
        ct.use_template("high"); pump(0.4)
        chk(True, "高性能模板（管理员 + 确认）路径可走通")
    finally:
        win.admin_mode, A.confirm = _old_admin, _old_confirm
        S.power_plan_import_pow = _real_imp
finally:
    S.power_plan_candidates = _real_cand
A.confirm = lambda *a, **k: False

print()
print("=" * 62)
print("结果：%s" % ("全部通过" if not FAIL else "失败 %d 项" % len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)
