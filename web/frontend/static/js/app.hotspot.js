// ===== app.hotspot.js —— 拆分自 app.js（热点发现：分区 / Tag云 / 活动情报 / AI选题），原始行 L148-L305 =====
// ============ 热点发现模块 ============

async function loadZones() {
    try {
        const data = await apiRequest('/hotspot/zones');
        const zoneSelect = document.getElementById('zone-select');
        const topicZone = document.getElementById('topic-zone');
        
        const options = data.zones.map(zone =>
            `<option value="${escapeHtml(zone)}">${escapeHtml(zone)}</option>`
        ).join('');
        const collectOptions = (data.zone_options || []).map(zone =>
            `<option value="${Number(zone.tid)}">${escapeHtml(zone.name)}</option>`
        ).join('');

        zoneSelect.innerHTML = options;
        topicZone.innerHTML = options;
        const collectSelect = document.getElementById('hotspot-tid-select');
        if (collectSelect && collectOptions) {
            collectSelect.innerHTML = collectOptions;
            collectSelect.addEventListener('change', () => loadHotspotLifecycle());
        }
    } catch (error) {
        console.error('加载分区失败:', error);
    }
}

// 生成分区热门Tag词云：提交后台采集任务后轮询进度，最终渲染词云卡片。
// 与活动情报/AI选题助手同级，前端仅负责交互与展示，采集逻辑在 /hotspot/tag-cloud/tasks。
async function generateTagCloud() {
    const zoneName = document.getElementById('zone-select').value;
    if (!zoneName) {
        showAppAlert('请选择分区');
        return;
    }

    showLoading('tag-cloud-result', {
        progress: 0,
        message: '正在创建分区热门Tag采集任务',
    });

    try {
        const started = await apiRequest('/hotspot/tag-cloud/tasks', {
            method: 'POST',
            body: JSON.stringify({
                zone_name: zoneName,
                limit: 100,
                top_n: 50
            })
        });
        const result = await pollTask(
            `/hotspot/tag-cloud/tasks/${started.task_id}`,
            'tag-cloud-result'
        );
        renderTagCloudResult(result, zoneName);
    } catch (error) {
        document.getElementById('tag-cloud-result').innerHTML = `<p style="color: red;">生成失败: ${escapeHtml(error.message)}</p>`;
    }
}

// 渲染 Tag 云采集结果：词频按热度映射字号，数据缺失时使用默认值。
function renderTagCloudResult(result, zoneName) {
    const tags = Object.entries(result.word_frequency || {})
        .slice(0, 20)
        .map(([tag, freq], i) =>
            `<div style="display: inline-block; margin: 10px; padding: 8px 16px;
                  background: linear-gradient(135deg, #d4a373, #c8997f);
                  color: white; border-radius: 20px; font-size: ${14 + freq * 0.5}px;">
                ${i + 1}. ${escapeHtml(tag)} (${escapeHtml(freq)})
            </div>`
        ).join('');

    document.getElementById('tag-cloud-result').innerHTML = `
        <h3>分区：${escapeHtml(result.zone_name || zoneName)}</h3>
        <p>采集进度：${result.progress ?? 100}%</p>
        <progress max="100" value="${result.progress ?? 100}" style="width: 100%;"></progress>
        <p>分析视频数：${result.video_count ?? 0} | 提取Tag数：${result.tag_count ?? 0}</p>
        <div style="margin-top: 20px;">${tags}</div>
    `;
}

async function fetchActivities() {
    // 读取分区下拉框（活动情报模块支持按分区筛选官号）
    const zone = document.getElementById('activity-zone')?.value || 'all';
    const zoneLabels = { all: '全部', game: '游戏区', anime: '动画区', paint: '绘画区' };

    showLoading('activity-result');

    try {
        const data = await apiRequest('/hotspot/activities', {
            method: 'POST',
            body: JSON.stringify({ include_ugc: true, zone: zone })
        });
        renderActivitiesResult(data.data, zone, zoneLabels);
    } catch (error) {
        document.getElementById('activity-result').innerHTML = `<p style="color: red;">拉取失败: ${escapeHtml(error.message)}</p>`;
    }
}

