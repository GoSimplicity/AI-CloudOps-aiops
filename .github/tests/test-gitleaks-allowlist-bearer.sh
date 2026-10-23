#!/usr/bin/env bash
# SEC-01A 知识库 Bearer 回归测试（第三轮 E，取代 review2 的 your-api-key 锚定测试）。
# 背景（review3c second-bearer.log）：curl-auth-header 一条 curl 只报第一个 Authorization 头；旧配置放行第一个头的
# your-api-key 后，同一条命令里后面的真 token 整条 0 命中（用例 3/8/9）。现配置：
#   (c) 知识库示例改用 ${AIOPS_API_KEY}，删除 your-api-key allowlist；
#   (b) 规则 kb-bearer-token：^data/knowledge_base/ 下任何 "Bearer <值>" 都报，只放行 secret 整体为 $AIOPS_API_KEY / ${AIOPS_API_KEY}。
#   review3f：放行范围扩为 secret 整体匹配 ^\$\{?[A-Z][A-Z0-9_]*_API_KEY\}?$（如 ${OPENAI_API_KEY}），畸形写法照常报。
# 判定：每个用例断言命中条数和“每条命中是哪条规则、secret 是哪个值”（与运行时生成的 token 逐一比对），不只看退出码。
# 输出只打印标签（TOKEN_A / TOKEN_B / JWT / 占位符 / 变量引用 / 其他(N字符)），不打印 token 本身。
# 用法: .github/tests/test-gitleaks-allowlist-bearer.sh [--rev <git-rev>]   （对 06d93b1 等修复前版本运行应报 FAIL）
#       bash --posix 同样可运行。
source "$(dirname "${BASH_SOURCE[0]}")/lib-gitleaks-workflow.sh"
gl_parse_args "$@"
gl_src .gitleaks.toml >"$GL_TMP/gitleaks.toml"

# 运行时拼接的合成值（不是真实凭据；源码中不出现完整的 Bearer 头）
B='Bear''er'
P='your-api''-key'
V='${AIOPS_API_KEY}'
# review3f 畸形变量引用（应照常报，用作 secret 标签）
M1='$OPENAI_API_KEY_x'
M2='${api_key}'
VX='${X_API_KEY}'
# 用 python secrets 生成（lib 的 gl_rand 是 tr|head 管道，在 pipefail + 变量赋值下会因 SIGPIPE 退出）
rnd() { python3 -c 'import secrets,string,sys;print("".join(secrets.choice(sys.argv[2]) for _ in range(int(sys.argv[1]))))' "$1" "${2:-$(printf '%s' 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789')}"; }
TA="$(rnd 40)"
TB="$(rnd 40)"
# JWT 形状：base64url(header).base64url(payload).签名（合成值，签名是随机串）
JWT="$(python3 -c '
import base64, json, secrets, time
e = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
print("%s.%s.%s" % (e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()),
                    e(json.dumps({"sub": secrets.token_hex(6), "iat": int(time.time())}).encode()),
                    e(secrets.token_bytes(32))))')"
KB=data/knowledge_base/deployment_configuration_guide.md
U=http://localhost:8080/api/v1/health

