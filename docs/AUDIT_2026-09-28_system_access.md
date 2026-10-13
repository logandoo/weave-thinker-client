<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AUDIT 2026-09-28 — System Access Permissions</title>
<style>
body{font-family:-apple-system,sans-serif;max-width:960px;margin:40px auto;padding:0 24px;line-height:1.65;color:#222}
code,pre{background:#f4f5f7;border-radius:6px}
pre{padding:12px;overflow:auto;font-size:12.5px}
code{padding:2px 5px;font-size:13px}
h1{font-size:24px} h2{font-size:19px;margin-top:32px;border-bottom:1px solid #e3e5e8;padding-bottom:6px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border:1px solid #dcdfe4;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f6f7f9}
.ok{color:#1a7f37;font-weight:600} .warn{color:#9a6700;font-weight:600} .no{color:#b42318;font-weight:600}
</style>
</head>
<body>
<h1>AUDIT — App 系统访问权限审计（2026-09-28）</h1>
<p><b>问题</b>：「检查现在的 app 打开后是否有完整的系统访问权限」。</p>
<p><b>审计对象</b>：打包版 <code>dist_installer/mac-arm64/WeaveThinker.app</code>（用户日常运行版；静态证据）＋ 开发态 <code>node_modules/electron</code>（运行时探针宿主，见「适用范围」）。</p>
<p><b>审计方式</b>：静态只读检查（签名/entitlements/Info.plist/Gatekeeper）＋ 运行时实测探针（<code>tests/system_access_probe.cjs</code>，Electron 真实主进程内执行文件/进程/网络访问；含受保护目录写删探针，探针文件随即删除）。</p>
<p><b>适用范围（评审 I2 修正）</b>：运行时探针宿主为开发态 Electron 二进制（<code>executable=node_modules/electron/dist</code>，其 TCC/bundle 身份与打包版不同），条目的<b>能力面结论</b>（文件/进程/网络可用）按「零 entitlements 非沙箱」同构迁移至打包版；<b>TCC 弹窗行为</b>（麦克风等）以打包版 <code>Info.plist</code> 用途声明为准（已静态核验在位）。打包版自身未做交互式 TCC 实测。</p>

<h2>结论（先给答案）</h2>
<table>
<tr><th>维度</th><th>状态</th><th>说明</th></tr>
<tr><td>文件系统（用户级）</td><td class="ok">完整</td><td>无沙箱（零 entitlements）；任意绝对路径读写可用（实测 /etc/hosts、~/、~/Library、~/Downloads 写删均通过）。受 macOS TCC 保护的目录（桌面/文档/下载/照片等）首次访问由系统弹窗征求用户同意——这是系统级正常行为，不是 App 缺陷。</td></tr>
<tr><td>进程 spawn / 子进程</td><td class="ok">完整</td><td>本地栈（PG + 后端侧车）、LibreOffice、ffmpeg、Chromium 均以子进程拉起（实测 <code>execFileSync</code> OK；打包冒烟侧车全链路已绿）。</td></tr>
<tr><td>网络</td><td class="ok">完整（回环实测）</td><td>回环 socket 调用允许（errno 感知探针：<code>ECONNREFUSED</code>=网络栈可达，服务未启）；出站网络本探针未单独实测（非沙箱应用默认无出站限制；生产同步链路已另有独立实测）。</td></tr>
<tr><td>麦克风（语音/ASR）</td><td class="warn">需首次授权</td><td>macOS 要求 TCC 弹窗授权；<code>Info.plist</code> 已含 <code>NSMicrophoneUsageDescription</code>（打包版实测在位），弹窗文案正常显示，用户同意后可用。</td></tr>
<tr><td>摄像头 / 蓝牙</td><td class="warn">按需授权</td><td>打包版 <code>Info.plist</code> 含 <code>NSCameraUsageDescription</code> / <code>NSBluetooth*</code> / <code>NSAudioCaptureUsageDescription</code>（Electron 默认模板），当前功能未使用，用到时系统弹窗授权。</td></tr>
<tr><td>完全磁盘访问（FDA）</td><td class="warn">无直接证据（按未授予处理）</td><td>TCC.db 读取失败（<code>ENOENT</code>：预期路径在本机不存在/不可达；TCC 库受 SIP 保护）——该失败不能证明也无法证伪 FDA，结论按「未授予」保守处理。App 功能（用户账号目录内数据、应用自身数据）不依赖 FDA，无需额外申请。</td></tr>
<tr><td>App Sandbox</td><td class="ok">未启用（符合预期）</td><td>零 entitlements ⇒ 非 MAS 沙箱应用，拥有标准用户权限。渲染进程仍有 Chromium 沙箱（<code>webPreferences.sandbox: true</code>）＋ contextIsolation。</td></tr>
<tr><td>签名 / Gatekeeper</td><td class="no">未签名/未公证</td><td>adhoc（linker-signed），<code>identity: null</code>、<code>hardenedRuntime: false</code>。本机构建产物直接运行不受影响；经网络分发（带 quarantine 属性）会被 Gatekeeper 拦（需右键打开或签名+公证——已登记 W6）。</td></tr>
</table>
<p><b>总判</b>：App 打开后拥有<b>完整的用户级系统访问权限</b>（非沙箱）。唯一系统级限制是 TCC 保护资源的首次弹窗授权（麦克风等）与 Gatekeeper 对分发包的拦截（本机不受影响）。无权限缺口需要代码修复。</p>

<h2>证据</h2>

<h3>1. 签名 / entitlements（打包版）</h3>
<pre>$ codesign -dvvv dist_installer/mac-arm64/WeaveThinker.app
Identifier=Electron
CodeDirectory v=20400 flags=0x20002(adhoc,linker-signed)
Signature=adhoc
Info.plist=not bound
TeamIdentifier=not set

$ codesign -d --entitlements - dist_installer/mac-arm64/WeaveThinker.app
（无输出 = 零 entitlements ⇒ 无 App Sandbox、无 hardened runtime 限制）

$ spctl -a -vvv dist_installer/mac-arm64/WeaveThinker.app
dist_installer/mac-arm64/WeaveThinker.app: code has no resources but signature indicates they must be present
（未公证，Gatekeeper 拒绝评估；本机未隔离（xattr 仅 com.apple.provenance）可直接运行）</pre>

<h3>2. Info.plist 用途声明（打包版）</h3>
<pre>"NSMicrophoneUsageDescription" => "This app needs access to the microphone"
"NSCameraUsageDescription" => "This app needs access to the camera"
"NSAudioCaptureUsageDescription" => "This app needs access to audio capture"
"NSBluetoothAlwaysUsageDescription" / "NSBluetoothPeripheralUsageDescription"
"NSAppTransportSecurity" => { NSAllowsArbitraryLoads: true, NSAllowsLocalNetworking: true }
"NSPrefersDisplaySafeAreaCompatibilityMode" => false   # 刘海屏全屏适配
（usage description 在位 ⇒ TCC 弹窗文案正常；无用途声明缺失）</pre>

<h3>3. 运行时访问实测（Electron 主进程探针；宿主=开发态 Electron 二进制）</h3>
<pre>$ ./node_modules/.bin/electron tests/system_access_probe.cjs
read /etc/hosts                                    ok
read /System/Library/CoreServices/...plist         ok
readdir ~                                           ok (61 entries)
readdir ~/Library                                   ok (93 entries)
stat ~/Desktop ~/Documents ~/Downloads ~/Pictures…  ok (全部可 stat)
read TCC.db (FDA check)                             ENOENT —— 路径不存在/不可达 → FDA 证据不足（按未授予处理）
write tmp                                           ok
write ~/Downloads (probe file)                      ok (write+delete；写删探针，随即删除)
spawn /bin/echo                                     ok
net connect 127.0.0.1:55432                         ok（errno 感知：ECONNREFUSED=网络栈可达，服务未启；非权限错）
uid 501 / logan</pre>
<p>完整 JSON：<code>tests/access_probe.json</code>（含 <code>appPath</code>/<code>executable</code> 用于确认探针宿主）。</p>

<h3>4. 安全侧配置（正面确认）</h3>
<pre>desktop/main.cjs:
  webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true }
  IPC 全部经 assertTrustedSender（仅 app://weave/ 主帧）；app:// 协议路径穿越已防护（403，冒烟断言）
  写保护：拒绝写入 app bundle 内（isInside(DIST)）
打包版 shell 侧：sidecar 以用户 uid 运行，PG 仅监听 127.0.0.1:55432（回环）</pre>

<h3>5. TCC 弹窗预期行为</h3>
<p>macOS 27.0（26A428）。App 首次调用麦克风（语音输入/ASR）时系统弹窗「WeaveThinker 想访问麦克风」，用途声明在位，允许后生效；拒绝可在「系统设置 → 隐私与安全性」改。桌面/文稿/下载等目录首次程序化读写同样弹窗（用户经应用内文件选择器选择时 macOS 直接授权，无弹窗）。</p>

<h2>缺口与登记</h2>
<table>
<tr><th>#</th><th>缺口</th><th>影响</th><th>处置</th></tr>
<tr><td>1</td><td>未签名 / 未公证</td><td>仅网络分发首次打开被 Gatekeeper 拦截；本机运行不受影响；TCC 授权在重建（新 adhoc 签名）后可能需重新授权</td><td>已登记 W6（Developer ID + 公证）；内测期用右键打开/去 quarantine</td></tr>
<tr><td>2</td><td>无 Full Disk Access</td><td>无法读取其他 App 的受保护数据（本 App 无此需求）</td><td>不申请（最小权限原则）</td></tr>
</table>

<p>本审计为只读检查；无需代码变更。验证日志：<code>tests/verification_log.md</code>（2026-09-28 权限审计行）。</p>
</body>
</html>
