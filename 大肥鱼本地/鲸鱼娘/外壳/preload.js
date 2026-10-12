// 预加载脚本：给网页搭一座桥，让 Windows 版和 macOS 版用同一套前端协议。
//
//   window.__DSHPET_SHELL__                 页面用它判断"我在不在桌面壳里"
//   window.webkit.messageHandlers.dshpetshell  和 macOS 版一一对应的消息通道
//   window.dshpetShell                     壳子自己加的（收起小球 / 命中 / 全局光标…，见下）
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('__DSHPET_SHELL__', true)
contextBridge.exposeInMainWorld('webkit', {
  messageHandlers: {
    dshpetshell: {
      postMessage: (msg) => ipcRenderer.send('shell-msg', String(msg)),
    },
  },
})

// ————————————————————————————————————————————————————————————————
// 全局光标：主进程每 16ms 喂一次「光标在窗口坐标系里的位置」。
//
// ⚠️ 只喂这一个只读值，**绝不再往页面里注入假鼠标事件**。
//    以前主进程用 webContents.sendInputEvent({type:'mouseMove'}) 注入，
//    坐标还是「整块屏幕归一化 × 窗口宽高」，会把页面的命中判定（决定窗口要不要
//    点击穿透）每 16ms 带偏一次 —— 按钮点不住、拖她抽搐。详见 外壳\main.js。
//
// seq 用来自报"我还有数据"：时间戳不能跨进程/跨上下文比（性能时钟原点不同），
// 所以由页面自己记录"上一次收到新 seq 是什么时候"。
let globalCursor = null
let globalCursorSeq = 0
ipcRenderer.on('cursor-global', (_e, pos) => {
  if (!pos) return
  const x = Number(pos.x)
  const y = Number(pos.y)
  if (!Number.isFinite(x) || !Number.isFinite(y)) return
  globalCursorSeq += 1
  globalCursor = { x: x, y: y, seq: globalCursorSeq }
})

// 壳子自己的接口：原来这些是系统托盘干的活，主人要求不用托盘了，改到网页控制条里。
contextBridge.exposeInMainWorld('dshpetShell', {
  hit: (kind) => ipcRenderer.send('shell-hit', String(kind)),
  toggleVisible: () => ipcRenderer.send('shell-toggle-visible'),
  hide: () => ipcRenderer.send('shell-hide'),
  reload: () => ipcRenderer.send('reload'),
  resetPos: () => ipcRenderer.send('shell-reset-pos'),
  setLowPower: (on) => ipcRenderer.send('shell-low-power', !!on),
  quit: () => ipcRenderer.send('shell-quit'),
  collapse: () => ipcRenderer.send('shell-collapse'),
  // 全局光标（只读，给视线跟随用）：返回 {x, y, seq} 或 null
  globalCursor: () => globalCursor,
  // 提示页那张卡片上的按钮
  install: () => ipcRenderer.send('install-plugin'),
  ballMove: (dx, dy) => ipcRenderer.send('ball-move', dx, dy),
  ballDrop: (moved) => ipcRenderer.send('ball-drop', moved),
})

// ————————————————————————————————————————————————————————————————
// 拖动：按下她 → 告诉主进程搬窗口。
//
// ⚠️ 原来这里是「任何左键按下都发 drag-start」，再由主进程用
//    elementFromPoint(坐标) 反过来判断"鼠标下面是不是控件"。
//    屏幕缩放不是 100% 时，渲染进程的 e.screenX 和主进程的窗口坐标
//    不在同一个坐标系里，判断就偏了 —— 于是拖设置里的滑块时，
//    主进程以为按在她身上，跟着鼠标搬窗口，滑块轨道从鼠标底下跑掉，
//    参数直接抽到最大。主人反馈的正是这个。
//
//    现在改成**按真实 DOM 目标判断**：事件目标在控件里就压根不发拖动，
//    不依赖任何坐标换算，缩放多少都不会错。
const NO_DRAG = [
  '.dshp-panel', '.dshp-menu', '.dshp-hud', '.dshp-bubble', '.dshp-composer',
  '.dshp-dock', '.dshp-tab', '.dshp-label', '.dshp-row', '.dshp-grid',
  '.dshp-panes', '.dshp-tabs', '.dshp-btn', '.dshp-chip', '.dshp-close',
  'input', 'select', 'textarea', 'button', 'label', 'a',
  '#bar', '#panel',
].join(',')

function inControl(e) {
  const t = e.target
  if (!t || typeof t.closest !== 'function') return false
  return !!t.closest(NO_DRAG)
}

window.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return
  if (inControl(e)) return // 控件上的按下交给网页，不搬窗口
  ipcRenderer.send('drag-start', { x: e.screenX, y: e.screenY })
}, true)

window.addEventListener('mouseup', () => ipcRenderer.send('drag-end'), true)
