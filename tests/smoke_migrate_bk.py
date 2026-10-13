# -*- coding: utf-8 -*-
"""迁移后的定向冒烟：只验 UI 装配 + 关键回调，不跑全量回归。"""
import io
import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import Qt                          # noqa: E402
import app as A                                        # noqa: E402
import server as S                                     # noqa: E402

# 测试脚本绝不写用户配置
A.settings_save = lambda *a, **k: None
_SRC = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()

app = QApplication.instance() or QApplication(sys.argv)
OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


win = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
pages = win.pages

print("[1] 注册表页：备份与还原 = 系统还原点")
reg = pages["regtool"]
chk("备份与还原" in [reg.tabs.tabText(i) for i in range(reg.tabs.count())],
    "第 3 个页签是「备份与还原」（注册表清理=%s）"
    % [reg.tabs.tabText(i) for i in range(reg.tabs.count())])
chk(hasattr(reg, "tb_rp") and hasattr(reg, "scan_rp"),
    "有还原点表格和扫描入口")
chk(hasattr(reg, "btn_rp_del") and not hasattr(reg, "btn_bk_restore"),
    "有「删除所选」按钮，.reg 备份按钮已移走")
chk(hasattr(reg, "_bk_tabs_loaded"), "有懒加载集合")

print("[2] 系统组件页：还原点已迁走，页签 3 个")
st = pages["systool"]
tabs = [st.tabs.tabText(i) for i in range(st.tabs.count())]
chk(st.tabs.count() == 3, "页签数 = 3（%s）" % tabs)
chk("系统还原点" not in tabs, "还原点页签已移走")
chk(not hasattr(st, "tb_rp") and not hasattr(st, "scan_rp"),
    "旧的还原点方法已彻底删除（不留死代码）")
chk("scan_rp" not in _SRC[_SRC.find("class SysToolsPage"):
                        _SRC.find("class PoliciesPage")],
    "系统组件页源码里不再出现 scan_rp")

print("[3] 设置页：新增 .reg 操作备份卡")
se = pages["settings"]
chk(hasattr(se, "tb_bk") and hasattr(se, "load_backups"),
    "有备份表格和 load_backups")
chk(hasattr(se, "restore_bk") and hasattr(se, "open_backup_dir"),
    "有还原 / 打开目录")
chk(hasattr(se, "btn_bk_restore") and hasattr(se, "pill_bk"),
    "有还原按钮和状态药丸")

print("[4] 刷新路径：refresh() 都要真重载")
reg.refresh()
se.refresh()
chk(True, "两个 refresh() 都不抛异常")

print("[5] 服务端接口仍在")
for f in ("list_restore_points", "create_restore_point", "remove_restore_point",
          "reg_backups_list", "reg_backup_restore"):
    chk(hasattr(S, f), "S.%s 存在" % f)

print("[6] 空列表文案不误导")
_segs = _SRC[_SRC.find("class SettingsPage"):]
chk("当前无备份" in _segs, "空列表写「当前无备份」而不是「0 个备份」")
chk("PillWarn" in _segs, "空/失败用警示色")
chk("读取失败" in _segs, "读取失败会明说，不留空表")
chk("S.BACKUP" in _segs, "提示里给出备份目录路径")
_rseg = _SRC[_SRC.find("class RegistryToolsPage"):
                 _SRC.find("class SysToolsPage")]
chk("需要管理员权限才能读取" in _rseg,
    "非管理员读不到还原点时明说（不显示「0 个」）")

print("[7] 勾选状态联动（Qt.UserRole 存 seq 字符串）")
reg._got_rp([{"seq": "12", "desc": "测试点", "type": "APPLICATION_INSTALL",
              "raw_date": "2026-10-04 10:00:00"}])
chk(reg.tb_rp.rowCount() == 1, "还原点行已填入")
it = reg.tb_rp.item(0, 0)
it.setCheckState(Qt.Checked)
reg._rp_sel()
chk(reg.btn_rp_del.isEnabled(), "勾选后「删除所选」才可用")
it.setCheckState(Qt.Unchecked)
reg._rp_sel()
chk(not reg.btn_rp_del.isEnabled(), "取消勾选后又禁用")

print("[8] .reg 备份列表填充")
se._bk_fail("模拟失败")
chk("读取失败" in se.pill_bk.text(), "失败路径会写明原因")
_segs_now = se.pill_bk.text()

print()
print("[9] 禁用记录（启动项 / 服务 / 计划任务）随档案迁移")
# 禁用这三样改的是 Windows 自己的状态，系统里没有「谁禁的」线索 →
# 本工具每次禁用都记一笔，档案带走它，导入时按本机实况算差集再补做。
chk(S._REC_ROOT.startswith(S._OWN_ROOT + chr(92)),
    "记录放在自有子树下（%s）" % S._REC_ROOT)
chk(set(S._REC_KINDS) == set(("startup", "service", "task")), "三类记录齐全")
_pb = S.profile_build({}, "1.4")
chk(any("禁用" in x for x in (_pb.get("_includes") or [])),
    "档案的「包含什么」里写明了这份记录")
try:
    S.disabled_record_add("service", name="ZZSmokeSvc")
    _rc = S.disabled_records()
    chk(len(_rc["service"]) == 1, "记一笔后能读回")
    chk("DisabledByTool" in __import__("json").dumps(
        S.own_state_export(), ensure_ascii=False), "记录进了自有状态（随档案走）")
    chk(S.disabled_record_label("service", {"name": "ZZSmokeSvc"}) == "ZZSmokeSvc",
        "标签可取")
finally:
    S.disabled_record_del("service", name="ZZSmokeSvc")
chk(not any(S.disabled_records().values()), "删完记录为空（测试不留残留）")
chk(not S.own_state_export(), "own_state_export 也回到空")
chk("disabled_records_pending" in _SRC and "disabled_records_apply" in _SRC,
    "导入路径接了差集计算 / 补做")

print()
print("=" * 56)
print("冒烟：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)

win.hide()
win.deleteLater()
os._exit(0 if not FAIL else 1)
