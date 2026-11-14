"""前端 apiRequest 请求头契约：默认 Content-Type 不能被调用方的 headers 覆盖。

回归背景（既有 bug，不是连接池改动引入的）：原实现是

    fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options
    })

``...options`` 展开在 ``headers`` 之后，调用方只要传了 ``options.headers``，整个
headers 对象就会被替换掉，默认的 ``Content-Type`` 随之丢失。字符串 body 没有
Content-Type 时浏览器会补 ``text/plain;charset=UTF-8``，FastAPI 对 pydantic body
直接返回 422，于是「01 正确排名」的两个写接口一直失败：

- ``POST /api/analysis/benchmark/candidates``（app.up.js，body 是 CandidateDiscoveryRequest）
- ``POST /api/analysis/benchmark/tasks``（同上）

覆盖两层：

- 静态契约：旧覆盖式写法必须消失（不绑定具体实现字符串）。
- 行为契约（本机有 node 时）：用 vm 加载真实 ``app.core.js``，假 fetch 记录实际
  init，逐组合断言最终请求头（合并后键名统一小写）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_CORE_JS = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js" / "app.core.js"

_JSON_HEADER = {"content-type": "application/json"}


def _api_request_source() -> str:
    """截取 ``apiRequest`` 函数体，避免断言误命中文件其它位置。"""
    js = _CORE_JS.read_text(encoding="utf-8")
    start = js.index("async function apiRequest(")
    end = js.index("\nfunction ", start)
    return js[start:end]


# ===========================================================================
# 静态契约（无 node 也必跑）
# ===========================================================================


def test_api_request_no_longer_spreads_options_over_headers():
    """旧覆盖式写法必须消失，且仍保留调用方优先的合并逻辑。"""
    body = _api_request_source()
    # 旧形态：headers 对象里再展开 options.headers（随后被 {...options} 整体覆盖）。
    assert "...options.headers" not in body
    # 合并仍然读取调用方传入的头，并把结果交给 fetch。
    assert "options.headers" in body
    assert "{ headers: mergedHeaders }" in body


def test_api_request_skips_content_type_for_form_data():
    """FormData 的 Content-Type（含 boundary）由浏览器生成，不能预设。"""
    assert "instanceof FormData" in _api_request_source()


# ===========================================================================
# 行为契约（需要本机 node；无 node 时 skip）
# ===========================================================================

_NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [corePath, casesPath] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));

// 沙箱里的 FormData 替身：只用于触发 apiRequest 的 FormData 分支。
class FakeFormData {}

// 在宿主机上把 HeadersInit 的其它合法形态造出来（沙箱里不提供 Headers 全局）。
function buildHeaders(spec, kind) {
    if (!spec) return undefined;
    if (kind === 'Headers') return new Headers(spec);
    if (kind === 'array') return Object.entries(spec);
    return spec;
}

// 统一转成 { 小写键: 值 }，Headers 实例与普通对象都能处理。
function headersToObject(h) {
    const out = {};
    if (!h) return out;
    if (typeof h.forEach === 'function') {
        h.forEach((value, key) => { out[String(key).toLowerCase()] = value; });
        return out;
    }
    Object.keys(h).forEach(key => { out[String(key).toLowerCase()] = h[key]; });
    return out;
}

function makeContext() {
    const calls = [];
    const ctx = {
        console: { error() {}, log() {}, warn() {} },
        document: { querySelectorAll: () => [], getElementById: () => null },
        window: { requestAnimationFrame() {} },
        sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        crypto: { randomUUID: () => 'fixed-uuid' },
        showAppAlert() {},
        showToast() {},
        escapeHtml: value => String(value ?? ''),
        FormData: FakeFormData,
        fetch: async (url, init) => {
            calls.push({ url, init });
            return { ok: true, status: 200, json: async () => ({ success: true }) };
        },
    };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(corePath, 'utf8'), ctx, { filename: 'app.core.js' });
    return { ctx, calls };
}

(async () => {
    const results = [];
    for (const c of cases) {
        const { ctx, calls } = makeContext();
        const options = { method: c.method || 'GET' };
        const headers = buildHeaders(c.headers, c.headersKind);
        if (headers !== undefined) options.headers = headers;
        if (c.form) options.body = new FakeFormData();
        else if (c.body !== undefined) options.body = c.body;
        await ctx.apiRequest(c.path || '/probe', options);
        const init = calls.length ? calls[0].init : {};
        results.push({
            name: c.name,
            url: calls.length ? calls[0].url : null,
            method: init.method,
            body: typeof init.body === 'string' ? init.body : (init.body ? '__object__' : '__none__'),
            headers: headersToObject(init.headers),
        });
    }
    process.stdout.write(JSON.stringify(results));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _run_cases(tmp_path: Path, cases: list[dict]) -> dict[str, dict]:
    """在 node vm 里执行用例，返回 ``name -> 实际请求``。"""
    harness = tmp_path / "headers_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    cases_file = tmp_path / "headers_cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [_NODE, str(harness), str(_CORE_JS), str(cases_file)],
        capture_output=True,
        timeout=60,
    )
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, stderr
    return {item["name"]: item for item in json.loads(proc.stdout.decode("utf-8"))}


@pytest.mark.skipif(_NODE is None, reason="本机无 node，跳过 JS 行为测试（静态契约仍覆盖）")
def test_api_request_header_behavior_under_node(tmp_path):
    """调用方传 headers 的几种真实形态：纯对象 / 大小写 / Headers 实例 / 数组 / FormData。"""
    body = '{"rid": 4}'
    token = "tok-value"
    results = _run_cases(tmp_path, [
        # 排行页的真实形态：只带 X-Local-Token，body 是 JSON 字符串。
        {"name": "token_only", "method": "POST", "body": body, "headers": {"X-Local-Token": token}},
        {"name": "no_headers", "method": "POST", "body": body},
        {"name": "explicit_override", "method": "POST", "body": "a,b",
         "headers": {"Content-Type": "text/csv"}},
        # 大小写不同也算同一个头：不能拼成 "application/json, text/csv"。
        {"name": "lowercase_override", "method": "POST", "body": "a,b",
         "headers": {"content-type": "text/csv"}},
        # HeadersInit 的其它合法形态不能被改坏（改动前 options.headers 是原样交给 fetch 的）。
        {"name": "headers_instance", "method": "POST", "body": body,
         "headersKind": "Headers", "headers": {"X-Local-Token": token}},
        {"name": "headers_array", "method": "POST", "body": body,
         "headersKind": "array", "headers": {"X-Local-Token": token}},
        {"name": "form_data", "method": "POST", "form": True,
         "headers": {"X-Local-Token": token}},
        # 旧实现下 GET 带 headers 同样会丢默认头；现在补齐（新增保证，不是保持原样）。
        {"name": "get_with_headers", "method": "GET", "headers": {"X-Local-Token": token}},
    ])

    # 关键回归：带 token 头时默认 Content-Type 必须仍在。
    assert results["token_only"]["headers"] == {
        "content-type": "application/json", "x-local-token": token}
    assert results["token_only"]["method"] == "POST"
    assert results["token_only"]["body"] == body
    assert results["token_only"]["url"].endswith("/probe")

    # 不传 headers 时行为与改动前一致。
    assert results["no_headers"]["headers"] == _JSON_HEADER

    # 调用方显式指定 Content-Type 时以调用方为准（大小写都算同一个头）。
    assert results["explicit_override"]["headers"] == {"content-type": "text/csv"}
    assert results["lowercase_override"]["headers"] == {"content-type": "text/csv"}

    # Headers 实例 / 数组形态：既不丢默认头，也不丢调用方的头。
    for name in ("headers_instance", "headers_array"):
        assert results[name]["headers"] == {
            "content-type": "application/json", "x-local-token": token}, name

    # FormData：不预设 Content-Type，其它头照常保留。
    form_headers = results["form_data"]["headers"]
    assert "content-type" not in form_headers
    assert form_headers == {"x-local-token": token}

    # GET 带 headers：现在也会补齐默认 JSON 头（旧实现会丢）。
    assert results["get_with_headers"]["headers"] == {
        "content-type": "application/json", "x-local-token": token}
