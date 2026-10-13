# -*- coding: utf-8 -*-
"""暂停 → 恢复 的**端到端**验证（走真实的按钮判据与 set_update）。

复现的 bug：`target = not self._upd_enabled` —— 暂停到 2099 时服务没被禁，
`_upd_enabled` 仍是 True，算出 target=False = **又暂停一次**；
而按钮文字写的是「恢复更新」→ 用户点恢复毫无反应。

本脚本验证：
  · 四种状态下的「按钮文字」与「点击后 target」一致（单一判据）
  · 真跑一次 暂停 → 恢复，注册表确实回到原样
退出前一定还原。
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


# ---------- 备份原状 ----------
ORIG = {k: S._reg_str(S._UPD_UX_KEY, k) for k in S._PAUSE_ALL_KEYS}
_orig_noauto = S._reg_dword(S._UPD_AU_KEY, "NoAutoUpdate")
_orig_svc = {n: S._svc_start(n) for n, _a in S._UPDATE_SVCS}
if any(ORIG.values()):
    print("ℹ 进入脚本时本机**处于暂停状态**（用户自己设的），测试结束后原样保留。")


def restore():
    for k, v in ORIG.items():
        try:
            if v:
                S.reg_write(S._UPD_UX_KEY, k, "REG_SZ", v)
            else:
                S.reg_delete(S._UPD_UX_KEY, k)
        except OSError:
            pass


win = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
pg = win.pages["optimize"]

print("[1] 四种状态下「按钮文字」与「点击后的 target」必须一致")
CASES = [
    ("正常", dict(enabled=True, paused=False, paused_long=False,
                  svc_disabled=False, reasons=[]), "暂停"),
    ("暂停到2099", dict(enabled=True, paused=True, paused_long=True,
                        svc_disabled=False,
                        reasons=["更新被暂停到 2099-12-31"]), "恢复"),
    ("服务被禁", dict(enabled=False, paused=False, paused_long=False,
                      svc_disabled=True,
                      reasons=["更新服务 wuauserv 被禁用"]), "恢复"),
    ("普通暂停", dict(enabled=True, paused=True, paused_long=False,
                      svc_disabled=False,
                      reasons=["更新被暂停到 2026-11-07"]), "恢复"),
]
for name, st, want in CASES:
    pg._upd_enabled = bool(st["enabled"])
    pg._upd_reasons = list(st["reasons"])
    pg._upd_status = dict(st)
    pg._paint_toggles()
    btn = pg.btn_upd.text()
    btn_says = "恢复" if "恢复" in btn else "暂停"
    # 实际点击会算出什么（与 toggle_update 用同一判据）
    target = pg._upd_suppressed()
    acts = "恢复" if target else "暂停"
    print("      %-10s 按钮=%r -> 文字说%s / 实际做%s"
          % (name, btn, btn_says, acts))
    chk(btn_says == want, "%s：按钮文字是「%s更新」" % (name, want))
    chk(acts == want,
        "%s：点击后真的执行「%s」（旧代码这里是反的）" % (name, want))

print()
print("[2] 只有一处判据：按钮文字与动作都读 _upd_suppressed")
_src = io.open(A.__file__, encoding="utf-8").read()
_pu = _src[_src.find("def _paint_toggles"):_src.find("def _toggle_done")]
_tu = _src[_src.find("def toggle_update"):_src.find("def toggle_defender")]
chk("_upd_suppressed()" in _pu, "_paint_toggles 用 _upd_suppressed")
chk("_upd_suppressed()" in _tu, "toggle_update 用 _upd_suppressed")
chk("not self._upd_enabled" not in _tu,
    "toggle_update 里已经没有 `not self._upd_enabled` 那个错判据")
chk(_src.count("def _upd_suppressed") == 1, "判据只定义一处")

print()
print("[3] 端到端：真的暂停一次，再真的恢复一次")
try:
    S.set_update(False)
    st = S.update_status()
    chk(st["paused"] is True and st["paused_long"] is True,
        "① 暂停后 status 是 paused_long（到期 %s）" % st["paused_until"])
    pg._upd_enabled = bool(st["enabled"])
    pg._upd_reasons = list(st["reasons"])
    pg._upd_status = dict(st)
    pg._paint_toggles()
    chk(pg.btn_upd.text() == "恢复更新",
        "② 此时按钮显示「恢复更新」（%r）" % pg.btn_upd.text())
    chk(pg._upd_suppressed() is True,
        "③ 判据说「当前是用不了的状态」-> 点击=恢复")

    S.set_update(True)                       # 相当于用户点了「恢复更新」
    st2 = S.update_status()
    chk(st2["paused"] is False, "④ 恢复后不再暂停")
    chk(st2["paused_long"] is False, "⑤ paused_long 复位")
    pg._upd_enabled = bool(st2["enabled"])
    pg._upd_reasons = list(st2["reasons"])
    pg._upd_status = dict(st2)
    pg._paint_toggles()
    chk(pg.btn_upd.text() == "暂停更新",
        "⑥ 按钮回到「暂停更新」（%r）" % pg.btn_upd.text())
    chk(pg._upd_suppressed() is False, "⑦ 判据回到「可用」")
except OSError as e:
    chk(False, "端到端跑挂了：%s" % e)

print()
print("[4] 文案精简：只说暂停到 2099")
_pg_src = _src[_src.find("class OptimizePage"):_src.find("class CpuTunePage")]
chk("暂停到 2099 年" in _pg_src, "卡片说明写「暂停到 2099 年」")
chk("不禁用任何更新服务" not in _pg_src,
    "卡片里不再长篇解释「不禁用服务」（按用户要求精简）")
chk("组策略工具" not in _pg_src, "去掉了那段岔开的补充说明")
_tu2 = _src[_src.find("def toggle_update"):_src.find("def toggle_defender")]
chk("暂停到 2099-12-31" in _tu2, "确认框告知「暂停到 2099-12-31」")
chk("基线检查" not in _tu2 and "驱动更新器" not in _tu2,
    "确认框不再堆技术细节")
chk("立即生效" in _tu2, "恢复那一侧仍说明不用重启")

print()
print("还原…")
restore()
time.sleep(0.2)
# ⚠️ 判据是「**跟进入脚本时一致**」，不是「键都为空」：用户自己可能正把更新
#    暂停着（他就是在测这个开关），那时快照里本来就有值，写回它们才是对的。
_now = {k: S._reg_str(S._UPD_UX_KEY, k) for k in S._PAUSE_ALL_KEYS}
_diff = [k for k in S._PAUSE_ALL_KEYS if _now[k] != ORIG[k]]
chk(not _diff,
    "本机状态已还原成**进入脚本时**的样子（差异项：%s）"
    % ("、".join("%s=%r(原%r)" % (k, _now[k], ORIG[k]) for k in _diff) or "无"))
chk({n: S._svc_start(n) for n, _a in S._UPDATE_SVCS} == _orig_svc,
    "三个服务的 Start 值也与进入时一致（测试没动过服务）")
if any(ORIG.values()):
    print("      注意：进入脚本时本机**本来就是暂停状态**（用户设的），"
          "已原样保留。")

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
win.hide()
win.deleteLater()
os._exit(0 if not FAIL else 1)