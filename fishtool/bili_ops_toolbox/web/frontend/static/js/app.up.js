// ===== app.up.js —— 拆分自 app.js（UP主拆解 & 账号自诊），原始行 L1021-L1438 =====
// ============ 头部UP主拆解 & 账号自诊模块 ============

// 加载分析分区列表（头部UP拆解 + 账号自诊共用）
async function loadAnalysisZones() {
    try {
        const data = await apiRequest('/analysis/categories');
        const zones = data.data || [];
        
        // 头部UP拆解分区下拉
        const analysisSelect = document.getElementById('analysis-zone-select');
        if (analysisSelect) {
            analysisSelect.innerHTML = zones.map(z => 
                `<option value="${z.name}">${z.name}</option>`
            ).join('');
        }
        
        // 账号自诊对比分区下拉（首项为不对比）
        const diagnosisSelect = document.getElementById('diagnosis-zone-select');
        if (diagnosisSelect) {
            diagnosisSelect.innerHTML = '<option value="">不对比分区</option>' + zones.map(z => 
                `<option value="${z.name}">${z.name}</option>`
            ).join('');
        }
    } catch (error) {
        console.error('加载分析分区失败:', error);
    }
}

async function fetchTopUps() {
    const category = document.getElementById('analysis-zone-select').value;
    const limit = parseInt(document.getElementById('analysis-limit').value) || 10;
    
    if (!category) {
        showAppAlert('请选择分区');
        return;
    }
    
    showLoading('top-ups-result');
    
    try {
        const data = await apiRequest('/analysis/category-top-ups', {
            method: 'POST',
            body: JSON.stringify({ category, limit })
        });
        renderTopUpsResult(data.data);
    } catch (error) {
        document.getElementById('top-ups-result').innerHTML = `<p style="color: red;">获取失败: ${error.message}</p>`;
    }
}

// 渲染分区头部 UP 列表：UP 卡片与一键填入分析框按钮。
function renderTopUpsResult(result) {
    const ups = (result.up_list || []).map((up, i) => `
        <div style="padding: 15px; margin: 10px 0; background: white; border-radius: 8px; 
                    border-left: 3px solid var(--primary-color);">
            <h4>${i + 1}. ${up.name || '未知UP主'} 
                <button class="btn btn-secondary" style="float:right;" 
                        onclick="document.getElementById('analyze-uid').value='${up.uid || up.mid || ''}'; analyzeUp();">
                    分析TA
                </button>
            </h4>
            <p style="color: var(--text-secondary); font-size: 13px;">
                粉丝: ${up.follower_count ?? up.fans ?? '未知'} | 
                充电: ${up.charge_count ?? '未知'} | 
                总播放: ${up.total_play ?? up.play_count ?? up.play ?? '未知'} | 
                UID: ${up.uid || up.mid || '未知'}
            </p>
        </div>
    `).join('');
    
    document.getElementById('top-ups-result').innerHTML = `
        <p>分区: ${result.category} | 获取到 ${result.count} 位UP主</p>
        <p style="color: var(--text-secondary); font-size: 12px;">点击"分析TA"可快速填入分析框</p>
        ${ups || '<p>暂无数据</p>'}
    `;
}

// 转义接口文本，避免昵称或AI大模型内容被当作HTML执行。
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    })[char]);
}

// 将数值格式化为适合概要卡展示的万/亿单位。
function formatUpMetric(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '暂无数据';
    if (number >= 100000000) return `${(number / 100000000).toFixed(1).replace(/\.0$/, '')}亿`;
    if (number >= 10000) return `${(number / 10000).toFixed(1).replace(/\.0$/, '')}万`;
    return number.toLocaleString('zh-CN');
}

// 每千粉转化率：guard/charge 数 ÷ 粉丝数 × 1000（‰）。粉丝为 0 或指标缺失显示 --。
function formatUpPermille(value, follower) {
    if (value === null || value === undefined) return '--';
    const v = Number(value);
    const f = Number(follower);
    if (!isFinite(v) || !isFinite(f) || f <= 0) return '--';
    return `${(v / f * 1000).toFixed(1)}‰`;
}

