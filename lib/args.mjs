/**
 * 把工具参数翻译成 `run.py` 的 argv。
 *
 * 全部集中在这里，是为了只有一处能拼错——历史 bug 就是子命令被拼到了
 * 可执行文件名上。子命令永远是 argv[0]，其余是选项。
 *
 * @module dsh-cnki-dl/args
 */

/** `--type` 的合法值（run.py 认这三个）。 */
export const SEARCH_TYPES = ['all', 'journal', 'thesis']

function clampInt(value, min, max, fallback) {
  const n = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10)
  if (!Number.isFinite(n)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(n)))
}

function pushIf(out, flag, value) {
  if (value === undefined || value === null || value === '') return
  out.push(flag, String(value))
}

/** `cnki_search` → `['search', <query>, '--type', t, '--top', n, '--pages', p, '--json', path]` */
export function buildSearchArgs({ query, type, top, pages, jsonPath }) {
  const out = ['search', String(query ?? '').trim()]
  const kind = SEARCH_TYPES.includes(type) ? type : 'all'
  out.push('--type', kind)
  out.push('--top', String(clampInt(top, 1, 100, 20)))
  out.push('--pages', String(clampInt(pages, 1, 10, 1)))
  pushIf(out, '--json', jsonPath)
  return out
}

/** `cnki_download` → `['download', <url>, '--out', dir]` */
export function buildDownloadArgs({ url, outputDir, wantFormat }) {
  const out = ['download', String(url ?? '').trim()]
  pushIf(out, '--out', outputDir)
  pushIf(out, '--want', wantFormat)
  return out
}

/** `cnki_batch` → `['batch', '--list', file, '--out', dir, '--want', fmt]` */
export function buildBatchArgs({ listPath, outputDir, wantFormat }) {
  const out = ['batch']
  pushIf(out, '--list', listPath)
  pushIf(out, '--out', outputDir)
  pushIf(out, '--want', wantFormat)
  return out
}

/** `cnki_verify` → `['verify', <dir>]`（位置参数，不认 --dir） */
export function buildVerifyArgs({ outputDir }) {
  return ['verify', String(outputDir ?? '').trim()]
}

/** `cnki_doctor` → `['doctor', '--out', dir]` */
export function buildDoctorArgs({ outputDir }) {
  const out = ['doctor']
  pushIf(out, '--out', outputDir)
  return out
}
