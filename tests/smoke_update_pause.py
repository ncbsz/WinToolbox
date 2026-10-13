# -*- coding: utf-8 -*-
"""「暂停更新改成暂停到 2099」的定向验证。

⚠️ 会写HKLM\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings —— 那是
**用户态配置键**（不是策略键），改它不会让系统拒绝更新、也不需要管理员。
但为了安全，本脚本**只测写入与恢复，最后一定还原成初始状态**。
"""
import io
import os
import sys
import time

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


# ---------- 先备份原状，退出时还原 ----------
ORIG = {}
for _k in S._PAUSE_ALL_KEYS:
    ORIG[_k] = S._reg_str(S._UPD_UX_KEY, _k)
_orig_svc = {n: S._svc_start(n) for n, _a in S._UPDATE_SVCS}
_orig_noauto = S._reg_dword(S._UPD_AU_KEY, "NoAutoUpdate")
print("原始状态：暂停键全空 =%s ； 服务=%s ； NoAutoUpdate=%s"
      % (not any(ORIG.values()), _orig_svc, _orig_noauto))


def restore():
    """还原成进入脚本时的状态。"""
    try:
        for k, v in ORIG.items():
            if v:
                S.reg_write(S._UPD_UX_KEY, k, "REG_SZ", v)
            else:
                S.reg_delete(S._UPD_UX_KEY, k)
        if _orig_noauto is not None:
            S.reg_write(S._UPD_AU_KEY, "NoAutoUpdate", "REG_DWORD",
                        _orig_noauto)
    except OSError as e:
        print("  !! 还原时出错（可忽略，键本来可能就是空的）：%s" % e)


print()
print("[1] 源码层面：不再禁服务")
_src = io.open(S.__file__, encoding="utf-8").read()
_i = _src.find("def set_update")
_j = _src.find("\ndef ", _i + 10)
_su = _src[_i:_j if _j > 0 else _i + 4000]
# 关闭分支 = 函数体开头到第一个 `return results`
_off = _su[:_su.find("return results")]
# ⚠️ **先把 docstring 剥掉**：docstring 里会提到 NoAutoUpdate（说明「恢复」
#    会删它），那是注释不是调用，留着会让「关闭分支不写策略」这条假FAIL。
_off_code = _off[_off.find('"""', _off.find('"""') + 3) + 3:]
chk("_svc_set_start" not in _off_code,
    "「关闭」分支里没有 _svc_set_start（**不禁用服务**）")
chk("NoAutoUpdate" not in _off_code, "「关闭」分支不写 NoAutoUpdate 策略")
chk("2099" in _off_code, "写到 2099 年")
chk("REG_SZ" in _off_code, "按 REG_SZ 写（这几个键是字符串，不是 DWORD）")
chk("update_status()" in _off_code, "写完回读复核（不信「命令返回成功」）")
# 三个 Pause*EndTime 是用**循环**写的（`for k in _PAUSE_END_KEYS`），
# 所以源码里找不到三个字面量名字 —— 要断言循环本身，别断言名字出现。
chk("for k in _PAUSE_END_KEYS:" in _off_code,
    "遍历 _PAUSE_END_KEYS 写全部三个到期时间（不是手写其中一两个）")
chk("PauseUpdatesStartTime" in _off_code,
    "也写 StartTime（只写 EndTime 在部分版本上会被当成过期而自动恢复）")
# 恢复分支必须同时清暂停 + 恢复服务
_on = _su[_su.find("return results"):]
chk("_svc_set_start" in _on and "reg_delete" in _on,
    "「恢复」既清暂停又恢复服务/策略（收拾旧版本的禁用残留）")

print()
print("[2] update_status 能区分「暂停到 2099」与普通暂停")
chk("paused_long" in S.update_status(), "status 里有 paused_long 字段")
chk("svc_disabled" in S.update_status(), "status 里有 svc_disabled 字段")
# ⚠️ 这一条**不能断言机器当前是哪种状态** —— 用户可能自己把更新暂停着
#    （他就在测这个开关）。要断言的是「两个字段与 paused 自洽」。
_st = S.update_status()
chk((not _st["paused"]) and _st["paused_long"] is False
    or _st["paused"] and isinstance(_st["paused_long"], bool),
    "paused_long 与 paused 自洽（paused=%s / paused_long=%s）"
    % (_st["paused"], _st["paused_long"]))
chk(_st["paused_long"] == (_st["paused"] and _st["paused_until"].startswith("2099")),
    "paused_long 恰好等价于「暂停且到期年份是 2099」")

print()
print("[3] 实跑「暂停」")
try:
    res = S.set_update(False)
    for r in res:
        print("     · %-32s ok=%-5s %s" % (r.get("name"), r.get("ok"),
                                          str(r.get("note"))[:46]))
    chk(all(r.get("ok") for r in res),
        "逐项都成功（含回读复核）")
    chk(any("复核" in (r.get("name") or "") for r in res),
        "结果里有一条显式的「复核」")
