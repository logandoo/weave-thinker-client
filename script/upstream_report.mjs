// 上游追踪报告 — 对 chatbot 仓只读，产出 vendored 映射的逐文件三方分类。
// 用法: node script/upstream_report.mjs [--manifest upstream.toml] [--local-root <dir>]
//        [--json-out <path>] [--write] [--md-out <path>]
// 分类: in-sync · converged · ff-safe · local-mod · conflict · added-both ·
//       new-upstream · gone-upstream · gone-local · local-only · excluded
import { readFileSync, writeFileSync, readdirSync, statSync, mkdirSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'
import { parse } from 'smol-toml'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

function arg(name, dflt) {
  const i = process.argv.indexOf(name)
  return i >= 0 ? process.argv[i + 1] : dflt
}

function fail(msg) {
  console.error(`upstream_report: ${msg}`)
  process.exit(1)
}

function gitUp(up, args) {
  const r = execFileSync('git', ['-C', up, ...args], { encoding: 'utf8' })
  return r
}

// ls-tree 一次拿整目录 blob sha，避免逐文件 rev-parse
function treeBlobs(up, commit, sub) {
  // -z：NUL 分隔，防非 ASCII 路径被 quotePath 转义误伤
  const out = gitUp(up, ['ls-tree', '-r', '-z', commit, '--', sub])
  const m = new Map()
  for (const entry of out.split('\0')) {
    if (!entry) continue
    const meta = entry.slice(0, entry.indexOf('\t'))
    const path = entry.slice(entry.indexOf('\t') + 1)
    const sha = meta.split(' ')[2]
    if (sha) m.set(path, sha)
  }
  return m
}

function localFiles(localRoot, sub) {
  const base = join(localRoot, sub)
  const out = []
  if (!existsSync(base)) return out
  const walk = dir => {
    for (const e of readdirSync(dir)) {
      const p = join(dir, e)
      if (statSync(p).isDirectory()) walk(p)
      else out.push(relative(localRoot, p))
    }
  }
  walk(base)
  return out
}

function localSha(localRoot, rel) {
  const p = join(localRoot, rel)
  // 目录/非常规文件视为缺失（上游 dir→file 重构时本地残留目录不崩溃，按新增/缺失分诊）
  if (!existsSync(p) || !statSync(p).isFile()) return null
  return execFileSync('git', ['-C', localRoot, 'hash-object', '--', rel], { encoding: 'utf8' }).trim()
}

function isExcluded(upath, patterns) {
  const segs = upath.split('/')
  for (const p of patterns) {
    if (p.startsWith('*')) { if (upath.endsWith(p.slice(1))) return true }
    else if (p.includes('/')) { if (upath === p || upath.startsWith(p + '/')) return true }
    else if (segs.includes(p)) return true
  }
  return false
}

// B=基线 blob · U=上游目标 blob · L=本地 blob（null=不存在）
export function classify(B, U, L) {
  if (B !== null && B === U && U === L) return 'in-sync'
  if (U !== null && L !== null && U === L) return 'converged'
  if (B === L && U !== null && U !== B) return B === null ? 'new-upstream' : 'ff-safe'
  if (B !== null && B === U && L !== null && L !== B) return 'local-mod'
  if (B === null && U === null && L !== null) return 'local-only'
  if (B !== null && U === null && L !== null) return 'gone-upstream'
  if (B !== null && U !== null && L === null) return 'gone-local'
  if (B !== null && U === null && L === null) return 'gone-both'
  if (B === null && U !== null && L !== null) return 'added-both'
  if (B !== null && U !== null && L !== null) return 'conflict'
  return 'unknown'
}

function main() {
  const manifestPath = arg('--manifest', join(root, 'upstream.toml'))
  const localRoot = arg('--local-root', root)
  const jsonOut = arg('--json-out', join(localRoot, 'docs', 'upstream', 'upstream_state.json'))
  const doWrite = process.argv.includes('--write')
  const mdOut = arg('--md-out', null)

  let cfg
  try {
    cfg = parse(readFileSync(manifestPath, 'utf8'))
  } catch (e) {
    fail(`manifest parse failed: ${e.message}`)
  }
  const up = cfg.upstream && cfg.upstream.path
  if (!up || !existsSync(join(up, '.git'))) fail(`upstream repo not found: ${up}`)
  const ref = (cfg.upstream && cfg.upstream.ref) || 'master'
  const baseInput = cfg.upstream && cfg.upstream.base
  if (!baseInput) fail('manifest missing [upstream].base')
  const maps = Array.isArray(cfg.map) ? cfg.map : []
  const filemaps = Array.isArray(cfg.filemap) ? cfg.filemap : []
  const patterns = (cfg.exclude && Array.isArray(cfg.exclude.patterns)) ? cfg.exclude.patterns : []
  if (!maps.length && !filemaps.length) fail('manifest has no [[map]]/[[filemap]]')

  const target = gitUp(up, ['rev-parse', ref]).trim()
  const base = gitUp(up, ['rev-parse', baseInput]).trim()
  const statusClean = gitUp(up, ['status', '--porcelain']).trim() === ''

  const files = {}
  const consider = (upath, lpath, b, u) => {
    if (files[upath]) return
    const l = localSha(localRoot, lpath)
    const category = isExcluded(upath, patterns) ? 'excluded' : classify(b ?? null, u ?? null, l)
    files[upath] = { local: lpath, category }
  }

  for (const m of maps) {
    const bTree = treeBlobs(up, base, m.from)
    const uTree = treeBlobs(up, target, m.from)
    const seen = new Set([...bTree.keys(), ...uTree.keys()])
    for (const upath of seen) {
      consider(upath, join(m.to, upath.slice(m.from.length + 1)), bTree.get(upath) ?? null, uTree.get(upath) ?? null)
    }
    for (const lp of localFiles(localRoot, m.to)) {
      const upath = m.from + '/' + relative(m.to, lp)
      if (!seen.has(upath)) consider(upath, lp, null, null)
    }
  }
  for (const fm of filemaps) {
    let b = null, u = null
    try { b = execFileSync('git', ['-C', up, 'rev-parse', `${base}:${fm.from}`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim() } catch { /* 基线无此文件 */ }
    try { u = execFileSync('git', ['-C', up, 'rev-parse', `${target}:${fm.from}`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim() } catch { /* 目标无此文件 */ }
    consider(fm.from, fm.to, b, u)
  }

  const counts = {}
  for (const v of Object.values(files)) counts[v.category] = (counts[v.category] || 0) + 1

  const state = {
    generated_at: new Date().toISOString(),
    upstream: { path: up, ref, base, target, status_clean: statusClean },
    counts,
    files
  }
  mkdirSync(dirname(jsonOut), { recursive: true })
  writeFileSync(jsonOut, JSON.stringify(state, null, 2) + '\n')

  if (doWrite) {
    const date = state.generated_at.slice(0, 10)
    const mdPath = mdOut || join(localRoot, 'docs', 'upstream', `UPSTREAM_SYNC_${date}.md`)
    const order = ['conflict', 'added-both', 'ff-safe', 'new-upstream', 'gone-upstream', 'gone-local', 'gone-both', 'local-mod', 'converged', 'local-only', 'in-sync', 'excluded', 'unknown']
    const lines = [
      `# 上游同步报告 — ${date}`,
      '',
      `- 上游: \`${up}\` ref=\`${ref}\` 工作树${statusClean ? '干净' : '**脏（非本工具造成，仅记录）**'}`,
      `- 基线: \`${base.slice(0, 9)}\` → 目标: \`${target.slice(0, 9)}\``,
      `- 清单: \`${relative(localRoot, manifestPath)}\` · 本报告由 \`script/linux/upstream_report.sh\` 生成，可重复运行`,
      '',
      '| 分类 | 数量 | 含义 |',
      '|---|---|---|'
    ]
    const meaning = {
      'conflict': '三方互异 — 需三方合并',
      'added-both': '基线无、双方各自新增且不同 — 需三方合并',
      'ff-safe': '本地未动、上游更新 — 可直接采纳',
      'new-upstream': '上游新增 — 可直接采纳',
      'gone-upstream': '上游已删、本地仍在 — 逐案裁定',
      'gone-local': '上游有、本地无 — 检查是否本地有意删除',
      'gone-both': '双方同删 — 无需动作',
      'local-mod': '本地改动、上游未动 — 保留本地',
      'converged': '双方已一致 — 无需动作',
      'local-only': '仅本地 — 保留',
      'in-sync': '完全一致',
      'excluded': '排除规则命中（D-36）',
      'unknown': '未覆盖组合（应随测试夹具补齐分类）'
    }
    for (const c of order) lines.push(`| ${c} | ${counts[c] || 0} | ${meaning[c]} |`)
    lines.push('')
    for (const c of order) {
      const fs = Object.entries(files).filter(([, v]) => v.category === c)
      if (!fs.length) continue
      lines.push(`## ${c} (${fs.length})`, '')
      for (const [up2, v] of fs.sort()) lines.push(`- \`${up2}\` → \`${v.local}\``)
      lines.push('')
    }
    mkdirSync(dirname(mdPath), { recursive: true })
    writeFileSync(mdPath, lines.join('\n'))
    console.log(`report: ${mdPath}`)
  }
  console.log(`base ${base.slice(0, 9)} → target ${target.slice(0, 9)} | upstream clean: ${statusClean}`)
  console.log(Object.entries(counts).map(([k, v]) => `${k}:${v}`).join(' '))
  console.log(`state: ${jsonOut}`)
}

main()
