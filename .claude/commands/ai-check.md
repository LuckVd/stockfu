对当前目标做健康检查。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能：

- `constraints-loader`

## 检查项

1. 工作流文件是否缺失或过时。
2. `goals/<id>.md` 与 `goals/<id>.state.yaml` 是否一致；目标的 `owner`/`branch`/`worktree` 三者是否匹配。
3. 是否缺验收标准或测试计划。
4. 实现是否偏离项目约定。
5. 是否还有阻塞实现的 open question。

## 代码质量与安全

本命令不再自带扫描。需要时调用原生能力（见 `docs/ai/convention.md`）：

- 代码 / 死代码 / 可简化点 → `/code-review`、`/simplify`
- 密钥与安全 → `/security-review`

## 输出

- 先给发现，按严重度排序。
- 再给出明确的下一步动作。