// 将分析文本按换行分段，保留AI大模型输出的阅读节奏。
function renderAnalysisText(value) {
    const text = String(value ?? '').trim();
    if (!text) return '<p class="card-empty">暂无数据</p>';
    return text.split(/\n+/).filter(Boolean)
        .map(paragraph => `<p>${escapeHtml(paragraph)}</p>`).join('');
}

// 渲染单个运营策略维度卡片。
function renderStrategyCard(dimension, fields) {
    const safeFields = fields && typeof fields === 'object' ? fields : {};
    return `
        <article class="card strategy-card">
            <h4 class="card-title">${escapeHtml(dimension)}</h4>
            ${['核心观察', '具体打法', '关键要点'].map(section => `
                <section class="card-section">
                    <h5>${section}</h5>
                    <div class="card-section-content">${renderAnalysisText(safeFields[section])}</div>
                </section>
            `).join('')}
        </article>
    `;
}

// 分析单个UP主运营策略
async function analyzeUp() {
    const uidOrUrl = document.getElementById('analyze-uid').value.trim();
    if (!uidOrUrl) {
        showAppAlert('请输入UP主UID或主页链接');
        return;
    }

    showLoading('analysis-result', {
        progress: 0,
        message: '正在创建UP主拆解任务',
    });

    try {
        const started = await apiRequest('/analysis/analyze-up/tasks', {
            method: 'POST',
            body: JSON.stringify({ uid_or_url: uidOrUrl })
        });
        const result = await pollTask(
            `/analysis/analyze-up/tasks/${started.task_id}`,
            'analysis-result'
        );
        renderAnalyzeUpResult(result);
    } catch (error) {
        document.getElementById('analysis-result').innerHTML = `<p class="analysis-error">分析失败: ${escapeHtml(error.message)}</p>`;
    }
}

// 渲染 UP 主拆解结果：概要卡 + 运营策略 + 原始输出。
function renderAnalyzeUpResult(result) {
    const upEnvelope = result.up_data || {};
    const up = { ...upEnvelope, ...(upEnvelope.data || {}) };
    const analysis = result.analysis || {};
    const displayName = up.name || '暂无数据';
    const displayUid = up.uid || '暂无数据';

    let html = `
        <div class="analysis-dashboard">
            ${buildUpSummaryHtml(up, displayName, displayUid)}
    `;
    html += buildStrategySectionHtml(analysis);
    html += '</div>';
    document.getElementById('analysis-result').innerHTML = html;
}

// 构建 UP 主概要卡片：头像、昵称、UID 与核心指标。
function buildUpSummaryHtml(up, displayName, displayUid) {
    const face = String(up.face || '').trim();
    const faceUrl = face.startsWith('//') ? `https:${face}` : face;
    const avatar = faceUrl
        ? `<img class="up-avatar" src="${escapeHtml(faceUrl)}" alt="${escapeHtml(displayName)}头像">`
        : '<div class="up-avatar up-avatar-placeholder">暂无头像</div>';

    return `
        <article class="card up-summary-card">
            ${avatar}
            <div class="up-summary-main">
                <p class="dashboard-eyebrow">UP主数据概要</p>
                <h3>${escapeHtml(displayName)}</h3>
                <span>UID ${escapeHtml(displayUid)}</span>
            </div>
            <div class="up-summary-metrics">
                <div class="metric-item"><span>粉丝数</span><strong>${formatUpMetric(up.fans)}</strong></div>
                <div class="metric-item"><span>充电人数</span><strong>${formatUpMetric(up.charge_count)}</strong></div>
                <div class="metric-item"><span>每千粉舰长率</span><strong>${formatUpPermille(up.guard_count, up.fans)}</strong></div>
                <div class="metric-item"><span>每千粉充电率</span><strong>${formatUpPermille(up.charge_count, up.fans)}</strong></div>
                <div class="metric-item"><span>总播放</span><strong>${formatUpMetric(up.total_play)}</strong></div>
            </div>
        </article>
    `;
}

