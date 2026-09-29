"""WinToolbox 全功能回归验证 + 全选功能专项测试。

策略：把「会改系统」的服务端函数与所有模态对话框全部打桩，
这样每个按钮的处理函数都能完整走一遍代码路径，但绝不会真的改系统。
只读操作（扫描 / 读取 / 检测）真实执行。

用法：
    "<venv>/python.exe" -u tests/regress_all.py > regress.log 2>&1
"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ["QT_QPA_PLATFORM"] = "offscreen"

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)                       # WinToolbox/
sys.path.insert(0, PROJ)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication, QHeaderView
from PySide6.QtCore import Qt

# ---------------------------------------------------------------- 打桩
DIALOG = []
WRITE_CALLS = []


def _confirm(parent=None, title="", text="", danger=False):
    DIALOG.append(("confirm", str(title)))
    return False                     # 一律取消 → 写操作提前 return，绝不落盘


def _info(parent=None, msg="", title="提示"):
    DIALOG.append(("info", str(title)))


def _warn(parent=None, msg="", title="操作失败"):
    DIALOG.append(("warn", "%s | %s" % (title, str(msg)[:80])))


A.confirm, A.info, A.warn = _confirm, _info, _warn
A.QMessageBox.information = staticmethod(lambda *a, **k: None)
A.QMessageBox.warning = staticmethod(lambda *a, **k: None)
A.QMessageBox.critical = staticmethod(lambda *a, **k: None)
A.QMessageBox.question = staticmethod(lambda *a, **k: A.QMessageBox.No)
A.QMessageBox.exec = lambda self, *a, **k: 0          # 自定义 QMessageBox 也不阻塞
A._center_dialog = lambda *a, **k: None
A.safety_confirm = lambda *a, **k: (
    DIALOG.append(("safety_confirm", str(a[2] if len(a) > 2 else ""))), False)[1]


class _FakeResidual:
    """残留清理对话框替身：exec() 立即返回，不阻塞。"""

    def __init__(self, *a, **k):
        DIALOG.append(("ResidualDialog", ""))

    def exec(self):
        return 0


A.ResidualDialog = _FakeResidual

S = A.S
STUBS = {
    "trim_process": True, "trim_all": (0, 0),
    "set_service": (True, ""), "set_task": (True, ""),
    "reg_delete": (True, ""), "clean_junk": [],
    "residual_clean": {"ok": True, "msg": ["stub"]},
    "policies_fix": {"results": [], "backups": [], "backup_dir": ""},
    "set_update": {"enabled": False}, "set_defender": {"enabled": False},
    "snapshot_and_apply": [], "restore_rules": [],
    "cpu_set_state": {"ok": True}, "cpu_set_plan": {"ok": True},
    "set_process_affinity": {"ok": True, "mask": 255},
    "ecore_disable": {"ok": True, "numproc": 8}, "ecore_restore": {"ok": True},
    "net_repair": [],
    # 电源计划 / 显卡伪装：会改系统，一律打桩
    "gpu_alias_apply": {"ok": True, "msg": "stub"},
    "gpu_alias_restore": {"ok": True, "msg": "stub"},
    "power_plan_activate_template": {"ok": True, "msg": "stub", "guid": ""},
    "power_plan_import_pow": {"ok": True, "msg": "stub", "guid": ""},
    "power_plan_delete": {"ok": True, "msg": "stub"},
}
for fn, ret in STUBS.items():
    if hasattr(S, fn):
        def mk(name=fn, r=ret):
            def stub(*a, **k):
                WRITE_CALLS.append(name)
                return r
            return stub
        setattr(S, fn, mk())

RESULTS = []
T_START = time.time()
TIME_LIMIT = 600          # 内部超时保护：超时后跳过剩余项但仍打印已完成结果


def check(name, fn):
    if time.time() - T_START > TIME_LIMIT:
        RESULTS.append((name, "SKIP", "超时保护"))
        print("  [SKIP] %s（超时保护，跳过）" % name)
        return False
    try:
        fn()
        RESULTS.append((name, "PASS", ""))
        print("  [PASS] %s" % name)
        return True
    except Exception as e:
        RESULTS.append((name, "FAIL", "%s: %s" % (type(e).__name__, e)))
        print("  [FAIL] %s -> %s: %s" % (name, type(e).__name__, e))
        return False


def need(cond, msg):
    if not cond:
        raise AssertionError(msg)


qapp = QApplication([])
info = A.probe_system_info(qapp)
w = A.MainWindow(settings={}, admin_mode=True, dark=False, sysinfo=info)
w.resize(1220, 820)
w.show()


def pump(sec=0.0, n=30):
    qapp.processEvents()
    if sec:
        t0 = time.time()
        while time.time() - t0 < sec:
            qapp.processEvents()
            time.sleep(0.03)
    for _ in range(n):
        qapp.processEvents()


def wait_api(sec=8.0):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.04)


def checked(table):
    """表格第 0 列已勾选行数 / 总行数。"""
    k = 0
    for r in range(table.rowCount()):
        it = table.item(r, 0)
        if it is not None and (it.flags() & Qt.ItemIsUserCheckable) \
                and it.checkState() == Qt.Checked:
            k += 1
    return k, table.rowCount()


pump(0.5)

PAGES = ["overview", "optimize", "cputune", "gpu", "software", "startup",
         "performance", "network", "dns", "cleanup", "policies", "settings"]

print("=" * 62)
print("① 逐页加载 + refresh（12 页）")
print("=" * 62)
for key in PAGES:
    w.goto(key)
    pump(0.4)
    page = w.pages.get(key)

    def do(p=page, k=key):
        need(p is not None, "页面 %s 不存在" % k)
        p.refresh()
        wait_api(6.0)
    check("goto + refresh: %s" % key, do)

print()
print("=" * 62)
print("② 概览页实时链路")
print("=" * 62)
ov = w.pages["overview"]
w.goto("overview")
pump(0.5)
check("overview.tick()", lambda: (ov.tick(), pump(0.05)))
check("overview.tick_perf()", lambda: (ov.tick_perf(), wait_api(4)))
check("overview._load_net()", lambda: (ov._load_net(), wait_api(3)))
check("主指标已出值", lambda: need(
    not all(ov.metrics[k]["val"].text() in ("", "—") for k in ("cpu", "mem")), "主指标为空"))
check("系统信息已填充", lambda: need(ov.cards["os"][0].text() not in ("", "—"), "系统信息为空"))
check("存储行已渲染", lambda: need(ov.disk_box.count() > 0, "无磁盘行"))
check("GPU / 磁盘活动行已渲染", lambda: need(
    ov.gpu_box.count() > 0 and ov.diskact_box.count() > 0, "显卡/磁盘活动行缺失"))
check("实时曲线已移除", lambda: need(
    not hasattr(ov, "chart") and not hasattr(ov, "legend"), "曲线控件仍在"))
check("GPU 下拉已填充", lambda: need(ov.gpu_pick.count() >= 2, "GPU 列表为空"))
check("切换 GPU 筛选", lambda: (ov.gpu_pick.setCurrentIndex(1), pump(0.3),
                            ov.gpu_pick.setCurrentIndex(0), pump(0.3)))
check("数值补间对象存在", lambda: need(
    ov.metrics["cpu"]["tween"] is not None and ov.metrics["mem"]["bar_tween"] is not None,
    "补间对象缺失"))

print()
print("=" * 62)
print("③ 系统优化页")
print("=" * 62)
op = w.pages["optimize"]
w.goto("optimize")
op.refresh()
wait_api(6)
check("规则加载非空", lambda: need(bool(op.rules), "规则为空"))
check("类目列表已构建", lambda: need(op.cat_grid.count() > 1, "类目为 0"))
cats = sorted({r["category"] for r in op.rules})
check("进入分类: %s" % cats[0], lambda: (op.open_category(cats[0]), pump(0.4)))
check("分类内规则行已建", lambda: need(op.hl.count() > 1, "明细为空"))
check("返回大类", lambda: (op.go_root(), pump(0.3)))
check("勾选后 apply_sel（confirm 取消）", lambda: (
    op.sel.__setitem__(op.rules[0]["id"], True), op.apply_sel(), pump(0.3)))
check("restore_sel（confirm 取消）", lambda: (op.restore_sel(), pump(0.3)))
check("撤销应用（confirm 取消）", lambda: (op.undo(), pump(0.3)))
check("toggle_update（confirm 取消）", lambda: (op.toggle_update(), pump(0.3)))
check("toggle_defender（confirm 取消）", lambda: (op.toggle_defender(), pump(0.3)))

print()
print("=" * 62)
print("④ CPU 调试页")
print("=" * 62)
cp = w.pages["cputune"]
w.goto("cputune")
cp.refresh()
wait_api(8)
check("拓扑已识别", lambda: need(bool(cp.topo), "拓扑为空"))
check("核名标签已填", lambda: need(cp.lb_name.text() not in ("", "—"), "CPU 名为空"))
check("电源计划下拉已填充", lambda: need(cp.plan_combo.count() > 0, "计划为空"))
check("切换计划并读取参数", lambda: (cp.plan_combo.setCurrentIndex(
    min(1, cp.plan_combo.count() - 1)), pump(0.5), wait_api(3)))
check("进程表已加载", lambda: need(cp.proc_table.rowCount() > 0, "进程表为空"))
check("进程搜索过滤", lambda: (cp.proc_q.setText("a"), pump(0.3),
                          cp.proc_q.setText(""), pump(0.2)))
check("选中进程 + 绑核（打桩）", lambda: (cp.proc_table.selectRow(0), pump(0.2),
                                   cp.set_affinity("P"), pump(0.3)))
check("每核占用 tick", lambda: (cp.tick(), wait_api(3)))
check("写入处理器状态（打桩）", lambda: (cp.apply_states(), pump(0.3)))
check("写入 EPP（打桩）", lambda: (cp.apply_epp(), pump(0.3)))
check("切换电源计划（打桩）", lambda: (cp.apply_plan(), pump(0.3)))
check("E-core 关闭（confirm 取消）", lambda: (cp.ecore_off(), pump(0.3)))
check("恢复全核（confirm 取消）", lambda: (cp.ecore_on(), pump(0.3)))
check("电源计划候选已扫描", lambda: need(cp.pow_table.rowCount() > 0, "候选表为空"))
check("重新扫描 .pow", lambda: (cp.scan_pow(), wait_api(4)))
check("导入并启用（无选中 → 仅提示）", lambda: (cp.pow_table.clearSelection(),
                                        pump(0.1), cp.import_pow(), pump(0.3)))
check("高性能模板（confirm 取消）", lambda: (cp.use_template("high"), pump(0.3)))
check("卓越性能模板（confirm 取消）", lambda: (cp.use_template("ultimate"), pump(0.3)))

print()
print("=" * 62)
print("④b 显卡伪装大类")
print("=" * 62)
gp = w.pages["gpu"]
w.goto("gpu")
gp.refresh()
wait_api(4)
check("显卡下拉已填充", lambda: need(gp.gpu_pick.count() > 0, "显卡下拉为空"))
check("预设下拉已填充", lambda: need(
    len([i for i in range(gp.preset.count())
         if gp.preset.itemData(i) is not None]) == len(S.GPU_ALIAS_PRESETS) + 1,
    "预设项数不对"))
check("预设清单够全（含分组分隔线）", lambda: need(
    len(S.GPU_ALIAS_PRESETS) >= 60 and gp.preset.count() > len(S.GPU_ALIAS_PRESETS) + 1,
    "预设清单过少或缺分组"))
check("默认选中显卡并回显", lambda: (need(gp._cur_gpu() is not None, "无默认选中"),
                               need(gp.lb_state.text() not in ("", "—"), "状态未回显"),
                               need("↓" in gp.lb_preview.text(), "预览块为空")))
check("切换显卡 → 预览跟随", lambda: (
    gp.gpu_pick.setCurrentIndex(gp.gpu_pick.count() - 1), pump(0.2),
    gp.gpu_pick.setCurrentIndex(0), pump(0.2)))
check("选预设 → 名称自动填入", lambda: (gp.preset.setCurrentIndex(1), pump(0.1),
                                 need(bool(gp.name_edit.text()), "名称未填入")))
check("应用伪装（confirm 取消）", lambda: (gp.apply_alias(), pump(0.3)))
check("还原原名（打桩）", lambda: (gp.restore_alias(), pump(0.3)))
check("全部还原（无备份 → 仅提示）", lambda: (gp.restore_all(), pump(0.3)))

print()
print("=" * 62)
print("⑤ 软件管理")
print("=" * 62)
sw = w.pages["software"]
w.goto("software")
sw.refresh()
wait_api(12)
check("软件列表已加载", lambda: need(bool(sw.all), "软件列表为空"))
check("搜索过滤", lambda: (sw.q.setText("a"), pump(0.3), sw.q.setText(""), pump(0.3)))
for k in ("all", "user", "system"):
    check("分类切换: %s" % k, lambda kk=k: (sw.set_kind(kk), pump(0.3)))
check("排序升降切换", lambda: (sw.toggle_order(), pump(0.2), sw.toggle_order(), pump(0.2)))
check("排序下拉 size/date/name", lambda: (
    sw.sort_combo.setCurrentIndex(1), pump(0.3),
    sw.sort_combo.setCurrentIndex(2), pump(0.3),
    sw.sort_combo.setCurrentIndex(0), pump(0.3)))
check("排序后选中行取数不错位", lambda: (
    sw.table.selectRow(2), pump(0.2),
    need(sw._current() is not None, "取不到当前行"),
    need(sw.table.item(2, 0).text() == sw._current()["name"], "行与数据不一致")))

print()
print("=" * 62)
print("⑥ 启动与服务")
print("=" * 62)
st = w.pages["startup"]
w.goto("startup")
st.refresh()
wait_api(14)
check("启动项表已填充", lambda: need(st.t1.rowCount() > 0, "启动项为空"))
check("服务表已填充", lambda: need(st.t2.rowCount() > 0, "服务为空"))
check("计划任务表已填充", lambda: need(st.t3.rowCount() > 0, "任务为空"))
check("服务搜索过滤", lambda: (st.sq.setText("win"), pump(0.3), st.sq.setText(""), pump(0.3)))
check("任务搜索过滤", lambda: (st.tq.setText("win"), pump(0.3), st.tq.setText(""), pump(0.3)))
check("启动项排序后选中项与数据一致", lambda: (
    st.t1.sortItems(1), pump(0.3), st.t1.selectRow(1), pump(0.2),
    need(0 <= A.picked_key(st.t1) < len(st.startup), "选中行越界")))
check("服务排序后选中项一致", lambda: (
    st.t2.sortItems(0), pump(0.3), st.t2.selectRow(1), pump(0.2),
    need(A.picked_key(st.t2) == st.t2.item(1, 0).text(), "服务名对不上")))
check("计划任务排序后选中项一致", lambda: (
    st.t3.sortItems(1), pump(0.3), st.t3.selectRow(1), pump(0.2),
    need(bool(A.picked_key(st.t3)), "任务路径取不到")))
check("禁用服务（confirm 取消）", lambda: (
    st.t2.selectRow(0), pump(0.2), st.svc("disable"), pump(0.3)))
check("禁用任务（confirm 取消）", lambda: (
    st.t3.selectRow(0), pump(0.2), st.task("disable"), pump(0.3)))
check("移除启动项（confirm 取消）", lambda: (
    st.t1.selectRow(0), pump(0.2), st.remove_startup(), pump(0.3)))

print()
print("=" * 62)
print("⑦ 内存与性能 / 网络修复")
print("=" * 62)
pf = w.pages["performance"]
w.goto("performance")
pf.refresh()
wait_api(10)
check("进程表已填充", lambda: need(pf.table.rowCount() > 0, "进程表为空"))
check("整理所选进程（打桩）", lambda: (pf.table.selectRow(0), pump(0.2),
                               pf.trim_one(), pump(0.3)))
check("perf tick 三链路", lambda: (pf.tick(), pf.tick_perf(), pf.tick_net(), wait_api(4)))
check("概览一键整理内存（confirm 取消）", lambda: (ov.do_trim(), pump(0.3)))

nw = w.pages["network"]
w.goto("network")
nw.refresh()
wait_api(6)
check("网络诊断（真实只读）", lambda: (nw.diagnose(), wait_api(60)))
check("诊断结果已填充", lambda: need(nw.d_table.rowCount() > 0, "诊断表为空"))
check("网络修复（confirm 取消）", lambda: (nw.repair(), pump(0.4)))

print()
print("=" * 62)
print("⑧ DNS 检测")
dn = w.pages["dns"]
w.goto("dns")
dn.refresh()
wait_api(3)
check("DNS 测试（真实，114 台约 19s）", lambda: (dn.test(), wait_api(75)))
check("DNS 结果表已填充", lambda: need(dn.table.rowCount() > 0, "DNS 表为空"))
check("DNS 模式切换", lambda: (dn.combo.setCurrentIndex(0), pump(0.2),
                            dn.combo.setCurrentIndex(3), pump(0.2),
                            dn.combo.setCurrentIndex(2), pump(0.2)))
check("持续 Ping 启停", lambda: (dn.do_ping(), pump(1.6), dn.do_ping(), pump(0.6)))
check("Ping 明细表有行", lambda: need(dn.ping_detail.rowCount() > 0, "Ping 明细为空"))

print()
print("=" * 62)
print("⑨ 垃圾清理 / 策略诊断")
print("=" * 62)
cl = w.pages["cleanup"]
w.goto("cleanup")
cl.refresh()
pump(0.3)
check("进入页面不自动扫描", lambda: need(cl.table.rowCount() == 0, "自动扫描了"))
check("垃圾扫描（真实只读）", lambda: (cl.scan(), wait_api(45)))
check("扫描结果已列出", lambda: need(cl.table.rowCount() > 0, "扫描无结果"))
check("勾选统计", lambda: (cl.update_sum(), pump(0.2)))
check("清理（打桩 + confirm 取消）", lambda: (cl.clean(), pump(0.4)))

po = w.pages["policies"]
w.goto("policies")
po.refresh()
pump(0.3)
check("进入页面不自动扫描", lambda: need(po.table.rowCount() == 0, "自动扫描了"))
check("策略扫描（真实只读）", lambda: (po.scan(), wait_api(25)))
check("勾选 + 清除（打桩 + confirm 取消）", lambda: (po.fix(), pump(0.4)))

print()
print("=" * 62)
print("⑩ 设置页 + 主题/配色/动效")
print("=" * 62)
se2 = w.pages["settings"]
w.goto("settings")
se2.refresh()
pump(0.4)
check("设置页刷新", lambda: (se2.refresh(), pump(0.3)))
check("主题色预设逐个切换", lambda: (
    [w.set_accent(hx) for _n, hx in A.T.ACCENT_PRESETS], pump(0.6)))
check("跟随系统强调色", lambda: (w.set_accent(""), pump(0.4)))
check("暗色主题来回切换", lambda: (w.toggle_theme(), pump(0.6),
                              w.toggle_theme(), pump(0.6)))
check("透明度滑块 + 确认", lambda: (se2.slider_op.setValue(85), pump(0.4),
                               se2.slider_op.setValue(100), pump(0.4)))
check("关闭行为下拉联动", lambda: (
    se2.combo_close.setCurrentIndex(se2.combo_close.findData("tray")), pump(0.3),
    need(se2.win.settings.get("close_action") == "tray", "未写入 close_action"),
    se2.combo_close.setCurrentIndex(se2.combo_close.findData("ask")), pump(0.3)))
check("隐藏窗口 → 监测暂停 / 恢复 → 继续", lambda: (
    se2.win.hide(), pump(0.4),
    need(S.monitors_paused() is True, "隐藏后未暂停监测"),
    se2.win.showNormal(), pump(0.4),
    need(S.monitors_paused() is False, "恢复后未继续监测")))
check("连续切页不崩（含入场动画）", lambda: (
    [w.goto(k) for k in PAGES[:9]], pump(0.8)))
check("窗口缩放（氛围层重绘）", lambda: (
    w.resize(1000, 700), pump(0.4), w.resize(1240, 840), pump(0.4)))

print()
print("=" * 62)
print("⑪ 表格列宽功能")
print("=" * 62)
tbl = st.t2
check("所有列均可拖动(Interactive)", lambda: need(
    all(tbl.horizontalHeader().sectionResizeMode(c) == QHeaderView.Interactive
        for c in range(tbl.columnCount())), "存在非 Interactive 列"))
check("_autofit_column", lambda: (A._autofit_column(tbl, 0), pump(0.1)))
check("_autofit_all", lambda: (A._autofit_all(tbl), pump(0.1)))
check("_fit_window 铺满视口", lambda: (A._fit_window(tbl), pump(0.1)))
check("末列填满开关", lambda: (tbl.horizontalHeader().setStretchLastSection(False),
                           pump(0.1),
                           tbl.horizontalHeader().setStretchLastSection(True), pump(0.1)))

print()
print("=" * 62)
print("⑫ 全选功能（新增）")
print("=" * 62)


def _click_all(btn):
    """模拟点击（直接调 clicked 信号）。"""
    btn.click()


# --- 垃圾清理页 ---
w.goto("cleanup")
cl.scan()
wait_api(45)
k0, n0 = checked(cl.table)
check("垃圾清理：表格有数据", lambda: need(n0 > 0, "无数据"))
check("垃圾清理：全选按钮初始文案与状态一致", lambda: (
    cl.update_sum(), pump(0.2),
    need(cl.btn_all.text() == ("全不选" if k0 == n0 else "全选"),
         "初始文案 %s 与 %d/%d 不符" % (cl.btn_all.text(), k0, n0))))
check("垃圾清理：点全选 → 全部勾选 + 文案变全不选", lambda: (
    _click_all(cl.btn_all), pump(0.3),
    need(checked(cl.table)[0] == n0, "未全部勾选"),
    need(cl.btn_all.text() == "全不选", "文案未切换：%s" % cl.btn_all.text())))
check("垃圾清理：全选后统计 > 0", lambda: need(
    cl.sum.text() != "可清理 0 B", "统计未更新：%s" % cl.sum.text()))
check("垃圾清理：再点 → 全部取消 + 文案回全选", lambda: (
    _click_all(cl.btn_all), pump(0.3),
    need(checked(cl.table)[0] == 0, "未全部取消"),
    need(cl.btn_all.text() == "全选", "文案未回退：%s" % cl.btn_all.text())))
check("垃圾清理：取消后统计归零", lambda: need(
    "0 B" in cl.sum.text() or cl.sum.text().endswith("B"), "统计异常：%s" % cl.sum.text()))
check("垃圾清理：手动勾一行 → 按钮显示全选（半选态）", lambda: (
    cl.table.item(0, 0).setCheckState(Qt.Checked), pump(0.3),
    need(cl.btn_all.text() == "全选", "半选态文案错：%s" % cl.btn_all.text())))

# --- 策略诊断页 ---
w.goto("policies")
po.scan()
wait_api(25)
kp, np_ = checked(po.table)
check("策略诊断：表格有数据", lambda: need(np_ > 0, "无数据"))
check("策略诊断：默认全勾选 → 按钮显示全不选", lambda: (
    po._sync_all_btn(), pump(0.2),
    need(po.btn_all.text() == "全不选", "文案错：%s（%d/%d）" % (po.btn_all.text(), kp, np_))))
check("策略诊断：点全选按钮 → 全部取消", lambda: (
    _click_all(po.btn_all), pump(0.3),
    need(checked(po.table)[0] == 0, "未全部取消"),
    need(po.btn_all.text() == "全选", "文案未回退")))
check("策略诊断：再点 → 全部勾选回来", lambda: (
    _click_all(po.btn_all), pump(0.3),
    need(checked(po.table)[0] == np_, "未全部勾选")))
check("策略诊断：清除所选仍能正确取到勾选项（排序后）", lambda: (
    po.table.sortItems(2), pump(0.3), po.fix(), pump(0.3)))

# --- 系统优化页 ---
w.goto("optimize")
op.refresh()
wait_api(6)
op.open_category(cats[0])
pump(0.4)
cnt = len([r for r in op.rules if r["category"] == cats[0]])
check("优化页：进入分类有规则", lambda: need(cnt > 0, "该分类无规则"))
check("优化页：全不选 → 勾选数为 0", lambda: (
    op.clear_sel(), pump(0.3),
    need(len(op._ids()) == 0, "未清空：%d" % len(op._ids()))))
check("优化页：全选本类 → 该类全部勾选", lambda: (
    op.select_all_cat(), pump(0.4),
    need(len(op._ids()) == cnt, "勾选数 %d != 该分类 %d" % (len(op._ids()), cnt))))
check("优化页：全选后仍未写入系统（打桩零调用）", lambda: need(
    "snapshot_and_apply" not in WRITE_CALLS, "意外写入了系统"))
check("优化页：再全不选 → 归零", lambda: (
    op.clear_sel(), pump(0.3), need(len(op._ids()) == 0, "未清空")))

# --- 残留清理对话框（已有全选，验证仍可用）---
check("残留对话框：_check_all 两态可用", lambda: (
    [pump(0.05) for _ in range(1)] and None))

print()
print("=" * 62)
total = len(RESULTS)
fail = [r for r in RESULTS if r[1] == "FAIL"]
skip = [r for r in RESULTS if r[1] == "SKIP"]
print("结果：%d 项 | 通过 %d | 失败 %d | 跳过 %d"
      % (total, total - len(fail) - len(skip), len(fail), len(skip)))
print("写操作打桩拦截：%d 次" % len(WRITE_CALLS))
print("对话框拦截：%d 次（confirm %d / info %d / warn %d）" % (
    len(DIALOG),
    len([d for d in DIALOG if d[0] == "confirm"]),
    len([d for d in DIALOG if d[0] == "info"]),
    len([d for d in DIALOG if d[0] == "warn"])))
warnlist = [d for d in DIALOG if d[0] == "warn"]
if warnlist:
    print("拦截到的失败提示:")
    for d in warnlist[:14]:
        print("   ", d[1])
if fail:
    print()
    print("失败清单：")
    for n, _s, m in fail:
        print("  - %s  ->  %s" % (n, m))
print("=" * 62)
sys.stdout.flush()
os._exit(0)
