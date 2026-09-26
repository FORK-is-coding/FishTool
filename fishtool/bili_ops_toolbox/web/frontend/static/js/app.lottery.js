// ===== app.lottery.js —— 拆分自 app.js（抽奖工具：真人筛选 / 验奖 / 开奖），原始行 L1604-L1925 =====
// ============ 抽奖工具 ============

/** 返回当前可配置的真人筛选侧重点模板。 */
function getLotteryFocusTemplate() {
    return document.getElementById('lottery-focus-template')?.value.trim() || '';
}

/** 渲染真人判定卡片，直接展示后端生成的运营分析文本。 */
function renderLotteryUser(item) {
    const profile = item.profile || {};
    const suspicious = item.classification === 'suspicious';
    // 后端保证 analysis_text 为自然语言；兼容旧响应时只提取文本，不渲染对象/JSON。
    const rawReasons = Array.isArray(item.reasons) ? item.reasons : [item.reasons];
    const textReasons = rawReasons
        .flatMap(value => {
            if (typeof value === 'string') {
                try {
                    const parsed = JSON.parse(value);
                    return typeof parsed === 'object' ? (parsed.reasons || parsed.reason || parsed.analysis || []) : [value];
                } catch (_) {
                    return [value];
                }
            }
            return typeof value === 'object' && value !== null
                ? (value.reasons || value.reason || value.analysis || [])
                : [value];
        })
        .filter(value => typeof value === 'string' && value.trim())
        .join('；');
    const analysis = String(item.analysis_text || textReasons || '暂无运营分析').slice(0, 100);
    return `
        <article class="lottery-user-card ${suspicious ? 'is-suspicious' : 'is-real'}">
            <div class="lottery-user-heading">
                <strong>${escapeHtml(profile.name || `UID ${item.uid}`)}</strong>
                <span>${suspicious ? '疑似抽奖号' : '真人候选'}</span>
            </div>
            <div class="lottery-user-metadata">${renderLotteryMetadata({ ...profile, ...item })}</div>
            <p>UID ${escapeHtml(item.uid)} · 投稿 ${Number(profile.video_count) || 0}</p>
            <p>近期活动 ${Number(profile.recent_activity_count) || 0} · 抽奖转发占比 ${((Number(profile.lottery_repost_ratio) || 0) * 100).toFixed(0)}%</p>
            <p class="lottery-reasons">${escapeHtml(analysis)}</p>
            <small>置信度 ${Math.round((Number(item.confidence) || 0) * 100)}% · ${item.source === 'llm' ? 'AI受限数据判定' : '可解释规则兜底'}</small>
        </article>`;
}

/** 启动评论区全用户筛选并持续展示采集与 AI 阶段进度。 */
async function startRealUserFilter() {
    const target = document.getElementById('lottery-filter-target')?.value.trim();
    const button = document.getElementById('lottery-filter-button');
    if (!target) return showAppAlert('请先输入视频 BV 号或动态完整链接');
    button.disabled = true;
    showLoading('lottery-filter-result', { progress: 0, message: '正在建立筛选任务，随后将优先检查本地评论数据' });
    try {
        const started = await apiRequest('/lottery/filter/tasks', {
            method: 'POST',
            body: JSON.stringify({ target, focus_template: getLotteryFocusTemplate() }),
        });
        const result = await pollTask(`/lottery/tasks/${started.task_id}`, 'lottery-filter-result');
        const realUsers = Array.isArray(result.real_users) ? result.real_users : [];
        const suspiciousUsers = Array.isArray(result.suspicious_users) ? result.suspicious_users : [];
        document.getElementById('lottery-filter-result').innerHTML = `
            <div class="lottery-summary-strip">
                <span>评论 ${Number(result.comment_count) || 0}</span>
                <span>用户 ${Number(result.user_count) || 0}</span>
                <span>真人 ${realUsers.length}</span>
                <span>疑似 ${suspiciousUsers.length}</span>
                <span>${result.data_source === 'local' ? '本地数据复用' : '现有爬虫采集'}</span>
            </div>
            <div class="lottery-result-columns">
                <section><h3>真人候选</h3>${realUsers.map(renderLotteryUser).join('') || '<p class="card-empty">暂无真人候选</p>'}</section>
                <section><h3>疑似抽奖号</h3>${suspiciousUsers.map(renderLotteryUser).join('') || '<p class="card-empty">暂无疑似账号</p>'}</section>
            </div>`;
    } catch (error) {
        document.getElementById('lottery-filter-result').innerHTML = `<p class="analysis-error">筛选失败：${escapeHtml(error.message)}</p>`;
    } finally {
        button.disabled = false;
    }
}

