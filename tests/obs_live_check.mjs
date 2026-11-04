// Live OBS check (manual): PixelPaw running + OBS open with WebSocket enabled (Tools → WebSocket Server Settings).
//   node tests/obs_live_check.mjs
// Does nothing while you are streaming or recording. Never switches your scenes. Read-only except for one test alert.
import { createHash, randomUUID } from 'node:crypto'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = Number(process.env.PIXELPAW_WEB_PORT || 8765)
const OUT = join(ROOT, 'output')
const fail = (msg) => { console.log('✖ ' + msg); process.exit(1) }
if (typeof WebSocket === 'undefined') fail('Node 22+ is needed (built-in WebSocket).')

const health = await fetch(`http://127.0.0.1:${PORT}/healthz`).then(r => r.json()).catch(() => null)
if (!health || health.app !== 'pixelpaw') fail(`PixelPaw is not running on port ${PORT}. Double-click PixelPaw first.`)
const token = readFileSync(join(ROOT, 'data', 'web_token'), 'utf8').trim()
const pp = (path, body) => fetch(`http://127.0.0.1:${PORT}${path}`, {
  method: body ? 'POST' : 'GET', headers: { 'X-PixelPaw-Token': token, 'Content-Type': 'application/json' },
  body: body ? JSON.stringify(body) : undefined }).then(r => r.json())

let cfg
try { cfg = JSON.parse(readFileSync(process.env.APPDATA + '/obs-studio/plugin_config/obs-websocket/config.json', 'utf8')) }
catch { fail('OBS WebSocket settings not found. In OBS: Tools → WebSocket Server Settings → Enable.') }
const sha = (s) => createHash('sha256').update(s).digest('base64')
const ws = new WebSocket(`ws://127.0.0.1:${cfg.server_port}`, 'obswebsocket.json')
const pending = new Map()
let ready; const identified = new Promise((r) => (ready = r))
ws.onerror = () => fail('Could not connect to OBS WebSocket. Is OBS open with the WebSocket server enabled?')
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.op === 0) {
    const a = msg.d.authentication
    ws.send(JSON.stringify({ op: 1, d: { rpcVersion: 1, eventSubscriptions: 0, ...(a ? { authentication: sha(sha(cfg.server_password + a.salt) + a.challenge) } : {}) } }))
  } else if (msg.op === 2) ready()
  else if (msg.op === 7) { pending.get(msg.d.requestId)?.(msg.d); pending.delete(msg.d.requestId) }
}
const call = async (t, d = {}) => {
  const r = await new Promise((res) => { const id = randomUUID(); pending.set(id, res); ws.send(JSON.stringify({ op: 6, d: { requestType: t, requestId: id, requestData: d } })) })
  if (!r.requestStatus.result) throw new Error(`${t}: ${r.requestStatus.comment || r.requestStatus.code}`)
  return r.responseData || {}
}

await identified
const v = await call('GetVersion')
console.log(`✔ OBS ${v.obsVersion} connected`)
if ((await call('GetStreamStatus')).outputActive || (await call('GetRecordStatus')).outputActive) fail('You are live or recording: not touching OBS.')

const inputs = (await call('GetInputList', { inputKind: 'browser_source' })).inputs
let found = null
for (const i of inputs) {
  const s = (await call('GetInputSettings', { inputName: i.inputName })).inputSettings
  if (s.is_local_file) { if (String(s.local_file).endsWith('.lua')) console.log(`⚠ "${i.inputName}" points to the .lua file. Click "Add overlay to current scene" in the PixelPaw script to fix it.`); continue }
  if (String(s.url || '').includes(`:${PORT}/overlay`)) { found = i.inputName; break }
}
if (!found) fail('No PixelPaw overlay in OBS yet. In OBS: Tools → Scripts → pixelpaw.lua → "Add overlay to current scene".')
console.log(`✔ Overlay source found: "${found}"`)
const active = await call('GetSourceActive', { sourceName: found })
console.log(active.videoShowing ? '✔ Overlay is visible in the current scene' : '⚠ Overlay exists but is not in the scene you are showing')

await pp('/api/test', {})
await new Promise((r) => setTimeout(r, 1500))
const shot = await call('GetSourceScreenshot', { sourceName: found, imageFormat: 'png', imageWidth: 1280 })
mkdirSync(OUT, { recursive: true })
const file = join(OUT, 'obs_overlay_check.png')
writeFileSync(file, Buffer.from(shot.imageData.split(',')[1], 'base64'))
const h = await pp('/api/health')
console.log(h.overlay_clients > 0 ? `✔ PixelPaw sees ${h.overlay_clients} overlay connection(s)` : '✖ OBS is not connected to PixelPaw (reload the overlay)')
console.log(`✔ Screenshot with a test alert: ${file}`)
ws.close()
process.exit(h.overlay_clients > 0 ? 0 : 1)
