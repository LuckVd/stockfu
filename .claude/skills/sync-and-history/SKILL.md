# sync-and-history

Use this skill during `/ai-sync`.

Use Chinese for all user-facing natural language output. Keep commands, file paths, and code identifiers in their original form.

Responsibilities:

- update the roadmap table row for the finished goal or subgoal
- mark the goal status accurately in `goals/<id>.state.yaml` (`stage: done`, `merge_status: merged`)
- append a concise change-log entry
- keep summaries short and factual
- merging into the main line is done by the orchestrator only; executors do not merge

Sync checklist:

1. goal-related tests passed
2. roadmap reflects the new status, dependencies, and verification result
3. change-log captures impact and verification
4. `goals/<id>.state.yaml` is updated (`stage: done`, `merge_status: merged`)
