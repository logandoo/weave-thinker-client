/* macOS dock 重开窗口 —— 桌面壳生命周期不变量（2026-11-05 用户缺陷）。
 *
 * 症状：关闭客户端窗口后 app 仍在 dock（macOS window-all-closed 不 quit），
 * 再次点击 dock 图标无法呼出窗口——因为主进程没有 app.on('activate')，且
 * 旧 mainWindow 引用已 destroyed，second-instance 的 focus 分支也失效。
 *
 * 契约：
 *   D1 app.on('activate') 存在
 *   D2 activate → focusOrCreateWindow
 *   D3 second-instance → focusOrCreateWindow（窗口已关也能重建）
 *   D4 win.on('closed') 清 mainWindow 引用
 *   D5 窗口存在但 hidden/minimized → show/restore + focus
 *   D6 窗口 destroyed → createWindow 重建
 *   D7 --activate-probe 测试接缝存在（runtime 证据见 verification_log）
 *
 * Run: node tests/workflows/test_dock_activate_reopen.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const REPO = path.resolve(__dirname, '..', '..', '..');
const main = fs.readFileSync(path.resolve(REPO, 'desktop', 'main.cjs'), 'utf8');

check('D1 app.on(activate) 存在', /app\.on\('activate'/.test(main));
check('D2 activate 调用 focusOrCreateWindow',
  /app\.on\('activate'[\s\S]{0,200}focusOrCreateWindow\(\)/.test(main));
check('D3 second-instance 调用 focusOrCreateWindow',
  /app\.on\('second-instance'[\s\S]{0,300}focusOrCreateWindow\(\)/.test(main));
check('D4 closed 清 mainWindow 引用',
  /win\.on\('closed'[\s\S]{0,200}mainWindow\s*=\s*null/.test(main));
check('D5 hidden/minimized 窗口 show/restore + focus',
  /isMinimized\(\)[\s\S]{0,120}restore\(\)[\s\S]{0,240}isVisible\(\)[\s\S]{0,120}show\(\)[\s\S]{0,120}focus\(\)/.test(main));
check('D6 destroyed 窗口走 createWindow 重建',
  /mainWindow\s*&&\s*!mainWindow\.isDestroyed\(\)[\s\S]{0,600}createWindow\(\)/.test(main));
check('D7 activate 探针接缝存在',
  main.includes('isActivateProbe') && main.includes('runActivateProbe'));
check('D8 初始创建纳入 windowCreating 闸',
  /windowCreating\s*=\s*true[\s\S]{0,300}await createWindow\(\)/.test(main));

console.log(`RESULT: ${failed ? 'fail' : 'all pass'} (${passed} pass / ${failed} fail)`);
process.exit(failed ? 1 : 0);
