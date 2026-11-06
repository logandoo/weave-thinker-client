// 设备 URL 跨源 apiWrap 守卫（W23 评审 C1 收口：apiUrl 包裹成对 + 嗅探容错绝对形式）。
// 背景：桌面壳 app:// 原生对 /api/* 直接 404——设备分支漏 apiUrl 包裹=预览 404；
// FilePreviewDialog 以 startsWith('/api/devices/') 嗅探只认相对形式——被包裹的绝对
// URL 落空 deviceId=null → 云端回落（错误环境读）。两处均字节 diff 不可见。
const fs = require('fs');
const path = require('path');

const S = (p) => fs.readFileSync(path.resolve(__dirname, '../../', p), 'utf8');
let failed = 0;
function check(name, ok) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name);
  if (!ok) failed++;
}

// 1) MessageBubble：apiUrl 必须 import 且 fileRefUrl 设备分支以 apiUrl(buildDeviceDownloadUrl( 包裹
const bubble = S('src/components/MessageBubble.vue');
check('P1 MessageBubble import apiUrl from @/api/base',
  /import\s*\{[^}]*\bapiUrl\b[^}]*\}\s*from\s*'@\/api\/base'/.test(bubble));
check('P2 MessageBubble 设备分支 apiUrl(buildDeviceDownloadUrl(',
  bubble.includes('apiUrl(buildDeviceDownloadUrl('));

// 2) FilePreviewDialog：deviceId 派生必须容错绝对（apiUrl 包裹后）形式
const fpd = S('src/components/FilePreviewDialog.vue');
check('P3 FPD 无裸 startsWith(/api/devices/) 嗅探门', !fpd.includes("startsWith('/api/devices/')"));
check('P4 FPD deviceId 派生容忍绝对形式',
  fpd.includes('\\/api\\/devices\\/([^/]+)'));


// 3) useMarkdown：signedFileUrl 设备分支包裹（既有 hunk 防回退）
const um = S('src/composables/useMarkdown.ts');
check('P5 useMarkdown signedFileUrl 设备分支 apiUrl 包裹', um.includes('apiUrl(buildDeviceDownloadUrl('));

// 4) self-wiring
const appPkg = S('package.json');
check('P6 test:unit 含 test_device_url_apiwrap', appPkg.includes('test_device_url_apiwrap.cjs'));

console.log(failed ? `RESULT: ${failed} failed` : 'RESULT: all pass');
process.exit(failed ? 1 : 0);
