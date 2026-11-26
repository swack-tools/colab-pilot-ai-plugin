export function applyDocumentTheme(theme) {
  if (!theme) return;
  const root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.style.colorScheme = theme;
}

export function applyHostStyleVariables(variables, root = document.documentElement) {
  if (!variables || typeof variables !== 'object') return;
  for (const [k, v] of Object.entries(variables)) {
    if (v !== undefined) root.style.setProperty(k, v);
  }
}

export class App {
  constructor(appInfo, appCapabilities = {}, options = { autoResize: true }) {
    this._appInfo = appInfo;
    this._appCapabilities = appCapabilities;
    this.options = options;
    this._nextId = 1;
    this._pending = new Map();
    this._initialized = false;
    this._hostCapabilities = undefined;
    this._hostInfo = undefined;
    this._hostContext = undefined;
    this.ontoolinput = undefined;
    this.ontoolinputpartial = undefined;
    this.ontoolresult = undefined;
    this.ontoolcancelled = undefined;
    this.onhostcontextchanged = undefined;
    this.onteardown = undefined;
    this._onMessage = (ev) => {
      const msg = ev.data;
      if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') return;
      if (msg.id !== undefined && msg.method === undefined) {
        const p = this._pending.get(msg.id);
        if (!p) return;
        this._pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else p.resolve(msg.result);
        return;
      }
      if (msg.id !== undefined && msg.method !== undefined) {
        if (msg.method === 'ping') {
          this._post({ jsonrpc: '2.0', id: msg.id, result: {} });
        } else if (msg.method === 'ui/resource-teardown') {
          Promise.resolve(this.onteardown ? this.onteardown(msg.params || {}) : {})
            .then((r) => this._post({ jsonrpc: '2.0', id: msg.id, result: r || {} }))
            .catch((e) => this._post({ jsonrpc: '2.0', id: msg.id, error: { code: -32603, message: String(e?.message || e) } }));
        }
        return;
      }
      const params = msg.params || {};
      if (msg.method === 'ui/notifications/tool-input') this.ontoolinput?.(params);
      else if (msg.method === 'ui/notifications/tool-input-partial') this.ontoolinputpartial?.(params);
      else if (msg.method === 'ui/notifications/tool-result') this.ontoolresult?.(params);
      else if (msg.method === 'ui/notifications/tool-cancelled') this.ontoolcancelled?.(params);
      else if (msg.method === 'ui/notifications/host-context-changed') {
        this._hostContext = { ...(this._hostContext || {}), ...params };
        this.onhostcontextchanged?.(params);
      }
    };
  }

  _post(payload) {
    window.parent.postMessage(payload, '*');
  }

  request(method, params = {}) {
    const id = this._nextId++;
    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject });
      this._post({ jsonrpc: '2.0', id, method, params });
    });
  }

  notification(method, params = {}) {
    this._post({ jsonrpc: '2.0', method, params });
  }

  getHostCapabilities() {
    return this._hostCapabilities;
  }

  getHostVersion() {
    return this._hostInfo;
  }

  getHostContext() {
    return this._hostContext;
  }

  async callServerTool({ name, arguments: args = {} }) {
    return this.request('tools/call', { name, arguments: args });
  }

  async openLink({ url }) {
    return this.request('ui/open-link', { url });
  }

  sendSizeChanged({ width, height }) {
    this.notification('ui/notifications/size-changed', { width, height });
  }

  setupSizeChangedNotifications() {
    let scheduled = false;
    let prevW = 0;
    let prevH = 0;
    const notify = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        const doc = document.documentElement;
        const prev = doc.style.height;
        doc.style.height = 'max-content';
        const h = Math.ceil(doc.getBoundingClientRect().height);
        doc.style.height = prev;
        const w = Math.ceil(window.innerWidth);
        if (w !== prevW || h !== prevH) {
          prevW = w;
          prevH = h;
          this.sendSizeChanged({ width: w, height: h });
        }
      });
    };
    notify();
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(notify);
      ro.observe(document.documentElement);
      ro.observe(document.body);
      return () => ro.disconnect();
    }
    return () => {};
  }

  async connect() {
    window.addEventListener('message', this._onMessage);
    const res = await this.request('ui/initialize', {
      appCapabilities: this._appCapabilities,
      appInfo: this._appInfo,
      protocolVersion: '2026-01-26',
    });
    this._hostCapabilities = res?.hostCapabilities;
    this._hostInfo = res?.hostInfo;
    this._hostContext = res?.hostContext;
    this.notification('ui/notifications/initialized', {});
    this._initialized = true;
    if (this.options?.autoResize) this.setupSizeChangedNotifications();
  }
}