// 渲染活动情报结果：分区账号与活动数量汇总，无数据时给出明确提示。
function renderActivitiesResult(result, zone, zoneLabels) {
    const activities = (result.activities || []).slice(0, 20).map(act => `
        <div style="padding: 15px; margin: 10px 0; background: white; border-radius: 8px; border-left: 3px solid var(--primary-color);">
            <h4>${escapeHtml(act.title)}</h4>
            <p>${escapeHtml(act.desc || '暂无描述')}</p>
            <p style="color: var(--text-secondary); font-size: 12px;">
                状态: ${escapeHtml(act.status)} | 来源: ${escapeHtml(act.source)}${act.source_username ? ' | 账号: ' + escapeHtml(act.source_username) : ''} |
                <a href="${safeUrl(act.link)}" target="_blank" rel="noopener noreferrer">查看详情</a>
            </p>
        </div>
    `).join('');

    // 展示分区与账号信息，无结果时给出明确提示
    const accountInfo = (result.zone_accounts || []).map(a => escapeHtml(a.name)).join('、') || '无';
    document.getElementById('activity-result').innerHTML = `
        <p>分区：${escapeHtml(zoneLabels[result.zone] || result.zone)} | 官方活动: ${Number(result.official_count) || 0} 个 | UGC动态: ${Number(result.ugc_count) || 0} 条 | 跟踪账号: ${accountInfo}</p>
        ${activities || '<p style="color: orange;">暂无活动数据（UGC动态可能受B站风控限制，可稍后重试或检查登录态）</p>'}
    `;
}

async function generateTopics() {
    const direction = document.getElementById('topic-direction').value;
    const zoneName = document.getElementById('topic-zone').value;
    const count = parseInt(document.getElementById('topic-count').value);

    if (!direction) {
        showAppAlert('请输入创作方向');
        return;
    }

    showLoading('topic-result');

    try {
        const data = await apiRequest('/hotspot/topics/generate', {
            method: 'POST',
            body: JSON.stringify({
                direction,
                zone_name: zoneName,
                count,
                use_llm: true
            })
        });
        renderTopicsResult(data.data);
    } catch (error) {
        document.getElementById('topic-result').innerHTML = `<p style="color: red;">生成失败: ${escapeHtml(error.message)}</p>`;
    }
}

// 渲染 AI 选题结果：列出选题卡片与生成方式、热门 Tag 摘要。
function renderTopicsResult(result) {
    const topics = result.topics.map((topic, i) => `
        <div style="padding: 15px; margin: 10px 0; background: white; border-radius: 8px;">
            <h4>${i + 1}. ${escapeHtml(topic.title)}</h4>
            <p>${escapeHtml(topic.description)}</p>
            <p style="color: var(--text-secondary); font-size: 12px;">
                难度: ${escapeHtml(topic.difficulty)} | 关键词: ${escapeHtml((topic.keywords || []).join(', '))}
            </p>
        </div>
    `).join('');

    document.getElementById('topic-result').innerHTML = `
        <p>生成方式: ${result.used_llm ? 'AI增强' : '降级方案'}</p>
        <p>热门Tag: ${escapeHtml((result.hot_tags || []).slice(0, 5).join(', ')) || '暂无'}</p>
        ${topics}
    `;
}

// ============ 评论监控模块 ============

// escapeHtml 的统一实现位于本文件后部，所有模块共用同一个函数。

// 统一将未知接口字段收敛为数组，避免 map(undefined) 中断整页渲染。


// 将增速格式化为可读百分比，空值展示为数据不足。
function formatPercent(value) {
    return value === null || value === undefined ? '数据不足' : `${(Number(value) * 100).toFixed(1)}%`;
}

// 每千粉转化率：guard/charge 数 ÷ 粉丝数 × 1000（‰）。粉丝为 0 或指标缺失显示 --。
function formatPermille(value, follower) {
    if (value === null || value === undefined) return '--';
    const v = Number(value);
    const f = Number(follower);
    if (!isFinite(v) || !isFinite(f) || f <= 0) return '--';
    return `${(v / f * 1000).toFixed(1)}‰`;
}

// 卡片伪徽标：圆角背景小标签，展示 UP 的每千粉舰长率 / 每千粉充电率。
function hotspotMetricBadges(owner) {
    if (!owner || owner.follower === null || owner.follower === undefined || Number(owner.follower) <= 0) return '';
    const guard = owner.guard_count === null || owner.guard_count === undefined ? null : Number(owner.guard_count);
    const charge = owner.charge_count === null || owner.charge_count === undefined ? null : Number(owner.charge_count);
    const parts = [];
    if (guard !== null) parts.push(`<span class="hotspot-metric-badge">舰长率 ${formatPermille(guard, owner.follower)}</span>`);
    if (charge !== null) parts.push(`<span class="hotspot-metric-badge">充电率 ${formatPermille(charge, owner.follower)}</span>`);
    return parts.length ? `<span class="hotspot-metric-badges">${parts.join('')}</span>` : '';
}

