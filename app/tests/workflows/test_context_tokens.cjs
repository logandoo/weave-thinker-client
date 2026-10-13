/* context token 徽章纯函数 + 接线测试（Node 直跑，type stripping）。
 * P2 (2026-09-05)：context_info 持久化跨设备播种 + 移动端徽章。
 * Run: node tests/workflows/test_context_tokens.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/composables/useContextTokens.ts');
  const { parseContextInfo, pickLatestContextInfo, formatContextTokens, contextTokenTooltipText } = mod;

  // ── part A: parseContextInfo ──────────────────────────────────────────────
  check('A1 parses valid JSON', parseContextInfo('{"tokens":130,"context_length":262144,"measured":true}')?.tokens === 130);
  check('A2 null/garbage → null', parseContextInfo(null) === null && parseContextInfo('not-json') === null);
  check('A3 missing fields → null', parseContextInfo('{"tokens":1}') === null && parseContextInfo('{"context_length":1}') === null);
  check('A4 non-string → null', parseContextInfo(42) === null && parseContextInfo({}) === null);

  // ── part B: pickLatestContextInfo（最后一条 assistant 优先） ───────────────
  const msgs = [
    { id: '1', role: 'user', content: 'q1' },
    { id: '2', role: 'assistant', content: 'a1', context_info: '{"tokens":100,"context_length":262144}' },
    { id: '3', role: 'user', content: 'q2' },
    { id: '4', role: 'assistant', content: 'a2', context_info: '{"tokens":20552,"context_length":262144,"measured":true}' },
  ];
  check('B1 picks LAST assistant snapshot', pickLatestContextInfo(msgs)?.tokens === 20552);
  check('B2 no snapshot → null', pickLatestContextInfo(msgs.slice(0, 3)) === null || pickLatestContextInfo([msgs[0], msgs[2]]) === null);
  check('B3 skips unparsable, falls to earlier', pickLatestContextInfo([
    { id: '1', role: 'assistant', content: 'a', context_info: '{"tokens":77,"context_length":262144}' },
    { id: '2', role: 'assistant', content: 'b', context_info: 'broken' },
  ])?.tokens === 77);
  check('B4 empty list → null', pickLatestContextInfo([]) === null);

  // ── part C: 展示助手 ──────────────────────────────────────────────────────
  check('C1 format 千分位', formatContextTokens(20552) === '20,552');
  check('C2 tooltip 实测', contextTokenTooltipText({ tokens: 100, context_length: 1000, measured: true }).includes('实测'));
  check('C3 tooltip 估算', contextTokenTooltipText({ tokens: 100, context_length: 1000 }).includes('估算'));
  check('C4 tooltip 空', contextTokenTooltipText(null) === '' && contextTokenTooltipText({ tokens: 1, context_length: 0 }) === '');

  // ── part D: 接线断言 ──────────────────────────────────────────────────────
  const layout = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'ChatLayout.vue'), 'utf-8');
  check('D1 移动端徽章挂载 mobile-header', layout.includes('context-token-chip-mobile') && layout.includes('showMobileContextChip'));
  const area = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'ChatArea.vue'), 'utf-8');
  check('D2 ChatArea 用共享 formatter（无本地重复定义）', !area.includes('function formatContextTokens'));
  const store = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'chat.ts'), 'utf-8');
  check('D3 store 播种两处接线', (store.match(/seedContextInfoFromMessages\(/g) || []).length >= 3); // 定义+2 调用
  const types = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'types', 'index.ts'), 'utf-8');
  check('D4 Message 类型带 context_info', /context_info\?:\s*string \| null/.test(types));

  if (failed) {
    console.error(`\n${failed} check(s) FAILED`);
    process.exit(1);
  }
  console.log('\nall checks pass');
}

main().catch(e => { console.error(e); process.exit(1); });
