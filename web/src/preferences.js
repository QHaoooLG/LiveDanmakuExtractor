/**
 * 面板显示偏好的本地存取（localStorage）。
 *
 * 键名沿用 `App.vue` 里那几个偏好的 `bili-danmaku.*` 前缀，便于一起清理。
 * `storage` 由调用方显式传入而不是在模块里直接抓 `localStorage`：
 * 这样这段逻辑不依赖浏览器环境，也便于隔离"隐私模式下 localStorage 直接抛异常"
 * 这条真实失败路径。
 */

/** 弹幕流是否**匿名化**发言者昵称（只留首字符） */
export const ANONYMIZE_NAME_KEY = 'bili-danmaku.anonymize-name'

/** 当天导出序号的前缀（按天独立计数，见 `nextExportSeq`） */
export const EXPORT_SEQ_PREFIX = 'bili-danmaku.export-seq.'

/**
 * 是否匿名化发言者昵称。
 *
 * **缺省为假**（"默认不进行匿名"是需求）；只有明确存过 `'1'` 才匿名。
 * 值缺失或读失败（隐私模式、配额异常）都退回缺省 —— 读偏好失败不该让弹幕流崩掉。
 */
export function loadAnonymizeName(storage) {
  try {
    return storage?.getItem(ANONYMIZE_NAME_KEY) === '1'
  } catch {
    return false
  }
}

/** 记住"是否匿名化昵称"。写失败静默忽略：只是偏好没存上，不该打断当前视图。 */
export function saveAnonymizeName(storage, value) {
  try {
    storage?.setItem(ANONYMIZE_NAME_KEY, value ? '1' : '0')
  } catch {
    /* 存不下就算了，本次会话内的显示状态仍然正确 */
  }
}

/**
 * 取"今天的导出序号"并自增，用于导出文件名里的 `<当天导出编号>`。
 *
 * 按**本地日期**分桶（`YYYYMMDD`）：用户说的"当天"是本地时间的概念，
 * 跨零点后应该从 1 重新数。键里带日期，所以历史计数不会互相干扰。
 *
 * 从 1 开始；读/写失败一律退回 1（宁可两次导出重名，也不要因为偏好读不到就导不出）。
 */
export function nextExportSeq(storage, now = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  const day = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
  const key = `${EXPORT_SEQ_PREFIX}${day}`
  let next = 1
  try {
    const raw = storage?.getItem(key)
    const current = Number(raw)
    if (Number.isFinite(current) && current >= 0) next = Math.floor(current) + 1
  } catch {
    return { day, seq: 1 }
  }
  try {
    storage?.setItem(key, String(next))
  } catch {
    /* 记不住就每次都从 1 开始，不影响本次导出 */
  }
  return { day, seq: next }
}

/**
 * 组装导出文件名：`<主播名字>_<房间号>_<日期>_<当天导出编号>.csv`。
 *
 * 文件名**必须在这里（前端）拼**：服务端只能给 `Content-Disposition`，
 * 而浏览器「另存为」用的名字由前端 `download` 属性决定，服务端那个名字在
 * 流式下载（`window.location.href`）下并不受控。
 *
 * 各段都做**文件名清洗**：去掉 `\/:*?"<>|` 与控制字符，
 * 否则主播昵称里带 `/` 会让文件名变成一个路径（Windows 上直接失败）。
 */
export function buildExportFilename({
  anchorName,
  roomId,
  day,
  seq,
  ext = 'csv',
} = {}) {
  const parts = [
    sanitize(anchorName) || '未知主播',
    sanitize(roomId) || '全部房间',
    sanitize(day) || '日期未知',
    String(seq ?? 1),
  ]
  return `${parts.join('_')}.${ext}`
}

/** 清掉非法文件名字符；顺带把连续空白收成一个空格。 */
export function sanitize(value) {
  if (value == null) return ''
  return String(value)
    // eslint-disable-next-line no-control-regex
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}