// 卡片趋势徽标：与抽屉增长判定联动。上升期/出现期嵌▲，衰退期嵌▼；数据不足不误导。
function hotspotTrendBadges(item) {
    const stage = item.stage || '';
    const observedDays = Number(item.metrics?.observed_days ?? item.metrics?.days ?? 0);
    if (observedDays < 2) return '';
    if (stage === '衰退期') {
        return '<span class="hotspot-trend-badge hotspot-trend-down" title="下滑">▼</span>';
    }
    if (stage === '上升期' || stage === '出现期') {
        return '<span class="hotspot-trend-badge hotspot-trend-up" title="增长">▲</span>';
    }
    return '';
}

// 生命周期卡片只消费统一 Detection DTO，并轮询采集进度。
// 卡片支持：点击标题区展开时间轴、UP 数/增速展示、账号抽屉入口。
async function loadHotspotLifecycle() {
    const grid = document.getElementById('hotspot-lifecycle-grid');
    if (!grid) return;
    try {
        const tid = Number(document.getElementById('hotspot-tid-select')?.value || 1008);
        const result = await apiRequest(`/hotspot/lifecycle?tid=${tid}`);
        const items = (result.data || {}).items || [];
        document.getElementById('hotspot-algorithm-version').textContent = `算法 ${result.data.algorithm_version}`;
        grid.innerHTML = items.length ? items.slice(0, 20).map(item => `
            <article class="hotspot-lifecycle-card">
                <div class="hotspot-card-title-row" onclick="loadHotspotTimeline('${escapeHtml(item.bvid)}', this)">
                    <h3>${escapeHtml(item.title || item.bvid)}</h3>
                    <span class="hotspot-stage-pill">${escapeHtml(item.stage)}</span>${hotspotTrendBadges(item)}
                    ${hotspotMetricBadges(item.owner_metrics)}
                </div>
                <p class="hotspot-lifecycle-meta">${escapeHtml(item.explain)}</p>
                <p class="hotspot-lifecycle-meta">置信度 <span class="hotspot-confidence">${Math.round((item.confidence || 0) * 100)}%</span> · 增速 ${formatPercent(item.metrics?.growth)}</p>
                <p class="hotspot-lifecycle-meta">参与UP数 <span class="hotspot-up-count">${item.metrics?.up_count ?? 0}</span> · ${item.metrics?.observed_days ?? item.metrics?.days ?? 0} 个采集日 · 实际跨度 ${Number(item.metrics?.window_span_days || 0).toFixed(2)} 天</p>
                <div class="hotspot-timeline-box" data-bvid="${escapeHtml(item.bvid)}"></div>
                <div class="hotspot-card-actions">
                    ${item.owner_mid ? `<button class="btn btn-secondary btn-sm" type="button" onclick="openHotspotAccount(${Number(item.owner_mid)}, '${escapeHtml(item.bvid)}')">查看上涨账号</button>` : ''}
                </div>
            </article>`).join('') : '<p class="text-muted">数据积累中，连续采集几天后解锁阶段判断。</p>';
    } catch (error) {
        grid.innerHTML = `<p class="text-muted">生命周期数据暂不可用：${escapeHtml(error.message)}</p>`;
    }
}