// 构建运营策略区块：成功时展示策略卡与原始输出，失败时展示提示。
function buildStrategySectionHtml(analysis) {
    if (analysis.success === true && analysis.analysis) {
        const strategy = analysis.analysis;
        const dimensions = ['选题方向', '标题套路', '封面风格', '发布节奏', '互动引导'];
        let html = `
            <div class="analysis-heading">
                <p class="dashboard-eyebrow">OPERATIONS PLAYBOOK</p>
                <h3>运营策略分析</h3>
            </div>
            <div class="strategy-card-grid">
                ${dimensions.map(dimension => renderStrategyCard(dimension, strategy[dimension])).join('')}
            </div>
        `;
        if (analysis.raw_text) {
            html += `
                <details class="raw-output-details">
                    <summary>查看原始输出</summary>
                    <div>${renderAnalysisText(analysis.raw_text)}</div>
                </details>
            `;
        }
        return html;
    }
    return `<div class="analysis-notice">${escapeHtml(analysis.message || '暂无策略分析（可能需要配置AI大模型）')}</div>`;
}

// 账号自诊大屏
const diagnosisCharts = {};

// 获取或初始化账号自诊图表实例：优先复用已存在实例，未初始化时才创建。
function getDiagnosisChart(elementId) {
    const element = document.getElementById(elementId);
    if (!element || typeof window.echarts === 'undefined') return null;
    const chart = window.echarts.getInstanceByDom(element) || window.echarts.init(element, null, { renderer: 'canvas' });
    diagnosisCharts[elementId] = chart;
    return chart;
}

// 安全数值转换：空值或非法数字回退到默认值，避免图表和文案出现 NaN。
function diagnosisNumber(value, fallback = 0) {
    if (value === null || value === undefined || value === '') return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
}

// 格式化自诊指标：数字千分位 + 可选后缀，空值统一显示“暂无数据”。
function formatDiagnosisMetric(value, suffix = '') {
    if (value === null || value === undefined || value === '') return '暂无数据';
    const number = Number(value);
    if (!Number.isFinite(number)) return '暂无数据';
    return `${number.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}${suffix}`;
}

// 汇总互动效率三项指标：统一换算成 0-100 分数供雷达图使用，超限封顶。
function engagementScores(engagement) {
    // 自诊接口的三项互动指标均为百分比；图表按 0-100% 映射，超过上限显示到顶。
    const dimensions = [
        { name: '粉丝触达', raw: diagnosisNumber(engagement.play_to_fans_ratio) },
        { name: '评论效率', raw: diagnosisNumber(engagement.comment_to_play_ratio) },
        { name: '收藏效率', raw: diagnosisNumber(engagement.favorite_to_play_ratio) },
    ];
    return dimensions.map(item => ({
        ...item,
        score: Math.min(100, Math.max(0, item.raw)),
    }));
}

// 把百分比指标格式化为带 % 的展示字符串。
function formatEngagementPercent(value) {
    return `${diagnosisNumber(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`;
}

// 渲染账号全部视频的词云：按词频加权字号，最多展示 40 个标签。
function renderAccountTagCloud(tagCloud) {
    const entries = Object.entries(tagCloud?.word_frequency || {}).slice(0, 40);
    if (!entries.length) return '<p class="card-empty">暂无可用视频标签</p>';
    const maxFrequency = Math.max(...entries.map(([, count]) => diagnosisNumber(count, 1)));
    return `<div class="account-tag-cloud" aria-label="账号全部视频标签词云">${entries.map(([tag, count], index) => {
        const weight = diagnosisNumber(count, 1) / maxFrequency;
        const size = 14 + Math.round(weight * 20);
        return `<span class="tag-cloud-word tone-${index % 5}" style="font-size:${size}px" title="${escapeHtml(tag)} · ${count}次">${escapeHtml(tag)}</span>`;
    }).join('')}</div>`;
}

