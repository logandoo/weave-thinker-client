/**
 * W18 G3/G4: device-execution URL derivation (target-aware builders).
 *
 * Regression contract: with a device context, workspace-relative paths route to
 * the device fs proxy; absolute paths / drive-letter paths / no-device context
 * keep the cloud URL; execution_target parsing extracts device ids only from
 * `device:` targets.
 *
 * Run: node --experimental-strip-types tests/workflows/test_device_exec_urls.cjs
 */
const {
  deviceIdFromTarget,
  isRoutableToDevice,
  buildDeviceDownloadUrl,
  resolveDownloadUrl,
  normalizeDeviceEntries,
} = require('../../src/utils/deviceUrls.ts')

let passed = 0
let failed = 0
function check(name, cond, detail = '') {
  if (cond) {
    passed += 1
    console.log(`PASS ${name}`)
  } else {
    failed += 1
    console.log(`FAIL ${name} ${detail}`)
  }
}

// deviceIdFromTarget
check('device target parsed', deviceIdFromTarget('device:abc123') === 'abc123')
check('cloud/null → null', deviceIdFromTarget(null) === null && deviceIdFromTarget(undefined) === null)
check('non-device prefix → null', deviceIdFromTarget('cloud:x') === null)

// isRoutableToDevice
check('relative routable', isRoutableToDevice('dir/a.txt') === true)
check('absolute not routable', isRoutableToDevice('/etc/passwd') === false)
check('drive not routable', isRoutableToDevice('C:/Windows/x') === false)
check('empty not routable', isRoutableToDevice('') === false)

// resolveDownloadUrl — no device context: cloud URL unchanged
const cloud = resolveDownloadUrl('dir/a.txt', null, 'test-token')
check('cloud download url', cloud.startsWith('/api/files/download?path=dir%2Fa.txt'), cloud)
check('cloud url carries token', cloud.includes('token=test-token'), cloud)

// device context → proxy URL for relative paths
const dev = resolveDownloadUrl('dir/a.txt', 'dev1', 'test-token')
check('device download url', dev.startsWith('/api/devices/dev1/fs/download?path=dir%2Fa.txt'), dev)
check('device url carries token', dev.includes('token=test-token'), dev)

// absolute / drive paths in DEVICE ctx → 仍走设备代理（显式 400 降级，
// 绝不回退云端读服务器工作区——A4.9 M4 契约）
const abs = resolveDownloadUrl('/etc/passwd', 'dev1', 'test-token')
check('absolute in device ctx stays device', abs.startsWith('/api/devices/dev1/fs/download?path='), abs)
const win = resolveDownloadUrl('C:/Windows/x', 'dev1', 'test-token')
check('drive in device ctx stays device', win.startsWith('/api/devices/dev1/fs/download?path='), win)
const absCloud = resolveDownloadUrl('/etc/passwd', null, 'test-token')
check('absolute in cloud ctx stays cloud', absCloud.startsWith('/api/files/download?path=%2Fetc%2Fpasswd'), absCloud)

// token optional
const noToken = resolveDownloadUrl('a.txt', 'dev1', null)
check('no token → no param', !noToken.includes('token='), noToken)

// buildDeviceDownloadUrl shape
const dd = buildDeviceDownloadUrl('dev9', 'x/y.txt', 't')
check('device builder shape', dd.startsWith('/api/devices/dev9/fs/download?path=x%2Fy.txt'), dd)

// normalizeDeviceEntries（A4.9 I-1：目录枚举宽容化）
const norm = normalizeDeviceEntries(
  [
    { name: 'sub', type: 'dir', size: 0 },
    { name: 'fold', type: 'folder' },
    { name: 'f.txt', type: 'file', size: 5 },
    { name: 'g', is_dir: true },
    { name: 'h.txt', mtime: 123, rel_path: 'custom/h.txt' },
  ],
  'root',
)
check('dir enum recognized', norm[0].is_dir === true && norm[1].is_dir === true, JSON.stringify(norm[1]))
check('file enum recognized', norm[2].is_dir === false && norm[2].size === 5)
check('is_dir bool honored', norm[3].is_dir === true)
check('rel_path passthrough', norm[4].rel_path === 'custom/h.txt', norm[4].rel_path)
check('rel_path derived', norm[0].rel_path === 'root/sub', norm[0].rel_path)
check('empty entries', normalizeDeviceEntries(undefined, 'p').length === 0)

console.log(`\n=== ${passed} pass / ${failed} fail ===`)
process.exit(failed ? 1 : 0)
