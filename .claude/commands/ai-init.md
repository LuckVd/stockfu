初始化或修复工作流骨架，并把框架安全接入已有项目（原 /ai-adopt 已并入本命令）。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能：

- `init-skeleton`
- `constraints-loader`

## 步骤

1. 确认 `docs/ai` 与 `.claude/{commands,skills,agents}` 存在；缺失则用模板补齐。
2. 判断场景：
   - 空项目：可接受一份技术蓝图（粘贴文本或仓库内路径），解析为规划文档。
   - 已有项目：检测接入状态，仅在安全时补齐缺失骨架，产出接入报告。
3. 只写规划文档：
   - `roadmap.md`
   - `goals/`（含 `INDEX.md` 与 `_TEMPLATE.*`）
   - `constraints/project.md`
4. 不自动锁定首个目标，只产出候选写入 roadmap，交给 `/ai-goal`。
5. 不覆盖已有实质内容，除非用户要求。
6. 汇总创建 / 修复 / 从蓝图初始化了什么。

## 边界

- 不生成业务实现代码。
- 不修改业务代码。
- 不自动 commit 或 push。
- 不再维护 project-tree / project-summary，让模型自己读仓库。
- 发现已有工作流与本框架冲突，停下来问用户怎么办。
