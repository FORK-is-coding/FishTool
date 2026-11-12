// ===== app.core.js —— 拆分自 app.js（核心：全局变量 / 导航 / 通用工具函数），原始行 L1-L146 =====
// ============================================================================
// B站运营工具箱 - 前端主脚本
// 功能分区：导航 / 工具函数 / 热点发现 / 评论监控 / 配置管理 / 日志查看 /
//          UP主拆解&账号自诊 / B站登录态 / 统一悬停光晕 / 评论区常驻监控
// 所有接口统一走 API_BASE 前缀，由后端 FastAPI 路由承接。
// ============================================================================
const API_BASE = '/api';

// 桌面端 QtWebEngine 5.15（Chromium 83）不支持 flex 容器的 gap（Chromium 84 起），
// 实测检测后给 <html> 加 no-flex-gap，style.flexgap.css 仅在该类下用外边距补回间距；
// 支持 gap 的浏览器不加类，兜底规则完全不生效。
// 非浏览器环境（如测试用 node vm 只桩了部分 document）直接跳过，不影响脚本其余部分加载。
(function detectFlexGapSupport() {
    const root = typeof document !== 'undefined' ? document.documentElement : null;
    if (!root || typeof document.createElement !== 'function' || !root.classList) return;
    const probe = document.createElement('div');
    probe.style.cssText = 'display:flex;flex-direction:column;row-gap:1px;position:absolute;visibility:hidden';
    probe.appendChild(document.createElement('div'));
    probe.appendChild(document.createElement('div'));
    root.appendChild(probe);
    const supported = probe.scrollHeight === 1;
    root.removeChild(probe);
    if (!supported) root.classList.add('no-flex-gap');
})();

const commentCharts = {};
let commentDashboardLoaded = false;
let currentLotteryWinners = [];

// ============ 导航功能 ============
function navigateTo(pageName) {
    // 离开当前页前收起弹窗与抽屉，避免它们残留在新页面，或回到原页面时自动弹出。
    if (typeof closeAllOverlays === 'function') closeAllOverlays();
    switchPage(pageName);

    refreshPageOnEnter(pageName);

    updateNavState(pageName);
}

// 隐藏所有页面并显示目标页面
function switchPage(pageName) {
    document.querySelectorAll('.page').forEach(page => {
        page.classList.remove('active');
    });

    const targetPage = document.getElementById(`${pageName}-page`);
    if (targetPage) {
        targetPage.classList.add('active');
    }
}

// 页面进入时的懒加载：评论页初始化图表、配置页刷新模型、日志页启动轮询
function refreshPageOnEnter(pageName) {
    // 评论页激活后再初始化图表，避免隐藏容器宽高为 0。
    if (pageName === 'comment') {
        window.requestAnimationFrame(() => {
            if (!commentDashboardLoaded) {
                loadCommentDashboard();
            } else {
                resizeCommentCharts();
            }
        });
    }

    // 每次进入配置页都刷新模型列表，确保 Base URL 变更后内容及时更新。
    if (pageName === 'config') {
        loadLLMConfig();
    }

    // 日志页进入后启动一次增量轮询，后续请求使用文件游标。
    if (pageName === 'logs') {
        startLogPolling();
    }
}

// 更新导航状态
function updateNavState(pageName) {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.remove('active');
        if (item.dataset.page === pageName) {
            item.classList.add('active');
        }
    });
    // 侧栏激活块跟随激活项平移，顶部细条同步面包屑与页码。
    moveNavIndicator();
    updateTopbarCrumb(pageName);
}

// ============ 侧栏激活块 / 顶部细条 ============
// 面包屑英文名与页码：与 index.html 各页 .page-head 的编号一致。
const PAGE_EN = {
    welcome: 'OVERVIEW', hotspot: 'SIGNALS', comment: 'MONITOR', config: 'CONFIG', analysis: 'ANALYSIS',
    'self-diagnosis': 'DIAGNOSIS', lottery: 'LOTTERY', logs: 'LOGS', usage: 'NOTICE'
};
const PAGE_NO = {
    welcome: '00', hotspot: '01', comment: '02', config: '03', analysis: '04',
    'self-diagnosis': '05', lottery: '06', logs: '07', usage: '08'
};

