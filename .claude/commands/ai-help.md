用简洁、可操作的方式说明工作流。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能：

- `help-router`
- `constraints-loader`

## 输出要求

1. 一张 markdown 表，列出当前所有命令。
2. 每条命令给出：命令名、用途、是否只读、何时用、一句话示例。
3. 基于 `goals/INDEX.md` 与各 `goals/<id>.state.yaml` 的并行状态小结。
4. 推荐当前最该执行的那一条命令，并说明原因。

## 命令清单

- `/ai-init`
- `/ai-goal`
- `/ai-dispatch`
- `/ai-claim`
- `/ai-status`
- `/ai-check`
- `/ai-sync`
- `/ai-help`
- `/ai-notes`

## 边界

- 优先依据当前工作流状态，而非通用建议。
- 没有活跃目标时明确说明。
- 遵守已加载的项目约束。
