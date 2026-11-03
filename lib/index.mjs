/**
 * dsh-cnki-dl — 知网（CNKI）检索与 PDF 批量下载。
 *
 * 宿主半只做三件事：把工具参数翻成 `run.py` 的 argv、跑子进程、把结果压成文本。
 * 真正的检索 / 验证码 / 下载逻辑全在 `python/` 里（内嵌源码，不依赖外部目录）。
 *
 * 六个工具：cnki_search / cnki_download / cnki_batch / cnki_verify / cnki_doctor / cnki_task_status
 *
 * @module dsh-cnki-dl
 */
import { buildBatchArgs, buildDoctorArgs, buildDownloadArgs, buildSearchArgs, buildVerifyArgs, SEARCH_TYPES } from './args.mjs'
import { DEFAULTS, resolveConfig } from './config.mjs'
import { PLUGIN_DIR, PYTHON_DIR, runPy, renderRun } from './py.mjs'
import { getDefineTool } from './tools.mjs'

export const name = 'dsh-cnki-dl'

/** 只需要 tools 服务；其余一律走 `ctx.get`。 */
export const inject = []

/** 后台批量任务表（工具返回 jobId，cnki_task_status 查进度）。 */
const jobs = new Map()

/** 生成一个短 jobId。 */
function newJobId() {
  return 'cnki-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6)
}

/**
 * 装载入口。
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {Record<string, unknown>} rawConfig
 */
