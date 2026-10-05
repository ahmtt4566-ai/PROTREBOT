import {spawnSync} from 'node:child_process'
import {resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

export function runCoinLogoRefresh({args = [], env = process.env, spawn = spawnSync} = {}) {
  const result = spawn(process.execPath, [fileURLToPath(new URL('./fetch-coin-logos.mjs', import.meta.url)), ...args], {stdio: 'inherit', env})
  if (result.error || result.status !== 0) {
    console.warn(`[coin-logos] Refresh process failed (${result.error?.message ?? `exit ${result.status}, signal ${result.signal ?? 'none'}`}); existing logos retained, build continues.`)
    return false
  }
  return true
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) runCoinLogoRefresh({args: process.argv.slice(2)})
