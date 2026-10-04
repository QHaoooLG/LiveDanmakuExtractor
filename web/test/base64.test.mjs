/**
 * `src/base64.js` 的单测。
 *
 * 这个文件存在的唯一理由：`base64ToBytes` 有两种"看起来对、实际错"的写法
 * （`Uint8Array.from(atob(s))` 与 `TextEncoder`），而错了之后**不报错** ——
 * 只是导出的 xlsx 打不开。所以要有一个拿真实 xlsx 字节来验的测试。
 *
 * 环境说明：`node --test` 里没有 `atob`，用 `Buffer` 补上
 * （Node 18+ 其实自带 `atob`，但显式兜底能让脚本在更老的运行时也不炸）。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { base64ToBytes } from '../src/base64.js'

if (typeof globalThis.atob !== 'function') {
  globalThis.atob = (text) => Buffer.from(text, 'base64').toString('binary')
}

/** 参照实现：用 Buffer 解，作为"标准答案"。 */
function reference(text) {
  return new Uint8Array(Buffer.from(text, 'base64'))
}

test('把 ASCII base64 解成对应字节', () => {
  const bytes = base64ToBytes(Buffer.from('hello').toString('base64'))
  assert.deepEqual(Array.from(bytes), Array.from(Buffer.from('hello')))
})

test('高位字节不被截断（0x80–0xFF 是关键回归点）', () => {
  // 0x00..0xFF 全谱：低 7 位与高位的字节都在里面。
  // 用 `Uint8Array.from(atob(s))` 的错误写法会让 0x80 以上全部变成 0x00+..，
  // 这个用例就是专门钉住它的。
  const raw = Buffer.from(Array.from({ length: 256 }, (_, i) => i))
  const bytes = base64ToBytes(raw.toString('base64'))
  assert.deepEqual(Array.from(bytes), Array.from(raw))
  // 顺带确认对照实现一样，避免"两边一起错"
  assert.deepEqual(Array.from(bytes), Array.from(reference(raw.toString('base64'))))
})

test('xlsx 的 zip 魔数原样还原（PK\\x03\\x04）', () => {
  // 真实 xlsx 的前 4 字节；`PK\x03\x04` 里的 0x03/0x04 是控制字符，
  // 任何"当文本处理"的实现都会在这里露馅。
  const head = Buffer.from([0x50, 0x4b, 0x03, 0x04])
  const bytes = base64ToBytes(head.toString('base64'))
  assert.deepEqual(Array.from(bytes), [0x50, 0x4b, 0x03, 0x04])
})

test('空串得到空字节数组（不抛）', () => {
  assert.equal(base64ToBytes('').length, 0)
})

test('长度与 base64 解出来的字节数一致（没有多字节膨胀）', () => {
  // 64 KiB 的伪随机数据：如果实现是按 UTF-8 再编码，长度会明显变长。
  const raw = Buffer.alloc(65536)
  for (let i = 0; i < raw.length; i += 1) raw[i] = (i * 37 + 11) & 0xff
  const bytes = base64ToBytes(raw.toString('base64'))
  assert.equal(bytes.length, raw.length)
  assert.deepEqual(Array.from(bytes.subarray(0, 64)), Array.from(raw.subarray(0, 64)))
})
