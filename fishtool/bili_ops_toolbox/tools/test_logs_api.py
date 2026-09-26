"""logs API 回归测试脚本。
用法: python tools/test_logs_api.py [--base-url http://127.0.0.1:8000]
验证 /api/logs 各端点行为，改 logs.py 前后各跑一次做对比。
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
if "--base-url" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base-url") + 1]

PASS = 0
FAIL = 0


def req(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} :: {detail}")


# 1. 默认日志查询
st, hd, body = req("/api/logs/?log_type=all&limit=50")
data = json.loads(body)
check("GET /logs/ 默认 200", st == 200, f"status={st}")
check("GET /logs/ success=True", data.get("success") is True, f"data={data}")
check("GET /logs/ records 是列表", isinstance(data.get("records"), list), f"type={type(data.get('records'))}")
check("GET /logs/ 返回 cursor", isinstance(data.get("cursor"), str), f"cursor={data.get('cursor')}")

# 2. 单类型查询
st, _, body = req("/api/logs/?log_type=error&limit=50")
data = json.loads(body)
check("GET /logs/ error 类型 200", st == 200, f"status={st}")
if data.get("records"):
    check("GET /logs/ error 记录均含 level", all("level" in r for r in data["records"]), f"records={len(data['records'])}")

# 3. 非法类型 -> 400
st, _, body = req("/api/logs/?log_type=badtype&limit=10")
check("GET /logs/ 非法类型 400", st == 400, f"status={st}")

# 4. 导出（默认问题日志模式）
st, hd, body = req("/api/logs/export")
check("GET /export 默认 200", st == 200, f"status={st}")
ctype = hd.get("Content-Type", "") or hd.get("content-type", "")
check("GET /export text/plain", "text/plain" in ctype, f"ctype={ctype}")
disp = hd.get("Content-Disposition", "") or hd.get("content-disposition", "")
check("GET /export 附件头", "attachment" in disp and ".txt" in disp, f"disp={disp}")
body_text = body.decode("utf-8", errors="replace")
check("GET /export 非空", len(body_text) > 0, f"len={len(body_text)}")

# 5. 全量导出（full 模式，改代码后新增）
st, hd, body = req("/api/logs/export?full=true")
data_txt = body.decode("utf-8", errors="replace")
check("GET /export full=true 200", st == 200, f"status={st}")
check("GET /export full=true 含分隔标题", "日志类型" in data_txt or "====" in data_txt, f"head={data_txt[:120]!r}")

# 6. 类型列表
st, _, body = req("/api/logs/types")
data = json.loads(body)
check("GET /types 200", st == 200, f"status={st}")
check("GET /types log_types 列表", isinstance(data.get("log_types"), list), f"data={data}")

print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
