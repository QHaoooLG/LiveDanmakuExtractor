/**
 * 注册 `test/vue-loader.mjs`，让 `node --test` 能直接 import `.vue`。
 *
 * 由 `npm test` 通过 `--import ./test/register-vue.mjs` 加载。
 * 只做一件事：登记 loader hook（Node 18.19+ / 20.6+ 的 `module.register()`）。
 */

import { register } from 'node:module'

register('./vue-loader.mjs', import.meta.url)
