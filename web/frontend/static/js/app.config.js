// ===== app.config.js —— 拆分自 app.js（配置管理：LLM配置 / 日志查看），原始行 L790-L1019 =====
// ============ 配置管理 ============

// 将模型输入切换为手填框，保证模型接口不可用时仍能保存自定义模型名。
function useManualModelInput(message = '') {
    const input = document.getElementById('llm-model');
    const select = document.getElementById('llm-model-select');
    const status = document.getElementById('llm-model-status');
    if (input) input.style.display = '';
    if (select) select.style.display = 'none';
    if (status) status.textContent = message;
}

// 将后端返回的模型名称填充到下拉框，并同步当前选择值。
function useModelSelect(models, currentModel = '') {
    const input = document.getElementById('llm-model');
    const select = document.getElementById('llm-model-select');
    const status = document.getElementById('llm-model-status');
    if (!input || !select || !Array.isArray(models) || models.length === 0) {
        useManualModelInput('未获取到模型，当前支持手动填写。');
        return;
    }
    select.innerHTML = models.map(model =>
        `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`
    ).join('');
    // 当前配置不在服务列表中时仍保留它，避免打开页面意外改动配置。
    if (currentModel && !models.includes(currentModel)) {
        select.insertAdjacentHTML('afterbegin', `<option value="${escapeHtml(currentModel)}">${escapeHtml(currentModel)}（当前配置）</option>`);
    }
    select.value = currentModel || models[0];
    input.value = select.value;
    input.style.display = 'none';
    select.style.display = '';
    status.textContent = `已加载 ${models.length} 个模型`;
    select.onchange = () => { input.value = select.value; };
}

// 根据当前 Base URL 与 API Key 请求模型列表；失败只降级，不阻断配置页。
async function loadLLMModels() {
    const baseUrl = document.getElementById('llm-base-url')?.value.trim();
    const apiKey = document.getElementById('llm-api-key')?.value.trim();
    const status = document.getElementById('llm-model-status');
    if (status) status.textContent = '正在获取模型列表...';
    try {
        const data = await apiRequest('/config/llm/models', {
            method: 'POST',
            body: JSON.stringify({ base_url: baseUrl, api_key: apiKey })
        });
        if (data.success && data.models?.length) {
            useModelSelect(data.models, document.getElementById('llm-model')?.value.trim());
        } else {
            useManualModelInput(data.message || '模型列表获取失败，当前支持手动填写。');
        }
    } catch (error) {
        // apiRequest 已记录错误；这里仅恢复可编辑输入框，避免页面卡死。
        useManualModelInput(`模型列表获取失败，当前支持手动填写：${error.message}`);
    }
}

// 加载已保存的 AI 大模型配置，再自动探测模型列表。
async function loadLLMConfig() {
    try {
        const data = await apiRequest('/config/llm');
        const config = data.config || {};
        document.getElementById('llm-base-url').value = config.api_base || 'https://api.openai.com/v1';
        document.getElementById('llm-model').value = config.model || 'gpt-3.5-turbo';
        await loadLLMModels();
    } catch (error) {
        console.error('加载 AI 大模型配置失败:', error);
        useManualModelInput('配置读取失败，当前支持手动填写。');
    }
}

// Base URL 失焦时刷新模型，减少输入过程中的无效请求。
document.getElementById('llm-base-url')?.addEventListener('change', loadLLMModels);

async function saveLLMConfig() {
    const apiKey = document.getElementById('llm-api-key').value;
    const baseUrl = document.getElementById('llm-base-url').value;
    const model = document.getElementById('llm-model').value;
    
    if (!apiKey) {
        showAppAlert('请输入API Key');
        return;
    }
    
    try {
        await apiRequest('/config/llm', {
            method: 'POST',
            body: JSON.stringify({
                api_key: apiKey,
                base_url: baseUrl,
                model: model
            })
        });
        
        showAppAlert('配置保存成功！');
    } catch (error) {
        showAppAlert(`保存失败: ${error.message}`);
    }
}

async function loadLLMUsage() {
    showLoading('llm-usage');
    
    try {
        const data = await apiRequest('/config/llm/usage?days=7');
        
        const usage = data.daily_usage.map(day => `
            <p>${day.date}: ${day.total_tokens} tokens (${day.request_count} 次请求)</p>
        `).join('');
        
        document.getElementById('llm-usage').innerHTML = `
            <h4>最近7天用量</h4>
            <p>总Token: ${data.total_tokens} | 总请求: ${data.total_requests}</p>
            ${usage}
        `;
    } catch (error) {
        document.getElementById('llm-usage').innerHTML = `<p style="color: red;">加载失败: ${escapeHtml(error.message)}</p>`;
    }
}

