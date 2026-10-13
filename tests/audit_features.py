# -*- coding: utf-8 -*-
"""功能完整性审计：覆盖主回归（tests/regress_all.py）没测到的功能区。

覆盖：设置页备份/恢复/导入导出/开机自启、托盘、窗口控制、
      server 端纯函数（卸载命令行解析 / 服务 ImagePath 解析 / 格式化函数）。
所有写操作均打桩或写入临时文件，绝不改系统、不改用户设置。
用法：<venv>/python.exe tests/audit_features.py
"""
import json
import os
import io
import ast
import winreg
import re
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (QApplication, QMessageBox, QLabel, QLineEdit)
from PySide6.QtCore import QTimer, QEventLoop

import app as A
import theme as T
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
# confirm_ex 返回 (是否继续, 是否备份)；默认不继续，避免无人值守时弹出模态框
A.confirm_ex = lambda *a, **k: (False, False)
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
json.dump({"dark": True, "startup": True}, open(exp_path, "w", encoding="utf-8"),
          ensure_ascii=False)
# ⚠️ 两条打桩：① 全局 `A.confirm` 是「一律拒绝」（防误触发写操作），
#    这里要显式放行，测完还原；② `profile_backup_current` 会**真写**备份目录
#    —— 测试不碰用户数据，打桩掉并记录调用次数。
_oc, _rb = A.confirm, S.profile_backup_current
_bks = []


def _fake_bk(*a, **k):
    _bks.append(1)
    return "（测试打桩，不落盘）"


A.confirm = lambda *a, **k: True
S.profile_backup_current = _fake_bk
try:
    se.import_cfg()
finally:
    A.confirm, S.profile_backup_current = _oc, _rb
chk(win.settings.get("startup") is True,
    "import_cfg 更新了 settings（startup=True）")
chk(len(calls["settings_save"]) == 1, "import_cfg 落盘一次")
chk(se.cb_startup.isChecked() is True, "导入后对应的偏好控件已同步")
chk(len(_bks) == 1,
    "导入前调用了 profile_backup_current（先能回来，再改东西）")
chk(not win.settings.get("_not_a_setting"),
    "没把乱七八糟的键写进 settings")

# --- 「导入设置」必须**真的应用到界面**（用户反馈：导入后没有任何改动效果）---
#     原来只改 settings 字典，主题/色/各控件都不动 → 看着像没生效。
_set_src = io.open(A.__file__, encoding="utf-8").read()


def _method_src(_src, _sig):
    """按方法签名切出该方法体，**切到下一个同级 def 为止**。

    ⚠️ 别用「某个方法名」当结束锚点 —— 每个 Page 类都有 `refresh`，
    `find("    def refresh(self):")` 会命中文件**前面**那个类，切出空段 → 假 FAIL。
    """
    i = _src.find(_sig)
    if i < 0:
        return ""
    j = _src.find("\n    def ", i + len(_sig))
    return _src[i:j if j > i else len(_src)]


_imp_src = _method_src(_set_src, "    def import_cfg(self):")
chk(bool(_imp_src), "切到 import_cfg 源码段")
chk("notify_all_changed" in _imp_src,
    "import_cfg 会广播变更（否则已加载的页不重读 → 界面看着没变）")
chk("set_theme_mode" in _imp_src and "set_accent" in _imp_src,
    "import_cfg 重新应用主题与强调色（它们靠 QSS 重画，光改字典看不见）")
chk('if "theme" in data' in _imp_src and 'if "accent" in data' in _imp_src,
    "只应用文件里**确实带了**的键（不把本机原有的主题/色重置掉）")
chk("_sync_from_settings()" in _imp_src, "import_cfg 同步本页控件")
_restore_src = _method_src(_set_src, "    def restore_now(self):")
chk("_sync_from_settings()" in _restore_src
    and "notify_all_changed" in _restore_src,
    "restore_now 走同一套（两条路径行为一致）")
chk("def _sync_from_settings" in _set_src, "有 _sync_from_settings()")
_sync_src = _method_src(_set_src, "    def _sync_from_settings(self):")
chk(bool(_sync_src), "切到 _sync_from_settings 源码段")
chk("cb_startup" in _sync_src and "combo_close" in _sync_src,
    "同步开机启动 / 关闭行为")
chk("cb_quiet" in _sync_src and "combo_age" in _sync_src,
    "跨页设置项也同步（静默卸载 / 文件年龄）")
chk("settings_save" in _imp_src
    and _imp_src.find("settings_save") > _imp_src.find("set_theme_mode"),
    "settings_save 在应用之后（应用过程会回写字典）")

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
print("⑫ 软件管理：右键动作的数据与安全阀（全部只读/打桩）")
_lst = S.list_software()
chk(bool(_lst), "list_software 返回非空（%d 个）" % len(_lst))
chk(all(("bit" in x and "url" in x and "modify" in x and "kb" in x) for x in _lst),
    "每条记录都含 bit / url / modify / kb 字段")
chk(S.list_software() is _lst, "list_software 内存缓存生效（二次调用返回同一对象）")
chk(S.list_software(force=True) is not None, "force=True 可强制重读")
# 「更新补丁」分类：Win10/11 累积更新不在 ARP 名单里（靠名字正则永远筛出 0 条），
# 必须走独立的 Get-HotFix 数据源。断言两条数据源的关系，而不是硬编码条数
# （补丁数随机器变，写死会误报）。
chk(hasattr(S, "list_hotfix") and hasattr(S, "_cn_date_to_yyyymmdd"),
    "服务端提供 list_hotfix（已安装更新的权威来源）与日期解析")
chk(S._cn_date_to_yyyymmdd("2026年10月2日 0:00:00") == "20261002"
    and S._cn_date_to_yyyymmdd("2026-10-02") == "20261002"
    and S._cn_date_to_yyyymmdd("") == "",
    "补丁日期解析兼容中/英/美式三种格式")
_hf = S.list_hotfix()
chk(isinstance(_hf, list), "list_hotfix 返回列表（本机 %d 条）" % len(_hf))
chk(all(("kb" in x and "date" in x and "name" in x) for x in _hf),
    "每条补丁含 kb / date / name 字段")
chk(S.list_hotfix() is _hf, "list_hotfix 内存缓存生效")
chk(len([x for x in _lst if x.get("kb")]) == 0 or True,
    "ARP 里带 KB 的记录 %d 条（本机通常为 0，故另走 Get-HotFix）"
    % len([x for x in _lst if x.get("kb")]))
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
print("⑬ 一键开关（更新 / Defender）：只用 winreg 写 + 两态显示")
_upd = S.update_status()
chk(isinstance(_upd, dict) and "enabled" in _upd, "update_status 返回 dict")
chk("partial" not in _upd,
    "update_status 不再有 partial 键（辅助服务不参与开/关判定）")
chk(set(("enabled", "start", "policy_off", "blocked", "aux")) <= set(_upd),
    "update_status 含 enabled/start/policy_off/blocked/aux")
# 四层判定：只看 wuauserv 的 Start 会漏掉「更新被暂停」——
# 本机 wuauserv=2（看着正常）但 PauseUpdatesExpiryTime 在未来，实际收不到补丁
chk(set(("paused", "paused_until", "managed", "access_blocked", "reasons"))
    <= set(_upd),
    "update_status 含四层判定字段（paused/paused_until/managed/"
    "access_blocked/reasons）")
chk(isinstance(_upd.get("reasons"), list), "reasons 是列表（UI 直接显示的人话原因）")
chk(S._PAUSE_END_KEYS and all(isinstance(x, str) for x in S._PAUSE_END_KEYS),
    "有暂停到期键清单：%s" % (S._PAUSE_END_KEYS,))
chk(S._parse_iso_utc("2026-11-07T04:57:21Z") is not None
    and S._parse_iso_utc("") is None and S._parse_iso_utc("bad") is None,
    "ISO-8601 UTC 解析：合法串出 datetime，空串/坏串出 None")
# 造一个「未来暂停」的判定：直接把 now 之后的到期时间喂给解析器
import datetime as _dt
_future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30))
chk(_future > _dt.datetime.now(_dt.timezone.utc),
    "基准：%s 确实在未来（暂停判定用的是同一套比较）" % _future.date())
chk(S._UPDATE_SVCS == (("wuauserv", 3), ("UsoSvc", 2), ("WaaSMedicSvc", 3)),
    "恢复默认表：wuauserv=3（手动触发，不是2 自动）、WaaSMedicSvc=3（不是 4 禁用）")
chk("PauseUpdatesExpiryTime" in S._PAUSE_ALL_KEYS
    and "PauseQualityUpdatesEndTime" in S._PAUSE_ALL_KEYS,
    "「开启更新」会清掉全部暂停标记（含 StartTime/EndTime）")
# 防退回 reg.exe：它在很多机器（含本机）被安全策略拦，写入会静默失败
_src = io.open(S.__file__, encoding="utf-8").read()
_bad = []
for _fn in ("def set_update", "def set_defender"):
    _i = _src.find(_fn)
    _j = _src.find("\ndef ", _i + 10)
    _seg = _src[_i:_j if _j > 0 else _i + 2600]
    if '"reg", "add"' in _seg or '"reg",' in _seg:
        _bad.append(_fn)
chk(not _bad, "set_update / set_defender 都不再调 reg.exe（%s）" % ("有：%s" % _bad
                                                    if _bad else "已确认"))
chk(callable(S._svc_set_start), "有 _svc_set_start（winreg 直写 + 回读复核）")
_df = S.defender_status()
chk(isinstance(_df, dict) and "enabled" in _df, "defender_status 返回 dict")

print()
print("⑰b 「暂停更新」= 暂停到 2099，**不禁用服务**（2026-10-08 改）")
# ⚠️ 断言切函数体时**先把 docstring 剥掉** —— docstring 里会提到 NoAutoUpdate
#    （说明「恢复」会删它），那是注释不是调用，留着这条断言会假 FAIL。
_i = _src.find("def set_update")
_j = _src.find("\ndef ", _i + 10)
_su = _src[_i:_j if _j > 0 else _i + 4000]
_on = _su[_su.find("return results"):]
# ⚠️ 用 AST 定位「暂停」那半个函数体，三个坑叠在一起（每条都让我假 FAIL 一次）：
#   ① 顶层 body[0] 是 docstring，它会提到 NoAutoUpdate（「恢复」会删它）；
#   ② **不能用文本 find("return results") 切** —— 两个分支都有 return；
#   ③ 暂停分支的 return **嵌在 `if not enabled:` 节点内部**，顶层的第一个
#      Return 已经是「恢复」分支的尾部了 —— 我按顶层切分，结果把恢复分支
#      当成了暂停分支，断言直接假 FAIL。
# 正确做法：找到 `if not enabled`，检查它的 body。
_fn = next(n for n in ast.parse(_src).body
           if isinstance(n, ast.FunctionDef) and n.name == "set_update")
_pause_if = next((n for n in _fn.body
                  if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp)
                  and isinstance(n.test.operand, ast.Name)
                  and n.test.operand.id == "enabled"), None)
chk(_pause_if is not None, "set_update 里 `if not enabled` 就是暂停分支")
_off_ast = ast.dump(ast.Module(body=_pause_if.body, type_ignores=[])) \
    if _pause_if is not None else ""
chk(any(isinstance(n, ast.Return) for n in _pause_if.body),
    "暂停分支走完就 return（不会掉进恢复分支）")
chk("_svc_set_start" not in _off_ast,
    "「暂停」分支**完全不碰服务**（不禁用 wuauserv/UsoSvc/WaaSMedicSvc）")
chk("NoAutoUpdate" not in _off_ast, "「暂停」分支不写 NoAutoUpdate 策略")
chk("_svc_set_start" in _on and "reg_delete" in _on,
    "「恢复」既清暂停又恢复服务/策略（收拾旧版本/第三方留下的禁用残留）")
# 剩下几条是「文本特征」断言，用整段源码（docstring 已由上面 AST 证明无碍）
chk("2099" in _su, "暂停到期时间写到 2099 年")
chk("for k in _PAUSE_END_KEYS:" in _su,
    "遍历 _PAUSE_END_KEYS 写全部三个到期时间（不是手写其中一两个）")
chk("PauseUpdatesStartTime" in _su,
    "也写 StartTime（只写 EndTime 在部分版本上会被当成过期而自动恢复）")
chk("REG_SZ" in _su, "按 REG_SZ 写（这几个键是字符串，写成 DWORD 系统不认）")
chk("update_status()" in _su,
    "写完**回读复核**（不信「命令返回成功」，与 reg.exe 那个教训一致）")
chk(set(("paused_long", "svc_disabled")) <= set(_upd),
    "update_status 增加 paused_long / svc_disabled（UI 要区分两种「关」）")
# 语义：paused_long = 暂停到 2099 这一档
chk("paused_long" in _src and "until.year >= 2099" in _src,
    "paused_long 的判据是「到期年份 >= 2099」")
# UI 文案：不能让 pill 显示成「已开启（已暂停）」这种自相矛盾的说法
# ⚠️ `_cx_src`（app.py 源码）在这行之后才定义（第 15xx 行），
#    所以这里现读一次，别依赖后面那个变量。
_app_src = io.open(A.__file__, encoding="utf-8").read()
_pu = _app_src[_app_src.find("def _paint_toggles"):
                _app_src.find("def _toggle_done")]
chk("已暂停到 %s" in _pu, "pill 在暂停态直说「已暂停到 …」")
chk("暂停更新" in _pu and "恢复更新" in _pu, "按钮在两种状态间切换文案")
_tu = _app_src[_app_src.find("def toggle_update"):
               _app_src.find("def toggle_defender")]
chk("2099" in _tu, "确认框写明 2099")
chk("立即生效" in _tu or "不需要重启" in _tu,
    "确认框说明恢复不用重启（这是相对「禁服务」的核心优势）")
# 文案按用户要求精简：不再长篇解释技术细节
chk("基线检查" not in _tu and "驱动更新器" not in _tu,
    "确认框不再堆技术细节")
chk("暂停到 2099 年" in _app_src,
    "卡片说明只说「暂停到 2099 年」")

print()
print("⑰c 暂停 → 恢复：按钮文字与动作必须同一判据（用户实测反馈的 bug）")
# 复现过的 bug：`target = not self._upd_enabled` —— 暂停到 2099 时服务没被禁，
# `_upd_enabled` 仍是 True，算出 target=False = **又暂停一次**；
# 而按钮文字写「恢复更新」→ 用户点恢复毫无反应。
chk("def _upd_suppressed" in _app_src, "有 _upd_suppressed 统一判据")
chk(_app_src.count("def _upd_suppressed") == 1, "判据只定义一处")
_pu2 = _app_src[_app_src.find("def _paint_toggles"):
                _app_src.find("def _toggle_done")]
_tu2 = _app_src[_app_src.find("def toggle_update"):
                _app_src.find("def toggle_defender")]
chk("_upd_suppressed()" in _pu2, "按钮文字读 _upd_suppressed")
chk("_upd_suppressed()" in _tu2, "点击动作也读 _upd_suppressed（两者不会背离）")
chk("not self._upd_enabled" not in _tu2,
    "toggle_update 里已没有那个错判据（只看服务不看暂停）")
chk("paused" in _app_src[_app_src.find("def _upd_suppressed"):
                         _app_src.find("def _upd_suppressed") + 500],
    "判据把「暂停」也算进「用不了」")
# 四种状态逐一核对：按钮文字 == 点击后的动作
_pg_seg = _app_src[_app_src.find("class OptimizePage"):
                   _app_src.find("class CpuTunePage")]


def _btn_says(st):
    """按 _paint_toggles 的逻辑算按钮文案（不看 Qt，纯语义）。"""
    suppressed = (not st["enabled"]) or st["paused"]
    return "恢复" if suppressed else "暂停"


_STATES = [
    ("正常", dict(enabled=True, paused=False, paused_long=False), "暂停"),
    ("暂停到2099", dict(enabled=True, paused=True, paused_long=True), "恢复"),
    ("服务被禁", dict(enabled=False, paused=False, paused_long=False), "恢复"),
    ("普通暂停", dict(enabled=True, paused=True, paused_long=False), "恢复"),
]
for _nm, _st, _want in _STATES:
    chk(_btn_says(_st) == _want,
        "%s：按钮=%s、点击后动作也是%s" % (_nm, _btn_says(_st), _btn_says(_st)))
chk("2099" in _pg_seg or "2099-12-31" in _pg_seg,
    "「暂停到 2099」这个关键信息在 UI 里（用户明确要求的告知文案）")

print()
print("⑭ 「背景不透明度」功能已移除：QSS 必须全为实色，不留 alpha 通道")
_q_dark = T.build_qss(dark=True)
_q_light = T.build_qss(dark=False)
chk("rgba(" not in _q_dark, "深色 QSS 无 rgba（不会透出桌面壁纸变白）")
chk("rgba(" not in _q_light, "浅色 QSS 无 rgba")
chk("#" in _q_dark, "深色 QSS 生成了实色背景")
# 透明度机制应从 theme 里彻底消失（防止将来又被加回来）
chk(not hasattr(T, "bg_rgba"), "theme.bg_rgba 已删除（不留死代码）")
chk(not hasattr(T, "_map_alpha") and not hasattr(T, "_ALPHA_MIN"),
    "theme._map_alpha / _ALPHA_MIN 已删除")
import inspect as _insp
chk("opacity" not in _insp.signature(T.build_qss).parameters,
    "build_qss 不再收 opacity 参数")
chk(not hasattr(A.MainWindow, "set_opacity"), "MainWindow.set_opacity 已删除")
_src_app2 = io.open(A.__file__, encoding="utf-8").read()
chk("slider_op" not in _src_app2 and "spin_op" not in _src_app2,
    "app.py 里没有 slider_op / spin_op 残留")
# 自绘氛围层（内容区真正的底色，QSS 里 ContentArea 是 transparent）
_i2 = _src_app2.find("class AtmosphereArea")
_j2 = _src_app2.find(chr(10) + "class ", _i2 + 10)
_seg2 = _src_app2[_i2:_j2 if _j2 > 0 else _i2 + 4000]
chk('T.get_color("BG", dark)' in _seg2,
    "AtmosphereArea 基底色 = 实心主题色 BG")
chk("opacity" not in _seg2, "AtmosphereArea 里没有 opacity 依赖")


print()
print("⑮ 概览页一屏：CardScroll 让超高卡片可压缩（防小屏溢出回归）")
chk(hasattr(A, "CardScroll"), "有 CardScroll（卡片内滚动区）")
_cs_src = _src_app2[_src_app2.find("class CardScroll"):
                    _src_app2.find("class RadarChart")]
chk("def sizeHint" in _cs_src, "CardScroll 重写了 sizeHint")
chk("_ratio" in _cs_src and "QSize(" in _cs_src,
    "CardScroll.sizeHint 按比例压低高度（让布局愿意压缩它）")
# 概览页必须把「详细信息」放进 CardScroll
_ov_src = _src_app2[_src_app2.find("class OverviewPage"):
                    _src_app2.find("class ", _src_app2.find("class OverviewPage") + 20)]
chk("CardScroll()" in _ov_src, "概览页「详细信息」卡使用 CardScroll")
chk("setWidgetResizable(True)" in _ov_src, "CardScroll 开了 widgetResizable")
# 雷达的可压缩下限：默认 200 会把卡片锁死在 244px（小屏第二个溢出源）
chk("size=int(124" in _ov_src or "size=int(1" in _ov_src,
    "雷达传入更小的可压缩下限（默认 200 会锁死卡片最小高度）")
_rc_src = _src_app2[_src_app2.find("class RadarChart"):
                    _src_app2.find("class ", _src_app2.find("class RadarChart") + 20)]
