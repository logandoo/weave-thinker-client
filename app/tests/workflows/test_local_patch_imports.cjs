// 登记表本地补丁的 import/callsite 成对守卫（2026-10-07 W6 评审双 Critical 根治）。
// 背景：--theirs 合并三次吞掉 import 行（apiUrl×2/workspaceRelPath×2/sync_workspace 别名），
// 调用点幸存而符号未定义——vite build 不类型检查、冒烟只到登录页，故必须机器守卫。
// 覆盖：DECISION_MATRIX §3 中前端侧「import+调用成对」条目（14 项含双 import 断言）；
// 服务端别名（sync_workspace）由 server/tests/test_main_sync_wiring.py 覆盖。
const fs = require('fs');
const path = require('path');

const S = (p) => fs.readFileSync(path.resolve(__dirname, '../../', p), 'utf8');
let failed = 0;
function check(name, ok) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name);
  if (!ok) failed++;
}

// 1) MessageBubble：workspaceRelPath 必须 import 且被调用
const bubble = S('src/components/MessageBubble.vue');
check('P1 MessageBubble import workspaceRelPath',
  /import\s*\{[^}]*\bworkspaceRelPath\b[^}]*\}\s*from\s*'@\/composables\/filePreview'/.test(bubble));
check('P1b MessageBubble callsite workspaceRelPath(', bubble.includes('workspaceRelPath('));

// 2) api/chat.ts：API_ORIGIN 必须 import 且在 streamChat 与 resumeStream 双处使用
const apiChat = S('src/api/chat.ts');
check('P2 api/chat import API_ORIGIN from ./base',
  /import\s*\{[^}]*\bAPI_ORIGIN\b[^}]*\}\s*from\s*'\.\/base'/.test(apiChat));
const originUses = (apiChat.match(/const baseUrl = API_ORIGIN/g) || []).length;
check('P2b streamChat+resumeStream 均用 API_ORIGIN（≥2）', originUses >= 2, );
check('P2c 无残留烘焙 base 于 resume/stream 路径',
  !/const baseUrl = \(import\.meta as any\)\.env\?\.VITE_API_BASE/.test(apiChat));

// 3) stores/chat.ts：apiUrl 必须 import 且被调用（跨源 fetch 包装）
const storeChat = S('src/stores/chat.ts');
check('P3 stores/chat import apiUrl', /import\s*\{[^}]*\bapiUrl\b[^}]*\}\s*from\s*'@\/api\/base'/.test(storeChat));
check('P3b stores/chat callsite apiUrl(', storeChat.includes('apiUrl('));
check('P3c stores/chat 载荷双保险在行',
  /isExternalizedStub\(\s*payload\.content\s*\)[\s\S]{0,60}return\s+false/.test(storeChat));

// 4) useMarkdown.ts：apiUrl import+调用（图片 src 跨源重写）
const um = S('src/composables/useMarkdown.ts');
check('P4 useMarkdown import apiUrl', /import\s*\{[^}]*\bapiUrl\b[^}]*\}\s*from\s*'@\/api\/base'/.test(um));
check('P4b useMarkdown callsite apiUrl(', um.includes('apiUrl(`'));

// 5) useVoiceDuplex：settle 守卫（1013 不挂起）
const voice = S('src/composables/useVoiceDuplex.ts');
check('P5 useVoiceDuplex settleErr 守卫在行', voice.includes('settleErr'));

// 6) package.json：本地测试入口在位
const pkg = S('package.json');
check('P6 test:unit 含 test_asr_reconnect', pkg.includes('test_asr_reconnect.cjs'));
check('P6b test:unit 含 test_payload_stub_guard', pkg.includes('test_payload_stub_guard.cjs'));
check('P6c test:unit 含 test_local_patch_imports', pkg.includes('test_local_patch_imports.cjs'));

console.log(failed ? `RESULT: ${failed} failed` : 'RESULT: all pass');
process.exit(failed ? 1 : 0);
