"""配置页模型列表探测的前端契约测试（app.config.js，#87）。

前端没有 JS 测试框架，沿用 test_frontend_llm_max_tokens.py 的做法：
1. 静态断言：源码里用条件展开决定是否带 api_key 字段；
2. 有 node 时用 vm 加载真实 app.config.js 验证行为：
   - 页面加载（密钥输入框为空）时 POST /config/llm/models 的请求体不含 api_key；
   - 填了密钥时请求体带上该密钥；
   - 保存路径（saveLLMConfig）在密钥为空时仍然不发请求。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend"
_CONFIG_JS = _FRONTEND_DIR / "static" / "js" / "app.config.js"


def test_models_request_omits_blank_api_key_in_source() -> None:
    """源码必须按密钥是否为空构造请求体，而不是无条件带上 api_key: ""。"""
    src = _CONFIG_JS.read_text(encoding="utf-8")

    assert "apiKey ? { base_url: baseUrl, api_key: apiKey } : { base_url: baseUrl }" in src
    # 旧的“无条件带空串”写法不允许复活
    assert "JSON.stringify({ base_url: baseUrl, api_key: apiKey })" not in src


_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = JSON.parse(process.argv[2]);

function el(id) {
  return { id, value: scenario.values && scenario.values[id] || '', innerHTML: '',
           style: {}, addEventListener() {} };
}

async function main() {
  const elements = {};
  const getEl = id => (elements[id] = elements[id] || el(id));
  const calls = [];
  const alerts = [];
  const ctx = {
    console: { error() {}, log() {}, warn() {} },
    document: { getElementById: getEl, addEventListener() {}, querySelector: () => null,
                querySelectorAll: () => [] },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    showAppAlert: msg => alerts.push(msg),
    showLoading() {},
    escapeHtml: s => String(s),
    apiRequest: async (url, options) => {
      calls.push({ url, method: (options && options.method) || 'GET',
                   body: options && options.body ? JSON.parse(options.body) : null });
      const route = Object.keys(scenario.responses || {}).find(k => url.indexOf(k) === 0);
      return route ? scenario.responses[route] : { success: true };
    },
    JSON, Number, String, Object, Array, Promise, Math, Error,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(src, ctx, { filename: 'app.config.js' });

  const out = { calls, alerts };
  if (scenario.action === 'load') {
    await ctx.loadLLMConfig();
  } else if (scenario.action === 'save') {
    await ctx.saveLLMConfig();
  } else if (scenario.action === 'models') {
    await ctx.loadLLMModels();
  }
  out.modelsCalls = calls.filter(c => c.url === '/config/llm/models');
  process.stdout.write(JSON.stringify(out));
}
main().catch(err => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
"""


def _run(scenario: dict) -> dict:
    """在 node 的 vm 里跑真实 app.config.js；无 node 时跳过。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用，跳过 vm 行为测试")
    proc = subprocess.run(
        [node, "-e", _NODE_SCRIPT, str(_CONFIG_JS), json.dumps(scenario, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_LOAD_RESPONSES = {
    "/config/llm": {"success": True, "config": {"api_base": "http://127.0.0.1:11434/v1",
                                                "model": "qwen2"}},
    "/config/llm/models": {"success": True, "models": ["qwen2"]},
}


def test_page_load_omits_api_key_field() -> None:
    """页面加载时密钥输入框为空：请求体只有 base_url，后端据此回退已保存密钥。"""
    out = _run({"action": "load", "responses": _LOAD_RESPONSES})

    assert out["modelsCalls"], "页面加载应触发一次模型列表探测"
    body = out["modelsCalls"][0]["body"]
    assert body == {"base_url": "http://127.0.0.1:11434/v1"}
    assert "api_key" not in body


def test_models_request_includes_typed_api_key() -> None:
    """用户填了密钥时请求体必须带上它（切换地址的场景）。"""
    out = _run({
        "action": "models",
        "values": {"llm-base-url": "https://api.new/v1", "llm-api-key": " sk-typed "},
        "responses": {"/config/llm/models": {"success": True, "models": ["m"]}},
    })

    assert out["modelsCalls"][0]["body"] == {"base_url": "https://api.new/v1",
                                             "api_key": "sk-typed"}


def test_blank_key_after_load_triggers_single_models_request() -> None:
    """密钥留空时页面只发一次探测请求，不会因空串被后端拒绝而反复重试。"""
    out = _run({"action": "load", "values": {"llm-api-key": "   "}, "responses": _LOAD_RESPONSES})

    assert len(out["modelsCalls"]) == 1
    assert "api_key" not in out["modelsCalls"][0]["body"]


def test_save_with_blank_key_still_blocked_locally() -> None:
    """#87 的改动不能放宽保存路径：空密钥保存仍然本地拦截、不发请求。"""
    out = _run({
        "action": "save",
        "values": {"llm-api-key": "", "llm-base-url": "https://x/v1", "llm-model": "m"},
    })

    assert out["calls"] == []
    assert out["alerts"] and "API Key" in out["alerts"][0]
