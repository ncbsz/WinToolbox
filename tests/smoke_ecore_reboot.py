# -*- coding: utf-8 -*-
"""「重启进入「高级启动」（Windows 修复菜单）」按钮的验证。

**全程不真重启**：`S.run` 打桩，只看下发的命令与返回处理。
"""
import io
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402
import app as A                                        # noqa: E402
import server as S                                     # noqa: E402

A.settings_save = lambda *a, **k: None
qapp = QApplication.instance() or QApplication(sys.argv)
OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


def pump(secs):
    """真泵事件循环 —— Qt 的 QTimer 不会「补跑」漏掉的节拍，
    只 sleep 再 processEvents 一次只会送到一拍。
    """
    t0 = time.time()
    while time.time() - t0 < secs:
        qapp.processEvents()
        time.sleep(0.05)


def pump_until(pred, secs=10.0):
    """泵事件直到 `pred()` 为真（或超时）。

    ⚠️ 凡是**等后台线程回调**的地方都用它，别拍一个固定时长：
    定时器/线程在系统繁忙时会晚于预期完成，固定时长会变成**假 FAIL**
    （实测：与另一个测试并发跑时，pump(0.8) 等不到 Tasks.run 的回调）。
    """
    t0 = time.time()
    while time.time() - t0 < secs:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.03)
    return bool(pred())


print("[1] server 侧：只会用 shutdown /r /o，且不带 /t 0、不带 /f")
calls = []
_orig_run = S.run
S.run = lambda cmd, **kw: (calls.append(list(cmd)), (0, ""))[1]
try:
    r1 = S.reboot_to()
finally:
    S.run = _orig_run
print("      下发：", [" ".join(c) for c in calls])
chk(r1["ok"], "返回成功")
chk(calls[0] == ["shutdown", "/r", "/o", "/t", str(S.REBOOT_DELAY_SECS)],
    "高级启动 = shutdown /r /o /t %d：%s" % (S.REBOOT_DELAY_SECS, calls[0]))
chk(all("/t" in c and c[-1] != "0" for c in calls),
    "**不是 /t 0** —— 必须留出保存工作的时间")
chk(not any("/f" in c for c in calls),
    "不带 /f（有未保存的文档时该让系统自己提示，而不是硬关）")
chk(S.REBOOT_DELAY_SECS >= 20, "倒计时够长（%d 秒）" % S.REBOOT_DELAY_SECS)
# 用户要的**只有一个功能**：UI 已无 UEFI 入口，代码里也不该再有 /fw
chk("/fw" not in io.open(S.__file__, encoding="utf-8").read(),
    "server 侧已没有 /fw（固件那条路彻底删掉）")

print()
print("[2] 失败要如实报，不能只看返回码")
S.run = lambda cmd, **kw: (1, "拒绝访问。\r\n")
try:
    r3 = S.reboot_to()
finally:
    S.run = _orig_run
chk(r3["ok"] is False, "非 0 返回码 → 判定失败")
chk("拒绝访问" in r3["err"], "把系统的原话带出来：%r" % r3["err"])
# 反例：返回码是 0 但有错误输出（PowerShell 那类坑的通用教训）
S.run = lambda cmd, **kw: (0, "某个错误")
try:
    r4 = S.reboot_to()
finally:
    S.run = _orig_run
chk(r4["ok"] is False, "**返回码 0 但有输出** 也判失败（不能只看 rc）")

print()
print("[3] 取消重启：成功 / 没有待执行的重启 两种都要区分")
S.run = lambda cmd, **kw: (0, "")
try:
    c1 = S.cancel_reboot()
finally:
    S.run = _orig_run
S.run = lambda cmd, **kw: (1116, "无法中止系统关机，因为没有正在进行的关机操作。")
try:
    c2 = S.cancel_reboot()
finally:
    S.run = _orig_run
chk(c1["ok"] is True, "有重启在等 → 取消成功")
chk(c2["ok"] is False and "没有正在进行的关机" in c2["err"],
    "本来就没有 → 如实说，不假装成功")

print()
print("[4] 卡片已搬到「设置」页，且只保留「高级启动」这一个功能")
w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
pg = w.pages["settings"]
labels = [b.text() for b in pg.findChildren(type(pg.btn_reboot_cancel))]
chk(any("高级启动" in x for x in labels), "有「重启进入「高级启动」」：%s" % labels)
chk(not any("UEFI" in x for x in labels), "不再有 UEFI 入口（用户只要蓝色的那个）")
chk(any("取消重启" in x for x in labels), "有「取消重启」")
chk(pg.btn_reboot_cancel.isEnabled() is False,
    "一开始「取消重启」不可点（没有待执行的重启）")
# 用户要求「移动啊设置界面」—— 卡片必须真的在设置页，且 CPU 页不再有
chk(hasattr(w.pages["settings"], "btn_reboot_cancel"),
    "卡片在「设置」页")
chk(not hasattr(w.pages["cputune"], "btn_reboot_cancel")
    and not hasattr(w.pages["cputune"], "lb_reboot"),
    "「CPU 调试」页已经彻底没有它了（不留残件）")
chk(not hasattr(w.pages["cputune"], "go_recovery")
    and hasattr(w.pages["settings"], "go_recovery"),
    "处理方法也一起搬走了")
chk(hasattr(w.pages["settings"], "do_cancel_reboot")
    and hasattr(w.pages["settings"], "timer_reboot") is False,
    "取消重启接在设置页（timer 用 getattr 惰性建，未安排时为 None）")