except OSError as e:
    chk(False, "暂停时抛异常：%s" % e)

print()
print("[4] 暂停后的真实状态：服务没被动，且status 认出来了")
_svc_now = {n: S._svc_start(n) for n, _a in S._UPDATE_SVCS}
print("     服务=%s（原始=%s）" % (_svc_now, _orig_svc))
chk(_svc_now == _orig_svc, "**三个服务的Start 值一个都没变**")
chk(S._reg_dword(S._UPD_AU_KEY, "NoAutoUpdate") == _orig_noauto,
    "NoAutoUpdate 策略没被写")
st2 = S.update_status()
print("     status: enabled=%s paused=%s paused_long=%s until=%s"
      % (st2["enabled"], st2["paused"], st2["paused_long"], st2["paused_until"]))
chk(st2["paused"] is True, "status 认出「处于暂停」")
chk(st2["paused_long"] is True, "status 认出「暂停到 2099」这一档")
chk(st2["enabled"] is True,
    "服务+策略仍然放行（enabled=True，UI 才能显示「恢复更新」）")
chk(st2["svc_disabled"] is False, "服务没被禁")
chk(st2["paused_until"].startswith("2099"), "到期日是 2099（%s）"
    % st2["paused_until"])
for k in S._PAUSE_END_KEYS:
    v = S._reg_str(S._UPD_UX_KEY, k)
    chk("2099" in v, "%s = %s" % (k, v))

print()
print("[5] 实跑「恢复」")
_admin = os.name == "nt" and S.is_admin()if hasattr(S, "is_admin") else None
try:
    res2 = S.set_update(True)
    for r in res2:
        print("     · %-32s ok=%-5s %s" % (r.get("name"), r.get("ok"),
                                           str(r.get("note"))[:46]))
    # 服务恢复要管理员；无 admin 时**如实报失败**正是我们要的行为
    # （UI 上这个按钮本来就在非 admin 下灰显，见 btn_upd.setEnabled）。
    svc_items = [r for r in res2 if r.get("name") in
                 dict(S._UPDATE_SVCS).keys()]
    pause_items = [r for r in res2 if r.get("name") == "更新暂停标记"]
    chk(bool(pause_items) and pause_items[0].get("ok"),
        "**暂停标记清掉了**（这一项不需要管理员）")
    if S.is_admin():
        chk(all(r.get("ok") for r in res2), "有管理员时逐项都成功")
    else:
        chk(all((not r.get("ok")) for r in svc_items),
            "无管理员：服务恢复如实报失败（不是谎报成功）")
except OSError as e:
    chk(False, "恢复时抛异常：%s" % e)
_st3 = S.update_status()
print("     status: paused=%s paused_long=%s enabled=%s"
      % (_st3["paused"], _st3["paused_long"], _st3["enabled"]))
chk(_st3["paused"] is False, "恢复后不再处于暂停")
chk(_st3["paused_long"] is False, "paused_long 也复位")
chk(_st3["enabled"] is True, "服务与策略放行")

print()
print("[6] 幂等：连点两次「暂停」不应报错")
try:
    r1 = S.set_update(False)
    r2 = S.set_update(False)
    chk(all(x.get("ok") for x in r1) and all(x.get("ok") for x in r2),
        "两次暂停都成功（幂等）")
    S.set_update(True)
except OSError as e:
    chk(False, "重复暂停抛异常：%s" % e)

print()
print("[7] 边界：enabled=None 或非法值不会崩")
for bad in (None, 0):
    try:
        S.set_update(bad)
        chk(True, "set_update(%r) 不崩" % bad)
    except Exception as e:
        chk(False, "set_update(%r) 抛异常 %s" % (bad, e))

print()
print("还原原始状态…")
restore()
time.sleep(0.2)
# ⚠️ 判据是「**跟进入脚本时一致**」，不是「键都为空」。
#    用户自己可能正把更新暂停着（他就是在测这个开关）—— 那时进入脚本
#    快照里就有值，restore() 忠实地写回它们才是对的；
#    若断言「必须全空」，会把「正确地还原了用户状态」误报成失败。
_now = {k: S._reg_str(S._UPD_UX_KEY, k) for k in S._PAUSE_ALL_KEYS}
_diff = [k for k in S._PAUSE_ALL_KEYS if _now[k] != ORIG[k]]
chk(not _diff,
    "已还原成**进入脚本时**的状态（差异项：%s）"
    % ("、".join("%s=%r(原%r)" % (k, _now[k], ORIG[k]) for k in _diff) or "无"))
if any(ORIG.values()):
    print("      注意：进入脚本时本机**本来就是暂停状态**（用户自己设的），"
          "已原样保留，未擅自改回。")

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)