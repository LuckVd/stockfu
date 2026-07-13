#!/usr/bin/env bash
# PreToolUse hook(Bash matcher):兜底拦截破坏性 git 操作。
# 读 stdin JSON 的 tool_input.command,匹配 push --force / push --delete / reset --hard 到远端。
# 命中 → permissionDecision:deny + exit 2;否则 exit 0 放行。
#
# 注意:用户选择「AI 执行 commit(经确认)」,所以普通 git commit/push 由 /ai-sync 经
# 用户确认后执行,这里不阻断(避免误伤)。只兜底不可逆的破坏性操作。
# hook 字符串匹配本身不可靠(GitHub issue #36389),仅作防御纵深;主闸是用户确认 gate。

set -o pipefail

cmd=$(jq -r '.tool_input.command // ""' 2>/dev/null || echo "")

case "$cmd" in
  *git*)
    if echo "$cmd" | grep -qE 'git[[:space:]]+push[[:space:]]+.*(--force|--delete)|git[[:space:]]+reset[[:space:]]+--hard[[:space:]]+[^[:space:]]*(origin|refs/remotes)'; then
      printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"破坏性 git 操作(push --force / push --delete / reset --hard 到远端)被兜底拦截:不可逆,请在自己的终端手动执行。普通 commit/push 经 /ai-sync 确认后可正常执行。"}}\n'
      exit 2
    fi
    ;;
esac

exit 0
