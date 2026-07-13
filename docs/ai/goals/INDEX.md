# 并行目标看板(派生投影)

> 本文件由 `/ai-status`、`/ai-help` 或编排者从各 `goals/<id>.state.yaml` **重新渲染**,不要手工长编辑。
> 长期事实在 `../roadmap.md`,活跃态在各 `goals/<id>.state.yaml`,本表只是便利视图。

## 活跃目标

| 目标ID | 标题 | stage | owner | branch | worktree | merge_status | 备注 |
|---|---|---|---|---|---|---|---|

_暂无活跃目标。编排者用 `/ai-goal` + `/ai-dispatch` 发布;执行者用 `/ai-claim [id]` 领取(不带 id 则列出可领取目标)。_

## stage 图例

`empty → discover → options → design → confirm_plan → ready_to_claim → claimed → implementing → ready_for_review → reviewing → sync → done`(分支 `blocked` / `dropped`)

## 僵尸领取检测

扫表时若 `stage=claimed` 且 `claimed_at` 超过约定时限(默认 24h)未推进到 `implementing`,标记为僵尸:编排者把 `owner` 置空、`stage` 回 `ready_to_claim`,该目标可被重新领取。
