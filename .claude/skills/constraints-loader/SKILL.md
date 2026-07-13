# constraints-loader

Use this skill whenever implementation or review should honor project-specific constraints.

Load order:

1. `docs/ai/constraints/global.md`
2. matching language constraint files
3. matching framework constraint files
4. `docs/ai/constraints/project.md`

Guidelines:

- Load only files relevant to the current task.
- If no matching file exists, continue with the default workflow.
- Prefer explicit constraints over inferred preferences.
- Apply loaded constraints to all downstream user-facing natural language output.
