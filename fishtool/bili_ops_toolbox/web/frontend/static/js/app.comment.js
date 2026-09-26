// ===== app.comment.js —— 拆分自 app.js（评论监控：可视化大屏 / 单BV与账号监控 / 预警），原始行 L307-L789 =====
// 获取或创建指定 ECharts 实例；库缺失时返回 null 并保留兜底文本。
function getCommentChart(elementId) {
    const element = document.getElementById(elementId);
    if (!element || typeof window.echarts === 'undefined') {
        return null;
    }
    const chart = window.echarts.getInstanceByDom(element) || window.echarts.init(element, null, { renderer: 'canvas' });
    commentCharts[elementId] = chart;
    return chart;
}

// 更新图表空状态，空数据时明确提示而不是保留白屏。
function setChartEmpty(elementId, isEmpty) {
    const panel = document.getElementById(elementId)?.closest('.chart-panel');
    if (panel) panel.classList.toggle('is-empty', isEmpty);
}

// 从全局 CSS 设计令牌生成 ECharts 浅色主题，确保图表与页面配色同步。
function getDashboardChartTheme() {
    const styles = getComputedStyle(document.documentElement);
    const cssVar = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    return {
        primary: cssVar('--primary-color', '#d4a373'),
        secondary: cssVar('--secondary-color', '#c8997f'),
        accent: cssVar('--accent-color', '#e8b896'),
        background: cssVar('--bg-secondary', '#ffffff'),
        text: cssVar('--text-primary', '#4a4a4a'),
        mutedText: cssVar('--text-secondary', '#7a7a7a'),
        border: cssVar('--border-color', '#e5ddd5'),
        success: cssVar('--success-color', '#a8c5a1'),
        warning: cssVar('--warning-color', '#e8b896'),
        danger: cssVar('--danger-color', '#d19b89'),
    };
}

// 生成所有评论图表共用的浅色 tooltip 配置。
function getDashboardTooltipTheme(trigger = 'item') {
    const theme = getDashboardChartTheme();
    return {
        trigger,
        backgroundColor: theme.background,
        borderColor: theme.border,
        borderWidth: 1,
        textStyle: { color: theme.text },
    };
}

// 渲染情感分布环图。
function renderSentimentChart(distribution) {
    const chart = getCommentChart('sentiment-chart');
    const theme = getDashboardChartTheme();
    const labels = { positive: '正面', negative: '负面', neutral: '中性', risk: '风险' };
    const colors = {
        positive: theme.success,
        negative: theme.danger,
        neutral: theme.secondary,
        risk: theme.warning,
    };
    const data = Object.entries(distribution || {})
        .filter(([, value]) => Number(value) > 0)
        .map(([name, value]) => ({
            name: labels[name] || name,
            value: Number(value),
            itemStyle: { color: colors[name] || theme.primary },
        }));
    setChartEmpty('sentiment-chart', data.length === 0);
    if (!chart || data.length === 0) return;
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: getDashboardTooltipTheme('item'),
        legend: { bottom: 0, textStyle: { color: theme.text } },
        series: [{
            type: 'pie',
            radius: ['48%', '72%'],
            center: ['50%', '44%'],
            label: { color: theme.text, formatter: '{b}  {d}%' },
            itemStyle: { borderColor: theme.background, borderWidth: 3 },
            data,
        }],
    }, true);
}

// 渲染按日期聚合的评论量柱状图。
function renderTrendChart(dateCounts) {
    const rows = asArray(dateCounts).filter(item => item && item.date);
    const chart = getCommentChart('trend-chart');
    const theme = getDashboardChartTheme();
    setChartEmpty('trend-chart', rows.length === 0);
    if (!chart || rows.length === 0) return;
    chart.setOption({
        tooltip: getDashboardTooltipTheme('axis'),
        grid: { left: 44, right: 18, top: 34, bottom: 42 },
        xAxis: {
            type: 'category',
            data: rows.map(item => item.date),
            axisLabel: { color: theme.text, hideOverlap: true },
            axisLine: { lineStyle: { color: theme.border } },
            axisTick: { lineStyle: { color: theme.border } },
        },
        yAxis: {
            type: 'value',
            minInterval: 1,
            axisLabel: { color: theme.text },
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { lineStyle: { color: theme.border, type: 'dashed' } },
        },
        series: [{
            type: 'bar',
            barMaxWidth: 30,
            data: rows.map(item => Number(item.count) || 0),
            itemStyle: { color: theme.primary, borderRadius: [6, 6, 0, 0] },
        }],
    }, true);
}

