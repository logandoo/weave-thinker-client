# 移动端（Capacitor）— 脚手架与命令

共享 SPA（`app/`）经 Capacitor 打为 iOS/Android 原生壳。**资产本地打包**（`webDir: dist`，无 `server.url` 远程加载）——符合 Apple 2.5.2 自包含要求；功能更新走商店二进制，OTA 默认关闭（评估报告 §2.1）。

## 前置条件

| 目标 | 需要 | 本机状态（2026-09-23） |
|---|---|---|
| Android | JDK 17+ · Android Studio/SDK | SDK 在 `~/Library/Android/sdk`；**缺 Java Runtime** |
| iOS | 完整 Xcode（非仅 CommandLineTools）· 开发者账号 | xcodebuild 仅 CLT → **需安装完整 Xcode** |

## 平台生成（环境就绪后）

```bash
npm --workspace app exec -- cap add android     # → app/android/
npm --workspace app exec -- cap add ios         # → app/ios/
npm --workspace app exec -- cap sync            # 把 app/dist 同步进两个平台
```

构建：

```bash
# Android
cd app/android && ./gradlew assembleDebug        # 产物 app/build/outputs/apk/debug/*.apk
# iOS（模拟器）
cd app/ios/App && xcodebuild -workspace App.xcworkspace -scheme App -configuration Debug -sdk iphonesimulator
```

## API 地址

与桌面同源：`config.toml [server].api_base` → `project_build.sh` → `VITE_API_BASE`。跨源请求需后端 CORS 放行 `capacitor://localhost`（iOS）与 `http://localhost` / `https://localhost`（Android `androidScheme: https`）。

## 插件路线（W4 能力翼，按评估报告 §2.1.1）

1. `@capacitor/push-notifications`（APNs/FCM；4.2 最强单信号）
2. `@capacitor/preferences`（token 镜像，防 iOS 快速终止 localStorage 蒸发）
3. `@capacitor/share` · `@capacitor/filesystem` · `@capacitor/camera`（文件/拍摄）
4. Sign in with Apple（若提供三方登录，Apple 4.8 强制）
5. 离线缓存（Cache API/Service Worker 或插件）

已安装待接线：`@capacitor/core`、`@capacitor/app`、`@capacitor/preferences`。

## 合规清单（送审前）

- 无远程 URL 加载、无功能级热更新（当前即满足）
- Info.plist usage strings（麦克风/相机/相册）与 Privacy Manifest（W4）
- 若拒审 4.2：按评估报告 §2.1.1——申诉模板 + 加厚原生面（W5 原生导航壳），勿空转
- 外链一律应用内浏览器/原生分享，严禁跳 Safari（实录拒因）