/** 针对单 UID 执行画像采集和强制模板注入分析。 */
async function quickFilterUser() {
    const uid = Number(document.getElementById('lottery-quick-uid')?.value);
    const button = document.getElementById('lottery-quick-button');
    if (!Number.isInteger(uid) || uid <= 0) return showAppAlert('请输入有效的用户 UID');
    button.disabled = true;
    showLoading('lottery-quick-result', {
        progress: 18,
        message: '正在核验该 UID 的等级、公开活动和抽奖转发特征，随后交给 AI 受限判定',
    });
    try {
        const response = await apiRequest('/lottery/quick-filter', {
            method: 'POST',
            body: JSON.stringify({ uid, focus_template: getLotteryFocusTemplate() }),
        });
        const data = response.data || {};
        document.getElementById('lottery-quick-result').innerHTML = renderLotteryUser({
            ...(data.assessment || {}), profile: data.profile || {},
        });
    } catch (error) {
        document.getElementById('lottery-quick-result').innerHTML = `<p class="analysis-error">快速筛选失败：${escapeHtml(error.message)}</p>`;
    } finally {
        button.disabled = false;
    }
}

/** 一键校验当前中奖名单，并路由到随机抽奖中奖名单区域。 */
async function verifyLotteryWinners() {
    navigateTo('lottery');
    const resultHost = document.getElementById('lottery-winner-verification-result');
    const drawCard = document.querySelector('.lottery-draw-card');
    const button = document.getElementById('lottery-verify-winners-button');
    drawCard?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (!currentLotteryWinners.length) return showAppAlert('请先进行抽奖！');
    button.disabled = true;
    showLoading('lottery-winner-verification-result', {
        progress: 25,
        message: '正在优先检索本地画像，缺失用户将通过低频 B 站请求补齐后交给 AI 校验',
    });
    try {
        const response = await apiRequest('/lottery/verify-winners', {
            method: 'POST',
            body: JSON.stringify({ winners: currentLotteryWinners, focus_template: getLotteryFocusTemplate() }),
        });
        renderVerifyWinnersResult(response.data || {}, resultHost);
    } catch (error) {
        resultHost.innerHTML = `<p class="analysis-error">中奖名单校验失败：${escapeHtml(error.message)}</p>`;
    } finally {
        button.disabled = false;
    }
}

// 渲染中奖名单 AI 真人校验结果：汇总条 + 真人/疑似两栏。
function renderVerifyWinnersResult(data, resultHost) {
    const results = Array.isArray(data.results) ? data.results : [];
    resultHost.innerHTML = `
        <div class="lottery-summary-strip">
            <span>中奖 ${Number(data.winner_count) || 0}</span>
            <span>真人 ${Number(data.real_count) || 0}</span>
            <span>疑似 ${Number(data.suspicious_count) || 0}</span>
            <span>本地画像 ${Number(data.local_count) || 0}</span>
            <span>在线补取 ${Number(data.fetched_count) || 0}</span>
        </div>
        <section class="lottery-winner-section">
            <h3>中奖名单 AI 真人校验</h3>
            <div class="lottery-result-columns">
                <section><h3>真人候选</h3>${results.filter(item => item.classification === 'real').map(renderLotteryUser).join('') || '<p class="card-empty">暂无真人候选</p>'}</section>
                <section><h3>疑似抽奖号</h3>${results.filter(item => item.classification === 'suspicious').map(renderLotteryUser).join('') || '<p class="card-empty">暂无疑似账号</p>'}</section>
            </div>
        </section>`;
}

/** 使用项目统一暖色弹窗询问是否确认开始抽奖。 */
function confirmLotteryTarget(metadata) {
    return new Promise(resolve => {
        document.querySelector('.app-modal-backdrop')?.remove();
        const { backdrop, confirm } = buildLotteryConfirmModal(metadata, resolve);
        document.body.append(backdrop);
        confirm.focus();
    });
}

