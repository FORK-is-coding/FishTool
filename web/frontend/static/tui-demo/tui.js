/* =====================================================================
   FishTool // TTY —— tui.js
   ---------------------------------------------------------------------
   纯前端交互：标签页路由、全键盘焦点/分屏、命令行、快捷菜单、开关、
   日志过滤、帮助浮层、CRT、字符单元格鼠标光标与 PTR 坐标。
   没有网络请求，也不读写任何持久化配置；语法保持 Chromium 83 可用。
   ===================================================================== */
(function () {
    'use strict';

    var PAGES = [
        { key: 'welcome', code: 'WELCOME', label: '首页' },
        { key: 'hotspot', code: 'HOTSPOT', label: '热点' },
        { key: 'watch', code: 'WATCH', label: '评论' },
        { key: 'config', code: 'CONFIG', label: '配置' },
        { key: 'bench', code: 'BENCH', label: '拆解' },
        { key: 'diag', code: 'DIAG', label: '自诊' },
        { key: 'draw', code: 'DRAW', label: '抽奖' },
        { key: 'patrol', code: 'PATROL', label: '伪人' },
        { key: 'logs', code: 'LOGS', label: '日志' },
        { key: 'help', code: 'HELP', label: '帮助' }
    ];
    var DEFAULT_HINT = 'TAB FOCUS · ↑↓←→ MOVE · CTRL←→ PAGE · PGUP/PGDN SCREEN · : CMD · ? HELP';

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    var app = document.getElementById('app');
    var content = document.getElementById('content');
    var tabsEl = document.getElementById('tabs');
    var tabList = [].slice.call(document.querySelectorAll('.tab'));
    var pages = [].slice.call(document.querySelectorAll('.page'));
    var crumb = document.getElementById('crumb');
    var clock = document.getElementById('clock');
    var statusMsg = document.getElementById('status-msg');
    var statusFocus = document.getElementById('status-focus');
    var statusCrt = document.getElementById('status-crt');
    var statusPtr = document.getElementById('status-ptr');
    var cmd = document.getElementById('cmd');
    var cmdbar = document.querySelector('.cmdbar');
    var helpMask = document.getElementById('help-mask');
    var helpClose = document.getElementById('help-close');
    var helpDialog = helpMask.querySelector('.dialog');
    var cellPtr = document.getElementById('cell-ptr');
    var ptrTrail = [];
    var ptrTrailParticles = [];
    var ptrTrailCursor = 0;
    var ptrTrailRaf = null;
    var ptrLastCell = null;
    var charRuler = document.getElementById('char-ruler');
    var logFilters = document.getElementById('log-filters');
    var logSearch = document.getElementById('log-search');
    var logList = document.getElementById('log-list');
    var logCount = document.getElementById('log-count');
    var logLevelEl = document.getElementById('log-level');
    var logEmpty = document.getElementById('log-empty');
    var logPager = document.getElementById('log-pager');
    var logPage = 1;
    var LOG_PAGE_SIZE = 16;
    var logFiltered = [];
    var drawTitle = document.getElementById('draw-title');
    var drawModeNote = document.getElementById('draw-mode-note');

    /* 桌面壳（PySide6/QtWebEngine）在 DocumentCreation 注入该标记；
       浏览器预览没有标记，保持 CORE · LIVE。 */
    if (window.__FISHTOOL_DESKTOP__) {
        document.body.classList.add('is-desktop');
        var desktopBadge = document.querySelector('.title-demo');
        if (desktopBadge) {
            desktopBadge.textContent = 'DESKTOP · CORE';
        }
    }

    /* 桌面壳的 QWebChannel 窗口桥：无边框标题栏按钮、最大化状态与缩放回退。 */
    var desktopBridge = null;
    var desktopBridgeStarting = false;
    var winFallbackBound = false;

    function setWinMaximized(isMaximized) {
        var maxed = !!isMaximized;
        document.body.classList.toggle('is-maximized', maxed);
        var maxBtn = document.querySelector('[data-win="max"]');
        if (maxBtn) {
            maxBtn.textContent = maxed ? '▣' : '□';
            maxBtn.setAttribute('aria-label', maxed ? '还原窗口' : '最大化');
            maxBtn.setAttribute('title', maxed ? '还原窗口' : '最大化');
        }
    }

    function bindWindowFallback() {
        if (winFallbackBound) {
            return;
        }
        var titlebar = document.querySelector('.titlebar');
        if (!titlebar) {
            return;
        }
        winFallbackBound = true;
        titlebar.addEventListener('mousedown', function (event) {
            if (event.button !== 0 || closest(event.target, '.win-controls')) {
                return;
            }
            if (desktopBridge && desktopBridge.startDrag) {
                event.preventDefault();
                desktopBridge.startDrag();
            }
        });
        titlebar.addEventListener('dblclick', function (event) {
            if (closest(event.target, '.win-controls')) {
                return;
            }
            if (desktopBridge && desktopBridge.toggleMaximize) {
                desktopBridge.toggleMaximize();
            }
        });
        all('.win-resize').forEach(function (handle) {
            handle.addEventListener('mousedown', function (event) {
                if (event.button !== 0 || !desktopBridge || !desktopBridge.startResize) {
                    return;
                }
                event.preventDefault();
                desktopBridge.startResize(handle.getAttribute('data-resize') || '');
            });
        });
    }

    function initDesktopBridge() {
        if (desktopBridge || desktopBridgeStarting) {
            return;
        }
        if (!window.__FISHTOOL_DESKTOP__ || !window.QWebChannel || !window.qt || !window.qt.webChannelTransport) {
            return;
        }
        desktopBridgeStarting = true;
        try {
            new QWebChannel(window.qt.webChannelTransport, function (channel) {
                desktopBridge = channel.objects.windowBridge;
                window.fishtoolDesktop = desktopBridge;
                desktopBridgeStarting = false;
                if (!desktopBridge) {
                    return;
                }
                desktopBridge.maximizedChanged.connect(setWinMaximized);
                desktopBridge.isMaximized(function (isMaximized) {
                    setWinMaximized(isMaximized);
                });
                desktopBridge.nativeHitTest(function (native) {
                    document.body.classList.toggle('native-window', !!native);
                    if (!native) {
                        bindWindowFallback();
                    }
                });
            });
        } catch (error) {
            desktopBridgeStarting = false;
            console.error('窗口桥初始化失败:', error);
        }
    }

    all('[data-win]').forEach(function (button) {
        button.addEventListener('click', function () {
            if (!desktopBridge) {
                return;
            }
            var action = button.getAttribute('data-win');
            if (action === 'min' && desktopBridge.minimize) {
                desktopBridge.minimize();
            } else if (action === 'max' && desktopBridge.toggleMaximize) {
                desktopBridge.toggleMaximize();
            } else if (action === 'close' && desktopBridge.close) {
                desktopBridge.close();
            }
        });
    });
    initDesktopBridge();
    window.addEventListener('load', initDesktopBridge);
    window.setTimeout(initDesktopBridge, 400);

    var current = 'welcome';
    var helpOpen = false;
    var lastFocus = null;
    var statusTimer = null;
    var logLevel = 'all';
    var cmdHistory = [];
    var historyIndex = -1;

    var cellW = 7.2;
    var cellH = 20;
    var pointerEnabled = true;
    var pointerActivated = false;
    var pointerRaf = null;
    var pendingX = 0;
    var pendingY = 0;

    function all(selector, root) {
        return [].slice.call((root || document).querySelectorAll(selector));
    }

    function closest(el, selector) {
        if (!el) {
            return null;
        }
        if (el.nodeType !== 1) {
            el = el.parentNode;
        }
        return el && el.closest ? el.closest(selector) : null;
    }

    function pad2(value) {
        return value < 10 ? '0' + value : '' + value;
    }

    function pad3(value) {
        return value < 10 ? '00' + value : value < 100 ? '0' + value : '' + value;
    }

    function pageIndex(key) {
        for (var i = 0; i < PAGES.length; i += 1) {
            if (PAGES[i].key === key) {
                return i;
            }
        }
        return -1;
    }

    function tabFor(key) {
        for (var i = 0; i < tabList.length; i += 1) {
            if (tabList[i].getAttribute('data-page') === key) {
                return tabList[i];
            }
        }
        return null;
    }

    function activeTab() {
        return tabFor(current) || tabList[0];
    }

    function activePageEl() {
        return document.getElementById('page-' + current);
    }

    /* ---------------- 页面内分屏：不出现纵向滚动条 ---------------- */
    var SCREEN_BAR_HEIGHT = 30;
    var screenRaf = null;
    var screenInternal = false;

    function screenRoot(page) {
        if (!page) return null;
        var root = page.querySelector(':scope > .live-page');
        if (!root && page.id !== 'page-logs') {
            root = document.createElement('div');
            root.className = 'live-page';
            var head = page.querySelector(':scope > .page-head');
            var nodes = Array.prototype.slice.call(page.children).filter(function (node) {
                return node !== head;
            });
            page.appendChild(root);
            nodes.forEach(function (node) { root.appendChild(node); });
        }
        return root;
    }

    function screenStep(page, step) {
        var root = screenRoot(page);
        if (!root) return false;
        var screens = all('.screen', root);
        if (!screens.length) return false;
        var idx = 0;
        for (var i = 0; i < screens.length; i += 1) {
            if (screens[i].classList.contains('active')) { idx = i; break; }
        }
        return showScreen(page, idx + step);
    }

    function showScreen(page, index) {
        closeSelect();
        var root = screenRoot(page);
        if (!root) return false;
        var screens = all('.screen', root);
        if (!screens.length) return false;
        var next = Math.max(0, Math.min(screens.length - 1, index));
        screens.forEach(function (screen, i) {
            screen.classList.toggle('active', i === next);
        });
        page.setAttribute('data-screen', String(next + 1));
        var counter = root.querySelector('.screen-count');
        if (counter) counter.textContent = (next + 1) + ' / ' + screens.length;
        var bar = root.querySelector('.screenbar');
        if (bar) {
            var prev = bar.querySelector('[data-screen-step="-1"]');
            var nextBtn = bar.querySelector('[data-screen-step="1"]');
            if (prev) prev.disabled = next <= 0;
            if (nextBtn) nextBtn.disabled = next >= screens.length - 1;
        }
        var active = screens[next];
        if (active && document.activeElement && !active.contains(document.activeElement) && document.activeElement.closest && document.activeElement.closest('.screenbar')) {
            var first = all('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', active).filter(function (el) {
                return !el.disabled && el.getClientRects().length > 0;
            })[0];
            (first || active).focus();
        }
        return true;
    }

    function paginateActivePage() {
        var page = activePageEl();
        if (!page || page.id === 'page-logs' || page.hidden) return;
        var root = screenRoot(page);
        if (!root) return;
        if (document.querySelector('.tui-select-menu:not([hidden])')) return;
        var focused = document.activeElement;
        var keepFocus = focused && root.contains(focused) && focused !== root;
        var oldIndex = 0;
        var oldActive = root.querySelector('.screen.active');
        var screensOld = all('.screen', root);
        if (oldActive) oldIndex = screensOld.indexOf(oldActive);

        /* 把旧分屏展开成原始顺序的子节点，再按实际高度重新分组。 */
        var nodes = [];
        Array.prototype.slice.call(root.children).forEach(function (node) {
            if (node.classList.contains('screenbar')) return;
            if (node.classList.contains('screen')) {
                while (node.firstChild) nodes.push(node.firstChild), node.removeChild(node.firstChild);
            } else {
                nodes.push(node);
            }
        });
        var oldBar = root.querySelector('.screenbar');
        if (oldBar) oldBar.remove();
        all('.screen', root).forEach(function (screen) { screen.remove(); });
        while (root.firstChild) root.removeChild(root.firstChild);
        nodes.forEach(function (node) { root.appendChild(node); });
        if (!nodes.length) return;

        /* 始终为 SCREEN 条留出空间；单屏时条隐藏，但分组仍按安全高度计算，避免 1px 溢出。 */
        var limit = root.clientHeight - SCREEN_BAR_HEIGHT - 8;
        if (limit < 140) return;
        var groups = [];
        var currentGroup = [];
        var used = 0;
        nodes.forEach(function (node) {
            var style = window.getComputedStyle(node);
            var margin = (parseFloat(style.marginTop) || 0) + (parseFloat(style.marginBottom) || 0);
            var height = node.getBoundingClientRect().height + margin;
            if (currentGroup.length && used + height > limit) {
                groups.push(currentGroup);
                currentGroup = [];
                used = 0;
            }
            currentGroup.push(node);
            used += height;
        });
        if (currentGroup.length) groups.push(currentGroup);

        screenInternal = true;
        var bar = document.createElement('div');
        bar.className = 'screenbar';
        bar.setAttribute('role', 'group');
        bar.setAttribute('aria-label', '页面分屏');
        bar.innerHTML = '<span class="screen-label">SCREEN</span>' +
            '<button class="screen-btn" type="button" data-screen-step="-1" aria-label="上一屏">◀</button>' +
            '<span class="screen-count">1 / ' + groups.length + '</span>' +
            '<button class="screen-btn" type="button" data-screen-step="1" aria-label="下一屏">▶</button>' +
            '<span class="screen-hint">PGUP / PGDN · 方向键在屏内控件间移动</span>';
        root.appendChild(bar);
        groups.forEach(function (group, index) {
            var screen = document.createElement('div');
            screen.className = 'screen' + (index === oldIndex ? ' active' : '');
            screen.setAttribute('data-screen-index', String(index));
            group.forEach(function (node) { screen.appendChild(node); });
            root.appendChild(screen);
        });
        if (bar) bar.hidden = groups.length <= 1;
        var idx = Math.min(oldIndex, groups.length - 1);
        showScreen(page, idx);
        if (keepFocus && focused && root.contains(focused)) {
            var screen = focused.closest ? focused.closest('.screen') : null;
            if (screen) showScreen(page, parseInt(screen.getAttribute('data-screen-index'), 10) || 0);
            focused.focus();
        }
        window.setTimeout(function () { screenInternal = false; }, 0);
    }

    function schedulePaginate() {
        if (screenInternal) return;
        if (screenRaf) return;
        screenRaf = window.requestAnimationFrame(function () {
            screenRaf = null;
            paginateActivePage();
        });
    }

    /* ---------------- 自定义下拉：Enter 打开，Enter 选中 ---------------- */
    var openSelect = null;

    function closeSelect() {
        if (!openSelect) return;
        openSelect.menu.hidden = true;
        openSelect.trigger.setAttribute('aria-expanded', 'false');
        openSelect = null;
    }

    function selectOptions(select) {
        return Array.prototype.slice.call(select.options).map(function (option, index) {
            return { value: option.value, label: option.textContent, disabled: option.disabled, index: index };
        });
    }

    function enhanceSelect(select) {
        if (!select || select.getAttribute('data-tui-select') === '1') return;
        if (select.closest('.tui-select')) return;
        select.setAttribute('data-tui-select', '1');
        select.classList.add('tui-native-select');
        select.tabIndex = -1;
        select.setAttribute('aria-hidden', 'true');
        var wrap = document.createElement('div');
        wrap.className = 'tui-select';
        select.parentNode.insertBefore(wrap, select);
        wrap.appendChild(select);
        var trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'tui-select-trigger';
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        wrap.appendChild(trigger);
        var menu = document.createElement('div');
        menu.className = 'tui-select-menu';
        menu.setAttribute('role', 'listbox');
        menu.hidden = true;
        wrap.appendChild(menu);
        var active = 0;

        function sync() {
            var options = selectOptions(select);
            var current = options[select.selectedIndex] || options[0];
            var label = (current ? current.label : '—') + '  ▾';
            if (trigger.textContent !== label) trigger.textContent = label;
            trigger.disabled = select.disabled;
        }

        function paint() {
            var options = selectOptions(select);
            menu.innerHTML = options.map(function (option, index) {
                return '<button class="tui-select-option' + (index === active ? ' active' : '') +
                    '" type="button" role="option" aria-selected="' + (index === active ? 'true' : 'false') +
                    '" data-select-index="' + index + '"' + (option.disabled ? ' disabled' : '') + '>' +
                    escapeHtml(option.label) + '</button>';
            }).join('') || '<span class="tui-select-empty">NO OPTIONS</span>';
            var activeButton = menu.querySelector('.tui-select-option.active');
            if (activeButton && document.activeElement === trigger) {
                activeButton.focus();
            }
        }

        function open() {
            closeSelect();
            sync();
            var options = selectOptions(select);
            if (!options.length) return;
            active = Math.max(0, select.selectedIndex);
            menu.hidden = false;
            trigger.setAttribute('aria-expanded', 'true');
            openSelect = { menu: menu, trigger: trigger };
            paint();
            var rect = trigger.getBoundingClientRect();
            menu.style.minWidth = Math.max(120, rect.width) + 'px';
            menu.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - Math.max(120, rect.width) - 8)) + 'px';
            var below = window.innerHeight - rect.bottom;
            if (below < 180 && rect.top > below) {
                menu.style.top = Math.max(4, rect.top - Math.min(360, menu.offsetHeight || 200) - 2) + 'px';
            } else {
                menu.style.top = (rect.bottom + 2) + 'px';
            }
            var activeButton = menu.querySelector('.tui-select-option.active');
            if (activeButton) activeButton.focus();
            schedulePaginate();
        }

        function choose(index) {
            var options = selectOptions(select);
            if (!options[index] || options[index].disabled) return;
            select.selectedIndex = index;
            sync();
            closeSelect();
            select.dispatchEvent(new Event('change', { bubbles: true }));
            trigger.focus();
        }

        trigger.addEventListener('click', function (event) {
            event.preventDefault();
            event.stopPropagation();
            sync();
            if (openSelect && openSelect.trigger === trigger) closeSelect();
            else open();
        });
        trigger.addEventListener('focus', sync);
        trigger.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                event.stopPropagation();
                open();
            }
        });
        menu.addEventListener('keydown', function (event) {
            var options = all('.tui-select-option', menu);
            if (!options.length) return;
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                event.stopPropagation();
                active = (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
                paint();
                var option = menu.querySelector('.tui-select-option.active');
                if (option) option.focus();
                return;
            }
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                event.stopPropagation();
                choose(active);
                return;
            }
            if (event.key === 'Escape' || event.key === 'Tab') {
                event.stopPropagation();
                var wasEscape = event.key === 'Escape';
                closeSelect();
                if (wasEscape) {
                    event.preventDefault();
                    trigger.focus();
                }
            }
        });
        menu.addEventListener('click', function (event) {
            var option = event.target.closest ? event.target.closest('[data-select-index]') : null;
            if (!option) return;
            event.preventDefault();
            event.stopPropagation();
            choose(parseInt(option.getAttribute('data-select-index'), 10));
        });
        select.addEventListener('change', sync);
        if (window.MutationObserver) {
            select._tuiSelectObserver = new MutationObserver(function () { sync(); });
            select._tuiSelectObserver.observe(select, { childList: true, subtree: true });
        }
        sync();
        window.setTimeout(sync, 0);
        window.setTimeout(sync, 180);
    }

    function enhanceSelects(root) {
        all('select:not([data-tui-select])', root || document).forEach(enhanceSelect);
    }

    /* ---------------- 数字输入：Enter 进入编辑，Enter 确认 ---------------- */
    function beginNumberEdit(input) {
        if (!input || input.type !== 'number') return;
        if (input.dataset.tuiEditing === '1') return;
        input.dataset.tuiEditing = '1';
        input.dataset.tuiOriginal = input.value;
        input.readOnly = false;
        input.classList.add('is-editing');
        input.focus();
        try { input.select(); } catch (err) { /* ignore */ }
    }

    function finishNumberEdit(input, cancel) {
        if (!input || input.dataset.tuiEditing !== '1') return;
        if (cancel && input.dataset.tuiOriginal !== undefined) {
            input.value = input.dataset.tuiOriginal;
        }
        delete input.dataset.tuiEditing;
        delete input.dataset.tuiOriginal;
        input.readOnly = true;
        input.classList.remove('is-editing');
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function enhanceNumber(input) {
        if (!input || input.getAttribute('data-tui-number') === '1') return;
        input.setAttribute('data-tui-number', '1');
        input.readOnly = true;
        input.classList.add('is-readonly-number');
        input.title = 'Enter 编辑 / 再按 Enter 确认；点击也可进入编辑；Esc 取消';
        input.addEventListener('click', function () {
            beginNumberEdit(input);
        });
        input.addEventListener('blur', function () {
            if (input.dataset.tuiEditing === '1') finishNumberEdit(input, false);
        });
    }

    function enhanceNumbers(root) {
        all('input[type="number"]:not([data-tui-number])', root || document).forEach(enhanceNumber);
    }

    /* 文本/搜索/日期等输入：Tab/方向键先移动焦点，Enter 或点击进入编辑，Enter 确认。 */
    function isTextInput(el) {
        if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return false;
        if (el.tagName === 'TEXTAREA') return true;
        var type = (el.type || 'text').toLowerCase();
        return ['text', 'search', 'email', 'url', 'tel', 'password', 'date', 'datetime-local', 'month', 'week', 'time'].indexOf(type) !== -1;
    }

    function startTextEdit(input, selectAll) {
        if (!isTextInput(input) || input.dataset.tuiEditing === '1') return;
        input.dataset.tuiEditing = '1';
        input.classList.add('is-editing');
        if (selectAll) {
            try { input.select(); } catch (err) { /* ignore */ }
        }
    }

    function finishTextEdit(input) {
        if (!input || input.dataset.tuiEditing !== '1') return;
        delete input.dataset.tuiEditing;
        input.classList.remove('is-editing');
    }

    document.addEventListener('click', function (event) {
        if (isTextInput(event.target)) startTextEdit(event.target, false);
    });
    document.addEventListener('input', function (event) {
        if (isTextInput(event.target)) startTextEdit(event.target, false);
    });
    document.addEventListener('focusout', function (event) {
        if (isTextInput(event.target)) finishTextEdit(event.target);
    });

    document.addEventListener('click', function (event) {
        if (!openSelect) return;
        if (openSelect.menu.contains(event.target) || openSelect.trigger.contains(event.target)) return;
        closeSelect();
    });
    window.addEventListener('resize', closeSelect);


    function isInsidePage(el) {
        var page = activePageEl();
        return !!(page && el && el !== document.body && page.contains(el));
    }

    /* 当前页面内可聚焦的控件：动作按钮、输入、开关、筛选、分段、菜单项。 */
    function pageControls() {
        var page = activePageEl();
        if (!page) {
            return [];
        }
        return all('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', page).filter(function (el) {
            return !el.disabled && el.getClientRects().length > 0;
        });
    }

    /* ↑/↓/←→ 在页面控件间前后移动；没有控件时保留 fallback。 */
    function focusPageControl(direction) {
        var items = pageControls();
        if (!items.length) {
            content.scrollTop += direction > 0 ? 80 : -80;
            return false;
        }
        var idx = items.indexOf(document.activeElement);
        var next;
        if (direction > 0) {
            next = idx === -1 ? 0 : (idx + 1) % items.length;
        } else {
            next = idx === -1 ? items.length - 1 : (idx - 1 + items.length) % items.length;
        }
        items[next].focus();
        return true;
    }

    /* 方向键按屏幕空间移动焦点：↑↓ 优先同列上下，←→ 优先同行左右。
       这样在双栏/卡片布局里，Down 不会因为 DOM 顺序而跑到右边。 */
    function focusSpatially(key) {
        var items = pageControls();
        if (!items.length) return false;
        var current = document.activeElement;
        if (items.indexOf(current) === -1) {
            items[0].focus();
            return true;
        }
        var cr = current.getBoundingClientRect();
        var ccx = cr.left + cr.width / 2;
        var ccy = cr.top + cr.height / 2;
        var bestOverlap = null;
        var bestOverlapScore = Infinity;
        var bestFallback = null;
        var bestFallbackScore = Infinity;
        for (var i = 0; i < items.length; i += 1) {
            var item = items[i];
            if (item === current) continue;
            var r = item.getBoundingClientRect();
            var dx = (r.left + r.width / 2) - ccx;
            var dy = (r.top + r.height / 2) - ccy;
            var primary;
            var lateral;
            var overlap;
            if (key === 'ArrowRight') {
                if (dx <= 1) continue;
                primary = dx;
                lateral = Math.abs(dy);
                overlap = Math.min(r.bottom, cr.bottom) - Math.max(r.top, cr.top);
            } else if (key === 'ArrowLeft') {
                if (dx >= -1) continue;
                primary = -dx;
                lateral = Math.abs(dy);
                overlap = Math.min(r.bottom, cr.bottom) - Math.max(r.top, cr.top);
            } else if (key === 'ArrowDown') {
                if (dy <= 1) continue;
                primary = dy;
                lateral = Math.abs(dx);
                overlap = Math.min(r.right, cr.right) - Math.max(r.left, cr.left);
            } else {
                if (dy >= -1) continue;
                primary = -dy;
                lateral = Math.abs(dx);
                overlap = Math.min(r.right, cr.right) - Math.max(r.left, cr.left);
            }
            if (overlap > 0) {
                var score = primary + lateral * 0.2;
                if (score < bestOverlapScore) {
                    bestOverlapScore = score;
                    bestOverlap = item;
                }
            } else {
                var fallbackScore = primary + lateral * 2.6;
                if (fallbackScore < bestFallbackScore) {
                    bestFallbackScore = fallbackScore;
                    bestFallback = item;
                }
            }
        }
        var best = bestOverlap || bestFallback;
        if (best) {
            best.focus();
            return true;
        }
        return false;
    }

    function focusContentEdge(last) {
        var items = pageControls();
        if (!items.length) {
            content.scrollTop += last ? -120 : 120;
            return false;
        }
        items[last ? items.length - 1 : 0].focus();
        return true;
    }

    /* ---------------- 状态栏 ---------------- */
    function resetStatus() {
        if (statusTimer) {
            window.clearTimeout(statusTimer);
            statusTimer = null;
        }
        statusMsg.textContent = DEFAULT_HINT;
        statusMsg.className = 'st-msg';
    }

    function flash(text, kind) {
        if (statusTimer) {
            window.clearTimeout(statusTimer);
        }
        statusMsg.textContent = text;
        statusMsg.className = 'st-msg' + (kind ? ' is-' + kind : '');
        statusTimer = window.setTimeout(resetStatus, 4500);
    }

    function focusLabel(el) {
        if (!el || el === document.body) {
            return '';
        }
        if (el.classList && el.classList.contains('tab')) {
            var tabNo = el.querySelector('.tab-no');
            var tabName = el.querySelector('.tab-name');
            return 'PAGE ' + (tabNo ? tabNo.textContent : '') + ' ' + (tabName ? tabName.textContent : '');
        }
        var label = el.getAttribute('data-label') || el.getAttribute('aria-label') || (el.textContent || '');
        label = label.replace(/\s+/g, ' ').trim();
        if (!label) {
            label = el.id || el.tagName.toLowerCase();
        }
        return label.length > 24 ? label.slice(0, 23) + '…' : label;
    }

    function refreshFocus() {
        var el = document.activeElement;
        if (!el || el === document.body || !app.contains(el)) {
            statusFocus.textContent = 'FOCUS —';
            return;
        }
        statusFocus.textContent = 'FOCUS ' + focusLabel(el);
    }

    document.addEventListener('focusin', refreshFocus);
    document.addEventListener('focusout', function () {
        window.setTimeout(refreshFocus, 0);
    });

    /* ---------------- 页面切换 ---------------- */
    function switchPage(key, options) {
        var opts = options || {};
        var idx = pageIndex(key);
        if (idx === -1) {
            return false;
        }
        var oldPage = document.getElementById('page-' + current);
        var newPage = document.getElementById('page-' + key);
        if (!newPage) {
            return false;
        }
        closeSelect();
        var focusWasInside = oldPage && oldPage !== newPage && oldPage.contains(document.activeElement);

        if (oldPage) {
            oldPage.hidden = true;
            oldPage.classList.remove('active');
        }
        newPage.hidden = false;
        newPage.classList.add('active');
        current = key;

        for (var i = 0; i < tabList.length; i += 1) {
            var active = tabList[i].getAttribute('data-page') === key;
            tabList[i].classList.toggle('active', active);
            tabList[i].setAttribute('aria-selected', active ? 'true' : 'false');
            tabList[i].tabIndex = active ? 0 : -1;
        }
        /* 如果焦点停在旧标签上，切页后把焦点带到新标签，避免屏幕显示 A、焦点框却在 B。 */
        var focusedTab = document.activeElement;
        if (focusedTab && focusedTab.classList && focusedTab.classList.contains('tab') &&
                focusedTab.getAttribute('data-page') !== key) {
            var nextTab = tabFor(key);
            if (nextTab) nextTab.focus();
        }

        crumb.textContent = PAGES[idx].code;
        document.title = 'FishTool // ' + PAGES[idx].code + ' · TTY';
        content.scrollTop = 0;
        schedulePaginate();

        if (opts.focusTab || focusWasInside) {
            var tab = tabFor(key);
            if (tab) {
                tab.focus();
            }
        }

        if (window.history && window.history.replaceState && !opts.noHash) {
            try {
                window.history.replaceState(null, '', '#' + key);
            } catch (err) {
                /* file:// 等环境下忽略哈希写入失败 */
            }
        }
        refreshFocus();
        if (window.FishLive && typeof window.FishLive.enter === 'function') {
            window.FishLive.enter(key);
        }
        return true;
    }

    tabsEl.addEventListener('click', function (event) {
        var tab = closest(event.target, '.tab');
        if (!tab) {
            return;
        }
        switchPage(tab.getAttribute('data-page'), { focusTab: true });
    });

    tabsEl.addEventListener('keydown', function (event) {
        var tab = closest(event.target, '.tab');
        if (!tab) {
            return;
        }
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        var idx = pageIndex(tab.getAttribute('data-page'));
        var next = -1;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            focusContentEdge(event.key === 'ArrowUp');
            return;
        }
        if (event.key === 'ArrowRight') {
            next = (idx + 1) % PAGES.length;
        } else if (event.key === 'ArrowLeft') {
            next = (idx - 1 + PAGES.length) % PAGES.length;
        } else if (event.key === 'Home') {
            next = 0;
        } else if (event.key === 'End') {
            next = PAGES.length - 1;
        }
        if (next >= 0) {
            event.preventDefault();
            switchPage(PAGES[next].key, { focusTab: true });
        }
    });

    function cyclePage(step) {
        var idx = pageIndex(current);
        switchPage(PAGES[(idx + step + PAGES.length) % PAGES.length].key, { focusTab: true });
    }

    /* ---------------- 顶部时钟 ---------------- */
    function tickClock() {
        var now = new Date();
        clock.textContent =
            pad2(now.getMonth() + 1) + '-' + pad2(now.getDate()) + ' ' +
            pad2(now.getHours()) + ':' + pad2(now.getMinutes()) + ':' + pad2(now.getSeconds());
    }
    tickClock();
    window.setInterval(tickClock, 1000);

    /* ---------------- CRT ---------------- */
    function setCrt(on) {
        document.body.classList.toggle('is-crt', on);
        statusCrt.textContent = on ? 'CRT ON' : 'CRT OFF';
        statusCrt.classList.toggle('is-on', on);
    }
    function toggleCrt() {
        setCrt(!document.body.classList.contains('is-crt'));
    }
    setCrt(false);

    /* ---------------- 帮助浮层 ---------------- */
    function isVisible(el) {
        return !!(el && el.getClientRects && el.getClientRects().length);
    }

    function openHelp() {
        if (helpOpen) {
            return;
        }
        helpOpen = true;
        lastFocus = document.activeElement;
        helpMask.hidden = false;
        helpClose.focus();
    }

    function closeHelp() {
        if (!helpOpen) {
            return;
        }
        helpOpen = false;
        helpMask.hidden = true;
        if (lastFocus && lastFocus !== document.body && document.contains(lastFocus) && isVisible(lastFocus)) {
            lastFocus.focus();
        } else {
            activeTab().focus();
        }
    }

    function helpFocusables() {
        return all('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', helpDialog);
    }

    function trapHelpFocus(event) {
        var items = helpFocusables();
        event.preventDefault();
        if (!items.length) {
            helpClose.focus();
            return;
        }
        var idx = items.indexOf(document.activeElement);
        var next;
        if (event.shiftKey) {
            next = idx <= 0 ? items.length - 1 : idx - 1;
        } else {
            next = idx === -1 || idx === items.length - 1 ? 0 : idx + 1;
        }
        items[next].focus();
    }

    helpClose.addEventListener('click', closeHelp);
    helpMask.addEventListener('click', function (event) {
        if (event.target === helpMask) {
            closeHelp();
        }
    });

    /* ---------------- 动作 ---------------- */
    var ACTIONS = {
        help: openHelp,
        scan: function () { flash('OK // scan queued · 0 requests (demo)', 'ok'); },
        refresh: function () { flash('OK // view refreshed · ' + crumb.textContent, 'ok'); },
        save: function () { flash('OK // config saved in memory (demo)', 'ok'); },
        test: function () { flash('OK // llm test 200 · 412ms (demo)', 'ok'); },
        export: function () { flash('OK // export queued (demo)', 'ok'); },
        run: function () { flash('OK // task queued (demo)', 'ok'); },
        report: function () { flash('OK // report prepared (demo)', 'ok'); },
        quit: function () {
            if (window.FishLive && typeof window.FishLive.quit === 'function') {
                window.FishLive.quit();
            } else {
                flash('INFO // close the tab to exit', 'warn');
            }
        },
        crt: toggleCrt
    };

    function runAction(name) {
        if (window.FishLive && typeof window.FishLive.action === 'function' && window.FishLive.action(name)) {
            return;
        }
        if (ACTIONS[name]) {
            ACTIONS[name]();
        } else {
            flash('INFO // demo action: ' + name, 'warn');
        }
    }

    document.addEventListener('click', function (event) {
        var gotoEl = closest(event.target, '[data-goto]');
        if (gotoEl) {
            if (gotoEl.tagName === 'A') {
                event.preventDefault();
            }
            switchPage(gotoEl.getAttribute('data-goto'), { focusTab: true });
            return;
        }
        var actEl = closest(event.target, '[data-act]');
        if (actEl) {
            runAction(actEl.getAttribute('data-act'));
        }
    });

    /* ---------------- 快捷菜单：↑↓ / Home / End ---------------- */
    var menuList = document.querySelector('.menu-list');
    if (menuList) {
        menuList.addEventListener('keydown', function (event) {
            var item = closest(event.target, '.menu-item');
            if (!item) {
                return;
            }
            if (event.ctrlKey || event.metaKey || event.altKey) {
                return;
            }
            var items = all('.menu-item', menuList);
            var idx = items.indexOf(item);
            var next = -1;
            if (event.key === 'ArrowDown') {
                next = (idx + 1) % items.length;
            } else if (event.key === 'ArrowUp') {
                next = (idx - 1 + items.length) % items.length;
            } else if (event.key === 'Home') {
                next = 0;
            } else if (event.key === 'End') {
                next = items.length - 1;
            }
            if (next >= 0) {
                event.preventDefault();
                items[next].focus();
            }
        });
    }

    /* ---------------- 开关 ---------------- */
    var toggles = all('.toggle');
    toggles.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var on = btn.getAttribute('aria-pressed') !== 'true';
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            var box = btn.querySelector('.tbox');
            var tv = btn.querySelector('.tv');
            if (box) {
                box.textContent = on ? '[x]' : '[ ]';
            }
            if (tv) {
                tv.textContent = on ? 'ON' : 'OFF';
            }
            flash('OK // ' + (btn.getAttribute('data-label') || 'option') + ' ' + (on ? 'ON' : 'OFF'), 'ok');
        });
    });

    /* ---------------- 抽奖模式分段 ---------------- */
    var MODES = {
        real: { title: '真人筛选 · 结果', note: 'MODE 真人筛选 · 常规采集' },
        draw: { title: '评论区抽奖 · 结果', note: 'MODE 评论区抽奖 · 需登录发布者' },
        patrol: { title: 'AI 伪人巡检 · 结果', note: 'MODE AI 伪人巡检 · 规则 + AI 甄别' }
    };
    var segBtns = all('.seg-btn');

    function setMode(mode) {
        var info = MODES[mode];
        if (!info) {
            return false;
        }
        segBtns.forEach(function (btn) {
            var on = btn.getAttribute('data-mode') === mode;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        if (drawTitle) {
            drawTitle.textContent = info.title;
        }
        if (drawModeNote) {
            drawModeNote.textContent = info.note;
        }
        flash('OK // ' + info.note, 'ok');
        return true;
    }

    segBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
            setMode(btn.getAttribute('data-mode'));
        });
    });
    var seg = document.querySelector('.seg');
    if (seg) {
        seg.addEventListener('keydown', function (event) {
            var idx = segBtns.indexOf(closest(event.target, '.seg-btn'));
            if (idx === -1) {
                return;
            }
            if (event.ctrlKey || event.metaKey || event.altKey) {
                return;
            }
            var next = -1;
            if (event.key === 'ArrowRight') {
                next = (idx + 1) % segBtns.length;
            } else if (event.key === 'ArrowLeft') {
                next = (idx - 1 + segBtns.length) % segBtns.length;
            } else if (event.key === 'Home') {
                next = 0;
            } else if (event.key === 'End') {
                next = segBtns.length - 1;
            }
            if (next >= 0) {
                event.preventDefault();
                segBtns[next].focus();
                setMode(segBtns[next].getAttribute('data-mode'));
            }
        });
    }

    /* ---------------- 表单（演示只回显状态） ---------------- */
    document.addEventListener('submit', function (event) {
        var form = closest(event.target, '[data-form]');
        if (!form) {
            return;
        }
        event.preventDefault();
        var kind = form.getAttribute('data-form');
        if (kind === 'topic') {
            flash('OK // topic request queued · demo only', 'ok');
        } else if (kind === 'bench') {
            flash('OK // benchmark queued · demo only', 'ok');
        } else if (kind === 'draw') {
            flash('OK // scan queued · demo only', 'ok');
        } else {
            flash('OK // submitted · demo only', 'ok');
        }
    });

    /* ---------------- 日志过滤 ---------------- */
    function logPageSize() {
        var height = logList ? logList.clientHeight : 0;
        return height > 0 ? Math.max(8, Math.floor((height - 28) / 22)) : LOG_PAGE_SIZE;
    }

    function applyLogFilter(resetPage) {
        if (!logList) {
            return;
        }
        if (resetPage) logPage = 1;
        var pageSize = logPageSize();
        var query = ((logSearch && logSearch.value) || '').trim().toLowerCase();
        var rows = all('.log-line', logList);
        var matched = [];
        for (var i = 0; i < rows.length; i += 1) {
            var level = rows[i].getAttribute('data-level') || 'info';
            var text = rows[i].textContent.toLowerCase();
            if ((logLevel === 'all' || level === logLevel) && (!query || text.indexOf(query) !== -1)) {
                matched.push(rows[i]);
            }
            rows[i].style.display = 'none';
        }
        var pages = Math.max(1, Math.ceil(matched.length / pageSize));
        logFiltered = matched;
        if (logPage > pages) logPage = pages;
        if (logPage < 1) logPage = 1;
        var start = (logPage - 1) * pageSize;
        var shown = 0;
        matched.slice(start, start + pageSize).forEach(function (row) {
            row.style.display = '';
            shown += 1;
        });
        if (logCount) {
            logCount.textContent = shown;
        }
        if (logLevelEl) {
            logLevelEl.textContent = logLevel.toUpperCase();
        }
        if (logEmpty) {
            logEmpty.hidden = matched.length !== 0;
        }
        if (logPager) {
            logPager.innerHTML = matched.length
                ? '<span class="log-page-label">PAGE ' + logPage + ' / ' + pages +
                  ' · ' + matched.length + ' MATCH</span>' +
                  '<button class="btn" type="button" data-log-page="-1"' + (logPage <= 1 ? ' disabled' : '') + '>◀ PREV</button>' +
                  '<button class="btn" type="button" data-log-page="1"' + (logPage >= pages ? ' disabled' : '') + '>NEXT ▶</button>'
                : '<span class="log-page-label">NO MATCH</span>';
        }
    }

    function stepLogPage(delta) {
        var pages = Math.max(1, Math.ceil(logFiltered.length / logPageSize()));
        var next = Math.max(1, Math.min(pages, logPage + delta));
        if (next === logPage) return false;
        logPage = next;
        applyLogFilter(false);
        return true;
    }

    if (logFilters) {
        logFilters.addEventListener('click', function (event) {
            var btn = closest(event.target, '.fbtn');
            if (!btn) {
                return;
            }
            logLevel = btn.getAttribute('data-level') || 'all';
            all('.fbtn', logFilters).forEach(function (chip) {
                var on = chip === btn;
                chip.classList.toggle('active', on);
                chip.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
            applyLogFilter(true);
        });
        logFilters.addEventListener('keydown', function (event) {
            var btn = closest(event.target, '.fbtn');
            if (!btn) {
                return;
            }
            if (event.ctrlKey || event.metaKey || event.altKey) {
                return;
            }
            var items = all('.fbtn', logFilters);
            var idx = items.indexOf(btn);
            var next = -1;
            if (event.key === 'ArrowRight') {
                next = (idx + 1) % items.length;
            } else if (event.key === 'ArrowLeft') {
                next = (idx - 1 + items.length) % items.length;
            }
            if (next >= 0) {
                event.preventDefault();
                items[next].focus();
            }
        });
    }
    if (logSearch) {
        logSearch.addEventListener('input', function () { applyLogFilter(true); });
    }
    if (logPager) {
        logPager.addEventListener('click', function (event) {
            var btn = closest(event.target, '[data-log-page]');
            if (!btn || btn.disabled) return;
            event.preventDefault();
            stepLogPage(parseInt(btn.getAttribute('data-log-page'), 10) || 0);
        });
    }
    applyLogFilter();

    /* ---------------- 命令行 ---------------- */
    function focusCmd() {
        cmd.focus();
        var len = cmd.value.length;
        try {
            cmd.setSelectionRange(len, len);
        } catch (err) {
            /* 某些输入类型不支持 setSelectionRange */
        }
    }

    function runCommand(raw) {
        var text = (raw || '').trim();
        if (!text) {
            return;
        }
        cmdHistory.push(text);
        if (cmdHistory.length > 50) {
            cmdHistory.shift();
        }
        historyIndex = -1;

        var parts = text.split(/\s+/);
        var name = parts[0].toLowerCase();
        var arg = parts[1] ? parts[1].toLowerCase() : '';

        if (name === 'help' || name === '?') {
            openHelp();
            return;
        }
        if (name === 'goto' || name === 'page' || name === 'go') {
            var target = arg;
            if (/^(0[0-9]|[0-9])$/.test(target)) {
                target = PAGES[parseInt(target, 10)].key;
            }
            if (switchPage(target, { focusTab: false })) {
                flash('OK // moved to ' + crumb.textContent, 'ok');
            } else {
                flash('ERR // unknown page: ' + (arg || '(empty)') + ' · use 00-08', 'err');
            }
            return;
        }
        if (name === 'scan' || name === 'collect') {
            if (window.FishLive && window.FishLive.action('scan')) {
                return;
            }
            flash('OK // scan queued', 'ok');
            return;
        }
        if (name === 'refresh') {
            if (window.FishLive && window.FishLive.action('refresh')) {
                return;
            }
            flash('OK // view refreshed · ' + crumb.textContent, 'ok');
            return;
        }
        if (name === 'login') {
            if (window.FishLive && typeof window.FishLive.action === 'function' && window.FishLive.action('login')) {
                return;
            }
            flash('INFO // login is available in the live build', 'warn');
            return;
        }
        if (name === 'logout') {
            if (window.FishLive && typeof window.FishLive.logout === 'function') {
                window.FishLive.logout();
            }
            return;
        }
        if (name === 'crt') {
            if (arg === 'on') {
                setCrt(true);
            } else if (arg === 'off') {
                setCrt(false);
            } else {
                toggleCrt();
            }
            flash('OK // crt ' + (document.body.classList.contains('is-crt') ? 'on' : 'off'), 'ok');
            return;
        }
        if (name === 'logs' || name === 'log') {
            switchPage('logs', { focusTab: false });
            flash('OK // moved to LOGS', 'ok');
            return;
        }
        if (name === 'config') {
            switchPage('config', { focusTab: false });
            flash('OK // moved to CONFIG', 'ok');
            return;
        }
        if (name === 'whoami') {
            var who = window.FishLive && typeof window.FishLive.whoami === 'function'
                ? window.FishLive.whoami()
                : 'operator · TTY';
            flash('OK // ' + who, 'ok');
            return;
        }
        if (name === 'clear') {
            resetStatus();
            return;
        }
        if (name === 'quit' || name === 'exit') {
            if (window.FishLive && typeof window.FishLive.quit === 'function') {
                window.FishLive.quit();
            } else {
                flash('INFO // close the tab to exit', 'warn');
            }
            return;
        }
        flash('ERR // unknown command: ' + name + ' · try help', 'err');
    }

    cmd.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            var value = cmd.value;
            cmd.value = '';
            runCommand(value);
            return;
        }
        if (event.key === 'ArrowUp') {
            event.preventDefault();
            if (!cmdHistory.length) {
                return;
            }
            historyIndex = Math.min(historyIndex + 1, cmdHistory.length - 1);
            cmd.value = cmdHistory[cmdHistory.length - 1 - historyIndex];
            return;
        }
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            if (historyIndex > 0) {
                historyIndex -= 1;
                cmd.value = cmdHistory[cmdHistory.length - 1 - historyIndex];
            } else {
                historyIndex = -1;
                cmd.value = '';
            }
        }
    });

    /* ---------------- 字符光标 + 时间衰减拖影 + PTR 坐标 ---------------- */
    (function createPointerTrail() {
        if (!cellPtr) return;
        for (var i = 0; i < 12; i += 1) {
            var trail = document.createElement('div');
            trail.className = 'cell-ptr cell-trail';
            trail.setAttribute('aria-hidden', 'true');
            trail.innerHTML = '<span>+</span>';
            document.body.appendChild(trail);
            ptrTrail.push(trail);
        }
    })();

    function trailTick(now) {
        var active = false;
        for (var i = 0; i < ptrTrailParticles.length; i += 1) {
            var p = ptrTrailParticles[i];
            if (!p.active) continue;
            var age = now - p.born;
            if (age >= p.life) {
                p.active = false;
                p.el.classList.remove('is-on');
                p.el.style.opacity = '0';
                continue;
            }
            p.el.style.opacity = (p.start * (1 - age / p.life)).toFixed(3);
            active = true;
        }
        ptrTrailRaf = active ? window.requestAnimationFrame(trailTick) : null;
    }

    function spawnTrail(x, y, start, life) {
        if (!ptrTrail.length) return;
        var el = ptrTrail[ptrTrailCursor % ptrTrail.length];
        ptrTrailCursor += 1;
        for (var i = 0; i < ptrTrailParticles.length; i += 1) {
            if (ptrTrailParticles[i].el === el) ptrTrailParticles[i].active = false;
        }
        if (ptrTrailParticles.length > 48) {
            ptrTrailParticles = ptrTrailParticles.filter(function (particle) { return particle.active; });
        }
        el.style.transform = 'translate3d(' + x + 'px,' + y + 'px,0)';
        el.style.opacity = start;
        el.classList.add('is-on');
        ptrTrailParticles.push({
            el: el,
            born: (window.performance && window.performance.now) ? window.performance.now() : Date.now(),
            life: life,
            start: start,
            active: true
        });
        if (!ptrTrailRaf) {
            ptrTrailRaf = window.requestAnimationFrame(trailTick);
        }
    }

    function clearTrail() {
        for (var i = 0; i < ptrTrail.length; i += 1) {
            ptrTrail[i].classList.remove('is-on');
            ptrTrail[i].style.opacity = '0';
        }
        ptrTrailParticles = [];
        ptrLastCell = null;
        if (ptrTrailRaf) {
            window.cancelAnimationFrame(ptrTrailRaf);
            ptrTrailRaf = null;
        }
    }
    function measureCells() {
        if (!charRuler) {
            return;
        }
        var rect = charRuler.getBoundingClientRect();
        if (rect.width > 8) {
            cellW = rect.width / 10;
        }
        cellH = 20;
        document.documentElement.style.setProperty('--cw', cellW.toFixed(2) + 'px');
        document.documentElement.style.setProperty('--lh', cellH + 'px');
    }

    function positionPtr(x, y) {
        var cols = Math.max(1, Math.ceil(window.innerWidth / cellW));
        var rows = Math.max(1, Math.ceil(window.innerHeight / cellH));
        var col = Math.floor(x / cellW);
        var row = Math.floor(y / cellH);
        if (col < 0) {
            col = 0;
        }
        if (col > cols - 1) {
            col = cols - 1;
        }
        if (row < 0) {
            row = 0;
        }
        if (row > rows - 1) {
            row = rows - 1;
        }
        cellPtr.style.transform = 'translate3d(' + (col * cellW) + 'px,' + (row * cellH) + 'px,0)';
        cellPtr.classList.add('is-on');
        var cell = { x: col * cellW, y: row * cellH };
        if (!ptrLastCell || ptrLastCell.x !== cell.x || ptrLastCell.y !== cell.y) {
            var previous = ptrLastCell;
            if (previous) {
                var dx = (cell.x - previous.x) / cellW;
                var dy = (cell.y - previous.y) / cellH;
                var steps = Math.max(Math.abs(dx), Math.abs(dy));
                var maxSteps = Math.min(8, Math.ceil(steps));
                for (var step = 0; step < maxSteps; step += 1) {
                    var ratio = maxSteps === 1 ? 1 : (step + 1) / (maxSteps + 1);
                    var tx = previous.x + (cell.x - previous.x) * ratio;
                    var ty = previous.y + (cell.y - previous.y) * ratio;
                    spawnTrail(tx, ty, 0.22 - step * 0.018, 260 + step * 16);
                }
                spawnTrail(previous.x, previous.y, 0.24, 320);
            }
            ptrLastCell = cell;
        }
        statusPtr.textContent = 'PTR ' + pad3(col + 1) + ',' + pad3(row + 1);
    }

    function hidePtr() {
        if (!cellPtr) {
            return;
        }
        cellPtr.classList.remove('is-on');
        cellPtr.classList.remove('is-down');
        clearTrail();
        statusPtr.textContent = 'PTR ---,---';
    }

    function onMouseMove(event) {
        if (!pointerEnabled) {
            return;
        }
        if (!pointerActivated) {
            document.body.classList.add('has-cell-ptr');
            pointerActivated = true;
        }
        pendingX = event.clientX;
        pendingY = event.clientY;
        if (!pointerRaf) {
            pointerRaf = window.requestAnimationFrame(function () {
                pointerRaf = null;
                positionPtr(pendingX, pendingY);
            });
        }
    }

    if (pointerEnabled && cellPtr) {
        window.addEventListener('mousemove', onMouseMove, { passive: true });
        window.addEventListener('mousedown', function () {
            if (pointerActivated) {
                cellPtr.classList.add('is-down');
            }
        });
        window.addEventListener('mouseup', function () {
            cellPtr.classList.remove('is-down');
        });
        document.documentElement.addEventListener('mouseleave', hidePtr);
        window.addEventListener('blur', hidePtr);
        window.addEventListener('resize', function () {
            measureCells();
            hidePtr();
            schedulePaginate();
            if (current === 'logs') applyLogFilter(false);
        });
    }

    measureCells();
    if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
        document.fonts.ready.then(measureCells);
    }

    var contentObserver = new MutationObserver(function () {
        if (screenInternal) return;
        enhanceSelects(content);
        enhanceNumbers(content);
        schedulePaginate();
    });
    contentObserver.observe(content, { childList: true, subtree: true });
    enhanceSelects(content);
    enhanceNumbers(content);
    document.addEventListener('click', function (event) {
        var step = event.target.closest ? event.target.closest('[data-screen-step]') : null;
        if (!step) return;
        event.preventDefault();
        var page = activePageEl();
        if (page) screenStep(page, parseInt(step.getAttribute('data-screen-step'), 10) || 0);
    });
    schedulePaginate();

    /* ---------------- 全局键盘 ---------------- */
    function isTyping(el) {
        var tag = el && el.tagName ? el.tagName.toLowerCase() : '';
        return tag === 'input' || tag === 'textarea' || tag === 'select';
    }

    function blurToTab() {
        var el = document.activeElement;
        if (el && el !== document.body && el.blur) {
            el.blur();
        }
        activeTab().focus();
    }

    document.addEventListener('keydown', function (event) {
        if (helpOpen) {
            if (event.key === 'Escape' || event.key === 'F1') {
                event.preventDefault();
                closeHelp();
                return;
            }
            if (event.key === 'Tab') {
                trapHelpFocus(event);
                return;
            }
            return;
        }

        if (event.key === 'F1') {
            event.preventDefault();
            openHelp();
            return;
        }
        if (event.key === 'F2') {
            event.preventDefault();
            runAction('scan');
            return;
        }
        if (event.key === 'F3') {
            event.preventDefault();
            switchPage('logs', { focusTab: true });
            return;
        }
        if (event.key === 'F4') {
            event.preventDefault();
            switchPage('config', { focusTab: true });
            return;
        }
        if (event.key === 'F5') {
            event.preventDefault();
            runAction('refresh');
            return;
        }
        if (event.key === 'F6') {
            event.preventDefault();
            toggleCrt();
            return;
        }
        if (event.key === 'F10') {
            event.preventDefault();
            runAction('quit');
            return;
        }

        if (isTyping(event.target)) {
            if (event.defaultPrevented) {
                return;
            }
            var typed = event.target;
            var isNumber = typed.tagName === 'INPUT' && typed.type === 'number';
            if (isNumber) {
                if (typed.dataset.tuiEditing === '1') {
                    if (event.key === 'Enter') {
                        event.preventDefault();
                        finishNumberEdit(typed, false);
                        return;
                    }
                    if (event.key === 'Escape') {
                        event.preventDefault();
                        finishNumberEdit(typed, true);
                        return;
                    }
                    return;
                }
                if (event.key === 'Enter') {
                    event.preventDefault();
                    beginNumberEdit(typed);
                    return;
                }
                if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' ||
                        event.key === 'ArrowUp' || event.key === 'ArrowDown') {
                    event.preventDefault();
                    focusSpatially(event.key);
                    return;
                }
                return;
            }
            if (typed.tagName === 'INPUT' && (typed.type === 'checkbox' || typed.type === 'radio')) {
                if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' ||
                        event.key === 'ArrowUp' || event.key === 'ArrowDown') {
                    event.preventDefault();
                    focusSpatially(event.key);
                    return;
                }
                if (event.key === 'Enter') {
                    event.preventDefault();
                    typed.click();
                    return;
                }
                return;
            }
            if (isTextInput(typed)) {
                var editing = typed.dataset.tuiEditing === '1';
                if (event.isComposing) return;
                if (!editing) {
                    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' ||
                            event.key === 'ArrowUp' || event.key === 'ArrowDown') {
                        event.preventDefault();
                        focusSpatially(event.key);
                        return;
                    }
                    if (event.key === 'Enter') {
                        event.preventDefault();
                        startTextEdit(typed, true);
                        return;
                    }
                    if (event.key === 'Escape') {
                        event.preventDefault();
                        blurToTab();
                        return;
                    }
                    if (event.key.length === 1 || event.key === 'Backspace' || event.key === 'Delete') {
                        startTextEdit(typed, false);
                        return;
                    }
                    return;
                }
                if (event.key === 'Enter') {
                    event.preventDefault();
                    finishTextEdit(typed);
                    return;
                }
                if (event.key === 'Escape') {
                    event.preventDefault();
                    finishTextEdit(typed);
                    return;
                }
                return;
            }
            return;
        }

        if (event.defaultPrevented) {
            return;
        }
        /* Ctrl/Cmd + ←/→ 是全局切页；在输入框内则保留原生按词移动。 */
        if ((event.ctrlKey || event.metaKey) && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
            event.preventDefault();
            cyclePage(event.key === 'ArrowLeft' ? -1 : 1);
            return;
        }
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }

        if (event.key === 'Escape') {
            event.preventDefault();
            blurToTab();
            return;
        }
        if (event.key === '?') {
            event.preventDefault();
            openHelp();
            return;
        }
        if (event.key === ':' || event.key === '/') {
            event.preventDefault();
            focusCmd();
            return;
        }
        if (event.key === 'c' || event.key === 'C') {
            toggleCrt();
            return;
        }
        if (/^[0-9]$/.test(event.key)) {
            event.preventDefault();
            switchPage(PAGES[parseInt(event.key, 10)].key, { focusTab: true });
            return;
        }

        /* 方向键：页面内移动控件；在页面外时进入内容。切页用 Ctrl+←/→、0-9 或顶部标签页。 */
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' || event.key === 'ArrowUp' || event.key === 'ArrowDown') {
            event.preventDefault();
            if (isInsidePage(document.activeElement)) {
                focusSpatially(event.key);
            } else {
                focusContentEdge(event.key === 'ArrowUp' || event.key === 'ArrowLeft');
            }
            return;
        }

        if (event.key === 'j') {
            event.preventDefault();
            if (current === 'logs' && stepLogPage(1)) return;
            if (!screenStep(activePageEl(), 1)) content.scrollTop += 36;
            return;
        }
        if (event.key === 'k') {
            event.preventDefault();
            if (current === 'logs' && stepLogPage(-1)) return;
            if (!screenStep(activePageEl(), -1)) content.scrollTop -= 36;
            return;
        }
        if (event.key === 'PageDown') {
            event.preventDefault();
            if (current === 'logs' && stepLogPage(1)) return;
            if (!screenStep(activePageEl(), 1)) content.scrollTop += Math.max(180, content.clientHeight - 40);
            return;
        }
        if (event.key === 'PageUp') {
            event.preventDefault();
            if (current === 'logs' && stepLogPage(-1)) return;
            if (!screenStep(activePageEl(), -1)) content.scrollTop -= Math.max(180, content.clientHeight - 40);
            return;
        }
        if (event.key === 'Home') {
            event.preventDefault();
            if (current === 'logs') { logPage = 1; applyLogFilter(false); return; }
            var homePage = activePageEl();
            if (homePage) showScreen(homePage, 0);
            return;
        }
        if (event.key === 'End') {
            event.preventDefault();
            if (current === 'logs') {
                logPage = Math.max(1, Math.ceil(logFiltered.length / logPageSize()));
                applyLogFilter(false);
                return;
            }
            var endPage = activePageEl();
            var endRoot = screenRoot(endPage);
            if (endRoot) showScreen(endPage, all('.screen', endRoot).length - 1);
            return;
        }
        if (event.key === 'g') {
            event.preventDefault();
            if (current === 'logs') { logPage = 1; applyLogFilter(false); return; }
            if (activePageEl()) showScreen(activePageEl(), 0);
            return;
        }
        if (event.key === 'G') {
            event.preventDefault();
            if (current === 'logs') {
                logPage = Math.max(1, Math.ceil(logFiltered.length / logPageSize()));
                applyLogFilter(false);
                return;
            }
            var gPage = activePageEl();
            var gRoot = screenRoot(gPage);
            if (gRoot) showScreen(gPage, all('.screen', gRoot).length - 1);
        }
    });

    /* ---------------- 初始状态与哈希路由 ---------------- */
    window.addEventListener('hashchange', function () {
        var key = window.location.hash.replace('#', '');
        if (pageIndex(key) !== -1) {
            switchPage(key, { focusTab: true });
        }
    });

    var initial = window.location.hash.replace('#', '');
    if (pageIndex(initial) === -1) {
        initial = 'welcome';
    }
    window.FishTui = {
        flash: flash,
        applyLogFilter: applyLogFilter,
        currentPage: function () { return current; },
        switchPage: switchPage
    };
    if (window.FishLive && typeof window.FishLive.init === 'function') {
        window.FishLive.init();
    }
    switchPage(initial, { focusTab: false });
    resetStatus();
    refreshFocus();
})();
