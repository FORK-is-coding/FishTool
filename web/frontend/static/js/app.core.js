// ===== app.core.js —— 拆分自 app.js（核心：全局变量 / 导航 / 通用工具函数），原始行 L1-L146 =====
// ============================================================================
// B站运营工具箱 - 前端主脚本
// 功能分区：导航 / 工具函数 / 热点发现 / 评论监控 / 配置管理 / 日志查看 /
//          UP主拆解&账号自诊 / B站登录态 / 统一悬停光晕 / 评论区常驻监控
// 所有接口统一走 API_BASE 前缀，由后端 FastAPI 路由承接。
// ============================================================================
const API_BASE = '/api';
const commentCharts = {};
let commentDashboardLoaded = false;
let currentLotteryWinners = [];

// ============ 导航功能 ============
function navigateTo(pageName) {
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
}

// 导航点击事件
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        navigateTo(item.dataset.page);
    });
});

// ============ 工具函数 ============

// 统一封装 fetch 请求：自动拼接 API_BASE、带 JSON 头；失败时可选弹窗提示。
// showError=false 用于轮询类静默请求，避免高频失败打断用户操作。
async function apiRequest(url, options = {}, showError = true) {
    try {
        const response = await fetch(`${API_BASE}${url}`, {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '请求失败');
        }
        
        return await response.json();
    } catch (error) {
        console.error('API请求失败:', error);
        if (showError) showAppAlert(`错误: ${error.message}`);
        throw error;
    }
}

// 把后端返回的预计剩余秒数格式化为人类可读文案；无估算时提示正在计算。
function formatEstimatedTime(seconds) {
    if (seconds === null || seconds === undefined) return '正在根据实际处理速度计算';
    if (seconds < 60) return `预计还需约 ${Math.max(1, seconds)} 秒`;
    return `预计还需约 ${Math.ceil(seconds / 60)} 分钟`;
}

// 在指定容器渲染带进度条、百分比和预计时间的加载状态，覆盖长耗时任务。
// options: { progress: 0-100, message: 提示文案, eta: 预计剩余秒数 }
function showLoading(elementId, options = {}) {
    const element = document.getElementById(elementId);
    if (!element) return;
    const progress = Math.max(0, Math.min(100, Number(options.progress) || 0));
    const message = options.message || '正在加载数据';
    const eta = options.eta ?? null;
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

// 轮询后台任务状态直到完成/失败：每次轮询都刷新加载 UI，最多 1800 次（约 30 分钟）。
async function pollTask(statusUrl, elementId) {
    for (let attempt = 0; attempt < 1800; attempt += 1) {
        const response = await apiRequest(statusUrl, {}, false);
        const task = response.data || {};
        showLoading(elementId, {
            progress: task.progress,
            message: task.message,
            eta: task.estimated_seconds,
        });
        if (task.status === 'completed') return task.result || {};
        if (task.status === 'failed') throw new Error(task.message || '任务执行失败');
        await sleep(1000);
    }
    throw new Error('任务等待超时，请稍后重试');
}

// 统一把 ISO 时间串格式化为 zh-CN 本地时间文本，空值显示未知。
function formatDate(dateString) {
    if (!dateString) return '未知';
    const date = new Date(dateString);
    return date.toLocaleString('zh-CN');
}

// 过滤外部链接：仅放行 http/https，其余（含 javascript: 伪协议）一律置空。
// 返回值已完成 HTML 转义，可直接插入 href 属性。
function safeUrl(value) {
    const url = String(value ?? '').trim();
    return /^https?:\/\//i.test(url) ? escapeHtml(url) : '';
}
