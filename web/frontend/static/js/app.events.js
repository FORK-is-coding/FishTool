// ===== app.events.js —— FishTool 04 · 第三批 h（§15.1/15.2/15.3 完整版）=====
// 事件工作台 / 机会列表 / 证据回看 / 反馈。仅调用 /api/hotspot 既有与新增端点。
//
// 硬口径（对齐 §15 + §11.7 D + 规格 §3.4）：
// - 生成走 200/202：202 绝不能被当成“已生成”；用**专用** pollTopicGeneration 轮询生成账本，
//   终态（completed/failed/cancelled/interrupted）必须结束轮询；不改动通用 pollTask 契约。
// - 刷新后从 sessionStorage 的键恢复请求；同键内容冲突提示“新操作”，不偷偷换键重发。
// - 覆盖不足要显示**缺哪几个窗口**与预计下次数据到达，不只一个灰色“数据不足”。
// - 每个数值可展开到来源 BVID 和窗口；**不显示无法溯源的综合热度百分数**。
// - 不依赖颜色唯一表达（同时给文字状态）；禁止话术一律不出现。
// - 客户端不自填 phase / 指标 / 已验证 deadline；只生成/保存选题并记录反馈，**不自动发布**。
// - 外部数值缺字段时如实标“未提供”，不臆造；旧 assessment 明确显示历史时间。

const EVENT_GEN_STORAGE_PREFIX = 'hotspot.eventGeneration.';

// 会话内最近一次生成保存的 Topic id（反馈据此带上合法 topic_id；无则为空）。
let __eventSavedTopicIds = [];
// 当前机会 run（供反馈 expected_revision 使用）。
let __eventCurrentRun = null;

// ---------------------------------------------------------------------------
// 通用小工具
// ---------------------------------------------------------------------------

// 从接口错误里提取稳定 error_code（detail 可能是对象 / 字符串 / pydantic 数组）。
function eventApiErrorCode(error) {
    const detail = error && error.detail;
    if (detail && typeof detail === 'object' && !Array.isArray(detail) && detail.error_code) {
        return String(detail.error_code);
    }
    return '';
}

// epoch 秒 → 本地时间字符串；非法返回空（不伪造时间）。
function eventEpochText(value) {
    if (typeof value !== 'number' || !isFinite(value) || value <= 0) return '';
    try {
        return new Date(value * 1000).toLocaleString();
    } catch (error) {
        return '';
    }
}

// 规避颜色唯一表达：状态词 + 等级标记一起给。
function eventStatusBadge(status) {
    const label = {
        active: '进行中', paused: '已暂停', draft: '草稿', archived: '已归档',
        running: '生成中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
        complete: '数据完整', partial: '部分数据', collecting: '采集中', insufficient: '数据不足', stale: '数据过期',
    }[status] || String(status || '未知');
    return `<span class="event-status event-status-${escapeHtml(String(status || 'unknown'))}">[${escapeHtml(label)}]</span>`;
}

// ---------------------------------------------------------------------------
// 生成：200 / 202 + 专用轮询 + 断线同键恢复
// ---------------------------------------------------------------------------

// 规范化请求（只用于本地键与冲突检测；服务器真值一律由后端冻结）。
function normalizeEventGenerationRequest(payload) {
    return {
        direction: String(payload.direction || '').trim(),
        zone_name: String(payload.zone_name || '').trim(),
        count: Number(payload.count) || 10,
        use_llm: payload.use_llm !== false,
        opportunity_run_id: payload.opportunity_run_id || null,
        selected_event_ids: Array.isArray(payload.selected_event_ids) ? payload.selected_event_ids.slice().sort() : [],
        context_mode: payload.context_mode || 'current',
    };
}

function eventGenerationStorageKey(normalized) {
    return EVENT_GEN_STORAGE_PREFIX + JSON.stringify(normalized);
}

// 专用轮询：读生成账本，识别**全部**终态；终态必须结束轮询。
async function pollTopicGeneration(generationRequestId, elementId, options = {}) {
    const maxAttempts = options.maxAttempts || 60;
    const intervalMs = options.intervalMs || 1500;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        const response = await apiRequest(
            `/hotspot/topics/generation-runs/${encodeURIComponent(generationRequestId)}`, {}, false);
        const view = response.data || {};
        if (view.status === 'completed') {
            return { terminal: true, status: 'completed', result: view.result || {} };
        }
        if (view.status === 'failed' || view.status === 'cancelled' || view.status === 'interrupted') {
            return { terminal: true, status: view.status, reason_code: view.reason_code || view.status };
        }
        showLoading(elementId, { progress: null, message: '选题生成中（服务器持有生成账本）…' });
        await sleep(intervalMs);
    }
    return { terminal: false, status: 'running' };
}

