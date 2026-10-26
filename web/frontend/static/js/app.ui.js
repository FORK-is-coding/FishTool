// ===== app.ui.js —— 拆分自 app.js（UI：悬停光晕 / 弹窗 / 常驻监控 / 页面初始化），原始行 L1926-L2140 =====

/**
 * 为单个卡片绑定鼠标光晕坐标更新。
 * @param {HTMLElement} card 需要响应光标位置的卡片元素。
 * @returns {void}
 */
function bindGlowCard(card) {
    if (card.dataset.glowBound === 'true') return;
    card.dataset.glowBound = 'true';

    let frameId = 0;
    let pointer = null;

    card.addEventListener('mousemove', event => {
        pointer = { clientX: event.clientX, clientY: event.clientY };
        if (frameId) return;

        // 每帧最多写入一次 CSS 变量，避免 mousemove 高频触发布局计算。
        frameId = window.requestAnimationFrame(() => {
            frameId = 0;
            if (!pointer) return;
            const rect = card.getBoundingClientRect();
            card.style.setProperty('--x', `${pointer.clientX - rect.left}px`);
            card.style.setProperty('--y', `${pointer.clientY - rect.top}px`);
        });
    }, { passive: true });

    card.addEventListener('mouseleave', () => {
        pointer = null;
        card.style.removeProperty('--x');
        card.style.removeProperty('--y');
    });
}

/**
 * 扫描指定根节点内的卡片并绑定光晕，兼容接口动态插入的结果卡片。
 * @param {Document|Element} root 扫描起点。
 * @returns {void}
 */
function bindGlowCards(root = document) {
    if (root instanceof Element && root.matches(GLOW_SELECTOR)) {
        bindGlowCard(root);
    }
    root.querySelectorAll?.(GLOW_SELECTOR).forEach(bindGlowCard);
}

/**
 * 打开QQ群邀请弹窗并展示项目内置的群聊图片。
 * 这样图片会随前端静态资源一起打包，不依赖用户桌面上的原始文件。
 * @returns {void}
 */
function showGroupQrModal() {
    // 先移除旧弹窗，避免连续点击产生多个遮罩层。
    document.querySelector('.app-modal-backdrop')?.remove();

    const backdrop = document.createElement('div');
    backdrop.className = 'app-modal-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-label', 'QQ群邀请');

    const modal = document.createElement('div');
    modal.className = 'app-modal group-qr-modal';

    const title = document.createElement('h3');
    title.textContent = '加入QQ群';

    const closeButton = document.createElement('button');
    closeButton.className = 'btn btn-primary';
    closeButton.type = 'button';
    closeButton.textContent = '关闭';

    const image = createGroupQrImage(modal, closeButton);

    // 点击关闭按钮或遮罩空白区域都可以退出弹窗。
    const closeModal = () => backdrop.remove();
    closeButton.addEventListener('click', closeModal);
    backdrop.addEventListener('click', event => {
        if (event.target === backdrop) closeModal();
    });

    modal.append(title, image, closeButton);
    backdrop.append(modal);
    document.body.append(backdrop);
    closeButton.focus();
}

// 创建群二维码图片，缺失时保留弹窗主体并给出明确提示。
function createGroupQrImage(modal, closeButton) {
    const image = document.createElement('img');
    image.className = 'group-qr-image';
    // 使用当前页面解析静态目录，兼容 8000 端口根路径和桌面内嵌页面。
    image.src = new URL('static/群聊.jpg', document.baseURI).href;
    image.alt = 'QQ群聊邀请图片';
    // 图片缺失时保留弹窗主体并给出明确提示，避免资源错误让用户误以为点击无效。
    image.addEventListener('error', () => {
        image.remove();
        const fallback = document.createElement('p');
        fallback.textContent = '群聊图片暂不可用，请检查静态资源目录。';
        modal.insertBefore(fallback, closeButton);
    }, { once: true });
    return image;
}

/**
 * 使用统一配色的大圆角应用弹窗替代浏览器原生提示框。
 * @param {unknown} message 需要展示的提示内容。
 * @returns {void}
 */
function showAppAlert(message) {
    document.querySelector('.app-modal-backdrop')?.remove();

    const backdrop = document.createElement('div');
    backdrop.className = 'app-modal-backdrop';
    backdrop.setAttribute('role', 'alertdialog');
    backdrop.setAttribute('aria-modal', 'true');

    const modal = document.createElement('div');
    modal.className = 'app-modal';

    const title = document.createElement('h3');
    title.textContent = String(message).startsWith('错误') ? '操作未完成' : '提示';

    const content = document.createElement('p');
    content.textContent = String(message);

    const closeButton = document.createElement('button');
    closeButton.className = 'btn btn-primary';
    closeButton.textContent = '确定';

    const closeModal = () => backdrop.remove();
    closeButton.addEventListener('click', closeModal);
    backdrop.addEventListener('click', event => {
        if (event.target === backdrop) closeModal();
    });

    modal.append(title, content, closeButton);
    backdrop.append(modal);
    document.body.append(backdrop);
    closeButton.focus();
}