// 构建抽奖确认弹窗，点击确认/取消/遮罩都会结束 Promise。
function buildLotteryConfirmModal(metadata, resolve) {
    const backdrop = document.createElement('div');
    backdrop.className = 'app-modal-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    const modal = document.createElement('div');
    modal.className = 'app-modal lottery-confirm-modal';
    const title = document.createElement('h3');
    title.textContent = '确认抽奖内容';
    const copy = document.createElement('p');
    copy.textContent = `标题：${metadata.title}\n发布者：${metadata.author}\n\n确认后将优先复用本地评论；仅在无缓存时启动低频采集。`;
    const actions = document.createElement('div');
    actions.className = 'lottery-modal-actions';
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-secondary';
    cancel.textContent = '返回检查';
    const confirm = document.createElement('button');
    confirm.className = 'btn btn-primary';
    confirm.textContent = '确认开始';
    const finish = value => { backdrop.remove(); resolve(value); };
    cancel.addEventListener('click', () => finish(false));
    confirm.addEventListener('click', () => finish(true));
    backdrop.addEventListener('click', event => { if (event.target === backdrop) finish(false); });
    actions.append(cancel, confirm);
    modal.append(title, copy, actions);
    backdrop.append(modal);
    return { backdrop, confirm };
}

/** 将评论时间格式化为候选卡片中的紧凑日期时间。 */
function formatLotteryCommentTime(value) {
    if (!value) return '时间未知';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? '时间未知' : parsed.toLocaleString('zh-CN', { hour12: false });
}

/** 渲染等级、会员和评论时间的圆角矩形字段徽标。 */
function renderLotteryMetadata(user) {
    const numericLevel = Number(user.level);
    const hasKnownLevel = user.level !== null && user.level !== undefined && Number.isInteger(numericLevel) && numericLevel >= 0 && numericLevel <= 6;
    const level = hasKnownLevel ? numericLevel : null;
    const vipType = Number(user.vip_type || 0);
    const vipText = user.vip_label || (vipType === 2 ? '年度大会员' : (user.is_vip === true ? '大会员' : (user.is_vip === false ? '非会员' : '会员未知')));
    const vipClass = vipType === 2 ? 'annual' : (user.is_vip === true ? 'active' : 'none');
    const vipSymbol = vipType === 2 ? '◆' : (user.is_vip === true ? '♦' : '◇');
    const commentTime = formatLotteryCommentTime(user.ctime);
    return `<span class="lottery-user-icons">
        <span class="lottery-meta-icon level level-${level ?? 0}" title="用户等级">Lv${level ?? '?'}</span>
        <span class="lottery-meta-icon vip ${vipClass}" title="${escapeHtml(vipText)}"><span aria-hidden="true">${vipSymbol}</span>${escapeHtml(vipText)}</span>
        <time class="lottery-meta-icon time" title="评论发布时间"><span aria-hidden="true">◷</span>${escapeHtml(commentTime)}</time>
    </span>`;
}

/** 渲染随机抽奖候选用户，昵称后展示等级、会员与评论时间图标。 */
function renderLotteryCandidate(candidate) {
    return `<article class="lottery-candidate-row">
        <div class="lottery-candidate-name"><strong>${escapeHtml(candidate.uname || `UID ${candidate.uid}`)}</strong>${renderLotteryMetadata(candidate)}</div>
        <span>UID ${escapeHtml(candidate.uid)}</span>
        <p>${escapeHtml(candidate.content || '无评论内容')}</p>
    </article>`;
}

/** 读取并校验抽奖筛选条件。 */
function getLotteryDrawFilters() {
    const dateStart = document.getElementById('lottery-date-start')?.value || null;
    const dateEnd = document.getElementById('lottery-date-end')?.value || null;
    if (dateStart && dateEnd && dateStart > dateEnd) throw new Error('评论开始日期不能晚于结束日期');
    const levelValue = document.getElementById('lottery-min-level')?.value ?? '';
    return {
        vip_only: Boolean(document.getElementById('lottery-vip-only')?.checked),
        min_level: levelValue === '' ? null : Number(levelValue),
        date_start: dateStart,
        date_end: dateEnd,
    };
}

