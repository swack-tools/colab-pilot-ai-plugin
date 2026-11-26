import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from './mcp-app-client.js';

const app = new App({ name: 'Colab Pilot', version: '1.3.2' }, {});
let currentState = null;
let busyAction = '';
let receivedInitialResult = false;
let bannerState = { msg: '', isError: false };

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function setBanner(msg, isError = false) {
  bannerState = { msg: msg || '', isError: Boolean(isError) };
  const el = document.getElementById('banner');
  if (!el) return;
  if (!bannerState.msg) {
    el.style.display = 'none';
    el.textContent = '';
    return;
  }
  el.style.display = 'block';
  el.className = bannerState.isError ? 'banner banner-err' : 'banner banner-info';
  el.textContent = bannerState.msg;
}

async function runTool(name, args = {}, label = '') {
  if (busyAction && name === 'colab_dashboard' && label === 'Auto-refresh') return;
  busyAction = label || name;
  if (label !== 'Auto-refresh') setBanner(`Running ${busyAction}...`);
  try {
    const res = await app.callServerTool({ name, arguments: args });
    if (res?.isError) {
      const txt = (res.content || []).map((c) => c.text || '').join(' ');
      setBanner(txt || `${name} failed`, true);
      return;
    }
    if (name === 'colab_dashboard' && res?.structuredContent) {
      currentState = res.structuredContent;
      if (label !== 'Auto-refresh') {
        bannerState = { msg: '', isError: false };
      }
      render(currentState);
    } else {
      let msg = '';
      if (name === 'colab_wakeup_alarm' && args?.action === 'test_chirp' && res?.structuredContent) {
        const sc = res.structuredContent;
        msg = sc.played_audio
          ? `Played test chirp (${sc.voice || 'macOS voice'}).`
          : 'Synthesized chirp (audio playback unavailable on host).';
      } else {
        const txt = (res?.content || []).map((c) => c.text || '').join(' ');
        msg = txt ? txt.slice(0, 220) : `${name} completed`;
      }
      setBanner(msg);
      const dash = await app.callServerTool({ name: 'colab_dashboard', arguments: {} });
      if (dash?.structuredContent) {
        currentState = dash.structuredContent;
        render(currentState);
      }
    }
  } catch (err) {
    setBanner(`Error: ${err?.message || err}`, true);
  } finally {
    busyAction = '';
  }
}