// 渲染四层去重原因横向柱图。
function renderDedupChart(reasons) {
    const reasonLabels = {
        same_user_repeat: '同用户复读',
        cross_user_aggregation: '跨用户聚合',
        fuzzy_similarity: '模糊相似',
        time_window_hotspot: '时间窗热点',
    };
    const rows = Object.entries(reasons || {}).map(([name, value]) => ({
        name: reasonLabels[name] || name,
        value: Number(value) || 0,
    }));
    const hasData = rows.some(item => item.value > 0);
    const chart = getCommentChart('dedup-chart');
    const theme = getDashboardChartTheme();
    setChartEmpty('dedup-chart', !hasData);
    if (!chart || !hasData) return;
    chart.setOption(buildDedupChartOption(rows, theme), true);
}

// 构建去重原因横向柱状图的 ECharts 配置。
function buildDedupChartOption(rows, theme) {
    return {
        tooltip: {
            ...getDashboardTooltipTheme('axis'),
            axisPointer: { type: 'shadow', shadowStyle: { color: `${theme.accent}33` } },
        },
        grid: { left: 96, right: 24, top: 22, bottom: 24 },
        xAxis: {
            type: 'value',
            minInterval: 1,
            axisLabel: { color: theme.text },
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { lineStyle: { color: theme.border, type: 'dashed' } },
        },
        yAxis: {
            type: 'category',
            data: rows.map(item => item.name),
            axisLabel: { color: theme.text },
            axisLine: { show: false },
            axisTick: { show: false },
        },
        series: [{
            type: 'bar',
            barMaxWidth: 22,
            data: rows.map(item => item.value),
            itemStyle: { color: theme.secondary, borderRadius: [0, 6, 6, 0] },
        }],
    };
}

// 渲染独立的前十高声量评论细则模块。
function renderVoiceComments(comments) {
    const container = document.getElementById('monitor-top10');
    if (!container) return;
    const sentimentLabels = { positive: '正面', negative: '负面', neutral: '中性', risk: '风险' };
    const rows = asArray(comments).slice(0, 10);
    if (rows.length === 0) {
        container.innerHTML = '<div class="voice-empty">暂无高声量评论，完成评论采集后将在此展示。</div>';
        return;
    }
    container.innerHTML = rows.map((item, index) => {
        const sentiment = ['positive', 'negative', 'neutral', 'risk'].includes(item.sentiment) ? item.sentiment : 'neutral';
        return `
            <article class="voice-item">
                <div class="voice-rank">${String(index + 1).padStart(2, '0')}</div>
                <div class="voice-content">
                    <div class="voice-meta">
                        ${escapeHtml(item.uname || '匿名用户')} · ${Number(item.like) || 0} 赞 · 权重 ${Number(item.voice_weight) || 1}
                        <span class="sentiment-tag ${sentiment}">${sentimentLabels[sentiment]}</span>
                    </div>
                    <p class="voice-comment">${escapeHtml(item.content || '（空评论）')}</p>
                    <div class="voice-reason">清洗结论：${escapeHtml(item.cleaning_reason || '唯一内容，清洗后保留')}</div>
                </div>
                <div class="voice-score">声量 ${Number(item.voice_score) || Number(item.like) || 0}</div>
            </article>`;
    }).join('');
}

// 统一消费后端 visualization 契约并渲染完整大屏。
function renderCommentDashboard(visualization, statusText = '数据已更新') {
    const data = visualization && typeof visualization === 'object' ? visualization : {};
    const dedup = data.dedup_statistics || {};
    const top10 = asArray(data.top10_voice_comments);
    document.getElementById('metric-before').textContent = Number(dedup.before_count) || 0;
    document.getElementById('metric-after').textContent = Number(dedup.after_count) || 0;
    document.getElementById('metric-removed').textContent = Number(dedup.removed_count) || 0;
    document.getElementById('metric-voice').textContent = top10.length;
    document.getElementById('dashboard-data-status').textContent = statusText;

    if (typeof window.echarts === 'undefined') {
        console.error('[评论大屏] 本地 ECharts 加载失败：/static/vendor/echarts.min.js');
        document.getElementById('dashboard-data-status').textContent = '图表库加载失败，已显示文字明细';
    }
    renderSentimentChart(data.sentiment_distribution || {});
    renderTrendChart(data.date_comment_counts || []);
    renderDedupChart(dedup.reasons || {});
    renderVoiceComments(top10);
    commentDashboardLoaded = true;
}

