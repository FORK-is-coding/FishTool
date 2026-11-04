"""配置页“每日 token 上限”前端契约测试（app.config.js / index.html）。

前端没有 JS 测试框架，按仓库惯例：
1. 静态断言：输入框与“保存上限”按钮在 AI 配置卡片中就位。
2. 有 node 时用 vm 加载真实 app.config.js，验证：
   - 保存上限走 PUT /api/config/，请求体是 {key: 'llm.daily_token_limit', value: <整数>}，
     不要求填写 API Key；
   - 非法输入（空、负数、小数）不发请求，只提示；
   - 加载配置时回显当前上限；
   - 用量区显示“今日已用 / 上限”，上限为 0 时显示“不限制”。
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


def test_daily_limit_input_and_button_present() -> None:
    """AI 配置卡片里有上限输入框（非负整数）和独立的保存按钮。"""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    card = html.split('<section class="section ai-config-card">', 1)[1].split("</section>", 1)[0]
    input_tag = card.split('id="llm-daily-limit"', 1)[0].rsplit("<input", 1)[1] + card.split(
        'id="llm-daily-limit"', 1)[1].split(">", 1)[0]
    assert 'type="number"' in input_tag
    assert 'min="0"' in input_tag
    assert 'onclick="saveLLMDailyLimit()"' in card
    assert "0 表示不限制" in card


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
    escapeHtml: s => String(s).replace(/[&<>"']/g, c => '&#' + c.charCodeAt(0) + ';'),
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
    getEl('llm-daily-limit').value = scenario.input;
    await ctx.saveLLMDailyLimit();
  } else if (scenario.action === 'load') {
    await ctx.loadLLMConfig();
    out.limitValue = getEl('llm-daily-limit').value;
  } else if (scenario.action === 'usage') {
    await ctx.loadLLMUsage();
    out.usageHtml = getEl('llm-usage').innerHTML;
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


@pytest.mark.parametrize("raw, expected", [("0", 0), ("2000000", 2000000), (" 150000 ", 150000)])
def test_save_daily_limit_puts_integer_without_api_key(raw, expected) -> None:
    out = _run({"action": "save", "input": raw})
    assert out["calls"] == [{"url": "/config/", "method": "PUT",
                             "body": {"key": "llm.daily_token_limit", "value": expected}}]
    assert len(out["alerts"]) == 1


@pytest.mark.parametrize("raw", ["", "-1", "1.5", "abc"])
def test_save_daily_limit_rejects_invalid_input_locally(raw) -> None:
    out = _run({"action": "save", "input": raw})
    assert out["calls"] == []
    assert out["alerts"] and "不小于 0 的整数" in out["alerts"][0]


def test_load_config_echoes_daily_limit() -> None:
    out = _run({"action": "load", "responses": {
        "/config/llm": {"success": True, "config": {"api_base": "https://x/v1", "model": "m",
                                                     "daily_token_limit": 2000000}},
        "/config/llm/models": {"success": False},
    }})
    assert out["limitValue"] == "2000000"


@pytest.mark.parametrize("today, expected", [
    ({"total_tokens": 150, "limit": 1000, "remaining": 850}, "今日已用: 150 / 上限: 1000"),
    ({"total_tokens": 150, "limit": 0, "remaining": None}, "今日已用: 150 / 上限: 不限制"),
])
def test_usage_shows_today_against_limit(today, expected) -> None:
    out = _run({"action": "usage", "responses": {
        "/config/llm/usage": {"success": True, "daily_usage": [], "total_tokens": 0,
                              "total_requests": 0, "today": today},
    }})
    assert expected in out["usageHtml"]


def test_usage_without_today_field_still_renders() -> None:
    """后端旧版本没有 today 字段时，用量区照常渲染，不报错。"""
    out = _run({"action": "usage", "responses": {
        "/config/llm/usage": {"success": True, "daily_usage": [], "total_tokens": 7,
                              "total_requests": 1},
    }})
    assert "总Token: 7" in out["usageHtml"]
    assert "今日已用" not in out["usageHtml"]