// ============ 高优先级错误 Toast ============
// 后端 core.toast 通过 /ws 推送 type=toast；这里只负责展示，不反向影响业务流程。
const APP_TOAST_TITLES = { info: '提示', warning: '注意', error: '操作失败' };
const APP_TOAST_MAX = 4;
let appToastSocket = null;
let appToastReconnectTimer = 0;

/**
 * 懒创建 toast 容器，固定右上角并限制最大宽度。
 * @returns {HTMLElement} toast 容器。
 */
function ensureToastStack() {
    let stack = document.getElementById('app-toast-stack');
    if (stack) return stack;
    stack = document.createElement('div');
    stack.id = 'app-toast-stack';
    stack.className = 'app-toast-stack';
    // aria-live 让读屏软件在错误出现时播报，但不抢占当前焦点。
    stack.setAttribute('aria-live', 'polite');
    stack.setAttribute('aria-atomic', 'false');
    document.body.append(stack);
    return stack;
}

/**
 * 展示一条自动消失的 toast；错误按高优先级停留更久。
 * @param {unknown} message 正文。
 * @param {{title?: string, level?: string, duration?: number}} options 展示选项。
 * @returns {void}
 */
function showToast(message, options = {}) {
    const toastOptions = options || {};
    const text = String(message ?? '').trim();
    if (!text) return;

    const level = Object.prototype.hasOwnProperty.call(APP_TOAST_TITLES, toastOptions.level)
        ? toastOptions.level
        : 'error';
    const title = String(toastOptions.title || APP_TOAST_TITLES[level]);
    const stack = ensureToastStack();

    // 同一正文在短时间内重复推送时只保留最新一条，避免轮询失败刷屏。
    Array.from(stack.children).forEach(item => {
        if (item.dataset.message === text) item.remove();
    });
    while (stack.children.length >= APP_TOAST_MAX) {
        stack.firstElementChild?.remove();
    }

    const toast = document.createElement('div');
    toast.className = `app-toast is-${level}`;
    toast.dataset.message = text;
    toast.setAttribute('role', level === 'error' ? 'alert' : 'status');

    const titleElement = document.createElement('strong');
    titleElement.className = 'app-toast-title';
    titleElement.textContent = title;

    const contentElement = document.createElement('span');
    contentElement.className = 'app-toast-message';
    contentElement.textContent = text;

    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'app-toast-close';
    closeButton.setAttribute('aria-label', '关闭提示');
    closeButton.textContent = '×';

    const removeToast = () => {
        if (!toast.isConnected) return;
        toast.classList.add('is-leaving');
        window.setTimeout(() => toast.remove(), 180);
    };
    closeButton.addEventListener('click', removeToast);

    toast.append(titleElement, contentElement, closeButton);
    stack.append(toast);

    // 错误停留 8 秒，警告 6 秒，普通提示 4 秒；调用方可显式覆盖。
    const fallbackDuration = level === 'error' ? 8000 : (level === 'warning' ? 6000 : 4000);
    const duration = Number.isFinite(toastOptions.duration) && toastOptions.duration > 0
        ? toastOptions.duration
        : fallbackDuration;
    window.setTimeout(removeToast, duration);
}

/**
 * 连接后端 /ws，接收高优先级错误 toast；断线后按 1s→30s 退避重连。
 * @returns {void}
 */