// ============ 日志查看 ============

let logCursor = null;
let logPollTimer = null;
let logRecords = [];
let logQueryKey = '';

/** 获取当前日志筛选级别，默认展示四个用户可筛选级别。 */
function getSelectedLogLevels() {
    return [...document.querySelectorAll('.log-level-filters input:checked')]
        .map(input => input.value).join(',');
}

/** 生成稳定的日志文本，供复制和导出使用。 */
function formatLogRecord(record) {
    return `[${record.timestamp}] [${record.level}] [${record.source}] ${record.message}`;
}

/** 按级别标签渲染结构化日志，所有字段均走转义。 */
function renderLogs() {
    const container = document.getElementById('logs-content');
    const count = document.getElementById('logs-count');
    if (!container) return;
    if (!logRecords.length) {
        container.innerHTML = '<div class="logs-empty">暂无匹配日志，等待新记录...</div>';
    } else {
        container.innerHTML = logRecords.map((record, index) => `
            <article class="log-entry log-${escapeHtml(record.level.toLowerCase())}" data-log-index="${index}">
                <time class="log-timestamp">${escapeHtml(record.timestamp)}</time>
                <span class="log-level-chip level-${escapeHtml(record.level.toLowerCase())}">${escapeHtml(record.level)}</span>
                <span class="log-source">${escapeHtml(record.source)}</span>
                <pre class="log-message">${escapeHtml(record.message)}</pre>
                <button class="log-copy-button" type="button" title="复制日志" onclick="copyLogRecord(${index})">复制</button>
            </article>`).join('');
    }
    if (count) count.textContent = `${logRecords.length} 条`;
    container.scrollTop = container.scrollHeight;
}

/** 拉取一次日志；reset=true 时重新建立文件偏移游标。 */
async function loadLogs(reset = false) {
    const logType = document.getElementById('log-type').value;
    const keyword = document.getElementById('log-keyword')?.value.trim() || '';
    const status = document.getElementById('logs-status');
    const queryKey = `${logType}|${getSelectedLogLevels()}|${keyword}`;
    if (logQueryKey !== queryKey) {
        reset = true;
        logQueryKey = queryKey;
    }
    if (reset) {
        logCursor = null;
        logRecords = [];
    }
    const query = new URLSearchParams({
        log_type: logType,
        levels: getSelectedLogLevels() || 'DEBUG,INFO,WARNING,ERROR',
        limit: '200'
    });
    if (keyword) query.set('keyword', keyword);
    if (logCursor) query.set('cursor', logCursor);
    try {
        const data = await apiRequest(`/logs/?${query.toString()}`);
        logCursor = data.cursor || logCursor;
        if (data.records?.length) {
            const existing = new Set(logRecords.map(formatLogRecord));
            logRecords.push(...data.records.filter(record => !existing.has(formatLogRecord(record))));
            logRecords = logRecords.slice(-500);
        }
        renderLogs();
        if (status) status.textContent = '实时轮询中 · 每 2 秒更新';
    } catch (error) {
        if (status) status.textContent = `连接异常 · ${error.message}`;
        renderLogs();
    }
}

/** 启动日志增量轮询，避免重复创建定时器。 */
function startLogPolling() {
    if (logPollTimer) return;
    loadLogs(true);
    logPollTimer = window.setInterval(() => loadLogs(false), 2000);
}

/** 复制单条结构化日志，兼容不支持 Clipboard API 的浏览器。 */
async function copyLogRecord(index) {
    const text = formatLogRecord(logRecords[index]);
    try {
        await navigator.clipboard.writeText(text);
    } catch (error) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        textarea.remove();
    }
}

/** 下载当前筛选出的 WARNING/ERROR 日志文本。 */
function exportProblemLogs() {
    const records = logRecords.filter(record => ['WARNING', 'ERROR', 'CRITICAL'].includes(record.level));
    const body = records.length ? records.map(formatLogRecord).join('\n') : '暂无 WARNING / ERROR 日志';
    const blob = new Blob([body], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `bili_ops_errors_${new Date().toISOString().slice(0, 19).replaceAll(':', '-')}.txt`;
    link.click();
    URL.revokeObjectURL(link.href);
}

document.addEventListener('change', event => {
    if (event.target.closest('.log-level-filters') || event.target.id === 'log-type') loadLogs(true);
});