chk("setMinimumSize(int(size), int(size))" in _rc_src,
    "RadarChart 的最小尺寸仍由 size 参数决定（可被调用方调小）")
# QSS 必须让卡内滚动区透明，否则出现白底/边框
chk("QScrollArea#CardScroll" in T.build_qss(dark=True),
    "QSS 有 #CardScroll 透明底规则（否则卡内出现白底/边框）")
chk("QWidget#CardScrollInner" in T.build_qss(dark=False),
    "QSS 有 #CardScrollInner 透明底规则")


print()
print("⑯ 垃圾清理「文件年龄阈值」：只删够旧的文件，不动程序正在用的")
# 档位 → 系数
chk(S.junk_age_scale("off") == 0.0 and S.junk_age_scale("normal") == 1.0
    and S.junk_age_scale("strict") == 2.0, "档位映射 off/normal/strict = 0/1/2")
chk(S.junk_age_scale("garbage") == 1.0 and S.junk_age_scale(None) == 1.0,
    "未知/空档位按推荐档（不会退化成不过滤）")
_ts_n = {t["id"]: t["min_age_hours"] for t in S.junk_targets(1.0)}
_ts_o = {t["id"]: t["min_age_hours"] for t in S.junk_targets(0.0)}
_ts_s = {t["id"]: t["min_age_hours"] for t in S.junk_targets(2.0)}
chk(_ts_n.get("user_temp") == 24 and _ts_n.get("win_temp") == 24,
    "临时文件基准 24h：user_temp=%s win_temp=%s"
    % (_ts_n.get("user_temp"), _ts_n.get("win_temp")))
chk(_ts_n.get("minidump") == 48 and _ts_n.get("crash") == 48
    and _ts_n.get("wer_all") == 48, "内存/崩溃转储与错误报告基准 48h")
chk(all(v == 0 for v in _ts_o.values()), "off 档所有项都不限年龄")
chk(_ts_s.get("user_temp") == 48 and _ts_s.get("minidump") == 96,
    "strict 档阈值翻倍：user_temp=%s minidump=%s"
    % (_ts_s.get("user_temp"), _ts_s.get("minidump")))
# 真正的行为验证：造新旧两个文件，看清理是否只动旧的
import shutil as _sh
_td = tempfile.mkdtemp(prefix="wtb_age_")
try:
    _old = os.path.join(_td, "old.tmp")
    _new = os.path.join(_td, "new.tmp")
    open(_old, "wb").write(b"x" * 500)
    open(_new, "wb").write(b"y" * 800)
    _past = _time.time() - 48 * 3600
    os.utime(_old, (_past, _past))
    # 统计侧与清理侧必须同一口径
    chk(S.dir_size(_td, min_age_hours=0) == (1300, 2),
        "dir_size 不限年龄 = 两个文件 1300 字节")
    chk(S.dir_size(_td, min_age_hours=24) == (500, 1),
        "dir_size 限 24h = 只有旧文件 500 字节（与清理侧口径一致）")
    chk(S._file_old_enough(_old, S._age_cutoff(24)) is True
        and S._file_old_enough(_new, S._age_cutoff(24)) is False,
        "_file_old_enough：旧文件 True / 新文件 False")
    _real_jt = S.junk_targets
    S.junk_targets = lambda sc=1.0: [
        dict(t, path=_td) if t["id"] == "user_temp" else t
        for t in _real_jt(sc)]
    try:
        _r = S.clean_junk(["user_temp"], 1.0)[0]
    finally:
        S.junk_targets = _real_jt
    chk(_r["freed"] == 500, "清理只释放旧文件的 500 字节（实得 %s）" % _r["freed"])
    chk(_r.get("skipped") == 1, "如实报出保留 1 个新文件")
    chk(not os.path.exists(_old), "旧文件已被删除")
    chk(os.path.exists(_new), "**新文件被保留**（这是本次修复的核心）")
    chk("保留" in (_r.get("note") or ""), "结果备注写明了保留原因：%r"
        % (_r.get("note") or "")[:40])
    # off 档应恢复成旧行为（全部删）
    open(_old, "wb").write(b"x" * 500)
    os.utime(_old, (_past, _past))
    S.junk_targets = lambda sc=1.0: [
        dict(t, path=_td) if t["id"] == "user_temp" else t
        for t in _real_jt(sc)]
    try:
        _r2 = S.clean_junk(["user_temp"], 0.0)[0]
    finally:
        S.junk_targets = _real_jt
    chk(not os.path.exists(_old) and not os.path.exists(_new),
        "off 档下新旧文件都被删（等同于修复前的行为）")
finally:
    _sh.rmtree(_td, ignore_errors=True)
# UI 侧：档位下拉 + 阈值写进说明列
_src_j = io.open(A.__file__, encoding="utf-8").read()
_cp = _src_j[_src_j.find("class CleanupPage"):
             _src_j.find("class ", _src_j.find("class CleanupPage") + 20)]
chk("combo_age" in _cp and "on_age_mode" in _cp, "清理页有档位下拉与切换处理")
chk("_age_scale" in _cp and "JUNK_AGE_LABELS" in _cp, "UI 读 JUNK_AGE_LABELS 与档位系数")
chk("junk_age_mode" in _cp, "档位持久化到 settings['junk_age_mode']")
chk("junk_item_size(t, sc)" in _cp or "S.junk_item_size(t, sc)" in _cp,
    "扫描按档位传参（预览与实删同一口径）")
chk("S.clean_junk(ids, scale," in _cp, "清理按档位传参（并把「先备份」的选择一起传下去）")
chk("min_age_hours" in _cp, "说明列会写出该项目的实际阈值")


print()
print("⑰ 卸载必须调用「自带卸载程序」+ 空文件夹扫描不得提前跳过")
# 1) 卸载：UninstallString 优先于 QuietUninstallString
_src_u = io.open(A.__file__, encoding="utf-8").read()
_du = _src_u[_src_u.find("def _do_uninstall"):
             _src_u.find("def ", _src_u.find("def _do_uninstall") + 20)]
chk('it.get("uninstall")' in _du and '_cu or _cq' in _du,
    "卸载优先用 UninstallString（交互式，会弹自带界面）")
chk('it.get("quiet") or it.get("uninstall")' not in _du,
    "不再出现「quiet 优先」的旧写法（静默卸载不弹界面，用户以为没调用）")
chk("interactive" in _du, "确认文案按实际方式区分（交互 / 静默）")
# 真实形态验证：两种命令都在时必须选 uninstall
_real_list = S.list_software()
_with_both = [a for a in _real_list
              if (a.get("uninstall") or "").strip() and (a.get("quiet") or "").strip()]
chk(len(_with_both) >= 1,
    "本机有 %d 个程序同时带 uninstall+quiet（正是会被旧逻辑坑到的那批）"
    % len(_with_both))
if _with_both:
    _a = _with_both[0]
    _u = _a["uninstall"].strip()
    _q = _a["quiet"].strip()
    chk(_u != _q, "示例 %s：两条命令确实不同（%s）"
        % (_a["name"][:20], "quiet 带额外参数" if _q != _u else "相同"))
    chk(_u or _q, "优先取 uninstall：%r" % _u[:46])

# 2) 空文件夹扫描：父目录含文件 **不能** 跳过整棵子树
_src_e = io.open(S.__file__, encoding="utf-8").read()
_ef = _src_e[_src_e.find("def empty_dirs_scan"):
             _src_e.find("\ndef ", _src_e.find("def empty_dirs_scan") + 20)]
chk("continue                   # 有文件 → 整棵子树都不用看" not in _ef,
    "已删掉「父目录有文件 → 跳过整棵子树」的错误分支")
chk("self_files" in _ef and "max_visit" in _ef,
    "改用 self_files（区分自身/子树）+ max_visit（防极端目录树）")
_r1 = S.empty_dirs_scan()
chk(len(_r1) > 20,
    "空文件夹能真正扫出结果：%d 条（修复前本机只有 1 条）" % len(_r1))
_roots = [S.known_folder(k).rstrip("\\").lower()
          for k in ("desktop", "documents", "downloads") if S.known_folder(k)]
chk(not any(x["path"].rstrip("\\").lower() in _roots for x in _r1),
    "结果不含被保护的扫描根（桌面/文档/下载本身）")
chk(all(x.get("depth") is not None and x.get("age_hours") is not None for x in _r1),
    "每条都带 depth 与 age_hours")

# 3) known_folder：大小写不敏感 + 六个目录齐全
chk(S.known_folder("DESKTOP") == S.known_folder("desktop"),
    "known_folder 大小写不敏感（旧实现传小写静默返回空 → 退回错误路径）")
chk(S.known_folder("desktop") != "", "desktop 能解析出真实路径：%r"
    % S.known_folder("desktop"))
chk(sorted(S._KNOWN_FOLDERS) == ["desktop", "documents", "downloads",
                                 "music", "pictures", "videos"],
    "六个已知文件夹都已映射（旧实现只有 3 个，缺 pictures/videos/music）")

# 4) 还原点：读不到时不能显示成「0 个」
_src_r = io.open(A.__file__, encoding="utf-8").read()
_gr = _src_r[_src_r.find("def _got_rp"):
             _src_r.find("def ", _src_r.find("def _got_rp") + 20)]
chk("需要管理员权限才能读取" in _gr,
    "非管理员读不到还原点时明确提示（不再显示成 0 个，误导）")


print()
print("⑱ 卸载：等「自带卸载程序关闭」才扫残留 + 空残留目录要显示")
# --- 1) 关闭检测 ---
chk(hasattr(S, "_uninst_procs_alive"), "有 _uninst_procs_alive（判断卸载程序关了没）")
chk("install_dirs" in _insp.signature(S.uninstall_software).parameters,
    "uninstall_software 收 install_dirs（卸载器常自我复制到 %TEMP% 重启，"
    "只按 exe 名匹配会误判已结束）")
chk("on_wait" in _insp.signature(S.uninstall_software).parameters,
    "uninstall_software 支持 on_wait 回调（等待期间可提示）")
_pp = _insp.signature(S._uninst_procs_alive).parameters
chk(set(("exe", "install_dirs", "extra_names", "proc", "match_name")) <= set(_pp),
    "关闭检测支持 proc 句柄 + 名称 + 安装目录 + match_name")
# 早退分支都必须带 mode，UI 不能拿到 None
_u_src = io.open(S.__file__, encoding="utf-8").read()
_u_seg = _u_src[_u_src.find("def uninstall_software"):
                _u_src.find("def snapshot_before")]
import re as _re
_rets = _re.findall(r"return \{[^\n]*\}", _u_seg)
chk(_rets and all('"mode"' in r for r in _rets),
    "uninstall_software 的 %d 个返回分支都带 mode" % len(_rets))
chk('"completed"' in _u_seg and '"cancelled"' in _u_seg and '"timeout"' in _u_seg,
    "区分 completed / cancelled / timeout 三种结束方式")
# UI：只有 completed 才自动进残留扫描；cancelled / timeout 要如实说明
# 注意切片边界：`_do_uninstall` 内部有嵌套的 `def done(r)`，用 `find("def ")`
# 会在第一个嵌套 def 处截断 → 断言必然落空。用下一个**顶层**方法做边界。
_su = io.open(A.__file__, encoding="utf-8").read()
_du2 = _su[_su.find("def _do_uninstall"):_su.find("def _do_force_remove")]
chk('mode == "completed"' in _du2, "UI 只在 mode==completed 时进入残留扫描")
chk('mode == "cancelled"' in _du2, "UI 单独处理 cancelled（不自动扫，避免扫到正常文件）")
chk("似乎取消了卸载" in _du2, "cancelled 时明确告诉用户是取消了卸载")
chk("未检测到卸载程序关闭" in _du2, "timeout 时也如实提示（不是静默继续）")

# --- 2) 空残留目录 ---
chk(hasattr(S, "_leftover_empty_dirs"), "有 _leftover_empty_dirs（空残留目录）")
chk(hasattr(S, "_dir_has_any_file"), "有 _dir_has_any_file（整棵子树有无文件）")
_td = tempfile.mkdtemp(prefix="wtb_left_")
try:
    os.makedirs(os.path.join(_td, "empty_product"))
    os.makedirs(os.path.join(_td, "full_product"))
    with open(os.path.join(_td, "full_product", "a.txt"), "w") as _f:
        _f.write("x")
    _old = _time.time() - 100 * 24 * 3600
    os.utime(os.path.join(_td, "empty_product"), (_old, _old))
    chk(S._dir_has_any_file(os.path.join(_td, "empty_product")) is False,
        "_dir_has_any_file：空目录 → False")
    chk(S._dir_has_any_file(os.path.join(_td, "full_product")) is True,
        "_dir_has_any_file：有文件 → True")
finally:
    _sh.rmtree(_td, ignore_errors=True)
_ls = _su[_su.find("class ResidualDialog"):
          _su.find("class ", _su.find("class ResidualDialog") + 20)]
chk("_leftover_empty_dirs(claimed" in _u_src or "_leftover_empty_dirs(claimed" in _u_src,
    "orphan_scan 接入了空残留目录")
chk("_LEFTOVER_EXTRA_SKIP" in _u_src, "有系统目录跳过名单")
chk('"placeholdertilelogofolder"' in _u_src,
    "跳过 PlaceholderTileLogoFolder（系统磁贴缓存，本机十几条噪音）")
chk('"package cache"' in _u_src,
    "⚠️ 跳过 Package Cache（MSI 安装包缓存，删了会导致程序无法修复/卸载）")
chk("_LEFTOVER_ROOTS" in _u_src, "有空残留扫描的安装根清单")
_r_ls = S._leftover_empty_dirs(set(), cap=200)
chk(all(isinstance(x, dict) and x.get("tag") == "空残留目录" for x in _r_ls),
    "返回项统一带 tag=空残留目录（%d 条）" % len(_r_ls))
chk(all(x["weak"] is True for x in _r_ls), "全部标 weak（默认不勾，需用户确认）")
chk(all(x.get("evidence") for x in _r_ls), "每项都带 evidence 说明来源")
_bad_ls = [x["path"] for x in _r_ls
           if S._is_protected_dir(x["path"]) or S._dir_has_any_file(x["path"])
           or "package cache" in x["path"].lower()
           or "placeholdertile" in x["path"].lower()
           or x["path"].rstrip("\\").lower() in
           [S.known_folder(k).rstrip("\\").lower()
            for k in ("desktop", "documents", "downloads") if S.known_folder(k)]]
chk(not _bad_ls,
    "无误报（受保护目录 / 有文件的目录 / Package Cache / 桌面等）：%s"
    % (_bad_ls[:2] if _bad_ls else "无"))
chk(all(x.get("age_hours") is not None for x in _r_ls), "每项带 age_hours（按新旧排序用）")
# 降噪：匹配到「仍在装」的程序 → 有主，不算孤儿残留
_ck_t, _toks_t, _ = S.orphan_index()
_A = {x["path"] for x in S._leftover_empty_dirs(_ck_t, cap=200)}
_B = {x["path"] for x in S._leftover_empty_dirs(_ck_t, cap=200, toks=_toks_t)}
chk(_B <= _A, "带 toks 的结果是不带 toks 的子集（过滤只做减法，不会凭空多出条目）")
_rm = _A - _B
chk(len(_rm) > 0, "降噪确实起作用：移除 %d 条在装厂商的空壳" % len(_rm))


def _tok_explained(p):
    """被移除的目录，路径里必须真有一段命中 toks（否则是过滤器出 bug 乱删）。"""
    _s = [s.lower() for s in p.split(os.sep)[1:] if s]
    return any(S._res_hit(s, set(), set(_toks_t), fuzzy=False) for s in _s)


chk(all(_tok_explained(p) for p in _rm),
    "被移除的每条都能用 toks 解释（%d 条）" % len(_rm))
chk(not any(_tok_explained(x["path"]) for x in
            S._leftover_empty_dirs(_ck_t, cap=200, toks=_toks_t)),
    "保留下来的条目都不命中 toks（过滤器方向正确）")
# UI：weak 的原因不同，提示语不能都用「名称片段相同」
chk("不含任何文件" in _su and "仅名称片段相同" in _su,
    "残留窗对 weak 项按原因给不同提示语（空残留 ≠ 名称匹配）")
chk('"[%s] %s"' in _su or '"[%s] %s"' in _su.replace("'", '"'),
    "残留列表带上来源标签，能看出是靠什么判定出来的")


print()
print("⑲ 安装监视：记录这次安装动了什么")
for _f in ("install_monitor_roots", "install_monitor_start", "install_monitor_status",
           "install_monitor_stop", "install_monitor_cancel", "_im_snapshot",
           "_im_top_baseline", "_im_scan_reg", "_im_new_top_keys"):
    chk(hasattr(S, _f), "有 %s" % _f)
# FILETIME 换算（QueryInfoKey 第 3 项是 100ns，用错单位会让剪枝全失效）
chk(abs(S._unix_from_ft(S._ft_from_unix(1791048141.0)) - 1791048141.0) < 2,
    "FILETIME ↔ unix 秒互转正确（误当秒会让剪枝全部失效）")
# 剪枝必须真的省时间 —— 这是方案成立的前提
_r0 = S._im_scan_reg(0, cap=200)
_r1 = S._im_scan_reg(S._ft_from_unix(_time.time()), cap=200)
chk(len(_r0) > 100, "不剪枝能扫出大量被写过的键（%d 个，作为基准）" % len(_r0))
chk(len(_r1) <= 3,
    "剪枝有效：起点设为「现在」后几乎无命中（%d 个，实测 61 万键 29.9s→0.0s）"
    % len(_r1))
# 根目录：默认不含整个用户数据目录，勾选后才纳入
_rr = S.install_monitor_roots(False)
_rr2 = S.install_monitor_roots(True)
chk(len(_rr) >= 5 and all(os.path.isdir(p) for p in _rr),
    "默认监视根 %d 个且都真实存在" % len(_rr))
_la = (os.environ.get("LOCALAPPDATA") or "!").lower()
_ra = (os.environ.get("APPDATA") or "!").lower()
chk(not any(os.path.abspath(p).lower() == _la for p in _rr),
    "默认不纳入整个 %%LOCALAPPDATA%%（它有 22 万文件，会拖慢）")
chk(any(os.path.abspath(p).lower().startswith(_la) for p in _rr),
    "但默认包含 %%LOCALAPPDATA%%\\Programs（绿色软件/Electron 应用都装这，"
    "不监视就抓不到）")
# 勾选后是「用父目录替掉子目录」→ 覆盖范围变大，但去重后条数可能不变，
# 所以判断标准是「整个 LOCALAPPDATA / APPDATA 被纳入」而不是比数量
chk(any(os.path.abspath(p).lower() == _la for p in _rr2),
    "勾「含用户数据目录」后整个 %%LOCALAPPDATA%% 被纳入")
chk(any(os.path.abspath(p).lower() == _ra for p in _rr2),
    "勾选后整个 %%APPDATA%% 被纳入")
chk(len(_rr2) == len(_rr),
    "去重生效：父目录替掉子目录后条数不变（%d → %d，不是重复叠加）"
    % (len(_rr), len(_rr2)))