// 进入评论页时加载最近一份已落库数据，解决后端有测试数据但页面始终空白的问题。
async function loadCommentDashboard() {
    const status = document.getElementById('dashboard-data-status');
    if (status) status.textContent = '正在加载历史数据';
    try {
        const data = await apiRequest('/comment/dashboard');
        const result = data.data || {};
        const videoText = result.video ? `${result.video.title} · ${result.video.bvid}` : (result.message || '暂无数据');
        renderCommentDashboard(result.visualization || {}, videoText);
    } catch (error) {
        console.error('[评论大屏] 历史数据加载失败:', error);
        renderCommentDashboard({}, `加载失败：${error.message}`);
    }
}

// 页面尺寸变化时同步调整所有 ECharts 画布。
function resizeCommentCharts() {
    Object.values(commentCharts).forEach(chart => chart?.resize());
}

window.addEventListener('resize', resizeCommentCharts);

// 设置普采按钮的加载状态，防止长时间串行采集期间重复提交请求。
function setMonitorButtonLoading(isLoading, targetType = '') {
    // 按固定 ID 获取按钮；页面尚未渲染时直接跳过，避免空引用报错。
    const button = document.getElementById('monitor-submit-button');
    if (!button) return;

    // 请求进行中禁用按钮，请求结束后恢复可点击状态。
    button.disabled = isLoading;
    // 根据采集目标显示明确状态，账号采集可能耗时更长。
    button.textContent = isLoading
        ? (targetType === 'account' ? '正在采集账号舆情...' : '正在采集视频评论...')
        : '开始监控';
}

/**
 * 渲染原有单视频监控结果，并继续驱动下方评论可视化大屏。
 * @param {string} bvid 用户输入并完成去空白处理的 BV 号。
 * @param {Object} result 后端 `/comment/monitor` 返回的 data 字段。
 * @returns {void} 本函数直接更新页面，不返回业务数据。
 */
function renderSingleVideoMonitorResult(bvid, result) {
    // 缺失字段统一回退为空对象，保证旧接口数据也能正常展示。
    const visualization = result.visualization || {};
    const sentiment = result.sentiment_result;
    // 单视频路径继续使用原有大屏，避免 BV 采集功能发生行为回退。
    renderCommentDashboard(visualization, `${bvid} · ${result.processed_count ?? 0} 条有效评论`);

    // 将完整结果一次写入，减少反复操作 DOM 带来的页面闪动。
    document.getElementById('monitor-result').innerHTML = buildSingleVideoMonitorHtml(result, sentiment);
}

// 构建单视频监控结果 HTML：统计、情感比例与预警逐条展示。
function buildSingleVideoMonitorHtml(result, sentiment) {
    // 所有接口文本都先转义，避免错误或预警文本被当成 HTML 执行。
    let html = `
        <h3>监控结果</h3>
        <p>采集评论: ${Number(result.collected_count) || 0} 条</p>
        <p>处理后: ${Number(result.processed_count) || 0} 条</p>
        ${result.warning ? `<p class="monitor-warning">落库警告: ${escapeHtml(result.warning)}</p>` : ''}
        ${result.error ? `<p class="monitor-warning">${escapeHtml(result.error)}</p>` : ''}
    `;

    // 后端启用情感分析时，补充正面、负面和中性比例。
    if (sentiment) {
        html += `
            <h4>情感分析</h4>
            <p>正面: ${(Number(sentiment.positive_ratio) * 100).toFixed(1)}% |
               负面: ${(Number(sentiment.negative_ratio) * 100).toFixed(1)}% |
               中性: ${(Number(sentiment.neutral_ratio) * 100).toFixed(1)}%</p>
        `;
    }

    // 保留原有逐条预警展示，并对级别及消息执行转义。
    if (Array.isArray(result.alerts) && result.alerts.length > 0) {
        html += '<h4 class="monitor-danger">预警信息</h4>';
        result.alerts.forEach(alert => {
            html += `<p class="monitor-danger">[${escapeHtml(alert.level || 'unknown')}] ${escapeHtml(alert.message || '未提供预警说明')}</p>`;
        });
    } else {
        // 没有预警时给出清晰的正常状态。
        html += '<p class="monitor-success">未发现异常</p>';
    }

    return html;
}

