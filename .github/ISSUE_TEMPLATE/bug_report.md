---
name: Bug 报告
about: 报告一个可复现的问题
title: "[Bug] "
labels: ["bug"]
assignees: []
---

<!--
⚠️ 贴日志/配置前请确认其中不含 SESSDATA、cookie、bili_jct、buvid 或任何真实用户 uid。
   本项目处理弹幕数据，请使用脱敏输出（--anonymize）或伪造样本。
-->

## 问题描述

<!-- 一句话说清"期望发生什么"vs"实际发生什么" -->

## 复现步骤

1.
2.
3.

## 复现命令

```bash
# 例如：bili-danmaku probe 7878 --seconds 30
```

## 期望行为

## 实际行为

<!-- 附上退出码。probe/grab 的退出码含义：
     0 正常 · 2 配置错误 · 3 房间/接口问题 · 4 认证失败 · 5 网络不可用 -->

## 环境

| 项 | 值 |
|----|----|
| 操作系统 | <!-- 如 Windows 11 / Ubuntu 24.04 --> |
| Python 版本 | <!-- python -V --> |
| 安装方式 | <!-- pip install -e ".[dev]" / wheel / exe --> |
| 版本 / 提交 | <!-- git rev-parse --short HEAD --> |
| 是否登录态 | <!-- 是（扫码 / 环境变量）/ 否（匿名） --> |
| 面板是否已构建 | <!-- GET /api/health 的 frontend_built 字段 --> |

## 诊断输出

<details>
<summary>展开：相关日志 / probe 输出 / <code>/api/health</code> 响应</summary>

```
（粘贴在这里，务必先脱敏）
```

</details>

## 补充信息

<!-- 是否必现？换房间/换网络还复现吗？最近做过什么改动？ -->
