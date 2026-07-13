目标实现并验证后，把状态同步回路线图与变更记录。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能：

- `sync-and-history`
- `constraints-loader`

## 步骤

1. 确认当前目标相关测试已通过（必要时调用原生 `/verify`）。
2. 更新 `roadmap.md`、`goals/<id>.md`、`goals/<id>.state.yaml`、`change-log.md`（`stage: done`、`merge_status: merged`）。
3. 把目标结果回写进 roadmap 表格：状态、依赖相关说明、测试状态、实现日期、commit id（若有）。
4. 汇总完成内容、影响范围、测试结果。

## 代码质量与安全

本命令不再自带扫描。提交前按需调用（见 `docs/ai/convention.md`）：

- 代码 / 死代码 → `/code-review`
- 安全 → `/security-review`

## 提交

5. 向用户展示建议的 commit message。
6. 任何 `git add` / `commit` / `push` 前必须显式确认。
7. 用户同意则创建本地 commit。
8. 用户同意则推送到远端。
9. 推送失败则报告失败，保留本地 commit。

## 边界

- 未经确认不 commit / push。
- 安全问题应阻断提交，除非用户明确覆盖。
- 推送失败不回滚本地 commit。
- 合并与提交由编排者在主线完成；执行者不合并、不向主线提交。