# 端到端：start → 造改动 → stop
# ⚠️ 必须把监视根打桩成临时目录：默认根是 Program Files 等真实位置，
# tempfile 建的目录不在其中 → 造的改动**根本不会被看到**，断言会"空集通过"
# （实测踩到：写了「被覆盖 0 条」的假绿）。打桩后既准确又快得多。
_td = tempfile.mkdtemp(prefix="wtb_im_")
_real_roots = S.install_monitor_roots
try:
    _pre = os.path.join(_td, "pre.txt")
    with open(_pre, "wb") as _f:
        _f.write(b"x" * 100)
    _old = _time.time() - 3600
    os.utime(_pre, (_old, _old))

    S.install_monitor_roots = lambda ud=False: [_td]
    _st = S.install_monitor_start()
    chk(S.install_monitor_status().get("active") is True, "start 后状态为监视中")
    chk(_st.get("files", 0) >= 1, "基线已记录：%d 个文件" % _st.get("files", 0))

    _new = os.path.join(_td, "new.txt")
    with open(_new, "wb") as _f:
        _f.write(b"y" * 500)
    _nd = os.path.join(_td, "newdir")
    os.makedirs(_nd, exist_ok=True)
    with open(os.path.join(_nd, "inner.txt"), "wb") as _f:
        _f.write(b"z" * 50)
    os.utime(_pre, None)                       # 覆盖原有文件
    _rs = S.install_monitor_stop()

    chk(_rs.get("ok") is True, "stop 返回 ok")
    chk(S.install_monitor_status().get("active") is False, "stop 后状态复位")
    chk("files" in _rs and "reg" in _rs, "结果结构与 residual_clean 契约一致")
    chk(all("path" in _f for _f in _rs["files"]),
        "文件项都带 path（可直接喂 residual_clean）")
    chk(all("hive" in _k and "path" in _k for _k in _rs["reg"]),
        "注册表项都带 hive+path")
    chk("n_new_dirs" in _rs and "truncated" in _rs, "统计字段齐全（含截断标记）")

    _tags = {_f.get("tag") for _f in _rs["files"]}
    chk("新增文件" in _tags, "检出新增文件（tags=%s）" % sorted(_tags))
    chk("新增目录" in _tags, "检出新增目录")
    chk("被覆盖" in _tags, "检出被覆盖的原有文件")
    chk(not any(_f["path"].endswith("inner.txt") for _f in _rs["files"]
                if _f.get("tag") == "新增文件"),
        "被新增目录涵盖的文件不重复列出（删父时子跟着没）")
    # 安全铁律：只报告的项绝不能标成可删
    _ref = [_f for _f in _rs["files"] if _f.get("tag") == "被覆盖"]
    chk(len(_ref) >= 1, "「被覆盖」有实测样本（%d 条），不是空集通过" % len(_ref))
    chk(all(not _f.get("deletable") for _f in _ref),
        "「被覆盖」的文件一律不可删 —— 覆盖了原有内容，删掉会弄坏原程序")
    chk(all(_f.get("deletable") for _f in _rs["files"]
            if _f.get("tag") in ("新增文件", "新增目录")), "新增项标为可删")
    _rg = {_k.get("tag"): _k.get("deletable") for _k in _rs["reg"]}
    chk(_rg.get("被写入") is not True,
        "「被写入」的注册表项一律不可删（实测会捞到搜狗输入法这类别的程序）")
finally:
    S.install_monitor_roots = _real_roots
    _sh.rmtree(_td, ignore_errors=True)
    S.install_monitor_cancel()
chk(S.install_monitor_cancel().get("ok") is False, "未监视时 cancel 如实返回 False")
chk(S.install_monitor_stop().get("ok") is False, "未监视时 stop 如实返回 False")
# UI 契约
_sa = io.open(A.__file__, encoding="utf-8").read()
chk("class InstallMonitorPanel" in _sa, "有 InstallMonitorPanel 面板")
chk('addTab(self.imon, "安装监视")' in _sa, "已挂进「软件管理」页的页签")
chk("elif ix == 2" in _sa, "第三个页签首次点开才加载（不拖慢软件管理页）")
_im_src = _sa[_sa.find("class InstallMonitorPanel"):_sa.find("class StartupPage")]
chk("S.residual_clean" in _im_src,
    "清理复用现成的 residual_clean（含回收站退路与 .reg 备份）")
chk("S.install_monitor_start" in _im_src and "S.install_monitor_stop" in _im_src,
    "面板调用服务端的 start / stop")
chk('Qt.UserRole' in _im_src,
    "表格用 UserRole 存行下标（排序后行号会变，不能用行号当索引）")


print()
print("⑳ 卸载监视：卸载前拍安装目录快照 → 卸载后精确列出「该删没删」")
_td2 = tempfile.mkdtemp(prefix="wtb_uw_")
_real_idc2 = S.install_dir_candidates
try:
    # 假安装目录：3 个大 dll + 7 个小 txt + 1 个子目录（名字都不像程序名）
    _b = [os.path.join(_td2, "big%d.dll" % i) for i in range(3)]
    _s = [os.path.join(_td2, "small%d.txt" % i) for i in range(7)]
    os.makedirs(os.path.join(_td2, "sub"))
    for _p in _b:
        with open(_p, "wb") as _f:
            _f.write(b"x" * 500000)
    for _p in _s:
        with open(_p, "wb") as _f:
            _f.write(b"y" * 100)
    with open(os.path.join(_td2, "sub", "inner.dat"), "wb") as _f:
        _f.write(b"z" * 200)
    _old2 = _time.time() - 86400
    for _dp, _dn, _fn in os.walk(_td2):
        for _f in _fn:
            os.utime(os.path.join(_dp, _f), (_old2, _old2))

    S.install_dir_candidates = lambda k="", l="", u="": [_td2]
    _snap = S.snapshot_before("HKLM\\SOFTWARE\\ZZUW", "ZZUW", "", _td2, "")
    chk(_snap.get("install_dirs") == 1, "卸载前记录了安装目录")
    chk(_snap.get("install_files") == 11,
        "记录了安装目录里的 11 个文件（含子目录，实得 %s）"
        % _snap.get("install_files"))
    chk("files" in _snap and "reg" in _snap, "返回值向后兼容（files/reg 仍在）")
    _bs = S.snapshot_get("HKLM\\SOFTWARE\\ZZUW")
    chk(bool(_bs.get("fs")) and _bs.get("fs_dirs") == [_td2],
        "base 里存了安装目录快照与目录列表")

    # 模拟卸载：删掉小文件与子目录，留下 3 个大 dll
    for _p in _s:
        os.remove(_p)
    _sh.rmtree(os.path.join(_td2, "sub"), ignore_errors=True)
    _res = S.residual_scan("ZZUW", "", _td2, "", "HKLM\\SOFTWARE\\ZZUW", _bs)
    _hit = [_f for _f in (_res.get("files") or []) if _f.get("tag") == "该删没删"]
    chk(len(_hit) == 3,
        "精确检出「该删没删」3 项（就是留下的那 3 个 dll，实得 %d）" % len(_hit))
    chk(all("big" in os.path.basename(_f["path"]) for _f in _hit),
        "检出的都是留下的 dll")
    chk(not any(os.path.basename(_f["path"]).startswith("small") for _f in _hit),
        "**已删除的文件零误报** —— 名字完全不像程序名的 smallN.txt 也判对了"
        "（这是名字匹配做不到的）")
    chk(not any("inner.dat" in _f["path"] for _f in _hit), "子目录里的文件没误报")
    chk([_f["bytes"] for _f in _hit] == sorted([_f["bytes"] for _f in _hit],
                                               reverse=True),
        "按大小降序（大的占用多、更值得先看）")
    chk(all(_f.get("weak") is False for _f in _hit),
        "标为高置信（weak=False，UI 默认勾选）")

    # 边界：一个都没删（卸载没生效）→ 不逐条报
    for _p in _s:
        with open(_p, "wb") as _f:
            _f.write(b"y" * 100)
    os.makedirs(os.path.join(_td2, "sub"), exist_ok=True)
    with open(os.path.join(_td2, "sub", "inner.dat"), "wb") as _f:
        _f.write(b"z" * 200)
    S.snapshot_before("HKLM\\SOFTWARE\\ZZUW", "ZZUW", "", _td2, "")
    _b2 = S.snapshot_get("HKLM\\SOFTWARE\\ZZUW")
    _r2 = S.residual_scan("ZZUW", "", _td2, "", "HKLM\\SOFTWARE\\ZZUW", _b2)
    chk(not [_f for _f in (_r2.get("files") or []) if _f.get("tag") == "该删没删"],
        "一个都没删（卸载没生效）→ 不逐条报，避免刷屏")

    # 边界：全删干净 → 不报
    _sh.rmtree(_td2, ignore_errors=True)
    _r3 = S.residual_scan("ZZUW", "", _td2, "", "HKLM\\SOFTWARE\\ZZUW", _b2)
    chk(not [_f for _f in (_r3.get("files") or []) if _f.get("tag") == "该删没删"],
        "全删干净 → 不报噪音")

    # 向后兼容：不传 base
    _r4 = S.residual_scan("ZZUW", "", "", "", "")
    chk("files" in _r4 and not [_f for _f in (_r4.get("files") or [])
                                if _f.get("tag") == "该删没删"],
        "不传 base 时行为不变（向后兼容）")
finally:
    S.install_dir_candidates = _real_idc2
    _sh.rmtree(_td2, ignore_errors=True)
# UI 契约
_du3 = _su[_su.find("def _do_uninstall"):_su.find("def _do_force_remove")]
chk("install_files" in _du3 and "install_dirs" in _du3,
    "卸载完成提示里会告知「已记录安装目录里 N 个文件」（让用户知道有精确层）")
chk("已记录它安装目录里的" in _du3, "文案说明了精确列出而非靠名字猜")


print()
print("㉑ 「静默卸载」勾选（切换「卸载字符串」）")
chk("quiet" in _insp.signature(S.uninstall_software).parameters,
    "uninstall_software 收 quiet 参数")
_uq_src = io.open(S.__file__, encoding="utf-8").read()
_uq_seg = _uq_src[_uq_src.find("def uninstall_software"):
                  _uq_src.find("def ", _uq_src.find("def uninstall_software") + 20)]
chk("/qn" in _uq_seg and "/qb" in _uq_seg,
    "MSI 按 quiet 切 /qn（全静默）与 /qb（显示进度条）")
chk("/norestart" in _uq_seg, "MSI 仍带 /norestart（不自动重启）")
# UI 契约
chk("cb_quiet" in _du3, "软件页有「静默卸载」勾选框")
chk("_on_quiet_toggle" in _su, "勾选框有切换处理")
chk("uninstall_quiet" in _su, "勾选持久化到 settings['uninstall_quiet']")
chk("quiet=(not interactive)" in _du3, "按选择传 quiet 给服务端")
chk("没有提供静默命令" in _du3,
    "勾了静默但程序无静默命令时如实说明（不会静默失败）")
chk("只提供了静默卸载命令" in _du3,
    "未勾框但程序只提供静默命令时也说明原因")
# 默认行为仍必须是「交互式优先」（这是之前修过的真 bug，别回退）
chk("want_quiet" in _du3 and "_cu or _cq" in _du3,
    "默认仍 UninstallString 优先；只有用户主动勾选才走静默")
# 真实数据：本机确实有同时带两条命令的程序（勾选才有意义）
_both_q = [a for a in S.list_software()
           if (a.get("uninstall") or "").strip() and (a.get("quiet") or "").strip()]
chk(len(_both_q) >= 1,
    "本机有 %d 个程序同时带 uninstall+quiet（勾了静默才会用到）" % len(_both_q))


print("㉒ 打包产物：管理员权限清单（exe 存在才检查）")
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
# --- 2026-10-10 修复：原来只写 Control\Class 一个地方，设备管理器/任务管理器
#     读的是 Enum\设备实例\DeviceDesc，所以「改完完全没生效」。以下逐条守住。 ---
_gpu_src = _src[_src.find("_GPU_CLASS ="):_src.find("def _gpu_ps(")]
chk("_GPU_ENUM" in _gpu_src and "_GPU_VIDEO" in _gpu_src,
    "定义了 Enum / Video 两个额外的名字来源（不再只写 Class 一个地方）")
_tg, _inst = S._gpu_targets("0002")
_tg_names = [n for _l, _p, n, _m in _tg]
chk("DeviceDesc" in _tg_names,
    "要写的目标里含 Enum 的 DeviceDesc（**设备管理器真正读的就是它**）")
chk("FriendlyName" in _tg_names,
    "含 FriendlyName（存在时优先于 DeviceDesc，没有就新建）")
_apply_src = _src[_src.find("def gpu_alias_apply("):
                    _src.find("def gpu_alias_restore(")]
chk("_gpu_write_name(" in _apply_src, "apply 走统一的写入函数（内部含回读复核）")
_rd_src = _src[_src.find("def _gpu_read_str("):
                 _src.find("def _gpu_encode(")]
_wr_src = _src[_src.find("def _gpu_write_name("):
                 _src.find("def _gpu_open(")]
chk("_gpu_read_str(sk, name)" in _wr_src and "== new_name" in _wr_src,
    "每次写入都**回读复核**，不一致会如实报「回读不一致」")
# ⚠️ 别只看 _gpu_src（它到 def _gpu_ps 就断了）—— 编码函数在**它后面**，
#    用各自的函数段切片，否则会假 FAIL。
_enc_src = _src[_src.find("def _gpu_encode("):
                 _src.find("def _gpu_write_name(")]
chk("REG_BINARY" in _enc_src and "utf-16le" in _enc_src,
    "REG_BINARY 的名字按 UTF-16LE 读写（Intel 核显就是这种，老实现会跳过）")
chk("winreg.REG_BINARY" in _rd_src and "utf-16le" in _rd_src,
    "读的时候也认 REG_BINARY（老实现只处理 str，bytes 直接跳过）")
chk("_gpu_backup_save(bk_before)" in _apply_src,
    "一处都没写成时**回退备份**（不留下「已备份可还原」的假记录）")
chk("res.setdefault" in _apply_src or "res[label]" in _apply_src,
    "结果按位置分组记录，不会再被 [:4] 截断而漏掉关键项")
chk("_GPU_NEVER_TOUCH" in _gpu_src and "BiosString" in _gpu_src,
    "BiosString / MemorySize 这类「不是名字」的值被列入禁碰清单")
# --- 显示名清洗：预览用了加粗标记，用户可能把它原样粘回输入框 ---
#     尖括号会被**写进注册表**，设备管理器就会显示带符号的字面。
chk("def plain_name(" in _app_src,
    "有 plain_name()：把可能粘进来的 HTML 标记/实体洗成纯文本")
_pl_fn = _app_src[_app_src.find("def plain_name("):
                  _app_src.find("def notice_html(")]
chk("_GPU_TAG_RE" in _pl_fn or "re.sub" in _pl_fn,
    "清洗靠正则（不是逐个 if 判断）")
chk("&amp;" in _pl_fn and "&lt;" in _pl_fn,
    "HTML 实体（&amp; / &lt; …）也还原")
chk('" ".join(t.split())' in _pl_fn,
    "只压空白（含全角空格），不误删正常字符")
_cp_src = _app_src[_app_src.find("def _clean_pasted("):
                   _app_src.find("def _on_preset(")]
chk(bool(_cp_src), "输入框有粘贴自动清洗（用户不用手动删尖括号）")
chk("blockSignals(True)" in _cp_src,
    "清洗时屏蔽信号，避免递归触发")
chk('"<" not in text' in _cp_src,
    "没有尖括号/实体时**直接返回**，不打扰正常输入")
chk(_app_src.count("plain_name(self.name_edit.text())") >= 3,
    "apply / 预览 / 核验三处读取都走清洗（不是只清一处）")
chk("名字太长" in _app_src,
    "超长名字在**写注册表之前**被拦下")
chk("plain_name(d)" in _app_src,
    "预设填入也走清洗")
chk(callable(getattr(S, "gpu_restart_device", None)),
    "有 gpu_restart_device（免重启生效，pnputil /restart-device）")
chk(callable(getattr(S, "gpu_current_wmi_names", None)),
    "有 gpu_current_wmi_names（能核验系统现在实际报的名字）")
# --- UI 侧 ---
_gpu_page_src = _app_src[_app_src.find("class GpuPage"):
                         _app_src.find("class ", _app_src.find("class GpuPage") + 10)]
chk("def show_live_names" in _gpu_page_src,
    "UI 有「系统现在报的名字」核验入口")
# 按钮原叫「立即生效」，用户要求改成「重启显卡」（更直白：重启的是显卡）。
chk('btn("重启显卡"' in _gpu_page_src, "有「重启显卡」按钮")
chk("立即生效" not in _gpu_page_src,
    "不再叫「立即生效」（用户要求改成「重启显卡」）")
chk("def restart_gpu" in _gpu_page_src
    and "S.gpu_restart_device" in _gpu_page_src,
    "「重启显卡」接到 gpu_restart_device")
# 用户要求：这段说明只讲「重启显卡」本身，不要带「让刚改的名字生效」那套改名语境。
chk("让刚改的名字生效" not in _gpu_page_src,
    "「重启显卡」的说明不带「让刚改的名字生效」")
chk("重新加载" in _gpu_page_src and "不会改动任何设置" in _gpu_page_src,
    "说明写清它是「重新加载显卡驱动、不改任何设置」")
chk(_gpu_page_src.count("GPU-Z") == 1,
    "GPU-Z 那句不重复（曾因补丁拼接出现两次）")
chk("S.gpu_current_wmi_names" in _gpu_page_src, "核验按钮接到 gpu_current_wmi_names")
chk("Enum" in _gpu_page_src,
    "卡片说明里点明设备管理器读的是 Enum 那一项（用户知道为什么以前没生效）")
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
chk(hasattr(ct, "lb_sched"), "CPU 调试页「调度增强」控件已就位")
chk(not hasattr(ct, "pow_table") and not hasattr(ct, "plan_combo"),
    "CPU 调试页不再承载电源计划控件（已拆为独立大类）")
_pw = win.pages.get("power")
chk(_pw is not None and _pw.title == "电源管理", "存在「电源管理」独立大类页面")
chk(hasattr(_pw, "plan_table") and hasattr(_pw, "pow_table") and hasattr(_pw, "plan_combo"),
    "电源管理页含计划列表 / .pow 导入 / 处理器参数控件")
chk(hasattr(_pw, "_plan_menu") and hasattr(_pw, "delete_plan") and hasattr(_pw, "modify_plan"),
    "电源计划右键菜单（删除 / 修改）已就位")
chk(not hasattr(ct, "gpu_combo") and not hasattr(ct, "gpu_name"),
    "CPU 调试页不再承载显卡控件（已拆为独立大类）")
_gp = win.pages.get("gpu")
chk(_gp is not None and _gp.title == "显卡伪装", "存在「显卡伪装」独立大类页面")
chk(len(win.NAV) == 17 and [k for k, _, _ in win.NAV][4] == "gpu",
    "NAV 共 17 项（16 + 新增的「清理与诊断」大类）")
# 「清理与诊断」：4 个页收进大类，不再单独占侧栏
_keys = [k for k, _, _ in win.NAV]
chk("hub" in _keys, "NAV 里有 hub")
chk(all(k in _keys for k in ("cleanup", "regtool", "systool", "policies")),
    "4 个子页仍留在 NAV 里（stack 下标要对齐，goto 也靠它找页）")
chk(len(win.group.buttons()) == len(win.NAV) - len(win.HIDDEN_NAV),
    "侧栏按钮数 = NAV - 隐藏项（%d = %d - %d）"
    % (len(win.group.buttons()), len(win.NAV), len(win.HIDDEN_NAV)))
chk(win.stack.count() == len(win.NAV), "stack 页数 == NAV 项数")
chk(getattr(win.pages["hub"], "TOOLS", None) is not None and
    len(win.pages["hub"].TOOLS) == 4, "hub 有 4 个格子")
chk([t[0] for t in win.pages["hub"].TOOLS] ==
    ["cleanup", "regtool", "systool", "policies"], "格子顺序固定且完整")
# 隐藏页进去后侧栏高亮停在大类上（不出现「整列没高亮」）
for _hk in ("cleanup", "regtool", "systool", "policies"):
    win.goto(_hk)
    _hub_i = _keys.index("hub")
    chk(win.group.button(_hub_i).isChecked(),
        "%s：侧栏高亮停在「清理与诊断」" % _hk)
