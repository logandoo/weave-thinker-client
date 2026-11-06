/* W26 云端/本地执行 —— 客户端层语义拆分接线不变量。
 *
 * 症状（用户）：客户端的执行应分为「云端」和「本地」，现状完全拷贝云端
 * 代码、客户端层无区分 —— 本地栈模式下执行环境选择器仍显示「云端服务器」。
 *
 * 方案（用户裁定 A，象限 A 本地层，server/ 契约零改动）：
 *   桌面壳已知本地栈是否实启（runtimeApiBase 仅本地栈成功时设置，
 *   main.cjs:795）→ additionalArguments 注入 --weave-exec-mode=local →
 *   preload 白名单校验后暴露 weaveRuntime.execMode → base.ts 导出
 *   EXECUTION_MODE（runtime 优先；无注入按烘焙 base 推断 loopback=local）
 *   → ChatInput 执行环境空目标标签/描述/title 按模式分流。
 *
 * 判据（结构 + 行为双轨）：
 *   M1 main.cjs 仅在 runtimeApiBase 分支注入 --weave-exec-mode=local
 *   M2 preload 解析 + 白名单（仅 'local'）+ 暴露 execMode
 *   M3 base.ts 导出 EXECUTION_MODE 且 runtime 注入优先
 *   M4 ChatInput 空目标三处文案（菜单项 label/desc、按钮 title、短标签）
 *      按 EXECUTION_MODE 分流，不再无条件「云端服务器」
 *   M5 行为级：execMode='local' 注入 → EXECUTION_MODE==='local'；
 *      无注入 → 'cloud'；非法值被拒 → 'cloud'
 *   M6 self-wiring：test:unit 含本测试
 *
 * Run: node --experimental-strip-types tests/workflows/test_exec_mode_wiring.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const ROOT = path.resolve(__dirname, '..', '..');
const REPO = path.resolve(ROOT, '..');
const S = (p) => fs.readFileSync(path.resolve(ROOT, p), 'utf8');

// ── M1 main.cjs 注入 ─────────────────────────────────────────────────────
const mainCjs = fs.readFileSync(path.resolve(REPO, 'desktop', 'main.cjs'), 'utf8');
const apiBaseArgIdx = mainCjs.indexOf('--weave-api-base=');
const execModeArgIdx = mainCjs.indexOf('--weave-exec-mode=local');
check('M1a main.cjs 注入 --weave-exec-mode=local', execModeArgIdx > 0);
check('M1b exec-mode 与 api-base 同一分支注入（本地栈实启=唯一信号源）',
  apiBaseArgIdx > 0 && execModeArgIdx > apiBaseArgIdx &&
  /additionalArguments:\s*\[[^\]]*--weave-api-base=[^\]]*--weave-exec-mode=local[^\]]*\]/.test(mainCjs));

// ── M2 preload 白名单 ────────────────────────────────────────────────────
const preload = fs.readFileSync(path.resolve(REPO, 'desktop', 'preload.cjs'), 'utf8');
check('M2a preload 解析 --weave-exec-mode', preload.includes("'--weave-exec-mode='"));
check('M2b preload 白名单仅放行 local', /===\s*'local'\s*\?\s*'local'\s*:\s*''/.test(preload));
check('M2c weaveRuntime 暴露 execMode', /exposeInMainWorld\('weaveRuntime',\s*\{[^}]*execMode/.test(preload));

// ── M3 base.ts 导出 ──────────────────────────────────────────────────────
const base = S('src/api/base.ts');
check('M3a base.ts 导出 EXECUTION_MODE', /export const EXECUTION_MODE/.test(base));
check('M3b EXECUTION_MODE 优先采信 runtime 注入', /weaveRuntime\?\.execMode/.test(base));
check('M3c 无注入时按烘焙 base 推断（loopback=local）',
  /LOOPBACK_RE\s*=\s*\/\^https\?/.test(base) &&
  /RUNTIME_MODE_RAW\s*===\s*'local'\s*\|\|\s*LOOPBACK_RE\.test/.test(base));

// ── M4 ChatInput 文案分流 ────────────────────────────────────────────────
const chatInput = S('src/components/ChatInput.vue');
check('M4a ChatInput 引入 EXECUTION_MODE', /import\s*\{[^}]*EXECUTION_MODE[^}]*\}\s*from\s*'@\/api\/base'/.test(chatInput));
// 菜单首项 label 不再无条件写死「云端服务器」——必须经模式分流
// W32 语义取代（判据 #100，用户裁定 A）：null=云端 · device:<self>=本地——
// W26「空目标=本地（本机）」的模式分流被云端联动映射取代；判别性=旧实现必红。
check('M4b 菜单三选一（本地/云端）label 在场', /selfDeviceId \? '本地' : '本地（本机）'/.test(chatInput) && /reasoning-menu-label">云端<\//.test(chatInput));
check('M4c execButtonTitle 空目标=云端语义',
  /execButtonTitle[\s\S]{0,400}云端（云端服务器执行）/.test(chatInput));
check('M4d execTargetLabel 映射=云端/本地（本机）分层 + 自设备=本地',
  /if \(!t\) return cloudLinked\.value \? '云端' : '本地（本机）'/.test(chatInput) &&
  /if \(isSelfTarget\(t\)\) return '本地'/.test(chatInput));

// ── M5 行为级：EXECUTION_MODE 解析 ───────────────────────────────────────
async function behavioral() {
  const basePath = path.resolve(ROOT, 'src', 'api', 'base.ts');
  const cases = [
    { name: "M5a runtime execMode='local' → local", stub: { execMode: 'local', apiBase: 'http://127.0.0.1:8159' }, want: 'local' },
    { name: 'M5b 无注入（web 同源）→ cloud', stub: undefined, want: 'cloud' },
    { name: "M5c 非法注入值被拒 → cloud", stub: { execMode: 'evil;alert(1)', apiBase: '' }, want: 'cloud' },
  ];
  for (let i = 0; i < cases.length; i++) {
    const c = cases[i];
    if (c.stub === undefined) delete globalThis.weaveRuntime;
    else globalThis.weaveRuntime = c.stub;
    try {
      const mod = await import(`file://${basePath}?case=${i}`);
      check(c.name, mod.EXECUTION_MODE === c.want, { got: mod.EXECUTION_MODE, want: c.want });
    } catch (e) {
      check(c.name + '（import 成功）', false, String(e));
    }
  }
  delete globalThis.weaveRuntime;
}

// ── M6 self-wiring ───────────────────────────────────────────────────────
const appPkg = S('package.json');
check('M6 test:unit 含 test_exec_mode_wiring', appPkg.includes('test_exec_mode_wiring.cjs'));

behavioral().finally(() => {
  console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
  if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
  console.log('RESULT: ALL CHECKS PASS');
});
