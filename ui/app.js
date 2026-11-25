import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';

const app = new App({ name: 'Colab Pilot', version: '1.0.0' }, {});
let currentState = null;
let busyAction = '';

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function setBanner(msg, isError = false) {
  const el = document.getElementById('banner');
  if (!el) return;
  if (!msg) {
    el.style.display = 'none';
    el.textContent = '';
    return;
  }
  el.style.display = 'block';
  el.className = isError ? 'banner banner-err' : 'banner banner-info';
  el.textContent = msg;
}

async function runTool(name, args = {}, label = '') {
  busyAction = label || name;
  setBanner(`Running ${busyAction}...`);
  try {
    const res = await app.callServerTool({ name, arguments: args });
    if (res?.isError) {
      const txt = (res.content || []).map((c) => c.text || '').join(' ');
      setBanner(txt || `${name} failed`, true);
      return;
    }
    if (name === 'colab_dashboard' && res?.structuredContent) {
      currentState = res.structuredContent;
      render(currentState);
      setBanner('');
    } else {
      const txt = (res?.content || []).map((c) => c.text || '').join(' ');
      setBanner(txt ? txt.slice(0, 220) : `${name} completed`);
      // Auto-refresh dashboard after state-mutating actions
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
  if (!data) {
    root.innerHTML = `<div class="loading">Loading Colab Pilot telemetry...</div>`;
    return;
  }

  const usage = data.usage || {};
  const balance = Number(usage.balance ?? 0);
  const rate = Number(usage.rate_hourly ?? 0);
  const activeCount = Number(usage.active_assignments ?? 0);
  const monthlyCu = Number(data.ultra_monthly_cu ?? 25000);
  const budgetFrac = Number(data.budget_fraction ?? 0.2);
  const budgetCap = monthlyCu * budgetFrac;
  const pctRemainingOfCap = budgetCap > 0 ? Math.min(100, Math.max(0, (balance / budgetCap) * 100)) : 100;

  const sessions = Array.isArray(data.sessions) ? data.sessions : [];
  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  const alarm = data.alarm || {};
  const caps = app.getHostCapabilities() || {};
  const canOpenLinks = Boolean(caps.openLinks);

  const rateClass = rate > 0 ? 'pill pill-warn' : 'pill pill-ok';
  const rateText = rate > 0 ? `${rate.toFixed(2)} CU/hr` : '0.00 CU/hr (Idle)';

  const sessionCards = sessions.length
    ? sessions
        .map((s) => {
          const leaseBadge = s.lease_expires_in_min
            ? `<span class="pill pill-info">Warm ${Math.round(s.lease_expires_in_min)}m</span>`
            : `<span class="pill pill-muted">No Lease</span>`;
          const hw = `${s.accelerator || 'CPU'} · ${s.machine_shape || 'STANDARD'}`;
          const openBtn =
            canOpenLinks && s.web_url
              ? `<button class="btn btn-ghost" data-action="open-url" data-url="${esc(s.web_url)}">Open Web UI</button>`
              : '';
          return `
            <div class="card session-card">
              <div class="card-head">
                <div>
                  <div class="card-title">${esc(s.name)}</div>
                  <div class="card-sub">${esc(hw)} · ${esc(s.status || 'unknown')}</div>
                </div>
                <div class="badges">
                  ${leaseBadge}
                </div>
              </div>
              <div class="btn-row">
                <button class="btn" data-action="reset-vram" data-session="${esc(s.name)}">Flush Zombie VRAM</button>
                <button class="btn" data-action="keep-warm" data-session="${esc(s.name)}">Keep Warm 30m</button>
                ${openBtn}
                <button class="btn btn-danger" data-action="stop-one" data-session="${esc(s.name)}">Stop VM</button>
              </div>
            </div>
          `;
        })
        .join('')
    : `<div class="empty">No active Colab VMs allocated (0.00 CU/hr).</div>`;

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
                  <div class="card-sub">${esc(j.script_name || '')} · GPU ${esc(j.gpu_util ?? '—')} · VRAM ${esc(j.vram_used ?? '—')}</div>
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

  root.innerHTML = `
    <div class="wrap">
      <header class="hdr">
        <div>
          <h1>Colab Pilot</h1>
          <div class="sub">Zero-Waste Compute &amp; Detached Job Control</div>
        </div>
        <button class="btn btn-primary" data-action="refresh">Refresh</button>
      </header>

      <div id="banner" class="banner" style="display:none"></div>

      <section class="kpi-grid">
        <div class="kpi">
          <div class="kpi-label">CU Balance</div>
          <div class="kpi-val">${balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} CU</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Live Burn Rate</div>
          <div class="kpi-val"><span class="${rateClass}">${esc(rateText)}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Active Assignments</div>
          <div class="kpi-val">${activeCount}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">1/5th Monthly Cap (${Math.round(budgetCap).toLocaleString()} CU)</div>
          <div class="meter"><div class="meter-fill" style="width:${pctRemainingOfCap.toFixed(1)}%"></div></div>
        </div>
      </section>

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
        </div>
        <div class="btn-row">
          <button class="btn" data-action="alarm-test">Test Chirp (Self-Test)</button>
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
      else if (act === 'alarm-test') runTool('colab_wakeup_alarm', { action: 'self_test' }, 'Alarm Self-Test');
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
  if (result?.structuredContent) {
    currentState = result.structuredContent;
    render(currentState);
  }
};

app.onhostcontextchanged = (ctx) => {
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
};

async function init() {
  await app.connect();
  const ctx = app.getHostContext();
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  render(currentState);
}

init();