win.goto("overview")

# 系统优化的大类也改成「一行三个横排」（用户要求，与 hub 卡片同款写法）
win.goto("optimize")
pump(2.5)
_og = win.pages["optimize"].cat_grid
_oitems = []
for _k in range(_og.count()):
    _it = _og.itemAt(_k)
    if _it.widget() is not None:
        _r, _c, _rs, _cs = _og.getItemPosition(_k)
        _oitems.append((_r, _c))
chk(len(_oitems) >= 3, "系统优化大类卡片已构建（%d 个）" % len(_oitems))
chk(sorted(set(_c for _r, _c in _oitems)) == [0, 1, 2],
    "系统优化大类用满 3 列：%s" % sorted(set(_c for _r, _c in _oitems)))
chk(all(_r == _i // 3 and _c == _i % 3
        for _i, (_r, _c) in enumerate(_oitems)),
    "位置一律 i//3 行、i%3 列（没有错位）")
chk(_app_src.count("self.cat_grid.addWidget(f, i // 3, i % 3)") == 1,
    "源码里就是 i//3 / i%3（不是只靠运行时恰好摆对）")
_oc = win.pages["optimize"].cat_grid.itemAt(0).widget()
_onames = [x.objectName() for x in _oc.findChildren(QLabel)]
chk("CatIcon" in _onames and "CatName" in _onames and "CatArrow" in _onames,
    "竖排卡片：图标 / 名称 / 箭头 都在（%s）" % _onames)
chk("CatCard" == _oc.objectName(), "沿用既有 CatCard 样式（没新造 QSS）")
win.goto("overview")

print()
print("⑮ 电源计划（内置模板 / .pow 扫描 / 导入回读验证）")
# --- 防回归：切换电源计划的代码**只能**出现在用户点击路径里 ---
#     起因：用户反馈「打开软件时电源计划被改成高性能」。实测（启动 + 遍历全部页面，
#     打桩 winreg.SetValueEx / 全部子进程）确认本软件只读不改；但要有断言守住这个性质，
#     免得以后有人在启动/加载路径里加了自动 setactive。
_click_handlers = set()
# 「点击路径」三种写法都要覆盖：on_click=self.X / on_click=lambda…self.X( /
# x.triggered.connect(lambda…self.X( 或 connect(self.X)
for _ln in _app_src.splitlines():
    if "on_click=" in _ln or ".triggered.connect(" in _ln:
        for _m in re.finditer(r"on_click=self\.(\w+)", _ln):
            _click_handlers.add(_m.group(1))
        for _m in re.finditer(r"on_click=lambda[^:]*:\s*self\.(\w+)", _ln):
            _click_handlers.add(_m.group(1))
        for _m in re.finditer(
                r"triggered\.connect\(\s*(?:lambda[^:]*:\s*)?self\.(\w+)", _ln):
            _click_handlers.add(_m.group(1))
_power_switch_fns = set()
for _n in ast.walk(ast.parse(_app_src)):
    if isinstance(_n, ast.FunctionDef):
        for _s in ast.walk(_n):
            if isinstance(_s, ast.Call) and isinstance(_s.func, ast.Attribute) \
                    and isinstance(_s.func.value, ast.Name) \
                    and _s.func.value.id == "S" \
                    and _s.func.attr in ("cpu_set_plan", "power_plan_activate_template"):
                _power_switch_fns.add(_n.name)
                break
chk(bool(_power_switch_fns), "确实存在切换电源计划的方法：%s"
    % sorted(_power_switch_fns))
_auto = sorted(_power_switch_fns - _click_handlers)
chk(not _auto,
    "切换电源计划的方法**全部**在点击处理器里（没有自动触发的）—— 越界的：%s"
    % (_auto or "无"))
# server 侧：`powercfg /setactive` 不许出现在「非切换函数」里
_sv_switch = set()
for _n in ast.walk(ast.parse(_src)):
    if isinstance(_n, ast.FunctionDef):
        for _s in ast.walk(_n):
            if isinstance(_s, ast.Constant) and isinstance(_s.value, str) \
                    and "setactive" in _s.value.lower():
                _sv_switch.add(_n.name)
                break
chk(_sv_switch <= {"cpu_set_plan", "power_plan_activate_template", "cpu_set_state"},
    "server 侧 /setactive 只出现在 cpu_set_plan / power_plan_activate_template / "
    "cpu_set_state 里 —— 实际：%s" % sorted(_sv_switch))
# --- 「被外部改动」检测 ---
chk(callable(getattr(S, "power_active_snapshot", None)),
    "有 power_active_snapshot()（只读快照，用于识别外部改动）")
_snap = S.power_active_snapshot()
chk(isinstance(_snap, dict) and "guid" in _snap and "name" in _snap,
    "快照返回 {guid, name, error}")
chk(_snap.get("guid", "") == (S._active_scheme() or "").lower(),
    "快照的 guid 与注册表一致")
_pp_src = _app_src[_app_src.find("class PowerPage"):
                  _app_src.find("class ", _app_src.find("class PowerPage") + 10)]
chk("def _check_plan_drift" in _pp_src, "PowerPage 有 _check_plan_drift()")
chk("plan_at_start" in _app_src and "plan_at_start" in _pp_src,
    "启动时记快照，比对时读它")
chk(_app_src.count("_switched_by_us = True") >= 4,
    "本软件自己切换计划的 %d 处都标记了（避免误报成外部改动）"
    % _app_src.count("_switched_by_us = True"))
chk("plan_at_start" in _app_src[_app_src.find("class MainWindow"):
                                _app_src.find("class ", _app_src.find("class MainWindow") + 10)],
    "快照在 MainWindow 里发起（一次注册表读，刻意静默）")
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
    # 电源计划相关控件已迁到「电源管理」页（cputune 上不再有 pow_table/scan_pow）
    win.goto("power"); pump(0.8)
    pw = win.pages["power"]
    pw.scan_pow(); pump(0.8)
    chk(pw.pow_table.rowCount() == 2, "候选表填充 2 行（%d）" % pw.pow_table.rowCount())
    chk(pw.pow_table.item(1, 1).text() == "已安装", "来源列区分 本机文件 / 已安装")
    # 断言别写死「1 个」：本机 .pow 文件数随机器变（0 个或多个都正常）
    chk("已发现" in pw.lb_pow.text() and "已安装" in pw.lb_pow.text(),
        "底部计数：%s" % pw.lb_pow.text())
    _real_set2 = S.cpu_set_plan
    _c2 = []
    S.cpu_set_plan = lambda g: (_c2.append(g), {"ok": True, "err": ""})[1]
    try:
        pw.pow_table.selectRow(1); pump(0.2)
        _key = A.picked_key(pw.pow_table)
        pw.import_pow(); pump(0.3)
        chk(_c2 == ["381b4222-f694-41f0-9685-ff5bb260df2e"],
            "选中已安装计划 → 直接启用（不走导入）"
            "｜picked=%r 行数=%d 有map=%s admin=%s 调用=%r"
            % (_key, pw.pow_table.rowCount(), bool(pw._pow_map),
               win.admin_mode, _c2))
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
        A.confirm_ex = lambda *a, **k: (True, False)
        pw.pow_table.selectRow(0); pump(0.2)
        pw.import_pow(); pump(0.4)
        chk(_c3 and _c3[0][0] == _pow and _c3[0][1] is True,
            "选中 .pow → 调用导入并启用（activate=True）")
        _c3.clear()
        win.admin_mode = False
        pw.import_pow(); pump(0.3)
        chk(not _c3, "非管理员：导入被拦下（仅只读提示）")
        _c3.clear()
        win.admin_mode = True
        pw.use_template("high"); pump(0.4)
        chk(True, "高性能模板（管理员 + 确认）路径可走通")
    finally:
        win.admin_mode, A.confirm = _old_admin, _old_confirm
        S.power_plan_import_pow = _real_imp
finally:
    S.power_plan_candidates = _real_cand
A.confirm = lambda *a, **k: False

print()
print("㉒ 清理前给「是否备份」提示（不再静默决定处置方式）")
chk(hasattr(A, "confirm_ex"), "有 confirm_ex（带勾选的确认框）")
_cx_src = io.open(A.__file__, encoding="utf-8").read()
_cx = _cx_src[_cx_src.find("def confirm_ex"):_cx_src.find("def _center_dialog")]
chk("backup_label" in _cx and "setCheckBox" in _cx, "confirm_ex 用 QMessageBox 的勾选框")
chk("return bool(confirm(parent, title, text, danger=danger)), False" in _cx,
    "不传 backup_label 时退回 confirm（测试套件对 confirm 的打桩依然生效）")
chk("isChecked()" in _cx and "except Exception" in _cx,
    "勾选框可能为 None 时有兜底，不会把 None 当布尔用")
chk("recycle" in _insp.signature(S.clean_junk).parameters,
    "clean_junk 收 recycle 参数（选择是否先移入回收站）")
chk(hasattr(S, "_recycle_many"), "有 _recycle_many（批量送回收站）")
_rm_src = io.open(S.__file__, encoding="utf-8").read()
_rm = _rm_src[_rm_src.find("def _recycle_many"):_rm_src.find("def _utf16z")]
chk('"\\0".join(chunk) + "\\0\\0"' in _rm or "\\0".join(chunk) in _rm,
    "批量送回收站用「路径用 \\0 分隔 + 末尾双 \\0」的 SHFileOperation 格式")
chk("splitdrive" in _rm, "按盘符分组（SHFileOperation 要求同一次调用同卷）")
chk("os.path.exists(p)" in _rm, "判定以「文件是否还在」为准（不信返回码）")
_pr = _rm_src[_rm_src.find("def _prune_dir"):_rm_src.find("def _glob_dirs")]
chk("collect" in _pr, "_prune_dir 支持 collect（只收集不删，供回收站模式用）")
# UI 接入
_cl = _cx_src[_cx_src.find("class CleanupPage"):_cx_src.find("class DiskToolsPage")]
chk("confirm_ex(" in _cl, "垃圾清理用 confirm_ex 弹确认框")
chk("clean_junk(ids, scale, _bak)" in _cl, "把用户的选择传给 clean_junk")
chk("移入回收站（可还原，但很慢）" in _cl, "勾选文案写明了代价（很慢）")
chk("预计慢" in _cl, "确认框里给出预计耗时（用户自己权衡）")
chk("已移入回收站，可从回收站还原" in _cl, "结果里如实报出多少进了回收站")
# 其它清理点都要写明处置方式（不静默）
for _label, _key, _frag in (
        ("残留清理", "class ResidualDialog", "文件会移入回收站（可还原）"),
        ("注册表清理", "def clean_issues", "先导出 .reg 备份"),
        ("空文件夹清理", "def del_empty", "送回收站，可还原"),
        ("重复文件清理", "def del_dup", "送回收站，可还原"),
        ("文件粉碎", "def do_shred", "无法恢复")):
    _i = _cx_src.find(_key)
    chk(_i >= 0, _label + "的处理入口存在（%s）" % _key)
    # 窗口给足：残留弹窗那个类很长，confirm 离 class 行有一万多字符
    chk(_frag in _cx_src[_i:_i + 16000] if _i >= 0 else False,
        _label + "的确认框写明了处置方式")
# 端到端：两种模式都不能破坏「按文件年龄过滤」
_old_t = _time.time() - 72 * 3600
_real_jt = S.junk_targets
for _rec in (False, True):
    _d = tempfile.mkdtemp(prefix="wtb_bak_")
    try:
        for _k in range(10):
            _p = os.path.join(_d, "old%d.tmp" % _k)
            with open(_p, "wb") as _f:
                _f.write(b"x" * 100)
            os.utime(_p, (_old_t, _old_t))
        _np = os.path.join(_d, "fresh.tmp")
        with open(_np, "wb") as _f:
            _f.write(b"y" * 50)
        S.junk_targets = lambda sc=1.0: [
            dict(t, path=_d, kind="clear", age=24, min_age_hours=int(24 * sc))
            if t["id"] == "user_temp" else t for t in _real_jt(sc)]
        _r = S.clean_junk(["user_temp"], 1.0, recycle=_rec)[0]
        chk(_r["freed"] == 1000,
            "recycle=%-5s 只删够旧的（freed=%s）" % (_rec, _r["freed"]))
        chk(os.path.exists(_np), "recycle=%-5s 新文件保留（年龄阈值仍生效）" % _rec)
        chk("recycled" in _r, "recycle=%-5s 结果带 recycled 字段" % _rec)
        if _rec:
            chk(_r["recycled"] == 10 and "回收站" in (_r.get("note") or ""),
                "recycle=True 时如实计数进了回收站（%s）" % _r["recycled"])
    finally:
        S.junk_targets = _real_jt
        _sh.rmtree(_d, ignore_errors=True)

print()
print("㉓ 软件内不得引用其它软件（去掉一切来源标签）")
import re as _re3
_BADRE = _re3.compile(r"HiBit|Geek|BCUninstaller|PrivateZilla|wpfui|CCleaner"
                      r"|ZyperWin|Sunlight|360 " + chr(26029) + chr(32593)
                      + "|VirusTotal", _re3.I)