# scan <名称> <相对路径> <期望命中（"规则=标签" 以空格分隔，顺序无关；无命中写 ->）> <文件第 3 行起的内容>
scan() {
    local name="$1" path="$2" want="$3" body="$4"
    local d="$GL_TMP/s$RANDOM$RANDOM" rep="$GL_TMP/report-$RANDOM$RANDOM.json" got
    mkdir -p "$d/$(dirname "$path")"
    { echo '# guide'; echo '```bash'; printf '%s\n' "$body"; echo '```'; } >"$d/$path"
    # 在扫描根目录内以 "." 扫描，File 为相对路径（与规则 path ^data/knowledge_base/ 一致，同 CI 的 gitleaks git）。
    # 报告不打码（要与 token 比对），写在 $GL_TMP，本进程内解析成标签后立即删除；控制台只输出标签。
    (cd "$d" && env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML gitleaks dir --no-banner --exit-code 0 \
        --config "${SCAN_CFG:-$GL_TMP/gitleaks.toml}" --report-format json --report-path "$rep" . >/dev/null 2>&1) || true
    if [ ! -s "$rep" ]; then
        echo "FAIL [$name] gitleaks 未生成报告（扫描出错）"; GL_FAILS=$((GL_FAILS+1)); return 0
    fi
    got="$(GL_TA="$TA" GL_TB="$TB" GL_JWT="$JWT" GL_P="$P" GL_V="$V" GL_M1="$M1" GL_M2="$M2" GL_VX="$VX" python3 - "$rep" <<'PY'
import json, os, sys
r = json.load(open(sys.argv[1])) or []
lab = {os.environ["GL_TA"]: "TOKEN_A", os.environ["GL_TB"]: "TOKEN_B", os.environ["GL_JWT"]: "JWT",
       os.environ["GL_P"]: "占位符", os.environ["GL_V"]: "变量引用",
       os.environ["GL_M1"]: "畸形引用M1", os.environ["GL_M2"]: "畸形引用M2", os.environ["GL_VX"]: "X变量"}
def who(s):
    if s in lab: return lab[s]
    for k, v in lab.items():
        if k and s.startswith(k): return "%s+后缀(%d字符)" % (v, len(s) - len(k))
    return "其他(%d字符)" % len(s)
print(" ".join(sorted("%s=%s" % (f.get("RuleID", ""), who(f.get("Secret", ""))) for f in r)) or "-")
PY
)"
    rm -f "$rep"
    want="$(printf '%s\n' "$want" | python3 -c 'import sys;print(" ".join(sorted(sys.stdin.read().split())) or "-")')"
    local n=0; [ "$got" = "-" ] || n="$(printf '%s\n' "$got" | wc -w | tr -d ' ')"
    if [ "$got" = "$want" ]; then
        echo "PASS [$name] 命中 ${n} 条: ${got}"
    else
        echo "FAIL [$name] 期望: ${want} | 实际(${n} 条): ${got}"; GL_FAILS=$((GL_FAILS+1))
    fi
}

C=curl-auth-header
K=kb-bearer-token
# review3c 用例 3：同一行，先占位符、后真 token
scan "3 同一行 先 your-api-key 后真 token" "$KB" "$C=占位符 $K=占位符 $K=TOKEN_A" \
    "curl -H \"Authorization: $B $P\" -H \"Authorization: $B $TA\" $U"
# 用例 8：第二个头用 --header 写法
scan "8 同一行 --header 写第二个头" "$KB" "$C=占位符 $K=占位符 $K=TOKEN_A" \
    "curl -H \"Authorization: $B $P\" --header \"Authorization: $B $TA\" $U"
# 用例 9：多行 curl，两个头分在两行
scan "9 多行 curl 占位符与真 token 分两行" "$KB" "$C=占位符 $K=占位符 $K=TOKEN_A" \
    "curl -H \"Authorization: $B $P\" \\
  -H \"Authorization: $B $TA\" $U"
# 用例 10：非 curl 的 HTTP 头示例（默认规则不报，靠 kb-bearer-token）
scan "10 非 curl 同一行两个 Bearer" "$KB" "$K=占位符 $K=TOKEN_A" \
    "Authorization: $B $P  Authorization: $B $TA"
# 用例 14：两条 curl 命令
scan "14 两条 curl 命令（; 分隔）" "$KB" "$C=占位符 $C=TOKEN_A $K=占位符 $K=TOKEN_A" \
    "curl -H \"Authorization: $B $P\" http://a/x; curl -H \"Authorization: $B $TA\" http://a/y"
# 新占位符 ${AIOPS_API_KEY} 在前、真 token 在后：变量引用不报，真 token 两条规则都报
scan "占位符 \${AIOPS_API_KEY} 后跟真 token（同一条 curl）" "$KB" "$C=TOKEN_A $K=TOKEN_A" \
    "curl -H \"Authorization: $B $V\" -H \"Authorization: $B $TA\" $U"
scan "占位符 \${AIOPS_API_KEY} 后跟真 token（多行 curl）" "$KB" "$C=TOKEN_A $K=TOKEN_A" \
    "curl -H \"Authorization: $B $V\" \\
  -H \"Authorization: $B $TA\" $U"
# JWT 形状的 token：默认规则 jwt 也会报；另外 gitleaks 8.30.1 会把 base64 片段解码后再匹配，
# 解码出的 JWT header 以 "{" 开头，kb-bearer-token 因此多一条 secret 为 "{" 的附带命中（tags decoded:base64）。
J1="$K=其他(1字符)"
scan "JWT 形状 token（curl）" "$KB" "$C=JWT jwt=JWT $K=JWT $J1" \
    "curl -H \"Authorization: $B $JWT\" $U"
