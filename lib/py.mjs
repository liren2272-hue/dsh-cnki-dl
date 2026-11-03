/**
 * 调用内嵌 Python 下载器（`<plugin>/python/run.py`）。
 *
 * 这里存在的唯一理由：历史上工具层把子命令拼进了可执行文件名，导致
 *   python.exe search
 * → `can't open file '…\python\search'`，六个 cnki_* 工具全部失效。
 * 正确形状永远是
 *   <python> <plugin>/python/run.py <subcommand> [args…]
 * 所有子命令都必须经过本模块，禁止在别处再拼一次。
 *
 * @module dsh-cnki-dl/py
 */
import { spawn } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

/** 插件根目录。 */
export const PLUGIN_DIR = resolve(HERE, '..')

/** 内嵌 Python 下载器所在目录（`run.py` 的 cwd）。 */
export const PYTHON_DIR = join(PLUGIN_DIR, 'python')

/** 下载器入口脚本。 */
export const RUN_PY = join(PYTHON_DIR, 'run.py')

/** 默认 Python 解释器；可用 config.pythonExe 覆盖。 */
export const DEFAULT_PYTHON = 'python'

/**
 * 跑一次 `run.py`。
 *
 * @param {string[]} argv `run.py` 之后的参数，例如 `['search', '主题', '--top', '20']`
 * @param {{ pythonExe?: string, timeoutMs?: number, cwd?: string, env?: Record<string,string> }} [options]
 * @returns {Promise<{ code: number|null, stdout: string, stderr: string, timedOut: boolean, argv: string[] }>}
 */
export function runPy(argv, { pythonExe = DEFAULT_PYTHON, timeoutMs = 120000, cwd = PYTHON_DIR, env } = {}) {
  const fullArgv = [RUN_PY, ...argv.map((a) => String(a))]
  return new Promise((resolvePromise) => {
    let child
    try {
      child = spawn(pythonExe, fullArgv, {
        cwd,
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', ...env },
      })
    } catch (error) {
      resolvePromise({ code: null, stdout: '', stderr: String(error?.message ?? error), timedOut: false, argv: fullArgv })
      return
    }

    let stdout = ''
    let stderr = ''
    let timedOut = false
    const timer = timeoutMs > 0
      ? setTimeout(() => { timedOut = true; try { child.kill() } catch { /* 已退出 */ } }, timeoutMs)
      : null

    child.stdout?.on('data', (chunk) => { stdout += chunk.toString('utf8') })
    child.stderr?.on('data', (chunk) => { stderr += chunk.toString('utf8') })
    child.on('error', (error) => {
      if (timer) clearTimeout(timer)
      resolvePromise({ code: null, stdout, stderr: stderr + String(error?.message ?? error), timedOut, argv: fullArgv })
    })
    child.on('close', (code) => {
      if (timer) clearTimeout(timer)
      resolvePromise({ code, stdout, stderr, timedOut, argv: fullArgv })
    })
  })
}

/** 把一次 runPy 结果压成给模型看的文本（stdout 优先，失败时附 stderr 与真实 argv）。 */
export function renderRun(label, result) {
  const parts = []
  if (label) parts.push(label)
  const out = result.stdout.trim()
  const err = result.stderr.trim()
  if (out) parts.push(out)
  if (result.timedOut) parts.push('[超时] 已终止子进程')
  if (result.code !== 0) {
    parts.push(`[退出码 ${result.code}]`)
    if (err) parts.push(err)
  } else if (err) {
    parts.push(`[stderr] ${err}`)
  }
  return parts.join('\n')
}