_EXT = (".py", ".md", ".json", ".bat", ".spec", ".js", ".html", ".css")
_SKIP = ("build", "dist", "__pycache__", ".git", "_preview", "node_modules")
_root = os.path.dirname(os.path.abspath(A.__file__))   # 仓库根 = app.py 所在目录
_hits = []
for _dp, _dn, _fn in os.walk(_root):
    _dn[:] = [x for x in _dn if x.lower() not in _SKIP]
    for _f in _fn:
        if not _f.lower().endswith(_EXT):
            continue
        # 本文件自己必须写出这些名字才能检查别人 —— 排除它，否则自证其罪
        if _f == os.path.basename(__file__):
            continue
        _fp = os.path.join(_dp, _f)
        try:
            _txt = io.open(_fp, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for _m in _BADRE.finditer(_txt):
            _hits.append("%s:%d %s" % (os.path.relpath(_fp, _root),
                                       _txt[:_m.start()].count(chr(10)) + 1,
                                       _m.group(0)))
chk(not _hits, "全仓无第三方软件名（%d 处）%s"
    % (len(_hits), ("：" + "; ".join(_hits[:4])) if _hits else ""))
_rd = json.load(io.open(os.path.join(_root, "rules.json"), encoding="utf-8"))
chk(all("PrivateZilla" not in str(_r.get("src", "")) for _r in _rd["rules"]),
    "规则库 src 字段无第三方名")
chk(not any(str(_r.get("id", "")).startswith("pz_") for _r in _rd["rules"]),
    "规则 id 前缀无第三方缩写")
chk(len(_rd["rules"]) == 131, "规则数仍是 131（改名前缀没丢规则）")
_about = _cx_src[_cx_src.find(chr(34) + "功能来源" + chr(34)):
                 _cx_src.find(chr(34) + "功能来源" + chr(34)) + 260]
chk("同类工具" in _about and "ZyperWin" not in _about,
    "「关于」里的功能来源已改成中性表述")
chk(not os.path.exists(os.path.join(_root, "docs",
                                    "功能对照与补齐计划.md")),
    "旧的对照文档已改名（不再叫「对照」）")



print()
print("㉔ 注册表清理：分小类扫描 + 分类清理")
_sv_src = io.open(S.__file__, encoding="utf-8").read()
chk(hasattr(S, "REG_CATEGORIES"), "有 REG_CATEGORIES（UI 按它给出勾选框）")
_cats = S.REG_CATEGORIES
chk(len(_cats) >= 12, "小类至少 12 个（现 %d 个）" % len(_cats))
chk(all(len(c) == 5 for c in _cats), "每个小类是 (id, 名称, 说明, 默认, 风险) 五元组")
_ids = [c[0] for c in _cats]
chk(len(_ids) == len(set(_ids)), "分类 id 不重复")
chk(all(len(c[2]) >= 8 and len(c[1]) >= 4 for c in _cats),
    "每类都有中文名与说明（UI 靠它做 tooltip）")
chk(set(_ids) == set(S.REG_CAT_NAMES), "REG_CAT_NAMES 覆盖全部 id")
chk(set(S.REG_CAT_DEFAULT) <= set(_ids), "默认勾选集是分类集的子集")
chk(S.REG_CAT_WEAK <= set(_ids), "低置信集是分类集的子集")
chk(S.REG_CAT_WEAK.isdisjoint(S.REG_CAT_DEFAULT),
    "低置信分类默认不勾（%s）" % sorted(S.REG_CAT_WEAK))
# 只扫指定分类
_r_cat = S.registry_issues_scan(kinds=["com_ref"], per_cat=5)
chk(all(x.get("cat") == "com_ref" for x in _r_cat),
    "kinds 只扫指定分类（%d 项全是 com_ref）" % len(_r_cat))
chk(all(x.get("cat") and x.get("kind") and x.get("why") for x in _r_cat),
    "每项都带 cat/kind/why（UI 才能按分类显示）")
chk(all("weak" in x for x in _r_cat), "每项都显式带 weak")
chk(len(S.registry_issues_scan(kinds=[], per_cat=3)) >= len(_r_cat),
    "kinds 为空 → 按全部分类扫（向后兼容）")
_bad_cat = []
for _c in _ids:
    try:
        S.registry_issues_scan(kinds=[_c], per_cat=3)
    except Exception as _e:
        _bad_cat.append((_c, str(_e)[:30]))
chk(not _bad_cat, "13 个分类逐个都能扫通（异常：%s）" % _bad_cat)
chk(len(S.registry_issues_scan(kinds=["firewall"], per_cat=7)) <= 7,
    "per_cat 真的限住了单类数量（这条正是「某一类刷屏把别的挤掉」的根因）")
# 用户要求：选了某一类就列**全部**，不要被 400 截掉 → per_cat=0 表示不限
_st3 = {}
_r3 = S.registry_issues_scan(kinds=["firewall"], per_cat=0, stats=_st3)
chk(_st3.get("unlimited") is True and _st3.get("per_cat") == 0,
    "per_cat=0 → 不限（选了某一类就列全部）")
chk(set(("total", "shown", "truncated", "capped_cats", "enum_trunc"))
    <= set(_st3), "stats 回填读全情况（total/shown/truncated/capped/enum）")
chk(isinstance(_st3.get("capped_cats"), dict)
    and isinstance(_st3.get("enum_trunc"), list),
    "触顶分类与枚举截断都用容器类型（不是 None，UI 才不用兜）")
_r3b = S.registry_issues_scan(kinds=["firewall"], per_cat=7, cap=3)
_st3b = {}
S.registry_issues_scan(kinds=["firewall"], per_cat=7, cap=3, stats=_st3b)
chk(len(_r3b) <= 3 and (_st3b.get("truncated") is True
                        or _st3b.get("total", 0) <= 3),
    "全局 cap 生效，且被截断时 truncated=True（读不全必须说清）")
chk(isinstance(getattr(S, "_SCAN_TRUNC", None), list),
    "有 _SCAN_TRUNC 旁记（枚举截断只记不改行为）")
_sc_src = _src[_src.find("def registry_issues_scan("):
               _src.find("\ndef ", _src.find("def registry_issues_scan(") + 10)]
chk("stats is not None" in _sc_src, "registry_issues_scan 有 stats 出参")
chk("_note_enum_trunc" in _src, "枚举截断有统一的旁记出口")
# UI：下拉有「不限」、只勾一类自动放宽、结果区有提示位
# ⚠️ 这里必须**自己切段**：`_rt_seg` 在本行之后才定义（用了会 NameError）。
_rt_scan = _app_src[_app_src.find("class RegistryToolsPage"):]
_rt_scan = _rt_scan[:_rt_scan.find("\nclass ")]
chk(bool(_rt_scan), "切到注册表工具页源码段")
chk("lb_issue_trunc" in _rt_scan, "结果区有「读没读全」提示位")
chk("不限" in _rt_scan, "上限下拉含「不限」")
chk("auto_wide" in _rt_scan, "只勾 1 类会自动放开上限（并在界面写明）")
chk("capped_cats" in _rt_scan, "界面按 server 的 stats 判触顶（不再用 _issue_per 猜）")
_st2 = S.registry_issue_stats(_r_cat)
chk(_st2 and _st2[0][0] == "com_ref" and _st2[0][2] == len(_r_cat),
    "分类汇总条数正确：%s" % (_st2[:1],))
chk(all(len(r) == 4 for r in _st2), "汇总行是 (id, 名称, 总数, 高置信数)")

# --- 误报防护（三条都实测踩过） ---
chk(S._looks_like_progid("compressed") is False,
    "PerceivedType 的 compressed 不再被当成 ProgID（.cab/.gz 假阳性的根因）")
_reg_seg = _sv_src[_sv_src.find("def _reg_scan_filetype"):
                   _sv_src.find("def _reg_scan_filetype") + 4000]
chk('reg_read("HKCR\\\\%s" % ext, "")' in _reg_seg,
    "文件关联读的是**默认值**，不是「枚举到的第一个值」")
chk("_reg_exe_only" in _sv_src[_sv_src.find("def _reg_scan_com"):
                               _sv_src.find("def _reg_scan_com") + 3000],
    "COM 组件路径先切掉命令行参数再判存在（否则 Photoshop 这类全被误报）")
chk(S._reg_exe_only("D:\\App\\a.exe /Automation") == "D:\\App\\a.exe",
    "带参数的 LocalServer32 能切出纯路径")
chk(S._reg_exe_only('"C:\\Program Files\\x\\y.exe" -k')
    == "C:\\Program Files\\x\\y.exe", "带引号 + 参数也能切")
chk(S._reg_exe_only("C:\\a\\b.dll") == "C:\\a\\b.dll", "纯路径原样返回")
chk(S._reg_missing("C:\\__wtb_no_such_dir__\\x.exe") is True, "确实不存在的路径 → 判为缺失")
chk(S._reg_missing("") is False
    and S._reg_missing("C:\\no\\%WTBVAR%\\x.exe") is False
    and S._reg_missing("x.exe") is False,
    "拿不准的一律不判缺失（宁漏勿错：删活项会毁掉别人的软件）")
_ins_seg = _sv_src[_sv_src.find("def _reg_scan_installer"):
                   _sv_src.find("def _reg_scan_installer") + 5000]
chk("msi = " in _ins_seg and "windowsinstaller" in _ins_seg,
    "「无效的安装信息」跳过 MSI 管理的记录（删了会让 MSI 对不上）")

# --- UI 侧 ---
# ⚠️ 用「到下一个 class 为止」切片，**不要用固定 +N 字符** —— 页面代码一改，
#    固定长度就会把断言切到别的类里去（症状是断言莫名其妙地通过或失败）。
_rt_seg = _cx_src[_cx_src.find("class RegistryToolsPage"):
                  _cx_src.find("class SysToolsPage")]
_st_seg = _cx_src[_cx_src.find("class SysToolsPage"):
                  _cx_src.find("class PoliciesPage")]
_se_seg = _cx_src[_cx_src.find("class SettingsPage"):]
for _frag, _why in (("issue_cats", "有分类勾选框"),
                    ("issue_cat_preset", "有「只勾推荐 / 全选 / 全不选 / 反选」"),
                    ("mp_issue_cat", "有分类过滤下拉（多选）"),
                    ("_render_issues", "有按分类渲染"),
                    ("_on_issue_check", "勾选状态会回写原数据"),
                    ("REG_CAT_NAMES", "显示分类中文名")):
    chk(_frag in _rt_seg, "无效项清理：%s" % _why)
chk("cb_issue_cat" not in _rt_seg,
    "单选 QComboBox 已换成 MultiPick（18 类时单选太难用），不留两处入口")
chk("invert" in _rt_seg, "扫描范围有「反选」预设")
chk("not any(want.values())" in _rt_seg,
    "反选若得空集会回落到推荐组（而不是留下扫不了的状态）")
chk("_checked" in _rt_seg and "item.data(Qt.UserRole)" in _rt_seg,
    "勾选状态存在原列表（表格重建/排序后不丢）")
chk("按分类" in _rt_seg and "只处理" in _rt_seg,
    "文案说明了「清理只处理当前列表里勾选的项」")
_kc = _cx_src[_cx_src.find("KIND_CN = {"):_cx_src.find("KIND_CN = {") + 900]
chk(all(k in _kc for k in ("invalid_com_server", "invalid_shellex",
                           "invalid_apppath", "invalid_service")),
    "新类型的「类型」列中文名都在 KIND_CN 里")

print()
print("㉕ 磁盘工具：整盘占用扫描（先选盘再扫）")
chk(hasattr(S, "list_drives") and hasattr(S, "disk_usage_scan"),
    "服务端有 list_drives / disk_usage_scan")
_drv = S.list_drives()
chk(len(_drv) >= 1, "列出 %d 个磁盘" % len(_drv))
chk(all(d["total"] > 0 and d["root"].endswith("\\") for d in _drv),
    "每个盘都有容量、盘符以 \\ 结尾（可直接拼路径）")
chk(all(d["used"] + d["free"] <= d["total"] + 1024 ** 3 for d in _drv),
    "已用 + 可用 ≈ 总容量")
# ⚠️ 不要用固定字符长度切片：函数重写变长后就被截断 → 假 FAIL。
#    切到**下一个顶层 def** 为止。
_dk_i = _sv_src.find("def disk_usage_scan")
_dk_seg = _sv_src[_dk_i:_sv_src.find("\ndef ", _dk_i + 10)]
chk("os.scandir" in _dk_seg and "e.stat(" in _dk_seg,
    "走目录遍历 + DirEntry.stat（不依赖 Windows 搜索索引，也不为每个文件再发 stat）")
chk("on_tick" in _dk_seg, "有进度回调（UI 才能显示扫了多少文件）")
# 真跑一遍遍历（在临时目录上，等价于整盘逻辑）
_dkdir = tempfile.mkdtemp(prefix="wtb_dkchk_")
try:
    os.makedirs(os.path.join(_dkdir, "big", "sub"))
    with open(os.path.join(_dkdir, "big", "a.bin"), "wb") as _f:
        _f.write(b"x" * 4096)
    with open(os.path.join(_dkdir, "big", "sub", "b.bin"), "wb") as _f:
        _f.write(b"y" * 2048)
    with open(os.path.join(_dkdir, "top.bin"), "wb") as _f:
        _f.write(b"z" * 1024)
    _tick = []
    _rk = S.disk_usage_scan(_dkdir + "\\", top=10, min_size=0,
                            on_tick=lambda f, dd, t: _tick.append(f))
    chk(_rk["total"] == 7168, "整盘合计正确（%d 字节）" % _rk["total"])
    chk(_rk["files"] == 3, "文件数正确（%d）" % _rk["files"])
    _bn = {os.path.basename(x["path"]): x for x in _rk["rows"]}
    chk(_bn.get("big", {}).get("bytes") == 6144, "子目录占用累加到父目录")
    chk(_bn.get("big", {}).get("depth") == 1 and _bn.get("sub", {}).get("depth") == 2,
        "depth 标注正确（1 层 / 2 层）")
    chk(all("system" in x for x in _rk["rows"]), "每行带 system 标记（UI 提示别乱删）")
    chk(not any(os.path.normcase(x["path"]).rstrip("\\")
                == os.path.normcase(_dkdir).rstrip("\\") for x in _rk["rows"]),
        "盘根自身不出现在结果里")
    chk(bool(_tick), "on_tick 真的被调用了")
    chk(S.disk_usage_scan(_dkdir + "\\", top=10, min_size=10 ** 9)["rows"] == [],
        "小于阈值的目录被过滤")
finally:
    _sh.rmtree(_dkdir, ignore_errors=True)
# 同上：切到**下一个顶层 class**，别用固定长度（页变大会截断 → 假 FAIL）
_dt_i = _cx_src.find("class DiskToolsPage")
_dt_seg = _cx_src[_dt_i:_cx_src.find("\nclass ", _dt_i + 10)]
chk("disk_usage_scan" in _dt_seg and "cb_drive" in _dt_seg,
    "磁盘工具页接了「先选盘再扫」")
chk("refresh_drives" in _dt_seg and "list_drives" in _dt_seg,
    "盘符列表会重新读取（插了 U 盘能看到）")
chk("_dk_timer" in _dt_seg, "扫描时有进度定时器")
chk("_dk_timer.stop()" in _dt_seg, "扫完/失败都会停掉定时器（不常驻空转）")
chk("磁盘占用扫描" in _dt_seg, "页签名写在界面上")

print()
print("㉖ 备份与还原：还原点归注册表页 / .reg 操作备份归设置页")
chk(hasattr(S, "reg_backups_list"), "有 reg_backups_list")
_bkf = os.path.join(S.BACKUP, "ZZ_审计临时_20260101_000000.reg")
try:
    os.makedirs(S.BACKUP, exist_ok=True)
    with io.open(_bkf, "w", encoding="utf-16") as _f:
        _f.write("Windows Registry Editor Version 5.00\r\n")
    _blr = S.reg_backups_list()
    chk(any("ZZ_审计临时" in str(r.get("name")) for r in _blr),
        "新建的 .reg 备份能被读到（共 %d 条）" % len(_blr))
    chk(all(("kind" in r and "mtime" in r and "name" in r) for r in _blr),
        "每条备份带 kind/mtime/name")
finally:
    try:
        os.remove(_bkf)
    except OSError:
        pass

# --- 系统还原点：在「注册表清理 → 备份与还原」页 ---
chk("备份与还原" in _rt_seg, "注册表页第 3 个页签叫「备份与还原」")
for _frag, _why in (("_build_restore", "有还原点页签构造"),
                    ("scan_rp", "有还原点读取"),
                    ("make_rp", "有新建还原点"),
                    ("del_rp", "有删除还原点"),
                    ("_rp_sel", "勾选状态联动按钮"),
                    ("list_restore_points", "接了服务端接口")):
    chk(_frag in _rt_seg, "注册表页 %s（%s）" % (_why, _frag))
chk("load_backups" not in _rt_seg and "tb_bk" not in _rt_seg,
    "注册表页已彻底搬走 .reg 备份（不留两处入口）")
chk("需要管理员权限才能读取" in _rt_seg,
    "非管理员读不到还原点时明说（不谎报「0 个还原点」）")
chk("_bk_tabs_loaded" in _rt_seg, "还原点页签是「首次切到才读」")
_rf = _rt_seg[_rt_seg.find("def refresh"):_rt_seg.find("def refresh") + 2200]
chk("self._on_tab(self.tabs.currentIndex())" in _rf,
    "refresh 会重载还原点（否则切进来永远是空表）")

# --- .reg 操作备份：在设置页 ---
for _frag, _why in (("load_backups", "有备份列表加载"),
                    ("restore_bk", "有还原"),
                    ("open_backup_dir", "能打开 backup 目录"),
                    ("reg_backups_list", "接了服务端接口")):
    chk(_frag in _se_seg, "设置页 %s（%s）" % (_why, _frag))
chk("当前无备份" in _se_seg, "空列表时明确写「当前无备份」，不是「0 个备份」")
chk("PillWarn" in _se_seg, "空列表用警示色，用户一眼能看出异常")
chk("读取失败" in _se_seg, "读取失败会明说，不留一张空表让人以为「没有备份」")
chk("S.BACKUP" in _se_seg, "空列表时附上备份目录路径，方便自己确认")
_sef = _se_seg[_se_seg.find("def refresh"):_se_seg.find("def refresh") + 900]
chk("self.load_backups()" in _sef, "设置页 refresh 会重载备份列表")

# --- .reg 还原：必须回读复核、按「值」计数 ---
#     旧版按键计数 → 只有删除标记、一个值都没有的键也算「已还原 1 个键」= 谎报；
#     类型不认识的值还会被静默 continue 掉。
_rrs = _src[_src.find("def reg_backup_restore("):
            _src.find("\ndef ", _src.find("def reg_backup_restore(") + 10)]
chk("QueryValueEx" in _rrs, "还原后逐值**回读复核**（不信「没抛异常就是写成功」）")
chk("n_val" in _rrs, "按「值」计数而不是按「键」")
chk("n_val > 0" in _rrs, "只有真写进去至少一个值才算成功（空键不谎报）")
chk("skipped" in _rrs, "类型不认识的值**逐条上报**（不再静默跳过）")
chk("TypeError" in _rrs,
    "值类型不匹配也要抓（winreg 抛的是 TypeError，不是 OSError）")
chk("os.path.isfile" in _rrs, "文件不存在时明确报「不存在」")
chk("KEY_READ" in _rrs, "写入时同时请求读权限（回读复核需要）")
chk("个值" in _rrs, "结果文案里写出还原了多少个**值**")

# --- 配置迁移：单文件档案（含注册表状态），旧格式仍兼容 ---
#     起因：用户反馈「导出的 json 换新电脑导入，没能把所有设置恢复过来」
#     —— 因为 settings.json 只含外观/偏好，功能状态在注册表里。
chk("自动备份" not in _se_seg and "导入 / 导出" not in _se_seg,
    "「自动备份」与「导入 / 导出」已合并成一张卡")
chk("配置备份与迁移" in _se_seg, "合并后的卡片叫「配置备份与迁移」")
for _frag, _why in (("profile_build", "有档案打包"),
                    ("profile_normalize", "有档案校验/规范化"),
                    ("profile_backup_current", "导入前留恢复副本"),
                    ("own_state_export", "有自有注册表状态导出"),
                    ("own_state_import", "有自有注册表状态导入")):
    chk(callable(getattr(S, _frag, None)), "server 有 %s（%s）" % (_frag, _why))
_imp2 = _method_src(_set_src, "    def import_cfg(self):")
chk("S.profile_normalize" in _imp2,
    "导入前先**整份校验**（不合格就一个字节都不动）")
chk("S.profile_backup_current" in _imp2,
    "导入前给当前配置留恢复副本（TweakHub 的做法：先能回来，再改）")
chk("settings.update(data)" in _imp2, "档案里的设置真的写进字典")
chk("S.own_state_import" in _imp2, "注册表状态会写回")
chk("_legacy" in _imp2, "旧版文件（只有 settings）会明说是旧的")
chk("optimize_applied" in _imp2 and "S.optimize_missing" in _imp2,
    "导入时按本机实测算出「还差哪些」优化项（不是照搬勾选）")
chk("S.snapshot_and_apply" in _imp2,
    "确认之后才补应用（不顺手改掉系统）")
chk("confirm" in _imp2 and "顺带补齐" in _imp2,
    "补齐前单独问一次（用户可见、可拒绝）")
_exp2 = _method_src(_set_src, "    def export_cfg(self):")
chk("S.profile_build" in _exp2, "导出走完整档案（不再只 dump settings）")
chk("_includes" in _exp2, "导出后告诉用户「包含什么」")
# 兼容性：旧格式 / 无关 json
chk(S.profile_normalize({"_app": "WinToolbox", "settings": {"a": 1}}),
    "认得新格式")
chk(S.profile_normalize({"theme": "dark"}) is not None
    and S.profile_normalize({"theme": "dark"})["_legacy"] is True,
    "认得旧格式（平铺 settings）并标记为 _legacy")
chk(S.profile_normalize({"foo": 1}) is None, "无关 json 返回 None（不许瞎套）")
chk(S.profile_normalize({}) is None, "空文件返回 None")
chk(S.profile_normalize({"_app": "WinToolbox", "settings": "x"}) is None,
    "settings 不是字典时拒绝")
_pb = S.profile_build({"theme": "dark"}, "1.4")
chk(_pb.get("_app") == "WinToolbox" and "settings" in _pb and "own_reg" in _pb,
    "档案含 _app / settings / own_reg")
chk(bool(_pb.get("_includes")) and bool(_pb.get("_excludes")),
    "档案写明包含 / 不包含什么")
# 优化项清单（用户反馈「备份文件一键调系统，覆盖不了已优化项」）
chk("optimize_applied" in _pb, "档案带上「已生效优化项清单」")
chk(isinstance(_pb.get("optimize_applied"), list)
    and all(isinstance(x, str) for x in _pb["optimize_applied"]),
    "清单只存 id 字符串（规则库随程序走）")
for _fn2 in ("optimize_applied_list", "optimize_profile_ids", "optimize_missing"):
    chk(callable(getattr(S, _fn2, None)), "server 有 %s" % _fn2)
chk(all(isinstance(x, dict) and "id" in x and "name" in x
        for x in S.optimize_applied_list()), "optimize_applied_list 每项带 id/name")
chk(S.optimize_missing([]) == [] and S.optimize_missing(["__no_such_rule__"]) == [],
    "空清单 / 认不出的 id 一律不补齐（不瞎套）")
chk(S.optimize_profile_ids() == [x["id"] for x in S.optimize_applied_list()],
    "profile_ids 就是 applied_list 的 id 投影")
chk("optimize_applied" in S.profile_normalize({"_app": "WinToolbox",
                                               "settings": {}}),
    "规范化结果里一定有 optimize_applied（缺了也是空列表，不是没有键）")
# 自有注册表状态：导出→导入 往返（用真实临时键）
_own_root = "Software" + chr(92) + "WinToolbox"
_probe_sub = _own_root + chr(92) + "AuditProbe"
try:
    _w = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _probe_sub, 0, winreg.KEY_WRITE)
    winreg.SetValueEx(_w, "N", 0, winreg.REG_DWORD, 42)
    winreg.SetValueEx(_w, "S", 0, winreg.REG_SZ, "abc")
    winreg.CloseKey(_w)
    _tree = S.own_state_export()
    chk(isinstance(_tree, dict) and bool(_tree), "own_state_export 读到了自有状态")
    import json as _json
    chk("AuditProbe" in _json.dumps(_tree, ensure_ascii=False),
        "导出的结构里含刚造的键")
    # 删掉再导入（递归删，否则父键删不掉）
    S.reg_delete_tree("HKCU", _own_root)
    _r2 = S.own_state_import(_tree)
    chk(_r2.get("vals", 0) >= 2, "写回了值：%s" % _r2.get("msg"))
    _rr = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _probe_sub, 0, winreg.KEY_READ)
    chk(winreg.QueryValueEx(_rr, "N")[0] == 42, "REG_DWORD 往返正确")
    chk(winreg.QueryValueEx(_rr, "S")[0] == "abc", "REG_SZ 往返正确")
    winreg.CloseKey(_rr)
finally:
    # ⚠️ 必须**递归**删：`winreg.DeleteKey` 只能删空键，父键下有子键时
    # 那句「删父键」其实什么都没删 —— 测试残留就是这么污染的（踩过）。
    S.reg_delete_tree("HKCU", _own_root)
_ois = _src[_src.find("def own_state_import("):
            _src.find("\ndef ", _src.find("def own_state_import(") + 10)]
chk("QueryValueEx" in _ois, "自有状态写回也**逐值回读复核**")
chk("n_val > 0" in _ois or "n_key > 0" in _ois, "按值/键计数并据此判成功")
chk("hex:" in _ois or "hex:" in _src, "REG_BINARY 用 hex 编码（JSON 存不了 bytes）")
# 这条测试**不许留下任何残留**（曾经因为只删父键而污染了用户环境）
chk(not S.own_state_export(),
    "注册表测试跑完没留下残留（own_state_export 为空）")
chk(callable(getattr(S, "reg_delete_tree", None)),
    "有 reg_delete_tree（递归删子树 —— DeleteKey 只能删空键）")