function render(data) {
  const root = document.getElementById('app');
  if (!root) return;

  const isLoading = !data;
  const usage = data?.usage || {};
  const balance = Number(usage.balance ?? 0);
  const rate = Number(usage.rate_hourly ?? 0);
  const activeCount = Number(usage.active_assignments ?? 0);
  const monthlyCu = Number(data?.ultra_monthly_cu ?? 25000);
  const budgetFrac = Number(data?.budget_fraction ?? 0.2);
  const budgetCap = monthlyCu * budgetFrac;
  const pctRemainingOfCap = budgetCap > 0 ? Math.min(100, Math.max(0, (balance / budgetCap) * 100)) : 100;

  const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
  const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
  const transfers = Array.isArray(data?.transfers) ? data.transfers : [];
  const keepalive = data?.keepalive || {};
  const activeOps = Array.isArray(data?.active_operations) ? data.active_operations : [];
  const alarm = data?.alarm || {};
  const caps = app.getHostCapabilities() || {};
  const canOpenLinks = Boolean(caps.openLinks);

  const rateClass = rate > 0 ? 'pill pill-warn' : 'pill pill-ok';
  const rateText = isLoading ? 'Syncing…' : rate > 0 ? `${rate.toFixed(2)} CU/hr` : '0.00 CU/hr (Idle)';
  const staleBadge = usage.stale ? ' <span class="pill pill-muted">cached</span>' : '';

  const opsSection = activeOps.length
    ? `
      <section class="sec">
        <div class="sec-hdr">
          <h2>In-Flight MCP Operations (${activeOps.length})</h2>
        </div>
        ${activeOps
          .map(
            (op) => `
          <div class="card">
            <div class="card-head">
              <div>
                <div class="card-title">${esc(op.tool)}${op.session ? ` <span class="muted">on ${esc(op.session)}</span>` : ''}</div>
                <div class="card-sub">Running for ${esc(op.elapsed_sec)}s</div>
              </div>
              <span class="pill pill-info">running</span>
            </div>
          </div>
        `
          )
          .join('')}
      </section>
    `
    : '';

  const sessionCards = sessions.length
    ? sessions
        .map((s) => {
          const leaseBadge = s.lease_expires_in_min
            ? `<span class="pill pill-info">Warm ${Math.round(s.lease_expires_in_min)}m</span>`
            : `<span class="pill pill-muted">${s.untracked ? 'Untracked' : 'No Lease'}</span>`;
          const t = s.telemetry || null;
          const kBadge = t?.kernel_state
            ? `<span class="pill ${t.kernel_state === 'busy' ? 'pill-warn' : 'pill-ok'}">Kernel ${esc(t.kernel_state)}</span>`
            : '';
          const barCls = (pct) => (pct >= 90 ? 'fill-err' : pct >= 75 ? 'fill-warn' : 'fill-info');
          const resItems = [];
          if (t && t.gpu_util_pct != null) {
            const pct = Math.min(100, Math.max(0, Number(t.gpu_util_pct)));
            resItems.push(`
              <div class="res-item">
                <div class="res-top"><span class="res-lbl">GPU</span><span>${pct.toFixed(0)}%</span></div>
                <div class="meter-sm"><div class="${barCls(pct)}" style="width:${pct.toFixed(1)}%"></div></div>
              </div>
            `);
          }
          if (t && t.vram_total_gb > 0) {
            const pct = Math.min(100, Math.max(0, Number(t.vram_pct ?? 0)));
            resItems.push(`
              <div class="res-item">
                <div class="res-top"><span class="res-lbl">VRAM</span><span>${Number(t.vram_used_gb ?? 0).toFixed(1)} / ${Number(t.vram_total_gb).toFixed(0)} GiB</span></div>
                <div class="meter-sm"><div class="${barCls(pct)}" style="width:${pct.toFixed(1)}%"></div></div>
              </div>
            `);
          }
          if (t && t.ram_total_gb > 0) {
            const pct = Math.min(100, Math.max(0, Number(t.ram_pct ?? 0)));
            resItems.push(`
              <div class="res-item">
                <div class="res-top"><span class="res-lbl">RAM</span><span>${Number(t.ram_used_gb ?? 0).toFixed(1)} / ${Number(t.ram_total_gb).toFixed(0)} GiB</span></div>
                <div class="meter-sm"><div class="${barCls(pct)}" style="width:${pct.toFixed(1)}%"></div></div>
              </div>
            `);
          }
          if (t && t.cpu_pct != null) {
            const pct = Math.min(100, Math.max(0, Number(t.cpu_pct)));
            const cpuLbl = t.cpu_count ? `CPU (${t.cpu_count}c)` : 'CPU';
            const loadTxt = t.load_1m != null ? ` · L${Number(t.load_1m).toFixed(2)}` : '';
            resItems.push(`
              <div class="res-item">
                <div class="res-top"><span class="res-lbl">${esc(cpuLbl)}</span><span>${pct.toFixed(1)}%${esc(loadTxt)}</span></div>
                <div class="meter-sm"><div class="${barCls(pct)}" style="width:${pct.toFixed(1)}%"></div></div>
              </div>
            `);
          }
          if (t && t.disk_total_gb > 0) {
            const pct = Math.min(100, Math.max(0, Number(t.disk_pct ?? 0)));
            resItems.push(`
              <div class="res-item">
                <div class="res-top"><span class="res-lbl">Disk</span><span>${Number(t.disk_used_gb ?? 0).toFixed(1)} / ${Number(t.disk_total_gb).toFixed(0)} GiB</span></div>
                <div class="meter-sm"><div class="${barCls(pct)}" style="width:${pct.toFixed(1)}%"></div></div>
              </div>
            `);
          }
          const resGrid = resItems.length ? `<div class="res-grid">${resItems.join('')}</div>` : '';
          const hw = `${s.accelerator || 'CPU'} · ${s.machine_shape || 'STANDARD'}`;
          const openBtn =
            canOpenLinks && s.web_url
              ? `<button class="btn btn-ghost" data-action="open-url" data-url="${esc(s.web_url)}">Open Web UI</button>`
              : '';
          const namedActions = s.untracked
            ? ''
            : `
                <button class="btn" data-action="reset-vram" data-session="${esc(s.name)}">Flush Zombie VRAM</button>
                <button class="btn" data-action="keep-warm" data-session="${esc(s.name)}">Keep Warm 30m</button>
              `;
          return `
            <div class="card session-card">
              <div class="card-head">
                <div>
                  <div class="card-title">${esc(s.display_name || s.name)}</div>
                  <div class="card-sub">${esc(hw)} · ${esc(s.status || 'unknown')}</div>
                </div>
                <div class="badges">
                  ${kBadge}
                  ${leaseBadge}
                </div>
              </div>
              ${resGrid}
              <div class="btn-row">
                ${namedActions}
                ${openBtn}
                <button class="btn btn-danger" data-action="stop-one" data-session="${esc(s.name)}">Stop VM</button>
              </div>
            </div>
          `;
        })
        .join('')
    : `<div class="empty">${isLoading ? 'Loading active Colab sessions...' : 'No active Colab VMs allocated (0.00 CU/hr).'}</div>`;

  const jobCards = jobs.length
    ? jobs
        .map((j) => {
          const st = String(j.state || 'unknown');
          const pillCls =
            st === 'completed'
              ? 'pill-ok'
              : st === 'failed' || st === 'vm_unreachable'
                ? 'pill-err'
                : 'pill-warn';
          return `
            <div class="card job-card">
              <div class="card-head">
                <div>
                  <div class="card-title">${esc(j.job_id)} <span class="muted">on ${esc(j.session)}</span></div>
                  <div class="card-sub">${esc(j.script_name || '')} · GPU ${esc(j.gpu_util ?? '—')} · VRAM ${esc(j.vram_used ?? '—')} · CPU ${j.cpu_pct != null ? `${Number(j.cpu_pct).toFixed(1)}%` : '—'}</div>
                </div>
                <span class="pill ${pillCls}">${esc(st)}</span>
              </div>
              ${j.tail_line ? `<pre class="log-line">${esc(j.tail_line)}</pre>` : ''}
              <div class="btn-row">
                <button class="btn" data-action="poll-job" data-session="${esc(j.session)}" data-job="${esc(j.job_id)}">Poll Status</button>
              </div>
            </div>
          `;
        })
        .join('')
    : `<div class="empty">No detached jobs tracked yet.</div>`;

  const transferSection = transfers.length
    ? `
      <section class="sec">
        <div class="sec-hdr">
          <h2>Background Transfers (${transfers.length})</h2>
        </div>
        ${transfers
          .map((t) => {
            const st = String(t.state || 'unknown');
            const pct = t.percent != null ? `${Number(t.percent).toFixed(1)}%` : '—';
            const pillCls = st === 'completed' ? 'pill-ok' : st === 'failed' ? 'pill-err' : 'pill-info';
            return `
              <div class="card">
                <div class="card-head">
                  <div>
                    <div class="card-title">${esc(t.transfer_id || t.job_id)} <span class="muted">${esc(t.phase || '')}</span></div>
                    <div class="card-sub">${esc(t.drive_target || '')} · ${pct}</div>
                  </div>
                  <span class="pill ${pillCls}">${esc(st)}</span>
                </div>
                <div class="btn-row">
                  <button class="btn" data-action="poll-job" data-session="${esc(t.session || '')}" data-job="${esc(t.transfer_id || t.job_id)}">Poll Transfer</button>
                </div>
              </div>
            `;
          })
          .join('')}
      </section>
    `
    : '';

  const keepaliveBadge = keepalive.running
    ? `<span class="pill pill-info">Keepalive PID ${esc(keepalive.pid)}</span>`
    : `<span class="pill pill-muted">Keepalive Idle</span>`;

  root.innerHTML = `
    <div class="wrap">
      <header class="hdr">
        <div>
          <h1>Colab Pilot</h1>
          <div class="sub">Zero-Waste Compute &amp; Detached Job Control</div>
        </div>
        <button class="btn btn-primary" data-action="refresh">Refresh</button>
      </header>

      <div id="banner" class="banner ${bannerState.isError ? 'banner-err' : 'banner-info'}" style="display:${bannerState.msg || isLoading ? 'block' : 'none'}">${esc(bannerState.msg || (isLoading ? 'Syncing live Colab telemetry...' : ''))}</div>

      <section class="kpi-grid">
        <div class="kpi">
          <div class="kpi-label">CU Balance</div>
          <div class="kpi-val">${isLoading ? '—' : `${balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} CU`}${staleBadge}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Live Burn Rate</div>
          <div class="kpi-val"><span class="${isLoading ? 'pill pill-muted' : rateClass}">${esc(rateText)}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Active Assignments</div>
          <div class="kpi-val">${isLoading ? '—' : activeCount}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">1/5th Monthly Cap (${Math.round(budgetCap).toLocaleString()} CU)</div>
          <div class="meter"><div class="meter-fill" style="width:${isLoading ? 0 : pctRemainingOfCap.toFixed(1)}%"></div></div>
        </div>
      </section>

      ${opsSection}
      ${transferSection}

      <section class="sec">
        <div class="sec-hdr">
          <h2>Active Sessions (${sessions.length})</h2>
          ${sessions.length ? `<button class="btn btn-danger" data-action="stop-all">Stop All VMs</button>` : ''}
        </div>
        ${sessionCards}
      </section>

      <section class="sec">
        <div class="sec-hdr">
          <h2>Detached Jobs (${jobs.length})</h2>
        </div>
        ${jobCards}
      </section>

      <section class="sec alarm-bar">
        <div>
          <strong>Wakeup Alarm:</strong>
          <span class="pill ${alarm.running ? 'pill-warn' : 'pill-muted'}">${alarm.running ? 'ACTIVE' : 'Idle'}</span>
          ${keepaliveBadge}
        </div>
        <div class="btn-row">
          <button class="btn" data-action="alarm-test">Test Chirp</button>
          <button class="btn btn-danger" data-action="alarm-stop">Silence Alarm</button>
        </div>
      </section>
    </div>
  `;

  root.querySelectorAll('button[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const act = btn.getAttribute('data-action');
      const sess = btn.getAttribute('data-session') || '';
      const job = btn.getAttribute('data-job') || '';
      const url = btn.getAttribute('data-url') || '';

      if (act === 'refresh') runTool('colab_dashboard', {}, 'Refresh');
      else if (act === 'stop-all') runTool('colab_stop_all', {}, 'Stop All VMs');
      else if (act === 'stop-one') runTool('colab_stop_all', { session: sess }, `Stop ${sess}`);
      else if (act === 'reset-vram') runTool('colab_reset_vram', { session: sess }, `Flush VRAM (${sess})`);
      else if (act === 'keep-warm') runTool('colab_keep_warm', { session: sess, minutes: 30, reason: 'Dashboard lease' }, `Keep Warm ${sess}`);
      else if (act === 'poll-job') runTool('colab_poll_job', { session: sess, job_id: job }, `Poll ${job}`);
      else if (act === 'alarm-test') runTool('colab_wakeup_alarm', { action: 'test_chirp' }, 'Test Chirp');
      else if (act === 'alarm-stop') runTool('colab_wakeup_alarm', { action: 'stop' }, 'Silence Alarm');
      else if (act === 'open-url' && url) app.openLink({ url });
    });
  });
}

