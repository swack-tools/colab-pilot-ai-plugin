import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';

const res = await esbuild.build({
  entryPoints: ['app.js'],
  bundle: true,
  format: 'iife',
  minify: true,
  write: false,
});

const js = res.outputFiles[0].text;
const safeJs = js.replace(/<\/script/gi, '<\\/script');
const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Colab Pilot</title>
<style>
  :root {
    --bg0: var(--color-background-primary, #0f172a);
    --bg1: var(--color-background-secondary, #1e293b);
    --bg2: var(--color-background-tertiary, #334155);
    --fg0: var(--color-text-primary, #f8fafc);
    --fg1: var(--color-text-secondary, #cbd5e1);
    --fg2: var(--color-text-tertiary, #94a3b8);
    --bd: var(--color-border-primary, #334155);
    --ok: var(--color-text-success, #22c55e);
    --warn: var(--color-text-warning, #f59e0b);
    --err: var(--color-text-danger, #ef4444);
    --info: var(--color-text-info, #38bdf8);
    font-family: var(--font-sans, system-ui, -apple-system, sans-serif);
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 14px; background: var(--bg0); color: var(--fg0); font-size: 13px; }
  .wrap { display: flex; flex-direction: column; gap: 14px; max-width: 960px; margin: 0 auto; }
  .hdr { display: flex; justify-content: space-between; align-items: center; }
  .hdr h1 { margin: 0; font-size: 17px; }
  .sub { color: var(--fg2); font-size: 12px; }
  .banner { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--bd); background: var(--bg1); font-size: 12px; }
  .banner-err { border-color: var(--err); color: var(--err); }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; }
  .kpi { background: var(--bg1); border: 1px solid var(--bd); border-radius: 10px; padding: 10px 12px; }
  .kpi-label { color: var(--fg2); font-size: 11px; margin-bottom: 4px; }
  .kpi-val { font-size: 16px; font-weight: 600; }
  .meter { height: 7px; border-radius: 999px; background: var(--bg2); overflow: hidden; margin-top: 8px; }
  .meter-fill { height: 100%; background: linear-gradient(90deg, #f9ab00, #38bdf8); }
  .sec { display: flex; flex-direction: column; gap: 8px; }
  .sec-hdr { display: flex; justify-content: space-between; align-items: center; }
  .sec-hdr h2 { margin: 0; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--fg2); }
  .card { background: var(--bg1); border: 1px solid var(--bd); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
  .card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .card-title { font-weight: 600; font-size: 14px; }
  .card-sub { color: var(--fg2); font-size: 12px; }
  .btn-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .btn { cursor: pointer; border: 1px solid var(--bd); background: var(--bg2); color: var(--fg0); border-radius: 6px; padding: 5px 10px; font-size: 12px; }
  .btn:hover { opacity: 0.9; }
  .btn-primary { background: #0284c7; border-color: #0284c7; color: #fff; }
  .btn-danger { background: rgba(239,68,68,0.16); border-color: var(--err); color: var(--err); }
  .btn-ghost { background: transparent; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid var(--bd); }
  .pill-ok { color: var(--ok); border-color: var(--ok); }
  .pill-warn { color: var(--warn); border-color: var(--warn); }
  .pill-err { color: var(--err); border-color: var(--err); }
  .pill-info { color: var(--info); border-color: var(--info); }
  .pill-muted { color: var(--fg2); }
  .badges { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .res-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 8px; padding-top: 2px; }
  .res-item { background: var(--bg0); border: 1px solid var(--bd); border-radius: 7px; padding: 6px 8px; }
  .res-top { display: flex; justify-content: space-between; align-items: baseline; font-size: 11px; color: var(--fg1); gap: 6px; }
  .res-lbl { color: var(--fg2); font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; font-size: 10px; }
  .meter-sm { height: 5px; border-radius: 999px; background: var(--bg2); overflow: hidden; margin-top: 5px; }
  .fill-ok { height: 100%; background: var(--ok); }
  .fill-info { height: 100%; background: var(--info); }
  .fill-warn { height: 100%; background: var(--warn); }
  .fill-err { height: 100%; background: var(--err); }
  .log-line { margin: 0; padding: 6px 8px; background: var(--bg0); border-radius: 6px; font-size: 11px; overflow-x: auto; color: var(--fg1); }
  .empty { color: var(--fg2); font-size: 12px; padding: 10px; background: var(--bg1); border: 1px dashed var(--bd); border-radius: 8px; }
  .alarm-bar { flex-direction: row; justify-content: space-between; align-items: center; background: var(--bg1); border: 1px solid var(--bd); border-radius: 10px; padding: 10px 12px; }
  .loading { color: var(--fg2); padding: 20px; text-align: center; }
</style>
</head>
<body>
<div id="app"><div class="loading">Connecting to Colab Pilot...</div></div>
<script>${safeJs}</script>
</body>
</html>
`;

const outPath = path.resolve(import.meta.dirname, '../plugins/colab-pilot/assets/dashboard.html');
fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, html, 'utf8');
console.log(`Bundled ${outPath} (${html.length} bytes)`);
