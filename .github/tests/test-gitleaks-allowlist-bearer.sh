#!/usr/bin/env bash
# SEC-01A 审查 P3（aiops）：.gitleaks.toml 中 "Bearer your-api-key" 占位符 allowlist 必须精确匹配，
# 不能放行 "your-api-key" 后面紧跟真实 token 的写法（旧正则未锚定结尾，regexTarget=match 时任何后缀都被放行）。
# 用法: .github/tests/test-gitleaks-allowlist-bearer.sh [--rev <git-rev>]   （对修复前版本运行应报 FAIL）
source "$(dirname "${BASH_SOURCE[0]}")/lib-gitleaks-workflow.sh"
gl_parse_args "$@"
gl_src .gitleaks.toml >"$GL_TMP/gitleaks.toml"

# 运行时拼接 curl 行，避免本脚本自身命中 curl-auth-header 规则
H='Authorization: Bearer'
line() { printf 'curl -H "%s %s" \\\n  http://localhost:8080/api/v1/health\n' "$H" "$1"; }
scan() { # scan <名称> <期望: allow|detect> <bearer 值> [路径]
    local name="$1" want="$2" val="$3" path="${4:-data/knowledge_base/deployment_configuration_guide.md}"
    local d="$GL_TMP/s$RANDOM$RANDOM"; mkdir -p "$d/$(dirname "$path")"
    { echo '# guide'; echo '```bash'; line "$val"; echo '```'; } >"$d/$path"
    local n
    # 在扫描根目录内以 "." 扫描，使 File 为相对路径，与 allowlist 的 ^data/... 路径条件一致（同 CI 行为）
    n="$(cd "$d" && gitleaks dir --no-banner --redact --exit-code 0 --config "$GL_TMP/gitleaks.toml" \
        --report-format json --report-path /dev/stdout . 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
    if { [ "$want" = allow ] && [ "$n" -eq 0 ]; } || { [ "$want" = detect ] && [ "$n" -gt 0 ]; }; then
        echo "PASS [$name] 期望 $want，命中 $n 条"
    else
        echo "FAIL [$name] 期望 $want，命中 $n 条"; GL_FAILS=$((GL_FAILS+1))
    fi
}
# 合成随机值（运行时生成，不含任何真实密钥）
rnd() { python3 -c 'import secrets,string,sys;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(int(sys.argv[1]))))' "$1"; }
SUFFIX="$(rnd 32)"
scan "占位符 your-api-key 不误报" allow "your-api-key"
scan "your-api-key 紧跟真实 token 后缀" detect "your-api-key${SUFFIX}"
scan "your-api-key- 加真实 token 后缀" detect "your-api-key-${SUFFIX}"
scan "对照 文档目录中的真实 token" detect "$(rnd 40)"
scan "对照 文档目录外的占位符仍按规则处理" detect "your-api-key${SUFFIX}" "docs/other.md"
gl_finish