// 激活块与导航项同处 .nav-menu（定位上下文），CSS 中激活块为 top:0，只需平移到激活项的 offsetTop。
// 测试用 node vm 只桩了部分 document，缺 querySelector 时直接跳过。
function moveNavIndicator() {
    if (typeof document === 'undefined' || typeof document.querySelector !== 'function') return;
    const active = document.querySelector('.nav-item.active');
    const bar = document.querySelector('.nav-indicator');
    if (!active || !bar) return;
    bar.style.height = `${active.offsetHeight}px`;
    bar.style.transform = `translateY(${active.offsetTop}px)`;
}

// 顶部细条：面包屑写当前页英文名，页码写 00–08；未知页回退为空 / 00。
function updateTopbarCrumb(pageName) {
    if (typeof document === 'undefined' || typeof document.getElementById !== 'function') return;
    const crumb = document.getElementById('crumb-current');
    if (crumb) crumb.textContent = PAGE_EN[pageName] || '';
    const index = document.getElementById('tb-index');
    if (index) index.textContent = PAGE_NO[pageName] || '00';
}

// 顶部细条时钟：本地时间 HH:MM:SS，每秒刷新一次。
function tickTopbarClock() {
    if (typeof document === 'undefined' || typeof document.getElementById !== 'function') return;
    const clock = document.getElementById('topbar-clock');
    if (clock) clock.textContent = new Date().toTimeString().slice(0, 8);
}

// 页面就绪后定位一次激活块并启动时钟；窗口尺寸变化时重新定位。
// 非浏览器环境（测试用 node vm 的 window 桩没有 addEventListener）直接跳过。
(function initNavChrome() {
    if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return;
    window.addEventListener('DOMContentLoaded', () => {
        moveNavIndicator();
        tickTopbarClock();
    });
    window.addEventListener('load', moveNavIndicator);
    window.addEventListener('resize', moveNavIndicator);
    if (typeof window.setInterval === 'function') window.setInterval(tickTopbarClock, 1000);
})();

// 导航点击事件
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        navigateTo(item.dataset.page);
    });
});

// ============ 工具函数 ============

// 统一封装 fetch 请求：自动拼接 API_BASE、带 JSON 头；失败时可选提示。
// showError=false 用于轮询类静默请求，避免高频失败打断用户操作。
async function apiRequest(url, options = {}, showError = true) {
    let responseStatus = 0;
    try {
        const response = await fetch(`${API_BASE}${url}`, {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        });
        responseStatus = response.status;
        
        if (!response.ok) {
            // FastAPI 的 detail 可能是字符串，也可能是 {error_code, message} 结构。
            const payload = await response.json().catch(() => ({}));
            const detail = payload && payload.detail !== undefined ? payload.detail : payload;
            let message;
            if (typeof detail === 'string' && detail) {
                message = detail;
            } else if (Array.isArray(detail)) {
                // pydantic 校验错误是 [{loc, msg, type}, ...]：拼接各项 msg，取不到再回退状态码文案。
                const messages = detail
                    .map(item => (item && typeof item === 'object' ? item.msg : item))
                    .filter(msg => typeof msg === 'string' && msg);
                message = messages.length ? messages.join('；') : `请求失败（HTTP ${response.status}）`;
            } else if (detail && typeof detail === 'object') {
                message = detail.message || detail.error_code || `请求失败（HTTP ${response.status}）`;
            } else {
                message = `请求失败（HTTP ${response.status}）`;
            }
            const error = new Error(message);
            error.status = response.status;
            // 保留后端原始 detail，供调用方按 error_code 分支（如 app.events.js 的 eventApiErrorCode）。
            error.detail = payload ? payload.detail : undefined;
            throw error;
        }
        
        return await response.json();
    } catch (error) {
        console.error('API请求失败:', error);
        if (showError) {
            // 5xx 属于高优先级错误：优先用 toast；后端也会通过 /ws 推同文案，前端按文案去重。
            // 弹窗仅作为旧浏览器 / toast 脚本缺失时的兜底。
            if (responseStatus >= 500 && typeof showToast === 'function') {
                showToast(error.message, { level: 'error', title: '操作失败' });
            } else {
                showAppAlert(`错误: ${error.message}`);
            }
        }
        throw error;
    }
}