scan "JWT 形状 token（非 curl，小写 bearer）" "$KB" "jwt=JWT $K=JWT $J1" \
    "authorization: bearer $JWT"
# 两个不同真 token：kb-bearer-token 两个都报
scan "同一行两个不同真 token" "$KB" "$C=TOKEN_A $K=TOKEN_A $K=TOKEN_B" \
    "curl -H \"Authorization: $B $TA\" -H \"Authorization: $B $TB\" $U"
# allowlist 只放行整体等于变量引用的值
scan "对照 只有 \${AIOPS_API_KEY}" "$KB" "-" "curl -H \"Authorization: $B $V\" $U"
scan "对照 只有 \$AIOPS_API_KEY（非 curl）" "$KB" "-" "Authorization: $B \$AIOPS_API_KEY"
scan "\${AIOPS_API_KEY} 紧跟真 token 后缀" "$KB" "$K=变量引用+后缀(40字符)" \
    "curl -H \"Authorization: $B $V$TA\" $U"
scan "形似变量的其他名字不放行" "$KB" "$K=其他(18字符)" \
    "Authorization: $B \${AIOPS_API_KEY_2}"
# review3f：allowlist 改为 ^\$\{?[A-Z][A-Z0-9_]*_API_KEY\}?$，${OPENAI_API_KEY} 放行；畸形写法照常报
scan "\${OPENAI_API_KEY} 不报（curl）" "$KB" "-" "curl -H \"Authorization: $B \${OPENAI_API_KEY}\" https://api.openai.com/v1/models"
scan "\${OPENAI_API_KEY} 不报（非 curl）" "$KB" "-" "Authorization: $B \${OPENAI_API_KEY}"
# 注意：gitleaks 8.30.1 默认配置（useDefault）的全局 allowlist 会放行“$ 或 ${} 包住、只含字母和下划线、不含数字”的值
# （实测大小写混写也放行，如 $Abc_def），$OPENAI_API_KEY_x、${api_key} 因此在整套配置下 0 条——是默认全局 allowlist
# 放的，不是本规则的 allowlist。所以这两条分两步断言：
#   整套配置（与 CI 相同）0 条；只用本规则（[[rules]] 段 + 规则内 allowlist，不加载默认配置）则照常报，证明本规则不放行。
sed -n '/^\[\[rules\]\]/,$p' "$GL_TMP/gitleaks.toml" >"$GL_TMP/kb-only.toml"
scan "\$OPENAI_API_KEY_x 整套配置 0 条（默认全局 allowlist 放行）" "$KB" "-" "Authorization: $B $M1"
SCAN_CFG="$GL_TMP/kb-only.toml" scan "\$OPENAI_API_KEY_x 只用本规则照常报（结尾不是 _API_KEY）" "$KB" "$K=畸形引用M1" "Authorization: $B $M1"
scan "\${api_key} 整套配置 0 条（默认全局 allowlist 放行）" "$KB" "-" "Authorization: $B $M2"
SCAN_CFG="$GL_TMP/kb-only.toml" scan "\${api_key} 只用本规则照常报（小写）" "$KB" "$K=畸形引用M2" "Authorization: $B $M2"
scan "\${api_key2} 整套配置照常报（含数字，默认全局 allowlist 不放行）" "$KB" "$K=其他(11字符)" "Authorization: $B \${api_key2}"
SCAN_CFG="$GL_TMP/kb-only.toml" scan "\${OPENAI_API_KEY} 只用本规则也不报（本规则 allowlist 放行）" "$KB" "-" "Authorization: $B \${OPENAI_API_KEY}"
scan "\${X_API_KEY}abc 照常报（变量后带后缀）" "$KB" "$K=X变量+后缀(3字符)" "Authorization: $B ${VX}abc"
scan "your-api-key 单独出现也报（allowlist 已删除）" "$KB" "$C=占位符 $K=占位符" \
    "curl -H \"Authorization: $B $P\" $U"
# 路径范围：kb-bearer-token 只管知识库目录
scan "对照 知识库外 非 curl 不受 kb 规则约束" docs/other.md "-" \
    "Authorization: $B $P  Authorization: $B $TA"
scan "对照 知识库外 curl 仍按默认规则" docs/other.md "$C=占位符" \
    "curl -H \"Authorization: $B $P\" -H \"Authorization: $B $TA\" $U"
gl_finish