// 点击卡片标题区加载该视频的统计历史时间轴（仅加载一次，重复点击折叠）。
// 使用 echarts 绘制多指标折线图（播放/弹幕/评论/点赞），替代早期简易趋势条。
async function loadHotspotTimeline(bvid, titleRow) {
    if (!bvid || !titleRow) return;
    const card = titleRow.closest('.hotspot-lifecycle-card');
    const box = card.querySelector('.hotspot-timeline-box');
    if (!box) return;
    // 已加载过：切换展开/折叠，展开时重绘图表避免隐藏容器宽度为 0 变形。
    if (box.dataset.loaded === '1') {
        box.classList.toggle('hotspot-timeline-open');
        if (box.classList.contains('hotspot-timeline-open')) {
            const chart = echarts.getInstanceByDom(box.querySelector('.hotspot-timeline-chart'));
            if (chart) chart.resize();
        }
        return;
    }
    try {
        const result = await apiRequest(`/hotspot/lifecycle/timeline?bvid=${encodeURIComponent(bvid)}`);
        const points = (result.data || {}).points || [];
        if (!points.length) {
            box.innerHTML = '<p class="hotspot-timeline-empty">暂无历史快照，先跑一轮采集。</p>';
            box.dataset.loaded = '1';
            box.classList.add('hotspot-timeline-open');
            return;
        }
        // 只取最近 12 个点，时间按快照顺序升序排列。
        const recent = points.slice(-12);
        const times = recent.map(p => (p.time || '').slice(5, 16));
        // 四个互动指标使用莫兰迪色系区分，播放为主轴。
        const metricSeries = [
            { key: 'view', name: '播放', color: '#d4a373' },
            { key: 'danmaku', name: '弹幕', color: '#8ab17d' },
            { key: 'reply', name: '评论', color: '#7a9cc6' },
            { key: 'like', name: '点赞', color: '#c98a8a' },
        ];
        box.innerHTML = `<div class="hotspot-timeline-title">互动趋势（最近 ${recent.length} 次快照）</div><div class="hotspot-timeline-chart"></div>`;
        const chartDom = box.querySelector('.hotspot-timeline-chart');
        const chart = echarts.init(chartDom);
        chart.setOption({
            tooltip: { trigger: 'axis' },
            legend: { top: 0, textStyle: { fontSize: 11 } },
            grid: { left: 46, right: 12, top: 30, bottom: 24 },
            xAxis: { type: 'category', data: times, axisLabel: { fontSize: 10 } },
            yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
            series: metricSeries.map(m => ({
                name: m.name,
                type: 'line',
                smooth: true,
                symbolSize: 5,
                data: recent.map(p => (p[m.key] === undefined ? null : p[m.key])),
                itemStyle: { color: m.color },
                lineStyle: { width: 2 },
            })),
        });
        box.dataset.loaded = '1';
        box.classList.add('hotspot-timeline-open');
    } catch (error) {
        box.innerHTML = `<p class="hotspot-timeline-empty">时间轴加载失败：${escapeHtml(error.message)}</p>`;
    }
}

// 打开账号关联抽屉：同时加载 UP 主关联数据与时间轴。
async function openHotspotAccount(mid, bvid) {
    const mask = document.getElementById('hotspot-drawer-mask');
    const drawer = document.getElementById('hotspot-drawer');
    if (!mask || !drawer) return;
    mask.classList.add('hotspot-drawer-mask-show');
    drawer.classList.add('hotspot-drawer-open');
    drawer.innerHTML = '<p class="text-muted">加载账号数据中...</p>';
    try {
        const result = await apiRequest(`/hotspot/lifecycle/accounts?bvid=${encodeURIComponent(bvid)}`);
        const d = result.data || {};
        const fmt = v => (v === null || v === undefined ? '暂无' : Number(v).toLocaleString());
        const growth = d.growth_ratio === null || d.growth_ratio === undefined ? '暂无'
            : d.growth_ratio >= 1.5 ? `近90日播放 ${(d.growth_ratio * 100).toFixed(0)}%（显著上涨）`
            : d.growth_ratio >= 1.1 ? `近90日播放 ${(d.growth_ratio * 100).toFixed(0)}%（温和上涨）`
            : `近90日播放 ${(d.growth_ratio * 100).toFixed(0)}%（持平或下滑）`;
        drawer.innerHTML = `
            <div class="hotspot-drawer-header">
                <h3>${escapeHtml(d.name || 'UP主')} <small>UID ${d.mid ?? ''}</small></h3>
                <button class="hotspot-drawer-close" onclick="closeHotspotDrawer()" aria-label="关闭">×</button>
            </div>
            <div class="hotspot-drawer-body">
                <div class="hotspot-drawer-stat-grid">
                    <div><span>粉丝</span><b>${fmt(d.follower)}</b></div>
                    <div><span>关注</span><b>${fmt(d.following)}</b></div>
                    <div><span>累计播放</span><b>${fmt(d.archive_view)}</b></div>
                    <div><span>近90日作品</span><b>${fmt(d.recent_count)}</b></div>
                </div>
                <p class="hotspot-drawer-growth">${growth}</p>
                <div class="hotspot-drawer-guard">
                    <span>舰长转化</span><b>${d.guard_count === null || d.guard_count === undefined ? '暂无直播数据' : d.guard_count + ' 人'}</b>
                    ${d.live_status === 1 ? '<span class="hotspot-live-dot">直播中</span>' : ''}
                </div>
                <div class="hotspot-drawer-guard">
                    <span>充电人数</span><b>${d.charge_count === null || d.charge_count === undefined ? '暂无数据' : d.charge_count + ' 人'}</b>
                    ${d.charge_source === 'unavailable' ? '<span class="hotspot-live-dot" style="background:#8a8f98;">接口不可用</span>' : ''}
                </div>
                <div class="hotspot-drawer-guard">
                    <span>每千粉舰长率</span><b>${formatPermille(d.guard_count, d.follower)}</b>
                </div>
                <div class="hotspot-drawer-guard">
                    <span>每千粉充电率</span><b>${formatPermille(d.charge_count, d.follower)}</b>
                </div>
                <div class="hotspot-drawer-actions">
                    <a class="btn btn-primary btn-sm" target="_blank" href="https://space.bilibili.com/${d.mid ?? ''}">打开B站主页</a>
                    <button class="btn btn-secondary btn-sm" type="button" onclick="jumpToAnalysis(${d.mid ?? 0})">去账号分析</button>
                </div>
            </div>`;
    } catch (error) {
        drawer.innerHTML = `<p class="hotspot-drawer-error">账号数据加载失败：${escapeHtml(error.message)}</p>`;
    }
}

