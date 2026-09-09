import { execFileSync, spawnSync } from 'node:child_process'
import { resolve } from 'node:path'

const commit = process.env.VITE_BUILD_COMMIT || (() => {
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
  } catch {
    return 'unknown'
  }
})()
const environment = { ...process.env, VITE_BUILD_COMMIT: commit }
const commands = [
  [resolve('node_modules/typescript/bin/tsc'), ['-b']],
  [resolve('node_modules/vite/bin/vite.js'), ['build']],
]

for (const [command, args] of commands) {
  const result = spawnSync(process.execPath, [command, ...args], { stdio: 'inherit', env: environment })
  if (result.status !== 0) process.exit(result.status ?? 1)
}