export function apply(ctx, rawConfig) {
  const config = resolveConfig(rawConfig)
  const logger = ctx.get?.('logger')

  /** 跑一次 run.py，统一错误兜底。 */
  async function run(argv) {
    const result = await runPy(argv, { pythonExe: config.pythonExe || DEFAULTS.pythonExe, timeoutMs: config.timeoutMs })
    if (result.code !== 0 && result.stderr) {
      logger?.warn?.(`[cnki-dl] ${argv[0]} 退出码 ${result.code}: ${result.stderr.slice(0, 300)}`)
    }
    return result
  }

  const outputDirOf = (given) => {
    const value = typeof given === 'string' && given.trim() !== '' ? given.trim() : config.outputDir
    return value
  }

  ctx.inject(['tools'], (sctx) => {
    void (async () => {
      const defineTool = await getDefineTool()
      const disposers = []

      // ---- cnki_search：检索并按下载量排序 ----
      disposers.push(sctx.tools.register(defineTool({
        name: 'cnki_search',
        description:
          '在知网（CNKI）检索文献，结果已按下载量降序。返回标题、作者、来源、年份、被引/下载量与详情页 URL。'
          + 'type=all 会切到期刊库（同时含学位论文）；thesis 只学位论文。',
        parameters: {
          query: { type: 'string', description: '检索词；空格分隔表示 AND，例如「农业文化遗产 空间规划」' },
          type: { type: 'string', description: '库类型：all（默认）/ journal / thesis', enum: SEARCH_TYPES },
          top: { type: 'number', description: '每页条数，默认 20' },
          pages: { type: 'number', description: '翻页数，默认 1' },
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: {
              ok: { type: 'boolean' },
              text: { type: 'string' },
            },
          },
          render(_args, value) {
            return [{ type: 'text', text: value.text }]
          },
        },
        async execute(args) {
          const result = await run(buildSearchArgs({
            query: args.query,
            type: args.type,
            top: args.top,
            pages: args.pages,
          }))
          const text = renderRun(`知网检索：${args.query}`, result)
          return { ok: result.code === 0, text }
        },
      })))

      // ---- cnki_download：下载单篇 ----
      disposers.push(sctx.tools.register(defineTool({
        name: 'cnki_download',
        description: '按详情页 URL 下载单篇文献。默认要 PDF；只有 CAJ 时才降级（可用 want 指定）。',
        parameters: {
          url: { type: 'string', description: '知网详情页 URL' },
          outputDir: { type: 'string', description: '下载落点目录；省略则用配置里的 outputDir' },
          want: { type: 'string', description: '要的格式：pdf（默认）/ caj', enum: ['pdf', 'caj'] },
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: { ok: { type: 'boolean' }, text: { type: 'string' } },
          },
          render(_args, value) {
            return [{ type: 'text', text: value.text }]
          },
        },
        async execute(args) {
          const result = await run(buildDownloadArgs({
            url: args.url,
            outputDir: outputDirOf(args.outputDir),
            wantFormat: args.want ?? config.wantFormat,
          }))
          return { ok: result.code === 0, text: renderRun('下载单篇', result) }
        },
      })))

      // ---- cnki_batch：批量下载（后台跑，返回 jobId）----
      disposers.push(sctx.tools.register(defineTool({
        name: 'cnki_batch',
        description:
          '批量下载：给 URL 清单文件（每行 URL 或「标题<TAB>URL」）连续下载，或直接给检索词按下载量取前 N 篇。'
          + '这是长任务，立即返回 jobId，用 cnki_task_status 查进度。',
        parameters: {
          listPath: { type: 'string', description: 'URL 清单文件路径（与 query 二选一）' },
          query: { type: 'string', description: '检索词（与 listPath 二选一）' },
          type: { type: 'string', description: '检索库类型：all / journal / thesis', enum: SEARCH_TYPES },
          top: { type: 'number', description: '检索时取前 N 篇' },
          limit: { type: 'number', description: '最多下载几篇' },
          outputDir: { type: 'string', description: '下载落点目录' },
          want: { type: 'string', description: '要的格式：pdf（默认）/ caj', enum: ['pdf', 'caj'] },
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: { ok: { type: 'boolean' }, jobId: { type: 'string' }, text: { type: 'string' } },
          },
          render(_args, value) {
            return [{ type: 'text', text: value.text }]
          },
        },
        async execute(args) {
          const jobId = newJobId()
          const argv = buildBatchArgs({
            listPath: args.listPath,
            outputDir: outputDirOf(args.outputDir),
            wantFormat: args.want ?? config.wantFormat,
          })
          // 检索词路线要补上 kw/--type/--top/--limit
          if (!args.listPath && args.query) {
            argv.splice(1, 0, String(args.query))
            if (args.type) argv.push('--type', SEARCH_TYPES.includes(args.type) ? args.type : 'journal')
            if (typeof args.top === 'number') argv.push('--top', String(Math.trunc(args.top)))
          }
          if (typeof args.limit === 'number') argv.push('--limit', String(Math.trunc(args.limit)))

          const record = { id: jobId, startedAt: new Date().toISOString(), argv, state: 'running', stdout: '', stderr: '', code: null }
          jobs.set(jobId, record)
          void runPy(argv, { pythonExe: config.pythonExe || DEFAULTS.pythonExe, timeoutMs: 0 })
            .then((result) => {
              record.state = result.code === 0 ? 'done' : 'failed'
              record.stdout = result.stdout
              record.stderr = result.stderr
              record.code = result.code
              record.finishedAt = new Date().toISOString()
            })
            .catch((error) => {
              record.state = 'failed'
              record.stderr = String(error?.message ?? error)
              record.finishedAt = new Date().toISOString()
            })
          return {
            ok: true,
            jobId,
            text: `已启动后台批量下载：${jobId}\n命令：${argv.join(' ')}\n用 cnki_task_status 查进度。`,
          }
        },
      })))

      // ---- cnki_verify：按魔数复核目录 ----
      disposers.push(sctx.tools.register(defineTool({
        name: 'cnki_verify',
        description: '按文件魔数复核一个目录里的下载结果：真 PDF（%PDF-）、伪 PDF（其实是 CAJ）、空文件分别列出。',
        parameters: {
          outputDir: { type: 'string', description: '要复核的目录；省略则用配置里的 outputDir' },
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: { ok: { type: 'boolean' }, text: { type: 'string' } },
          },
          render(_args, value) {
            return [{ type: 'text', text: value.text }]
          },
        },
        async execute(args) {
          const dir = outputDirOf(args.outputDir)
          if (!dir) return { ok: false, text: '没有指定目录：请传 outputDir，或在插件配置里设 outputDir。' }
          const result = await run(buildVerifyArgs({ outputDir: dir }))
          return { ok: result.code === 0, text: renderRun(`复核 ${dir}`, result) }
        },
      })))

      // ---- cnki_doctor：环境体检 ----
      disposers.push(sctx.tools.register(defineTool({
        name: 'cnki_doctor',
        description: '环境体检：python / bsk 路径、输出目录可写、下载器扩展冲突、验证码识别链路。',
        parameters: {
          outputDir: { type: 'string', description: '顺带检查这个目录可写' },
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: { ok: { type: 'boolean' }, text: { type: 'string' } },
          },
          render(_args, value) {
            return [{ type: 'text', text: value.text }]
          },
        },
        async execute(args) {
          const result = await run(buildDoctorArgs({ outputDir: outputDirOf(args.outputDir) }))
          return { ok: result.code === 0, text: renderRun('环境体检', result) }
        },
      })))

      // ---- cnki_task_status：后台任务进度 ----
      disposers.push(sctx.tools.register(defineTool({
        name: 'cnki_task_status',
        description: '查 cnki_batch 的后台进度。省略 jobId 则列出全部任务。',
        parameters: {
          jobId: { type: 'string', description: 'cnki_batch 返回的 jobId' },
          tail: { type: 'number', description: '只回最后 N 行日志，默认 30' },
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: { ok: { type: 'boolean' }, text: { type: 'string' } },
          },
          render(_args, value) {
            return [{ type: 'text', text: value.text }]
          },
        },
        async execute(args) {
          const tail = typeof args.tail === 'number' && args.tail > 0 ? Math.trunc(args.tail) : 30
          if (!args.jobId) {
            if (jobs.size === 0) return { ok: true, text: '没有后台任务。' }
            const lines = [...jobs.values()].map((j) => `${j.id}  ${j.state}  起于 ${j.startedAt}`)
            return { ok: true, text: lines.join('\n') }
          }
          const job = jobs.get(args.jobId)
          if (!job) return { ok: false, text: `找不到任务 ${args.jobId}` }
          const outTail = job.stdout.trim().split('\n').slice(-tail).join('\n')
          const errTail = job.stderr.trim().split('\n').slice(-Math.min(tail, 10)).join('\n')
          const parts = [
            `任务 ${job.id}：${job.state}${job.code === null ? '' : `（退出码 ${job.code}）`}`,
            `起于 ${job.startedAt}${job.finishedAt ? `，止于 ${job.finishedAt}` : ''}`,
          ]
          if (outTail) parts.push('--- stdout ---', outTail)
          if (errTail) parts.push('--- stderr ---', errTail)
          return { ok: true, text: parts.join('\n') }
        },
      })))

      // 插件卸载时注销全部工具
      ctx.on?.('dispose', () => {
        for (const dispose of disposers) {
          try { dispose() } catch { /* 已注销 */ }
        }
        disposers.length = 0
      })
    })().catch((error) => {
      logger?.warn?.('[cnki-dl] 工具注册失败：' + String(error?.message ?? error))
    })
  })

  logger?.info?.(`[cnki-dl] 已装载，python 目录：${PYTHON_DIR}`)
}

export { PLUGIN_DIR, PYTHON_DIR }
