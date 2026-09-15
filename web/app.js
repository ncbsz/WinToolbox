/* ============================================================
   WinToolbox front-end  (vanilla JS, no build step)
   ============================================================ */
(function () {
'use strict';

const $ = (s, r) => (r || document).querySelector(s);
const content = $('#content');
const state = {
  admin: false,
  page: 'overview',
  metrics: { cpu: 0, mem: 0, mem_total: 0, mem_used: 0 },
  hist: [],
  chartTimer: null,
  rules: [],
  sel: {},          // generic selection store per page
};

/* ---------------- icons ---------------- */
const ICONS = {
  home:   '<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.6V21h13V9.6"/>',
  tune:   '<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2.1"/><circle cx="15" cy="12" r="2.1"/><circle cx="7.5" cy="17" r="2.1"/>',
  shield: '<path d="M12 3l7.5 3v6c0 4.6-3.2 8.3-7.5 9.6C7.7 20.3 4.5 16.6 4.5 12V6L12 3z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
  box:    '<path d="M3 7.6 12 3l9 4.6v8.8L12 21l-9-4.6V7.6z"/><path d="M3 7.6 12 12.2l9-4.6M12 12.2V21"/>',
  power:  '<path d="M12 3.5v8.5"/><path d="M6.8 6.6a7.6 7.6 0 1 0 10.4 0"/>',
  speed:  '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4.2-5.4"/><circle cx="12" cy="18" r="1.4"/>',
  wifi:   '<path d="M2.6 9.4a15 15 0 0 1 18.8 0M5.6 13a10.4 10.4 0 0 1 12.8 0M8.6 16.5a5.8 5.8 0 0 1 6.8 0"/><circle cx="12" cy="19.8" r="1.1"/>',
  broom:  '<path d="M14.8 4l5.2 5.2-4 4L10.8 8l4-4z"/><path d="M10.8 8 4 14.8V20h5.2L16 13.2"/>',
  lock:   '<rect x="4.6" y="10.4" width="14.8" height="10" rx="2.2"/><path d="M8.2 10.4V7.2a3.8 3.8 0 0 1 7.6 0v3.2"/>',
};
const svg = (n, cls) =>
  `<svg class="${cls || 'ico'}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[n] || ''}</svg>`;

const NAV = [
  { key: 'overview',   name: '概览',        icon: 'home'   },
  { key: 'optimize',   name: '系统优化',    icon: 'tune'   },
  { key: 'security',   name: '安全检测',    icon: 'shield' },
  { key: 'software',   name: '软件管理',    icon: 'box'    },
  { key: 'startup',    name: '启动与服务',  icon: 'power'  },
  { key: 'performance',name: '内存与性能',  icon: 'speed'  },
  { key: 'network',    name: '网络修复',    icon: 'wifi'   },
  { key: 'cleanup',    name: '垃圾清理',    icon: 'broom'  },
  { key: 'policies',   name: '策略诊断',    icon: 'lock'   },
];

/* ---------------- utils ---------------- */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
}
function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + ' 天 ' : '') + h + ' 小时 ' + m + ' 分';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  let j = null;
  try { j = await r.json(); } catch (e) { throw new Error('后端返回异常 (' + r.status + ')'); }
  if (!j.ok) throw new Error(j.error || ('请求失败 ' + r.status));
  return j.data;
}
const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b || {}) });

function toast(msg, kind) {
  const t = el(`<div class="toast ${kind || ''}">${esc(msg)}</div>`);
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => t.remove(), 3600);
}

function confirmBox(title, text, okText, danger) {
  return new Promise(res => {
    const m = el(`<div class="mask"><div class="modal">
      <h3>${esc(title)}</h3><p>${text}</p>
      <div class="acts">
        <button class="btn" data-a="no">取消</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-a="yes">${esc(okText || '确定')}</button>
      </div></div></div>`);
    document.body.appendChild(m);
    m.addEventListener('click', e => {
      const a = e.target.getAttribute && e.target.getAttribute('data-a');
      if (a === 'yes') { m.remove(); res(true); }
      if (a === 'no' || e.target === m) { m.remove(); res(false); }
    });
  });
}

function busy(btn, on, label) {
  if (!btn) return;
  if (on) {
    btn.dataset.old = btn.innerHTML;
    btn.innerHTML = '<span class="spin"></span> ' + (label || '处理中');
    btn.disabled = true;
  } else {
    btn.innerHTML = btn.dataset.old || btn.innerHTML;
    btn.disabled = false;
  }
}
function row(inner, cls) { return `<div class="row ${cls || ''}">${inner}</div>`; }

/* ============================================================
   PAGES
   ============================================================ */
const PAGES = {};

