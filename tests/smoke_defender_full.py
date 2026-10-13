# -*- coding: utf-8 -*-
"""Defender 「全关闭」的验证。

⚠️ **本机 Defender 本来就是全关的**（用户自己关的），所以：
  · 真跑「关闭」只会重复写同样的值（幂等），但会**多写几个策略键** →
    仍然改变用户状态。所以**关闭/恢复两个方向都用打桩**验证逻辑与返回结构，
    只对**只读**部分（status / 清单 / 快照读写）做真跑。
  · 绝不真跑「恢复」—— 那会把用户的 Defender 打开。
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S                                          # noqa: E402

OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


# 备份可能被测试碰到的文件
_snap_existed = os.path.exists(S._DEF_SNAP)
_snap_backup = None
if _snap_existed:
    _snap_backup = io.open(S._DEF_SNAP, encoding="utf-8").read()


def cleanup():
    try:
        if _snap_existed and _snap_backup is not None:
            with open(S._DEF_SNAP, "w", encoding="utf-8") as f:
                f.write(_snap_backup)
        elif os.path.exists(S._DEF_SNAP):
            os.remove(S._DEF_SNAP)
    except OSError:
        pass


print("[1] 全关闭清单的完整性与正确性")
comp = {n: (k, d, x) for n, k, d, x in S._DEF_COMPONENTS}
print("      8 个组件：")
for n, k, d, desc in S._DEF_COMPONENTS:
    print("        %-24s %-4s 默认 Start=%s  %s" % (n, k, d, desc))
chk(len(S._DEF_COMPONENTS) == 8, "清单是 8 个组件（%d）" % len(S._DEF_COMPONENTS))
for n in ("WinDefend", "WdNisSvc", "WdNisDrv", "WdFilter", "WdBoot", "Sense",
          "SecurityHealthService", "WaaSMedicSvc"):
    chk(n in comp, "清单含 %s" % n)
# ⚠️ WdBoot / WdFilter 默认是 0（Boot）不是 3 —— 写错会让启动期驱动不加载
chk(comp["WdBoot"][0 if False else 0] is not None, "WdBoot 有条目")
chk([d for _n, _k, d, _x in S._DEF_COMPONENTS
     if _n in ("WdBoot", "WdFilter")] == [0, 0],
    "WdBoot / WdFilter 的默认 Start 是 0（Boot 启动）")
chk(dict((n, d) for n, _k, d, _x in S._DEF_COMPONENTS)["WinDefend"] == 2,
    "WinDefend 默认 Start=2（自动）")
chk(len(S._DEF_TASKS) == 4, "4 个计划任务：%s" % len(S._DEF_TASKS))
chk(all("Windows Defender" in t for t in S._DEF_TASKS),
    "4 个任务都在 \\Windows Defender\\ 路径下")

print()
print("[2] 「前置」清单能说清楚（用户明确要这份说明）")
txt = S.defender_prereq_text()
print("      ------ 清单文本 ------")
for line in txt.splitlines():
    print("      " + line)
print("      ----------------------")
chk("SecurityHealthService" in txt, "点出了 SecurityHealthService")
chk("只读" in txt, "说明它的键对管理员只读")
chk("SCM" in txt, "说明本工具改走 SCM")
chk("WaaSMedicSvc" in txt, "点出了 WaaSMedicSvc")
chk("改回来" in txt, "说明不禁它会被自动改回来")
chk("安全模式" in txt and "重启" in txt, "说明了重启 / 安全模式")

print()
print("[3] 快照：已禁用的值不能当成「原值」")
cleanup()
saved, n_skip = S._def_snapshot_save()
print("      save ->", saved, " 跳过（已禁用）:", n_skip)
if os.path.exists(S._DEF_SNAP):
    d = json.load(io.open(S._DEF_SNAP, encoding="utf-8"))
    print("      快照内容:", d)
    chk(d.get("svcs") is not None, "快照里有 svcs")
    chk(all(v != 4 for v in (d.get("svcs") or {}).values()),
        "**快照里没有 4**（已禁用的值不当原值，否则恢复会静默写回禁用）")
    chk(len(d.get("skipped_disabled") or []) == n_skip,
        "跳过的组件数一致（%s）" % n_skip)
vals, guessed, note = S._def_snapshot_restore_values()
print("      restore -> guessed=%s  note=%s" % (guessed, note))
chk(all(v in (0, 1, 2, 3) for v in vals.values()),
    "恢复值都是合法启动类型（不是 4）")
if S.is_admin() or True:
    # 本机 7/8 已是禁用 → 必须回落到默认值并说明
    if n_skip >= 7:
        chk(guessed is True, "已禁用组件占多数时 marked as「按默认值」（%s）" % guessed)
        chk("默认值" in note or "手工关过" in note, "note 说明了原因（%s）" % note)

print()
print("[4] 幂等：第二次点「关闭」不该覆盖快照")
again, _ = S._def_snapshot_save()
chk(again is False, "已有快照时不再覆盖（%s）" % again)

print()
print("[5] set_defender(False) 逻辑（**全程打桩，不碰系统**）")
# ⚠️ 先删掉第 3 节留下的快照 —— 否则 `_def_snapshot_save()` 会因为
#    「已有快照」而跳过，「原值快照」这条结果就不会出现（写成假 FAIL）。
cleanup()
calls = {"svc": [], "write": [], "delete": [], "task": []}
_o = (S._svc_set_start, S.reg_write, S.reg_delete, S.set_task, S.defender_status)


def f_svc(n, s):
    calls["svc"].append((n, s))
    return True, "stub"


def f_wr(k, v, t, d):
    calls["write"].append((k, v, d))


def f_del(k, v):
    calls["delete"].append((k, v))


def f_task(n, e):
    calls["task"].append((n, e))
    return True, "stub"


try:
    S._svc_set_start = f_svc
    S.reg_write = f_wr
    S.reg_delete = f_del
    S.set_task = f_task
    S.defender_status = lambda: {"realtime": False, "tamper": False,
                                 "n_disabled": 8, "n_total": 8, "remaining": []}
    res = S.set_defender(False)
finally:
    (S._svc_set_start, S.reg_write, S.reg_delete, S.set_task,
     S.defender_status) = _o

names = [r.get("name") for r in res]
for r in res:
    print("      · %-40s ok=%-5s %s"
          % (r.get("name"), r.get("ok"), str(r.get("note"))[:40]))
chk(len(calls["svc"]) == 8, "8 个服务/驱动都被设了 Start（%d）" % len(calls["svc"]))
chk(all(v == 4 for _n, v in calls["svc"]),
    "**全部设成 4（禁用）**，没有漏掉写成别的值")
chk(len(calls["task"]) == 4, "4 个计划任务都被禁用（%d）" % len(calls["task"]))
chk(all(e is False for _n, e in calls["task"]), "计划任务是「禁用」方向")
wkeys = [(k.rsplit("\\", 1)[-1], v) for k, v, _d in calls["write"]]
print("      写入的键值：", wkeys)
chk(any("DisableAntiSpyware" == v for _k, v in wkeys),
    "写了 DisableAntiSpyware=1")
chk(any("DisableRealtimeMonitoring" == v for _k, v in wkeys),
    "写了实时保护禁用策略")
chk(any("DisableAntiVirus" == v for _k, v in wkeys),
    "写了 DisableAntiVirus=1")
chk(any("复核" in (n or "") for n in names), "有回读复核项")
chk(any("原值快照" in (n or "") for n in names), "记录了原值快照")

print()
print("[6] set_defender(True) 逻辑（打桩）：按快照/默认值恢复 + 清理")
calls2 = {"svc": [], "write": [], "delete": [], "task": []}
try:
    S._svc_set_start = lambda n, s: (calls2["svc"].append((n, s)) or (True, "stub"))
    S.reg_write = lambda *a: None
    S.reg_delete = lambda k, v: calls2["delete"].append((k, v))
    S.set_task = lambda n, e: (calls2["task"].append((n, e)) or (True, "stub"))
    S.defender_status = lambda: {"realtime": True, "tamper": False,
                                 "n_disabled": 0, "n_total": 8, "remaining": []}
    res2 = S.set_defender(True)
finally:
    (S._svc_set_start, S.reg_write, S.reg_delete, S.set_task,
     S.defender_status) = _o

for r in res2:
    print("      · %-40s ok=%-5s %s"
          % (r.get("name"), r.get("ok"), str(r.get("note"))[:40]))
chk(len(calls2["svc"]) == 8, "8 个组件都恢复了 Start")
chk(all(v != 4 for _n, v in calls2["svc"]),
    "恢复时没有任何一个写成 4（%s）" % [v for _n, v in calls2["svc"]])
gone = [v for _k, v in calls2["delete"]]
chk("DisableAntiSpyware" in gone, "删了 DisableAntiSpyware")
chk("DisableRealtimeMonitoring" in gone, "删了实时保护禁用策略")
chk("ServiceStartStates" in gone,
    "删了 ServiceStartStates（留着会让 Defender 干脆不启动）")
chk(len(calls2["task"]) == 4 and all(e is True for _n, e in calls2["task"]),
    "4 个计划任务都重新启用")
chk(any("复核" in (r.get("name") or "") for r in res2), "恢复也有回读复核")

print()
print("[7] 篡改防护：不谎报成功")
try:
    S._svc_set_start = lambda n, s: (False, "被拒绝")
    S.reg_write = lambda *a: None
    S.reg_delete = lambda *a: None
    S.set_task = lambda n, e: (False, "被拒绝")
    S.defender_status = lambda: {"realtime": True, "tamper": True,
                                 "n_disabled": 0, "n_total": 8, "remaining": []}
    res3 = S.set_defender(False)
finally:
    (S._svc_set_start, S.reg_write, S.reg_delete, S.set_task,
     S.defender_status) = _o
tp = [r for r in res3 if "篡改防护" in (r.get("name") or "")]
chk(bool(tp), "篡改防护开启且写失败时给出专门提示")
chk(bool(tp) and tp[0].get("ok") is False, "该提示是失败态（不谎报）")

print()
print("[8] defender_status 报「全关闭进度」")
try:
    S.defender_status = _o[4]
finally:
    pass
st = S.defender_status()
print("      ->", {k: st.get(k) for k in
                   ("realtime", "tamper", "n_disabled", "n_total", "remaining")})
chk(set(("disabled", "remaining", "n_disabled", "n_total")) <= set(st),
    "status 含 disabled/remaining/n_disabled/n_total")
chk(st["n_disabled"] == len([n for n, v in st["svc_starts"].items() if v == 4]),
    "n_disabled 与逐个读到的 Start 一致")

print()
print("[9] UI 文案与判据")
_app = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
_pu = _app[_app.find("def _paint_toggles"):_app.find("def _toggle_done")]
_ti = _app.find("def toggle_defender")
_tj = _app.find("\n    def ", _ti + 10)
_tu = _app[_ti:_tj if _tj > 0 else _ti + 3200]
chk("_def_suppressed()" in _pu and "_def_suppressed()" in _tu,
    "按钮文字与动作读同一判据")
chk("n_disabled" in _pu or "n_dis" in _pu, "药丸显示禁用进度")
chk("8 个服务/驱动" in _tu or "8 个服务" in _tu, "确认框列出 8 个服务/驱动")
chk("SecurityHealthService" in _tu, "确认框点出 SecurityHealthService")
chk("WaaSMedicSvc" in _tu, "确认框点出 WaaSMedicSvc")
chk("安全模式" in _tu, "确认框提到安全模式")
chk("PPL" in _tu, "确认框说明 PPL 自我保护")
chk("篡改防护" in _tu and "手动关闭" in _tu,
    "确认框里也提醒要先手动关实时保护/篡改防护")
chk("defender_prereq_text" in _app, "清单文本来自 server.defender_prereq_text()")
chk("show_def_list" in _app, "有「查看完整服务清单」入口（不用用户去猜）")

print()
print("[10] 从上一版测试继承的、仍然有效的断言")
_src = io.open(S.__file__, encoding="utf-8").read()
chk(S._DEF_RT_VALUE == "DisableRealtimeMonitoring",
    "_DEF_RT_VALUE 指向 DisableRealtimeMonitoring")
chk("Real-Time Protection" in S._DEF_RT_KEY,
    "_DEF_RT_KEY 在 Real-Time Protection 子键下")
chk("DisableAntiSpyware" in _src and "已忽略" in _src,
    "注释里点明 DisableAntiSpyware 在客户端 SKU 已被忽略（不再是唯一手段）")
chk("def _mp_run" in _src and "def _mp_ok" in _src,
    "有 _mp_run / _mp_ok（拿 Get-MpComputerStatus 的值）")
chk("def _svc_set_start_scm" in _src,
    "有 SCM 写入路径（受保护服务键的绕行方案）")
chk("ChangeServiceConfigW" in _src, "用 ChangeServiceConfig 而不是改 ACL")
chk("取得所有权" in _src or "ACL" in _src,
    "注释说明了为什么走 SCM 而不是改 ACL（改 SD 会留下更松的权限）")
chk("def_hint" in _app, "篡改防护在 UI 上有提示位")

print()
print("[11] 「手动关实时保护/篡改防护 + 重启」提示（用户点名要的文案）")
chk(S.DEFENDER_SETTINGS_URI == "windowsdefender://threatsettings",
    "主 URI 是 windowsdefender://threatsettings（%s）" % S.DEFENDER_SETTINGS_URI)
chk(bool(S.DEFENDER_SETTINGS_FALLBACK), "有兜底 URI（%s）"
    % S.DEFENDER_SETTINGS_FALLBACK)
# 打桩验证「先试主 URI、失败再兜底、全失败给手动指引」
_opened = []
_o_sf = S.os.startfile
try:
    S.os.startfile = lambda u: (_opened.append(u), None)[1]
    ok1, m1 = S.open_defender_settings()
    chk(ok1 and m1 == S.DEFENDER_SETTINGS_URI, "成功时走主 URI（%s）" % m1)
    _opened.clear()

    def _boom(u):
        _opened.append(u)
        raise OSError("未注册该协议")
    S.os.startfile = _boom
    ok2, m2 = S.open_defender_settings()
    chk(ok2 is False, "两个都失败时如实返回失败")
    chk(_opened == [S.DEFENDER_SETTINGS_URI, S.DEFENDER_SETTINGS_FALLBACK],
        "按「主 → 兜底」的顺序试（%s）" % _opened)
    chk("手动" in m2 and "病毒和威胁防护" in m2,
        "失败时告诉用户手动去哪一页（%s）" % m2[:40])
finally:
    S.os.startfile = _o_sf

# UI：那段提示必须真的在卡片上，并且链接接上了处理函数
chk("def notice_html" in _app, "有支持可点击链接的 notice_html")
_dc = _app[_app.find('f_def, v_def = card'):
           _app.find('self.toggle_row.addWidget(f_def')]
chk("实时保护" in _dc and "篡改防护" in _dc,
    "提示里点名要手动关「实时保护」和「篡改防护」两项")
chk("重新启动计算机" in _dc or "重启" in _dc,
    "提示里说明必须重启才完全生效")
chk("全关闭才能成功" in _dc, "提示里说明「不手动关就关不成」")
chk("open_def_settings" in _dc, "链接接到 open_def_settings")
chk("def open_def_settings" in _app, "有 open_def_settings 方法")
chk("linkActivated" in _app or "setOpenExternalLinks" in _app,
    "用 linkActivated 自己接（自定义协议走 QDesktopServices 不可靠）")
_od = _app[_app.find("def open_def_settings"):_app.find("def show_def_list")]
chk("S.open_defender_settings()" in _od, "open_def_settings 调 server 侧函数")
chk("warn(" in _od, "打不开时给用户提示（不静默）")

print()
print("[12] 「关了却像没生效」的修正：结论字段 + 把系统会改回的挑出来")
# 用户 2026-10-12 反馈：全关闭之后看到「7/8 已禁」以为没生效 —— 差的那 1 个是
# WaaSMedicSvc，Windows 的更新医疗会定期把它恢复成手动，属正常现象。
_d2 = S.defender_status()
for _k in ("self_heal", "sticky_remaining", "effectively_off"):
    chk(_k in _d2, "defender_status 有 %s" % _k)
chk(bool(getattr(S, "_DEF_SELF_HEAL", None))
    and "WaaSMedicSvc" in S._DEF_SELF_HEAL,
    "WaaSMedicSvc 被认定为「Windows 会自己改回」")
chk(set(_d2.get("remaining") or []) ==
    set(_d2.get("self_heal") or []) | set(_d2.get("sticky_remaining") or []),
    "self_heal + sticky_remaining 恰好等于 remaining（不漏不重）")
chk(all(n in S._DEF_SELF_HEAL for n in (_d2.get("self_heal") or [])),
    "self_heal 里的都在清单内")
chk(all(n not in S._DEF_SELF_HEAL for n in (_d2.get("sticky_remaining") or [])),
    "sticky_remaining 与清单不重叠")
chk(_d2.get("effectively_off") in (True, False, None),
    "effectively_off 是三态（读不到为 None，不退化成 False）")
_prq2 = S.defender_prereq_text()
chk("WaaSMedicSvc" in _prq2 and "不是本工具没写成" in _prq2,
    "前置清单里说清「它会自己变回去，不是我们没写成」")
_pu2 = _app[_app.find("def _paint_toggles"):_app.find("def _toggle_done")]
chk("def_prog" in _app, "卡片有组件明细位（def_prog）")
chk("self_heal" in _pu2 and "sticky_remaining" in _pu2,
    "明细把「系统会改回」与「真的没禁成」分开说")
chk("已实际关闭" in _pu2, "药丸给结论式文案（不是只有 N/M）")
chk("仍未禁用" in _pu2, "只有真没禁成才写「仍未禁用」")

print()
print("清理测试快照…")
cleanup()
chk(not os.path.exists(S._DEF_SNAP) or _snap_existed,
    "测试产生的快照已清理（%s）" % os.path.exists(S._DEF_SNAP))

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)