/**
 * 渲染 UP 主账号总体舆情报告，包含汇总指标和每个投稿的采集结果。
 * @param {Object} result 后端 `/comment/monitor/account` 返回的 data 字段。
 * @returns {void} 本函数直接更新账号报告和评论大屏。
 */
function renderAccountMonitorResult(result) {
    // 后端分别返回投稿元数据 videos 与采集结果 results，先转换为稳定数组。
    const videos = Array.isArray(result.videos) ? result.videos : [];
    const videoResults = Array.isArray(result.results) ? result.results : [];
    // 使用 BV 号建立投稿元数据索引，接口调整顺序后仍能正确匹配标题。
    const videoByBvid = new Map(videos.map(video => [String(video.bvid || ''), video]));

    // 账号无投稿或接口返回业务失败时，优先展示后端提供的可读错误。
    if (result.success === false && videoResults.length === 0) {
        renderAccountMonitorError(result);
        return;
    }

    // 每个结果生成一行报告，成功和失败状态使用不同视觉标记。
    const videoRows = videoResults.map((item, index) =>
        buildAccountVideoRow(item, index, videos, videoByBvid)
    ).join('');

    // 汇总数字只按数值方式读取，异常字符串不会进入页面结构。
    const totalVideos = Number(result.total_videos) || videos.length || videoResults.length;
    const successfulCount = Number(result.successful_count) || 0;
    const totalAlerts = Number(result.total_alerts) || 0;
    const monitoredAt = result.monitored_at ? formatDate(result.monitored_at) : '刚刚';

    // 先展示账号级指标，再展示所有视频明细，满足总体舆情快速浏览需求。
    document.getElementById('monitor-result').innerHTML = `
        <section class="account-monitor-report" aria-live="polite">
            <header class="account-report-header">
                <div>
                    <h3>账号舆情报告</h3>
                    <p>UP主：${escapeHtml(result.nickname || result.username || result.uid || '')} · UID：${escapeHtml(result.uid || '')}</p>
                </div>
                <time>${escapeHtml(monitoredAt)}</time>
            </header>
            <div class="account-report-metrics">
                <div><span>总视频数</span><strong>${totalVideos}</strong></div>
                <div><span>成功数</span><strong>${successfulCount}</strong></div>
                <div><span>预警数</span><strong>${totalAlerts}</strong></div>
            </div>
            <div class="account-video-list">
                ${videoRows || '<p class="monitor-warning">接口未返回逐视频结果</p>'}
            </div>
        </section>
    `;

    // 账号接口没有聚合图表时，用首个成功视频填充现有大屏作为明细入口。
    const firstSuccessful = videoResults.find(item => item.success && item.visualization);
    const dashboardText = `UID ${result.uid || ''} · ${successfulCount}/${totalVideos} 个视频采集成功`;
    renderCommentDashboard(firstSuccessful?.visualization || {}, dashboardText);
}

// 账号无投稿或失败时渲染错误报告，并清空大屏。
function renderAccountMonitorError(result) {
    const message = escapeHtml(result.error || '未找到可监控的视频');
    document.getElementById('monitor-result').innerHTML = `
        <section class="account-monitor-report" aria-live="polite">
            <h3>账号舆情报告</h3>
            <p class="monitor-danger">UID ${escapeHtml(result.uid || '')}：${message}</p>
        </section>
    `;
    // 空账号没有可供大屏展示的视频数据。
    renderCommentDashboard({}, `UID ${result.uid || ''} · 暂无可展示数据`);
}

// 构建单条视频采集结果的报告行，优先按 BV 匹配标题。
function buildAccountVideoRow(item, index, videos, videoByBvid) {
    // 优先按 BV 匹配投稿标题，找不到时再按数组位置兜底。
    const metadata = videoByBvid.get(String(item.bvid || '')) || videos[index] || {};
    const bvid = item.bvid || metadata.bvid || `第 ${index + 1} 个视频`;
    const title = metadata.title || '未获取到视频标题';
    const alerts = Array.isArray(item.alerts) ? item.alerts : [];
    const statusText = item.success ? '采集成功' : '采集失败';
    const statusClass = item.success ? 'is-success' : 'is-failed';
    const detailText = item.success
        ? `评论 ${Number(item.collected_count) || 0} 条 · 有效 ${Number(item.processed_count) || 0} 条 · 预警 ${alerts.length} 条`
        : (item.error || '未返回失败原因');

    // 标题、BV 号和详情均来自外部接口，必须转义后再插入 HTML。
    return `
        <article class="account-video-row ${statusClass}">
            <div class="account-video-main">
                <strong>${escapeHtml(title)}</strong>
                <span>${escapeHtml(bvid)}</span>
            </div>
            <span class="account-video-status">${statusText}</span>
            <p>${escapeHtml(detailText)}</p>
        </article>
    `;
}

