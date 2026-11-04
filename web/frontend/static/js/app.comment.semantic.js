// ===== app.comment.semantic.js —— 评论语义折叠（可逆）实验面板 =====
// 挂在评论监控页，依赖 app.core.js 的 apiRequest / escapeHtml / jsArg / asArray / sleep，
// 以及 app.ui.js 的 mountAppModal / showAppAlert / showAppConfirm；必须在它们之后加载。
//
// 设计约束（与实现契约 §API/UI wire contract 对齐）：
// - 实验性可选功能：首次使用弹模态，披露 provider/费用/外发范围与「准确性未经验证」，
//   征得同意后才允许发起；同意绑定 model identity + CONSENT_VERSION，经 API 持久化。
// - 不随页面加载自动计费：只有用户显式点「开始分析」才创建任务并发起模型请求。
// - 任务进度支持取消/继续（继续需显式确认未知结果）；轮询不设总次数上限（长任务可达数小时），
//   页面隐藏 / 任务终态即停，回到前台且任务仍在进行时恢复；切换 BV 时作废旧响应，避免陈旧结果串台。
// - auto_fold 开关只改展示视图、经 API 持久化偏好，绝不触发任何模型请求。
// - 分组展示代表项 + 归并理由 + 全部成员详情；未分组/被排除/不确定分别分页列全量原文。
// - 撤销整组 / 拆分单条 / 显式允许（恢复）；不删除任何原始评论、不改旧大屏统计。
// - 所有模型/接口文本经 escapeHtml 后渲染，绝不把模型输出按 HTML 直插。
// - 写请求复用本机 session token（X-Local-Token，不进 URL/日志）。

