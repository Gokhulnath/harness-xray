#!/bin/bash
# Claude Code hooks that move tool work to the tools container ($CCO_TOOLS, see execd.py).
#   pre-bash     rewrite the Bash command into `rexec`, which runs it on the tools container (all other input fields kept)
#   pre-read     before Read/Edit/Write: refresh the local mirror of the file from the tools container if it changed there,
#                so Claude Code's own "modified since read" check sees remote changes exactly as it would see local ones
#   post-upload  after Write/Edit changed the local mirror, upload the file to the tools container
set -u
in=$(cat)
case "$1" in
  pre-bash)
    cmd=$(jq -r '.tool_input.command' <<<"$in")
    cwd=$(jq -r '.cwd // "/work/next-app"' <<<"$in")
    to=$(jq -r '((.tool_input.timeout // 600000) / 1000 | floor)' <<<"$in")
    new="/opt/cco/rexec $(printf %s "$cmd" | base64 -w0) $(printf %s "$cwd" | base64 -w0) $to"
    jq -c --arg c "$new" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "allow",
      updatedInput: (.tool_input + {command: $c})}}' <<<"$in"
    ;;
  pre-read)
    path=$(jq -r '.tool_input.file_path' <<<"$in")
    tmp="/tmp/cco-dl.$$"
    code=$(curl -s -o "$tmp" -w '%{http_code}' --get --data-urlencode "path=$path" "$CCO_TOOLS/file")
    if [ "$code" = 200 ]; then
      if [ ! -f "$path" ] || [ "$(sha256sum < "$tmp")" != "$(sha256sum < "$path")" ]; then
        mkdir -p "$(dirname "$path")" && cp "$tmp" "$path"
      fi
    fi
    ;;
  post-upload)
    path=$(jq -r '.tool_input.file_path' <<<"$in")
    [ -f "$path" ] && curl -s -o /dev/null -X PUT --data-binary @"$path" "$CCO_TOOLS/file?path=$(jq -rn --arg p "$path" '$p|@uri')"
    ;;
esac
exit 0
