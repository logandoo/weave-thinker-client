/* npm workspaces 下的 patch-package 垫片（2026-11-05 W36）。
 *
 * 上游 frontend 是 standalone：postinstall=patch-package，patches/ 与
 * node_modules 同层。本仓是 npm workspaces（app + desktop），vue-router 被提升到
 * 仓根 node_modules；直接在 app 内跑 patch-package 会报
 * “package vue-router which is not present at node_modules/vue-router”。
 *
 * 本垫片按实际布局选择 cwd，并在“已打补丁”时跳过（patch-package 对已应用的
 * 补丁会报错；prebuild 另有 fail-closed 检查）。
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const APP = path.resolve(__dirname, '..');
const REPO = path.resolve(APP, '..');
const PATCH_DIR = path.join(APP, 'patches');
const candidates = [
  { pkg: path.join(APP, 'node_modules', 'vue-router'), cwd: APP, patchDir: PATCH_DIR },
  { pkg: path.join(REPO, 'node_modules', 'vue-router'), cwd: REPO, patchDir: PATCH_DIR },
];
const hit = candidates.find((c) => fs.existsSync(path.join(c.pkg, 'package.json')));
if (!hit) {
  console.log('[postinstall] vue-router not installed; skip patch-package');
  process.exit(0);
}
const runtime = path.join(hit.pkg, 'dist', 'vue-router.mjs');
if (fs.existsSync(runtime) && !fs.readFileSync(runtime, 'utf8').includes('visibilitychange')) {
  console.log('[postinstall] vue-router backport already applied; skip');
  process.exit(0);
}
const bin = path.join(hit.cwd, 'node_modules', '.bin', 'patch-package');
if (!fs.existsSync(bin)) {
  console.error('[postinstall] patch-package binary not found at ' + bin);
  process.exit(1);
}
execFileSync(bin, ['--patch-dir', hit.patchDir], { cwd: hit.cwd, stdio: 'inherit' });
console.log('[postinstall] vue-router backport applied');
