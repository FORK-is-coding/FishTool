// ===== app.theme.js —— 暗色主题切换 =====
// 顶栏右上角灯泡按钮：点击在浅色 / 暗色之间切换，选择持久化到 localStorage（key: fishtool.theme）。
// 首帧恢复在 index.html <head> 的内联脚本里同步完成，这里只负责按钮状态与图表联动。
// 只用 ES2019 语法（桌面端 QtWebEngine = Chromium 83）。
(function () {
    'use strict';

    var THEME_KEY = 'fishtool.theme';
    var root = document.documentElement;

    function isDark() {
        return root.getAttribute('data-theme') === 'dark';
    }

    // 写入并广播主题变化：图表等已渲染的 canvas 内容不会跟随 CSS 变量自动重绘，
    // 各业务脚本监听 fishtool:themechange 事件自行重取颜色。
    function applyTheme(dark) {
        if (dark) {
            root.setAttribute('data-theme', 'dark');
        } else {
            root.removeAttribute('data-theme');
        }
        try {
            window.localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
        } catch (error) { /* localStorage 不可用时只本次会话生效 */ }
        // Chromium 83 支持 CustomEvent 与 dispatchEvent；老环境没有时静默跳过（图表保持旧配色）。
        if (typeof window.CustomEvent === 'function') {
            root.dispatchEvent(new CustomEvent('fishtool:themechange', {
                bubbles: true,
                detail: { theme: dark ? 'dark' : 'light' }
            }));
        }
        syncButton();
    }

    function toggleTheme() {
        applyTheme(!isDark());
    }

    // 按钮可访问状态同步：aria-pressed / title / aria-label 跟随当前主题。
    function syncButton() {
        var btn = document.getElementById('theme-toggle-btn');
        if (!btn) return;
        var dark = isDark();
        btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
        var label = dark ? '切换回浅色模式' : '切换暗色模式';
        btn.setAttribute('title', label);
        btn.setAttribute('aria-label', label);
    }

    function initThemeToggle() {
        var btn = document.getElementById('theme-toggle-btn');
        if (!btn) return;
        btn.addEventListener('click', toggleTheme);
        syncButton();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initThemeToggle);
    } else {
        initThemeToggle();
    }

    // 显式导出，兼容模板 onclick 与测试沙箱。
    window.FishTheme = { toggle: toggleTheme, isDark: isDark };
})();