function initToastSocket() {
    if (appToastSocket || typeof window.WebSocket !== 'function') return;

    let retryDelay = 1000;
    const scheduleReconnect = () => {
        if (appToastReconnectTimer) return;
        appToastReconnectTimer = window.setTimeout(() => {
            appToastReconnectTimer = 0;
            connectToastSocket();
        }, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
    };

    const connectToastSocket = () => {
        if (appToastSocket || !window.location.host) return;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        let socket;
        try {
            socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
        } catch (error) {
            console.warn('Toast WebSocket 创建失败:', error);
            scheduleReconnect();
            return;
        }
        appToastSocket = socket;

        socket.addEventListener('open', () => {
            retryDelay = 1000;
        });
        socket.addEventListener('message', event => {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (error) {
                console.warn('Toast WebSocket 消息不是有效 JSON:', error);
                return;
            }
            if (!payload || payload.type !== 'toast') return;
            const data = payload.data || {};
            showToast(data.message, {
                title: data.title,
                level: data.level,
                duration: data.duration,
            });
        });
        socket.addEventListener('close', () => {
            if (appToastSocket === socket) appToastSocket = null;
            scheduleReconnect();
        });
        socket.addEventListener('error', () => {
            // 关闭动作交给 close 事件统一重连，避免同一次故障排两次队。
            try {
                socket.close();
            } catch (error) {
                console.warn('Toast WebSocket 关闭失败:', error);
            }
        });
    };

    connectToastSocket();
}



// ============ 评论区常驻监控控制 ============
// 使用 textContent 更新状态，兼容 FishTool 内置 Qt WebEngine 的旧 Chromium。
function setResidentText(elementId, value) {
    const element = document.getElementById(elementId);
    if (element) element.textContent = String(value);
}

// 渲染常驻监控状态卡片：同步状态徽标、最近采集时间、累计条数、失败次数与目标 BV 列表。
// data-monitor-state 属性驱动 CSS 波形/光晕动效（running=波形跳动，paused=弱光晕，stopped=静态）。
function renderResidentMonitor(data) {
    const labels = { running: '运行中', paused: '已暂停', stopped: '已停止' };
    const card = document.querySelector('.resident-monitor-card');
    const state = data.status || 'stopped';
    card?.setAttribute('data-monitor-state', state);
    setResidentText('resident-monitor-status', labels[state] || state);
    setResidentText('resident-last-collect', data.last_collect_at ? new Date(data.last_collect_at).toLocaleString() : '暂无');
    setResidentText('resident-total-collected', data.total_collected || 0);
    setResidentText('resident-failures', data.consecutive_failures || 0);
    const input = document.getElementById('resident-bvids');
    if (input && Array.isArray(data.target_bvids) && !input.value) input.value = data.target_bvids.join(',');
}

// 拉取后端常驻监控状态并刷新卡片；供页面加载与 15 秒定时器复用。
async function loadResidentMonitorStatus() {
    try { const result = await apiRequest('/comment/resident/status', {}, false); renderResidentMonitor(result.data || {}); }
    catch (error) { console.error('读取常驻监控状态失败:', error); }
}

// 从输入框读取目标 BV 列表：按逗号拆分、去空白、过滤空串后返回数组。
function residentTargets() {
    return (document.getElementById('resident-bvids')?.value || '').split(',').map(item => item.trim()).filter(Boolean);
}

// 开启常驻监控：把当前输入框的 BV 列表作为采集目标提交后端。
async function enableResidentMonitor() {
    const result = await apiRequest('/comment/resident/enable', { method: 'POST', body: JSON.stringify({ bvids: residentTargets() }) });
    renderResidentMonitor(result.data || {});
}
// 暂停常驻监控：保留目标与累计统计，任务协程仍存活但跳过采集。
async function pauseResidentMonitor() {
    const result = await apiRequest('/comment/resident/pause', { method: 'POST', body: '{}' });
    renderResidentMonitor(result.data || {});
}
// 停止常驻监控：enabled 置 False，采集主循环退出，卡片回到静态态。
async function stopResidentMonitor() {
    const result = await apiRequest('/comment/resident/stop', { method: 'POST', body: '{}' });
    renderResidentMonitor(result.data || {});
}
// 每 15 秒静默刷新一次常驻监控状态卡片，让统计与状态在无人操作时保持最新。
window.setInterval(loadResidentMonitorStatus, 15000);

// ============ 页面加载 ============
window.addEventListener('DOMContentLoaded', () => {
    bindGlowCards();
    const glowObserver = new MutationObserver(records => {
        records.forEach(record => record.addedNodes.forEach(node => {
            if (node instanceof Element) bindGlowCards(node);
        }));
    });
    glowObserver.observe(document.body, { childList: true, subtree: true });

    loadZones();
    loadHotspotLifecycle();
    // 采集进度轮询由 app.hotspot.js 内部递归驱动：采集中 2s 一次，平时 5s 一次。
    pollHotspotCollectProgress(true);
    loadAnalysisZones();
    // 加载评论区常驻监控状态卡片。
    loadResidentMonitorStatus();
    loadAuthStatus();
    // 订阅后端高优先级错误 toast（断线自动重连，不阻塞页面其它初始化）。
    initToastSocket();
});

// 显式导出弹窗入口，兼容模板 onclick、内嵌浏览器和普通浏览器三种调用方式。
Object.assign(window, {
    showGroupQrModal,
    showAppAlert,
    showToast,
    verifyLotteryWinners,
});
