"""配置页“单次输出上限”前端契约测试（app.config.js / index.html）。

做法与 test_frontend_llm_daily_limit.py 相同（前端没有 JS 测试框架）：
1. 静态断言：输入框与“保存输出上限”按钮在 AI 配置卡片中就位。
2. 有 node 时用 vm 加载真实 app.config.js，验证：
   - 保存走 PUT /api/config/，请求体是 {key: 'llm.max_tokens', value: <正整数>}，不要求填写 API Key；
   - 非法输入（空、0、负数、小数、非数字）不发请求，只提示；
   - 加载配置时回显当前的 llm.max_tokens。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend"
_CONFIG_JS = _FRONTEND_DIR / "static" / "js" / "app.config.js"
_INDEX_HTML = _FRONTEND_DIR / "templates" / "index.html"


def test_max_tokens_input_and_button_present() -> None:
    """AI 配置卡片里有输出上限输入框（正整数）和独立的保存按钮，并说明思考模式也计入。"""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    card = html.split('<section class="section ai-config-card">', 1)[1].split("</section>", 1)[0]
    before, after = card.split('id="llm-max-tokens"', 1)
    input_tag = before.rsplit("<input", 1)[1] + after.split(">", 1)[0]
    assert 'type="number"' in input_tag
    assert 'min="1"' in input_tag
    assert 'onclick="saveLLMMaxTokens()"' in card
    assert "思考" in card


_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = JSON.parse(process.argv[2]);

function el(id) {
  return { id, value: '', innerHTML: '', style: {}, addEventListener() {} };
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
  if (scenario.action === 'save') {
    getEl('llm-max-tokens').value = scenario.input;
    await ctx.saveLLMMaxTokens();
  } else if (scenario.action === 'load') {
    await ctx.loadLLMConfig();
    out.maxTokensValue = getEl('llm-max-tokens').value;
  }
  process.stdout.write(JSON.stringify(out));
}
main().catch(err => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
"""


def _run(scenario: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用，跳过 vm 行为测试")
    proc = subprocess.run(
        [node, "-e", _NODE_SCRIPT, str(_CONFIG_JS), json.dumps(scenario, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("raw, expected", [("1", 1), ("2000", 2000), (" 8192 ", 8192)])
def test_save_max_tokens_puts_integer_without_api_key(raw, expected) -> None:
    out = _run({"action": "save", "input": raw})
    assert out["calls"] == [{"url": "/config/", "method": "PUT",
                             "body": {"key": "llm.max_tokens", "value": expected}}]
    assert len(out["alerts"]) == 1


@pytest.mark.parametrize("raw", ["", "0", "-1", "1.5", "abc"])
def test_save_max_tokens_rejects_invalid_input_locally(raw) -> None:
    out = _run({"action": "save", "input": raw})
    assert out["calls"] == []
    assert out["alerts"] and "大于 0 的整数" in out["alerts"][0]


def test_load_config_echoes_max_tokens() -> None:
    out = _run({"action": "load", "responses": {
        "/config/llm": {"success": True, "config": {"api_base": "https://x/v1", "model": "m",
                                                     "max_tokens": 4096}},
        "/config/llm/models": {"success": False},
    }})
    assert out["maxTokensValue"] == "4096"
