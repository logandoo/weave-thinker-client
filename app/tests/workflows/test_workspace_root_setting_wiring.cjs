#!/usr/bin/env node
// W12 P2#10 静态接线：设置页「工作区」面板 ↔ /api/users/me/workspace ↔ 桌面桥选目录。
const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..', '..')
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8')
let ok = true
const check = (desc, cond) => {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${desc}`)
  if (!cond) ok = false
}

const api = read('src/api/userSettings.ts')
check('A1 API 含 GET /users/me/workspace', /getWorkspaceRoot[\s\S]{0,120}\/users\/me\/workspace/.test(api))
check('A2 API 含 PUT /users/me/workspace', /setWorkspaceRoot[\s\S]{0,160}\/users\/me\/workspace/.test(api))
check('A3 WorkspaceRootInfo 三字段', /WorkspaceRootInfo[\s\S]{0,400}workspace_root[\s\S]{0,200}default_root[\s\S]{0,200}is_override/.test(api))

const panel = read('src/components/WorkspaceSettingsPanel.vue')
check('B1 面板调用 getWorkspaceRoot', /userSettingsApi\.getWorkspaceRoot\(/.test(panel))
check('B2 面板调用 setWorkspaceRoot', /userSettingsApi\.setWorkspaceRoot\(/.test(panel))
check('B3 桌面桥 pickDirectory 接线', /NativeBridge[\s\S]{0,80}pickDirectory/.test(panel))
check('B4 留空=恢复默认（null 载荷）', /value\s*\|\|\s*null/.test(panel))
check('B5 显示当前生效路径', /当前生效/.test(panel))

const dlg = read('src/components/SystemSettingsDialog.vue')
check('C1 面板已导入', /import WorkspaceSettingsPanel from '\.\/WorkspaceSettingsPanel\.vue'/.test(dlg))
check('C2 tabs 含 workspace 键', /\{ key: 'workspace', label: '工作区' \}/.test(dlg))
check('C3 渲染分支接线', /activeTab === 'workspace'[\s\S]{0,200}<WorkspaceSettingsPanel \/>/.test(dlg))

process.exit(ok ? 0 : 1)