function closeHotspotDrawer() {
    const mask = document.getElementById('hotspot-drawer-mask');
    const drawer = document.getElementById('hotspot-drawer');
    if (mask) mask.classList.remove('hotspot-drawer-mask-show');
    if (drawer) drawer.classList.remove('hotspot-drawer-open');
}

// 从抽屉跳转到账号分析页（保留原有分析页入口能力）。
function jumpToAnalysis(mid) {
    closeHotspotDrawer();
    navigateTo('analysis');
    const input = document.getElementById('analysis-uid-or-url');
    if (input) input.value = String(mid);
}

// 触发一轮采集，成功后进入高频轮询；已失败状态点击可重试。
async function triggerHotspotCollect() {
    const btn = document.getElementById('hotspot-collect-btn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    btn.textContent = '采集中...';
    try {
        // 读取分区与数量选择：默认游戏区 1008（新 pid_v2），数量默认 20，可到 200（触发榜单翻页）。
        const tid = Number(document.getElementById('hotspot-tid-select')?.value || 1008);
        const limit = Number(document.getElementById('hotspot-limit-select')?.value || 20);
        const minView = Number(document.getElementById('hotspot-min-view-select')?.value || 0);
        const result = await apiRequest(`/hotspot/collect?tid=${tid}&limit=${limit}&min_view=${minView}&sample_comments=true&sample_danmaku=true`, { method: 'POST' });
        const started = (result.data || {}).started;
        if (!started) {
            // 已有任务在跑，直接继续轮询。
            btn.textContent = '采集中...';
        }
        pollHotspotCollectProgress(true);
    } catch (error) {
        btn.disabled = false;
        btn.textContent = '立即采集';
        document.getElementById('hotspot-collect-message').textContent = `启动失败：${error.message}`;
    }
}

// 轮询采集进度；任务完成或失败后恢复按钮，并展示失败明细供重试参考。
async function pollHotspotCollectProgress(highFrequency = false) {
    const progress = document.getElementById('hotspot-collect-progress');
    const message = document.getElementById('hotspot-collect-message');
    const btn = document.getElementById('hotspot-collect-btn');
    if (!progress || !message) return;
    try {
        const result = await apiRequest('/hotspot/collect/progress');
        const data = result.data || {};
        progress.value = Number(data.progress || 0);
        message.textContent = data.message || '采集进度未知';
        // 失败明细渲染在进度条下方，点击重试前可确认失败原因。
        const failed = data.failed_items || [];
        let detail = '';
        if (data.status === 'failed') {
            detail = '<p class="hotspot-collect-detail">采集异常，请检查日志或稍后重试。</p>';
        } else if (failed.length) {
            detail = `<p class="hotspot-collect-detail">失败 ${failed.length} 个：${failed.slice(0, 3).map(f => `${f.bvid}(${f.error})`).join('、')}${failed.length > 3 ? ' 等' : ''}</p>`;
        }
        const oldDetail = message.parentElement.querySelector('.hotspot-collect-detail');
        if (oldDetail) oldDetail.remove();
        if (detail) message.insertAdjacentHTML('afterend', detail);
        if (data.status === 'completed' || data.status === 'failed' || data.status === 'idle') {
            if (btn) {
                btn.disabled = false;
                btn.textContent = data.status === 'failed' ? '重试采集' : '立即采集';
            }
            if (data.status === 'completed') {
                // 新数据落库后刷新生命周期卡片。
                loadHotspotLifecycle();
            }
            // idle：后端没有采集任务在跑，停止轮询，等用户点击采集再启动，避免误显示“采集中”。
            if (data.status === 'idle') return;
            return;
        }
        if (btn) btn.textContent = '采集中...';
    } catch (error) {
        message.textContent = '进度暂不可用，请稍后重试';
    }
    // 普通轮询 5 秒一次；采集进行中 2 秒一次。
    setTimeout(() => pollHotspotCollectProgress(false), highFrequency ? 2000 : 5000);
}