(function () {
    'use strict';

    // CONSENT_VERSION 与后端契约常量保持一致（semantic-fold-v1）。
    const CONSENT_VERSION = 'semantic-fold-v1';
    const API_PREFIX = '/comment/semantic';
    const DEFAULT_LIMIT = 200;
    const MAX_LIMIT = 500;
    // 轮询节奏：每次开始轮询（发起、接管、回到前台）后的前 2 分钟每 1.5 秒一次，之后每 5 秒一次。
    // 状态 DTO 没有任务开始时间，按本轮轮询计；“无进展”计时同样随每轮轮询重新开始。
    // 语义任务可能跑几十分钟到几小时，不设总次数上限：只要任务仍在进行就一直跟进，
    // 由代际令牌 / BV 切换 / 页面隐藏 / 终态负责停止。
    const POLL_INTERVAL_MS = 1500;
    const POLL_SLOW_INTERVAL_MS = 5000;
    const POLL_FAST_WINDOW_MS = 2 * 60 * 1000;
    // running 状态下这么久 revision 没变就提示“长时间无进展”（不停止轮询）。
    // queued 不提示：语义任务全局串行，排队时 revision 本来就不会变。
    const POLL_STALL_WARN_MS = 10 * 60 * 1000;

    // 面板运行态：单例，随 BV 切换重置。
    const state = {
        bvid: '',
        // fetchSeq 单调递增：每次切换 BV / 重新加载时 +1，异步响应回来时比对，过期则丢弃。
        fetchSeq: 0,
        preference: null,          // {auto_fold,revision,consent_version,model_identity}
        preflight: null,           // 预检投影（provider/费用/范围/限额/是否需同意）
        task: null,                // 当前任务 public_state
        result: null,             // 当前结果投影
        resultPage: 1,
        resultRequest: 0,          // 同视频/任务的分页和决策刷新也须按请求代际防旧回写。
        polling: false,
        pollToken: 0,             // 轮询代际：停止时 +1，使在途轮询循环自行退出
        pollStalled: false,       // running 且长时间 revision 未变：任务区显示提示
        localToken: null,
        limit: DEFAULT_LIMIT,
        starting: false,          // 发起任务进行中（防双击双计费）
        decisionBusy: false,      // 决策请求进行中（防双击重复 POST）
        requestKey: null,         // 当前 BV 的稳定发起 key（终态/切 BV 才换新）
        requestKeyBvid: '',       // requestKey 绑定的 BV
    };

    // ---- 本机 session token（写请求必带；token 不进 URL / 日志）----
    async function getLocalToken(force) {
        if (state.localToken && !force) return state.localToken;
        try {
            const response = await apiRequest(`${API_PREFIX}/local-token`, {}, false);
            state.localToken = (response.data || {}).token || null;
        } catch (error) {
            state.localToken = null;
        }
        return state.localToken;
    }

    async function writeHeaders() {
        const token = await getLocalToken(false);
        return token ? { 'X-Local-Token': token } : {};
    }

    // ---- API 封装（读静默失败，写请求带 token）----
    function apiGet(path, showError) {
        return apiRequest(`${API_PREFIX}${path}`, {}, showError !== false);
    }

    async function apiWrite(path, method, body) {
        const headers = await writeHeaders();
        // 必须显式带 Content-Type：app.core.js 的 apiRequest 在 headers 之后展开 ...options，
        // options.headers 会整体覆盖其默认的 {'Content-Type':'application/json'}，
        // 只传 {X-Local-Token} 会让 fetch 用 text/plain，FastAPI 不按 JSON 解析 → 422。
        return apiRequest(`${API_PREFIX}${path}`, {
            method,
            headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
            body: body ? JSON.stringify(body) : undefined,
        });
    }

    function fetchPreflight(bvid, limit) {
        const query = `?bvid=${encodeURIComponent(bvid)}&limit=${encodeURIComponent(limit)}`;
        return apiGet(`/preflight${query}`, false);
    }

    function fetchPreference(bvid) {
        return apiGet(`/preferences?bvid=${encodeURIComponent(bvid)}`, false);
    }

    function fetchLatest(bvid) {
        return apiGet(`/latest?bvid=${encodeURIComponent(bvid)}`, false);
    }

    function fetchTaskStatus(taskId) {
        return apiGet(`/tasks/${encodeURIComponent(taskId)}`, false);
    }

    function fetchResult(taskId, page, pageSize) {
        const query = `?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize || 50)}`;
        return apiGet(`/tasks/${encodeURIComponent(taskId)}/result${query}`, false);
    }

    // ---- DOM helpers ----
    function el(id) {
        return typeof document !== 'undefined' ? document.getElementById(id) : null;
    }

    function setHtml(id, html) {
        const node = el(id);
        if (node) node.innerHTML = html;
    }

    function show(id, visible) {
        const node = el(id);
        if (node) node.hidden = !visible;
    }

    // 把数值/文本安全转字符串（null/undefined -> fallback），再交 escapeHtml。
    function text(value, fallback) {
        const raw = value === null || value === undefined || value === '' ? (fallback ?? '—') : value;
        return escapeHtml(raw);
    }

    // ---- 首次使用同意模态：披露 provider / 费用 / 外发范围 / 准确性未经验证 ----
    // 返回 Promise<boolean>：用户点「我已知悉并同意」为 true，取消/关闭为 false。
    function showConsentModal(preflight) {
        return new Promise(resolve => {
            const pf = preflight || {};
            const identity = pf.provider_identity || pf.model_identity || {};
            const backdrop = document.createElement('div');
            backdrop.className = 'app-modal-backdrop';
            backdrop.setAttribute('role', 'alertdialog');
            backdrop.setAttribute('aria-modal', 'true');
            backdrop.setAttribute('aria-label', '语义折叠使用须知');

            const modal = document.createElement('div');
            modal.className = 'app-modal semantic-consent-modal';

            // 全部字段来自接口 / 模型配置，统一 escapeHtml 后插入。
            const apiBase = text(identity.api_base, '未配置');
            const model = text(identity.model, '未配置');
            const limit = Number(pf.limit) || state.limit || DEFAULT_LIMIT;
            modal.innerHTML = `
                <h3>语义折叠 · 使用须知</h3>
                <p class="semantic-consent-flag">实验性功能 · 分组结果由大模型给出，<strong>准确性未经验证</strong>，请人工复核后再采纳。</p>
                <ul class="semantic-consent-list">
                    <li>调用方：<strong>${model}</strong></li>
                    <li>接口地址：<strong>${apiBase}</strong></li>
                    <li>计费：按该服务商 token 用量计费，具体金额由服务商结算；工具不代收费用，也无法为任意服务商承诺精确上限。</li>
                    <li>外发内容：仅发送本视频最多 <strong>${escapeHtml(String(limit))}</strong> 条（上限 ${MAX_LIMIT}）评论的<strong>短 ID 与正文文本</strong>；不发送 UID、昵称、rpid 等账号标识。</li>
                    <li>发起方式：仅在你点「开始分析」时发起，不随页面加载自动计费。</li>
                </ul>
                <div class="app-modal-actions">
                    <button type="button" class="btn btn-secondary" data-act="cancel">取消</button>
                    <button type="button" class="btn btn-primary" data-act="agree">我已知悉并同意</button>
                </div>`;

            backdrop.append(modal);
            let settled = false;
            const done = value => {
                if (settled) return;
                settled = true;
                closeModal();
                resolve(value);
            };
            const closeModal = mountAppModal(backdrop, () => { if (!settled) { settled = true; resolve(false); } });
            modal.querySelector('[data-act="agree"]').addEventListener('click', () => done(true));
            modal.querySelector('[data-act="cancel"]').addEventListener('click', () => done(false));
            backdrop.addEventListener('click', event => { if (event.target === backdrop) done(false); });
            modal.querySelector('[data-act="agree"]').focus();
        });
    }

    // 该 BV 是否已持久化「绑定当前 identity + 当前 CONSENT_VERSION」的同意。
    function hasValidConsent(preference, preflight) {
        if (!preference) return false;
        if (preference.consent_version !== CONSENT_VERSION) return false;
        // identity 必须与预检给出的 provider 身份一致（跨 identity 不复用同意）。
        const bound = preference.model_identity || null;
        const current = (preflight && (preflight.provider_identity || preflight.model_identity)) || null;
        if (!bound || !current) return false;
        return bound.api_base === current.api_base && bound.model === current.model;
    }

    // ---- BV 切换入口：由 app.comment.js 的 setCurrentCommentBvid 回调触发 ----
    async function selectBvid(bvid) {
        const normalized = String(bvid || '').trim();
        if (!normalized) return;
        stopPolling();
        state.bvid = normalized;
        state.task = null;
        state.result = null;
        state.resultPage = 1;
        // 作废旧 BV 在途响应：递增序号，回调回来后比对不等即丢弃。
        const seq = (state.fetchSeq += 1);
        revealPanel();
        setHtml('semantic-body', '<p class="semantic-hint">正在加载语义折叠状态…</p>');
        await loadForBvid(normalized, seq);
    }

    // 拉取预检 + 偏好 + 最近任务；刷新/重启后加载最新任务与偏好。全部静默失败降级。
    async function loadForBvid(bvid, seq) {
        let preflight = null;
        let preference = null;
        let latest = null;
        try {
            const results = await Promise.all([
                fetchPreflight(bvid, state.limit).catch(() => null),
                fetchPreference(bvid).catch(() => null),
                fetchLatest(bvid).catch(() => null),
            ]);
            preflight = results[0] && results[0].data;
            preference = results[1] && results[1].data;
            latest = results[2] && results[2].data;
        } catch (error) {
            // 任一失败都降级为“不可用”提示，不抛给用户打断采集主流程。
        }
        // 过期响应：期间用户已切到别的 BV，丢弃本次渲染。
        if (seq !== state.fetchSeq) return;

        if (!preflight) {
            setHtml('semantic-body', '<p class="semantic-hint">语义折叠服务暂不可用，可稍后在此视频重试。</p>');
            return;
        }
        state.preflight = preflight;
        state.preference = preference || null;
        state.task = latest || null;
        renderOverview();
        // 有最近任务且处于进行中：接管轮询（不重复发起模型请求）。
        if (state.task && isActiveStatus(state.task.status)) {
            startPolling(state.task.task_id, seq);
        } else if (state.task && isTerminalWithResult(state.task.status)) {
            await loadResult(state.task.task_id, 1, seq);
        }
    }

    function isActiveStatus(status) {
        return status === 'queued' || status === 'running';
    }

    function isTerminalWithResult(status) {
        return status === 'completed' || status === 'partial';
    }

    function revealPanel() {
        // 面板在评论页常驻（index.html 不再默认隐藏）；保留函数仅为语义清晰。
        show('semantic-panel', true);
        // 选择 BV 后把输入框同步为当前值，便于用户确认/修改后重新加载。
        const input = el('semantic-bvid');
        if (input && state.bvid && input.value.trim() !== state.bvid) input.value = state.bvid;
    }

    // 渲染面板概览：选中范围、provider、同意/未配置状态、开始按钮、auto_fold 开关。
    function renderOverview() {
        const pf = state.preflight || {};
        const counts = pf.counts || {};
        const identity = pf.provider_identity || pf.model_identity || {};
        const configured = pf.model_configured !== false && (identity.api_base || identity.model);
        // 无偏好行时 store 约定 auto_fold 默认 true（opt-in 后生效）。
        const autoFold = state.preference ? !!state.preference.auto_fold : true;
        const scopeLimit = Number(pf.limit) || state.limit || DEFAULT_LIMIT;

        const providerLine = configured
            ? `调用方 <strong>${text(identity.model)}</strong> · ${text(identity.api_base)}`
            : '<span class="semantic-warn">未配置大模型，请先到配置页填写后再使用</span>';

        // 概览统计只做展示，取 preflight 的 counts；不改动旧大屏统计。
        const html = `
            <div class="semantic-overview">
                <p class="semantic-provider">${providerLine}</p>
                <div class="semantic-scope">
                    <span>原始评论 <strong>${text(counts.raw_total, 0)}</strong></span>
                    <span>本次范围 <strong>${text(counts.selected, 0)}</strong>/${escapeHtml(String(scopeLimit))}</span>
                    <span>可参与 <strong>${text(counts.eligible, 0)}</strong></span>
                </div>
                <label class="semantic-autofold">
                    <input type="checkbox" id="semantic-autofold-toggle" ${autoFold ? 'checked' : ''}>
                    自动折叠等价评论（仅改展示，不触发模型调用）
                </label>
                <div class="semantic-actions" id="semantic-actions"></div>
                <p class="semantic-flag-note">实验性功能，分组由大模型给出，准确性未经验证。</p>
            </div>
            <div id="semantic-task-area"></div>
            <div id="semantic-result-area"></div>`;
        setHtml('semantic-body', html);

        const toggle = el('semantic-autofold-toggle');
        if (toggle) toggle.addEventListener('change', onAutoFoldToggle);
        renderActions(configured);
        renderTaskArea();
    }

    // 开始 / 继续 / 取消按钮：按当前任务状态与配置决定可见性。
    function renderActions(configured) {
        const area = el('semantic-actions');
        if (!area) return;
        area.innerHTML = '';
        const task = state.task;
        const makeBtn = (label, cls, handler, disabled) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `btn ${cls}`;
            btn.textContent = label;
            if (disabled) btn.disabled = true;
            btn.addEventListener('click', handler);
            return btn;
        };
        if (!task || isTerminalWithResult(task.status) || task.status === 'failed'
            || task.status === 'cancelled') {
            const label = task ? '重新分析' : '开始分析';
            area.append(makeBtn(label, 'btn-primary', onStart, !configured));
        }
        if (task && isActiveStatus(task.status)) {
            area.append(makeBtn('取消', 'btn-secondary', onCancel));
        }
        if (task && (task.status === 'interrupted' || task.status === 'partial'
            || task.status === 'failed')) {
            area.append(makeBtn('继续执行', 'btn-primary', onResume, !configured));
        }
    }

    // auto_fold 开关：仅经 API 持久化偏好 + 本地重渲染展示，绝不发起模型请求。
    async function onAutoFoldToggle(event) {
        const checked = !!(event && event.target && event.target.checked);
        const pref = state.preference;
        // 无偏好行时 store 默认 revision 0（auto_fold 默认 true），CAS 从 0 起安全创建。
        const expected = pref ? pref.revision : 0;
        await persistPreference({ auto_fold: checked, expected_revision: expected });
        // 折叠只改展示：若已有结果，按新偏好重渲染，不触发任何网络模型调用。
        if (state.result) renderResult();
    }

    async function persistPreference(patch) {
        // 捕获发起时的 bvid/seq：await 回来若已切 BV，不把结果写到新 BV 的状态。
        const bvid = state.bvid;
        const seq = state.fetchSeq;
        try {
            const body = Object.assign({ bvid }, patch);
            const resp = await apiWrite('/preferences', 'PUT', body);
            if (seq !== state.fetchSeq || bvid !== state.bvid) return;
            state.preference = (resp && resp.data) || state.preference;
        } catch (error) {
            // 冲突(409)多因并发改版本：重新拉偏好让用户看到最新值（仍按捕获 BV 核对）。
            const refreshed = await fetchPreference(bvid).catch(() => null);
            if (seq === state.fetchSeq && bvid === state.bvid && refreshed && refreshed.data) {
                state.preference = refreshed.data;
            }
            showAppAlert(`保存偏好失败：${error.message}`);
        }
    }

    // 显式开始：点击时即捕获 bvid/seq/pf/identity，同意模态是异步窗口，
    // 其间用户可能切到别的 BV——await 后必须先核对捕获的 seq/bvid 仍当前，
    // 再用**捕获值**（而非 state.*）写同意，避免把旧 identity 越界写到新 BV。
    async function onStart() {
        // 并发/双击护栏：发起期间按钮置忙 + 同一 BV 复用同一 request_key，
        // 直到任务明确创建成功/失败才释放，防止双击产生两个计费任务。
        if (state.starting) return;
        const bvid = state.bvid;
        const seq = state.fetchSeq;
        const pf = state.preflight || {};
        const identity = pf.provider_identity || pf.model_identity || null;
        if (!bvid) return;
        state.starting = true;
        setActionsBusy(true);
        try {
            if (!hasValidConsent(state.preference, pf)) {
                const agreed = await showConsentModal(pf);
                // await 后先核对：期间切了 BV / 重新加载则放弃，不给新 BV 写旧同意。
                if (seq !== state.fetchSeq || bvid !== state.bvid) return;
                if (!agreed) return;
                const expected = state.preference ? state.preference.revision : 0;
                try {
                    const resp = await apiWrite('/preferences', 'PUT', {
                        bvid,
                        expected_revision: expected,
                        consent_version: CONSENT_VERSION,
                        model_identity: identity,
                    });
                    if (seq !== state.fetchSeq || bvid !== state.bvid) return;
                    state.preference = (resp && resp.data) || state.preference;
                } catch (error) {
                    showAppAlert(`保存同意失败：${error.message}`);
                    return;
                }
            }
            if (seq !== state.fetchSeq || bvid !== state.bvid) return;
            await createTask(bvid, seq);
        } finally {
            state.starting = false;
            setActionsBusy(false);
        }
    }

    // 同一 BV 的发起 request_key 稳定复用：双击/重试不产生第二个计费任务；
    // 仅当任务进入终态（失败/取消）或切换 BV 时才换新 key 允许明确重试。
    function stableRequestKey(bvid) {
        if (!state.requestKey || state.requestKeyBvid !== bvid) {
            state.requestKey = `${bvid}-${Date.now()}`;
            state.requestKeyBvid = bvid;
        }
        return state.requestKey;
    }

    async function createTask(bvid, seq) {
        const requestKey = stableRequestKey(bvid);
        try {
            const resp = await apiWrite('/tasks', 'POST', {
                bvid,
                limit: state.limit,
                consent_version: CONSENT_VERSION,
                request_key: requestKey,
            });
            if (seq !== state.fetchSeq || bvid !== state.bvid) return;
            state.task = (resp && resp.data) || null;
            renderOverview();
            if (state.task && state.task.task_id) startPolling(state.task.task_id, seq);
        } catch (error) {
            // 创建失败：释放 request_key，允许用户显式重试（store 对新 key 重新计费前仍需同意闸门）。
            state.requestKey = null;
            state.requestKeyBvid = null;
            showAppAlert(`发起分析失败：${error.message}`);
        }
    }

    // 发起期间禁用动作区按钮，避免双击/并发点击。
    function setActionsBusy(busy) {
        const area = el('semantic-actions');
        if (!area || !area.querySelectorAll) return;
        area.querySelectorAll('button').forEach(btn => { btn.disabled = !!busy; });
    }

    async function onCancel() {
        const task = state.task;
        if (!task || !task.task_id) return;
        // 点击时即捕获，确认框是异步窗口，期间可能切 BV。
        const bvid = state.bvid;
        const seq = state.fetchSeq;
        const taskId = task.task_id;
        const revision = task.revision;
        const ok = await showAppConfirm('确定取消本次语义折叠分析吗？已产生的用量不会退回。', {
            title: '取消分析', confirmText: '取消分析', cancelText: '继续等待', danger: true,
        });
        // await 确认框后若已切 BV，不对旧任务结果改写新 BV 状态。
        if (!ok || seq !== state.fetchSeq || bvid !== state.bvid) return;
        try {
            const resp = await apiWrite(`/tasks/${encodeURIComponent(taskId)}/cancel`, 'POST', {
                expected_revision: revision,
            });
            if (seq !== state.fetchSeq || bvid !== state.bvid) return;
            stopPolling();
            state.task = (resp && resp.data) || state.task;
            // 取消是终态：释放 request_key，允许之后显式重新发起。
            state.requestKey = null;
            state.requestKeyBvid = null;
            renderOverview();
        } catch (error) {
            showAppAlert(`取消失败：${error.message}`);
        }
    }

    // 继续执行：必须显式确认“未知结果”（中断期间的在途请求结果不可知）。
    async function onResume() {
        const task = state.task;
        if (!task || !task.task_id) return;
        // 点击时即捕获，确认框是异步窗口，期间可能切 BV。
        const bvid = state.bvid;
        const seq = state.fetchSeq;
        const taskId = task.task_id;
        const revision = task.revision;
        const ok = await showAppConfirm(
            '继续执行前请确认：服务中断时可能有一次模型请求结果未知，继续不会重发已成功的部分，但无法判定该次请求是否已计费。确认继续？',
            { title: '继续执行', confirmText: '确认继续', cancelText: '暂不继续' },
        );
        if (!ok || seq !== state.fetchSeq || bvid !== state.bvid) return;
        try {
            const resp = await apiWrite(`/tasks/${encodeURIComponent(taskId)}/resume`, 'POST', {
                expected_revision: revision,
                acknowledge_unknown: true,
            });
            if (seq !== state.fetchSeq || bvid !== state.bvid) return;
            state.task = (resp && resp.data) || state.task;
            renderOverview();
            if (state.task && state.task.task_id) startPolling(state.task.task_id, seq);
        } catch (error) {
            showAppAlert(`继续执行失败：${error.message}`);
        }
    }

    // ---- 轮询：代际令牌防重入；BV 切换 / 页面隐藏 / 终态即停，回到前台时恢复 ----
    function stopPolling() {
        state.polling = false;
        state.pollStalled = false;  // 不把“无进展”提示带到下一个 BV / 下一轮轮询
        state.pollToken += 1;
    }

    function pollInterval(startedAt) {
        return Date.now() - startedAt < POLL_FAST_WINDOW_MS ? POLL_INTERVAL_MS : POLL_SLOW_INTERVAL_MS;
    }

    async function startPolling(taskId, seq) {
        stopPolling();
        const token = state.pollToken;
        state.polling = true;
        state.pollStalled = false;
        const startedAt = Date.now();
        let lastRevision = null;
        let lastChangeAt = startedAt;
        for (;;) {
            // 任一失效条件成立即退出：代际变更 / BV 切换 / 页面隐藏。
            if (token !== state.pollToken || seq !== state.fetchSeq) return;
            if (typeof document !== 'undefined' && document.hidden) { state.polling = false; return; }
            let resp;
            try {
                resp = await fetchTaskStatus(taskId);
            } catch (error) {
                await sleep(pollInterval(startedAt));
                continue;
            }
            if (token !== state.pollToken || seq !== state.fetchSeq) return;
            const task = (resp && resp.data) || null;
            if (!task) { await sleep(pollInterval(startedAt)); continue; }
            const now = Date.now();
            if (task.revision !== lastRevision || task.status !== 'running') {
                lastRevision = task.revision;
                lastChangeAt = now;
            }
            state.pollStalled = task.status === 'running' && now - lastChangeAt >= POLL_STALL_WARN_MS;
            state.task = task;
            renderOverview();
            if (isTerminalWithResult(task.status)) {
                state.polling = false;
                state.pollStalled = false;
                loadResult(taskId, 1, seq);
                return;
            }
            if (task.status === 'failed' || task.status === 'cancelled'
                || task.status === 'interrupted') {
                state.polling = false;
                state.pollStalled = false;
                return;
            }
            await sleep(pollInterval(startedAt));
        }
    }

    // 页面回到前台：当前 BV 的任务若仍在进行，就重新接管轮询（隐藏时已停）。
    function resumePollingIfActive() {
        if (state.polling || !state.task || !state.task.task_id) return;
        if (!isActiveStatus(state.task.status)) return;
        startPolling(state.task.task_id, state.fetchSeq);
    }

    // 任务进度区：阶段 / 进度 / 覆盖 / 预算 / 错误（只读展示）。
    function renderTaskArea() {
        const area = el('semantic-task-area');
        if (!area) return;
        const task = state.task;
        if (!task) { area.innerHTML = ''; return; }
        const coverage = task.coverage || {};
        const budget = task.budget || {};
        // progress 是 0..1 的标量（store._state_dto: float(task.progress)），不是对象。
        const ratio = Number(task.progress);
        const statusLabel = STATUS_LABELS[task.status] || task.status;
        let html = `<div class="semantic-task-status">状态：<strong>${text(statusLabel)}</strong>`;
        if (task.phase) html += ` · 阶段 ${text(task.phase)}`;
        if (Number.isFinite(ratio) && ratio > 0) {
            html += ` · ${escapeHtml(String(Math.round(Math.min(1, ratio) * 100)))}%`;
        }
        html += '</div>';
        // 跨批覆盖：service 输出 pairs_visited / pairs_unvisited（-1 表示未知）。
        if (coverage.pairs_visited !== undefined || coverage.pairs_unvisited !== undefined) {
            const visited = Number(coverage.pairs_visited) || 0;
            const unvisited = Number(coverage.pairs_unvisited);
            const totalText = Number.isFinite(unvisited) && unvisited >= 0
                ? String(visited + unvisited) : '未知';
            html += `<p class="semantic-coverage">跨批覆盖：${escapeHtml(String(visited))}/${escapeHtml(totalText)}`;
            if (coverage.oversize_skipped) html += ` · 超限跳过 ${escapeHtml(String(coverage.oversize_skipped))}`;
            if (coverage.partial) html += ' · 未完全覆盖';
            html += '</p>';
        }
        // 预算：已结算 / 预约 token（store budget DTO）。
        if (budget.settled_tokens !== undefined || budget.reserved_tokens !== undefined) {
            html += `<p class="semantic-coverage">本次用量：已结算 ${text(budget.settled_tokens, 0)} · 预约 ${text(budget.reserved_tokens, 0)} tokens`;
            if (budget.daily_settled !== undefined) html += ` · 今日累计 ${text(budget.daily_settled, 0)}`;
            html += '</p>';
        }
        if (task.status === 'partial') {
            html += '<p class="semantic-hint">部分完成：受预算 / 组大小限制，仍有候选对未比较，结果可能漏并。</p>';
        }
        if (state.pollStalled && task.status === 'running') {
            html += '<p class="semantic-warn">任务已超过 10 分钟没有新进展，可能卡在模型请求上；页面会继续跟进，也可以取消后重试。</p>';
        }
        if (task.error) {
            const errMsg = (task.error && (task.error.message || task.error.reason)) || task.error;
            html += `<p class="semantic-warn">错误：${text(errMsg)}</p>`;
        }
        area.innerHTML = html;
    }

    const STATUS_LABELS = {
        queued: '排队中', running: '分析中', completed: '已完成', partial: '部分完成',
        failed: '失败', cancelled: '已取消', interrupted: '已中断',
    };

    // ---- 结果加载与渲染 ----
    // taskId/bvidGuard：隔离结果请求代际——旧任务（重新分析后）或已切 BV 的迟到响应不得回写。
    async function loadResult(taskId, page, seq, bvidGuard) {
        const bvid = bvidGuard !== undefined ? bvidGuard : state.bvid;
        const requestId = ++state.resultRequest;
        try {
            const resp = await fetchResult(taskId, page, 50);
            // 同 seq 还不够：重新分析沿用同 seq，需同时校验当前任务仍是本次请求的 taskId。
            if (requestId !== state.resultRequest || seq !== state.fetchSeq || bvid !== state.bvid) return;
            if (!state.task || String(state.task.task_id) !== String(taskId)) return;
            state.result = (resp && resp.data) || null;
            state.resultPage = page;
            // 偏好 CAS 版本只认 result.preference.revision（视频偏好修订）；
            // 结果顶层 revision 是任务 run 修订，二者几乎必然不等，绝不能用它覆盖偏好版本，
            // 否则 undo/split/allow 与 auto_fold PUT 会持续 409。
            if (state.result && state.result.preference) state.preference = state.result.preference;
            renderResult();
        } catch (error) {
            if (requestId !== state.resultRequest || seq !== state.fetchSeq || bvid !== state.bvid) return;
            setHtml('semantic-result-area', `<p class="semantic-warn">加载结果失败：${escapeHtml(error.message)}</p>`);
        }
    }

    function renderResult() {
        const area = el('semantic-result-area');
        if (!area) return;
        const result = state.result;
        if (!result) { area.innerHTML = ''; return; }
        const autoFold = state.preference ? !!state.preference.auto_fold : true;
        const groups = asArray(result.groups);
        const items = asArray(result.items);
        const folded = {};
        if (autoFold) {
            groups.forEach(group => asArray(group.current_folded_rpids).forEach(rpid => {
                folded[String(rpid)] = true;
            }));
        }
        // 在自动折叠视图中从原始分页列表隐藏服务端明确折叠的成员；
        // 关闭自动折叠时保留全部原文平铺，分组证据区始终完整可查看。
        const displayItems = autoFold
            ? items.filter(item => !folded[String(item && item.rpid)])
            : items;

        // 折叠视图（仅当 auto_fold 开启）：把“实际被折叠”的成员 rpid（current_folded_rpids，
        // 已排除受保护与 stale）聚合，列出代表项 + 折叠条数；其余所有入选原文平铺在 items。
        let html = '';
        if (autoFold) {
            html += '<section class="semantic-foldview"><h4>折叠视图</h4>';
            html += '<p class="semantic-hint">自动折叠已开启：每组仅代表项计入展示，受保护 / 内容已变化的成员仍独立显示。</p>';
            html += '</section>';
        } else {
            html += '<section class="semantic-foldview"><h4>平铺视图</h4>';
            html += '<p class="semantic-hint">自动折叠已关闭：所有评论平铺独立展示；下方分组卡片仅作为分析证据。</p>';
            html += '</section>';
        }

        // 分析证据区：等价分组卡片（无论折叠开关都展示，供人工复核）。
        html += '<section class="semantic-groups">';
        html += `<h4>等价分组证据（${groups.length}）</h4>`;
        if (!groups.length) {
            html += '<p class="semantic-hint">暂无等价分组。</p>';
        } else {
            groups.forEach(group => { html += renderGroup(group, autoFold); });
        }
        html += '</section>';

        // 全量原文：所有入选评论（含未分组 / 被排除 / 不确定）分页平铺，不隐藏任何成员。
        html += renderItemList(
            autoFold ? '未折叠 / 独立保留' : '全部入选评论',
            'all',
            displayItems,
            result.pagination
        );
        area.innerHTML = html;
        bindResultHandlers();
    }

    // 单个分组证据卡：代表全文 + 归并理由 + 全部成员详情 + 折叠计数。
    // 折叠判断来自 server 的 current_folded_rpids（已排除受保护 / stale），不靠前端猜。
    function renderGroup(group, autoFold) {
        const memberList = asArray(group.members);
        const gid = group.group_id || '';
        const memberCount = group.member_count !== undefined ? group.member_count : memberList.length;
        const foldedRpids = asArray(group.current_folded_rpids);
        const foldedSet = {};
        foldedRpids.forEach(r => { foldedSet[String(r)] = true; });
        // 代表全文优先用 representative_content，回退到标记代表的成员。
        const repContent = group.representative_content
            || (memberList.find(m => m && m.is_representative) || memberList[0] || {}).content;

        let html = `<div class="semantic-group" data-group="${escapeHtml(String(gid))}">`;
        html += `<div class="semantic-group-head"><span class="semantic-badge">${escapeHtml(String(memberCount))} 条等价</span>`;
        // 折叠计数：实际被折叠的成员数（代表项外）；受保护/stale 不计入。
        if (autoFold) html += ` <span class="semantic-chip">本组折叠 ${escapeHtml(String(foldedRpids.length))} 条</span>`;
        html += ` <span class="semantic-reason">理由：${text(group.reason)}</span>`;
        if (group.stale) html += ' <span class="semantic-chip semantic-chip-warn">组内有内容已变化</span>';
        html += '</div>';
        html += `<p class="semantic-rep">代表：${text(repContent)}</p>`;
        // 成员详情：始终提供完整列表（无分页隐藏）。
        // 自动折叠时证据卡默认收起成员；关闭自动折叠时展开全部原文。
        html += `<details class="semantic-members"${autoFold ? '' : ' open'}><summary>全部成员（${escapeHtml(String(memberList.length))}）</summary>`;
        memberList.forEach(member => { html += renderMemberRow(member, foldedSet); });
        html += '</details>';
        // 组级决策：撤销折叠（保护全组成员，全部独立）/ 重新允许本组折叠（解除全组保护）。
        html += '<div class="semantic-group-actions">';
        html += `<button type="button" class="btn btn-secondary" data-decide="undo_group" data-group="${escapeHtml(String(gid))}">撤销折叠（全部独立）</button>`;
        html += `<button type="button" class="btn btn-secondary" data-decide="allow_group" data-group="${escapeHtml(String(gid))}">重新允许本组折叠</button>`;
        html += '</div></div>';
        return html;
    }

    // 单条成员：完整快照原文 + 状态标记。受保护成员明确显示“已独立”并可「重新允许折叠」；
    // stale 标“内容已变化/已删除”、不计入折叠。所有决策按钮无条件渲染（操作后不消失）。
    function renderMemberRow(member, foldedSet) {
        const m = member || {};
        const rpid = String(m.rpid || '');
        const isFolded = !!(foldedSet && foldedSet[rpid]);
        const flags = [];
        if (m.is_representative) flags.push('代表');
        if (m.keep_separate) flags.push('已独立（受保护）');
        if (m.stale) flags.push('内容已变化/已删除');
        let html = `<div class="semantic-member${m.keep_separate ? ' semantic-member-protected' : ''}">`;
        html += `<span class="semantic-member-text">${text(m.content)}</span>`;
        if (m.sentiment) html += ` <span class="semantic-chip">${text(m.sentiment)}</span>`;
        if (isFolded) html += ' <span class="semantic-chip">已折叠</span>';
        flags.forEach(flag => { html += ` <span class="semantic-chip semantic-chip-warn">${escapeHtml(flag)}</span>`; });
        html += '<span class="semantic-member-actions">';
        if (m.keep_separate) {
            // 已受保护：提供「重新允许折叠」（解除该成员保护）。
            html += `<button type="button" class="btn btn-link" data-decide="allow_member" data-rpid="${escapeHtml(rpid)}">重新允许折叠</button>`;
        } else {
            // 未受保护：提供「拆出」（保护该成员，独立展示）。
            html += `<button type="button" class="btn btn-link" data-decide="split_member" data-rpid="${escapeHtml(rpid)}">拆出（独立）</button>`;
        }
        html += '</span></div>';
        return html;
    }

    // 未分组 / 被排除 / 不确定：全量原文分页列出，不隐藏任何成员。
    function renderItemList(title, key, items, pagination) {
        let html = `<section class="semantic-items"><h4>${escapeHtml(title)}（${items.length}）</h4>`;
        if (!items.length) {
            html += '<p class="semantic-hint">本页无记录。</p>';
        } else {
            items.forEach(item => {
                const it = item || {};
                html += '<div class="semantic-item">';
                html += `<span class="semantic-member-text">${text(it.content)}</span>`;
                if (it.context_status) html += ` <span class="semantic-chip">${text(it.context_status)}</span>`;
                if (it.sentiment) html += ` <span class="semantic-chip">${text(it.sentiment)}</span>`;
                html += '</div>';
            });
        }
        html += renderPagination(pagination);
        html += '</section>';
        return html;
    }

    function renderPagination(pagination) {
        const pg = pagination || {};
        const page = Number(pg.page) || state.resultPage || 1;
        const totalPages = Number(pg.total_pages) || 1;
        if (totalPages <= 1) return '';
        let html = '<div class="semantic-pagination">';
        html += `<button type="button" class="btn btn-link" data-page="prev"${page <= 1 ? ' disabled' : ''}>上一页</button>`;
        html += `<span>第 ${escapeHtml(String(page))} / ${escapeHtml(String(totalPages))} 页</span>`;
        html += `<button type="button" class="btn btn-link" data-page="next"${page >= totalPages ? ' disabled' : ''}>下一页</button>`;
        html += '</div>';
        return html;
    }

    // 事件委托：决策/分页按钮在结果区根节点处理。只绑定一次（innerHTML 重写不移除父监听器，
    // 每次 renderResult 再 addEventListener 会累积，导致一次点击触发多次 POST / reload）。
    function bindResultHandlers() {
        const area = el('semantic-result-area');
        if (!area || area._semanticBound) return;
        area._semanticBound = true;
        area.addEventListener('click', async event => {
            const target = event.target;
            if (!target || typeof target.getAttribute !== 'function') return;
            const action = target.getAttribute('data-decide');
            if (action) {
                await submitDecision(action, target.getAttribute('data-group'), target.getAttribute('data-rpid'));
                return;
            }
            const page = target.getAttribute('data-page');
            if (page && state.task && state.task.task_id) {
                const current = state.resultPage || 1;
                const next = page === 'next' ? current + 1 : Math.max(1, current - 1);
                loadResult(state.task.task_id, next, state.fetchSeq);
            }
        });
    }

    // 手动决策：undo_group 保护全组、split 保护单条、allow_* 解除保护。CAS 视频偏好版本。
    // 点击时捕获 bvid/seq/task_id：await 后若已切 BV / 重新分析，不把结果回写到新上下文。
    // busy 防双击；稳定 request_key（同一目标点击复用）避免重复计费/重复 reload。
    async function submitDecision(action, groupId, rpid) {
        if (state.decisionBusy) return;
        const task = state.task;
        if (!task || !task.task_id) return;
        const bvid = state.bvid;
        const seq = state.fetchSeq;
        const taskId = String(task.task_id);  // router DecisionRequest.task_id 为 StrictStr
        const page = state.resultPage || 1;
        const revision = state.preference ? state.preference.revision : 0;
        const body = {
            bvid,
            task_id: taskId,
            action,
            expected_revision: revision,
            request_key: `${bvid}-${taskId}-${action}-${groupId || rpid || ''}-${revision}`,
        };
        if (groupId) body.group_id = groupId;
        if (rpid) body.rpid = rpid;
        state.decisionBusy = true;
        try {
            const resp = await apiWrite('/decisions', 'POST', body);
            if (seq !== state.fetchSeq || bvid !== state.bvid) return;  // 期间切 BV / 重新分析
            if (resp && resp.data && resp.data.preference) state.preference = resp.data.preference;
            loadResult(taskId, page, seq, bvid);
        } catch (error) {
            // 409 多为版本陈旧：仅当仍在同一上下文时重载，避免把旧结果写进新 BV/任务。
            if (error.status === 409 && seq === state.fetchSeq && bvid === state.bvid) {
                loadResult(taskId, page, seq, bvid);
            }
            showAppAlert(`操作失败：${error.message}`);
        } finally {
            state.decisionBusy = false;
        }
    }

    // 页面隐藏时停止轮询，回到前台时若任务仍在进行就恢复轮询。
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) stopPolling();
            else resumePollingIfActive();
        });
    }

    // 独立 BV 选择：历史数据 / 刷新后也有入口，不依赖本次采集。
    function onLoadClick() {
        const input = el('semantic-bvid');
        const bvid = input ? input.value.trim() : '';
        if (!bvid) {
            showAppAlert('请输入要分析的视频 BV 号');
            return;
        }
        selectBvid(bvid);
    }

    function init() {
        const btn = el('semantic-load-btn');
        if (btn) btn.addEventListener('click', onLoadClick);
        const input = el('semantic-bvid');
        if (input) {
            input.addEventListener('keydown', event => {
                if (event.key === 'Enter') { event.preventDefault(); onLoadClick(); }
            });
        }
    }

    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
        } else {
            init();
        }
    }

    // 对外暴露：app.comment.js 成功采集单视频后调用，把当前 BV 告知本面板。
    if (typeof window !== 'undefined') {
        window.onCommentBvidSelected = selectBvid;
        // 测试 / 调试入口：暴露内部状态与关键函数（不改变运行行为）。
        window.__semanticPanel = {
            state, selectBvid, renderResult, stopPolling, init, onLoadClick, submitDecision,
            resumePollingIfActive,
        };
    }
})();