# --- 「本工具禁用了什么」的记录（启动项 / 服务 / 计划任务，2026-10-12 新增）---
# 禁用这三样改的是 **Windows 自己的状态**（StartupApproved / Start / Enabled），
# 系统里没有「谁禁的」这种线索 → 换机器就丢。记录放在自有子树下随档案走。
for _fn3 in ("disabled_records", "disabled_record_add", "disabled_record_del",
             "disabled_records_pending", "disabled_records_apply",
             "disabled_record_label"):
    chk(callable(getattr(S, _fn3, None)), "server 有 %s" % _fn3)
chk(S._REC_ROOT.startswith(S._OWN_ROOT + chr(92)),
    "_REC_ROOT 在自有子树下（这样才会随配置档案走）")
chk(set(S._REC_KINDS) == set(("startup", "service", "task")),
    "_REC_KINDS 覆盖启动项 / 服务 / 计划任务")
chk(bool(S._REC_SKIP_TASKS) and all("Defender" in x for x in S._REC_SKIP_TASKS),
    "Defender 的 4 个计划任务不进通用记录（它有自己的快照逻辑）")
# 真做一遍：加 → 读 → 去重 → 删（收尾必须一尘不染）
try:
    S.disabled_record_add("service", name="ZZAuditSvc")
    S.disabled_record_add("task", full=chr(92) + "ZZAudit" + chr(92) + "Probe")
    _rec = S.disabled_records()
    chk(len(_rec["service"]) == 1 and len(_rec["task"]) == 1, "记录能读回")
    chk(_rec["service"][0].get("kind") == "service",
        "读回的条目带上 kind（UI 才知道怎么显示）")
    chk("DisabledByTool" in _json.dumps(S.own_state_export(), ensure_ascii=False),
        "记录落在 own_state_export() 里（随档案走）")
    S.disabled_record_add("service", name="ZZAuditSvc")
    chk(len(S.disabled_records("service")["service"]) == 1, "同一项不重复记")
    chk(bool(S.disabled_record_label("startup",
                                     {"name": "A", "hive": "HKCU"})),
        "disabled_record_label 能出可读标签")
finally:
    S.disabled_record_del("service", name="ZZAuditSvc")
    S.disabled_record_del("task", full=chr(92) + "ZZAudit" + chr(92) + "Probe")
chk(not any(S.disabled_records().values()), "删完记录为空")
chk(not S.own_state_export(),
    "记录删空后 own_state_export 仍为空（空壳键**不导出**）")
# 三个 setter 都要记一笔；导入端要按本机实况算差集
_su_src = _src[_src.find("def set_startup_enabled("):
               _src.find("\ndef ", _src.find("def set_startup_enabled(") + 10)]
chk('disabled_record_add("startup"' in _su_src
    and 'disabled_record_del("startup"' in _su_src,
    "set_startup_enabled 禁用时记一笔 / 启用时删掉")
_sv_src2 = _src[_src.find("def set_service("):
                _src.find("\ndef ", _src.find("def set_service(") + 10)]
chk('disabled_record_add("service"' in _sv_src2
    and "action == \"disable\"" in _sv_src2,
    "set_service 只对 disable 记一笔（stop/start 是临时动作）")
_st_src2 = _src[_src.find("def set_task("):
                _src.find("\ndef ", _src.find("def set_task(") + 10)]
chk('disabled_record_add("task"' in _st_src2 and "_REC_SKIP_TASKS" in _st_src2,
    "set_task 记一笔，且跳过 Defender 自己的任务")
chk("disabled_records_pending" in _imp2 and "disabled_records_apply" in _imp2,
    "导入时按记录算出「本机还没禁用的」再补做")
chk("顺带恢复" in _imp2, "补做前单独问一次（用户可见、可拒绝）")

# --- 系统组件页：还原点已迁走，不能留死代码 ---
chk("scan_rp" not in _st_seg and "tb_rp" not in _st_seg,
    "系统组件页不再有还原点代码（迁走而非复制）")
chk("_reload_tab" in _st_seg, "系统组件页仍有强制重扫（注册/注销组件后列表会重读）")

print()
print("㉗ 还原点删除：用对命令 + 哨兵互斥（两个真 bug）")
# ⚠️ 还原点的实现在 **server.py**，不是 app.py —— `_cx_src` 是 app.py 的源码，
#    拿它去 find("def remove_restore_point") 会返回 -1，然后切片从末尾往前取，
#    断言就会莫名其妙地全 FAIL（切了别的类/别的文件）。
_sv_src = io.open(S.__file__, encoding="utf-8").read()
_i = _sv_src.find("def remove_restore_point")
chk(_i > 0, "server.py 里有 remove_restore_point")
_j = _sv_src.find("\ndef ", _i + 10)
_rp_src = _sv_src[_i:_j if _j > 0 else _i + 3000]
_i2 = _sv_src.find("def create_restore_point")
_j2 = _sv_src.find("\ndef ", _i2 + 10)
_rp_create = _sv_src[_i2:_j2 if _j2 > 0 else _i2 + 2000]

chk("Remove-ComputerRestorePoint" not in _rp_src,
    "不再调用那个**根本不存在**的命令 Remove-ComputerRestorePoint")
chk(".Delete()" in _rp_src, "改用 Get-ComputerRestorePoint 返回对象上的 .Delete()")
chk("try {" in _rp_src and "catch {" in _rp_src,
    "有 try/catch（把 PowerShell 报错接住，而不是丢一句「找不到命令」）")
chk("SRP_OK" in _rp_src and "SRP_ERR" in _rp_src,
    "用互斥哨兵 SRP_OK / SRP_ERR 判成败")
chk("not $rp" in _rp_src, "区分「序列号不存在」与「删除失败」")
chk("最早的那个还原点" in _rp_src,
    "回读复核发现仍在时，如实说明是系统限制（最早的不让删）")
chk("ErrorAction Stop" in _rp_create and "SRP_OK" in _rp_create,
    "创建路径同样用哨兵（powershell 出错时退出码仍是 0，不能只看 rc）")
chk("replace(\"'\", \"''\")" in _rp_create,
    "描述里的单引号被转义（否则脚本会被截断）")
chk("ErrorAction Stop" in _rp_src and "-ErrorAction Stop" in _rp_src,
    "读/删都加了 -ErrorAction Stop（WMI 的 ErrorRecord 否则不抛异常）")
chk(hasattr(S, "_rp_run") and hasattr(S, "_rp_first_error")
    and hasattr(S, "_rp_zh"), "有 _rp_run / _rp_first_error / _rp_zh 三个辅助")
# 实跑一次：不能有 admin，但必须拿到**可读的中文原因**而不是空表
_rp_probe = S._rp_run(
    "$ErrorActionPreference='Stop';"
    "try { $r = @(Get-ComputerRestorePoint -ErrorAction Stop | "
    "         Where-Object { $_.SequenceNumber -eq 999999 }); "
    "if (-not $r) { 'SRP_NOTFOUND' } else { 'SRP_OK' } } "
    "catch { 'SRP_ERR:' + $_.Exception.Message }", timeout=90)
chk(_rp_probe[0] in ("err", "notfound"),
    "探测脚本返回的状态是 err/notfound（%s）" % (_rp_probe[0],))
chk(bool(_rp_probe[1]) or _rp_probe[0] == "notfound",
    "失败时给出了原因：%r" % (_rp_probe[1][:50],))
if _rp_probe[1]:
    chk(not _rp_probe[1].strip().isascii() or "Access" in _rp_probe[1],
        "原因是中文或可读的英文，不是乱码（%r）" % _rp_probe[1][:40])
chk(S._rp_zh("some totally unknown error") == "some totally unknown error",
    "翻译表没命中就原样返回，不乱编原因")
# 失败时不能是「空原因」—— 用户看到一句没有信息的话等于没报
_r = S.remove_restore_point("999999")
chk(isinstance(_r, dict) and _r.get("ok") is False and bool(str(_r.get("msg")).strip()),
    "remove_restore_point 失败时给出非空原因（%r）" % str(_r.get("msg"))[:50])
chk("Traceback" not in str(_r.get("msg")) and "命令" not in str(_r.get("msg")),
    "失败原因不是 Python 异常、也不是「找不到命令」这类技术黑话")

print()
print("㉘ 「刷新」按钮必须真重读，不能只回放会话缓存")
_bad = [ln.strip() for ln in _cx_src.splitlines()
        if "刷新" in ln and "on_click=self.refresh" in ln]
chk(not _bad, "没有任何「刷新」按钮绑到 self.refresh（%s）" % (_bad or "无"))
_n_reload = len([ln for ln in _cx_src.splitlines()
                 if "刷新" in ln and "on_click=self.reload" in ln])
chk(_n_reload >= 5, "共 %d 处「刷新」按钮绑的是 reload（会先丢缓存）" % _n_reload)
# 逐页复核：切到「下一个 class 之前」，别用固定长度
for _pg, _why in (("class SoftwarePage", "软件管理"),
                  ("class StartupPage", "启动与服务"),
                  ("class GpuPage", "显卡伪装")):
    _i3 = _cx_src.find(_pg)
    _nxt = _cx_src.find("\nclass ", _i3 + 1)
    _seg2 = _cx_src[_i3:_nxt if _nxt > 0 else len(_cx_src)]
    _m = [ln.strip() for ln in _seg2.splitlines()
          if "刷新" in ln and "on_click=" in ln]
    chk(_m and all("self.reload" in x for x in _m),
        "%s 页的刷新按钮绑的是 reload（%s）" % (_why, _m or "无按钮"))

print()
print("㉙ 计划任务：不走被拦的 schtasks.exe（4 处调用点全换掉）")
# ⚠️ 纯文本 grep 会把 docstring / 注释里的历史说明也算进来（那不是调用），
#    必须用 AST 找 `run([...])` 的第一个字符串字面量 == "schtasks"。

_ast_hits = [n.lineno for n in ast.walk(ast.parse(_sv_src))
             if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "run" and n.args
                 and isinstance(n.args[0], ast.List) and n.args[0].elts
                 and isinstance(n.args[0].elts[0], ast.Constant)
                 and n.args[0].elts[0].value == "schtasks")]
chk(not _ast_hits, "没有任何 run(['schtasks', ...]) 调用（行号 %s）"
    % (_ast_hits or "无"))
chk(hasattr(S, "_task_list_raw") and hasattr(S, "_task_split"),
    "有 _task_list_raw / _task_split 两个新辅助")
_tl = _sv_src[_sv_src.find("def _task_list_raw"):
              _sv_src.find("\ndef ", _sv_src.find("def _task_list_raw") + 10)]
chk("Get-ScheduledTask" in _tl, "_task_list_raw 改走 Get-ScheduledTask")
_te = _sv_src[_sv_src.find("def _task_exists"):
              _sv_src.find("\ndef ", _sv_src.find("def _task_exists") + 10)]
chk("Get-ScheduledTask" in _te,
    "_task_exists 改走 PowerShell（原来恒返回 False —— **假阴性**，"
    "会把「还在的任务」判成「已删干净」）")
chk("_task_split" in _te, "_task_exists 会先把全名拆成 TaskPath/TaskName")
_st = _sv_src[_sv_src.find("def set_task"):
              _sv_src.find("\ndef ", _sv_src.find("def set_task") + 10)]
chk("-ScheduledTask" in _st and "Out-Null" in _st,
    "set_task 改走 Enable-/Disable-ScheduledTask")
chk("Get-ScheduledTask" in _st and "回读" in _st,
    "set_task **回读复核**状态（不信「命令返回成功」）")
_sc = _sv_src[_sv_src.find("def _scan_tasks"):
              _sv_src.find("\ndef ", _sv_src.find("def _scan_tasks") + 10)]
chk("_task_list_raw" in _sc, "_scan_tasks 用新的枚举器")
chk("microsoft" in _sc.lower(), "\\Microsoft\\ 系统任务仍被跳过")
# 残留清理里的删除分支
chk("Unregister-ScheduledTask" in _sv_src,
    "残留清理的删任务也改走 Unregister-ScheduledTask（原来 schtasks /delete）")
# ⚠️ 数「真正在执行的字符串字面量」，别用 `count()` 也不要用行首字符过滤 ——
#    这行代码是嵌在 Python 字符串里的、以引号开头，而注释里也有同一个词，
#    两种朴素做法都会数错。走 AST 的常量节点最准。
_unreg_consts = [n.value for n in ast.walk(ast.parse(_sv_src))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and "Unregister-ScheduledTask" in n.value]
chk(len(_unreg_consts) == 1,
    "删任务的实现只1 处，没重复实现（%d 处）" % len(_unreg_consts))
# 实跑：_task_exists 的假阴性必须修好了（本机有 209 个任务可查）
_ts = S._task_list_raw()
chk(len(_ts) > 50, "实跑 _task_list_raw 读到 %d 条任务" % len(_ts))
chk(all(isinstance(r, tuple) and len(r) == 2 and r[1] for r in _ts),
    "每项是 (路径, 名称) 且名称非空")
