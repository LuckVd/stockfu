# 并行目标看板(派生投影)

> 本文件由 `/ai-status`、`/ai-help` 或编排者从各 `goals/<id>.state.yaml` **重新渲染**,不要手工长编辑。
> 长期事实在 `../roadmap.md`,活跃态在各 `goals/<id>.state.yaml`,本表只是便利视图。

## 活跃目标

| 目标ID | 标题 | stage | owner | branch | worktree | merge_status | 备注 |
|---|---|---|---|---|---|---|---|
| G02 | 回测基准激活(任意区间+更新机制) | done | — | — | — | merged | 已合并 775e881(main,未推送)。基准=上证综指 sh000001;最小激活;实证通过。见 goals/G02.md |

## stage 图例

`empty → discover → options → design → confirm_plan → ready_to_claim → claimed → implementing → ready_for_review → reviewing → sync → done`(分支 `blocked` / `dropped`)

## 僵尸领取检测

扫表时若 `stage=claimed` 且 `claimed_at` 超过约定时限(默认 24h)未推进到 `implementing`,标记为僵尸:编排者把 `owner` 置空、`stage` 回 `ready_to_claim`,该目标可被重新领取。
