// 右键菜单模板构建器（2026-09-30 右键复制粘贴波，判据#2）。
// 纯函数：由 webContents 'context-menu' 的 params 构建 Electron MenuItem 模板。
//   - editable：undo/redo/cut/copy/paste/selectAll，enabled 取 params.editFlags
//   - selection：copy/selectAll（绝无 paste——粘贴只属于可编辑区）
//   - link：复制链接（deps.clipboard.writeText）
//   - image：copyImage
//   - 空场景：[] → 主进程据此不弹菜单
// deps 注入 clipboard（单测可 spy；主进程传 electron clipboard）。
'use strict';

/** @param {object} params webContents context-menu 事件 params 摘要 */
/** @param {{clipboard?: {writeText: (s: string) => void}}} deps */
function buildContextMenuTemplate(params, deps) {
  const flags = (params && params.editFlags) || {}
  const template = []

  if (params && params.isEditable) {
    if (flags.canUndo) template.push({ role: 'undo', enabled: true })
    if (flags.canRedo) template.push({ role: 'redo', enabled: true })
    if (flags.canUndo || flags.canRedo) template.push({ type: 'separator' })
    if (flags.canCut) template.push({ role: 'cut', enabled: true })
    if (flags.canCopy) template.push({ role: 'copy', enabled: true })
    if (flags.canPaste) template.push({ role: 'paste', enabled: true })
    if (flags.canCut || flags.canCopy || flags.canPaste) template.push({ type: 'separator' })
    if (flags.canSelectAll) template.push({ role: 'selectAll', enabled: true })
    return template
  }

  const selection = params && params.selectionText ? String(params.selectionText) : ''
  if (selection.trim()) {
    if (flags.canCopy !== false) template.push({ role: 'copy', enabled: true })
    if (flags.canSelectAll !== false) template.push({ role: 'selectAll', enabled: true })
    return template
  }

  if (params && params.linkURL) {
    const url = String(params.linkURL)
    template.push({
      label: '复制链接',
      enabled: true,
      click: () => {
        if (deps && deps.clipboard && typeof deps.clipboard.writeText === 'function') {
          deps.clipboard.writeText(url)
        }
      },
    })
  }

  if (params && params.mediaType === 'image') {
    template.push({ role: 'copyImage', enabled: true })
  }

  return template
}

module.exports = { buildContextMenuTemplate }