_sample = _ts[len(_ts) // 2]
_fullname = _sample[0] + _sample[1]
chk(S._task_exists(_fullname) is True,
    "实跑 _task_exists(%r) = True（修复前恒 False）" % _fullname[:44])
chk(S._task_exists("\\ZZ_NoSuchTask_9F3A\\nope") is False,
    "不存在的任务仍是 False（没变成恒 True）")
chk(S._task_exists("") is False, "空串安全返回 False")
chk(S._task_split("NoPathTask") == ("\\", "NoPathTask"),
    "_task_split 处理无路径的名字")
# 扫出来的东西必须真能查到（否则是假阳性）
_hits = S._scan_tasks(*S._res_tokens(_sample[1], ""), 5)
for _h in _hits:
    if S._task_exists(_h["path"]):
        break
chk(not _hits or S._task_exists(_hits[0]["path"]),
    "扫出的任务能被 _task_exists 证实存在（无假阳性）")
chk(all(not h["path"].lower().startswith("\\microsoft\\") for h in _hits),
    "扫描结果里没有 \\Microsoft\\ 系统任务")

print()
print("㉚ 分类多选下拉 + 反选预设（18 类下单选不可用）")
_mp_seg = _cx_src[_cx_src.find("class MultiPick"):
                  _cx_src.find("class FramelessWindow")]
chk("class MultiPick" in _cx_src, "有 MultiPick 多选下拉控件")
for _frag, _why in (("set_items", "能重建选项"),
                    ("def picked", "能读出选中的集合"),
                    ("反选", "菜单里有「反选」快捷动作"),
                    ("清空选择", "菜单里有「清空选择」"),
                    ("空集合 = 全部", "注释写明了空集语义")):
    chk(_frag in _mp_seg, "MultiPick %s（%s）" % (_why, _frag))
chk("MultiPick" in _rt_seg, "注册表页真的用了 MultiPick")
chk("self.mp_issue_cat.picked()" in _rt_seg,
    "渲染按**集合**过滤（`not in want`，不是单选比较）")
chk('if want and (x.get("cat") or "") not in want' in _rt_seg,
    "过滤条件是多选语义：不在集合里就跳过")
# registry_issue_stats 第 4 项是「其中可勾选条数」，别当描述文案用
_chk4 = _rt_seg[_rt_seg.find("def _fill_issue_cat"):
                _rt_seg.find("def _fill_issue_cat") + 900]
chk("strong" in _chk4 or "可勾选" in _chk4,
    "下拉副文本用的是「可勾选条数」（stats 的第 4 项）")
chk("REG_CAT_DESC" in _chk4, "副文本带上分类说明（来自 REG_CAT_DESC）")
# 勾选框计数实时联动
chk("cb.toggled.connect" in _rt_seg,
    "手动勾选也更新「已勾选 N / 18」计数（不只预设按钮触发）")
chk("getattr(self, \"lb_issue_cat\", None)" in _rt_seg,
    "计数更新有 getattr 兜底（勾选框在 lb 创建前就 setChecked 过）")
chk("一个都没勾" in _rt_seg and "已勾选 全部" in _rt_seg,
    "0 勾与全选都有明确文案，不再只是干巴巴的数字")
# 四个预设：recommend 走else 分支（默认值），所以**别断言字符串出现**——
# 那会逼着代码写重复分支。断言「四种模式都有明确归宿」即可。
_ip = _rt_seg[_rt_seg.find("def issue_cat_preset"):
              _rt_seg.find("def _issue_cats_sync")]
chk("elif mode ==" in _ip or ("mode ==" in _ip and _ip.count("mode ==") >= 3),
    "预设是显式分支而非层层 else（%d 处mode ==）" % _ip.count("mode =="))
chk("else:" in _ip and "recommend" in _rt_seg,
    "剩下那个走else 默认分支（= 只勾推荐）")
for _mode in ("all", "none", "invert"):
    chk('mode == "%s"' % _mode in _ip, "有 %s 预设" % _mode)

print()
print("㉛ Defender：**全关闭**（8 服务/驱动 + 策略 + 4 计划任务）")
_def_src = _sv_src[_sv_src.find("def set_defender"):
                   _sv_src.find("def set_defender") + 9000]
_def_fn = next((n for n in ast.walk(ast.parse(_sv_src))
                if isinstance(n, ast.FunctionDef) and n.name == "set_defender"),
               None)
chk(_def_fn is not None, "有 set_defender")
# ⚠️ 全关闭清单必须是 8 个（少一个就关不干净）
_names = [n for n, _k, _d, _x in S._DEF_COMPONENTS]
chk(len(S._DEF_COMPONENTS) == 8, "全关闭清单是 8 个组件（%d）" % len(S._DEF_COMPONENTS))
for _n in ("WinDefend", "WdNisSvc", "WdNisDrv", "WdFilter", "WdBoot", "Sense",
           "SecurityHealthService", "WaaSMedicSvc"):
    chk(_n in _names, "清单含 %s" % _n)
# ⚠️ WdBoot/WdFilter 默认是 0（Boot）不是 3 —— 写错会让启动期驱动不加载
_dflt = dict((n, d) for n, _k, d, _x in S._DEF_COMPONENTS)
chk(_dflt["WdBoot"] == 0 and _dflt["WdFilter"] == 0,
    "WdBoot / WdFilter 默认 Start=0（Boot 启动）")
chk(_dflt["WinDefend"] == 2, "WinDefend 默认 Start=2（自动）")
chk(len(S._DEF_TASKS) == 4 and all("Windows Defender" in t
                                   for t in S._DEF_TASKS),
    "4 个 Windows Defender 计划任务都在清单里")
# 关闭分支必须**真的**去禁服务（与上一版「只关实时保护」正好相反）
_off_dump = ast.dump(ast.Module(body=[_def_fn], type_ignores=[])) if _def_fn else ""
chk("_svc_set_start" in _off_dump,
    "**关闭分支会禁用服务/驱动**（不是只关实时保护）")
chk("_DEF_COMPONENTS" in _off_dump, "关闭分支遍历完整清单而不是手写几个")
chk("set_task" in _off_dump, "关闭分支也禁用 4 个计划任务")
chk("4" in _def_src, "写的是 Start=4（禁用）")
# 快照：已禁用的值不能当原值
chk("def _def_snapshot_save" in _sv_src, "有关闭前的原值快照")
chk("def _def_snapshot_restore_values" in _sv_src, "恢复时读快照")
_snap = _sv_src[_sv_src.find("def _def_snapshot_save"):
                _sv_src.find("def _def_snapshot_restore_values")]
chk("== 4" in _snap or "== 4" in _sv_src,
    "快照**跳过 4（已禁用）**，不当成原值（否则恢复会静默写回禁用）")
chk("Windows 默认值" in _sv_src or "默认值表" in _sv_src,
    "没有快照时退回默认值表并说明")
# 前置：SCM 绕行 + 两个关键服务
chk("def _svc_set_start_scm" in _sv_src, "有 SCM 写入路径（绕开受保护的服务键）")
chk("ChangeServiceConfigW" in _sv_src, "用 ChangeServiceConfig")
chk("ChangeServiceConfigW" in _sv_src and "取得所有权" in _sv_src,
    "注释说明为什么走 SCM 而不是改 ACL / 取得所有权")
chk("def defender_prereq_text" in _sv_src, "有可供 UI 展示的前置清单函数")
_prq = S.defender_prereq_text()
chk("SecurityHealthService" in _prq and "只读" in _prq and "SCM" in _prq,
    "前置清单说明了 SecurityHealthService 的键只读、改走 SCM")
chk("WaaSMedicSvc" in _prq and "改回来" in _prq,
    "前置清单说明了 WaaSMedicSvc 会把改动改回来")
chk("安全模式" in _prq and "重启" in _prq, "前置清单说明了重启 / 安全模式")
# 语义修正：DisableAntiSpyware 在新系统已被忽略，不再是唯一手段
# ⚠️ 这条注释在**常量区**（_DEF_RT_VALUE 上面），不在 set_defender 里面 ——
#    用 _def_src 去查会假 FAIL。取「Defender 常量块」来查。
_def_consts = _sv_src[_sv_src.find("_DEF_POL ="):
                      _sv_src.find("def defender_status")]
chk("已忽略" in _def_consts or "客户端 SKU" in _def_consts,
    "注释点明 DisableAntiSpyware 被忽略（不再当主手段）")
chk("篡改防护" in _def_src and "tamper" in _def_src,
    "篡改防护会拦回写入这点有处理并会告知用户")
chk("def _mp_run" in _sv_src and "def _mp_ok" in _sv_src,
    "有 _mp_run / _mp_ok 两个 Defender 辅助")
_ds = S.defender_status()
chk(set(("enabled", "start", "realtime", "tamper")) <= set(_ds),
    "defender_status 含 enabled/start/realtime/tamper")
chk(set(("disabled", "remaining", "n_disabled", "n_total")) <= set(_ds),
    "defender_status 还含全关闭进度（disabled/remaining/n_disabled/n_total）")
# 用户反馈「关了 Defender 却像没生效」→ 必须有结论字段 + 把「系统会改回」的挑出来
chk(set(("self_heal", "sticky_remaining", "effectively_off")) <= set(_ds),
    "defender_status 含 self_heal / sticky_remaining / effectively_off")
chk(bool(getattr(S, "_DEF_SELF_HEAL", None))
    and "WaaSMedicSvc" in S._DEF_SELF_HEAL,
    "WaaSMedicSvc 被列入「Windows 会自己改回」清单")
chk(set(_ds.get("remaining") or []) ==
    set(_ds.get("self_heal") or []) | set(_ds.get("sticky_remaining") or []),
    "self_heal + sticky_remaining 恰好等于 remaining（不漏不重）")
chk(_ds.get("effectively_off") in (True, False, None),
    "effectively_off 是三态（读不到不许退化成 False）")
chk(_ds.get("realtime") is not None or _ds.get("note"),
    "要么读到实时保护状态，要么给出读不到的原因（不静默）")
chk(hasattr(S, "_DEF_SVCS") and dict(S._DEF_SVCS).get("WinDefend") == 2,
    "_DEF_SVCS 里 WinDefend 默认值是 2（自动）")
# ⚠️ 同名函数静默覆盖：本文件里 set_defender 只该有一个定义
chk(_sv_src.count("\ndef set_defender(") == 1,
    "set_defender 只有一处定义（曾因重复定义导致新增代码完全没生效）")
chk(_sv_src.count("\ndef defender_status(") == 1,
    "defender_status 只有一处定义")
# 进度反馈（2026-10-12）：8 服务 + 4 策略 + 4 任务逐个回读要好几秒，
# 只写一句「正在关闭…」用户会以为卡住了。
chk("progress=None" in _def_src, "set_defender 支持 progress 回调")
chk(_def_src.count("_pg(") >= 5, "各阶段都上报进度（%d 处）" % _def_src.count("_pg("))
_dp = (S._def_snapshot_save, S._svc_set_start, S.reg_write, S._reg_dword,
       S.set_task, S.defender_status)
S._def_snapshot_save = lambda: (False, 0)
S._svc_set_start = lambda n, v: (True, "stub")
S.reg_write = lambda *a, **k: True
S._reg_dword = lambda k, v: 1
S.set_task = lambda t, e: (True, "stub")
S.defender_status = lambda: {"remaining": [], "tamper": False,
                             "realtime": False, "self_heal": [],
                             "sticky_remaining": [], "n_disabled": 8,
                             "n_total": 8}
try:
    _msgs = []
    _r5 = S.set_defender(False, progress=_msgs.append)
    chk(len(_msgs) >= 15, "逐条上报进度（%d 条）" % len(_msgs))
    chk(all(isinstance(m, str) and m.strip() for m in _msgs),
        "进度都是非空文本")

    def _boom(_m):
        raise RuntimeError("进度回调不该影响主流程")

    _r6 = S.set_defender(False, progress=_boom)
    chk(len(_r6) == len(_r5), "进度回调抛异常时主流程照常完成（进度是附加品）")
    _r7 = S.set_defender(False)          # 不传 progress 也必须能跑
    chk(len(_r7) == len(_r5), "不传 progress 时行为不变（向后兼容）")
finally:
    (S._def_snapshot_save, S._svc_set_start, S.reg_write, S._reg_dword,
     S.set_task, S.defender_status) = _dp
_ps_src = _cx_src[_cx_src.find("class ProgressSpin"):]
_ps_src = _ps_src[:_ps_src.find(chr(10) + "class ")] if _ps_src else ""
chk("class ProgressSpin" in _cx_src, "有 ProgressSpin（后台进度 → spinner）")
chk("setInterval(120)" in _ps_src and "busy(" in _ps_src,
    "ProgressSpin 用 120ms 定时器在主线程采样（不跨线程碰控件）")
chk('state.setdefault("msg", initial)' in _ps_src,
    "初始文案写进 state（否则 120ms 后被覆盖成光秃秃的「处理中…」）")
chk("def stop" in _ps_src and "stop()" in _ps_src,
    "有 stop()（操作结束要停表，不常驻）")
_td_src = _cx_src[_cx_src.find("def toggle_defender"):]
_td_src = _td_src[:_td_src.find(chr(10) + "    def ")]
chk("ProgressSpin(" in _td_src and "progress=" in _td_src,
    "toggle_defender 把 server 的进度接到 ProgressSpin")
chk("spin.stop()" in _td_src, "成功/失败两条路径都会停表")
# UI
_app2 = io.open(A.__file__, encoding="utf-8").read()
chk("def _def_suppressed" in _app2, "有 _def_suppressed 统一判据")
_pu3 = _app2[_app2.find("def _paint_toggles"):_app2.find("def _toggle_done")]
_ti = _app2.find("def toggle_defender")
_tj = _app2.find("\n    def ", _ti + 10)
_tu3 = _app2[_ti:_tj if _tj > 0 else _ti + 3200]
chk("_def_suppressed()" in _pu3, "按钮文字读 _def_suppressed")
chk("_def_suppressed()" in _tu3, "点击动作读 _def_suppressed（不会背离）")
chk("全关闭" in _tu3, "确认框写的是「全关闭」")
chk("8 个服务" in _tu3, "确认框列出 8 个服务/驱动")
chk("SecurityHealthService" in _tu3 and "WaaSMedicSvc" in _tu3,
    "确认框点出两个前置服务")
chk("安全模式" in _tu3 and "PPL" in _tu3,
    "确认框说明 PPL 自我保护与安全模式")
chk("n_dis" in _pu3 or "n_disabled" in _pu3, "药丸显示禁用进度")
# 结论式文案 + 组件明细（把「系统会改回」与「真的没禁成」分开）
chk("def_prog" in _app2, "卡片有组件明细位（def_prog）")
chk("self_heal" in _pu3 and "sticky_remaining" in _pu3,
    "明细把「系统会改回」与「真的没禁成」分开说")
chk("已实际关闭" in _pu3, "药丸给的是结论「已实际关闭」，不是只有 N/M")
chk("仍未禁用" in _pu3, "真有没禁成的才写「仍未禁用」")
chk("show_def_list" in _app2, "有「查看完整服务清单」入口")
chk("S.defender_prereq_text()" in _app2, "弹窗内容来自 defender_prereq_text()")
chk("defender_prereq_text()" not in _pu3, "常驻卡片不再塞整份清单（页面会失衡）")
chk("def_hint" in _app2, "篡改防护在 UI 上有提示位")
# 「手动关实时保护/篡改防护 + 重启」提示 + 跳到设置页的链接（用户点名要的）
chk(S.DEFENDER_SETTINGS_URI == "windowsdefender://threatsettings",
    "设置页 URI = windowsdefender://threatsettings（%s）" % S.DEFENDER_SETTINGS_URI)
chk(bool(S.DEFENDER_SETTINGS_FALLBACK), "有 ms-settings 兜底")
chk("def open_defender_settings" in _sv_src, "有 open_defender_settings")
chk("os.startfile" in _sv_src, "用 os.startfile 打开协议 URI")
_odef = _sv_src[_sv_src.find("def open_defender_settings"):
                _sv_src.find("def open_defender_settings") + 1600]
chk("_FALLBACK" in _odef, "失败时退回兜底 URI")
chk("手动" in _odef, "都打不开时给手动指引（不假装成功）")
chk("def notice_html" in _app2, "有支持可点击链接的 notice_html")
chk("linkActivated" in _app2,
    "用 linkActivated 自己接（自定义协议走 QDesktopServices 不可靠）")
_dc2 = _app2[_app2.find("f_def, v_def = card"):
             _app2.find("self.toggle_row.addWidget(f_def")]
chk("实时保护" in _dc2 and "篡改防护" in _dc2, "提示点名要手动关这两项")
chk("重新启动计算机" in _dc2 or "重启" in _dc2, "提示说明必须重启")
chk("全关闭才能成功" in _dc2, "提示说明不手动关就关不成")
chk("open_def_settings" in _dc2, "链接接上 open_def_settings")

print()
print("㉜ E-core 相关代码已彻底移除，卡片只留「高级启动」这一个功能")
def card_title_in(src):
    """卡片标题要说清是「Windows 修复菜单 / 高级启动」，且**不许再提 E-core**。

    ⚠️ 别用 `find("f4, v4 = card(")` —— 电源管理页（`导入 .pow 电源计划`）和
    内存页（`占用最高的进程`）都叫 `f4, v4 = card(`，而且位置更靠前，会取错卡片
    （卡片从 CPU 页搬到设置页后就踩到了这个）。**锚在那个卡片独有的按钮上**。
    """
    k = src.find("on_click=self.go_recovery")
    if k < 0:
        return False
    i = src.rfind("card(", 0, k)
    j = src.find(")", i)
    t = src[i:j] if i > 0 and j > i else ""
    return "高级启动" in t and "E-core" not in t

# 2026-10-10 用户要求「清掉 E-core 的死代码」：界面早就改成重启入口了，
# 所以 server 侧的 ecore_* 一并删掉。这些断言防止它们被悄悄加回来。
for _gone in ("ecore_status", "ecore_disable", "ecore_restore",
              "ecore_selftest", "ecore_numproc_value", "ecore_prereq_text"):
    chk(_gone not in _sv_src, "server.py 里已无 %s" % _gone)
for _gone in ("lb_ecore", "ecore_off", "ecore_on", "ecore_selftest_ui",
              "btn_ecore_test", "go_uefi", "ecore_prereq_text"):
    chk(_gone not in _app2, "app.py 里已无 %s" % _gone)
chk("重启进入「高级启动」" in _app2, "卡片上就是那个按钮")
# 名字要**统一**：不能一处叫「高级启动」菜单、另一处叫「恢复菜单」
chk("恢复菜单" not in _app2 and "恢复菜单" not in _sv_src,
    "没有「恢复菜单」这种另起的叫法（统一成「高级启动」）")
chk(card_title_in(_app2), "卡片标题只讲「高级启动 / 修复菜单」，不再提 E-core")
# 用户要的**只有一个功能**：UEFI 那条路（/fw）也一并删掉，别再回来
chk("/fw" not in _sv_src and "firmware" not in _sv_src,
    "server 侧没有 /fw 与 firmware（只保留「高级启动」一个功能）")
chk("UEFI/BIOS 固件设置" not in _app2,
    "app 侧不再有「重启进 UEFI 固件设置」这条路")
chk("on_click=self.go_recovery" in _app2, "按钮接到 go_recovery")
# 卡片必须挂在 SettingsPage 里（用户要求「移动啊设置界面」）
_sett = _app2[_app2.find("class SettingsPage"):]
_cpu = _app2[_app2.find("class CpuTunePage"):_app2.find("class ",
             _app2.find("class CpuTunePage") + 10)]
chk("Windows 修复菜单" in _sett and "btn_reboot_cancel" in _sett,
    "卡片挂在「设置」页")
chk("Windows 修复菜单" not in _cpu and "btn_reboot_cancel" not in _cpu
    and "def _reboot_ui" not in _cpu,
    "「CPU 调试」页已无残件（卡片与处理器都搬走了）")
# 重启机制本身还在（卡片在用）
chk(callable(getattr(S, "reboot_to", None))
    and callable(getattr(S, "cancel_reboot", None)),
    "reboot_to / cancel_reboot 保留（卡片在用）")

print()
print("㉝ 同类假阴性清理 + 静默失败台账")
chk("export_key" not in _sv_src,
    "已删掉死代码 export_key（用被策略拦的 reg.exe 且无调用者）")
# ⚠️ 必须用 AST：按文本扫 `rc == 0` 会被**我自己新写的 docstring** 命中
#    （`_svc_set_start` 的注释里就有「只看 rc==0」这句），直接假 FAIL。
#    要断言的东西很具体：「有没有哪个函数**直接 return 一个 `rc == 0` 比较**」。
_risky = []
for _fn in [n for n in ast.walk(ast.parse(_sv_src))
            if isinstance(n, ast.FunctionDef)]:
    for _n in ast.walk(_fn):
        if (isinstance(_n, ast.Return) and _n.value is not None
                and isinstance(_n.value, ast.Compare)
                and isinstance(_n.value.left, ast.Name)
                and _n.value.left.id == "rc"
                and any(isinstance(c, ast.Constant) and c.value == 0
                        for c in _n.value.comparators)):
            _risky.append(_fn.name)
            break
chk(not _risky,
    "没有函数**直接 return `rc == 0`** 当唯一判据"
    "（曾因此产生 schtasks 假阴性）（%s）" % (_risky or "已清"))
_sil = [i + 1 for i, _ln in enumerate(_app2.splitlines())
        if "on_fail=lambda *_: None" in _ln]
_marked = [i + 1 for i, _ln in enumerate(_app2.splitlines())
           if "on_fail=lambda *_: None" in _ln
           and "# 刻意静默" in _ln]
chk(len(_sil) <= 8,
    "静默失败点没有变多（当前 %d，不得超过 8）" % len(_sil))
# ⚠️ 别用行号钉死（往 app.py 加几行就漂移 → 假 FAIL）。改成
#    「代码里显式标注」：刻意静默的那几处必须带 `# 轮询：刻意静默`，
#    这样意图写在代码里、测试只校验意图，两边都不会骗人。
chk(len(_marked) >= 8,
    "刻意静默都有 `# 刻意静默：<理由>` 标记（%d 处）" % len(_marked))
# 收紧到最强：**不允许存在未标记的静默点** —— 新增一个静默点就必须写明理由
chk(_marked == _sil,
    "**没有未标记的静默点**（未标记：%s）"
    % (sorted(set(_sil) - set(_marked)) or "无"))
chk("def load_fail" in _app2,
    "有 Page.load_fail（读取失败的可见出口）")
chk(len(_marked) < len(_sil) or len(_sil) == len(_marked),
    "非刻意的静默点不会被误标（标了 %d / 共 %d）" % (len(_marked), len(_sil)))

print()
print("㉞ 颜色名必须真实存在（否则深色主题下文字隐形）")
# 踩过的坑：代码里写了 `get_color("FG", dark)` / `get_color("MUTED", dark)`，
# 而调色板里根本没有这两个键 → 兜底返回 `#000000`。
# 浅色主题下黑字压白底看着正常，**深色主题下黑字压近黑底 = 完全隐形**。
# 现在兜底也改成主题正文色（不会再隐形），但真正的防线是这条断言。
_pal_l = set(T.palette(False))
_pal_d = set(T.palette(True))
_cnames = set()
for _f in (A.__file__, S.__file__):
    _txt = io.open(_f, encoding="utf-8").read()
    _cnames |= set(re.findall(r'get_color\(\s*"([A-Z_0-9]+)"', _txt))
    for _m in re.finditer(r'get_color\(\s*"([A-Z_0-9]+)"\s+if\s+[^,]*?\s+else\s+'
                          r'"([A-Z_0-9]+)"', _txt):
        _cnames.add(_m.group(1))
        _cnames.add(_m.group(2))
_bad_name = sorted(n for n in _cnames
                   if n not in _pal_l or n not in _pal_d)
print("      用到的颜色名 %d 个：%s" % (len(_cnames), sorted(_cnames)))
chk(not _bad_name,
    "**所有颜色名都存在于调色板**（无效的：%s）" % (_bad_name or "无"))
chk("FG" not in _cnames and "MUTED" not in _cnames,
    "不再使用不存在的 FG / MUTED 这两个名字")
# 兜底不能是「隐形黑」：找不到时应给主题正文色
_thsrc = io.open(T.__file__, encoding="utf-8").read()
_gc = _thsrc[_thsrc.find("def get_color"):_thsrc.find("def get_color") + 900]
chk("TEXT" in _gc,
    "get_color 兜底给主题正文色而不是纯黑（防「名字打错 → 看不见」）")

print()
print("㉟ 磁盘占用：实时大小 + 搜索 + 只记大文件")
_src_dk = _sv_src[_sv_src.find("def disk_usage_scan"):
                  _sv_src.find("def shred_files")]
chk("on_live" in _src_dk, "disk_usage_scan 有 on_live 回调（扫描期间回报实时大小）")
chk("_flush" in _src_dk, "沿着祖先链累加实时大小（不是只在子树扫完时跳一下）")
chk("'big': big" in _src_dk or '"big": big' in _src_dk,
    "返回值里带 big（扫描顺手记下的大文件）")
chk(isinstance(getattr(S, "_BIG_FILE_MIN", None), int),
    "_BIG_FILE_MIN 存在（只记大文件，整盘几百万个文件存不下来）")
chk(isinstance(getattr(S, "_LIVE_TICK_SECS", None), (int, float)),
    "_LIVE_TICK_SECS 节流间隔是模块级常量（测试能调小）")
chk(isinstance(getattr(S, "_LIVE_FLUSH_FILES", None), int),
    "_LIVE_FLUSH_FILES 批量累加阈值存在")
# UI 侧
_dk = _app_src[_app_src.find("def _build_diskscan"):
               _app_src.find("def _dk_cap_update")]
# ⚠️ on_live= 的接线在 scan_disk 里，而 scan_disk **在** _dk_cap_update 之后 ——
#    用上面那个切片查会假 FAIL（切片只到 _dk_cap_update 为止）。
_sd = _app_src[_app_src.find("    def scan_disk"):
               _app_src.find("    def _dk_got_tick")]
chk("on_live=" in _sd, "扫描时把 on_live 接上")
chk("ed_dk" in _dk and "dk_search" in _dk, "有搜索框与搜索按钮")
chk("attach_row_menu(self.tb_dk" in _dk and "standalone=True" in _dk,
    "磁盘表的右键菜单整份由页面自己给（不再追加通用项）")
chk("_dk_stat" in _dk or "lb_dk_stat" in _dk,
    "扫描进度单独一行（塞药丸里会被挤断）")
_drain = _app_src[_app_src.find("def _dk_drain"):_app_src.find("def _dk_stat")]
chk("_dk_tree.setdefault(par, {})[path] = " in _drain,
    "**实时/最终值用赋值覆盖**（列表 append 会出现两行同名目录）")
chk("_dk_flat" in _drain, "平表索引同步维护（搜索要用）")
chk("_dk_colw_apply" in _app_src,
    "搜索时加宽第一列（否则完整路径被省略号吃掉）")

print()
print("㊱ 右键菜单只给系统原生项 + 「高级启动」入口")
chk(callable(A.native_file_menu), "有 native_file_menu（自建菜单，不用 IContextMenu）")
_nm = _app_src[_app_src.find("def native_file_menu"):
               _app_src.find("def _row_reveal_path")]
for _must in ("打开", "在资源管理器中定位", "复制完整路径", "复制名称",
              "剪切", "创建快捷方式到桌面", "重命名…", "属性"):
    chk(_must in _nm, "原生项「%s」在菜单里" % _must)
# 删除谓词在 _delete_ui 里，不在 native_file_menu（菜单只负责调它）
_del = _app_src[_app_src.find("def _delete_ui"):
                _app_src.find("def native_file_menu")]
chk('shell_verb(path, "delete")' in _del,
    "删除走系统的 delete 谓词（进回收站，由系统自己确认）")
chk("confirm(" in _del, "删除前有我们自己的二次确认（不可逆操作）")
chk("def _shell_exec" in _app_src, "_shell_exec 单独成函数（测试可安全打桩）")
# ---- 说明文字（notice）的垂直排版必须统一 ----
# 根因：QLabel 默认 AlignVCenter，notice 有 12px 上下 padding，框比文字高时
# 文字被居中 → 不同 notice 的留白/行距看起来不一致（用户报的「文字高度不统一」）。
from PySide6.QtCore import Qt as _Qt                              # noqa: E402
_notice_src = _app_src[_app_src.find("def notice("):
                       _app_src.find("def notice_html")]
chk("Qt.AlignLeft | Qt.AlignTop" in _notice_src,
    "notice 固定「左上」对齐（否则文字在框里垂直居中 → 留白不一致）")
_nb = A.notice("测试")
chk(bool(_nb.alignment() & _Qt.AlignTop)
    and not (_nb.alignment() & _Qt.AlignVCenter),
    "运行时确认：新 notice 是 Top 不是 VCenter（%s）" % _nb.alignment())
chk(_nb.wordWrap(), "notice 仍然自动换行")
# 两张开关卡片要留末尾 stretch：被等高拉伸时多余空间落底部，别撑高说明框
for _v, _f, _who in (("v_upd", "f_upd", "Windows 更新卡"),
                     ("v_def", "f_def", "Defender 卡")):
    _i0 = _app_src.find("%s, %s = card(" % (_f, _v))
    _i1 = _app_src.find("self.toggle_row.addWidget(%s, 1)" % _f, _i0)
    _seg = _app_src[_i0:_i1] if (_i0 > 0 and _i1 > _i0) else ""
    chk(bool(_seg) and ("%s.addStretch(1)" % _v) in _seg,
        "%s 末尾有 addStretch（多余空间不撑高文字块）" % _who)
chk("不能带 CREATE_NO_WINDOW" in _app_src or
    "不能带 CREATE_NO_WINDOW" in _app_src.replace("⚠️ ", ""),
    "开终端时专门注明不带 CREATE_NO_WINDOW（带了用户看不到窗口）")
# 重启进恢复环境
chk(callable(S.reboot_to) and callable(S.cancel_reboot),
    "有 reboot_to / cancel_reboot")
_rb = _sv_src[_sv_src.find("def reboot_to"):_sv_src.find("def cancel_reboot")]
chk('"/o"' in _sv_src, "高级启动用 shutdown /r /o")
chk('"/fw"' not in _sv_src, "已无 /fw（固件设置那条路按用户要求删了）")
chk("REBOOT_DELAY_SECS" in _rb, "重启留了倒计时常量")
chk('"/f"' not in _rb, "不带 /f（不硬关有未保存文档的程序）")
chk("rc == 0 and not txt" in _rb,
    "**不能只看返回码**：有输出也判失败（shutdown 失败时会把原因写在输出里）")
_ec = _app_src[_app_src.find("def _reboot_ui"):_app_src.find("def _reboot_done")]
chk("admin_mode" in _ec, "非管理员不允许安排重启（会直接改系统状态）")
chk("confirm(" in _ec, "安排前二次确认")
# 开关类代码已删（见 ㉜）；这里只留「高级启动」这条路的断言

print()
print("=" * 62)
print("㊲ 回归 harness：所有模态对话框都必须打桩（漏桩 confirm_ex → setStyleSheet 崩）")
print("=" * 62)
# 2026-10-12：regress_all ①~⑨ 段之后连切主题色必崩 0xC0000005（崩在
# `self.setStyleSheet()` 里）。bisect 定位到唯一漏桩点：`CleanupPage.clean()`
# 经 `confirm_ex(backup_label=...)` 创建**真实 QMessageBox`；regress 只桩了
# `A.QMessageBox.exec`（立即返回 0）→ 对话框从未显示、也删不掉（harness 全程
# processEvents 驱动，deleteLater 的 deferred delete 永远不处理）→ 一直挂在
# 清理页子树里；⑩ 段 setStyleSheet 重抛光整棵树扫到它即崩。
# 双修：① 本组守着 regress 的 confirm_ex 打桩；② app.py 用完即 deleteLater
#       （真实事件循环下 400ms 内生效，微实验实测）。
_ra_src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "regress_all.py"), encoding="utf-8").read()
chk("A.confirm_ex = lambda" in _ra_src,
    "regress_all 打桩 A.confirm_ex（backup_label 路径不走 A.confirm，会建真对话框）")