// 把后端返回的预计剩余秒数格式化为人类可读文案；无估算时提示正在计算。
function formatEstimatedTime(seconds) {
    if (seconds === null || seconds === undefined) return '正在根据实际处理速度计算';
    if (seconds < 60) return `预计还需约 ${Math.max(1, seconds)} 秒`;
    return `预计还需约 ${Math.ceil(seconds / 60)} 分钟`;
}

// 在指定容器渲染加载状态：有真实进度时显示百分比/进度条，未知进度时只显示转圈。
// options: { progress: >0 的 0-100 数值或 null/undefined, message: 提示文案, eta: 预计剩余秒数 }
function showLoading(elementId, options = {}) {
    const element = document.getElementById(elementId);
    if (!element) return;
    const message = options.message || '正在加载数据';
    const eta = options.eta ?? null;
    const rawProgress = options.progress;
    const numericProgress = Number(rawProgress);
    // 0 / null / undefined 都视为「还没有可展示的真实进度」：
    // 不再画一根停在 0% 的假进度条，直接显示转圈 loading。
    const hasProgress = rawProgress !== null
        && rawProgress !== undefined
        && Number.isFinite(numericProgress)
        && numericProgress > 0;

    if (!hasProgress) {
        element.innerHTML = `
            <div class="loading-state loading-state--indeterminate" role="status" aria-live="polite">
                <div class="loading-spinner" aria-hidden="true"></div>
                <div class="loading-copy">
                    <strong>${escapeHtml(message)}</strong>
                    <span class="loading-status">处理中</span>
                </div>
                <p>${eta !== null ? formatEstimatedTime(eta) : '正在处理，请稍候…'}</p>
            </div>`;
        return;
    }

    const progress = Math.round(Math.max(0, Math.min(100, numericProgress)));
    element.innerHTML = `
        <div class="loading-state" role="status" aria-live="polite">
            <div class="loading-spinner" aria-hidden="true"></div>
            <div class="loading-copy">
                <strong>${escapeHtml(message)}</strong>
                <span class="loading-percentage">${progress}%</span>
            </div>
            <div class="loading-track" aria-label="加载进度 ${progress}%">
                <span style="width:${progress}%"></span>
            </div>
            <p>${formatEstimatedTime(eta)}</p>
        </div>`;
}