/* ---------------- 概览 ---------------- */
PAGES.overview = async () => {
  content.innerHTML = '<div class="empty"><span class="spin"></span> 正在读取系统信息…</div>';
  const d = await api('/api/sysinfo');
  const mem = d.mem, total = mem.total, used = total - mem.avail;
  const memPct = Math.round(used * 100 / total);
  content.innerHTML = `
    <div class="grid c4" style="margin-bottom:14px">
      <div class="stat"><div class="k">操作系统</div><div class="v" style="font-size:14px;font-weight:600">${esc(d.os.caption || '—')}</div><div class="s mono" style="margin-top:4px">${esc(d.os.version)} · ${esc(d.os.arch)}</div></div>
      <div class="stat"><div class="k">处理器</div><div class="v" style="font-size:14px;font-weight:600">${esc(d.cpu)}</div><div class="s mono" style="margin-top:4px">${d.cores} 核 / ${d.logical} 线程</div></div>
      <div class="stat"><div class="k">内存占用</div><div class="v">${memPct}<small>%</small></div>
        <div class="bar"><i class="${memPct > 85 ? 'err' : memPct > 65 ? 'warn' : ''}" style="width:${memPct}%"></i></div>
        <div class="s mono" style="margin-top:6px">${fmtBytes(used)} / ${fmtBytes(total)}</div></div>
      <div class="stat"><div class="k">运行时长</div><div class="v" style="font-size:16px">${fmtUptime(d.uptime)}</div><div class="s mono" style="margin-top:6px">${esc(d.host)} · ${esc(d.user)}</div></div>
    </div>

    <div class="card">
      <div class="card-head"><h2>磁盘占用</h2><div class="spacer"></div>
        <span class="pill">${d.disks.length} 个分区</span></div>
      ${d.disks.map(x => `
        <div style="margin-bottom:12px">
          <div style="display:flex;font-size:13px"><b>${esc(x.drive)}</b>
            <span class="spacer" style="flex:1"></span>
            <span class="mono">${fmtBytes(x.used)} / ${fmtBytes(x.total)} · 剩余 ${fmtBytes(x.free)}</span></div>
          <div class="bar" style="margin-top:6px;height:6px;background:#EDEDED;border-radius:3px;overflow:hidden">
            <i style="display:block;height:100%;width:${x.percent}%;border-radius:3px;background:${x.percent > 90 ? '#C42B1C' : x.percent > 75 ? '#F7A600' : '#0067C0'}"></i></div>
        </div>`).join('')}
    </div>

    <div class="grid c2">
      <div class="card">
        <div class="card-head"><h2>实时占用</h2><div class="spacer"></div>
          <span class="pill" id="m-cpu">CPU —</span><span class="pill" id="m-mem">内存 —</span></div>
        <canvas id="chart" height="120"></canvas>
      </div>
      <div class="card">
        <div class="card-head"><h2>快速操作</h2></div>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          <button class="btn primary" id="q-trim">一键整理内存</button>
          <button class="btn" id="q-diag">网络快速诊断</button>
          <button class="btn" id="q-scan">扫描桌面是否被感染</button>
        </div>
        <div class="notice info" style="margin-top:14px;margin-bottom:0">
          本工具是<b>功能整合版</b>：优化项来自 ZyperWin++ 的思路，卸载/启动项/服务来自 HiBit Uninstaller，
          内存监控与整理来自 Sunlight 内存整理，网络诊断修复来自 360 断网急救箱，策略诊断针对
          “受组织管理”问题。全部<b>重新实现</b>，未复用任何被感染文件中的代码。
        </div>
      </div>
    </div>`;

  // live chart
  if (state.chartTimer) { clearInterval(state.chartTimer); state.chartTimer = null; }
  const cv = $('#chart'), ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  function fit() {
    const w = cv.clientWidth, h = 120;
    cv.width = w * dpr; cv.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function draw() {
    fit();
    const w = cv.clientWidth, h = 120, pad = 4;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = '#EDEDED'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad + (h - pad * 2) * i / 4;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    const n = 60, xs = i => w * i / (n - 1);
    const line = (key, color) => {
      const arr = state.hist.map(p => p[key]);
      if (arr.length < 2) return;
      ctx.beginPath();
      arr.forEach((v, i) => {
        const y = h - pad - (h - pad * 2) * Math.min(100, v) / 100;
        i ? ctx.lineTo(xs(i * (n - 1) / (arr.length - 1)), y) : ctx.moveTo(xs(0), y);
      });
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
      ctx.lineTo(xs(n - 1), h); ctx.lineTo(xs(0), h); ctx.closePath();
      ctx.fillStyle = color.replace('rgb', 'rgba').replace(')', ',.10)');
      ctx.fill();
    };
    line('mem', 'rgb(0,103,192)');
    line('cpu', 'rgb(79,163,227)');
  }
  async function tick() {
    try {
      const m = await api('/api/metrics');
      state.metrics = m;
      state.hist.push({ cpu: m.cpu, mem: m.mem });
      if (state.hist.length > 60) state.hist.shift();
      const c = $('#m-cpu'), me = $('#m-mem');
      if (c) c.textContent = 'CPU ' + m.cpu.toFixed(0) + '%';
      if (me) me.textContent = '内存 ' + m.mem.toFixed(0) + '%';
      draw();
    } catch (e) { /* ignore */ }
  }
  tick();
  state.chartTimer = setInterval(() => { if (state.page !== 'overview') { clearInterval(state.chartTimer); state.chartTimer = null; return; } tick(); }, 1000);

  $('#q-trim').onclick = async e => {
    busy(e.target, true, '整理中');
    try { const r = await post('/api/memory/trim', { mode: 'all' }); toast(`已整理 ${r.trimmed} 个进程`, 'ok'); }
    catch (er) { toast(er.message, 'err'); }
    busy(e.target, false);
  };
  $('#q-diag').onclick = () => { location.hash = '#network'; };
  $('#q-scan').onclick = () => { location.hash = '#security'; };
};

/* ---------------- 系统优化 ---------------- */
PAGES.optimize = async () => {
  content.innerHTML = '<div class="empty"><span class="spin"></span> 加载优化规则…</div>';
  if (!state.rules.length) state.rules = await api('/api/rules');
  const rules = state.rules;
  const cats = {};
  rules.forEach(r => { (cats[r.category] = cats[r.category] || []).push(r); });
  const catName = { explorer: '资源管理器', performance: '性能优化', privacy: '隐私保护', update: '系统更新', edge: 'Edge 浏览器', security: '安全防护', system: '系统偏好' };
  const policyCount = rules.filter(r => r.risk === 'policy').length;

  content.innerHTML = `
    <div class="notice warn">
      <b>注意“受组织管理”问题：</b>标有 <span class="tag warn">策略</span> 的 ${policyCount} 项会写入
      <span class="mono">SOFTWARE\\Policies</span>，应用后 Windows 会显示“某些设置由你的组织管理”。
      这类项<b>默认不勾选</b>；想彻底恢复，用左侧「策略诊断」一键清除。
    </div>
    <div class="toolbar">
      <button class="btn primary" id="o-apply">应用所选</button>
      <button class="btn" id="o-restore">还原所选</button>
      <button class="btn" id="o-undo">撤销上次优化</button>
      <div class="spacer"></div>
      <label class="chk"><input type="checkbox" id="o-safe"> 只显示安全项</label>
      <button class="btn sm" id="o-none">全不选</button>
    </div>
    <div id="o-body"></div>`;

  function paint(onlySafe) {
    const body = $('#o-body');
    body.innerHTML = Object.keys(cats).map(cat => {
      const list = cats[cat].filter(r => !onlySafe || r.risk !== 'policy');
      if (!list.length) return '';
      return `<div class="card">
        <div class="card-head"><h2>${catName[cat] || cat}</h2><div class="spacer"></div>
          <span class="pill">${list.filter(r => state.sel[r.id]).length} / ${list.length}</span>
          <button class="btn sm" data-cat="${cat}">全选本组</button></div>
        ${list.map(r => {
          const on = !!state.sel[r.id];
          const applied = r.state === true;
          return `<div class="row" data-id="${r.id}">
            <div class="toggle ${on ? 'on' : ''}" data-t="${r.id}"><i></i></div>
            <div class="grow">
              <div class="t">${esc(r.name)} ${r.risk === 'policy' ? '<span class="tag warn">策略</span>' : ''}
                ${applied ? '<span class="tag ok">已生效</span>' : ''}</div>
              <div class="s">${esc(r.desc)}</div>
            </div>
          </div>`;
        }).join('')}
      </div>`;
    }).join('') || '<div class="empty">没有可显示的项</div>';

    body.querySelectorAll('.toggle').forEach(t => t.onclick = () => {
      const id = t.dataset.t;
      state.sel[id] = !state.sel[id];
      paint($('#o-safe').checked);
    });
    body.querySelectorAll('button[data-cat]').forEach(b => b.onclick = () => {
      const c = b.dataset.cat;
      const list = cats[c].filter(r => !$('#o-safe').checked || r.risk !== 'policy');
      const allOn = list.every(r => state.sel[r.id]);
      list.forEach(r => state.sel[r.id] = !allOn);
      paint($('#o-safe').checked);
    });
  }
  paint(false);

  $('#o-safe').onchange = () => paint($('#o-safe').checked);
  $('#o-none').onclick = () => { state.sel = {}; paint($('#o-safe').checked); };

  const idsOf = pre => rules.filter(r => state.sel[r.id] &&
    (pre === 'apply' ? true : true)).map(r => r.id);

  $('#o-apply').onclick = async e => {
    const ids = objKeysOn();
    if (!ids.length) return toast('还没有勾选任何项', 'err');
    const pol = rules.filter(r => ids.includes(r.id) && r.risk === 'policy');
    let msg = `将对 <b>${ids.length}</b> 项执行优化。`;
    if (pol.length) msg += `<br><br>其中 <b>${pol.length}</b> 项是策略项，<b>会让 Windows 出现“由你的组织管理”</b>。`;
    if (!await confirmBox('确认应用优化', msg, '应用', !!pol.length)) return;
    busy(e.target, true);
    try {
      const res = await post('/api/optimize/apply', { ids });
      const bad = res.filter(x => !x.ok);
      toast(`完成 ${res.length - bad.length} / ${res.length} 项`, bad.length ? 'err' : 'ok');
      if (bad.length) console.warn(bad);
      state.rules = await api('/api/rules'); PAGES.optimize();
    } catch (er) { toast(er.message, 'err'); busy(e.target, false); }
  };
  $('#o-restore').onclick = async e => {
    const ids = objKeysOn();
    if (!ids.length) return toast('还没有勾选任何项', 'err');
    if (!await confirmBox('确认还原', `将把 <b>${ids.length}</b> 项恢复为 Windows 默认值。`, '还原')) return;
    busy(e.target, true);
    try {
      const res = await post('/api/optimize/restore', { ids });
      toast(`已还原 ${res.length} 项`, 'ok');
      state.rules = await api('/api/rules'); PAGES.optimize();
    } catch (er) { toast(er.message, 'err'); busy(e.target, false); }
  };
  $('#o-undo').onclick = async e => {
    if (!await confirmBox('撤销上次优化', '将按操作记录把注册表值恢复到你优化之前的状态。', '撤销')) return;
    busy(e.target, true);
    try {
      const r = await post('/api/optimize/undo', {});
      toast(`已撤销 ${r.time} 的记录（恢复 ${r.restored} 个值）`, 'ok');
      state.rules = await api('/api/rules'); PAGES.optimize();
    } catch (er) { toast(er.message, 'err'); busy(e.target, false); }
  };
  function objKeysOn() { return Object.keys(state.sel).filter(k => state.sel[k]); }
};

/* ---------------- 安全检测 ---------------- */
PAGES.security = async () => {
  content.innerHTML = `
    <div class="notice err">
      <b>Synaptics / XRed 感染型病毒检测</b><br>
      本模块只读扫描 PE 文件，比对病毒壳特征（CODE 节 629760 字节、MD5
      <span class="mono">33fbe30e…6542</span>）以及 <span class="mono">xred.mooo.com</span> 等特征字符串。
      <b>不会执行或修改任何文件。</b>
    </div>
    <div class="card">
      <div class="card-head"><h2>选择扫描范围</h2></div>
      <div class="toolbar">
        <select id="s-preset">
          <option value="__desktop">桌面</option>
          <option value="__downloads">下载</option>
          <option value="__docs">文档</option>
          <option value="__d">D:\\</option>
          <option value="__c">C:\\</option>
          <option value="__custom">自定义路径…</option>
        </select>
        <input type="text" id="s-path" style="display:none;width:340px" placeholder="例如 D:\\桌面\\桌面文件">
        <label class="chk"><input type="checkbox" id="s-deep"> 深度扫描（连非标准壳也查，较慢）</label>
        <button class="btn primary" id="s-go">开始扫描</button>
      </div>
      <div id="s-out"></div>
    </div>`;

  const preset = $('#s-preset'), pth = $('#s-path');
  preset.onchange = () => { pth.style.display = preset.value === '__custom' ? '' : 'none'; };
  $('#s-go').onclick = async e => {
    let base = pth.value.trim();
    const v = preset.value;
    if (v === '__custom' && !base) return toast('请输入路径', 'err');
    const out = $('#s-out');
    out.innerHTML = '<div class="empty"><span class="spin"></span> 扫描中，请稍候…</div>';
    busy(e.target, true, '扫描中');
    try {
      const r = await api(`/api/security/scan?path=${encodeURIComponent(base)}&deep=${$('#s-deep').checked ? 1 : 0}`);
      if (!r.hits.length) {
        out.innerHTML = `<div class="notice ok" style="margin:0">
          扫描完成：检查了 <b>${r.scanned}</b> 个 PE 文件，<b>未发现</b> Synaptics / XRed 感染特征。</div>`;
      } else {
        const inf = r.hits.filter(h => h.verdict === 'infected-loader');
        out.innerHTML = `<div class="notice err">
            发现 <b>${r.hits.length}</b> 个可疑文件（其中 <b>${inf.length}</b> 个确认为病毒壳）。<br>
            处理方式：① 先跑全盘杀毒并开启“防感染模式”；② 把被感染文件<b>删除</b>，
            从官方渠道重新下载；③ 用脚本把资源段里的干净原件提取出来再扫描确认。
          </div>
          <div class="scroll-y">${r.hits.map(h => row(`
            <div class="grow"><div class="t mono">${esc(h.path)}</div>
              <div class="s">${fmtBytes(h.size)}</div></div>
            <span class="tag ${h.verdict === 'infected-loader' ? 'err' : 'warn'}">
              ${h.verdict === 'infected-loader' ? '确认感染' : '可疑'}</span>`)).join('')}</div>`;
        toast(`发现 ${r.hits.length} 个可疑文件`, 'err');
      }
    } catch (er) { out.innerHTML = `<div class="notice err">${esc(er.message)}</div>`; }
    busy(e.target, false);
  };
};

/* ---------------- 软件管理 ---------------- */
PAGES.software = async () => {
  content.innerHTML = '<div class="empty"><span class="spin"></span> 读取已安装程序…</div>';
  const list = await api('/api/software');
  let kw = '';
  content.innerHTML = `
    <div class="toolbar">
      <input type="search" id="sw-q" placeholder="搜索程序名 / 发布者" style="width:260px">
      <span class="pill" id="sw-n">共 ${list.length} 个</span>
      <div class="spacer"></div>
      <button class="btn" id="sw-left">残留扫描</button>
    </div>
    <div class="card"><div class="scroll-y" id="sw-body"></div></div>`;

  function paint() {
    const f = list.filter(x =>
      !kw || (x.name + ' ' + (x.publisher || '')).toLowerCase().includes(kw));
    $('#sw-n').textContent = '共 ' + f.length + ' 个';
    $('#sw-body').innerHTML = f.length ? f.map(x => row(`
      <div class="grow"><div class="t">${esc(x.name)}</div>
        <div class="s">${esc(x.publisher || '未知发布者')}${x.version ? ' · v' + esc(x.version) : ''}
          ${x.size ? ' · ' + fmtBytes(x.size * 1024) : ''}${x.date ? ' · ' + esc(x.date) : ''}</div></div>
      <button class="btn sm" data-k="${encodeURIComponent(x.key)}"
        data-n="${encodeURIComponent(x.name)}"
        data-c="${encodeURIComponent(x.uninstall || '')}">卸载</button>`)).join('')
      : '<div class="empty">没有匹配的程序</div>';

    $('#sw-body').querySelectorAll('button[data-k]').forEach(b => b.onclick = async () => {
      const name = decodeURIComponent(b.dataset.n);
      const cmd = decodeURIComponent(b.dataset.c);
      if (!cmd) return toast('该程序没有提供卸载命令，可能需手动卸载', 'err');
      if (!await confirmBox('确认卸载', `即将卸载 <b>${esc(name)}</b>。<br><br>会调用它自带的卸载程序，可能弹出交互窗口。`, '卸载', true)) return;
      busy(b, true);
      try {
        await post('/api/soft/uninstall', { cmd });
        toast('已启动卸载程序', 'ok');
      } catch (e) { toast(e.message, 'err'); }
      busy(b, false);
    });
  }
  paint();
  $('#sw-q').oninput = e => { kw = e.target.value.trim().toLowerCase(); paint(); };
  $('#sw-left').onclick = async () => {
    const k = prompt('输入已卸载程序的关键词，扫描残留文件/目录（≥3 字符）');
    if (!k || k.trim().length < 3) return;
    try {
      const r = await post('/api/soft/leftover', { keyword: k.trim() });
      if (!r.length) return toast('未发现残留目录', 'ok');
      $('#sw-body').innerHTML = `<div class="notice warn">发现 ${r.length} 条可能残留，请<b>人工确认</b>后再删除：</div>` +
        r.map(x => row(`<div class="grow"><div class="t mono">${esc(x.path)}</div>
          <div class="s">${x.is_dir ? '目录' : '文件'} · ${fmtBytes(x.size)}</div></div>`)).join('');
    } catch (e) { toast(e.message, 'err'); }
  };
};

/* ---------------- 启动与服务 ---------------- */
PAGES.startup = async () => {
  content.innerHTML = '<div class="empty"><span class="spin"></span> 读取启动项与服务…</div>';
  const [startup, svcs] = await Promise.all([api('/api/startup'), api('/api/services')]);
  let f = '';
  content.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>开机启动项</h2><div class="spacer"></div>
        <span class="pill">${startup.length} 项</span></div>
      <div class="scroll-y">${startup.length ? startup.map((x, i) => row(`
        <div class="grow"><div class="t">${esc(x.name)} ${x.hive === 'DIR' ? '<span class="tag warn">启动文件夹</span>' : ''}</div>
          <div class="s mono">${esc(x.cmd)}</div></div>
        ${x.hive === 'DIR' ? '' : `<button class="btn sm danger" data-i="${i}">移除</button>`}`)).join('')
        : '<div class="empty">没有自启动项</div>'}</div>
    </div>
    <div class="card">
      <div class="card-head"><h2>系统服务</h2><div class="spacer"></div>
        <input type="search" id="sv-q" placeholder="按名称过滤" style="width:200px">
        <span class="pill" id="sv-n">${svcs.length}</span></div>
      <div class="scroll-y" id="sv-body"></div>
    </div>`;

  $('#sv-body').innerHTML = '';
  function paintSvc() {
    const ff = svcs.filter(s => !f || (s.name + s.display).toLowerCase().includes(f));
    $('#sv-n').textContent = ff.length;
    $('#sv-body').innerHTML = ff.map(s => row(`
      <div class="grow"><div class="t">${esc(s.display || s.name)}</div>
        <div class="s mono">${esc(s.name)}</div></div>
      <span class="tag ${s.status === 'Running' ? 'ok' : ''}">${s.status === 'Running' ? '运行中' : '已停止'}</span>
      <span class="tag ${s.start === '禁用' ? 'err' : ''}">${esc(s.start)}</span>
      <button class="btn sm" data-a="start" data-n="${encodeURIComponent(s.name)}">启动</button>
      <button class="btn sm" data-a="stop" data-n="${encodeURIComponent(s.name)}">停止</button>
      <button class="btn sm" data-a="disable" data-n="${encodeURIComponent(s.name)}">禁用</button>
      <button class="btn sm" data-a="manual" data-n="${encodeURIComponent(s.name)}">改手动</button>`)).join('')
      || '<div class="empty">没有匹配的服务</div>';
    $('#sv-body').querySelectorAll('button[data-a]').forEach(b => b.onclick = async () => {
      const n = decodeURIComponent(b.dataset.n), a = b.dataset.a;
      if (a === 'disable' && !await confirmBox('禁用服务', `确定禁用 <b>${esc(n)}</b>？可能影响依赖它的功能。`, '禁用', true)) return;
      busy(b, true);
      try {
        const r = await post('/api/service', { name: n, action: a });
        toast(r.ok ? '操作成功' : ('失败：' + (r.note || '')), r.ok ? 'ok' : 'err');
        const fresh = await api('/api/services');
        svcs.length = 0; fresh.forEach(x => svcs.push(x)); paintSvc();
      } catch (e) { toast(e.message, 'err'); busy(b, false); }
    });
  }
  paintSvc();
  $('#sv-q').oninput = e => { f = e.target.value.trim().toLowerCase(); paintSvc(); };

  content.querySelectorAll('button[data-i]').forEach(b => b.onclick = async () => {
    const x = startup[+b.dataset.i];
    if (!await confirmBox('移除启动项', `删除 <b>${esc(x.name)}</b> 的自启动注册表项？`, '移除', true)) return;
    busy(b, true);
    try {
      await post('/api/startup/toggle', { hive: x.hive, path: x.path, name: x.name, enable: false });
      toast('已移除', 'ok'); PAGES.startup();
    } catch (e) { toast(e.message, 'err'); busy(b, false); }
  });
};

/* ---------------- 内存与性能 ---------------- */
PAGES.performance = async () => {
  content.innerHTML = `
    <div class="grid c3" style="margin-bottom:14px">
      <div class="stat"><div class="k">CPU 占用</div><div class="v" id="p-cpu">—</div>
        <div class="bar"><i id="p-cpubar" style="width:0"></i></div></div>
      <div class="stat"><div class="k">内存占用</div><div class="v" id="p-mem">—</div>
        <div class="bar"><i id="p-membar" style="width:0"></i></div>
        <div class="s mono" id="p-memt" style="margin-top:6px">—</div></div>
      <div class="stat"><div class="k">操作</div>
        <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px">
          <button class="btn primary" id="p-trim">一键整理内存</button>
          <button class="btn" id="p-reload">刷新进程</button>
        </div></div>
    </div>
    <div class="card">
      <div class="card-head"><h2>占用最高的进程</h2><div class="spacer"></div>
        <span class="pill">点「整理」可把该进程工作集换出</span></div>
      <div class="scroll-y" id="p-body"></div>
    </div>`;

  async function tick() {
    try {
      const m = await api('/api/metrics');
      $('#p-cpu').textContent = m.cpu.toFixed(0) + '%';
      $('#p-mem').textContent = m.mem.toFixed(0) + '%';
      $('#p-memt').textContent = fmtBytes(m.mem_used) + ' / ' + fmtBytes(m.mem_total);
      $('#p-cpubar').style.width = m.cpu + '%';
      $('#p-membar').style.width = m.mem + '%';
      $('#p-cpubar').className = m.cpu > 85 ? 'err' : m.cpu > 65 ? 'warn' : '';
      $('#p-membar').className = m.mem > 85 ? 'err' : m.mem > 65 ? 'warn' : '';
    } catch (e) {}
  }
  tick();
  state.chartTimer = setInterval(() => { if (state.page !== 'performance') { clearInterval(state.chartTimer); state.chartTimer = null; return; } tick(); }, 1500);

  async function loadProcs() {
    const ps = await api('/api/processes?limit=60');
    $('#p-body').innerHTML = ps.map(p => row(`
      <div class="grow"><div class="t">${esc(p.name)}</div><div class="s mono">PID ${p.pid}</div></div>
      <span class="tag">${fmtBytes(p.mem)}</span>
      <button class="btn sm" data-pid="${p.pid}">整理</button>`)).join('')
      || '<div class="empty">没有数据</div>';
    $('#p-body').querySelectorAll('button[data-pid]').forEach(b => b.onclick = async () => {
      busy(b, true);
      try { await post('/api/memory/trim', { mode: 'pid', pid: +b.dataset.pid }); toast('已整理', 'ok'); }
      catch (e) { toast(e.message, 'err'); }
      busy(b, false); loadProcs();
    });
  }
  loadProcs();
  $('#p-reload').onclick = loadProcs;
  $('#p-trim').onclick = async e => {
    if (!await confirmBox('整理内存', '将对所有占用 &gt;8MB 的进程执行 <span class="mono">SetProcessWorkingSetSize(-1,-1)</span>，把工作集换出到页面文件。<br><br><b>说明：</b>这会让“内存占用”数字立刻下降，但被换出的数据下次访问要从磁盘读回，<b>可能反而变慢</b>。系统本身会按需管理内存，此操作更多是“让数字好看”。', '仍要整理')) return;
    busy(e.target, true, '整理中');
    try { const r = await post('/api/memory/trim', { mode: 'all' }); toast(`已整理 ${r.trimmed} / ${r.total} 个进程`, 'ok'); loadProcs(); }
    catch (er) { toast(er.message, 'err'); }
    busy(e.target, false);
  };
};

/* ---------------- 网络修复 ---------------- */
const REPAIRS = [
  ['flushdns', '刷新 DNS 解析缓存', '最快、最安全，先试这个'],
  ['registerdns', '重新注册 DNS 记录', 'DNS 解析异常时用'],
  ['release', '释放 IP 地址', '配合“重新获取 IP”使用'],
  ['renew', '重新获取 IP 地址', 'DHCP 获取不到地址时用'],
  ['arpr', '清除 ARP 缓存', '局域网访问异常 / ARP 冲突'],
  ['route', '重置路由表', '路由表被写乱导致上不了网'],
  ['winsock', '重置 Winsock 目录', 'LSP 被劫持、浏览器打不开网页'],
  ['tcpip', '重置 TCP/IP 协议栈', '疑难断网，需重启'],
  ['ipv4', '重置 IPv4 协议栈', 'TCP/IP 重置的补充'],
  ['firewall', '重置防火墙规则', '规则冲突导致拦截，慎用'],
];

PAGES.network = async () => {
  content.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>全面诊断</h2><div class="spacer"></div>
        <span class="pill" id="n-score">—</span>
        <button class="btn primary" id="n-diag">开始诊断</button></div>
      <div id="n-out"><div class="empty">点「开始诊断」依次检查 网卡 / IP / DNS / HOSTS / 代理 / 防火墙 / 外网连通 / DNS 解析 / LSP 协议链</div></div>
    </div>
    <div class="card">
      <div class="card-head"><h2>一键修复</h2><div class="spacer"></div>
        <button class="btn" id="n-quick">常用修复（前 5 项）</button>
        <button class="btn primary" id="n-run">执行所选修复</button></div>
      <div class="notice warn" style="margin-bottom:10px">
        重置 Winsock / 协议栈 / 路由表属于<b>强力修复</b>，会改写系统网络配置，<b>执行后需要重启</b>。
        修复前建议先创建系统还原点。
      </div>
      <div>${REPAIRS.map(([k, n, d], i) => `
        <div class="row">
          <div class="toggle ${i < 5 ? 'on' : ''}" data-r="${k}"><i></i></div>
          <div class="grow"><div class="t">${n}</div><div class="s">${d}</div></div>
        </div>`).join('')}</div>
    </div>`;

  const sel = {}; REPAIRS.forEach(([k], i) => sel[k] = i < 5);
  content.querySelectorAll('.toggle[data-r]').forEach(t => t.onclick = () => {
    sel[t.dataset.r] = !sel[t.dataset.r]; t.classList.toggle('on');
  });

  $('#n-diag').onclick = async e => {
    const out = $('#n-out');
    out.innerHTML = '<div class="empty"><span class="spin"></span> 诊断中（约 10~20 秒）…</div>';
    busy(e.target, true, '诊断中');
    $('#n-score').textContent = '—';
    try {
      const r = await api('/api/network/diagnose');
      out.innerHTML = r.steps.map(s => row(`
        <span class="tag ${s.ok ? 'ok' : 'err'}">${s.ok ? '正常' : '异常'}</span>
        <div class="grow"><div class="t">${esc(s.name)}</div><div class="s">${esc(s.detail)}</div></div>
      `)).join('');
      const p = $('#n-score');
      p.textContent = `正常 ${r.score}/${r.total}`;
      p.className = 'pill ' + (r.score === r.total ? 'ok' : r.score >= r.total - 2 ? 'warn' : 'err');
    } catch (er) { out.innerHTML = `<div class="notice err">${esc(er.message)}</div>`; }
    busy(e.target, false);
  };
  $('#n-quick').onclick = () => { REPAIRS.forEach(([k], i) => sel[k] = i < 5); content.querySelectorAll('.toggle[data-r]').forEach(t => t.classList.toggle('on', sel[t.dataset.r])); };
  $('#n-run').onclick = async e => {
    const steps = Object.keys(sel).filter(k => sel[k]);
    if (!steps.length) return toast('没有选择任何修复项', 'err');
    if (!await confirmBox('确认修复', `将执行 <b>${steps.length}</b> 项网络修复操作。<br><br>部分操作会重置网络配置，<b>需要重启电脑</b>后才能完全生效。`, '执行', true)) return;
    busy(e.target, true, '修复中');
    try {
      const r = await post('/api/network/repair', { steps });
      const bad = r.filter(x => !x.ok);
      toast(`完成 ${r.length - bad.length} / ${r.length} 项` + (bad.length ? '，部分失败' : ''), bad.length ? 'err' : 'ok');
      $('#n-out').innerHTML = r.map(x => row(`
        <span class="tag ${x.ok ? 'ok' : 'err'}">${x.ok ? '已完成' : '失败'}</span>
        <div class="grow"><div class="t">${esc(x.name)}</div></div>`)).join('') +
        '<div class="notice info" style="margin:12px 0 0">若做了 Winsock / 协议栈 / 路由表重置，请<b>重启电脑</b>。</div>';
    } catch (er) { toast(er.message, 'err'); }
    busy(e.target, false);
  };
};

/* ---------------- 垃圾清理 ---------------- */
PAGES.cleanup = async () => {
  content.innerHTML = `
    <div class="toolbar">
      <button class="btn primary" id="c-scan">开始扫描</button>
      <button class="btn danger" id="c-clean">清理所选</button>
      <div class="spacer"></div>
      <span class="pill" id="c-sum">—</span>
    </div>
    <div class="card"><div id="c-out"><div class="empty">点「开始扫描」统计各位置可清理的体积（只读，不会删东西）</div></div></div>`;

  let items = [], sel = {};
  $('#c-scan').onclick = async e => {
    const out = $('#c-out');
    out.innerHTML = '<div class="empty"><span class="spin"></span> 扫描中…</div>';
    busy(e.target, true, '扫描中');
    try {
      items = await api('/api/cleanup/scan');
      sel = {}; items.forEach(x => sel[x.id] = x.size > 0);
      paint();
    } catch (er) { out.innerHTML = `<div class="notice err">${esc(er.message)}</div>`; }
    busy(e.target, false);
  };
  function paint() {
    const total = items.filter(x => sel[x.id]).reduce((a, b) => a + b.size, 0);
    $('#c-sum').textContent = '可清理 ' + fmtBytes(total);
    $('#c-out').innerHTML = items.map(x => `
      <div class="row">
        <div class="toggle ${sel[x.id] ? 'on' : ''}" data-c="${x.id}"><i></i></div>
        <div class="grow"><div class="t">${esc(x.name)}</div>
          <div class="s mono">${esc(x.path)}${x.files ? ' · ' + x.files + ' 个文件' : ''}</div></div>
        <span class="tag ${x.size > 0 ? '' : 'ok'}">${fmtBytes(x.size)}</span>
      </div>`).join('');
    $('#c-out').querySelectorAll('.toggle[data-c]').forEach(t => t.onclick = () => {
      sel[t.dataset.c] = !sel[t.dataset.c]; t.classList.toggle('on');
      const tt = items.filter(x => sel[x.id]).reduce((a, b) => a + b.size, 0);
      $('#c-sum').textContent = '可清理 ' + fmtBytes(tt);
    });
  }
  $('#c-clean').onclick = async e => {
    const ids = Object.keys(sel).filter(k => sel[k]);
    if (!ids.length) return toast('没有选择任何项', 'err');
    const total = items.filter(x => ids.includes(x.id)).reduce((a, b) => a + b.size, 0);
    if (!await confirmBox('确认清理', `将删除约 <b>${fmtBytes(total)}</b> 的临时/缓存文件。<br><br>
      ⚠️ <b>删除后无法恢复。</b>正在运行的程序占用的文件会被跳过。<br>
      回收站也会被清空。`, '清理', true)) return;
    busy(e.target, true, '清理中');
    try {
      const r = await post('/api/cleanup/clean', { ids });
      const freed = r.reduce((a, b) => a + (b.freed || 0), 0);
      toast(`清理完成，释放 ${fmtBytes(freed)}`, 'ok');
      PAGES.cleanup();
    } catch (er) { toast(er.message, 'err'); busy(e.target, false); }
  };
};

/* ---------------- 策略诊断 ---------------- */
PAGES.policies = async () => {
  content.innerHTML = `
    <div class="notice info">
      <b>为什么会出现“由你的组织管理”？</b><br>
      优化工具为了禁用 Defender、关闭更新、关掉 SmartScreen，会把设置写进组策略分支
      <span class="mono">SOFTWARE\\Policies\\…</span>。Windows 一旦读到这些值，就认为“有组织在统一管控本机”，
      于是安全中心 / Windows 更新 / Edge 出现“由你的组织管理”。<b>本模块就是把这些残留策略清掉。</b>
    </div>
    <div class="toolbar">
      <button class="btn primary" id="pl-scan">扫描策略残留</button>
      <button class="btn danger" id="pl-fix">清除所选</button>
      <div class="spacer"></div>
      <span class="pill" id="pl-sum">—</span>
    </div>
    <div class="card"><div id="pl-out"><div class="empty">点「扫描策略残留」检测当前系统中的“受组织管理”成因</div></div></div>`;

  let found = [], sel = {};
  $('#pl-scan').onclick = async e => {
    const out = $('#pl-out');
    out.innerHTML = '<div class="empty"><span class="spin"></span> 扫描中…</div>';
    busy(e.target, true, '扫描中');
    try {
      found = await api('/api/policies/scan');
      sel = {}; found.forEach(x => sel[x.path] = true);
      paint();
    } catch (er) { out.innerHTML = `<div class="notice err">${esc(er.message)}</div>`; }
    busy(e.target, false);
  };
  function paint() {
    if (!found.length) {
      $('#pl-sum').textContent = '未发现残留';
      $('#pl-out').innerHTML = '<div class="notice ok" style="margin:0">当前系统未发现这类策略残留，“受组织管理”提示应该不会出现。</div>';
      return;
    }
    $('#pl-sum').textContent = `${found.length} 处残留`;
    $('#pl-out').innerHTML = found.map(x => `
      <div class="row">
        <div class="toggle ${sel[x.path] ? 'on' : ''}" data-p="${encodeURIComponent(x.path)}"><i></i></div>
        <div class="grow"><div class="t">${esc(x.path)}</div>
          <div class="s">影响：${esc(x.why)} · ${x.count} 个值</div>
          ${x.values && x.values.length ? `<div class="s mono">${x.values.slice(0, 8).map(v => esc(v.name) + '=' + esc(v.value)).join('  ')}</div>` : ''}
        </div>
      </div>`).join('');
    $('#pl-out').querySelectorAll('.toggle[data-p]').forEach(t => t.onclick = () => {
      const p = decodeURIComponent(t.dataset.p);
      sel[p] = !sel[p]; t.classList.toggle('on');
    });
  }
  $('#pl-fix').onclick = async e => {
    const paths = Object.keys(sel).filter(k => sel[k]);
    if (!paths.length) return toast('没有选择任何项', 'err');
    if (!await confirmBox('清除策略残留',
      `将删除 <b>${paths.length}</b> 处组策略分支，并解除更新暂停。<br><br>
       脚本会<b>先把每一项导出成 .reg 备份</b>（存放在 WinToolbox\\backup\\），
       出问题双击备份即可还原。<br><br>清除后<b>需要重启</b>，“由你的组织管理”才会消失。`, '清除', true)) return;
    busy(e.target, true, '清除中');
    try {
      const r = await post('/api/policies/fix', { paths });
      toast(`已清除 ${r.filter(x => x.ok).length} / ${r.length} 处，备份在 backup\\ 目录`, 'ok');
      $('#pl-out').innerHTML = r.map(x => row(`
        <span class="tag ${x.ok ? 'ok' : 'err'}">${x.ok ? '已清除' : '失败'}</span>
        <div class="grow"><div class="t mono">${esc(x.path)}</div>
          <div class="s">${x.backup ? '备份：' + esc(x.backup) : ''}</div></div>`)).join('') +
        '<div class="notice info" style="margin:12px 0 0">请<b>重启电脑</b>，“由你的组织管理”提示才会消失。</div>';
    } catch (er) { toast(er.message, 'err'); }
    busy(e.target, false);
  };
};

/* ============================================================
   router / shell
   ============================================================ */
function buildNav(alertCount) {
  $('#nav-list').innerHTML = NAV.map(n => `
    <div class="nav-item ${state.page === n.key ? 'active' : ''}" data-k="${n.key}">
      ${svg(n.icon)}<span>${n.name}</span>
      ${n.key === 'security' && alertCount ? `<span class="badge">${alertCount}</span>` : ''}
    </div>`).join('');
  $('#nav-list').querySelectorAll('.nav-item').forEach(it => it.onclick = () => {
    location.hash = '#' + it.dataset.k;
  });
}

async function go(key) {
  if (!PAGES[key]) key = 'overview';
  state.page = key;
  if (state.chartTimer) { clearInterval(state.chartTimer); state.chartTimer = null; }
  buildNav(0);
  const nav = NAV.find(n => n.key === key);
  $('#page-title').textContent = nav ? nav.name : key;
  try {
    await PAGES[key]();
  } catch (e) {
    content.innerHTML = `<div class="notice err">加载失败：${esc(e.message)}</div>`;
  }
}

window.addEventListener('hashchange', () => go(location.hash.replace('#', '') || 'overview'));
$('#btn-refresh').onclick = () => go(state.page);

(async function boot() {
  try {
    const h = await api('/api/health');
    state.admin = h.admin;
    const pa = $('#pill-admin');
    pa.textContent = h.admin ? '管理员权限' : '普通权限';
    pa.className = 'pill ' + (h.admin ? 'ok' : 'warn');
    $('#foot-admin').textContent = h.admin ? '权限：管理员 ✓' : '权限：普通（部分操作需管理员）';
    $('#foot-py').textContent = '后端：Python ' + h.python;
    if (!h.admin) {
      content.innerHTML = `<div class="notice warn">
        <b>当前不是管理员权限。</b><br>
        注册表 HKLM 写入、服务启停、网络重置、Defender 策略清除等操作会失败或无效。<br>
        请关闭本窗口，右键 <b>启动.bat</b> →「以管理员身份运行」。</div>`;
      return;
    }
  } catch (e) {
    content.innerHTML = `<div class="notice err">
      无法连接后端。<br>请确认 <b>启动.bat</b> 仍在运行，且没有防火墙拦截 127.0.0.1:8760。<br>
      <span class="mono">${esc(e.message)}</span></div>`;
    return;
  }
  go(location.hash.replace('#', '') || 'overview');
})();

})();