print()
print("[5] 非管理员：不允许安排重启（会直接改系统状态）")
w2 = A.MainWindow(admin_mode=False, dark=False, sysinfo=None)
pg2 = w2.pages["settings"]
warned = {"n": 0}
pg2.win._warn_readonly = lambda *a, **k: warned.__setitem__("n", warned["n"] + 1)
ran = {"n": 0}
_orig_task = A.Tasks.run
A.Tasks.run = lambda *a, **k: ran.__setitem__("n", ran["n"] + 1) or None
A.confirm = lambda *a, **k: True
try:
    pg2._reboot_ui()
finally:
    A.Tasks.run = _orig_task
chk(warned["n"] == 1, "非管理员下弹了「只读模式」提示")
chk(ran["n"] == 0, "**没有真的去下发重启**")

print()
print("[6] 用户点「否」时不下发")
A.confirm = lambda *a, **k: False
ran["n"] = 0
A.Tasks.run = lambda *a, **k: ran.__setitem__("n", ran["n"] + 1) or None
try:
    pg._reboot_ui()
finally:
    A.Tasks.run = _orig_task
chk(ran["n"] == 0, "确认框点「取消」→ 什么都不做")

print()
print("[7] 安排成功后：倒计时在走、取消按钮可用、文案说清后果")
A.confirm = lambda *a, **k: True
S.reboot_to = lambda delay=None: {"ok": True,
                                 "name": "「高级启动」", "delay": 3, "err": ""}
pg._reboot_ui()
pump_until(lambda: pg.btn_reboot_cancel.isEnabled())
chk(pg.btn_reboot_cancel.isEnabled(), "「取消重启」变可点")
chk("已安排重启" in pg.lb_reboot.text(), "提示文案：%r" % pg.lb_reboot.text())
chk("秒后执行" in pg.lb_reboot.text(), "写明倒计时")
chk(not pg.lb_reboot.isHidden(), "提示可见")
pump(3.4)
chk(pg.lb_reboot.text() == "正在重启…", "倒计时走完 → 显示「正在重启…」"
    "（实际 %r）" % pg.lb_reboot.text())
chk(pg.btn_reboot_cancel.isEnabled() is False,
    "倒计时结束后不能再点取消（窗口已经过了）")

print()
print("[8] 取消：成功后按钮复位、文案复位")
S.cancel_reboot = lambda: {"ok": True, "err": ""}
# ⚠️ 别用「按钮变不可点」当条件 —— [7] 倒计时走完时它**本来**就不可点了，
#    条件会立刻为真、等于没等。要等的是「取消回调真的跑完」（文案变了）。
pg.do_cancel_reboot()
pump_until(lambda: "已取消" in pg.lb_reboot.text())
chk(pg.btn_reboot_cancel.isEnabled() is False, "取消后按钮回到不可点")
chk("已取消" in pg.lb_reboot.text(), "文案：%r" % pg.lb_reboot.text())

print()
print("[9] 取消失败（本来就没了）—— 如实说，不假装成功")
S.cancel_reboot = lambda: {"ok": False, "err": "没有正在进行的关机操作。"}
pg.do_cancel_reboot()
pump_until(lambda: "没有取消成功" in pg.lb_reboot.text())
chk("没有取消成功" in pg.lb_reboot.text(), "文案：%r" % pg.lb_reboot.text())

print()
print("[10] 下发失败时，把「手工命令」明确写给用户")
shown = {}
A.warn = lambda parent, a, b=None, **k: shown.update(t=a, b=b)
S.reboot_to = lambda delay=None: {"ok": False, "name": "「高级启动」", "delay": 30,
                                 "err": "拒绝访问。"}
pg._reboot_ui()
pump_until(lambda: bool(shown))
print("      弹出：", shown)
chk(shown.get("t") == "重启没能安排成功", "标题写明失败")
chk("shutdown /r /o /t 30" in (shown.get("b") or ""),
    "正文给出可直接抄的命令：%r" % (shown.get("b") or "")[:60])
chk("拒绝访问" in (shown.get("b") or ""), "带上系统原话")

print()
print("[11] 卡片上的说明文字：说清这就是「修复菜单」，且不再提 E-core")
_ap11 = io.open(os.path.join(os.path.dirname(S.__file__), "app.py"),
                encoding="utf-8").read()
_card_txt = _ap11[_ap11.find('f4, v4 = card("Windows 修复菜单（「高级启动」）")'):
                  _ap11.find("self.btn_reboot_cancel = btn(")]
for must in ("Windows 恢复环境", "多次启动失败", "修复", "高级启动",
             "启动修复", "安全模式", "命令提示符"):
    chk(must in _card_txt, "卡片说明提到「%s」" % must)
chk("E-core" not in _card_txt and "bcdedit" not in _card_txt,
    "卡片里**不再出现** E-core / bcdedit（用户要求全部删掉）")

print()
print("[12] 卡片只剩「高级启动」一个功能（E-core 代码已全删）")
_app = io.open(os.path.join(os.path.dirname(S.__file__), "app.py"),
               encoding="utf-8").read()
chk("重启进入「高级启动」" in _app, "卡片上有那个按钮")
chk("恢复菜单" not in _app, "没有「恢复菜单」这种另起的叫法（名字已统一）")
chk("on_click=self.go_recovery" in _app, "按钮接到 go_recovery")
chk("btn_ecore_test" not in _app, "自检按钮已移除")
chk("lb_ecore" not in _app, "状态标签已移除")
chk("取消重启" in _app, "「取消重启」保留（倒数里能反悔）")
chk(not hasattr(S, "ecore_disable") and not hasattr(S, "ecore_status"),
    "server 侧的 ecore_* 已删除")
chk("E-core" not in _app, "app.py 里已找不到 E-core 这个词")

w.hide()
w2.hide()
w.deleteLater()
w2.deleteLater()

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)