// 简易 Promise 版延时，供轮询循环在两次请求之间使用。
function sleep(milliseconds) {
    return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

// 轮询后台任务状态直到完成/失败：每次轮询都刷新加载 UI，默认最多 1800 次、间隔 1 秒（约 30 分钟）。
// options: { maxAttempts: 最大轮询次数, intervalMs: 轮询间隔毫秒 }，不传时保持原默认值。
// 达到上限时任务可能仍在后台运行：抛出的错误带 code='TASK_STILL_RUNNING'，调用方可据此保留任务继续查看。
// 任务被取消时抛出 code='TASK_CANCELLED' 的错误；服务重启打断时抛出 code='TASK_INTERRUPTED' 的错误。
async function pollTask(statusUrl, elementId, options = {}) {
    const maxAttempts = options.maxAttempts ?? 1800;
    const intervalMs = options.intervalMs ?? 1000;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        const response = await apiRequest(statusUrl, {}, false);
        const task = response.data || {};
        showLoading(elementId, {
            progress: task.progress,
            message: task.message,
            eta: task.estimated_seconds,
        });
        if (task.status === 'completed') return task.result || {};
        if (task.status === 'failed') throw new Error(task.message || '任务执行失败');
        if (task.status === 'interrupted') {
            // 服务重启打断的任务：带 code 供调用方提供「继续执行」，其他任务类型不会出现该状态。
            const interruptedError = new Error(task.message || '任务已中断');
            interruptedError.code = 'TASK_INTERRUPTED';
            throw interruptedError;
        }
        if (task.status === 'cancelled') {
            // 用户已取消：带 code 供调用方区分「取消」与「失败」，其他任务类型不会出现该状态。
            const cancelledError = new Error(task.message || '任务已取消');
            cancelledError.code = 'TASK_CANCELLED';
            throw cancelledError;
        }
        await sleep(intervalMs);
    }
    // 文案保持不变，其他调用方行为不受影响；code 供需要区分“仍在运行”的调用方使用。
    const timeoutError = new Error('任务等待超时，请稍后重试');
    timeoutError.code = 'TASK_STILL_RUNNING';
    throw timeoutError;
}

// 统一把 ISO 时间串格式化为 zh-CN 本地时间文本，空值显示未知。
function formatDate(dateString) {
    if (!dateString) return '未知';
    const date = new Date(dateString);
    return date.toLocaleString('zh-CN');
}

// 转义接口文本，避免昵称或AI大模型内容被当作HTML执行。
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    })[char]);
}

// 内联事件处理器参数：先 JSON 编码成 JS 字符串字面量，再做 HTML 属性转义。
// 浏览器先解实体再解析 JS，单靠 escapeHtml 无法阻止跳出 JS 字符串。
function jsArg(value) { return escapeHtml(JSON.stringify(String(value ?? ''))); }

// 视频输入识别：口径与 bilibili/link.py 一致，改规则时两边同步。
// 第 1 组 = b23.tv 短链（第 2 组为路径），第 3 组 = 文本中独立出现的 BV 号。
const VIDEO_INPUT_PATTERN = /((?:https?:\/\/)?(?:b23\.tv|bili2233\.cn)\/([0-9A-Za-z]+))|(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])/i;

/**
 * 从用户输入里取出 BV 号：纯 BV 号、浏览器地址栏的视频链接、带 BV 号的分享文字都能认。
 * b23.tv 短链要联网才知道指向哪个视频，暂不支持（``b23.tv/BV…`` 这种直接带 BV 号的除外）。
 * @param {string} value 用户输入。
 * @returns {{bvid: string, error: string}} 认出时 error 为空；输入为空时两者都为空，由调用方提示必填。
 */
function parseVideoInput(value) {
    const text = String(value ?? '').trim();
    if (!text) return { bvid: '', error: '' };
    const match = VIDEO_INPUT_PATTERN.exec(text);
    if (!match) return { bvid: '', error: '没有识别到 BV 号，请填写 BV 号或视频链接' };
    if (match[3]) return { bvid: match[3], error: '' };
    if (/^BV[0-9A-Za-z]{10}$/i.test(match[2])) return { bvid: match[2], error: '' };
    return { bvid: '', error: '暂不支持 b23.tv 短链，请在浏览器打开后复制地址栏里的链接，或直接填 BV 号' };
}

// 接口列表字段归一：数组原样返回，null/undefined/对象/字符串等一律视为空列表，避免渲染时抛错。
function asArray(value) {
    return Array.isArray(value) ? value : [];
}

// 过滤外部链接：仅放行 http/https，其余（含 javascript: 伪协议）一律置空。
// 返回值已完成 HTML 转义，可直接插入 href 属性。
function safeUrl(value) {
    const url = String(value ?? '').trim();
    return /^https?:\/\//i.test(url) ? escapeHtml(url) : '';
}
