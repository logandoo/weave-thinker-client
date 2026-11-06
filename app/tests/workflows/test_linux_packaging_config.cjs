// Linux .deb 打包链配置守卫（LINUX_DEB_UPSTREAM_FIXES 缺陷 3/7 + 打包链，2026-10-14 W23）。
// 判别性断言：desktopName/syncDesktopName 同源契约、pginstall 整树收集、工具钉版、
// 打包脚本接线——全部为「缺了必炸」的字面锚点，不做宽松包含式检查。
const fs = require('fs');
const path = require('path');

const S = (p) => fs.readFileSync(path.resolve(__dirname, '../../', p), 'utf8');
let failed = 0;
function check(name, ok) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name);
  if (!ok) failed++;
}

// 1) 根 package.json desktopName（Electron app_id/WM_CLASS 派生源，缺陷 7）
const pkg = JSON.parse(S('../package.json'));
check('L1 desktopName == WeaveThinker', pkg.desktopName === 'WeaveThinker');

// 2) electron-builder-linux.yml：syncDesktopName + deb 目标（缺陷 7）
const yml = S('../electron-builder-linux.yml');
check('L2 syncDesktopName: true', /syncDesktopName:\s*true/.test(yml));
check('L3 linux.category Utility', /category:\s*Utility/.test(yml));
check('L4 icon build/icon.png', /icon:\s*build\/icon\.png/.test(yml));
check('L5 target deb', /target:\s*deb/.test(yml));
check('L6 sidecar extraResources', /dist_sidecar\/weave-server/.test(yml));

// 3) script/linux/package_linux.sh：流程接线 + 桌面身份断言
const pl = S('../script/linux/package_linux.sh');
check('L7 package_linux 引用 electron-builder-linux.yml', pl.includes('electron-builder-linux.yml'));
check('L8 package_linux 引用 build_sidecar.sh', pl.includes('script/linux/build_sidecar.sh'));
check('L9 package_linux 含 BUILD_ID 段', pl.includes('BUILD_ID'));
check('L10 package_linux 含 desktopName 断言', pl.includes('desktopName'));

// 4) build_sidecar.sh：pginstall 整树 + 工具钉版（缺陷 3）
const bs = S('../script/linux/build_sidecar.sh');
check('L11 pginstall 整树 --add-data', bs.includes('--add-data "$EP_DIR/pginstall${SEP}embedded_postgres/pginstall"'));
check('L12 pginstall 缺树 fail-fast', bs.includes('pginstall not found'));
check('L13 pyinstaller 钉版', /pyinstaller==\d/.test(bs));
const pipPyiLines = bs.split('\n').filter((l) => l.includes('pip install') && l.includes('pyinstaller'));
check('L14 pip install pyinstaller 全部钉版', pipPyiLines.length > 0 && pipPyiLines.every((l) => l.includes('pyinstaller==')));

// 5) self-wiring：test:unit 链包含本守卫
const appPkg = S('package.json');
check('L15 test:unit 含 test_linux_packaging_config', appPkg.includes('test_linux_packaging_config.cjs'));

console.log(failed ? `RESULT: ${failed} failed` : 'RESULT: all pass');
process.exit(failed ? 1 : 0);
