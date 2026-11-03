/**
 * 配置读取：只认 profile 里 `config:` 段，键都有默认值，坏值不抛。
 *
 * @module dsh-cnki-dl/config
 */

/** 默认配置。 */
export const DEFAULTS = {
  /** Python 解释器路径；留空走 PATH 里的 `python`。 */
  pythonExe: '',
  /** 单次子进程超时（毫秒）。 */
  timeoutMs: 120000,
  /** 下载落点；空则用 `<plugin>/python/data/cnki`。 */
  outputDir: '',
  /** 优先要的文件格式：pdf（默认）或 caj。 */
  wantFormat: 'pdf',
}

/** 把外部配置归一成安全形状：只保留认识的键，类型不对就回落默认值。 */
export function resolveConfig(raw) {
  const cfg = raw && typeof raw === 'object' ? raw : {}
  const pickString = (value, fallback) => (typeof value === 'string' && value.trim() !== '' ? value.trim() : fallback)
  const pickNumber = (value, fallback) => (typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback)
  return {
    pythonExe: pickString(cfg.pythonExe, DEFAULTS.pythonExe),
    timeoutMs: pickNumber(cfg.timeoutMs, DEFAULTS.timeoutMs),
    outputDir: pickString(cfg.outputDir, DEFAULTS.outputDir),
    wantFormat: cfg.wantFormat === 'caj' ? 'caj' : DEFAULTS.wantFormat,
  }
}
