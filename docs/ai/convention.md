# 工作约定

本框架只保留强 LLM 和原生平台都替代不了的东西：

- **外部记忆**：跨会话的路线图与并行目标
- **约束一致性**：硬约束的统一加载与检查
- **进度回写**：把实现结果同步回路线图与变更记录
- **人类确认 gate**：方案、提交、推送前的确认

其余能力一律交给 Claude Code 原生机制，不要自己造。

## 原生能力对照表

| 需求 | 用这个，不要自己实现 |
|---|---|
| 实现前确认方案 | `plan mode` / `ExitPlanMode` |
| 方案评审 | `design-reviewer` subagent |
| 影响面分析 | `impact-mapper` subagent |
| 代码审查 / 死代码 / 可简化点 | 原生 `/code-review`、`/simplify` |
| 密钥与安全扫描 | 原生 `/security-review` |
| 实现后端到端验证 | 原生 `/verify` |
| 模型分层（贵模型规划 / 便宜模型开发） | 原生 `Agent` 工具（`model` 参数）+ worktree 隔离 |
| 并行目标看板 | `/ai-status` + `goals/INDEX.md`（派生投影） |
| 不自动提交的纪律 | `.claude/settings.json` hooks（兜底破坏性 git） |
| 跨会话记忆 | 本框架的 `roadmap.md` + `goals/` |

## 原则

- 状态只读写 `roadmap.md` / `goals/<id>.md` / `goals/<id>.state.yaml` / `goals/INDEX.md` / `change-log.md`。
- 让模型自己读代码，不维护项目结构摘要。
- 关键节点（方案确认、提交、推送）必须停下问用户。
- 发现文档与代码不一致，先问用户，不要擅自改。
- bug 修复、特性开发、TDD 这些流程本身交给正常对话，不再由命令强制驱动。

## 模型分工

多实例并行协作时，按成本分层指派角色：

- **编排者（贵模型）**：roadmap 规划、目标设计与方案确认（`/ai-goal`）、发布任务（`/ai-dispatch`）、影响面分析与方案评审（`impact-mapper`/`design-reviewer`）、合并回主线、状态回写（`/ai-sync`）、看板维护。建议同一时刻只有一个编排者实例负责主线合并，避免合并并发。
- **执行者（便宜模型，可多个）**：用 `/ai-claim` 领取 `ready_to_claim` 目标，在专属 worktree/分支里按已确认方案写代码、跑测试、修复，完工 parking。

模型分层通过原生 `Agent` 工具的 `model` 参数实现：主会话用贵模型，派发给执行者的 subagent 指定便宜 model。多实例协作（多个独立会话）则各自领目标，靠文件协议协调。

## 并行编排

- **worktree 约定**：每个目标一个分支 `goal/<id-小写kebab>` 与 worktree `.claude/worktrees/<id-小写kebab>/`，基于主线 HEAD 创建：`git worktree add .claude/worktrees/<id> -b goal/<id> main`。
- **领取协议**：执行者 `/ai-claim` 守卫 `owner` 为空且 `stage: ready_to_claim` → 写 owner/branch/worktree → 写后回读校验（Tier 1 伪原子）。文件系统无真锁，残余竞态由编排者合并阶段兜底。
- **合并 gate**：执行者完工置 `ready_for_review` 停下；编排者评审（`/ai-check` + 原生 `/code-review`/`/verify`）→ 合并 → `/ai-sync` 回写 roadmap。合并与提交只由编排者在主线做。
- **僵尸领取回收**：`stage: claimed` 且 `claimed_at` 超时未推进，编排者回收（owner 置空、stage 回 `ready_to_claim`）。
- **执行者红线**：不得改 roadmap、不得自行合并、不得 commit/push 到主线。

## 提交纪律

用户选择「AI 执行 commit（经确认）」。因此 hook 不阻断普通 commit/push（避免误伤 `/ai-sync`），只兜底破坏性 git 操作（`git push --force`、`git push --delete`、`git reset --hard` 到远端）。完整纪律依赖三层：用户确认 gate（主闸）、执行者红线约定、hook 兜底。hook 字符串匹配本身不可靠（见 GitHub issue #36389），工作流不主动调用破坏性操作即是无单点故障的保障。
