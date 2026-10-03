import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'
import { existsSync, unlinkSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(new URL('../..', import.meta.url).pathname)
const dbPath = '/tmp/rros-browser-smoke.db'
if (existsSync(dbPath)) unlinkSync(dbPath)
const env = { ...process.env, APP_ENV: 'development', DATABASE_URL: `sqlite:////${dbPath}`, AUTO_CREATE_SCHEMA: 'true', SEED_DEMO: 'true', LIVE_ACTIONS: 'false', ALLOWED_HOSTS: '*', PUBLIC_BASE_URL: 'http://127.0.0.1:8765' }
const server = spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765'], { cwd: root, env, stdio: 'ignore' })
const stop = () => { if (!server.killed) server.kill('SIGTERM') }
process.on('exit', stop)
try {
  let ready = false
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try { const response = await fetch('http://127.0.0.1:8765/api/health'); ready = response.ok; if (ready) break } catch {}
    await delay(250)
  }
  if (!ready) throw new Error('API did not become ready')
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()
  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: /Turn leakage into a governed operating system/i }).waitFor()
  await page.getByLabel('Organization slug').fill('acme-demo')
  await page.getByLabel('Email').fill('owner@example.com')
  await page.getByLabel('Password').fill('demo-password')
  await page.getByRole('button', { name: 'Enter command center' }).click()
  await page.getByRole('heading', { name: /Revenue, with receipts/i }).waitFor()
  await page.getByRole('button', { name: /Customer 360/i }).click()
  await page.getByRole('heading', { name: 'Customer 360' }).waitFor()
  await page.getByRole('button', { name: /Search workspace/i }).click()
  await page.getByRole('textbox', { name: 'Search workspace' }).fill('John')
  await page.getByRole('button', { name: /John Carter/i }).waitFor()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'Log out' }).click()
  await page.getByRole('heading', { name: /Turn leakage into a governed operating system/i }).waitFor()
  await browser.close()
  console.log(JSON.stringify({ passed: true, journey: ['anonymous_login', 'login', 'command_center', 'customer_360', 'global_search', 'logout'] }))
} finally {
  stop()
}
