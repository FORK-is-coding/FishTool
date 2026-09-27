// ===== app.auth.js —— 拆分自 app.js（B站登录态：扫码登录 / 轮询 / 登出），原始行 L1440-L1602 =====
// ============ B站登录态模块 ============
// 轮询/倒计时定时器句柄，切换页面或关闭面板时必须清理
let qrPollTimer = null;
let qrCountdownTimer = null;
let qrDeadline = 0;
let currentQrKey = '';

// 加载登录态：进入页面时调用，展示 未登录/已登录+用户名UID
async function loadAuthStatus() {
    const badge = document.getElementById('auth-status-badge');
    const userInfo = document.getElementById('auth-user-info');
    const loginBtn = document.getElementById('auth-login-btn');
    const logoutBtn = document.getElementById('auth-logout-btn');
    if (!badge) return;

    try {
        const data = await apiRequest('/auth/status');
        if (data.logged_in) {
            const user = data.user || {};
            badge.textContent = '✅ 已登录';
            badge.style.background = '#e8f5e9';
            badge.style.color = '#2e7d32';
            userInfo.innerHTML = `${escapeHtml(user.username || '未知用户')}（UID: ${escapeHtml(user.uid || '未知')}） | ${escapeHtml(data.cookie_masked || '')}`;
            logoutBtn.style.display = '';
        } else {
            badge.textContent = '未登录';
            badge.style.background = '#fff3e0';
            badge.style.color = '#e65100';
            userInfo.innerHTML = data.invalid
                ? '<span style="color:#c62828;">本地Cookie已失效，请重新扫码</span>'
                : '扫码后可解锁评论监控、视频数据采集等身份功能';
            logoutBtn.style.display = 'none';
        }
    } catch (error) {
        badge.textContent = '检测失败';
        userInfo.innerHTML = `<span style="color:#c62828;">${escapeHtml(error.message)}</span>`;
    }
}

function toggleQrPanel() {
    const panel = document.getElementById('auth-qr-panel');
    if (!panel) return;
    if (panel.style.display === 'none' || !panel.style.display) {
        panel.style.display = 'block';
        startQrLogin();
    } else {
        closeQrPanel();
    }
}

// 关闭二维码面板并停止轮询
function closeQrPanel() {
    stopQrPoll();
    const panel = document.getElementById('auth-qr-panel');
    if (panel) panel.style.display = 'none';
}

// 开始扫码登录：拿二维码 -> 启动 180s 倒计时 -> 每 2s 轮询
async function startQrLogin() {
    stopQrPoll();
    const statusEl = document.getElementById('auth-qr-status');
    const imgEl = document.getElementById('auth-qr-img');
    const countdownEl = document.getElementById('auth-qr-countdown');
    if (!statusEl) return;

    statusEl.textContent = '正在获取二维码...';
    countdownEl.textContent = '';

    try {
        const data = await apiRequest('/auth/qrcode');
        currentQrKey = data.qrcode_key;
        imgEl.src = `data:image/png;base64,${data.qrcode_base64}`;
        statusEl.textContent = '请使用B站APP扫描二维码登录';

        // 180 秒本地倒计时（二维码默认有效期约 3 分钟）
        qrDeadline = Date.now() + 180000;
        qrCountdownTimer = setInterval(updateQrCountdown, 1000);
        updateQrCountdown();

        // 每 2 秒轮询一次登录状态（后端单次查询，不循环等待）
        qrPollTimer = setInterval(pollQrLogin, 2000);
        pollQrLogin();
    } catch (error) {
        statusEl.textContent = `获取二维码失败: ${error.message}`;
    }
}

function updateQrCountdown() {
    const countdownEl = document.getElementById('auth-qr-countdown');
    if (!countdownEl) return;
    const remain = Math.max(0, Math.ceil((qrDeadline - Date.now()) / 1000));
    if (remain <= 0) {
        countdownEl.textContent = '二维码已过期，请点击"重新生成"';
        const statusEl = document.getElementById('auth-qr-status');
        if (statusEl) statusEl.textContent = '❌ 二维码已过期';
        stopQrPoll();
        return;
    }
    countdownEl.textContent = `二维码剩余有效时间：${remain} 秒`;
}

// 单次轮询登录状态（POST /api/auth/poll）
async function pollQrLogin() {
    if (!currentQrKey) return;
    const statusEl = document.getElementById('auth-qr-status');
    if (!statusEl) return;

    try {
        const data = await apiRequest('/auth/poll', {
            method: 'POST',
            body: JSON.stringify({ qrcode_key: currentQrKey })
        });
        applyQrPollStatus(data, statusEl);
    } catch (error) {
        statusEl.textContent = `查询失败: ${error.message}`;
    }
}

// 根据轮询状态更新扫码文案，并处理成功/过期等终态。
function applyQrPollStatus(data, statusEl) {
    if (data.status === 'confirmed') {
        // 三重校验通过：显示成功，关面板刷新登录态
        statusEl.textContent = data.message;
        stopQrPoll();
        setTimeout(() => {
            closeQrPanel();
            loadAuthStatus();
        }, 1000);
    } else if (data.status === 'expired') {
        statusEl.textContent = data.message;
        stopQrPoll();
    } else if (data.status === 'scanned') {
        statusEl.textContent = data.message;
    } else if (data.status === 'not_scanned') {
        // 未扫码：保持等待文案，不打断倒计时
        statusEl.textContent = '请使用B站APP扫描二维码登录';
    } else {
        // error：显示提示但继续轮询（网络抖动等）
        statusEl.textContent = data.message || '状态未知，继续等待...';
    }
}

function stopQrPoll() {
    if (qrPollTimer) {
        clearInterval(qrPollTimer);
        qrPollTimer = null;
    }
    if (qrCountdownTimer) {
        clearInterval(qrCountdownTimer);
        qrCountdownTimer = null;
    }
}

async function logoutBili() {
    if (!confirm('确定要清除B站登录态吗？清除后需要重新扫码才能使用身份功能。')) return;
    try {
        await apiRequest('/auth/logout', { method: 'POST' });
        loadAuthStatus();
        showAppAlert('已清除登录态');
    } catch (error) {
        showAppAlert(`清除失败: ${error.message}`);
    }
}
