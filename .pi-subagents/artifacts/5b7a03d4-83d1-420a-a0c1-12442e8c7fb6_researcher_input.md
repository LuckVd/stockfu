# Task for researcher

调研 2025-2026 年中国 A 股市场风格因子/异象的最新实证研究，为扩展量化选股策略服务。当前已有策略覆盖：价值、股息、动量、反转、低波、低β、规模、流动性；缺少质量、成长、盈利（缺财务数据）和行业轮动。请从以下角度做 2-4 组搜索：

1. A 股质量因子（ROE、毛利率、资产负债率）与成长因子（营收/利润增速）近年有效性实证，是否显著、是否与价值因子正交，2024-2026 是否有效
2. 近年新被验证的、与价值正交的 A 股异象/因子（如成交干旱/低关注度、偏度、振幅、隔夜收益、拥挤度、涨停效应等）的实证证据
3. A 股行业轮动/行业中性化实证：行业动量、板块资金流的预测能力

输出一份中文调研简报（markdown），结构：Summary（2-3 句直接回答）、Findings（编号+来源引用）、Sources（保留/丢弃）、Gaps。重点关注可复现性（用日线数据能否实现）和与现有价值系策略的正交性。输出到 /tmp/research-style-factors-2026.md

---
**Output:**
Write your findings to exactly this path: /tmp/research-style-factors-2026.md
This path is authoritative for this run.
Ignore any other output filename or output path mentioned elsewhere, including output destinations in the base agent prompt, system prompt, or task instructions.

## Acceptance Contract
Acceptance level: attested
Completion is not accepted from prose alone. End with a structured acceptance report.

Criteria:
- criterion-1: Return concrete findings with file paths and severity when applicable

Required evidence: review-findings, residual-risks

Finish with a fenced JSON block tagged `acceptance-report` in this shape:
Use empty arrays when no items apply; array fields contain strings unless object entries are shown.
`criteriaSatisfied[].status` must be exactly one of: satisfied, not-satisfied, not-applicable.
`commandsRun[].result` must be exactly one of: passed, failed, not-run.
`manualNotes` and `notes` are optional strings; an empty string means no note and does not satisfy `manual-notes` evidence.
```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "specific proof"
    }
  ],
  "changedFiles": [
    "src/file.ts"
  ],
  "testsAddedOrUpdated": [
    "test/file.test.ts"
  ],
  "commandsRun": [
    {
      "command": "command",
      "result": "passed",
      "summary": "short result"
    }
  ],
  "validationOutput": [
    "validation output or concise summary"
  ],
  "residualRisks": [
    "none"
  ],
  "noStagedFiles": true,
  "diffSummary": "short description of the diff",
  "reviewFindings": [
    "blocker: file.ts:12 - issue found, or no blockers"
  ],
  "manualNotes": "anything else the parent should know"
}
```