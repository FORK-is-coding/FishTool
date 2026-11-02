// ===== app.notice.js —— 欢迎页启动公告 =====
// 每次启动进入欢迎页时弹一次：桌面版每次启动都是新会话；浏览器里同一标签页刷新不重复弹。
// 开场动画播放期间（<html class="intro-pending">）先不弹，等开场结束再弹。
// 截图模式 ?shot=1 不弹，与 intro-gate.js 一致。
// 经 mountAppModal 挂载：Esc、切换页面都会收起；点遮罩空白处不关闭，需要点按钮。
// 依赖 app.core.js 的 navigateTo 与 app.ui.js 的 mountAppModal，必须在 app.ui.js 之后加载。
// 桌面端是 Chromium 83：不用 replaceAll、||= 等 84 以后的语法。
(function () {
    const SHOWN_KEY = 'fishtool.notice.v1';

    const NOTICE_HTML = `
        <span class="welcome-notice-hollow" aria-hidden="true">WELCOME</span>
        <span class="welcome-notice-kicker">NOTICE.// <b>FISHTOOL</b> · FREE &amp; OPEN SOURCE</span>
        <h3 id="welcome-notice-title">欢迎使用 FishTool！</h3>
        <div class="welcome-notice-body">
            <p>感谢您使用 FishTool！FishTool 是一款可接入多种 AI 大模型的<strong>个人创作者运营工作台</strong>，内置多项强大功能，希望能为您带来高效、愉快的创作赋能。</p>
            <p>我们想特别说明：FishTool 是以 <strong>AGPL-3.0</strong> 协议发布的<strong>免费开源软件项目</strong>，您可以在官方社群、GitHub 上找到 FishTool 的全部源代码及相关资源。截至目前，FishTool 项目<strong>未开展任何形式的商业化服务</strong>，官方<strong>不会以任何名义向用户收取费用</strong>。</p>
            <p class="welcome-notice-alert">如果您在获取或使用 FishTool 的过程中被要求付费，表明您<strong>已经遭遇诈骗行为</strong>。请<strong>立即向相关方申请退款、举报</strong>，并及时通过邮件向我们反馈。</p>
            <p>📈 在使用本项目之前，请仔细阅读 <a href="#usage" class="welcome-notice-link" onclick="navigateTo('usage'); return false;">使用说明</a>。<strong>安装即表示您已阅读并同意其中的全部内容。</strong></p>
            <p>📮 反馈邮箱：<strong class="welcome-notice-mail">3575029912@qq.com</strong></p>
        </div>`;

    /** 本会话是否已经弹过。sessionStorage 不可用时按没弹过处理。 */
    function alreadyShown() {
        try { return window.sessionStorage.getItem(SHOWN_KEY) === '1'; } catch (e) { return false; }
    }

    function markShown() {
        try { window.sessionStorage.setItem(SHOWN_KEY, '1'); } catch (e) { /* 不可写时只影响刷新后是否重弹 */ }
    }

    function screenshotMode() {
        try { return new URLSearchParams(window.location.search).get('shot') === '1'; } catch (e) { return false; }
    }

    function onWelcomePage() {
        const page = document.getElementById('welcome-page');
        return !!page && page.classList.contains('active');
    }

    function makeButton(className, text) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = className;
        button.textContent = text;
        return button;
    }

    /**
     * 挂载启动公告弹窗。
     * @returns {HTMLElement} 弹窗遮罩元素。
     */
    function showWelcomeNotice() {
        const backdrop = document.createElement('div');
        backdrop.className = 'app-modal-backdrop welcome-notice-backdrop';
        backdrop.setAttribute('role', 'dialog');
        backdrop.setAttribute('aria-modal', 'true');
        backdrop.setAttribute('aria-labelledby', 'welcome-notice-title');

        const modal = document.createElement('div');
        modal.className = 'app-modal welcome-notice-modal';
        modal.innerHTML = NOTICE_HTML;

        const actions = document.createElement('div');
        actions.className = 'app-modal-actions';
        const usageButton = makeButton('btn btn-line', '阅读使用说明');
        const okButton = makeButton('btn btn-primary', '我已知晓');
        actions.append(usageButton, okButton);
        modal.append(actions);
        backdrop.append(modal);

        const close = mountAppModal(backdrop);
        okButton.addEventListener('click', close);
        // navigateTo 会先收起所有浮层，再切到使用说明页。
        usageButton.addEventListener('click', () => navigateTo('usage'));
        markShown();
        okButton.focus();
        return backdrop;
    }

    /** 开场还没结束：<html> 带 intro-pending，或开场层 #fish-intro 还在页面上。 */
    function introRunning() {
        return document.documentElement.classList.contains('intro-pending') || !!document.getElementById('fish-intro');
    }

    /**
     * 开场结束后再执行。intro-pending 在开场层推出动画开始时就去掉了，
     * 开场层（z-index 1200）要到动画收尾才移除，收尾时还会把焦点交给欢迎页导航项，
     * 所以要等两者都结束，再晚一帧弹出，焦点才会落在弹窗按钮上。
     */
    function whenIntroDone(callback) {
        if (!introRunning()) {
            callback();
            return;
        }
        const observer = new MutationObserver(() => {
            if (introRunning()) return;
            observer.disconnect();
            window.setTimeout(callback, 0);
        });
        observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'], childList: true, subtree: true });
    }

    function initWelcomeNotice() {
        if (alreadyShown() || screenshotMode()) return;
        whenIntroDone(() => {
            if (onWelcomePage() && !alreadyShown()) showWelcomeNotice();
        });
    }

    if (document.readyState === 'loading') {
        window.addEventListener('DOMContentLoaded', initWelcomeNotice);
    } else {
        initWelcomeNotice();
    }
})();
