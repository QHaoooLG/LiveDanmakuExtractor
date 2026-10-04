/**
 * 让 `node --test` 直接 `import` `.vue`（客户端模式编译）。
 *
 * ## 为什么不用 Vite 的 `ssrLoadModule()`
 *
 * 它是为**服务端渲染**设计的：`@vitejs/plugin-vue` 会按 SSR 模式编译 SFC，
 * 产物里只有 `ssrRender`、**没有** `render`。`@vue/test-utils` 挂载时因此报
 * 一句完全指不到病根的
 * `TypeError: Cannot read properties of undefined (reading 'modules')`
 * （实测踩到；真正的提示是同时打出的
 * `Component is missing template or render function`）。
 *
 * 更麻烦的是：SFC 由 Vite 解析依赖、测试文件由 Node 解析依赖，两边会各拿到一份
 * Vue，`currentInstance` 互不相认。
 *
 * 所以改成**自己的 loader**：用 `@vue/compiler-sfc` 以 `inlineTemplate` +
 * `isProd: false` 编译，模块图完全交给 Node —— 一份 Vue、一个 `render`。
 * 这样也不需要在测试里起 Vite dev server。
 *
 * 用法（`npm test` 已经带上了参数）：
 *     node --import ./test/register-vue.mjs --test ...
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { compileScript, compileStyle, compileTemplate, parse } from '@vue/compiler-sfc'

/** `id` 只用来生成 scoped 属性的哈希；路径唯一即可（与官方插件同样的思路）。 */
function scopeIdFor(filename) {
  let hash = 0
  for (let i = 0; i < filename.length; i += 1) {
    hash = (hash * 31 + filename.charCodeAt(i)) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

function compileSFC(filename) {
  const source = readFileSync(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  if (errors.length) {
    const detail = errors.map((error) => error.message ?? String(error)).join('; ')
    throw new Error(`[vue-loader] ${filename} 解析失败：${detail}`)
  }

  const id = scopeIdFor(filename)
  const scopeId = `data-v-${id}`
  const hasScoped = descriptor.styles.some((style) => style.scoped)
  const parts = []

  // 1) <script> / <script setup> —— `inlineTemplate: true` 把 render 直接内联进
  //    这个函数的返回值，省掉一次单独的模板编译与一次导入。
  let bindingMetadata = {}
  if (descriptor.script || descriptor.scriptSetup) {
    const compiled = compileScript(descriptor, {
      id,
      inlineTemplate: true,
      isProd: false,
      genDefaultAs: '__sfc__',
      // 有 scoped 样式时要挂 scopeId，与官方插件的行为保持一致
      templateOptions: hasScoped ? { scoped: true, compilerOptions: { scopeId } } : {},
    })
    bindingMetadata = compiled.bindings ?? {}
    parts.push(compiled.content)
  } else {
    parts.push('const __sfc__ = {}')
  }

  // 2) 没有 script 的纯模板 SFC：单独编译模板并挂上去
  if (!descriptor.script && !descriptor.scriptSetup && descriptor.template) {
    const { code } = compileTemplate({
      id,
      filename,
      source: descriptor.template.content,
      scoped: hasScoped,
      compilerOptions: hasScoped ? { scopeId, bindingMetadata } : { bindingMetadata },
    })
    parts.push(code.replace('export function render', 'function render'))
    parts.push('__sfc__.render = render')
  }

  if (!descriptor.script && !descriptor.scriptSetup) {
    parts.push(`__sfc__.__file = ${JSON.stringify(filename)}`)
  }
  if (hasScoped) {
    parts.push(`__sfc__.__scopeId = ${JSON.stringify(scopeId)}`)
  }

  // 3) 样式：测试不校验外观，编译一遍只为**走到**这条路径（写错的选择器会在这里报错），
  //    产物丢弃，不注入 DOM。
  for (const [index, style] of descriptor.styles.entries()) {
    compileStyle({
      id: scopeId,
      filename,
      source: style.content,
      scoped: Boolean(style.scoped),
    })
    void index
  }

  parts.push('export default __sfc__')
  return parts.join('\n')
}

/** `<style>` 的请求：给一个空模块（Node 不认识 CSS，测试也不需要它）。 */
function compileStyleRequest() {
  return 'export default {}'
}

export async function load(url, context, nextLoad) {
  if (!url.includes('.vue')) return nextLoad(url, context)

  if (url.includes('vue&type=style') || url.endsWith('.css')) {
    return { format: 'module', shortCircuit: true, source: compileStyleRequest() }
  }

  const [path] = url.split('?')
  const filename = fileURLToPath(path)
  return { format: 'module', shortCircuit: true, source: compileSFC(filename) }
}