/**
 * 普采入口：UID 优先走账号接口，否则沿用原有单 BV 采集接口。
 * @returns {Promise<void>} 请求结束后恢复按钮状态并完成结果渲染。
 */
async function monitorVideo() {
    const { bvid, uid, videoLimit, enableDedup, enableSentiment, strategy, targetType } = readMonitorTarget();
    if (!bvid && !uid) return;

    // 结果区和按钮同步进入加载态，让账号串行采集过程有明确反馈。
    showLoading('monitor-result');
    setMonitorButtonLoading(true, targetType);

    // 采集开始前展示进度条容器并启动轮询，进度与后端日志同源。
    ensureProgressWrap();
    if (bvid) startProgressPolling(bvid);

    try {
        if (uid) {
            await monitorAccountByUid(uid, videoLimit, strategy);
            return;
        }
        await monitorSingleVideo(bvid, enableDedup, enableSentiment, strategy);
    } catch (error) {
        // 网络、校验或后端异常统一显示到结果区，并转义错误文本。
        document.getElementById('monitor-result').innerHTML = `<p class="monitor-danger">监控失败: ${escapeHtml(error.message)}</p>`;
    } finally {
        // 无论成功或失败都恢复按钮，允许用户修正输入后再次提交。
        setMonitorButtonLoading(false);
    }
}

// 读取并校验监控输入，非法输入直接提示并返回空目标。
function readMonitorTarget() {
    // 对两个输入值统一去除首尾空白，避免空格导致后端校验失败。
    const bvid = document.getElementById('monitor-bvid').value.trim();
    const uid = document.getElementById('monitor-uid').value.trim();
    // 数量输入只对 UID 账号监控生效，先保留原始文本以便严格校验整数格式。
    const videoLimitInput = document.getElementById('monitor-video-limit').value.trim();

    // 两种目标都为空时不发请求，直接提示用户补充采集目标。
    if (!uid && !bvid) {
        showAppAlert('请输入视频BV号或UP主UID');
        return { bvid: '', uid: '' };
    }
    // UID 只接受纯数字，提前阻止主页链接或非法字符进入账号接口。
    if (uid && !/^\d+$/.test(uid)) {
        showAppAlert('UP主UID只能包含数字');
        return { bvid: '', uid: '' };
    }
    // 账号采集数量必须是 1 到 50 的正整数，非法值不会发送到后端。
    if (uid && !/^\d+$/.test(videoLimitInput)) {
        showAppAlert('视频数量必须是 1-50 的正整数');
        return { bvid: '', uid: '' };
    }
    const videoLimit = Number(videoLimitInput || 10);
    if (uid && (!Number.isInteger(videoLimit) || videoLimit < 1 || videoLimit > 50)) {
        showAppAlert('视频数量必须是 1-50 的正整数');
        return { bvid: '', uid: '' };
    }

    // 原有开关只由单 BV 接口消费；读取方式保持不变。
    const enableDedup = document.getElementById('enable-dedup').checked;
    const enableSentiment = document.getElementById('enable-sentiment').checked;
    // 采集策略下拉栏：normal=快速(100条) / full=全面(全量)
    const strategyEl = document.getElementById('monitor-strategy');
    const strategy = strategyEl ? strategyEl.value : 'normal';
    const targetType = uid ? 'account' : 'video';
    return { bvid, uid, videoLimit, enableDedup, enableSentiment, strategy, targetType };
}

// UID 分支使用用户输入的合法数量；BV 分支完全不读取该字段。
async function monitorAccountByUid(uid, videoLimit, strategy) {
    const data = await apiRequest('/comment/monitor/account', {
        method: 'POST',
        body: JSON.stringify({ uid, video_limit: videoLimit, strategy })
    });
    // 账号响应交由专用报告渲染器处理。
    renderAccountMonitorResult(data.data || {});
}

// BV 分支保持原接口、请求字段和开关语义，确保已有采集流程不受影响。
async function monitorSingleVideo(bvid, enableDedup, enableSentiment, strategy) {
    const data = await apiRequest('/comment/monitor', {
        method: 'POST',
        body: JSON.stringify({
            bvid,
            enable_dedup: enableDedup,
            enable_sentiment: enableSentiment,
            strategy
        })
    });
    // 采集结束后停止轮询，进度条固定为完成态。
    stopProgressPolling();
    // 复用拆出的单视频渲染器展示原有结果和可视化大屏。
    renderSingleVideoMonitorResult(bvid, data.data || {});
}