// 带键生成：创建/复用 generation_request_id（存 sessionStorage）；202 进专用轮询。
async function generateEventTopics(payload, elementId) {
    const normalized = normalizeEventGenerationRequest(payload);
    const storageKey = eventGenerationStorageKey(normalized);
    let generationRequestId = sessionStorage.getItem(storageKey);
    if (!generationRequestId) {
        generationRequestId = (window.crypto && crypto.randomUUID)
            ? crypto.randomUUID()
            : `gen-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        sessionStorage.setItem(storageKey, generationRequestId);
    }
    const body = Object.assign({}, normalized, { generation_request_id: generationRequestId });
    showLoading(elementId, { progress: null, message: '正在生成选题…' });

    let response;
    try {
        response = await apiRequest('/hotspot/topics/generate', { method: 'POST', body: JSON.stringify(body) }, false);
    } catch (error) {
        if (eventApiErrorCode(error) === 'generation_key_conflict') {
            // 同键内容冲突：提示新操作，不偷偷换键重发。
            sessionStorage.removeItem(storageKey);
            throw new Error('相同请求的内容已变化：请作为一次新操作重新发起（不会自动换键重发）。');
        }
        throw error;
    }

    const data = response.data || {};
    if (data.accepted || data.status === 'running') {
        // 202：不能当作已生成；进入专用轮询。
        const polled = await pollTopicGeneration(generationRequestId, elementId);
        if (!polled.terminal) {
            throw new Error('生成仍在进行：请稍后在生成账本里查看结果，不会重复生成。');
        }
        if (polled.status !== 'completed') {
            throw new Error(`生成未完成（${polled.status}：${polled.reason_code}）`);
        }
        return polled.result;
    }
    sessionStorage.removeItem(storageKey);
    return data;
}

// 点击“生成针对这个事件的选题”。用户明确“重新生成”才建新键（清 sessionStorage）。
async function generateTopicsForEvent(opportunityRunId, eventIds, forceNew) {
    const elementId = 'event-opportunity-result';
    const direction = (document.getElementById('event-topic-direction') || {}).value || '围绕该事件做差异化创作';
    if (forceNew) {
        const normalized = normalizeEventGenerationRequest({
            direction, zone_name: (document.getElementById('topic-zone') || {}).value || '',
            count: 5, use_llm: true, opportunity_run_id: opportunityRunId, selected_event_ids: eventIds,
        });
        sessionStorage.removeItem(eventGenerationStorageKey(normalized));
    }
    try {
        const result = await generateEventTopics({
            direction,
            zone_name: (document.getElementById('topic-zone') || {}).value || '',
            count: 5,
            use_llm: true,
            opportunity_run_id: opportunityRunId,
            selected_event_ids: eventIds,
            context_mode: 'current',
        }, elementId);
        renderTopicsResultWithEvidence(result);
    } catch (error) {
        const el = document.getElementById(elementId);
        if (el) el.innerHTML = `<p class="event-error">生成失败：${escapeHtml(error.message)}</p>`;
    }
}

// 渲染生成结果：显示**实际 generation_mode**；completed 重放显示原生成时间与同一 saved_ids。
function renderTopicsResultWithEvidence(result) {
    if (!result || result.accepted || result.status === 'running') {
        // 防御：202 形状绝不当成已生成。
        return;
    }
    const topics = (result.topics || []).map((topic, index) => `
        <div class="event-topic-card">
            <h4>${index + 1}. ${escapeHtml(topic.title || '')}</h4>
            <p>${escapeHtml(topic.description || '')}</p>
            ${topic.generation_mode ? `<p class="event-meta">生成方式：${escapeHtml(topic.generation_mode)}</p>` : ''}
        </div>
    `).join('');
    const mode = result.generation_mode || (result.used_llm ? 'llm_assisted' : 'rule_template');
    __eventSavedTopicIds = Array.isArray(result.saved_ids) ? result.saved_ids.map(String) : [];
    const generatedAtText = eventEpochText(result.generated_at_s) || result.generated_at || '';
    const replayBadge = result.replayed
        ? `<span class="event-status event-status-completed">[历史重放]</span>` : '';
    document.getElementById('event-opportunity-result').innerHTML = `
        <p>生成方式（实际）：${escapeHtml(mode)} ${replayBadge}</p>
        <p class="event-meta">已保存选题：${(result.saved_ids || []).length} 条｜saved_ids：${escapeHtml((result.saved_ids || []).join(', ') || '无')}</p>
        ${generatedAtText ? `<p class="event-meta">原生成时间：${escapeHtml(generatedAtText)}</p>` : ''}
        ${topics || '<p class="event-insufficient">本次未产出选题（未伪造结果）。</p>'}
    `;
}

// ---------------------------------------------------------------------------
// 覆盖不足：显示缺哪几个窗口 + 预计下一次数据到达
// ---------------------------------------------------------------------------

const EVENT_WINDOW_LABELS = {
    missing_previous_window: '前一日窗口',
    missing_current_window: '当日窗口',
    insufficient_fast_coverage: '2 小时快窗',
    sampling_changed: '采样口径变化窗口',
    author_coverage_insufficient: '作者覆盖窗口',
    insufficient_sample: '样本量窗口',
};

// 由最近窗口右边界 + 日窗宽度推算下一次数据到达（只做推算提示，不承诺）。
function eventNextDataEtaText(windowEndS) {
    if (typeof windowEndS !== 'number' || !isFinite(windowEndS) || windowEndS <= 0) return '';
    const next = windowEndS + 86400;
    const text = eventEpochText(next);
    return text ? `预计下一次数据到达：约 ${text}（按日窗 24h 推算，实际以采样为准）` : '';
}

function renderCoverageShortfall(reasonCodes, nextAction, windowEndS) {
    const codes = Array.isArray(reasonCodes) ? reasonCodes : [];
    if (!codes.length) return '';
    const windows = codes.map(code => EVENT_WINDOW_LABELS[code] || code).join('、');
    const eta = eventNextDataEtaText(windowEndS);
    const fallback = nextAction === 'continue_watch'
        ? '预计下一次采样送达后自动补全（观测到新数据前不升级结论）'
        : '请稍后重新评估';
    return `<p class="event-insufficient">数据不足：缺少 ${escapeHtml(windows)}；${escapeHtml(eta || fallback)}。</p>`;
}

// ---------------------------------------------------------------------------
// 数值可溯源：展开到来源 BVID 与窗口（不显示无法溯源的综合热度百分数）
// ---------------------------------------------------------------------------

function renderTraceableMetrics(metrics, provenance) {
    const m = metrics || {};
    const rows = [];
    const push = (label, value) => {
        if (value === null || value === undefined || value === '') return;
        rows.push(`<li>${escapeHtml(label)}：${escapeHtml(String(value))}</li>`);
    };
    push('可用完整窗口数', m.available_windows);
    push('窗 A 增量', m.a_delta);
    push('窗 B 增量', m.b_delta);
    push('窗 C 增量', m.c_delta);
    push('匹配视频数', m.panel_video_count);
    push('已知作者数', m.panel_author_count);
    push('成员覆盖率', m.member_coverage);
    push('窗口右边界', eventEpochText(m.window_end_s) || m.window_end_s);
    push('阶段', m.topic_phase);
    push('阶段依据', m.stage_reason);
    const prov = Array.isArray(provenance) ? provenance : [];
    const provRows = prov.map(p =>
        `<li>BVID ${escapeHtml(String(p.bvid || '?'))}｜view=${escapeHtml(String(p.view))}</li>`).join('');
    return `
        <details class="event-trace">
            <summary>数值与来源（可展开到 BVID / 窗口）</summary>
            <ul class="event-trace-list">${rows.join('') || '<li>无可溯源数值</li>'}</ul>
            ${provRows ? `<p class="event-meta">来源快照：</p><ul class="event-trace-list">${provRows}</ul>` : ''}
            <p class="event-meta">说明：只展示可复算的窗口增量与来源；不提供无法溯源的综合热度百分数。</p>
        </details>
    `;
}

// ---------------------------------------------------------------------------
// 事件工作台
// ---------------------------------------------------------------------------

function currentEventAutoMembership() {
    const checked = document.querySelector('input[name="event-auto-membership"]:checked');
    return checked ? checked.value : 'review';
}

function setEventAutoMembership(mode) {
    const el = document.getElementById('event-attribution-preview');
    if (el) {
        el.innerHTML = `<p class="event-meta">归属方式：${mode === 'strict_auto'
            ? 'strict-auto（仅高精度规则自动接受，其余留待确认）'
            : '手动确认（review，所有命中进入待确认）'}</p>`;
    }
}

// 从热门 Tag 建草稿需要用户先加载（复用既有词云能力；未授权/无网络时如实提示）。
async function loadEventHotTags() {
    const el = document.getElementById('event-hot-tags');
    const zone = (document.getElementById('topic-zone') || {}).value || '游戏';
    if (el) el.innerHTML = '<p class="event-meta">正在读取热门 Tag…</p>';
    try {
        const response = await apiRequest('/hotspot/tag-cloud', {
            method: 'POST',
            body: JSON.stringify({ zone_name: zone, limit: 100, top_n: 20 }),
        });
        const freq = (response.data || {}).word_frequency || {};
        const tags = Object.keys(freq).slice(0, 20);
        if (!tags.length) {
            if (el) el.innerHTML = '<p class="event-insufficient">未取到热门 Tag（可能无数据或未授权联网）。</p>';
            return;
        }
        if (el) {
            el.innerHTML = tags.map(tag =>
                `<button class="btn btn-sm event-tag-chip" onclick="createEventDraftFromHotTag('${escapeHtml(tag)}')">${escapeHtml(tag)}</button>`
            ).join(' ');
        }
    } catch (error) {
        if (el) el.innerHTML = `<p class="event-insufficient">读取热门 Tag 失败：${escapeHtml(error.message)}（未伪造建议）。</p>`;
    }
}

// 用热门 Tag 建草稿：Tag 作为实体锚点初始名，用户可再编辑后“新建事件”。
function createEventDraftFromHotTag(tag) {
    const nameEl = document.getElementById('event-name');
    if (nameEl) {
        nameEl.value = nameEl.value ? `${nameEl.value} ${tag}`.trim() : String(tag || '');
    }
    const el = document.getElementById('event-workbench-result');
    if (el) el.innerHTML = '<p class="event-meta">已用热门 Tag 填入草稿名，请补充“具体事件锚点”后点击“新建事件”。</p>';
}

async function createEventDraft() {
    const nameEl = document.getElementById('event-name');
    const name = nameEl ? nameEl.value.trim() : '';
    const el = document.getElementById('event-workbench-result');
    if (!name) {
        if (el) el.innerHTML = '<p class="event-error">请填写实体 + 具体事件锚点。</p>';
        return;
    }
    try {
        const sourcePolicy = { auto_membership: currentEventAutoMembership() };
        const response = await apiRequest('/hotspot/events', {
            method: 'POST', body: JSON.stringify({ name, status: 'active', source_policy: sourcePolicy }),
        });
        window.__eventWorkbenchEventId = response.data.event_id;
        if (el) el.innerHTML = `<p>已创建事件 ${escapeHtml(response.data.event_id)}（版本 ${response.data.revision}）${eventStatusBadge(response.data.status)}</p>`;
    } catch (error) {
        if (el) el.innerHTML = `<p class="event-error">创建失败：${escapeHtml(error.message)}</p>`;
    }
}

// §15.1-3：一次点击“发现并观察”，展示预算、候选、待确认、采样名额。
async function discoverAndObserveEvent() {
    const el = document.getElementById('event-workbench-result');
    const eventId = window.__eventWorkbenchEventId;
    if (!eventId) {
        if (el) el.innerHTML = '<p class="event-error">请先新建事件。</p>';
        return;
    }
    try {
        const start = await apiRequest(`/hotspot/events/${encodeURIComponent(eventId)}/discover/tasks`, { method: 'POST' });
        const taskId = start.data.task_id;
        const runId = start.data.run_id;
        if (el) el.innerHTML = `<p>发现已启动 ${eventStatusBadge('running')}（run ${escapeHtml(runId)}）</p>`;
        const task = await pollEventTask(taskId);
        await renderDiscoveryObservation(eventId, runId, task);
    } catch (error) {
        const code = eventApiErrorCode(error);
        if (code === 'discovery_in_progress') {
            if (el) el.innerHTML = `<p class="event-meta">已有发现进行中（${eventStatusBadge('running')}），不会重复发起。</p>`;
            return;
        }
        if (code === 'discovery_disabled_by_config') {
            if (el) el.innerHTML = `<p class="event-insufficient">事件发现未启用（${escapeHtml(code)}）。请在配置中开启后重试；不会伪造结果。</p>`;
            return;
        }
        if (el) el.innerHTML = `<p class="event-error">发现失败：${escapeHtml(error.message)}</p>`;
    }
}

// 渲染发现观察：候选 / 待确认 / 已接受 / 排除 + 预算与采样名额（缺则如实标未提供）。
async function renderDiscoveryObservation(eventId, runId, task) {
    const el = document.getElementById('event-workbench-result');
    if (!el) return;
    let runView = {};
    if (runId) {
        try {
            runView = (await apiRequest(`/hotspot/event-discovery-runs/${encodeURIComponent(runId)}`)).data || {};
        } catch (error) { runView = {}; }
    }
    const counters = runView.counters || {};
    let members = [];
    try { members = await loadEventMembers(eventId); } catch (error) { members = []; }
    const byStatus = { proposed: 0, accepted: 0, rejected: 0 };
    members.forEach(m => { if (byStatus[m.status] !== undefined) byStatus[m.status] += 1; });
    const result = (task && task.result) || {};
    // §15.1-③：预算 / 候选 / 待确认 / 采样名额从后端既有服务状态暴露（逐项可溯源）。
    let budgetView = null;
    try {
        budgetView = (await apiRequest(`/hotspot/events/${encodeURIComponent(eventId)}/budget`)).data || null;
    } catch (error) { budgetView = null; }
    const lines = [
        `任务状态：${eventStatusBadge((task && task.status) || 'unknown')}`,
        `候选：${escapeHtml(String(result.candidate_count ?? counters.candidate_count ?? 0))}`,
        `待确认：${escapeHtml(String(byStatus.proposed))}｜已接受：${escapeHtml(String(byStatus.accepted))}｜排除：${escapeHtml(String(byStatus.rejected))}`,
        `命中 cap_reached：${counters.cap_reached ? '是' : '否'}｜页重复：${counters.page_duplicate ? '是' : '否'}`,
    ];
    const budgetLines = budgetView
        ? [
            renderBudgetValue(budgetView.budget && budgetView.budget.discovery_requests_per_24h, '发现预算'),
            renderBudgetValue(budgetView.budget && budgetView.budget.watch_samples_per_24h, '采样预算'),
            renderBudgetValue(budgetView.candidate_count, '候选数'),
            renderBudgetValue(budgetView.pending_confirm, '待确认'),
            renderBudgetValue(budgetView.sampling_quota, '采样名额'),
        ]
        : ['预算 / 采样名额：未提供（budget_unavailable）'];
    const shortfall = renderCoverageShortfall((result.reason_codes) || (runView.counters || {}).reason_codes, result.next_action);
    const attribution = renderAttributionPreview(members);
    const channels = renderEventChannels(counters, members);
    el.innerHTML = lines.map(text => `<p class="event-meta">${text}</p>`).join('')
        + budgetLines.map(text => `<p class="event-meta">${text}</p>`).join('')
        + shortfall + attribution + channels;
}

// §15.1-2：查看示例命中 / 冲突 / 排除（基于真实成员决定，不臆造）。
function renderAttributionPreview(members) {
    const list = Array.isArray(members) ? members : [];
    if (!list.length) {
        return '<p class="event-meta">归属预览：暂无成员决定（先执行“发现并观察”）。</p>';
    }
    const hit = list.filter(m => m.status === 'accepted').map(m => m.bvid);
    const pending = list.filter(m => m.status === 'proposed').map(m => m.bvid);
    const excluded = list.filter(m => m.status === 'rejected').map(m => m.bvid);
    const conflict = list.filter(m => {
        const codes = ((m.evidence || {}).reason_codes) || [];
        return codes.some(c => String(c).includes('conflict') || String(c).includes('ambiguous'));
    }).map(m => m.bvid);
    const fmt = arr => arr.length ? arr.map(escapeHtml).join('、') : '无';
    return `
        <details class="event-attribution-preview">
            <summary>示例命中 / 冲突 / 排除（真实决定）</summary>
            <p class="event-meta">命中（accepted）：${fmt(hit)}</p>
            <p class="event-meta">待确认（proposed）：${fmt(pending)}</p>
            <p class="event-meta">冲突/歧义：${fmt(conflict)}</p>
            <p class="event-meta">排除（rejected）：${fmt(excluded)}</p>
        </details>
    `;
}

// §15.1-④：事件卡片双通道（匹配视频增量趋势 ／ 新作品作者线索）；
// 单条视频点击复用 02 原卡片打开逻辑（不新建详情页），失败有明确提示。
function renderEventChannels(counters, members) {
    const c = counters || {};
    const matched = c.matched !== undefined ? String(c.matched) : '未提供';
    const newAuthors = Array.isArray(c.new_authors) ? c.new_authors.length : undefined;
    const newBvids = Array.isArray(c.newly_discovered_bvids) ? c.newly_discovered_bvids.length : undefined;
    const list = Array.isArray(members) ? members : [];
    const videos = list
        .map(m => String((m && m.bvid) || ''))
        .filter(Boolean)
        .map(bvid => `<button class="btn btn-sm event-video-link" type="button" onclick="openEventVideoCard('${escapeHtml(bvid)}')">${escapeHtml(bvid)} → 02 卡片</button>`)
        .join(' ');
    return `
        <div class="event-dual-channel">
            <div class="event-channel">
                <h4>通道 A：匹配视频增量趋势</h4>
                <p class="event-meta">匹配成员：${escapeHtml(matched)}（趋势数值见下方“数值与来源”展开）</p>
            </div>
            <div class="event-channel">
                <h4>通道 B：新作品作者线索</h4>
                <p class="event-meta">新发现视频：${escapeHtml(String(newBvids ?? '未提供'))}｜新作者：${escapeHtml(String(newAuthors ?? '未提供'))}</p>
            </div>
        </div>
        <div class="event-video-links">
            <p class="event-meta">单视频详情（复用 02 原卡片，不新建详情页）：</p>
            ${videos || '<p class="event-meta">暂无可打开的成员视频</p>'}
        </div>
        <div id="event-jump-hint" class="event-jump-hint" role="status" aria-live="polite"></div>
    `;
}

// 评估（当日窗口）：真跑后端聚合，然后渲染双通道 + 可溯源数值 + 历史时间徽标。
async function assessEventDaily() {
    const el = document.getElementById('event-workbench-result');
    const eventId = window.__eventWorkbenchEventId;
    if (!eventId) {
        if (el) el.innerHTML = '<p class="event-error">请先新建事件。</p>';
        return;
    }
    try {
        const resp = await apiRequest(`/hotspot/events/${encodeURIComponent(eventId)}/assess/tasks`, {
            method: 'POST', body: JSON.stringify({ window_kind: 'daily24h' }),
        });
        const data = resp.data || {};
        const result = data.result || {};
        if (el) {
            el.innerHTML = `<p>评估完成 ${eventStatusBadge(result.data_status || 'collecting')}</p>`
                + renderCoverageShortfall(result.reason_codes, result.next_action);
        }
        await showLatestAssessment(eventId);
    } catch (error) {
        if (el) el.innerHTML = `<p class="event-error">评估失败：${escapeHtml(error.message)}</p>`;
    }
}

// 读取并展示最新评估（含历史时间徽标与可溯源数值）。
async function showLatestAssessment(eventId) {
    const el = document.getElementById('event-workbench-result');
    try {
        const listResp = await apiRequest(`/hotspot/events/${encodeURIComponent(eventId)}/assessments?limit=20`);
        const items = (listResp.data || {}).items || [];
        window.__eventAssessments = items;
        if (!items.length) return;
        const latest = items[0];
        const detail = (await apiRequest(`/hotspot/event-assessments/${encodeURIComponent(latest.assessment_id)}`)).data || {};
        if (el) el.innerHTML += `
            <div class="event-assessment">
                <h4>评估冻结事实 ${eventStatusBadge(detail.data_status || detail.status)}</h4>
                <p class="event-meta">窗口时间（历史证据时间）：${escapeHtml(eventEpochText(detail.window_end_s) || String(detail.window_end_s || ''))}</p>
                ${renderTraceableMetrics(detail.metrics, detail.provenance)}
                ${renderAssessmentHistoryBadges(items)}
            </div>
        `;
    } catch (error) {
        if (el) el.innerHTML += `<p class="event-error">读取评估失败：${escapeHtml(error.message)}</p>`;
    }
}

// §15.2-6：选择旧 assessment 时明显显示历史时间，不用今天界面时间冒充当时证据。
function renderAssessmentHistoryBadges(items) {
    const list = Array.isArray(items) ? items : [];
    if (list.length <= 1) return '';
    const rows = list.map(item => {
        const hist = eventEpochText(item.window_end_s) || String(item.window_end_s || '');
        return `<li>${escapeHtml(item.assessment_id)}｜历史证据时间 ${escapeHtml(hist)}｜${escapeHtml(item.data_status || item.status || '')}</li>`;
    }).join('');
    return `<details class="event-assessment-history"><summary>历史评估（每条带当时证据时间）</summary><ul class="event-trace-list">${rows}</ul></details>`;
}

// 通用任务读取（事件任务）：completed 正常结束；failed 带 reason_code。
async function pollEventTask(taskId, options = {}) {
    const maxAttempts = options.maxAttempts || 60;
    const intervalMs = options.intervalMs || 1500;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        const response = await apiRequest(`/hotspot/event-tasks/${encodeURIComponent(taskId)}`, {}, false);
        const task = response.data || {};
        if (task.status === 'completed') return task;
        if (task.status === 'failed') return task;
        await sleep(intervalMs);
    }
    return { status: 'running' };
}

async function loadEventMembers(eventId, status) {
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    const response = await apiRequest(`/hotspot/events/${encodeURIComponent(eventId)}/members${query}`);
    return response.data.items || [];
}

// ---------------------------------------------------------------------------
// 机会列表 + 反馈
// ---------------------------------------------------------------------------

// §15.2-1：收集 CreatorBrief / 可用素材 / 制作时间（客户端只填条件，不自填指标）。
function collectCreatorBrief() {
    const num = (id, dflt) => {
        const el = document.getElementById(id);
        const value = el ? Number(el.value) : NaN;
        return isFinite(value) && value >= 0 ? value : dflt;
    };
    const list = (id) => {
        const el = document.getElementById(id);
        return el && el.value.trim()
            ? el.value.split(',').map(s => s.trim()).filter(Boolean) : null;
    };
    const brief = {
        brief_version: 'v1',
        production_hours: num('event-brief-production', 2),
        review_hours: num('event-brief-review', 1),
        publish_buffer_hours: num('event-brief-buffer', 0),
        max_experiment_hours: num('event-brief-experiment', 4),
    };
    const assets = list('event-brief-assets');
    const formats = list('event-brief-formats');
    const entities = list('event-brief-entities');
    if (assets) brief.available_assets = assets;
    if (formats) brief.supported_formats = formats;
    if (entities) brief.allowed_entities = entities;
    return brief;
}

// 生成机会列表：POST /opportunities/tasks → GET /opportunities/{id} → 渲染。
async function createOpportunityForEvent() {
    const el = document.getElementById('event-opportunity-result');
    const eventId = window.__eventWorkbenchEventId;
    if (!eventId) {
        if (el) el.innerHTML = '<p class="event-error">请先新建事件。</p>';
        return;
    }
    try {
        const created = await apiRequest('/hotspot/opportunities/tasks', {
            method: 'POST',
            body: JSON.stringify({ creator_brief: collectCreatorBrief(), event_ids: [eventId] }),
        });
        const runId = (created.data || {}).opportunity_run_id;
        const run = (await apiRequest(`/hotspot/opportunities/${encodeURIComponent(runId)}`)).data || {};
        __eventCurrentRun = run;
        renderOpportunityList(run);
    } catch (error) {
        if (el) el.innerHTML = `<p class="event-error">机会生成失败：${escapeHtml(error.message)}</p>`;
    }
}

async function submitOpportunityFeedback(runId, payload, elementId) {
    const el = document.getElementById(elementId);
    try {
        const body = Object.assign({ feedback_id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `fb-${Date.now()}` }, payload);
        const response = await apiRequest(`/hotspot/opportunities/${encodeURIComponent(runId)}/feedback`, {
            method: 'POST', body: JSON.stringify(body),
        });
        const data = response.data;
        const note = data.idempotent_replay ? '（幂等：复用原记录）' : '';
        if (el) el.innerHTML = `<p>反馈已记录 ${eventStatusBadge('completed')}${escapeHtml(note)}；验证等级：${escapeHtml(data.verification_status || 'user_reported')}（不自动发布）</p>`;
    } catch (error) {
        if (el) el.innerHTML = `<p class="event-error">反馈失败：${escapeHtml(error.message)}</p>`;
    }
}

// 供给角度密度展示（第四批 c / 06 §7.2）：分母是 |C| 不是 |E|；C 空或低 coverage
// 只标「不足」，绝不显示 0，也绝不称饱和率 / 蓝海 / 空白 / 市场未满足率。
function renderSupplyAngleDensity(discovery) {
    const density = (discovery && typeof discovery === 'object') ? discovery.angle_density : null;
    if (density && typeof density === 'object') {
        const coverage = density.angle_coverage;
        const share = density.angle_share || {};
        const shareKeys = Object.keys(share).filter(key => share[key] !== null && share[key] !== undefined);
        const insufficient = coverage === null || coverage === undefined || density.evaluable !== true;
        if (insufficient || !shareKeys.length) {
            return '不足（已见样本角度覆盖率不足以评价拥挤线索）';
        }
        const parts = shareKeys.map(key => `${key} ${(share[key] * 100).toFixed(0)}%`);
        const coverageText = `${(coverage * 100).toFixed(0)}%（可分类 ${density.classified_count}/${density.member_count}）`;
        return `已见样本角度拥挤线索：${parts.join('、')}｜角度覆盖率 ${coverageText}｜未分类 ${density.unknown_angle_count}`;
    }
    // 兼容旧证据结构：仅有 token 时只列已见内容，不折算成任何拥挤率。
    const counters = (discovery && discovery.counters) || {};
    const tokens = (discovery && (discovery.angle_tokens || counters.angle_tokens)) || [];
    return tokens.length ? `已见角度：${tokens.join('、')}` : '不足（证据不足）';
}

// 渲染机会列表：action / fit 原因 / 日-快信号区别 / 供给角度密度 / ETA / 截止依据；
// 不显示无法溯源的综合热度。反馈含 采用 / 不采用 / 已发布（只记录，不自动发布）。
function renderOpportunityList(run) {
    const el = document.getElementById('event-opportunity-result');
    if (!el) return;
    const topicId = __eventSavedTopicIds.length ? __eventSavedTopicIds[0] : '';
    const candidates = (run.candidates || []).map(candidate => {
        const daily = (candidate.evidence || {}).daily || {};
        const early = (candidate.evidence || {}).early || {};
        const discovery = (candidate.evidence || {}).discovery || {};
        const deadline = candidate.deadline || {};
        const etaText = eventEpochText(deadline.publish_eta_s) || '未提供';
        const deadlineBasis = deadline.longevity_unknown
            ? '无已知/可信且绑定的截止（寿命未知，不编造剩余天数）'
            : `截止 ${eventEpochText(deadline.deadline_s) || deadline.deadline_s || '未提供'}（可信度 ${deadline.deadline_confidence ?? '未提供'}，可行性 ${deadline.feasibility_category || '未提供'}）`;
        const angleDensity = renderSupplyAngleDensity(discovery);
        const runId = escapeHtml(run.opportunity_run_id);
        const eid = escapeHtml(candidate.event_id || '');
        const topicArg = topicId ? `'${escapeHtml(topicId)}'` : "''";
        return `
            <div class="event-opportunity-card">
                <h4>${eid} ${eventActionBadge(candidate.action)}</h4>
                <p>适配原因：${escapeHtml(candidate.reason_code || '')}｜账号匹配：${escapeHtml(candidate.account_match || '')}</p>
                <p class="event-meta">日信号：${escapeHtml(daily.topic_phase || '未定')}（依据 ${escapeHtml(daily.stage_reason || '未提供')}）｜快信号：${escapeHtml(early.signal || '无')}（状态 ${escapeHtml(early.status || '未提供')}）</p>
                <p class="event-meta">供给角度密度：${escapeHtml(String(angleDensity))}</p>
                <p class="event-meta">制作 ETA：${escapeHtml(etaText)}｜截止依据：${escapeHtml(deadlineBasis)}</p>
                <p class="event-meta">排序键 rank_key：${escapeHtml(JSON.stringify(candidate.rank_key || []))}</p>
                <p class="event-meta">局限：${escapeHtml((candidate.notes || []).join('；') || '无额外说明')}</p>
                ${renderTraceableMetrics(daily, ((candidate.evidence || {}).provenance) || [])}
                <button class="btn" onclick="generateTopicsForEvent('${runId}', ['${eid}'])">生成针对这个事件的选题</button>
                <button class="btn" onclick="generateTopicsForEvent('${runId}', ['${eid}'], true)">重新生成（新键）</button>
                <button class="btn" onclick="submitOpportunityFeedback('${runId}', {kind:'adopted', event_id:'${eid}', topic_id:${topicArg}, reason:'用户采纳'}, 'event-opportunity-result')">采用</button>
                <button class="btn" onclick="submitOpportunityFeedback('${runId}', {kind:'rejected', event_id:'${eid}', topic_id:${topicArg}, reason:'用户拒绝'}, 'event-opportunity-result')">不采用</button>
                <button class="btn" onclick="submitOpportunityFeedback('${runId}', {kind:'published', event_id:'${eid}', topic_id:${topicArg}, reason:'用户已发布'}, 'event-opportunity-result')">已发布</button>
            </div>
        `;
    }).join('');
    el.innerHTML = `${candidates || '<p class="event-insufficient">暂无机会候选（未伪造）。</p>'}`;
}

// action 徽标（文字，不依赖颜色）。
function eventActionBadge(action) {
    const label = {
        make_candidate: '可做候选', prepare_or_pilot: '试做/准备', differentiate_research: '差异化研究',
        watch_and_collect: '继续观察', not_suitable: '不适合', deadline_missed: '错过截止',
    }[action] || String(action || '');
    return `<span class="event-action">[${escapeHtml(label)}]</span>`;
}


// ---------------------------------------------------------------------------
// 第三批 i：只读预览 / 预算名额 / 单视频跳 02 卡片
// ---------------------------------------------------------------------------

// 解析“实体 + 具体事件锚点”输入为锚点列表（只用于预览示例，不提交服务器真值）。
function parseEventAnchors() {
    const el = document.getElementById('event-name');
    const raw = el ? el.value.trim() : '';
    if (!raw) return [];
    return raw.split(/[\s,，、|]+/).map(s => s.trim()).filter(Boolean);
}

// §15.1-③：数值逐项可溯源；拿不到的显式“未提供 + 原因码”，不臆造。
function renderBudgetValue(view, label) {
    if (!view) return `${label}：未提供（no_data）`;
    if (view.available) {
        const unit = view.unit ? ` ${String(view.unit)}` : '';
        return `${label}：${escapeHtml(String(view.value))}${escapeHtml(unit)}（来源 ${escapeHtml(String(view.source || '未标注'))}）`;
    }
    return `${label}：未提供（${escapeHtml(String(view.reason_code || 'unknown'))}）`;
}

// §15.1-②：只读预览三分类；预览不可当已提交决定（decisions 仍走 CAS）。
function renderPreviewResult(data) {
    const el = document.getElementById('event-attribution-preview');
    if (!el) return;
    const fmt = rows => (Array.isArray(rows) && rows.length)
        ? rows.map(r => `<li>${escapeHtml(r.title || r.bvid || '(示例)')}｜决定 ${escapeHtml(r.decision || '')}｜理由 ${escapeHtml((r.reason_codes || []).join('、') || '无')}${r.example ? '（示例）' : ''}</li>`).join('')
        : '<li>无</li>';
    const gen = data.examples_generated ? '<span class="event-meta">（当前无发现结果，下面为规则字面量示例）</span>' : '';
    el.innerHTML = `
        <p class="event-meta">只读预览 ${gen}：预览不是已提交决定，成员写入仍走 CAS 与围栏。</p>
        <p class="event-meta">命中（would_match）：</p><ul class="event-trace-list">${fmt(data.would_match)}</ul>
        <p class="event-meta">冲突（would_conflict）：</p><ul class="event-trace-list">${fmt(data.would_conflict)}</ul>
        <p class="event-meta">排除（would_exclude）：</p><ul class="event-trace-list">${fmt(data.would_exclude)}</ul>
    `;
}

// §15.1-②：调用只读预览端点（纯计算，不落库、不写 revision）。
async function previewEventMembers() {
    const el = document.getElementById('event-attribution-preview');
    const eventId = window.__eventWorkbenchEventId;
    if (!eventId) {
        if (el) el.innerHTML = '<p class="event-error">请先新建事件，再预览归属示例。</p>';
        return;
    }
    try {
        const resp = await apiRequest(`/hotspot/events/${encodeURIComponent(eventId)}/members/preview`, {
            method: 'POST',
            body: JSON.stringify({
                entity_anchors: parseEventAnchors(),
                strict_auto: currentEventAutoMembership() === 'strict_auto',
            }),
        });
        renderPreviewResult(resp.data || {});
    } catch (error) {
        if (el) el.innerHTML = `<p class="event-error">预览失败：${escapeHtml(error.message)}</p>`;
    }
}

// §15.1-④：单视频点击复用 02 原卡片打开逻辑；打不开时明确提示，绝不静默。
async function openEventVideoCard(bvid) {
    const hint = document.getElementById('event-jump-hint');
    if (typeof openHotspotVideoCard !== 'function') {
        if (hint) hint.textContent = `无法打开 02 卡片：02 卡片模块未加载（${bvid}）。`;
        return;
    }
    let ok = false;
    try {
        ok = await openHotspotVideoCard(bvid);
    } catch (error) {
        ok = false;
    }
    if (hint) {
        hint.textContent = ok
            ? `已打开 ${bvid} 的 02 原卡片。`
            : `无法打开 ${bvid} 的 02 原卡片：该视频尚未进入热点生命周期列表，可先跑一轮采集。`;
    }
}