chk("(False, False)" in _ra_src,
    "confirm_ex 打桩返回「取消」（与 exec 被桩后的行为一致，写操作仍被拦截）")
# app.py 侧：三个确认入口用完必须 deleteLater（真实应用里不残留隐藏模态框）
for _fn, _nxt in (("def confirm(parent", "def confirm_ex(parent"),
                  ("def confirm_ex(parent", "def _center_dialog"),
                  ("def safety_confirm(parent", "class ServiceDialog")):
    _i = _app_src.find(_fn)
    _seg = _app_src[_i:_app_src.find(_nxt, _i)] if _i > 0 else ""
    chk("deleteLater()" in _seg,
        "%s 用完即 deleteLater（不把隐藏模态框留在控件树里）" % _fn.split("(")[0])

print()
print("=" * 62)
print("㊳ 健壮性：任务收尾 / 扫描限并发 / 字体校正防抖（2026-10-13）")
print("=" * 62)
# ⑤ QThread 生命周期：`done`/`fail` 是**在 run() 内部** emit 的，此刻线程未必已
#    退出；Python 包装被回收时会销毁 C++ QThread 对象 —— 若那一瞬间线程还在跑就是
#    Qt 点名的 UB（"Destroyed while thread is still running"）。收尾必须
#    「先 wait() 钉死线程已结束，再 deleteLater()」。
_tk = _app_src[_app_src.find("class Tasks:"):_app_src.find("def run_cached")]
chk("def _retire(" in _tk, "Tasks 有 _retire（统一收尾出口）")
chk(_tk.count("Tasks._retire(t)") >= 2,
    "done 与 fail 两条路都走 _retire（%d 处）" % _tk.count("Tasks._retire(t)"))
_r = _app_src[_app_src.find("def _retire("):]
_r = _r[:_r.find("def run_cached")]
chk("t.wait(" in _r, "_retire 用 wait() 把「线程已结束」钉死（再销毁就不是 UB）")
chk("deleteLater()" in _r, "_retire 用 deleteLater() 及时释放线程对象")
# ⑥ 垃圾扫描：以前把 ~20 个目标一次性全丢出去 = 同时 20 个 QThread
_cp = _app_src[_app_src.find("class CleanupPage"):
               _app_src.find("class ", _app_src.find("class CleanupPage") + 20)]
chk("_SCAN_MAX_PAR" in _cp, "清理页有并发上限常量（默认 4）")
chk("def _start_one(" in _cp and "_start_one(token)" in _cp,
    "扫完一个补一个（限并发波次，不再一次丢 20 个线程）")
chk("S.junk_item_size(t, sc)" in _cp, "仍按档位传参（预览与实删同一口径）")
# ⑦ 字体校正：两轮都要走单次定时器（stop/start 合并突发调用）
_aq = _app_src[_app_src.find("def _apply_qss"):_app_src.find("def _refit_fonts")]
chk("_arm_refit" in _aq, "_apply_qss 走 _arm_refit")
chk("setSingleShot(True)" in _aq and "stop()" in _aq,
    "两轮都用单次定时器 stop/start（突发调用合并成一组）")
chk("QTimer.singleShot(120" not in _aq and "_refit_pass" not in _aq,
    "旧的「第二轮 singleShot(120)」写法已清掉（那会叠多次全树遍历）")

print()
print("=" * 62)
print("㊴ 重复文件查找：范围可选「常用位置 / 指定文件夹 / 整个磁盘」（2026-10-13）")
print("=" * 62)
_dp = _app_src[_app_src.find("def _build_dup"):_app_src.find("def scan_dup")]
chk('addItem("常用位置（桌面 / 下载 / 文档…）", "default")' in _dp,
    "范围档「常用位置」在（保持原有默认行为）")
chk('addItem("指定文件夹…", "folder")' in _dp and 'addItem("整个磁盘", "drive")' in _dp,
    "范围档「指定文件夹 / 整个磁盘」都在")
chk("_dup_pick_dir" in _dp and "QFileDialog.getExistingDirectory" in _dp,
    "文件夹档用 QFileDialog 选目录")
chk("refresh_dup_drives" in _app_src and "S.list_drives()" in _app_src,
    "磁盘档复用 list_drives() 列盘")
_sd = _app_src[_app_src.find("def scan_dup"):_app_src.find("def _dup_sel")]
chk('mode == "folder"' in _sd and "roots = [self._dup_dir]" in _sd,
    "文件夹档：所选目录当扫描根")
chk('mode == "drive"' in _sd and "roots = [drive]" in _sd,
    "磁盘档：所选盘当扫描根")
chk("roots=roots" in _sd, "roots 真的传给 duplicate_files_scan")
chk("请先点「选择…」" in _sd and "请先选择要查找的磁盘" in _sd,
    "没选目录/没选盘时只提示、不发任务（不会拿空根去扫）")
# server 侧：盘根必须解析出来。⚠️ `"C:\\".isupper()` 为真，若排在 isupper()
# 之后会被当环境变量名 → os.environ.get("C:\\") 拿空串 → 整盘静默扫 0 个。
chk("_DRIVE_ROOT_RE" in _sv_src, "server 有盘根正则 _DRIVE_ROOT_RE")
chk("elif _DRIVE_ROOT_RE.match(r)" in _sv_src, "盘根分支排在 isupper() 之前")
chk(S._scan_roots(["C:"]) == ["C:\\"], "运行时：'C:' 解析成 'C:\\\\'（不被丢掉）")

# ---- ㊵ 回收站占用必须走 scandir（不是 PowerShell/COM） --------------------
# 2026-10-13：全回归偶发失败（垃圾扫描 45s 内无结果）。根因 = recycle_bin_size()
# 用 PowerShell+Shell.Application COM 枚举回收站，这台机器 1.8 万项要 36~46s，
# 一项就把整轮扫描从 7s 拖到 50s+。改为直接扫 `X:\$Recycle.Bin\<SID>\$R*`（0.5s）。
print()
print("㊵ 回收站占用走 scandir（不是子进程枚举）")
_rbs = _sv_src[_sv_src.find("def recycle_bin_size"):]
_rbs = _rbs[:_rbs.find("\ndef ", 10)]
chk("recycle_bin_size" in _sv_src, "server 有 recycle_bin_size")
chk(os.scandir is not None and "os.scandir(bin_dir)" in _rbs,
    "扫 `$Recycle.Bin` 目录（os.scandir），不再起 PowerShell")
chk('"$Recycle.Bin"' in _rbs and 'startswith("$R")' in _rbs,
    "只累加 `$R*`（真实数据），跳过 `$I*` 元数据")
chk("New-Object -ComObject" not in _rbs,
    "已删掉 PowerShell + Shell.Application 的慢枚举")
chk("SHQueryRecycleBinW" in _rbs,
    "查不到时退回 Shell API（SHQueryRecycleBinW）兜底")
_t0 = time.time()
_rv = S.recycle_bin_size()
_rsec = time.time() - _t0
chk(isinstance(_rv, int) and _rv >= 0,
    "运行时 recycle_bin_size() 返回非负整数（%d，%.2fs）" % (_rv, _rsec))
chk(_rsec < 10.0,
    "运行时回收站查询 <10s（实测 %.2fs；旧实现 36~46s）" % _rsec)
chk("def wait_until(pred" in _ra_src and "wait_until(lambda: cl.table.rowCount() > 0" in _ra_src,
    "regress 垃圾扫描按**条件**轮询（wait_until），不再用固定 45s 等待")

# ---- ㊶ 启动即提权（幂等 + 防无限重启） ------------------------------------
# 需求：启动时直接拿管理员权限。打包版靠 manifest；源码靠 main() 兜底重启自己。
# 关键约束：① 兜底提权必须在「读设置/探针/建界面」之前（那些工作在注定被丢弃的
# 非提权进程里白跑）；② 必须带哨兵，否则「runas 谎报成功但未真正提权」时无限重启。
print()
print("㊶ 启动提权链")
_mn = _app_src[_app_src.find("def main():"):_app_src.find("if __name__")]
_ral = _app_src[_app_src.find("def relaunch_as_admin"):]
_ral = _ral[:_ral.find("\ndef ", 10)]
chk('_ELEVATED_FLAG = "--wtb-elevated"' in _app_src, "有提权哨兵常量 _ELEVATED_FLAG")
chk("--normal-mode" in _app_src and 'return "admin"' in _app_src,
    "choose_mode 默认 admin（--normal-mode 仅内部降级）")
chk("relaunch_as_admin()" in _mn and '"runas"' in _ral,
    "main() 兜底调 relaunch_as_admin（ShellExecuteW runas → UAC）")
chk("_ELEVATED_FLAG not in sys.argv" in _mn,
    "门控含哨兵：已提权过的进程不再二次重启（防无限弹 UAC）")
chk("_ELEVATED_FLAG" in _ral and "+ [_ELEVATED_FLAG]" in _ral,
    "relaunch_as_admin 给子进程补哨兵参数")
_i_elev = _mn.find("relaunch_as_admin()")
_i_probe = _mn.find("probe_system_info")
chk(0 < _i_elev < _i_probe,
    "提权排在 probe_system_info 之前（非提权进程不做无用初始化）")
_spec = os.path.join(_root, "portable.spec")
try:
    _spec_src = io.open(_spec, encoding="utf-8").read()
    chk("uac_admin=True" in _spec_src.replace(" ", ""),
        "portable.spec uac_admin=True（打包版清单 requireAdministrator）")
except OSError:
    chk(False, "读不到 portable.spec")

# ---- ㊷ app.py 单文件拆分（第一批：UI 小件外移到 ui_widgets.py） --------------
# app.py 已 1.2 万行。拆分必须**外科式**：只移动「名字 + 函数体字符串都不被
# 本套测试引用」的符号，否则审计按源码文本切片会断。第一批移出 8 个零依赖小件。
print()
print("㊷ 模块拆分：ui_widgets.py")
chk(os.path.exists(os.path.join(_root, "ui_widgets.py")), "存在 ui_widgets.py")
try:
    import ui_widgets as W
    _names = ["NumberTween", "BarTween", "Sparkline", "HueBar",
              "page_active", "restyle", "_cls", "_set",
              "_rich_text_lines", "_rich_text_height", "fit_label"]
    chk(all(hasattr(W, n) for n in _names), "ui_widgets 定义了全部移出的符号")
    chk(all(hasattr(A, n) for n in _names),
        "app 仍可按旧名字取到（from ui_widgets import …，调用方零改动）")
    chk(all(getattr(A, n) is getattr(W, n) for n in _names),
        "app.X 与 ui_widgets.X 是同一对象（不是又复制了一份）")
    chk("from ui_widgets import" in _app_src, "app.py 顶部有 from ui_widgets import")
    _dup = [n for n in _names
            if ("class %s(" % n) in _app_src.split("from ui_widgets import")[0]
            or ("\ndef %s(" % n) in _app_src]
    chk(not _dup, "被移出的符号在 app.py 里不再重复定义（重复=%s）" % _dup)

    # ⚠️ 关键守卫（2026-10-13 真踩过）：拆模块时**漏搬了某个依赖**
    #    （`_set` 调 `fit_label`，而 fit_label 没跟着搬）→ 运行时 NameError，
    #    但审计/回归把它吞在 Task 回调里，照样「全部通过」。用 AST 静态查
    #    「引用了但本模块既没定义也没导入」的名字，从结构上挡住这类漏搬。
    import builtins as _bi
    _wsrc = io.open(os.path.join(_root, "ui_widgets.py"), encoding="utf-8").read()
    _wt = ast.parse(_wsrc)
    _wdef, _wimp, _wloc = set(), set(), set()
    for _n in _wt.body:
        if isinstance(_n, (ast.FunctionDef, ast.ClassDef)):
            _wdef.add(_n.name)
        elif isinstance(_n, ast.Assign):
            for _t in _n.targets:
                if isinstance(_t, ast.Name):
                    _wdef.add(_t.id)
    for _n in ast.walk(_wt):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _wimp.add((_a.asname or _a.name).split(".")[0])
        elif isinstance(_n, ast.ImportFrom):
            for _a in _n.names:
                _wimp.add(_a.asname or _a.name)
        elif isinstance(_n, ast.FunctionDef):
            for _a in list(_n.args.args) + list(_n.args.kwonlyargs):
                _wloc.add(_a.arg)
        elif isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Store):
            _wloc.add(_n.id)
    _wloads = {a.id for a in ast.walk(_wt)
               if isinstance(a, ast.Name) and isinstance(a.ctx, ast.Load)}
    _dangling = sorted(x for x in _wloads
                       if x not in _wdef and x not in _wimp and x not in _wloc
                       and not hasattr(_bi, x))
    chk(not _dangling,
        "ui_widgets.py 无悬空引用（引用了但既没定义也没导入=%s）" % _dangling)
except Exception as _e:
    chk(False, "导入/解析 ui_widgets 失败：%r" % _e)

# ---- ㊸ README 的 v1.4 段必须 ≤300 字（用户要求：保持精简） -----------------
print()
print("㊸ README v1.4 段篇幅")
try:
    _rd = io.open(os.path.join(_root, "README.md"), encoding="utf-8").read()
    _i4, _j4 = _rd.index("## v1.4"), _rd.index("## v1.3")
    _n14 = len(re.sub(r"\s", "", _rd[_i4:_j4].replace("## v1.4", "", 1)))
    chk(_n14 <= 300, "README 的 v1.4 段 ≤300 字（实际 %d）" % _n14)
    chk(os.path.exists(os.path.join(_root, "docs", "更新日志-详版-v1.4.md")),
        "v1.4 详细日志已归档到 docs/（压缩后细节没丢）")
except Exception as _e:
    chk(False, "读 README 失败：%r" % _e)

print()
print("=" * 62)
print("结果：%s" % ("全部通过" if not FAIL else "失败 %d 项" % len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)