// ===== 采集进度条 =====

// 进度轮询定时器句柄，全局唯一避免重复轮询。
let monitorProgressTimer = null;

// 在结果区前动态创建进度条容器（首次调用时创建，后续复用）。
function ensureProgressWrap() {
    let wrap = document.getElementById('monitor-progress-wrap');
    if (wrap) return wrap;
    wrap = document.createElement('div');
    wrap.id = 'monitor-progress-wrap';
    wrap.style.display = 'none';
    wrap.innerHTML = `
        <div class="monitor-progress-bar"><div class="monitor-progress-fill"></div></div>
        <span class="monitor-progress-text"></span>
    `;
    const result = document.getElementById('monitor-result');
    if (result && result.parentNode) {
        result.parentNode.insertBefore(wrap, result);
    }
    return wrap;
}

// 启动进度轮询：每 1.5s 拉取一次后端进度，与"已采集 N 条"日志同源。
function startProgressPolling(bvid) {
    stopProgressPolling();
    const wrap = ensureProgressWrap();
    wrap.style.display = 'block';
    const fill = wrap.querySelector('.monitor-progress-fill');
    const text = wrap.querySelector('.monitor-progress-text');
    // 初始状态：等待后端写入第一条进度。
    fill.style.width = '0%';
    text.textContent = '正在启动采集...';
    monitorProgressTimer = setInterval(async () => {
        try {
            // 第三个参数 false：轮询失败不弹全局错误，避免打断采集主流程。
            const resp = await apiRequest(`/comment/monitor/progress?bvid=${encodeURIComponent(bvid)}`, {}, false);
            const p = (resp && resp.data) || {};
            updateProgressUI(p);
            if (p.finished) stopProgressPolling();
        } catch (e) {
            // 轮询请求失败保持现状，下个周期重试。
        }
    }, 1500);
}

// 停止进度轮询；采集完成后由调用方显式触发。
function stopProgressPolling() {
    if (monitorProgressTimer) {
        clearInterval(monitorProgressTimer);
        monitorProgressTimer = null;
    }
}

// 渲染进度条：快速模式按上限算百分比，全面模式显示已采集条数。
function updateProgressUI(p) {
    const wrap = document.getElementById('monitor-progress-wrap');
    if (!wrap) return;
    const fill = wrap.querySelector('.monitor-progress-fill');
    const text = wrap.querySelector('.monitor-progress-text');
    const collected = Number(p.collected) || 0;
    if (p.finished) {
        // 完成态：进度条拉满并显示最终条数。
        fill.style.width = '100%';
        text.textContent = `采集完成，共 ${collected} 条`;
        return;
    }
    if (p.limit) {
        // 快速模式：按已采集/上限计算百分比。
        const pct = Math.min(100, Math.round((collected / p.limit) * 100));
        fill.style.width = pct + '%';
        text.textContent = `已采集 ${collected} / ${p.limit} 条 (${pct}%)`;
    } else {
        // 全面模式：无上限，进度条保持流动（宽度100%仅表示进行中）。
        fill.style.width = '100%';
        text.textContent = `已采集 ${collected} 条（全面采集，请稍候）`;
    }
}

async function loadAlerts() {
    showLoading('alerts-list');
    
    try {
        const data = await apiRequest('/comment/alerts?limit=20');
        
        if (data.alerts.length === 0) {
            document.getElementById('alerts-list').innerHTML = '<p>暂无预警</p>';
            return;
        }
        
        const alerts = data.alerts.map(alert => `
            <div style="padding: 15px; margin: 10px 0; background: white; border-radius: 8px; 
                        border-left: 3px solid ${alert.level === 'high' ? 'red' : 'orange'};">
                <h4>[${alert.level}] ${alert.type}</h4>
                <p>${alert.message}</p>
                <p style="color: var(--text-secondary); font-size: 12px;">
                    视频ID: ${alert.video_id} | 时间: ${formatDate(alert.created_at)}
                </p>
            </div>
        `).join('');
        
        document.getElementById('alerts-list').innerHTML = alerts;
    } catch (error) {
        document.getElementById('alerts-list').innerHTML = `<p style="color: red;">加载失败: ${error.message}</p>`;
    }
}

