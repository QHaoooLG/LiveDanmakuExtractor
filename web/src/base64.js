/**
 * base64 → 字节的转换（统计数据导出 xlsx 用）。
 *
 * 为什么不放在 `App.vue` 里：SFC 只能靠 Vite 的 `ssrLoadModule` 渲染，
 * `node --test` 直接测不了；而这段转换逻辑是本轮最容易被写错的一块
 * （见下面 `base64ToBytes` 的注释），必须能单独测。
 */

/**
 * base64 → `Uint8Array`。
 *
 * ⚠️ **不要**写成 `Uint8Array.from(atob(text))`：那个重载把字符串当**类数组**，
 * 逐字符取 `charCode` 后**截断到低 8 位** —— 对 ASCII 恰好对，对 `>0x7F`
 * 的字节全毁（`0xFF` 会变成 `0x00`+…）。xlsx 是 zip，里面必然有大量
 * 高位字节，这么写出来的文件 Excel 一律打不开。
 *
 * 也不要 `new TextEncoder().encode(atob(text))`：那是把二进制当 UTF-8
 * 文本再编码一遍，`0x80` 以上会被扩成两字节。
 *
 * 唯一正确的做法是逐字符 `charCodeAt`，因为 `atob` 的返回值语义就是
 * "每个字符 = 一个字节"。
 */
export function base64ToBytes(text) {
  const binary = atob(text)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}