// 渲染 AI 运营建议卡片：优先使用结构化文本，缺失时回退到原文。
function renderAIResearch(aiReport) {
    if (!aiReport?.success) {
        return `<section class="ai-research-grid"><article class="ai-research-card ai-unavailable"><h4>AI 调研报告与运营点评</h4><p>${escapeHtml(aiReport?.message || 'AI调研暂不可用')}</p></article></section>`;
    }
    const highlights = (aiReport.highlights || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
    return `<section class="ai-research-grid">
        <article class="ai-research-card"><p class="dashboard-eyebrow">AI RESEARCH</p><h4>账号调研报告</h4><p>${escapeHtml(aiReport.report)}</p>${highlights ? `<ul>${highlights}</ul>` : ''}</article>
        <article class="ai-research-card"><p class="dashboard-eyebrow">OPERATIONS REVIEW</p><h4>运营点评</h4><p>${escapeHtml(aiReport.commentary)}</p><small>${escapeHtml(aiReport.model || '')} · ${formatDate(aiReport.generated_at)}</small></article>
    </section>`;
}

// 渲染自诊三张图表：播放量走势、互动效率雷达、分区基准对比。
function renderDiagnosisCharts(videoStats, engagement, benchmark) {
    const theme = getDashboardChartTheme();
    renderDiagnosisVolumeChart(getDiagnosisChart('diagnosis-volume-chart'), videoStats, theme);
    renderDiagnosisEngagementChart(getDiagnosisChart('diagnosis-engagement-chart'), engagement, theme);
    renderDiagnosisBenchmarkChart(getDiagnosisChart('diagnosis-benchmark-chart'), benchmark, theme);
}

// 渲染内容互动总量柱状图。
function renderDiagnosisVolumeChart(volumeChart, videoStats, theme) {
    if (!volumeChart) return;
    volumeChart.setOption({
        tooltip: getDashboardTooltipTheme('axis'),
        grid: { left: 56, right: 24, top: 28, bottom: 34 },
        xAxis: { type: 'category', data: ['累计播放', '累计评论', '累计收藏'], axisLabel: { color: theme.text }, axisLine: { lineStyle: { color: theme.border } } },
        yAxis: { type: 'value', axisLabel: { color: theme.text }, splitLine: { lineStyle: { color: theme.border, type: 'dashed' } } },
        series: [{ type: 'bar', barMaxWidth: 44, data: [videoStats.total_play, videoStats.total_comment, videoStats.total_favorite].map(value => diagnosisNumber(value)), itemStyle: { color: theme.primary, borderRadius: [6, 6, 0, 0] } }],
    }, true);
}

// 渲染互动效率雷达图。
function renderDiagnosisEngagementChart(engagementChart, engagement, theme) {
    if (!engagementChart) return;
    const scores = engagementScores(engagement);
    const indicators = scores.map(item => ({
        name: `${item.name}\n${formatEngagementPercent(item.raw)}`,
        max: 100,
    }));
    engagementChart.setOption({
        tooltip: {
            ...getDashboardTooltipTheme('item'),
            formatter: () => scores.map(item => {
                const capped = item.raw > 100 ? '（图表按 100% 封顶）' : '';
                return `${escapeHtml(item.name)}：${formatEngagementPercent(item.raw)}${capped}`;
            }).join('<br>'),
        },
        radar: {
            indicator: indicators,
            shape: 'polygon',
            center: ['50%', '55%'],
            radius: '72%',
            startAngle: 90,
            splitNumber: 4,
            axisName: { color: theme.text, fontSize: 13, fontWeight: 600, lineHeight: 19 },
            axisLine: { lineStyle: { color: theme.border, width: 1.5 } },
            splitLine: { lineStyle: { color: theme.border, width: 1 } },
            splitArea: { areaStyle: { color: ['rgba(250,247,242,.32)', 'rgba(250,247,242,.12)'] } },
        },
        series: [{
            type: 'radar',
            symbol: 'circle',
            symbolSize: 10,
            lineStyle: { color: theme.secondary, width: 3 },
            itemStyle: { color: theme.secondary, borderColor: '#ffffff', borderWidth: 2 },
            areaStyle: { color: theme.secondary, opacity: 0.28 },
            data: [{ value: scores.map(item => item.score), name: '互动效率' }],
        }],
    }, true);
    engagementChart.resize();
}

// 渲染分区播放基准对比图，最后一根柱子高亮为本人均播。
function renderDiagnosisBenchmarkChart(benchmarkChart, benchmark, theme) {
    const categoryMetrics = benchmark?.category_metrics;
    if (!benchmarkChart || !categoryMetrics) return;
    benchmarkChart.setOption({
        tooltip: getDashboardTooltipTheme('axis'),
        grid: { left: 56, right: 24, top: 28, bottom: 34 },
        xAxis: { type: 'category', data: ['P25', 'P50', 'P75', '我的均播'], axisLabel: { color: theme.text }, axisLine: { lineStyle: { color: theme.border } } },
        yAxis: { type: 'value', axisLabel: { color: theme.text }, splitLine: { lineStyle: { color: theme.border, type: 'dashed' } } },
        series: [{ type: 'bar', data: [categoryMetrics.p25, categoryMetrics.p50, categoryMetrics.p75, benchmark.self_metrics?.avg_play].map(value => diagnosisNumber(value)), itemStyle: { color: params => params.dataIndex === 3 ? theme.secondary : theme.accent, borderRadius: [6, 6, 0, 0] } }],
    }, true);
}

// 运行账号自诊：校验 UID 后请求后端聚合数据，依次渲染指标、图表与 AI 建议。
async function runSelfDiagnosis() {
    const uid = parseInt(document.getElementById('diagnosis-uid').value, 10);
    const category = document.getElementById('diagnosis-zone-select').value || null;
    if (!uid) {
        showAppAlert('请输入有效的B站UID');
        return;
    }

    showLoading('diagnosis-result');
    try {
        const response = await apiRequest('/analysis/self-diagnosis', { method: 'POST', body: JSON.stringify({ uid, category }) });
        const result = response.data || {};
        renderSelfDiagnosisResult(uid, result);
    } catch (error) {
        document.getElementById('diagnosis-result').innerHTML = `<p class="analysis-error">自诊失败: ${escapeHtml(error.message)}</p>`;
    }
}

// 解析自诊响应并渲染整个诊断仪表盘，随后初始化三个图表。
function renderSelfDiagnosisResult(uid, result) {
    const self = result.self_data || {};
    const basic = self.basic_info || {};
    const fans = self.fan_stats || {};
    const videos = self.video_stats || {};
    const engagement = self.engagement_metrics || {};
    const rhythm = self.post_rhythm || {};
    const availability = self.data_availability || {};
    const benchmark = result.benchmark?.has_benchmark ? result.benchmark : null;
    const aiReport = result.ai_report || {};
    const tagCloud = self.tag_cloud || {};
    const availableCount = Object.values(availability).filter(Boolean).length;
    const availabilityItems = [
        ['基础资料', availability.basic_info], ['粉丝关系', availability.fan_stats], ['投稿统计', availability.video_stats],
        ['投稿节奏', availability.post_rhythm], ['互动表现', availability.engagement_metrics], ['视频标签', availability.tag_cloud], ['粉丝增长曲线', availability.fans_growth_curve],
        ['完播率', availability.completion_rate], ['观众画像', availability.audience_profile], ['流量来源', availability.traffic_source],
    ];

    document.getElementById('diagnosis-result').innerHTML = `
        <div class="diagnosis-dashboard">
            <header class="diagnosis-header">
                <div><p class="dashboard-eyebrow">ACCOUNT HEALTH DASHBOARD</p><h3>${escapeHtml(basic.name || `UID ${uid}`)}</h3><p>UID ${uid} · ${basic.level ? `Lv.${basic.level}` : '公开资料'} · ${formatDate(self.fetched_at)}</p></div>
                <div class="diagnosis-score"><strong>${availableCount}</strong><span>项数据可用</span></div>
            </header>
            <div class="diagnosis-metrics">
                <article class="metric-item"><span>粉丝数</span><strong>${formatDiagnosisMetric(fans.follower)}</strong><small>关注 ${formatDiagnosisMetric(fans.following)}</small></article>
                <article class="metric-item"><span>充电人数</span><strong>${formatDiagnosisMetric(fans.charge_count)}</strong><small>${escapeHtml(fans.charge_source || '来源未知')}</small></article>
                <article class="metric-item"><span>每千粉舰长率</span><strong>${formatUpPermille(fans.guard_count, fans.follower)}</strong><small>${escapeHtml(fans.guard_source || '来源未知')}</small></article>
                <article class="metric-item"><span>每千粉充电率</span><strong>${formatUpPermille(fans.charge_count, fans.follower)}</strong></article>
                <article class="metric-item"><span>视频总数</span><strong>${formatDiagnosisMetric(videos.total_count)}</strong><small>累计播放 ${formatDiagnosisMetric(videos.total_play)}</small></article>
                <article class="metric-item"><span>平均播放</span><strong>${formatDiagnosisMetric(videos.avg_play)}</strong><small>最高播放 ${formatDiagnosisMetric(videos.max_play_video?.play)}</small></article>
                <article class="metric-item"><span>累计收藏</span><strong>${formatDiagnosisMetric(videos.total_favorite)}</strong><small>平均每稿 ${formatDiagnosisMetric(videos.avg_favorite)}</small></article>
                <article class="metric-item"><span>近30天投稿</span><strong>${formatDiagnosisMetric(rhythm.recent_30d_count)}</strong><small>周均 ${formatDiagnosisMetric(rhythm.videos_per_week)} 条</small></article>
            </div>
            <div class="diagnosis-chart-grid">
                <article class="chart-panel diagnosis-chart-panel"><h4 class="panel-title">内容互动总量</h4><div id="diagnosis-volume-chart" class="dashboard-chart diagnosis-chart-canvas"></div></article>
                <article class="chart-panel diagnosis-chart-panel diagnosis-triangle-panel"><h4 class="panel-title">互动效率 · 三角维度</h4><div id="diagnosis-engagement-chart" class="dashboard-chart diagnosis-chart-canvas"></div></article>
                ${benchmark ? `<article class="chart-panel chart-panel-wide diagnosis-chart-panel"><h4 class="panel-title">${escapeHtml(benchmark.category)}分区播放基准</h4><div id="diagnosis-benchmark-chart" class="dashboard-chart diagnosis-chart-canvas"></div></article>` : ''}
            </div>
            <div class="diagnosis-detail-grid">
                <article class="diagnosis-detail-card"><h4>互动指标</h4><dl><div><dt>粉丝触达率</dt><dd>${formatDiagnosisMetric(engagement.play_to_fans_ratio, '%')}</dd></div><div><dt>评论率</dt><dd>${formatDiagnosisMetric(engagement.comment_to_play_ratio, '%')}</dd></div><div><dt>收藏率</dt><dd>${formatDiagnosisMetric(engagement.favorite_to_play_ratio, '%')}</dd></div><div><dt>最长断更</dt><dd>${formatDiagnosisMetric(rhythm.longest_gap_days, ' 天')}</dd></div></dl></article>
                <article class="diagnosis-detail-card"><h4>数据可用性</h4><div class="availability-list">${availabilityItems.map(([label, ready]) => `<span class="availability-item ${ready ? 'ready' : 'unavailable'}">${ready ? '已获取' : '暂不可得'} ${label}</span>`).join('')}</div></article>
                <article class="diagnosis-detail-card"><h4>对比结论</h4>${benchmark ? `<p>${escapeHtml(benchmark.comparison?.position || '暂无结论')}，当前均播为分区基准的 ${formatDiagnosisMetric(benchmark.comparison?.vs_avg, '%')}。</p><p>参照样本 ${formatDiagnosisMetric(benchmark.sample_size)} 条。</p>` : `<p>${escapeHtml(result.benchmark?.message || '未选择或暂无分区参照数据，当前展示真实公开账号数据。')}</p>`}</article>
            </div>
            ${renderAIResearch(aiReport)}
            <article class="account-tag-cloud-card">
                <div class="tag-cloud-heading"><div><p class="dashboard-eyebrow">CONTENT DNA</p><h4>总体视频 Tag 词云</h4></div><span>${formatDiagnosisMetric(tagCloud.tagged_video_count)}/${formatDiagnosisMetric(tagCloud.video_count)} 个视频 · ${formatDiagnosisMetric(tagCloud.tag_count)} 个标签</span></div>
                ${renderAccountTagCloud(tagCloud)}
            </article>
        </div>`;
    renderDiagnosisCharts(videos, engagement, benchmark);
}

async function exportDiagnosisReport() {
    const uid = parseInt(document.getElementById('diagnosis-uid').value);
    const category = document.getElementById('diagnosis-zone-select').value || null;
    
    if (!uid) {
        showAppAlert('请先输入B站UID');
        return;
    }
    
    try {
        const data = await apiRequest('/analysis/export-report', {
            method: 'POST',
            body: JSON.stringify({ uid, format: 'markdown', category })
        });
        
        const result = data.data;
        showAppAlert(`报告已生成: ${result.filepath}`);
    } catch (error) {
        showAppAlert(`导出失败: ${error.message}`);
    }
}