/** 读取标题与发布者，得到明确确认后才创建抽奖任务。 */
async function previewLotteryDraw() {
    const target = document.getElementById('lottery-draw-target')?.value.trim();
    const winnerCount = Number(document.getElementById('lottery-winner-count')?.value);
    const uniqueUsers = Boolean(document.getElementById('lottery-unique-users')?.checked);
    let filters;
    try {
        filters = getLotteryDrawFilters();
    } catch (error) {
        return showAppAlert(error.message);
    }
    const button = document.getElementById('lottery-preview-button');
    if (!target) return showAppAlert('请先输入视频 BV 号或动态完整链接');
    if (!Number.isInteger(winnerCount) || winnerCount < 1 || winnerCount > 100) return showAppAlert('中奖人数须为 1 至 100');
    button.disabled = true;
    document.getElementById('lottery-preview-result').innerHTML = '<span class="mini-spinner"></span><span>正在读取标题与发布者，帮你排除输错内容的风险...</span>';
    try {
        const response = await apiRequest('/lottery/preview', { method: 'POST', body: JSON.stringify({ target }) });
        const metadata = response.data || {};
        document.getElementById('lottery-preview-result').innerHTML = `
            <strong>${escapeHtml(metadata.title || '未获取到标题')}</strong>
            <span>发布者：${escapeHtml(metadata.author || '未知')}</span>`;
        if (!await confirmLotteryTarget(metadata)) return;
        await runLotteryDraw(target, winnerCount, uniqueUsers, filters);
    } catch (error) {
        document.getElementById('lottery-draw-result').innerHTML = `<p class="analysis-error">抽奖准备失败：${escapeHtml(error.message)}</p>`;
    } finally {
        button.disabled = false;
    }
}

/** 创建随机抽奖任务并渲染中奖名单。 */
async function runLotteryDraw(target, winnerCount, uniqueUsers, filters) {
    showLoading('lottery-draw-result', {
        progress: 0,
        message: '正在检索工具箱本地目录与数据库，找不到可复用评论后才会启动爬虫',
    });
    const started = await apiRequest('/lottery/draw/tasks', {
        method: 'POST',
        body: JSON.stringify({ target, winner_count: winnerCount, unique_users: uniqueUsers, ...filters }),
    });
    const result = await pollTask(`/lottery/tasks/${started.task_id}`, 'lottery-draw-result');
    const winners = Array.isArray(result.winners) ? result.winners : [];
    currentLotteryWinners = winners.map(winner => ({ ...winner }));
    document.getElementById('lottery-winner-verification-result').innerHTML = '';
    renderLotteryDrawResult(result);
}

// 渲染抽奖结果：候选/排除汇总条 + 候选列表 + 中奖名单。
function renderLotteryDrawResult(result) {
    const winners = Array.isArray(result.winners) ? result.winners : [];
    const candidates = Array.isArray(result.candidates) ? result.candidates : [];
    const excluded = result.excluded || {};
    document.getElementById('lottery-draw-result').innerHTML = `
        <div class="lottery-summary-strip">
            <span>候选 ${Number(result.candidate_count) || 0}</span>
            <span>等级排除 ${Number(excluded.level) || 0}</span>
            <span>会员排除 ${Number(excluded.vip) || 0}</span>
            <span>日期排除 ${Number(excluded.date) || 0}</span>
            <span>${result.data_source === 'local' ? '本地数据复用' : '现有爬虫采集'}</span>
        </div>
        <section class="lottery-candidate-section">
            <h3>候选用户</h3>
            <div class="lottery-candidate-list">${candidates.map(renderLotteryCandidate).join('') || '<p class="card-empty">暂无符合条件的候选用户</p>'}</div>
        </section>
        <section class="lottery-winner-section">
            <h3>中奖名单</h3>
            <div class="lottery-winner-grid">${winners.map((winner, index) => `
                <article class="lottery-winner-card">
                    <span class="winner-rank">${index + 1}</span>
                    <div><div class="lottery-candidate-name"><strong>${escapeHtml(winner.uname || `UID ${winner.uid}`)}</strong>${renderLotteryMetadata(winner)}</div>
                    <p>UID ${escapeHtml(winner.uid)}</p>
                    <blockquote>${escapeHtml(winner.content || '无评论内容')}</blockquote></div>
                </article>`).join('')}</div>
        </section>`;
}

// ============ 统一悬停光晕与弹窗 ============
const GLOW_SELECTOR = [
    '.card',
    '.section',
    '.comment-dashboard',
    '.metric-item',
    '.chart-panel',
    '.voice-module',
    '.voice-item',
    '.result-area',
    '.logs-area',
    '.raw-output-details',
    '.analysis-notice',
    '.analysis-error',
    '.diagnosis-dashboard',
    '.diagnosis-score',
    '.diagnosis-detail-card',
    '.ai-research-card',
    '.account-tag-cloud-card',
    '.resident-monitor-card',
    '.lottery-user-card',
    '.lottery-candidate-row',
    '.lottery-winner-card'
].join(', ');
