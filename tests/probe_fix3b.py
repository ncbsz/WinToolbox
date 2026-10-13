# -*- coding: utf-8 -*-
"""② Defender 进度反馈 + ③ 禁用记录随档案迁移 —— 运行时验证（全部打桩）。"""
import io
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

import app as A

A.settings_save = lambda *a, **k: None
q = QApplication.instance() or QApplication(sys.argv)
OK = [0]
BAD = []


def ck(cond, label):
    if cond:
        OK[0] += 1
    else:
        BAD.append(label)
    print("  %s %s" % ("ok  " if cond else "FAIL", label))


def pump(secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        q.processEvents()
        time.sleep(0.03)


def until(pred, secs=12.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        q.processEvents()
        if pred():
            return True
        time.sleep(0.03)
    return bool(pred())


w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
w.resize(1200, 900)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()
A.info = lambda *a, **k: None      # _toggle_done 会弹模态框，不打桩就会卡住
A.warn = lambda *a, **k: None
pump(1.8)

# ------------------------------------------------------- ② ProgressSpin
print("=== ② 后台进度 → spinner ===")
state = {}
spin = A.ProgressSpin(w, state, "正在干活…")
pump(0.3)
ck("正在干活" in w.spinner.text(), "初始文案已显示（%r）" % w.spinner.text())
state["msg"] = "禁用服务/驱动 3/8：WdNisDrv（驱动）…"
ck(until(lambda: "3/8" in w.spinner.text(), 3),
   "worker 写的进度被主线程定时器读到（%r）" % w.spinner.text())
state["msg"] = "复核（逐个回读）…"
ck(until(lambda: "复核" in w.spinner.text(), 3), "进度会持续更新")
spin.stop()
pump(0.3)
ck(w.spinner.text() == "", "stop() 后 spinner 清空（定时器已停）")
ck(not spin._t.isActive(), "定时器确实停了（不常驻）")

print()
print("=== ② Defender 端到端：进度真的打到 spinner ===")
op = w.pages["optimize"]
w.goto("optimize")
pump(1.2)
calls = []
_ob = w.busy
w.busy = lambda m="": (calls.append(m), _ob(m))[1]
_sd, _oc, _od = A.S.set_defender, A.confirm, A.S.defender_status
A.S.defender_status = lambda: {"enabled": False, "realtime": False,
                               "antivirus": False, "tamper": False,
                               "effectively_off": True, "n_disabled": 8,
                               "n_total": 8, "remaining": [], "self_heal": [],
                               "sticky_remaining": []}
op._def_status = A.S.defender_status()
op._def_enabled = False


def fake_set_defender(enabled, progress=None):
    for m in ("禁用服务/驱动 1/8：WinDefend（服务）…",
              "禁用服务/驱动 8/8：WaaSMedicSvc（服务）…",
              "禁用计划任务 4/4：Verification…"):
        if progress is not None:
            progress(m)
        time.sleep(0.12)
    return [{"name": "WinDefend（服务）", "ok": True, "note": "stub"}]


A.S.set_defender = fake_set_defender
A.confirm = lambda *a, **k: True
try:
    op.toggle_defender()
    until(lambda: any("WinDefend（服务）" in str(c) for c in calls), 20)
    pump(0.8)
    got = [c for c in calls if "禁用服务/驱动" in str(c) or "计划任务" in str(c)]
    print("     捕获到的进度文案：")
    for c in got[-4:]:
        print("       ", c)
    # 进度按 120ms 节流采样 —— 中间几条被跳过是正常的，不能要求每条都到
    ck(len(got) >= 2, "进度持续可见（捕获 %d 条）" % len(got))
    ck(any("禁用服务/驱动" in str(c) for c in calls), "组件阶段的进度打到了 spinner")
    ck(any("8/8" in str(c) for c in calls), "最后一个组件也报到了")
    ck(any("计划任务" in str(c) for c in calls), "计划任务阶段也报进度")
    ck(w.spinner.text() == "", "任务结束后 spinner 清空（不会一直挂着）")
finally:
    A.S.set_defender, A.confirm = _sd, _oc
    A.S.defender_status = _od
    w.busy = _ob

# ------------------------------------------------------- ③ 记录随档案走
print()
print("=== ③ 禁用记录 → 档案 → 导入补做 ===")
se = w.pages["settings"]
tmpd = tempfile.mkdtemp()
prof_path = os.path.join(tmpd, "prof.json")
arch = A.S.profile_build({"dark": False}, "1.4")
# 造一份「服务 + 启动项」的禁用记录（模拟从另一台机器带过来）
arch["own_reg"] = {"v": [], "k": {"DisabledByTool": {"v": [], "k": {
    "service": {"v": [["0", "REG_SZ", json.dumps({"name": "ZZProbeSvc"})]], "k": {}},
    "startup": {"v": [["0", "REG_SZ", json.dumps(
        {"hive": "HKCU", "sub": "Software\\ZZProbe\\Run",
         "name": "ZZProbeApp", "folder": False})]], "k": {}},
    "task": {"v": [], "k": {}}}}}}
with io.open(prof_path, "w", encoding="utf-8") as f:
    json.dump(arch, f, ensure_ascii=False)

_oc2, _obk, _osa, _ofd = A.confirm, A.S.profile_backup_current, A.S.snapshot_and_apply, A.QFileDialog
_oset = (A.S.set_service, A.S.set_startup_enabled, A.S.set_task)
_box = {}
_applied = []


class FD:
    @staticmethod
    def getOpenFileName(*a, **k):
        return (prof_path, "")

    @staticmethod
    def getSaveFileName(*a, **k):
        return (prof_path, "")


A.QFileDialog = FD
A.S.profile_backup_current = lambda *a, **k: "(打桩)"
A.info = lambda parent, t, m=None, **k: _box.update(title=t, msg=m)
A.warn = lambda parent, t, m=None, **k: _box.update(title="WARN", msg=m)
A.S.set_service = lambda n, a: (_applied.append(("service", n, a)), (True, "stub"))[1]
A.S.set_startup_enabled = lambda h, s, n, folder=False, on=True: (
    _applied.append(("startup", n, on)),
    {"ok": True, "verified": True, "msg": "stub"})[1]
A.S.set_task = lambda f, e: (_applied.append(("task", f, e)), (True, "stub"))[1]
try:
    _n = [0]

    def confirm_all(*a, **k):
        _n[0] += 1
        return True

    A.confirm = confirm_all
    se.import_cfg()
    pump(0.5)
    print("     实际执行:", _applied)
    print("     完成弹窗尾部:", str(_box.get("msg"))[-160:].replace("\n", " | "))
    ck(any(x[0] == "service" and x[1] == "ZZProbeSvc" and x[2] == "disable"
           for x in _applied), "档案里的服务记录被照着禁用了")
    ck(any(x[0] == "startup" and x[1] == "ZZProbeApp" and x[2] is False
           for x in _applied), "启动项记录也被照做了（on=False）")
    ck("补禁用" in str(_box.get("msg")), "完成提示里报了补禁用结果")

    # 取消 → 一项都不动
    _applied.clear()
    _box.clear()
    _m = [0]

    def confirm_second_false(parent=None, title="", *a, **k):
        # 只拒绝「补禁用」那一次 —— 按标题判，比按调用次序稳
        return "禁用" not in str(title)

    A.confirm = confirm_second_false
    se.import_cfg()
    pump(0.4)
    print("     取消后实际执行:", _applied)
    ck(not _applied, "在禁用记录提示里选「取消」→ 一个字节都不改")
    ck("没有" in str(_box.get("msg")), "取消时如实说明")
finally:
    A.confirm, A.S.profile_backup_current = _oc2, _obk
    A.S.snapshot_and_apply = _osa
    A.S.set_service, A.S.set_startup_enabled, A.S.set_task = _oset
    A.QFileDialog = _ofd
    try:
        os.remove(prof_path)
        os.rmdir(tmpd)
    except OSError:
        pass

# 收尾：把测试写进 HKCU 的记录清掉
print()
print("=== 收尾（清掉测试写进注册表的记录）===")
A.S.disabled_record_del("service", name="ZZProbeSvc")
A.S.disabled_record_del("startup", hive="HKCU", sub="Software\\ZZProbe\\Run",
                        name="ZZProbeApp", folder=False)
left = A.S.disabled_records()
ck(not any(left.values()), "记录已清空（%s）" % {k: len(v) for k, v in left.items()})
ck(not A.S.own_state_export(), "own_state_export 回到空（无残留）")

print()
print("结果：%s（ok %d / fail %d）" % ("全通过" if not BAD else "有失败",
                                     OK[0], len(BAD)))
for b in BAD:
    print("   -", b)
w.hide()
w.deleteLater()
sys.stdout.flush()
os._exit(0 if not BAD else 1)