// Register handlers BEFORE connect()
app.ontoolinput = () => {
  render(currentState);
};

app.ontoolresult = (result) => {
  receivedInitialResult = true;
  if (result?.isError) {
    const txt = (result.content || []).map((c) => c.text || '').join(' ');
    render(currentState);
    setBanner(txt || 'Dashboard telemetry query returned an error.', true);
    return;
  }
  if (result?.structuredContent) {
    currentState = result.structuredContent;
    render(currentState);
    setBanner('');
  }
};

app.onhostcontextchanged = (ctx) => {
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
};

async function init() {
  render(currentState);
  await app.connect();
  const ctx = app.getHostContext();
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  render(currentState);

  window.setTimeout(() => {
    if (!receivedInitialResult && !currentState && !busyAction) {
      runTool('colab_dashboard', {}, 'Sync');
    }
  }, 1500);

  window.setInterval(() => {
    if (document.visibilityState !== 'visible' || busyAction || !currentState) return;
    const hasActivity =
      (currentState.active_operations || []).length > 0 ||
      Number(currentState.usage?.active_assignments || 0) > 0 ||
      (currentState.jobs || []).some((j) => j.state === 'running') ||
      (currentState.transfers || []).some((t) => t.state === 'running');
    if (hasActivity) {
      runTool('colab_dashboard', {}, 'Auto-refresh');
    }
  }, 12000);
}

init();
