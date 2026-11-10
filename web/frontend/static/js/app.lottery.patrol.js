/* 抽奖工具 · AI 伪人巡检
 *
 * 依赖：app.core.js（apiRequest / showLoading / escapeHtml / safeUrl / formatDate）、
 *       app.ui.js（mountAppModal / showAppConfirm / showAppAlert / showToast），须在二者之后加载。
 * 约定：
 * - 所有用户内容走 escapeHtml；弹窗内文本用 textContent；不写内联事件处理器。
 * - 写请求带 X-Local-Token，并显式带 Content-Type（apiRequest 的 headers 会整体覆盖默认值）。
 * - 不用 pollTask（它每次都会重写目标元素且有 30 分钟上限），自写轮询，页面隐藏时暂停。
 * - Chromium 83 兼容：不用 replaceAll、可选链赋值等新语法；randomUUID 带兜底。
 */
(function () {
    'use strict';

    const API_PREFIX = '/lottery/patrol';
    const STORAGE_PREFIX = 'fishtool.lottery.patrol.';
    const PAGE_SIZE = 50;
    const POLL_FAST_MS = 1500;
    const POLL_SLOW_MS = 5000;
    const POLL_FAST_WINDOW_MS = 120000;
    const TERMINAL = { completed: true, failed: true, cancelled: true, interrupted: true };
    const TIER_LABEL = { bot: '高度疑似', suspect: '疑似', human: '较像真人', unknown: '证据不足', whitelist: '白名单' };
    const SOURCE_LABEL = { 'rule': '规则估计', 'hard_rule': '规则估计', 'rule+llm': '规则+AI', 'whitelist': '白名单' };
    const REPORT_LABEL = {
        none: '未处理', queued: '排队中', sending: '提交中', reported: '已举报', duplicate: '已举报过',
        deleted: '评论已删除', failed: '失败', unknown: '结果未知', skipped: '已跳过',
        skipped_rate_limited: '限频未发', cancelled: '已取消', dry_run: '演练', native_opened: '待手动提交',
        native_done: '已手动提交'
    };
    const REPORT_PILL = {
        reported: 'is-ok', duplicate: 'is-ok', native_done: 'is-ok', failed: 'is-error',
        unknown: 'is-warn', skipped_rate_limited: 'is-warn', native_opened: 'is-warn', sending: 'is-warn'
    };
    const MODE_LABEL = { native: '原生', proxy: '代发', dry_run: '演练' };
    const DEFAULT_REPORT_CONTENT = '疑似AI自动生成评论的机器人账号，请核实';

    const state = {
        localToken: null,
        preflight: null,
        scanId: null,
        scan: null,
        page: 1,
        filters: { tier: '', min_score: '', focus_only: false, disagreement_only: false, report_status: '', keyword: '', sort: 'score_desc' },
        rows: [],
        total: 0,
        totalPages: 1,
        selected: {},          // rpid -> 摘要
        tasks: { scan: '', judge: '', report: '' },
        pollTokens: { scan: 0, judge: 0, report: 0 },
        starting: { scan: false, judge: false, report: false },
        requestKeys: { judge: '', report: '' },
        fetchSeq: 0,
        lastRefreshPct: { judge: 0, report: 0 },
        enteredOnce: false,
        view: 'draw',
        memory: {}
    };

    // ============ 基础工具 ============

    function $(id) { return document.getElementById(id); }

    function newRequestKey(prefix) {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
        return prefix + '-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    }

    function storageGet(key) {
        try {
            if (window.sessionStorage) return window.sessionStorage.getItem(STORAGE_PREFIX + key) || '';
        } catch (error) { /* 隐私模式等不可用时回退内存 */ }
        return state.memory[key] || '';
    }

    function storageSet(key, value) {
        state.memory[key] = value || '';
        try {
            if (!window.sessionStorage) return;
            if (value) window.sessionStorage.setItem(STORAGE_PREFIX + key, value);
            else window.sessionStorage.removeItem(STORAGE_PREFIX + key);
        } catch (error) { /* 同上 */ }
    }

    function qs(query) {
        const params = new URLSearchParams();
        Object.keys(query || {}).forEach(function (key) {
            const value = query[key];
            if (value === '' || value === null || value === undefined || value === false) return;
            params.append(key, String(value));
        });
        const text = params.toString();
        return text ? '?' + text : '';
    }

    async function getLocalToken(force) {
        if (state.localToken && !force) return state.localToken;
        try {
            // 复用语义折叠的 token 端点：进程内同一个 token，AstrBot 代理已对它做本地应答。
            const response = await apiRequest('/comment/semantic/local-token', {}, false);
            state.localToken = (response.data || {}).token || null;
        } catch (error) {
            state.localToken = null;
        }
        return state.localToken;
    }

    function apiRead(path, query) {
        return apiRequest(API_PREFIX + path + qs(query), {}, false);
    }

    async function apiWrite(path, method, body) {
        for (let attempt = 0; attempt < 2; attempt += 1) {
            const token = await getLocalToken(attempt > 0);
            const headers = Object.assign(body ? { 'Content-Type': 'application/json' } : {}, token ? { 'X-Local-Token': token } : {});
            try {
                return await apiRequest(API_PREFIX + path, {
                    method: method,
                    headers: headers,
                    body: body ? JSON.stringify(body) : undefined
                }, false);
            } catch (error) {
                const detail = error && error.detail;
                if (attempt === 0 && error.status === 403 && detail === 'local_token_required') continue;
                throw error;
            }
        }
        return null;
    }

    function errorCode(error) {
        const detail = error && error.detail;
        if (detail && typeof detail === 'object' && detail.error_code) return detail.error_code;
        return typeof detail === 'string' ? detail : '';
    }

    function errorMessage(error) {
        const detail = error && error.detail;
        if (detail && typeof detail === 'object' && detail.message) return String(detail.message);
        return (error && error.message) || '操作失败';
    }

    function fmtSeconds(seconds) {
        const s = Math.max(0, Number(seconds) || 0);
        if (s < 60) return Math.max(1, Math.round(s)) + ' 秒';
        return Math.round(s / 60) + ' 分钟';
    }

    function clampPct(value) {
        const n = Number(value) || 0;
        return Math.max(0, Math.min(100, Math.round(n)));
    }

    function notice(html) {
        return '<div class="analysis-notice" role="status">' + html + '</div>';
    }

    function errorBlock(text) {
        return '<p class="analysis-error">' + escapeHtml(text) + '</p>';
    }

    function setProgress(html) {
        const host = $('patrol-progress');
        if (host) host.innerHTML = html || '';
    }

    // ============ 视图切换与初始化 ============

    function setView(view) {
        state.view = view === 'patrol' ? 'patrol' : 'draw';
        const isPatrol = state.view === 'patrol';
        const draw = $('lottery-draw-view');
        const panel = $('lottery-patrol-panel');
        if (draw) draw.hidden = isPatrol;
        if (panel) panel.hidden = !isPatrol;
        [['lottery-view-draw', !isPatrol], ['lottery-view-patrol', isPatrol]].forEach(function (pair) {
            const button = $(pair[0]);
            if (!button) return;
            button.classList.toggle('btn-primary', pair[1]);
            button.classList.toggle('btn-secondary', !pair[1]);
            button.setAttribute('aria-pressed', pair[1] ? 'true' : 'false');
        });
        storageSet('view', state.view);
        if (isPatrol) onEnterPatrol();
    }

    async function onEnterPatrol() {
        if (!state.enteredOnce) {
            state.enteredOnce = true;
            const scanId = Number(storageGet('scan_id')) || 0;
            if (scanId) state.scanId = scanId;
        }
        await refreshPreflight();
        if (state.preflight && state.preflight.prefs && !state.preflight.prefs.risk_notice_ok) {
            const agreed = await ensureRiskNotice();
            if (!agreed) {
                setView('draw');
                return;
            }
        }
        if (state.scanId && !state.scan) await loadScan(state.scanId);
        resumeActiveTasks();
        loadLedger();
    }

    async function refreshPreflight(target) {
        const seq = (state.preflightSeq = (state.preflightSeq || 0) + 1);
        const targetSnapshot = target || '';
        let data;
        try {
            const response = await apiRead('/preflight', { target: targetSnapshot });
            data = response.data || {};
        } catch (error) {
            if (seq !== state.preflightSeq) return null;
            if (error.status === 404) {
                setProgress(notice('AI 伪人巡检已在配置中关闭（patrol.enabled）。'));
                ['patrol-scan-button', 'patrol-judge-button', 'patrol-report-button'].forEach(function (id) {
                    const b = $(id);
                    if (b) b.disabled = true;
                });
                return null;
            }
            setProgress(errorBlock('读取巡检状态失败：' + errorMessage(error)));
            return null;
        }
        // 已有更新的 preflight 请求发出时，丢弃本次过期响应，避免覆盖用户的最新操作。
        if (seq !== state.preflightSeq) return null;
        state.preflight = data;
        const badge = $('patrol-account-badge');
        const login = data.login || {};
        if (badge) {
            if (login.ok) {
                badge.className = 'lottery-badge success';
                badge.textContent = '当前账号：' + (login.uname || '已登录') + (login.level !== null && login.level !== undefined ? ' Lv' + Number(login.level) : '');
            } else {
                badge.className = 'lottery-badge secondary';
                badge.textContent = '未登录';
            }
        }
        const replies = $('patrol-include-replies');
        if (replies && !replies.dataset.touched && data.prefs) replies.checked = !!data.prefs.include_replies;
        const hint = $('patrol-eta-hint');
        if (hint) {
            if (data.video) {
                hint.textContent = '《' + (data.video.title || '') + '》约 ' + Number(data.video.reply_count || 0)
                    + ' 条评论；预计耗时：开启楼中楼约 ' + fmtSeconds(data.eta.with_replies)
                    + '，关闭约 ' + fmtSeconds(data.eta.without_replies);
            } else if (data.video_error) {
                hint.textContent = data.video_error;
            } else {
                hint.textContent = '输入 BV 号后显示预计耗时';
            }
        }
        syncButtons();
        return data;
    }

    function syncButtons() {
        const pf = state.preflight || {};
        const login = pf.login || {};
        const llm = pf.llm || {};
        const scanBtn = $('patrol-scan-button');
        const judgeBtn = $('patrol-judge-button');
        const reportBtn = $('patrol-report-button');
        const exportBtn = $('patrol-export-button');
        if (scanBtn) {
            const running = !!state.tasks.scan;
            scanBtn.textContent = running ? '取消扫描' : '开始扫描';
            scanBtn.classList.toggle('btn-primary', !running);
            scanBtn.classList.toggle('btn-secondary', running);
            scanBtn.disabled = !running && !login.ok;
            scanBtn.title = login.ok ? '' : '请先在欢迎页扫码登录 B站';
        }
        if (judgeBtn) {
            const running = !!state.tasks.judge;
            judgeBtn.textContent = running ? '取消甄别' : 'AI 甄别';
            judgeBtn.disabled = !running && (!state.scanId || !llm.configured || !!state.tasks.scan);
            judgeBtn.title = llm.configured ? '' : '请先在配置页填写 AI 的 API Key';
        }
        if (reportBtn) {
            const count = Object.keys(state.selected).length;
            const running = !!state.tasks.report;
            reportBtn.textContent = running ? '取消举报' : (count ? '一键举报（已选 ' + count + '）' : '一键举报');
            reportBtn.disabled = !running && (count === 0 || !!state.tasks.scan || !login.ok);
        }
        if (exportBtn) exportBtn.disabled = !state.scanId;
    }

    // ============ 任务：启动、轮询、取消、恢复 ============

    function stopPolling() {
        Object.keys(state.pollTokens).forEach(function (kind) { state.pollTokens[kind] += 1; });
    }

    function setTask(kind, taskId) {
        state.tasks[kind] = taskId || '';
        storageSet('task.' + kind, taskId || '');
        syncButtons();
    }

    function sleepMs(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

    async function pollLoop(kind, taskId, onTick, onDone) {
        state.pollTokens[kind] += 1;
        const token = state.pollTokens[kind];
        const startedAt = Date.now();
        while (true) {
            if (token !== state.pollTokens[kind] || document.hidden) return;
            let task;
            try {
                const response = await apiRead('/tasks/' + encodeURIComponent(taskId));
                task = response.data || {};
            } catch (error) {
                if (error.status === 404) {
                    task = { status: 'failed', message: '任务记录不存在，请重新发起' };
                } else {
                    await sleepMs(POLL_SLOW_MS);
                    continue;
                }
            }
            if (token !== state.pollTokens[kind]) return;
            if (onTick) onTick(task);
            if (TERMINAL[task.status]) {
                setTask(kind, '');
                if (onDone) await onDone(task);
                return;
            }
            const onPage = $('lottery-page') && $('lottery-page').classList.contains('active') && state.view === 'patrol';
            const fast = onPage && Date.now() - startedAt < POLL_FAST_WINDOW_MS;
            await sleepMs(fast ? POLL_FAST_MS : POLL_SLOW_MS);
        }
    }

    function showTaskProgress(task) {
        showLoading('patrol-progress', {
            progress: Number(task.progress) || 0,
            message: task.message || '处理中',
            eta: task.estimated_seconds
        });
        renderCancelButton();
    }

    function renderCancelButton() {
        const host = $('patrol-progress');
        if (!host) return;
        const kind = state.tasks.judge ? 'judge' : (state.tasks.report ? 'report' : '');
        if (!kind || host.querySelector('[data-patrol-cancel]')) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-secondary btn-sm';
        button.textContent = '取消任务';
        button.setAttribute('data-patrol-cancel', kind);
        button.addEventListener('click', function () { cancelTask(kind); });
        host.appendChild(button);
    }

    async function cancelTask(kind) {
        const taskId = state.tasks[kind];
        if (!taskId) return;
        const text = {
            scan: '已采集的评论会保留，可以稍后重新扫描。',
            judge: '已完成的批次会保留；已产生的 AI 用量费用不退。',
            report: '当前这条提交完成后停止，剩余评论不会提交。'
        }[kind];
        const ok = await showAppConfirm(text, { title: '取消任务', confirmText: '取消任务', cancelText: '继续运行', danger: true });
        if (!ok) return;
        try {
            await apiWrite('/tasks/' + encodeURIComponent(taskId) + '/cancel', 'POST');
        } catch (error) {
            showAppAlert('错误: ' + errorMessage(error));
        }
    }

    async function resumeActiveTasks() {
        const handlers = { scan: onScanDone, judge: onJudgeDone, report: onReportDone };
        const kinds = ['scan', 'judge', 'report'];
        for (let i = 0; i < kinds.length; i += 1) {
            const kind = kinds[i];
            const taskId = state.tasks[kind] || storageGet('task.' + kind);
            if (!taskId) continue;
            let task;
            try {
                task = (await apiRead('/tasks/' + encodeURIComponent(taskId))).data || {};
            } catch (error) {
                storageSet('task.' + kind, '');
                continue;
            }
            if (task.status === 'running') {
                setTask(kind, taskId);
                pollLoop(kind, taskId, function (t) { onTickFor(kind, t); }, handlers[kind]);
            } else if (task.status === 'interrupted') {
                storageSet('task.' + kind, '');
                renderInterrupted(kind, taskId);
            } else {
                storageSet('task.' + kind, '');
            }
        }
    }

    function onTickFor(kind, task) {
        showTaskProgress(task);
        if ((kind === 'judge' || kind === 'report') && state.scanId) {
            const pct = Number(task.progress) || 0;
            if (pct - state.lastRefreshPct[kind] >= 10) {
                state.lastRefreshPct[kind] = pct;
                loadComments(state.page);
            }
        }
    }

    function renderInterrupted(kind, taskId) {
        if (kind === 'report') {
            setProgress(notice('举报任务因服务重启而中断。未提交的评论不会自动续报，请重新确认后提交。'));
            return;
        }
        const label = kind === 'scan' ? '扫描' : 'AI 甄别';
        setProgress(notice('服务重启，' + label + '任务已中断。<div class="patrol-actions">'
            + '<button type="button" class="btn btn-primary btn-sm" data-patrol-resume="1">继续执行</button>'
            + '<button type="button" class="btn btn-secondary btn-sm" data-patrol-drop="1">放弃</button></div>'));
        const host = $('patrol-progress');
        const resume = host.querySelector('[data-patrol-resume]');
        const drop = host.querySelector('[data-patrol-drop]');
        if (resume) resume.addEventListener('click', async function () {
            try {
                await apiWrite('/tasks/' + encodeURIComponent(taskId) + '/resume', 'POST');
                setTask(kind, taskId);
                pollLoop(kind, taskId, function (t) { onTickFor(kind, t); }, kind === 'scan' ? onScanDone : onJudgeDone);
            } catch (error) {
                setProgress(errorBlock('继续执行失败：' + errorMessage(error)));
            }
        });
        if (drop) drop.addEventListener('click', function () { setProgress(''); });
    }

    // ============ 开始扫描 ============

    async function onScanClick() {
        if (state.tasks.scan) return cancelTask('scan');
        if (state.starting.scan) return;
        const target = ($('patrol-target').value || '').trim();
        if (!target) {
            showAppAlert('请输入视频 BV 号或视频链接');
            return;
        }
        state.starting.scan = true;
        try {
            const pf = await refreshPreflight(target);
            if (!pf) return;
            if (!pf.login || !pf.login.ok) {
                showAppAlert('请先在欢迎页扫码登录 B站');
                return;
            }
            if (!pf.video) {
                showAppAlert(pf.video_error || '未获取到视频信息');
                return;
            }
            const replies = $('patrol-include-replies').checked;
            const max = Number($('patrol-max-comments').value) || 500;
            const eta = replies ? pf.eta.with_replies : pf.eta.without_replies;
            const ok = await showAppConfirm(
                '视频：' + (pf.video.title || '') + '\nUP 主：' + (pf.video.owner_name || '') + '\n扫描账号：'
                + (pf.login.uname || '') + '\n采集上限：' + max + ' 条，' + (replies ? '包含' : '不含') + '楼中楼'
                + '\n预计耗时：约 ' + fmtSeconds(eta) + '\n\n扫描只读取公开评论，不会发出任何举报。',
                { title: '确认扫描内容', confirmText: '开始扫描', cancelText: '返回检查' });
            if (!ok) return;
            const body = { target: target, max_comments: max, include_replies: replies, mode: $('patrol-sort-mode').value };
            const response = await apiWrite('/scans', 'POST', body);
            setTask('scan', response.task_id);
            showTaskProgress({ progress: 0, message: '任务已进入队列' });
            pollLoop('scan', response.task_id, function (t) { showTaskProgress(t); }, onScanDone);
        } catch (error) {
            const code = errorCode(error);
            if (code === 'scan_running') setProgress(notice('已有扫描任务在运行，请等待完成或取消后再试。'));
            else if (code === 'login_required') showAppAlert('请先在欢迎页扫码登录 B站');
            else if (code === 'risk_cooling') setProgress(errorBlock('B站风控冷却中，请稍后再试。'));
            else setProgress(errorBlock('发起扫描失败：' + errorMessage(error)));
        } finally {
            state.starting.scan = false;
        }
    }

    async function onScanDone(task) {
        if (task.status === 'completed' && task.result && task.result.scan_id) {
            setProgress('');
            state.scanId = Number(task.result.scan_id);
            storageSet('scan_id', String(state.scanId));
            state.selected = {};
            await loadScan(state.scanId);
            await refreshPreflight();
            return;
        }
        if (task.status === 'cancelled') setProgress(notice('扫描已取消。'));
        else if (task.status === 'interrupted') renderInterrupted('scan', task.task_id);
        else {
            setProgress(errorBlock(task.message || '扫描失败'));
            if (String(task.message || '').indexOf('登录') >= 0) {
                refreshPreflight();
                showToast('登录已失效，巡检任务已停止', { level: 'warning' });
            }
        }
    }

    async function loadScan(scanId) {
        try {
            const response = await apiRead('/scans/' + Number(scanId));
            state.scan = response.data || null;
        } catch (error) {
            if (error.status === 404) {
                state.scanId = null;
                state.scan = null;
                storageSet('scan_id', '');
                syncButtons();
                return;
            }
            setProgress(errorBlock('读取扫描结果失败：' + errorMessage(error)));
            return;
        }
        renderSummary();
        renderFilters();
        await loadComments(1);
        syncButtons();
    }

    // ============ AI 甄别 ============

    async function onJudgeClick() {
        if (state.tasks.judge) return cancelTask('judge');
        if (state.starting.judge || !state.scanId) return;
        const pf = state.preflight || {};
        if (!pf.llm || !pf.llm.configured) {
            const go = await showAppConfirm('还没有配置 AI。配置后才能做 AI 甄别；规则估计的伪人率不受影响。',
                { title: '未配置 AI', confirmText: '去配置页', cancelText: '取消' });
            if (go && typeof navigateTo === 'function') navigateTo('config');
            return;
        }
        state.starting.judge = true;
        try {
            if (!pf.llm.consent_ok) {
                const agreed = await ensureAiConsent();
                if (!agreed) return;
            }
            const scope = $('patrol-judge-scope').value;
            const rpids = scope === 'selected' ? Object.keys(state.selected).map(Number) : [];
            if (scope === 'selected' && !rpids.length) {
                showAppAlert('请先在表格里勾选要甄别的评论');
                return;
            }
            const stats = (state.scan && state.scan.stats) || {};
            const candidates = stats.judge_candidates || {};
            const limit = (pf.settings && pf.settings.judge_max_items) || 300;
            let count = scope === 'selected' ? rpids.length : Number(candidates[scope] || 0);
            const capped = count > limit;
            count = Math.min(count, limit);
            if (!count) {
                showAppAlert('当前范围内没有需要甄别的评论。');
                return;
            }
            const batch = (pf.settings && pf.settings.judge_batch_size) || 8;
            const lines = [
                '将发送 ' + count + ' 条评论，约 ' + Math.ceil(count / batch) + ' 批。',
                '预计用量：约 ' + (count * 120) + '–' + (count * 260) + ' tokens，按你的 API 账户计费。',
                '会发送：评论正文、评论者昵称与签名、等级、是否大会员 / 认证、规则命中类别。',
                '不会发送：UID、rpid、头像、IP 属地、你的 Cookie。'
            ];
            if (capped) lines.push('超出单次上限，只处理伪人率最高的 ' + limit + ' 条。');
            lines.push('任务可以随时取消；已产生的用量费用不退。');
            const ok = await showAppConfirm(lines.join('\n'), { title: '确认 AI 甄别', confirmText: '开始甄别', cancelText: '再想想' });
            if (!ok) return;
            if (!state.requestKeys.judge) state.requestKeys.judge = newRequestKey('judge');
            const response = await apiWrite('/scans/' + Number(state.scanId) + '/judge', 'POST', {
                scope: scope, rpids: rpids, focus: ($('patrol-focus').value || '').slice(0, 600),
                request_key: state.requestKeys.judge
            });
            setTask('judge', response.task_id);
            state.lastRefreshPct.judge = 0;
            showTaskProgress({ progress: 0, message: '任务已进入队列' });
            pollLoop('judge', response.task_id, function (t) { onTickFor('judge', t); }, onJudgeDone);
        } catch (error) {
            const code = errorCode(error);
            if (code === 'consent_required') {
                state.preflight.llm.consent_ok = false;
                setProgress(notice('AI 外发说明需要重新确认（可能更换了模型），请再点一次「AI 甄别」。'));
            } else if (code === 'llm_not_configured') {
                setProgress(errorBlock('请先在配置页填写 AI 的 API Key。'));
            } else {
                setProgress(errorBlock('发起 AI 甄别失败：' + errorMessage(error)));
            }
        } finally {
            state.starting.judge = false;
        }
    }

    async function onJudgeDone(task) {
        state.requestKeys.judge = '';
        if (task.status === 'completed') {
            const r = task.result || {};
            let html = '已甄别 ' + Number(r.judged || 0) + ' 条，缓存命中 ' + Number(r.from_cache || 0) + ' 条。';
            if (r.failed_batches) html += ' 有 ' + Number(r.failed_batches) + ' 批调用失败，这些评论保留规则估计。';
            setProgress(notice(escapeHtml(html)));
            if (r.partial_reason === 'daily_token_limit') {
                setProgress(notice(escapeHtml(html)) + '<p class="monitor-warning">今日 AI 用量已达上限，剩余评论保留规则估计结果。</p>');
            }
        } else if (task.status === 'cancelled') {
            setProgress(notice('AI 甄别已取消，已完成的批次会保留。'));
        } else if (task.status === 'interrupted') {
            renderInterrupted('judge', task.task_id);
        } else {
            setProgress(errorBlock(task.message || 'AI 甄别失败'));
        }
        if (state.scanId) await loadScan(state.scanId);
    }

    // ============ 结果区 ============

    function renderSummary() {
        const host = $('patrol-summary');
        const scan = state.scan;
        if (!host) return;
        if (!scan) {
            host.innerHTML = '<p class="card-empty">输入 BV 号后点击「开始扫描」。未配置 AI 也能得到规则估计的伪人率。</p>';
            return;
        }
        const stats = scan.stats || {};
        const tiers = stats.tiers || {};
        const report = (state.preflight && state.preflight.report) || {};
        let html = '<div class="lottery-summary-strip">'
            + '<span>视频 ' + escapeHtml(scan.title || scan.bvid) + '</span>'
            + '<span>UP ' + escapeHtml(scan.owner_name || '') + '</span>'
            + '<span>评论 ' + (Number(stats.top_level) || 0) + '</span>'
            + '<span>楼中楼 ' + (Number(stats.replies) || 0) + '</span>'
            + '<span>高度疑似 ' + (Number(tiers.bot) || 0) + '</span>'
            + '<span>疑似 ' + (Number(tiers.suspect) || 0) + '</span>'
            + '<span>较像真人 ' + (Number(tiers.human) || 0) + '</span>'
            + '<span>证据不足 ' + (Number(tiers.unknown) || 0) + '</span>'
            + '<span>已 AI 甄别 ' + (Number(stats.judged) || 0) + '</span>'
            + '<span>已举报 ' + (Number(stats.reported) || 0) + '</span>'
            + '<span>24h 剩余代发 ' + (Number(report.daily_remaining) || 0) + '/' + (Number(report.daily_limit) || 0) + '</span>'
            + '</div>';
        if (scan.status === 'interrupted') html += '<p class="monitor-warning">这次扫描被中断，结果可能不完整，建议重新扫描。</p>';
        if (scan.partial) html += '<p class="monitor-warning">采集不完整：' + escapeHtml(scan.partial_reason || '')
            + '。跨评论特征（复制粘贴、互相回复）可能偏低。</p>';
        const tops = stats.top_rules || [];
        if (tops.length) {
            html += '<p class="patrol-hint">命中最多：' + tops.map(function (r) {
                return escapeHtml(r.label) + ' ×' + (Number(r.count) || 0);
            }).join('　') + '</p>';
        }
        if (report.cooldown_seconds > 0) {
            html += '<p class="monitor-warning">代发举报冷却中，约 ' + fmtSeconds(report.cooldown_seconds) + '后可再试。</p>';
        }
        host.innerHTML = html;
    }

    function renderFilters() {
        const host = $('patrol-filters');
        if (!host || !state.scan) return;
        host.hidden = false;
        if (host.dataset.ready) return;
        host.dataset.ready = '1';
        host.innerHTML = ''
            + '<div class="form-group"><label for="patrol-f-tier">档位</label><select id="patrol-f-tier" class="form-control lottery-select" data-filter="tier">'
            + '<option value="">全部</option><option value="bot">高度疑似</option><option value="suspect">疑似</option>'
            + '<option value="human">较像真人</option><option value="unknown">证据不足</option><option value="whitelist">白名单</option></select></div>'
            + '<div class="form-group"><label for="patrol-f-min">最低伪人率</label><input id="patrol-f-min" class="form-control" type="number" min="0" max="100" data-filter="min_score" placeholder="0–100"></div>'
            + '<label class="lottery-toggle lottery-vip-toggle"><input type="checkbox" data-filter="focus_only"><span>仅命中重点词</span></label>'
            + '<label class="lottery-toggle lottery-vip-toggle"><input type="checkbox" data-filter="disagreement_only"><span>仅分歧</span></label>'
            + '<div class="form-group"><label for="patrol-f-status">举报状态</label><select id="patrol-f-status" class="form-control lottery-select" data-filter="report_status">'
            + '<option value="">全部</option><option value="none">未处理</option><option value="native_opened">待手动提交</option>'
            + '<option value="reported">已举报</option><option value="failed">失败</option></select></div>'
            + '<div class="form-group"><label for="patrol-f-keyword">关键词 / 排序</label><input id="patrol-f-keyword" class="form-control" type="text" maxlength="50" data-filter="keyword" placeholder="昵称或内容"></div>'
            + '<div class="form-group"><label for="patrol-f-sort">排序</label><select id="patrol-f-sort" class="form-control lottery-select" data-filter="sort">'
            + '<option value="score_desc">伪人率从高到低</option><option value="time_desc">最新评论</option><option value="like_desc">点赞最多</option></select></div>';
        let timer = null;
        host.addEventListener('input', onFilterChange);
        host.addEventListener('change', onFilterChange);
        function onFilterChange(event) {
            const el = event.target;
            const key = el && el.getAttribute('data-filter');
            if (!key) return;
            state.filters[key] = el.type === 'checkbox' ? el.checked : el.value;
            clearTimeout(timer);
            timer = setTimeout(function () { loadComments(1); }, 300);
        }
    }

    async function loadComments(page) {
        if (!state.scanId) return;
        const seq = ++state.fetchSeq;
        const f = state.filters;
        let data;
        try {
            const response = await apiRead('/scans/' + Number(state.scanId) + '/comments', {
                page: page || 1, page_size: PAGE_SIZE, tier: f.tier, min_score: f.min_score,
                focus_only: f.focus_only ? 'true' : '', disagreement_only: f.disagreement_only ? 'true' : '',
                report_status: f.report_status, keyword: (f.keyword || '').trim(), sort: f.sort
            });
            data = response.data || {};
        } catch (error) {
            if (seq === state.fetchSeq) $('patrol-table-host').innerHTML = errorBlock('读取评论失败：' + errorMessage(error));
            return;
        }
        if (seq !== state.fetchSeq) return;
        state.rows = data.items || [];
        state.total = Number(data.total) || 0;
        state.totalPages = Number(data.total_pages) || 1;
        state.page = Number(data.page) || 1;
        renderTable();
        renderPager();
    }

    function renderTable() {
        const host = $('patrol-table-host');
        if (!host) return;
        const rows = state.rows.map(function (c) { return renderRow(c, false); }).join('');
        host.innerHTML = '<table class="patrol-table"><thead><tr>'
            + '<th><input type="checkbox" id="patrol-select-page" aria-label="全选当前页"></th>'
            + '<th class="patrol-col-score">伪人率</th><th class="patrol-col-user">用户</th>'
            + '<th>评论内容与证据</th><th class="patrol-col-status">状态</th></tr></thead><tbody>'
            + (rows || '<tr><td colspan="5"><p class="card-empty">没有符合筛选条件的评论。</p></td></tr>')
            + '</tbody></table>';
        bindTable(host);
    }

    function selectable(c) {
        return ['reported', 'duplicate', 'sending'].indexOf(c.report_status) < 0;
    }

    function renderRow(c, isChild) {
        const rpid = Number(c.rpid) || 0;
        const tier = TIER_LABEL[c.tier] ? c.tier : 'human';
        const score = clampPct(c.final_score);
        const classes = [];
        if (c.is_reply || isChild) classes.push('patrol-row-reply');
        if (c.disagreement) classes.push('patrol-row-disagree');
        const checked = state.selected[rpid] ? ' checked' : '';
        const disabled = selectable(c) ? '' : ' disabled';
        const lv = c.level === null || c.level === undefined ? '?' : Number(c.level);
        const lvClass = c.level === null || c.level === undefined ? 0 : Number(c.level);
        const hits = (c.rule_hits || []).filter(function (h) { return Number(h.weight) !== 0; });
        let evidence = '';
        if (hits.length) {
            evidence = '<ul class="patrol-hits">' + hits.map(function (h) {
                const w = Number(h.weight) || 0;
                return '<li>' + escapeHtml(h.label) + '（' + (w > 0 ? '+' : '') + w + '）</li>';
            }).join('') + '</ul>';
        }
        if (c.ai_score !== null && c.ai_score !== undefined) {
            evidence += '<p class="patrol-ai-reason">AI：' + escapeHtml(c.ai_reason || '无理由') + '（规则 '
                + (Number(c.rule_score) || 0) + ' / AI ' + Number(c.ai_score) + '）</p>';
        }
        const replies = c.replies || [];
        if (!isChild && replies.length) {
            evidence += '<button type="button" class="btn btn-secondary btn-sm patrol-replies-toggle" data-toggle-replies="'
                + rpid + '" aria-expanded="false">展开楼中楼（' + replies.length + '）</button>';
        }
        const status = c.report_status || 'none';
        let statusHtml = '<span class="tag-pill ' + (REPORT_PILL[status] || '') + '">' + escapeHtml(REPORT_LABEL[status] || '未处理') + '</span>';
        const sign = c.sign ? '<small>签名：' + escapeHtml(c.sign) + '</small>' : '';
        return '<tr class="' + classes.join(' ') + '" data-rpid="' + rpid + '">'
            + '<td><input type="checkbox" data-select="' + rpid + '"' + checked + disabled + ' aria-label="选择 ' + escapeHtml(c.uname || '') + ' 的评论"></td>'
            + '<td><span class="patrol-score is-' + tier + '">' + score + '%</span>'
            + '<span class="patrol-score-bar is-' + tier + '"><span style="width:' + score + '%"></span></span>'
            + '<span class="patrol-score-meta">' + escapeHtml(TIER_LABEL[tier]) + ' · ' + escapeHtml(SOURCE_LABEL[c.score_source] || '规则估计')
            + (c.disagreement ? ' · 分歧' : '') + '</span></td>'
            + '<td><strong>' + escapeHtml(c.uname || '') + '</strong><br><span class="lottery-meta-icon level level-' + lvClass + '">Lv' + lv + '</span>' + sign + '</td>'
            + '<td><div class="patrol-content">' + highlight(c.content || '', c.rule_hits || []) + '</div>' + evidence + '</td>'
            + '<td>' + statusHtml + '</td></tr>';
    }

    function highlight(text, hits) {
        let html = escapeHtml(text);
        const snippets = [];
        (hits || []).forEach(function (h) {
            if (h.field !== 'text' || !h.snippet || Number(h.weight) <= 0) return;
            const esc = escapeHtml(h.snippet);
            if (esc && snippets.indexOf(esc) < 0) snippets.push(esc);
        });
        snippets.sort(function (a, b) { return b.length - a.length; });
        const done = [];
        snippets.forEach(function (esc) {
            if (done.some(function (d) { return d.indexOf(esc) >= 0; })) return;
            if (html.indexOf(esc) < 0) return;
            html = html.split(esc).join('<mark>' + esc + '</mark>');
            done.push(esc);
        });
        return html;
    }

    function bindTable(host) {
        if (host._patrolBound) return;
        host._patrolBound = true;
        host.addEventListener('change', function (event) {
            const el = event.target;
            if (el.id === 'patrol-select-page') {
                state.rows.forEach(function (c) {
                    if (!selectable(c)) return;
                    if (el.checked) state.selected[Number(c.rpid)] = summaryOf(c);
                    else delete state.selected[Number(c.rpid)];
                });
                host.querySelectorAll('[data-select]').forEach(function (box) {
                    if (!box.disabled) box.checked = el.checked;
                });
                renderPager();
                syncButtons();
                return;
            }
            const rpid = el.getAttribute && el.getAttribute('data-select');
            if (!rpid) return;
            const row = findRow(Number(rpid));
            if (el.checked && row) state.selected[Number(rpid)] = summaryOf(row);
            else delete state.selected[Number(rpid)];
            renderPager();
            syncButtons();
        });
        host.addEventListener('click', function (event) {
            const toggle = event.target.closest('[data-toggle-replies]');
            if (!toggle) return;
            const rpid = Number(toggle.getAttribute('data-toggle-replies'));
            const parent = state.rows.filter(function (c) { return Number(c.rpid) === rpid; })[0];
            const tr = toggle.closest('tr');
            const expanded = toggle.getAttribute('aria-expanded') === 'true';
            if (expanded) {
                let next = tr.nextElementSibling;
                while (next && next.getAttribute('data-child-of') === String(rpid)) {
                    const drop = next;
                    next = next.nextElementSibling;
                    drop.remove();
                }
                toggle.setAttribute('aria-expanded', 'false');
                toggle.textContent = '展开楼中楼（' + ((parent && parent.replies) || []).length + '）';
                return;
            }
            const temp = document.createElement('tbody');
            temp.innerHTML = ((parent && parent.replies) || []).map(function (c) { return renderRow(c, true); }).join('');
            let anchor = tr;
            Array.prototype.slice.call(temp.children).forEach(function (child) {
                child.setAttribute('data-child-of', String(rpid));
                anchor.parentNode.insertBefore(child, anchor.nextSibling);
                anchor = child;
            });
            toggle.setAttribute('aria-expanded', 'true');
            toggle.textContent = '收起楼中楼';
        });
    }

    function findRow(rpid) {
        for (let i = 0; i < state.rows.length; i += 1) {
            const c = state.rows[i];
            if (Number(c.rpid) === rpid) return c;
            const subs = c.replies || [];
            for (let j = 0; j < subs.length; j += 1) if (Number(subs[j].rpid) === rpid) return subs[j];
        }
        return null;
    }

    function summaryOf(c) {
        return { rpid: Number(c.rpid), mid: Number(c.mid), uname: c.uname || '', content: c.content || '',
            final_score: Number(c.final_score) || 0, tier: c.tier, score_source: c.score_source, disagreement: !!c.disagreement };
    }

    function renderPager() {
        const host = $('patrol-pager');
        if (!host) return;
        if (!state.scanId) { host.innerHTML = ''; return; }
        const count = Object.keys(state.selected).length;
        host.innerHTML = '<button type="button" class="btn btn-secondary btn-sm" data-page="prev"' + (state.page <= 1 ? ' disabled' : '') + '>上一页</button>'
            + '<span>第 ' + state.page + ' / ' + state.totalPages + ' 页 · 共 ' + state.total + ' 条 · 已选 ' + count + ' 条（跨页）</span>'
            + '<button type="button" class="btn btn-secondary btn-sm" data-page="next"' + (state.page >= state.totalPages ? ' disabled' : '') + '>下一页</button>';
        if (!host._patrolBound) {
            host._patrolBound = true;
            host.addEventListener('click', function (event) {
                const btn = event.target.closest('[data-page]');
                if (!btn || btn.disabled) return;
                loadComments(btn.getAttribute('data-page') === 'next' ? state.page + 1 : state.page - 1);
            });
        }
    }

    async function loadLedger() {
        const host = $('patrol-ledger');
        if (!host) return;
        let rows = [];
        try {
            rows = (await apiRead('/reports', { days: 30 })).data || [];
        } catch (error) {
            host.innerHTML = errorBlock('读取台账失败：' + errorMessage(error));
            return;
        }
        if (!rows.length) {
            host.innerHTML = '<p class="card-empty">暂无举报记录。</p>';
            return;
        }
        host.innerHTML = '<table class="patrol-table"><thead><tr><th>时间</th><th>方式</th><th>评论用户</th><th>伪人率</th><th>状态</th><th>说明</th></tr></thead><tbody>'
            + rows.slice(0, 100).map(function (r) {
                const s = r.status || '';
                let action = '';
                if (r.mode === 'native' && (s === 'native_opened' || s === 'native_done')) {
                    action = '<button type="button" class="btn btn-secondary btn-sm" data-mark="' + Number(r.report_id) + '" data-done="'
                        + (s === 'native_opened' ? '1' : '0') + '">' + (s === 'native_opened' ? '标记已提交' : '标记未提交') + '</button>';
                }
                return '<tr><td>' + escapeHtml(formatDate(r.created_at)) + '</td><td>' + escapeHtml(MODE_LABEL[r.mode] || r.mode)
                    + '</td><td>' + escapeHtml(r.target_uname || '') + '</td><td>' + (r.final_score === null || r.final_score === undefined ? '—' : Number(r.final_score) + '%')
                    + '</td><td><span class="tag-pill ' + (REPORT_PILL[s] || '') + '">' + escapeHtml(REPORT_LABEL[s] || s) + '</span> ' + action
                    + '</td><td>' + escapeHtml(r.bili_message || (r.bili_code !== null && r.bili_code !== undefined ? '返回码 ' + r.bili_code : '')) + '</td></tr>';
            }).join('') + '</tbody></table>';
        if (!host._patrolBound) {
            host._patrolBound = true;
            host.addEventListener('click', async function (event) {
                const btn = event.target.closest('[data-mark]');
                if (!btn) return;
                try {
                    await apiWrite('/reports/' + Number(btn.getAttribute('data-mark')) + '/mark', 'POST', { done: btn.getAttribute('data-done') === '1' });
                    loadLedger();
                    loadComments(state.page);
                } catch (error) {
                    showAppAlert('错误: ' + errorMessage(error));
                }
            });
        }
    }

    // ============ 弹窗 ============

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function buildModal(opts) {
        const backdrop = el('div', 'app-modal-backdrop');
        backdrop.setAttribute('role', opts.role || 'dialog');
        backdrop.setAttribute('aria-modal', 'true');
        backdrop.setAttribute('aria-label', opts.title);
        const modal = el('div', ('app-modal patrol-modal ' + (opts.variant || '')).trim());
        modal.appendChild(el('h3', '', opts.title));
        (opts.body || []).forEach(function (node) { modal.appendChild(node); });
        const bar = el('div', 'app-modal-actions');
        (opts.actions || []).forEach(function (a) {
            const b = el('button', a.className, a.text);
            b.type = 'button';
            b.setAttribute('data-act', a.act);
            if (a.disabled) b.disabled = true;
            bar.appendChild(b);
        });
        modal.appendChild(bar);
        backdrop.appendChild(modal);
        return { backdrop: backdrop, modal: modal, bar: bar };
    }

    function openModal(built, handlers) {
        return new Promise(function (resolve) {
            let settled = false;
            const close = mountAppModal(built.backdrop, function () {
                if (!settled) { settled = true; resolve(false); }
            });
            const done = function (value) {
                if (settled) return;
                settled = true;
                resolve(value);
                close();
            };
            built.backdrop.addEventListener('click', function (event) {
                if (event.target === built.backdrop) done(false);
            });
            built.bar.addEventListener('click', async function (event) {
                const btn = event.target.closest('[data-act]');
                if (!btn || btn.disabled) return;
                const act = btn.getAttribute('data-act');
                if (act === 'cancel') return done(false);
                const handler = handlers && handlers[act];
                if (!handler) return done(act);
                btn.disabled = true;
                try {
                    const result = await handler();
                    if (result !== undefined) done(result);
                } finally {
                    btn.disabled = false;
                }
            });
            const cancel = built.bar.querySelector('[data-act="cancel"]');
            if (cancel) cancel.focus();
        });
    }

    function list(items) {
        const ul = el('ul');
        items.forEach(function (text) { ul.appendChild(el('li', '', text)); });
        return ul;
    }

    async function savePrefs(patch) {
        for (let attempt = 0; attempt < 2; attempt += 1) {
            const current = (await apiRead('/preferences')).data || {};
            try {
                const res = await apiWrite('/preferences', 'PUT', Object.assign({ expected_revision: Number(current.revision) || 0 }, patch));
                return res.data;
            } catch (error) {
                if (attempt === 0 && errorCode(error) === 'revision_conflict') continue;
                throw error;
            }
        }
        return null;
    }

    async function ensureRiskNotice() {
        const built = buildModal({
            title: 'AI 伪人巡检 · 使用须知', role: 'alertdialog',
            body: [
                el('p', '', '伪人率是基于公开信息的估计值，不等于身份认定。'),
                list([
                    '扫描使用你当前登录的 B站账号读取公开评论，请求自带限频。',
                    '默认的举报方式是打开 B站页面由你本人提交，FishTool 只做记录。',
                    '由 FishTool 代为提交举报是可选功能，需要在「巡检设置」里单独开启。它使用你的账号发出，频繁或不当举报可能被平台限制，B站用户协议也限制自动化程序访问。',
                    '扫描结果包含他人的昵称、签名和评论，只保存在本机，默认 7 天后自动清理；导出的文件请勿外传。'
                ])
            ],
            actions: [{ act: 'cancel', text: '取消', className: 'btn btn-secondary' },
                { act: 'agree', text: '我已知悉', className: 'btn btn-primary' }]
        });
        const agreed = await openModal(built, {
            agree: async function () {
                try {
                    await savePrefs({ risk_notice: 'agree' });
                    if (state.preflight && state.preflight.prefs) state.preflight.prefs.risk_notice_ok = true;
                    return true;
                } catch (error) {
                    showToast('保存失败：' + errorMessage(error), { level: 'error' });
                    return undefined;
                }
            }
        });
        return agreed === true;
    }

    async function ensureAiConsent() {
        const llm = (state.preflight && state.preflight.llm) || {};
        const built = buildModal({
            title: 'AI 甄别 · 使用须知', role: 'alertdialog',
            body: [
                el('p', 'patrol-warn', '实验性功能 · 伪人率由大模型估计，准确性未经大规模验证，请人工复核。'),
                list([
                    '调用方：' + (llm.model || '未设置模型'),
                    '接口地址：' + (llm.api_base || '未设置'),
                    '计费：按你的 API 账户计费；若该服务位于境外，数据将传输至境外。',
                    '会发送：评论正文、评论者昵称与签名、等级、是否大会员 / 认证、规则命中类别。',
                    '不会发送：UID、rpid、头像、IP 属地、你的 Cookie。',
                    '发起方式：仅在你点「AI 甄别」时发送；更换模型后需要重新同意。'
                ])
            ],
            actions: [{ act: 'cancel', text: '取消', className: 'btn btn-secondary' },
                { act: 'agree', text: '我已知悉并同意', className: 'btn btn-primary' }]
        });
        const agreed = await openModal(built, {
            agree: async function () {
                try {
                    await savePrefs({ ai_consent: 'agree' });
                    if (state.preflight && state.preflight.llm) state.preflight.llm.consent_ok = true;
                    return true;
                } catch (error) {
                    showToast('保存失败：' + errorMessage(error), { level: 'error' });
                    return undefined;
                }
            }
        });
        return agreed === true;
    }

    // ============ 举报确认 ============

    async function onReportClick() {
        if (state.tasks.report) return cancelTask('report');
        if (state.starting.report) return;
        const picked = Object.keys(state.selected).map(function (k) { return state.selected[k]; });
        if (!picked.length || !state.scanId) return;
        state.starting.report = true;
        try {
            await refreshPreflight();
            await openReportModal(picked);
        } finally {
            state.starting.report = false;
        }
    }

    async function openReportModal(picked) {
        const pf = state.preflight || {};
        const report = pf.report || {};
        const login = pf.login || {};
        let reasons = { source: 'fallback', groups: [] };
        try {
            reasons = (await apiRead('/report/reasons', { scan_id: Number(state.scanId) })).data || reasons;
        } catch (error) {
            showToast('读取举报理由失败，代发时将无法选择理由', { level: 'warning' });
        }
        const perAccount = Number(report.per_account_limit) || 1;
        const byMid = {};
        picked.forEach(function (c) { (byMid[c.mid] = byMid[c.mid] || []).push(c); });
        let sameAccountSkip = 0;
        Object.keys(byMid).forEach(function (mid) { sameAccountSkip += Math.max(0, byMid[mid].length - perAccount); });
        const needConfirm = picked.filter(function (c) { return c.disagreement || c.score_source !== 'rule+llm' || c.tier !== 'bot'; });
        const unknownResult = picked.filter(function (c) { return c.report_status === 'unknown'; });
        const countTier = function (t) { return picked.filter(function (c) { return c.tier === t; }).length; };

        const body = [];
        // 方式
        const modeNative = el('label', 'lottery-toggle patrol-mode-option');
        const rNative = el('input'); rNative.type = 'radio'; rNative.name = 'patrol-report-mode'; rNative.value = 'native'; rNative.checked = true;
        modeNative.appendChild(rNative); modeNative.appendChild(el('span', '', '在 B站页面手动举报（推荐）'));
        const modeProxy = el('label', 'lottery-toggle patrol-mode-option');
        const rProxy = el('input'); rProxy.type = 'radio'; rProxy.name = 'patrol-report-mode'; rProxy.value = 'proxy';
        // 代发默认开启：唯一缺的是首次风险同意时仍允许选择，选中时再弹同意框。
        const needsConsentOnly = !report.proxy_available && report.proxy_enabled && !report.proxy_consent_ok
            && (report.proxy_unavailable_reason || '').indexOf('同意') >= 0;
        rProxy.disabled = !report.proxy_available && !needsConsentOnly;
        modeProxy.appendChild(rProxy);
        modeProxy.appendChild(el('span', '', report.dry_run ? '由 FishTool 代为提交（演练模式，不会真的发出）' : '由 FishTool 代为提交'));
        body.push(modeNative, modeProxy);
        if (needsConsentOnly) {
            body.push(el('p', 'patrol-warn', '首次使用代为提交时需要先阅读并同意风险说明。'));
        } else if (!report.proxy_available) {
            body.push(el('p', 'patrol-warn', '代为提交不可用：' + (report.proxy_unavailable_reason || '')));
        }
        rProxy.addEventListener('change', async function () {
            if (!rProxy.checked || !needsConsentOnly || state.proxyConsentGiven) return;
            const ok = await showAppConfirm(
                'FishTool 会在你确认后用当前登录的 B站账号直接提交举报。\n频繁或不当举报可能导致账号被平台限制，B站用户协议也限制自动化程序访问；若触发风控，FishTool 的其他采集功能会暂停至少 15 分钟。\n每次举报仍需逐批确认。',
                { title: '首次使用代为提交', confirmText: '我已了解，继续', cancelText: '改为手动举报', danger: true });
            if (!ok) { rNative.checked = true; rNative.dispatchEvent(new Event('change')); return; }
            try {
                await savePrefs({ proxy_consent: 'agree' });
                state.proxyConsentGiven = true;
            } catch (error) {
                showAppAlert('错误: ' + errorMessage(error));
                rNative.checked = true; rNative.dispatchEvent(new Event('change'));
            }
        });

        body.push(el('p', '', '举报账号：' + (login.uname || '未登录') + (login.level !== null && login.level !== undefined ? ' Lv' + Number(login.level) : '')));
        if (state.scan && state.scan.scanner_mid && login.mid && Number(state.scan.scanner_mid) !== Number(login.mid)) {
            body.push(el('p', 'patrol-danger', '当前登录账号与扫描时不同。'));
        }
        body.push(el('p', '', '高度疑似 ' + countTier('bot') + ' 条 · 疑似 ' + countTier('suspect') + ' 条 · 其他 '
            + (picked.length - countTier('bot') - countTier('suspect')) + ' 条；同一账号只保留 ' + perAccount + ' 条代表评论，预计跳过 ' + sameAccountSkip + ' 条。'));

        // 需逐条核实（代发时）
        const confirmWrap = el('div');
        if (needConfirm.length) {
            confirmWrap.appendChild(el('p', 'patrol-warn', '以下评论未经 AI 甄别、未达“高度疑似”或规则与 AI 结论分歧。代为提交时，只提交你逐条勾选核实的：'));
            const box = el('div', 'patrol-confirm-list');
            needConfirm.forEach(function (c) {
                const label = el('label', 'lottery-toggle');
                const input = el('input'); input.type = 'checkbox'; input.setAttribute('data-confirm', String(c.rpid));
                label.appendChild(input);
                label.appendChild(el('span', '', c.uname + ' · ' + c.final_score + '% · ' + (c.content || '').slice(0, 40)));
                box.appendChild(label);
            });
            confirmWrap.appendChild(box);
        }
        if (unknownResult.length) {
            confirmWrap.appendChild(el('p', 'patrol-warn', '以下评论上次提交结果未知，可能已到达 B站。如需重试，请逐条确认：'));
            const ubox = el('div', 'patrol-confirm-list');
            unknownResult.forEach(function (c) {
                const label = el('label', 'lottery-toggle');
                const input = el('input'); input.type = 'checkbox'; input.setAttribute('data-ack-unknown', String(c.rpid));
                label.appendChild(input);
                label.appendChild(el('span', '', c.uname + ' · 结果未知 · ' + (c.content || '').slice(0, 40)));
                ubox.appendChild(label);
            });
            confirmWrap.appendChild(ubox);
        }
        // 代发专属
        const proxyWrap = el('div');
        const reasonGroup = el('div', 'form-group');
        const reasonLabel = el('label', '', '举报理由' + (reasons.source === 'fallback' ? '（离线兜底表，可能已过期）' : ''));
        reasonLabel.setAttribute('for', 'patrol-report-reason');
        const reasonSelect = el('select', 'form-control lottery-select');
        reasonSelect.id = 'patrol-report-reason';
        reasonSelect.appendChild(new Option('请选择举报理由', ''));
        (reasons.groups || []).forEach(function (g) {
            const og = document.createElement('optgroup');
            og.label = g.group || '理由';
            (g.items || []).forEach(function (it) {
                const opt = new Option(it.name, String(it.id));
                opt.setAttribute('data-required', it.content_required || Number(it.id) === 0 ? '1' : '0');
                og.appendChild(opt);
            });
            reasonSelect.appendChild(og);
        });
        reasonGroup.appendChild(reasonLabel);
        reasonGroup.appendChild(reasonSelect);
        const contentGroup = el('div', 'form-group');
        const contentLabel = el('label', '', '举报说明（选“其他”时必填，2–200 字）');
        contentLabel.setAttribute('for', 'patrol-report-content');
        const contentInput = el('textarea', 'form-control');
        contentInput.id = 'patrol-report-content';
        contentInput.rows = 3;
        contentInput.maxLength = 200;
        contentInput.value = (pf.prefs && pf.prefs.default_report_content) || DEFAULT_REPORT_CONTENT;
        const counter = el('small', 'patrol-counter', contentInput.value.length + '/200');
        contentGroup.appendChild(contentLabel);
        contentGroup.appendChild(contentInput);
        contentGroup.appendChild(counter);
        proxyWrap.appendChild(reasonGroup);
        proxyWrap.appendChild(contentGroup);
        proxyWrap.appendChild(el('p', 'patrol-warn', '举报使用你的账号发出，频繁或不当举报可能被平台限制；若触发风控，FishTool 的其他采集功能也会暂停至少 15 分钟。'
            + '单次最多 ' + (Number(report.max_per_task) || 5) + ' 条，24 小时内还可代发 ' + (Number(report.daily_remaining) || 0) + ' 条；每条间隔 3–6 秒，预计约 '
            + Math.round(Math.min(picked.length, Number(report.max_per_task) || 5) * 4.5) + ' 秒。'));
        proxyWrap.hidden = true;
        confirmWrap.hidden = true;
        body.push(confirmWrap, proxyWrap);

        const ackLabel = el('label', 'lottery-toggle');
        const ack = el('input'); ack.type = 'checkbox';
        ackLabel.appendChild(ack);
        ackLabel.appendChild(el('span', '', '我已逐条核对，以上评论均为疑似机器人'));
        body.push(ackLabel);

        const built = buildModal({
            title: '确认举报 ' + picked.length + ' 条评论', variant: 'patrol-report-modal', body: body,
            actions: [{ act: 'cancel', text: '取消', className: 'btn btn-secondary' },
                { act: 'submit', text: '确认举报', className: 'btn btn-danger', disabled: true }]
        });
        const submitBtn = built.bar.querySelector('[data-act="submit"]');
        const currentMode = function () { return rProxy.checked ? 'proxy' : 'native'; };
        const validate = function () {
            const proxy = currentMode() === 'proxy';
            proxyWrap.hidden = !proxy;
            confirmWrap.hidden = !proxy;
            counter.textContent = contentInput.value.trim().length + '/200';
            let ok = ack.checked;
            if (proxy) {
                const opt = reasonSelect.options[reasonSelect.selectedIndex];
                const required = opt && opt.getAttribute('data-required') === '1';
                const len = contentInput.value.trim().length;
                ok = ok && reasonSelect.value !== '' && (!required || (len >= 2 && len <= 200));
            }
            submitBtn.disabled = !ok;
        };
        [rNative, rProxy, ack, reasonSelect].forEach(function (n) { n.addEventListener('change', validate); });
        contentInput.addEventListener('input', validate);

        if (!state.requestKeys.report) state.requestKeys.report = newRequestKey('report');
        await openModal(built, {
            submit: async function () {
                const mode = currentMode();
                if (mode === 'proxy' && needsConsentOnly && !state.proxyConsentGiven) {
                    showAppAlert('请先同意代为提交的风险说明，或改为在 B站页面手动举报。');
                    return undefined;
                }
                const confirmed = Array.prototype.slice.call(built.modal.querySelectorAll('[data-confirm]'))
                    .filter(function (n) { return n.checked; }).map(function (n) { return Number(n.getAttribute('data-confirm')); });
                const ackUnknown = Array.prototype.slice.call(built.modal.querySelectorAll('[data-ack-unknown]'))
                    .filter(function (n) { return n.checked; }).map(function (n) { return Number(n.getAttribute('data-ack-unknown')); });
                const payload = {
                    rpids: picked.map(function (c) { return c.rpid; }),
                    mode: mode === 'proxy' && report.dry_run ? 'dry_run' : mode,
                    confirmed_rpids: confirmed,
                    acknowledge_unknown_rpids: ackUnknown,
                    request_key: state.requestKeys.report
                };
                if (mode === 'proxy') {
                    payload.reason = Number(reasonSelect.value);
                    payload.content = contentInput.value.trim();
                }
                return submitReport(payload);
            }
        });
    }

    async function submitReport(payload) {
        try {
            if (payload.mode === 'native') {
                const res = await apiWrite('/scans/' + Number(state.scanId) + '/report', 'POST', payload);
                state.requestKeys.report = '';
                renderNativeLinks((res && res.data) || {});
                state.selected = {};
                await loadComments(state.page);
                loadLedger();
                syncButtons();
                return true;
            }
            const res = await apiWrite('/scans/' + Number(state.scanId) + '/report', 'POST', payload);
            setTask('report', res.task_id);
            state.lastRefreshPct.report = 0;
            showTaskProgress({ progress: 0, message: '任务已进入队列' });
            pollLoop('report', res.task_id, function (t) { onTickFor('report', t); }, onReportDone);
            return true;
        } catch (error) {
            const code = errorCode(error);
            if (code === 'account_changed' && !payload.accept_account_change) {
                const go = await showAppConfirm('当前登录账号与扫描时不同，仍用当前账号提交吗？',
                    { title: '账号已变化', confirmText: '仍然提交', cancelText: '取消', danger: true });
                if (go) return submitReport(Object.assign({}, payload, { accept_account_change: true }));
                return false;
            }
            showAppAlert('错误: ' + errorMessage(error));
            return undefined;
        }
    }

    function renderNativeLinks(data) {
        const links = data.links || [];
        const skipped = data.skipped || [];
        let html = '<p>请逐条点开以下链接，在 B站页面的评论“举报”菜单里自己选择理由并提交；提交后可在举报台账里点「标记已提交」。</p>';
        if (links.length) {
            html += '<ul>' + links.map(function (l) {
                const url = safeUrl(l.url);
                return '<li>' + (url ? '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' : '')
                    + escapeHtml(l.uname || ('评论 ' + l.rpid)) + ' · ' + (Number(l.final_score) || 0) + '%' + (url ? '</a>' : '') + '</li>';
            }).join('') + '</ul>';
        } else {
            html += '<p>没有可以举报的评论。</p>';
        }
        if (skipped.length) {
            html += '<p>已跳过 ' + skipped.length + ' 条：' + skipped.slice(0, 5).map(function (s) {
                return escapeHtml((s.uname || s.rpid) + '（' + s.reason + '）');
            }).join('；') + (skipped.length > 5 ? ' 等' : '') + '</p>';
        }
        setProgress(notice(html));
    }

    async function onReportDone(task) {
        state.requestKeys.report = '';
        if (task.status === 'completed') {
            const r = task.result || {};
            const counts = r.counts || {};
            const parts = Object.keys(counts).map(function (k) { return (REPORT_LABEL[k] || k) + ' ' + Number(counts[k]); });
            let html = (r.dry_run ? '演练完成（未真的发出）：' : '举报完成：') + escapeHtml(parts.join('，') || '无');
            if (r.stopped) html += '<br>已停止：' + escapeHtml(r.stopped);
            if ((r.skipped || []).length) html += '<br>跳过 ' + r.skipped.length + ' 条（未达门槛、同账号、已举报等）。';
            setProgress(notice(html));
            if (counts.skipped_rate_limited) showToast('举报过于频繁，已暂停一段时间', { level: 'warning' });
            state.selected = {};
        } else if (task.status === 'cancelled') {
            setProgress(notice('举报已停止，剩余评论没有提交。'));
        } else if (task.status === 'interrupted') {
            renderInterrupted('report', task.task_id);
        } else {
            setProgress(errorBlock(task.message || '举报失败'));
        }
        await refreshPreflight();
        if (state.scanId) await loadScan(state.scanId);
        loadLedger();
    }

    // ============ 巡检设置 / 清除 / 导出 ============

    async function openSettingsModal() {
        let prefs;
        try {
            prefs = (await apiRead('/preferences')).data || {};
        } catch (error) {
            showAppAlert('错误: ' + errorMessage(error));
            return;
        }
        await refreshPreflight();
        const report = (state.preflight && state.preflight.report) || {};
        const body = [];
        const proxyLabel = el('label', 'lottery-toggle');
        const proxyBox = el('input'); proxyBox.type = 'checkbox'; proxyBox.checked = !!prefs.proxy_enabled;
        proxyLabel.appendChild(proxyBox);
        proxyLabel.appendChild(el('span', '', '由 FishTool 代为提交举报'));
        body.push(proxyLabel);
        const reason = report.proxy_unavailable_reason || '';
        const astrbotBlocked = reason.indexOf('AstrBot') >= 0;
        const configBlocked = reason.indexOf('patrol.report.proxy_enabled') >= 0;
        if (astrbotBlocked || configBlocked) {
            proxyBox.disabled = !proxyBox.checked;
            body.push(el('p', 'patrol-warn', reason));
        } else {
            body.push(el('p', '', '默认开启。一键举报时可选择由 FishTool 用你的账号直接提交；首次使用需同意风险说明，每次仍需逐批确认。'));
        }

        const whiteGroup = el('div', 'form-group');
        const whiteLabel = el('label', '', '白名单 UID（每行一个，这些账号的评论记 0 分且不会被举报）');
        whiteLabel.setAttribute('for', 'patrol-set-whitelist');
        const whiteInput = el('textarea', 'form-control');
        whiteInput.id = 'patrol-set-whitelist';
        whiteInput.rows = 3;
        whiteInput.value = (prefs.whitelist_mids || []).join('\n');
        whiteGroup.appendChild(whiteLabel); whiteGroup.appendChild(whiteInput);
        body.push(whiteGroup);

        const kwGroup = el('div', 'form-group');
        kwGroup.appendChild(el('label', '', '自定义关键词（只按字面匹配，权重 −30 到 +30，最多 100 个）'));
        const kwRows = el('div');
        const addKwRow = function (item) {
            const row = el('div', 'patrol-keyword-row');
            const word = el('input', 'form-control'); word.type = 'text'; word.maxLength = 20; word.placeholder = '关键词'; word.value = (item && item.word) || '';
            const field = el('select', 'form-control lottery-select');
            [['name', '昵称'], ['sign', '签名'], ['text', '正文']].forEach(function (p) { field.appendChild(new Option(p[1], p[0])); });
            field.value = (item && item.field) || 'name';
            const weight = el('input', 'form-control'); weight.type = 'number'; weight.min = '-30'; weight.max = '30'; weight.value = String((item && item.weight) || 10);
            const drop = el('button', 'btn btn-secondary btn-sm', '删除'); drop.type = 'button';
            drop.addEventListener('click', function () { row.remove(); });
            row.appendChild(word); row.appendChild(field); row.appendChild(weight); row.appendChild(drop);
            kwRows.appendChild(row);
        };
        (prefs.user_keywords || []).forEach(addKwRow);
        const addBtn = el('button', 'btn btn-secondary btn-sm patrol-replies-toggle', '添加一行'); addBtn.type = 'button';
        addBtn.addEventListener('click', function () { if (kwRows.children.length < 100) addKwRow(null); });
        kwGroup.appendChild(kwRows); kwGroup.appendChild(addBtn);
        body.push(kwGroup);

        const contentGroup = el('div', 'form-group');
        const contentLabel = el('label', '', '默认举报说明（2–200 字）');
        contentLabel.setAttribute('for', 'patrol-set-content');
        const contentInput = el('textarea', 'form-control');
        contentInput.id = 'patrol-set-content'; contentInput.rows = 2; contentInput.maxLength = 200;
        contentInput.value = prefs.default_report_content || DEFAULT_REPORT_CONTENT;
        contentGroup.appendChild(contentLabel); contentGroup.appendChild(contentInput);
        body.push(contentGroup);

        const built = buildModal({
            title: '巡检设置', body: body,
            actions: [{ act: 'cancel', text: '取消', className: 'btn btn-secondary' },
                { act: 'save', text: '保存', className: 'btn btn-primary' }]
        });
        await openModal(built, {
            save: async function () {
                const mids = [];
                const bad = [];
                whiteInput.value.split(/[\s,，]+/).forEach(function (s) {
                    if (!s) return;
                    if (/^\d{1,19}$/.test(s) && Number(s) > 0) mids.push(Number(s)); else bad.push(s);
                });
                if (bad.length) { showAppAlert('错误: 白名单只能填写正整数 UID：' + bad.slice(0, 3).join('、')); return undefined; }
                if (mids.length > 200) { showAppAlert('错误: 白名单最多 200 个 UID'); return undefined; }
                const keywords = [];
                Array.prototype.slice.call(kwRows.children).forEach(function (row) {
                    const inputs = row.querySelectorAll('input, select');
                    const word = (inputs[0].value || '').trim();
                    if (!word) return;
                    const w = Math.max(-30, Math.min(30, Math.round(Number(inputs[2].value) || 0)));
                    if (w !== 0) keywords.push({ word: word.slice(0, 20), field: inputs[1].value, weight: w });
                });
                const text = contentInput.value.trim();
                if (text && (text.length < 2 || text.length > 200)) { showAppAlert('错误: 默认举报说明需为 2–200 字'); return undefined; }
                const patch = { whitelist_mids: mids, user_keywords: keywords, default_report_content: text };
                if (proxyBox.checked !== !!prefs.proxy_enabled) {
                    // 重新打开代发且尚未同意过风险说明时，先确认一次。
                    if (proxyBox.checked && !prefs.proxy_consent_ok) {
                        const ok = await showAppConfirm(
                            '开启后，FishTool 会在你确认后用当前登录的 B站账号直接提交举报。\n频繁或不当举报可能导致账号被平台限制，B站用户协议也限制自动化程序访问；若触发风控，FishTool 的其他采集功能会暂停至少 15 分钟。\n开启后每次举报仍需逐批确认。',
                            { title: '开启代为提交举报', confirmText: '我已了解，开启', cancelText: '暂不开启', danger: true });
                        if (!ok) { proxyBox.checked = false; return undefined; }
                        patch.proxy_consent = 'agree';
                    }
                    patch.proxy_enabled = proxyBox.checked;
                }
                try {
                    await savePrefs(patch);
                } catch (error) {
                    showAppAlert('错误: ' + errorMessage(error));
                    return undefined;
                }
                showToast('巡检设置已保存', { level: 'info' });
                await refreshPreflight();
                return true;
            }
        });
    }

    async function onClearClick() {
        const ok = await showAppConfirm('将删除本机保存的全部巡检扫描结果。举报台账默认保留，便于日后核对。',
            { title: '清除巡检数据', confirmText: '清除', cancelText: '取消', danger: true });
        if (!ok) return;
        try {
            await apiWrite('/data?keep_reports=true', 'DELETE');
        } catch (error) {
            showAppAlert('错误: ' + errorMessage(error));
            return;
        }
        state.scanId = null;
        state.scan = null;
        state.rows = [];
        state.selected = {};
        storageSet('scan_id', '');
        renderSummary();
        $('patrol-table-host').innerHTML = '';
        $('patrol-pager').innerHTML = '';
        $('patrol-filters').hidden = true;
        setProgress('');
        syncButtons();
        showToast('巡检数据已清除', { level: 'info' });
    }

    async function onExportClick() {
        if (!state.scanId) return;
        const ok = await showAppConfirm('导出文件包含他人的昵称和评论内容，请勿外传。默认不包含 UID 和签名。',
            { title: '导出 CSV', confirmText: '导出', cancelText: '取消' });
        if (!ok) return;
        window.location.href = API_BASE + API_PREFIX + '/scans/' + Number(state.scanId) + '/export';
    }

    // ============ 启动 ============

    function init() {
        const draw = $('lottery-view-draw');
        const patrol = $('lottery-view-patrol');
        if (!draw || !patrol || !$('lottery-patrol-panel')) return;
        [draw, patrol].forEach(function (button) {
            button.addEventListener('click', function () { setView(button.getAttribute('data-lottery-view')); });
        });
        $('patrol-scan-button').addEventListener('click', onScanClick);
        $('patrol-judge-button').addEventListener('click', onJudgeClick);
        $('patrol-report-button').addEventListener('click', onReportClick);
        $('patrol-settings-button').addEventListener('click', openSettingsModal);
        $('patrol-export-button').addEventListener('click', onExportClick);
        $('patrol-clear-button').addEventListener('click', onClearClick);
        $('patrol-include-replies').addEventListener('change', function (event) { event.target.dataset.touched = '1'; });
        const target = $('patrol-target');
        let timer = null;
        target.addEventListener('keydown', function (event) { if (event.key === 'Enter') onScanClick(); });
        target.addEventListener('change', function () {
            const value = (target.value || '').trim();
            if (!/BV[0-9A-Za-z]{10}/i.test(value)) return;
            clearTimeout(timer);
            timer = setTimeout(function () { refreshPreflight(value); }, 600);
        });
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) stopPolling();
            else if (state.view === 'patrol') resumeActiveTasks();
        });
        if (storageGet('view') === 'patrol') setView('patrol');
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    // 仅供测试静态定位函数名，生产代码不依赖。
    window.__lotteryPatrol = { state: state, highlight: highlight, renderRow: renderRow, setView: setView, newRequestKey: newRequestKey };
})();
