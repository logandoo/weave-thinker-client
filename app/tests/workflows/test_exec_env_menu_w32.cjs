/* W32 执行环境云端联动 — 显示/菜单/免配对码接线不变量（判据 #94/#95/#100）。
 *
 * 契约（Consistency Hub 映射）：
 *  1. execTargetLabel：device:<self> → 「本地」；null → 「云端」（云端链接态）；
 *     device:<other> → 注册表名（回落「客户端」）；
 *  2. 菜单三类选项：「本地」绑定 device:<self>、「云端」绑定 null、设备行绑定 device:<id>；
 *  3. 「生成配对码」区块仅 EXECUTION_MODE === 'cloud'（上游网页端）可见——客户端源码
 *     分支必须带门控（判据 #95）；
 *  4. 菜单设备源=云端注册表（getCloudDevices / client-exec/cloud-devices），非本地空注册表；
 *  5. chat.ts：非本地目标 turn 后有同步等待（postSyncNow/waitSyncComplete）再刷新——
 *     消息行经同步落本地可见（判据 #96）。
 *
 * Run: node --experimental-strip-types tests/workflows/test_exec_env_menu_w32.cjs */
const fs = require('fs')
const path = require('path')

let failed = 0
let passed = 0
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`)
  if (!cond) failed += 1
  else passed += 1
}

const SRC = path.resolve(__dirname, '..', '..', 'src', 'components', 'ChatInput.vue')
const src = fs.readFileSync(SRC, 'utf8')
const CHAT = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'stores', 'chat.ts'), 'utf8')
const EXEC_API = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'api', 'clientExec.ts'), 'utf8')

// 1. 显示映射：自设备 → 「本地」
check('M1 self-device label maps to 本地', /is_self|selfDevice/.test(src) && /'本地'|"本地"|「本地」/.test(src))
check('M2 null target label has 云端 branch', /云端/.test(src))
check('M3 label fallback 客户端 retained', src.includes("'客户端'"))

// 2. 菜单三类选项绑定值
check('M4 本地 menu item binds device:self target', /device:\$\{/.test(src) || /`device:\$\{selfDeviceId[^`]*`/.test(src))
check('M5 云端 menu item binds null target', /selectExecTarget\(null\)/.test(src))
check('M6 device rows bind device:<id>', /selectExecTarget\(`device:\$\{dev\.device_id\}`\)/.test(src))

// 3. 配对码门控（客户端不生成配对码——仅 cloud）
check('M7 pairing section gated to cloud-only',
  /EXECUTION_MODE\s*===\s*'cloud'/.test(src) && /exec-pair-section/.test(src))

// 4. 菜单设备源=云端注册表
check('M8 menu devices come from cloud registry',
  /getCloudDevices|cloud-devices/.test(src) && /getCloudDevices|cloud-devices/.test(EXEC_API))

// 5. 中继后同步等待（消息行经同步落本地）
check('M9 post-turn sync wait for relayed targets',
  /postSyncNow|waitSyncComplete/.test(CHAT) && /settleRelayedTurn|needsPostTurnSync|isRelayedTarget|relayTarget/.test(CHAT))

// 6. W26 空目标文案被新语义取代（判据 #100 的判别性：旧实现必须不再满足）
check('M10 legacy W26 sole-null-label superseded',
  !/EXECUTION_MODE === 'local' \? '本地（本机）' : '云端服务器'/.test(src))

// 7. 文件访问面（判据 #99）：device:<self> 文件引用不走设备代理（本地 workspace）
const BUBBLE = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'components', 'MessageBubble.vue'), 'utf8')
check('M11 self-target file refs use local workspace (not device proxy)',
  /isSelfDeviceTarget\(target\)/.test(BUBBLE) && /primeSelfDeviceId/.test(BUBBLE))

console.log(failed === 0 ? 'RESULT: all pass' : `RESULT: ${failed} FAILED`)
process.exit(failed === 0 ? 0 : 1)
