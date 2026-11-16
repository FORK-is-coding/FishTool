/* =====================================================================
   FishTool // TTY —— tui.live.js
   ---------------------------------------------------------------------
   核心优先版本的真实数据层：首页 / 热点 / 评论 / 配置 / 拆解 / 自诊 / 抽奖 / 日志。
   拆解、自诊、抽奖均接真实接口；所有请求同源走 /api，WS 走 /ws。
   保持 Chromium 83 可用语法。
   ===================================================================== */
(function () {
    'use strict';

    var API = '/api';
    var state = {
        welcome: null,
        welcomeAt: 0,
        zones: [],
        zoneOptions: [],
        zone: '游戏',
        hotspot: null,
        event: null,
        eventMembers: null,
        eventBudget: null,
        eventAssessments: null,
        eventTask: '',
        opportunity: null,
        watch: null,
        watchAt: 0,
        watchProgressTimer: null,
        watchProgressBvid: '',
        semantic: { bvid: '', preflight: null, preference: null, task: null, result: null, page: 1, polling: false, token: '' },
        config: null,
        logs: [],
        logsAt: 0,
        analysisCategories: [],
        bench: null,
        benchTask: '',
        benchTimer: null,
        diag: null,
        ranking: { token: '', runId: '', uid: null, peers: [], candidates: [], discoveryToken: null, result: null, polling: false },
        localTokens: {},
        lottery: null,
        lotteryTimer: null,
        patrol: { preflight: null, prefs: null, scanId: null, scan: null, comments: null, reports: null, task: null, polling: false },
        loading: {},
        ws: false,
        loginOpen: false,
        loginTimer: null,
        loginKey: '',
        logTimer: null
    };

    function esc(value) {
        return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    function num(value) {
        var n = Number(value);
        return Number.isFinite(n) ? n : 0;
    }

    function fmt(value) {
        return num(value).toLocaleString('en-US');
    }

    function compact(value) {
        var n = num(value);
        if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
        if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
        return String(n);
    }

    function pct(value, total) {
        var t = num(total);
        if (!t) return '0%';
        return Math.round(num(value) / t * 100) + '%';
    }

    function short(value, max) {
        var text = String(value === null || value === undefined ? '' : value)
            .replace(/\s+/g, ' ').trim();
        if (text.length > max) return text.slice(0, max - 1) + '…';
        return text;
    }

    function hm(value) {
        var text = String(value || '');
        var match = text.match(/(\d{2}:\d{2}:\d{2})/);
        if (match) return match[1];
        var stamp = Number(text);
        if (Number.isFinite(stamp) && stamp > 1000000000) {
            var date = new Date(stamp * 1000);
            return date.toTimeString().slice(0, 8);
        }
        return text || '—';
    }

    function dateOf(value) {
        var text = String(value || '');
        if (!text) return '—';
        return text.slice(5, 16).replace('T', ' ');
    }

    function levelKey(level) {
        var value = String(level || '').toUpperCase();
        if (value === 'WARNING' || value === 'WARN') return 'warn';
        if (value === 'ERROR' || value === 'CRITICAL' || value === 'ERR') return 'error';
        if (value === 'DEBUG') return 'debug';
        return 'info';
    }

    function levelLabel(level) {
        var key = levelKey(level);
        if (key === 'warn') return 'WARN';
        if (key === 'error') return 'ERR';
        if (key === 'debug') return 'DEBUG';
        return 'INFO';
    }

    function sleep(ms) {
        return new Promise(function (resolve) { window.setTimeout(resolve, ms); });
    }

    function api(path, options) {
        var opts = options || {};
        var headers = { 'Content-Type': 'application/json' };
        if (opts.headers) {
            Object.keys(opts.headers).forEach(function (key) { headers[key] = opts.headers[key]; });
        }
        var init = {
            method: opts.method || 'GET',
            headers: headers,
            credentials: 'same-origin'
        };
        if (opts.body !== undefined && opts.body !== null && opts.method && opts.method !== 'GET') {
            init.body = JSON.stringify(opts.body);
        }
        return fetch(API + path, init).then(function (response) {
            return response.text().then(function (text) {
                var payload = null;
                try {
                    payload = text ? JSON.parse(text) : {};
                } catch (error) {
                    payload = { detail: text || ('HTTP ' + response.status) };
                }
                if (!response.ok) {
                    var detail = payload && payload.detail !== undefined ? payload.detail : payload;
                    var message;
                    if (typeof detail === 'string' && detail) {
                        message = detail;
                    } else if (Array.isArray(detail)) {
                        message = detail.map(function (item) {
                            return item && item.msg ? item.msg : String(item);
                        }).join('；');
                    } else if (detail && typeof detail === 'object') {
                        message = detail.message || detail.error_code || ('HTTP ' + response.status);
                    } else {
                        message = 'HTTP ' + response.status;
                    }
                    var error = new Error(message || ('HTTP ' + response.status));
                    error.status = response.status;
                    error.detail = detail;
                    throw error;
                }
                return payload;
            });
        });
    }

    function settle(promise) {
        return promise.then(
            function (value) { return { ok: true, value: value }; },
            function (error) { return { ok: false, error: error }; }
        );
    }

    function getLocalToken(path, force) {
        if (!force && state.localTokens[path]) return Promise.resolve(state.localTokens[path]);
        return api(path).then(function (payload) {
            state.localTokens[path] = (payload && payload.data && payload.data.token) || '';
            return state.localTokens[path];
        }).catch(function () {
            state.localTokens[path] = '';
            return '';
        });
    }

    function writeHeaders(path) {
        return getLocalToken(path, false).then(function (token) {
            return token ? { 'X-Local-Token': token } : {};
        });
    }

    function notify(message, level) {
        if (window.FishTui && window.FishTui.flash) {
            window.FishTui.flash(String(message || ''), level || 'ok');
            return;
        }
        var el = document.getElementById('status-msg');
        if (el) el.textContent = String(message || '');
    }

    function liveRoot(key) {
        return document.getElementById(key + '-live');
    }

    function setHTML(key, html) {
        var root = liveRoot(key) || document.getElementById(key);
        if (root) root.innerHTML = html;
    }

    function loading(key, message) {
        setHTML(key, '<p class="live-loading">' + esc(message || 'LOADING...') + '</p>');
    }

    function errorBox(message) {
        return '<div class="live-error">ERROR // ' + esc(short(message, 220)) + '</div>';
    }

    function emptyBox(message) {
        return '<p class="live-empty">EMPTY // ' + esc(message || '暂无数据') + '</p>';
    }

    function setMeta(page, text) {
        var el = document.querySelector('#page-' + page + ' .ph-meta');
        if (el) el.textContent = text;
    }

    function pane(tag, title, meta, body, extraClass) {
        var interactive = /<(input|select|button|textarea)\b/i.test(body);
        var cls = 'pane' + (interactive ? ' interactive' : '') +
            (extraClass ? ' ' + extraClass : '');
        return '<section class="' + esc(cls) + '">' +
            '<header class="pane-head"><span class="pane-tag">' + esc(tag) + '</span>' +
            '<span class="pane-title">' + esc(title) + '</span>' +
            '<span class="pane-meta">' + esc(meta || '') + '</span></header>' +
            '<div class="pane-body">' + body + '</div></section>';
    }

    function kv(label, value) {
        return '<div class="kv"><span class="k">' + esc(label) + '</span>' +
            '<i class="dots" aria-hidden="true"></i><span class="v">' + value + '</span></div>';
    }

    function bar(fill, total, cls) {
        var t = Math.max(1, num(total));
        var n = Math.max(0, Math.min(t, num(fill)));
        var cells = 10;
        var filled = Math.round(n / t * cells);
        return '<span class="meter ' + esc(cls || '') + '"><span class="fill">' +
            new Array(filled + 1).join('█') + '</span><span class="rest">' +
            new Array(cells - filled + 1).join('░') + '</span></span>';
    }

    function spark(values) {
        var chars = '▁▂▃▄▅▆▇█';
        var list = (values || []).map(num);
        if (!list.length) {
            return new Array(7).join('▁') + '▁';
        }
        var max = Math.max.apply(null, list.concat([1]));
        return list.map(function (value) {
            var idx = Math.min(chars.length - 1, Math.round(value / max * (chars.length - 1)));
            return chars.charAt(idx);
        }).join('');
    }

    /* ------------------------------------------------------------------ */
    /* 首页                                                                */
    /* ------------------------------------------------------------------ */
    function loadWelcome(force) {
        if (state.loading.welcome) return Promise.resolve();
        if (!force && state.welcome && Date.now() - state.welcomeAt < 15000) return Promise.resolve();
        state.loading.welcome = true;
        var root = liveRoot('welcome');
        if (!state.welcome && root) loading('welcome', 'CONNECTING · 读取登录态、配置、用量与日志...');

        return Promise.all([
            settle(api('/auth/status')),
            settle(api('/config/llm')),
            settle(api('/config/llm/usage?days=7')),
            settle(api('/hotspot/watch?limit=50')),
            settle(api('/hotspot/events?limit=50')),
            settle(api('/comment/dashboard')),
            settle(api('/logs/?limit=8')),
            settle(api('/config/'))
        ]).then(function (results) {
            var auth = results[0].ok ? results[0].value : null;
            var llm = results[1].ok ? results[1].value : null;
            var usage = results[2].ok ? results[2].value : null;
            var watch = results[3].ok ? results[3].value : null;
            var events = results[4].ok ? results[4].value : null;
            var dashboard = results[5].ok ? results[5].value : null;
            var logs = results[6].ok ? results[6].value : null;
            var config = results[7].ok ? results[7].value : null;
            state.welcome = {
                auth: auth, llm: llm, usage: usage, watch: watch,
                events: events, dashboard: dashboard, logs: logs, config: config
            };
            state.welcomeAt = Date.now();
            renderWelcome();
        }).catch(function (error) {
            setHTML('welcome', errorBox(error.message));
        }).then(function () {
            state.loading.welcome = false;
        });
    }

    function renderWelcome() {
        var data = state.welcome || {};
        var auth = data.auth || {};
        var llm = (data.llm && data.llm.config) || {};
        var usage = data.usage || {};
        var watchData = (data.watch && data.watch.data) || {};
        var eventData = (data.events && data.events.data) || {};
        var dash = (data.dashboard && data.dashboard.data) || {};
        var visual = dash.visualization || {};
        var dedup = visual.dedup_statistics || {};
        var logs = (data.logs && data.logs.records) || [];
        var allConfig = (data.config && data.config.config) || {};
        var user = auth.user || null;
        var today = usage.today || {};
        var limit = num(today.limit) || num(llm.daily_token_limit);
        var used = num(today.total_tokens);
        var authName = auth.logged_in && user
            ? (user.username || ('UID ' + user.uid))
            : '未登录 / GUEST';
        var authAction = auth.logged_in && user
            ? '<button class="btn" type="button" data-live-act="logout">[LOGOUT]</button>'
            : '<button class="btn btn-primary" type="button" data-live-act="login">[LOGIN] SCAN</button>';
        var eventsCount = num(eventData.total);
        var watchCount = num(watchData.total);
        var comments = num(dedup.before_count);
        var negative = num((visual.sentiment_distribution || {}).negative);
        var totalSentiment = num((visual.sentiment_distribution || {}).positive) +
            num((visual.sentiment_distribution || {}).neutral) + negative;
        var video = dash.video || null;

        var sparkValues = (usage.daily_usage || []).map(function (day) { return num(day.total_tokens); });
        var sparkFoot = (usage.daily_usage || []).length ?
            String((usage.daily_usage[0].date || '')).slice(5) + ' → ' + String((usage.daily_usage[usage.daily_usage.length - 1].date || '')).slice(5) :
            'NO USAGE YET';
        var eventsHtml = logs.length ? logs.map(function (record) {
            var level = levelKey(record.level);
            return '<div class="log-line" data-level="' + level + '">' +
                '<span class="log-t">' + esc(hm(record.timestamp)) + '</span>' +
                '<span class="log-lv ' + level + '">' + esc(levelLabel(record.level)) + '</span>' +
                '<span class="log-mod">' + esc(short(record.source || 'app', 18)) + '</span>' +
                '<span class="log-msg">' + esc(record.message || '') + '</span></div>';
        }).join('') : emptyBox('暂无日志记录');

        var html = '' +
            '<p class="page-sub">本机真实数据 · ' + esc(location.host) +
            ' · 登录态 / 配置 / 用量 / 日志均来自当前服务。</p>' +
            '<div class="tui-command-strip">' +
            '<span class="strip-label">AUTH</span>' +
            '<span class="strip-value">' + esc(authName) + '</span>' +
            authAction +
            '<button class="btn" type="button" data-live-act="refresh">REFRESH</button>' +
            '<span class="strip-hint">SESSION CONTROL · ENTER</span></div>' +
            '<div class="banner"><pre class="banner-art" aria-hidden="true">><((((\'></pre>' +
            '<div class="banner-copy"><p class="banner-title">FISHTOOL <span>//</span> OPERATIONS CONSOLE</p>' +
            '<p class="banner-sub">让运营不再成为企业特权。</p></div>' +
            '<span class="banner-meta">BUILD 0.3.1<br>REAL API / TTY</span></div>' +
            '<div class="metrics" aria-label="运营指标">' +
            '<div class="metric"><span class="m-code">EVENTS</span><span class="m-val">' + fmt(eventsCount) +
            '</span><span class="m-label">热点事件 <em>' + fmt((eventData.items || []).length) + ' recent</em></span></div>' +
            '<div class="metric"><span class="m-code">WATCHING</span><span class="m-val">' + fmt(watchCount) +
            '</span><span class="m-label">跟踪视频 <em class="' + ((watchData.collector || {}).running ? 'is-ok' : '') + '">' +
            ((watchData.collector || {}).running ? 'LIVE' : 'IDLE') + '</em></span></div>' +
            '<div class="metric"><span class="m-code">COMMENTS</span><span class="m-val">' + fmt(comments) +
            '</span><span class="m-label">' + esc(video ? short(video.title, 18) : '最新视频评论') +
            ' <em>' + (negative && totalSentiment ? pct(negative, totalSentiment) + ' 负面' : '暂无') + '</em></span></div>' +
            '<div class="metric"><span class="m-code">TOKENS</span><span class="m-val">' + compact(used) +
            '</span><span class="m-label">LLM Token <em class="' + (limit && used / limit > .8 ? 'is-warn' : 'is-ok') + '">' +
            (limit ? pct(used, limit) : 'UNLIMITED') + '</em></span></div></div>' +
            '<div class="tui-card-grid welcome-grid">' +
            pane('SESSION', '运行状态', 'LOCAL · READ ONLY', '' +
                kv('auth', esc(authName)) +
                kv('model', esc((llm.provider || 'openai') + ' / ' + (llm.model || '未配置'))) +
                kv('base', esc(short(llm.api_base || '未配置', 34))) +
                kv('database', esc(((allConfig.database || {}).path) || 'data/bili_ops.db')) +
                kv('watch', fmt(watchCount) + ' targets · ' + ((watchData.collector || {}).running ? 'LIVE' : 'IDLE')) +
                kv('api', esc(location.host))) +
            pane('ACTIONS', '快捷入口', '↑↓ + ENTER',
                '<div class="menu-list" data-live-menu>' +
                menuButton('config', '配置 AI 大模型', '03 CONFIG') +
                menuButton('hotspot', '探索热点', '01 HOTSPOT') +
                menuButton('watch', '监控评论', '02 WATCH') +
                menuButton('logs', '查看日志', '07 LOGS') +
                '</div>') +
            pane('ACTIVITY', '7D TOKEN USAGE', 'LLM / DAY',
                '<div class="spark-col"><pre class="spark" aria-label="7 天 Token 用量">' + spark(sparkValues) + '</pre>' +
                '<div class="spark-foot"><span>' + esc(sparkFoot) + '</span></div></div>' +
                '<div class="spark-stats"><span>TOTAL ' + fmt(usage.total_tokens || 0) + '</span>' +
                '<span>REQ ' + fmt(usage.total_requests || 0) + '</span>' +
                '<span class="is-ok">TODAY ' + fmt(used) + '</span></div>') +
            pane('BUDGET', '今日用量', 'LLM',
                '<div class="bar-row"><span class="bar-label">TOKEN</span>' +
                bar(used, limit || Math.max(used, 1)) +
                '<span class="bar-val">' + (limit ? fmt(used) + ' / ' + fmt(limit) : fmt(used) + ' / ∞') + '</span></div>' +
                '<div class="bar-row"><span class="bar-label">7D REQUESTS</span>' +
                bar(num(usage.total_requests), Math.max(num(usage.total_requests), 1)) +
                '<span class="bar-val">' + fmt(usage.total_requests || 0) + '</span></div>' +
                '<div class="bar-row"><span class="bar-label">MAX TOKENS</span>' +
                '<span class="bar-val">' + fmt(llm.max_tokens || 0) + ' / call</span></div>' +
                '<p class="pane-note">额度由 /api/config/llm/usage 读取；没有调用记录时显示 0，不伪造数据。</p>') +
            '</div>' +
            pane('EVENTS', '最近事件', 'LIVE · ' + logs.length,
                '<div class="logs">' + eventsHtml + '</div>') +
            '</div>';

        setHTML('welcome', html);
        setMeta('welcome', 'LIVE · ' + (auth.logged_in ? 'AUTH OK' : 'NOT LOGGED IN'));
    }

    function menuButton(page, title, meta) {
        return '<button class="menu-item" type="button" data-goto="' + esc(page) + '" data-label="' + esc(title) + '">' +
            '<span class="mi-mark" aria-hidden="true">&gt;</span>' +
            '<span class="mi-title">' + esc(title) + '</span>' +
            '<span class="mi-meta">' + esc(meta) + '</span></button>';
    }

    /* ------------------------------------------------------------------ */
    /* 热点                                                                */
    /* ------------------------------------------------------------------ */
    function loadHotspot(force) {
        if (state.loading.hotspot) return Promise.resolve();
        if (!force && state.hotspot) {
            renderHotspot();
            return Promise.resolve();
        }
        state.loading.hotspot = true;
        if (!liveRoot('hotspot') || !liveRoot('hotspot').querySelector('.live-actions')) {
            renderHotspotShell();
        }
        var zone = state.zone;
        var option = findZone(zone);
        var tid = option ? option.tid : 1008;
        return Promise.all([
            settle(api('/hotspot/zones')),
            settle(api('/hotspot/watch?limit=50')),
            settle(api('/hotspot/lifecycle?tid=' + encodeURIComponent(tid) + '&algorithm=lifecycle_v2')),
            settle(api('/hotspot/discovery/latest')),
            settle(api('/hotspot/topics?limit=20')),
            settle(api('/hotspot/events?limit=20'))
        ]).then(function (results) {
            var zones = results[0].ok ? results[0].value : null;
            var watch = results[1].ok ? results[1].value : null;
            var lifecycle = results[2].ok ? results[2].value : null;
            var discovery = results[3].ok ? results[3].value : null;
            var library = results[4].ok ? results[4].value : null;
            var events = results[5].ok ? results[5].value : null;
            if (zones) {
                state.zones = zones.zones || [];
                state.zoneOptions = zones.zone_options || [];
            }
            state.hotspot = {
                watch: watch,
                lifecycle: lifecycle,
                discovery: discovery,
                library: library,
                events: events,
                tags: state.hotspot ? state.hotspot.tags : null,
                activities: state.hotspot ? state.hotspot.activities : null,
                topics: state.hotspot ? state.hotspot.topics : null
            };
            renderHotspot();
        }).catch(function (error) {
            var root = liveRoot('hotspot');
            if (root && !root.querySelector('.live-actions')) {
                setHTML('hotspot', errorBox(error.message));
            } else {
                setStatus('hotspot-status', 'ERROR // ' + error.message);
            }
        }).then(function () {
            state.loading.hotspot = false;
        });
    }

    function findZone(name) {
        for (var i = 0; i < state.zoneOptions.length; i += 1) {
            if (state.zoneOptions[i].name === name) return state.zoneOptions[i];
        }
        return null;
    }

    function renderHotspotShell() {
        var options = state.zones.map(function (name) {
            return '<option value="' + esc(name) + '"' + (name === state.zone ? ' selected' : '') + '>' + esc(name) + '</option>';
        }).join('');
        var html = '' +
            '<p class="page-sub">分区热词、活动情报、生命周期与跟踪列表。动作会真实请求 /api/hotspot。</p>' +
            '<div class="live-actions">' +
            '<select class="live-select" id="hs-zone" aria-label="热点分区">' + options + '</select>' +
            '<button class="btn btn-primary" type="button" data-live-act="hotspot_tags" data-label="采集词云">SCAN TAG CLOUD</button>' +
            '<button class="btn" type="button" data-live-act="hotspot_activities" data-label="拉取活动">ACTIVITIES</button>' +
            '<button class="btn" type="button" data-live-act="hotspot_refresh" data-label="刷新热点">REFRESH</button>' +
            '<span class="hint" id="hotspot-status">LIVE · /api/hotspot</span>' +
            '</div>' +
            '<div class="tui-card-grid hotspot-grid">' +
            pane('TAG CLOUD', '分区热词', 'TOP 20', '<div id="hs-tags" class="live-tags">' + emptyBox('尚未采集词云') + '</div>') +
            pane('WATCH', '跟踪视频', 'REAL', '<div id="hs-watch">' + emptyBox('暂无跟踪目标') + '</div>' +
                '<form class="live-actions" data-live-form="watch_add">' +
                '<input class="live-input" id="hs-bvid" placeholder="BV1..." aria-label="跟踪 BV 号">' +
                '<button class="btn btn-primary" type="submit">WATCH</button></form>') +
            pane('ACTIVITIES', '活动情报', 'POST /activities', '<div id="hs-activities">' + emptyBox('尚未拉取活动') + '</div>') +
            pane('LIFECYCLE', '生命周期', 'LIVE',
                '<div class="live-actions"><button class="btn" type="button" data-live-act="lifecycle_collect">COLLECT SNAPSHOT</button></div>' +
                '<div id="hs-lifecycle">' + emptyBox('加载中') + '</div>') +
            '</div>' +
            pane('EVENTS', '事件工作台', 'REAL · /events',
                '<div class="live-actions">' +
                '<input class="live-input live-input-wide" id="hs-event-name" placeholder="新事件名称，例如：某番剧争议" aria-label="事件名称">' +
                '<button class="btn btn-primary" type="button" data-live-act="event_create">CREATE</button>' +
                '<button class="btn" type="button" data-live-act="event_refresh">REFRESH</button>' +
                '<button class="btn" type="button" data-live-act="event_discover">DISCOVER</button>' +
                '<button class="btn" type="button" data-live-act="event_assess">ASSESS</button>' +
                '<button class="btn" type="button" data-live-act="event_budget">BUDGET</button></div>' +
                '<div class="live-note" id="hs-event-status">IDLE · 选择事件后可发现 / 评估 / 查看预算；创建只写草稿，不自动采集。</div>' +
                '<div id="hs-events"></div><div id="hs-event-detail"></div>') +
            pane('AI TOPICS', '选题助手', 'POST /topics/generate',
                '<form data-live-form="topics" class="live-form-grid">' +
                '<label class="field live-field"><span class="field-label">DIRECTION</span>' +
                '<input class="live-input live-input-wide" id="hs-direction" placeholder="例如：开学季 3000 元装机" aria-label="创作方向"></label>' +
                '<label class="field live-field"><span class="field-label">COUNT</span>' +
                '<input class="live-input live-input-sm" id="hs-count" type="number" min="1" max="20" value="6" aria-label="选题数量"></label>' +
                '<div class="live-actions"><button class="btn btn-primary" type="submit">GENERATE</button>' +
                '<span class="hint" id="hs-topic-status">LLM / 规则模板</span></div></form>' +
                '<div id="hs-topics">' + emptyBox('尚未生成选题') + '</div>') +
            pane('TOPIC LIBRARY', '选题库', 'DB · REAL', '<div id="hs-topic-library">' + emptyBox('加载中') + '</div>') +
            pane('DISCOVERY', '最近发现轮', 'GET /discovery/latest', '<div id="hs-discovery">' + emptyBox('加载中') + '</div>');
        setHTML('hotspot', html);
        setMeta('hotspot', 'LIVE · /api/hotspot');
        var zoneSelect = document.getElementById('hs-zone');
        if (zoneSelect) {
            zoneSelect.addEventListener('change', function () {
                state.zone = zoneSelect.value;
                state.hotspot = null;
                loadHotspot(true);
            });
        }
    }

    function renderHotspot() {
        if (!liveRoot('hotspot') || !document.getElementById('hs-tags')) {
            renderHotspotShell();
        }
        var data = state.hotspot || {};
        var watch = (data.watch && data.watch.data) || {};
        var life = (data.lifecycle && data.lifecycle.data) || {};
        var discovery = (data.discovery && data.discovery.data) || {};
        var snapshot = discovery.snapshot || null;
        var zoneSelect = document.getElementById('hs-zone');
        if (zoneSelect && zoneSelect.options.length !== state.zones.length) {
            zoneSelect.innerHTML = state.zones.map(function (name) {
                return '<option value="' + esc(name) + '"' + (name === state.zone ? ' selected' : '') + '>' + esc(name) + '</option>';
            }).join('');
        }
        var watchHtml = (watch.items || []).length ? (watch.items || []).map(function (item) {
            var stage = item.last_confirmed_stage || item.display_state || item.state || 'tracking';
            return '<div class="kv"><span class="k">' + esc(item.bvid) + '</span>' +
                '<i class="dots" aria-hidden="true"></i><span class="v">' + esc(stage) +
                ' {' + esc(item.state || '') + '} <button class="live-link" type="button" data-live-act="watch_release" data-bvid="' +
                esc(item.bvid) + '">RELEASE</button></span></div>';
        }).join('') : emptyBox('暂无跟踪目标，先在上方添加 BV');
        setHTML('hs-watch', watchHtml);
        var items = life.items || [];
        setHTML('hs-lifecycle', items.length ? items.slice(0, 12).map(function (item) {
            var change = item.relative_change !== undefined ? item.relative_change : item.growth;
            return '<div class="kv"><span class="k">' + esc(short(item.title || item.bvid, 18)) + '</span>' +
                '<i class="dots" aria-hidden="true"></i><span class="v">' + esc(item.stage || '—') +
                (change !== undefined && change !== null ? ' · ' + esc(String(change)) : '') +
                '</span></div>';
        }).join('') : emptyBox('暂无生命周期数据，先跑一轮采集'));
        setHTML('hs-discovery', snapshot ?
            '<div class="kv"><span class="k">CAPTURED</span><i class="dots"></i><span class="v">' +
            esc(dateOf(new Date(num(snapshot.captured_epoch_s) * 1000).toISOString())) + '</span></div>' +
            '<div class="kv"><span class="k">VIDEOS</span><i class="dots"></i><span class="v">' + fmt(snapshot.video_count) + '</span></div>' +
            '<div class="kv"><span class="k">KEYWORDS</span><i class="dots"></i><span class="v">' + fmt(snapshot.keyword_count) + '</span></div>' +
            ((snapshot.keywords || []).length ? '<div class="tagline">' + snapshot.keywords.slice(0, 12).map(function (item) {
                return '<span class="tag">' + esc(item.keyword || item) + '</span>';
            }).join('') + '</div>' : '') :
            emptyBox('尚未跑过发现轮'));
        if (data.tags) renderTags(data.tags);
        if (data.activities) renderActivities(data.activities);
        if (data.topics) renderTopics(data.topics);
        if (data.library) renderTopicLibrary(data.library);
        renderEvents(data.events);
        setMeta('hotspot', 'LIVE · ' + state.zone);
    }

    function renderEvents(payload) {
        var data = payload && payload.data ? payload.data : payload || {};
        var items = data.items || [];
        var list = document.getElementById('hs-events');
        if (!list) return;
        list.innerHTML = items.length ? items.map(function (item) {
            return '<div class="kv"><span class="k">' + esc(short(item.name || item.event_id, 30)) + '</span>' +
                '<i class="dots" aria-hidden="true"></i><span class="v">' + esc(item.status || '—') +
                ' · rev ' + fmtMaybe(item.revision) +
                ' <button class="live-link" type="button" data-live-act="event_select" data-id="' +
                esc(item.event_id) + '">OPEN</button></span></div>';
        }).join('') : emptyBox('暂无事件；先创建一个草稿');
        if (state.event) renderEventDetail();
        else {
            var detail = document.getElementById('hs-event-detail');
            if (detail && !detail.innerText.trim()) detail.innerHTML = '';
        }
    }

    function selectEvent(eventId) {
        state.event = null;
        state.opportunity = null;
        setStatus('hs-event-status', 'EVENT · ' + eventId + ' ...');
        return Promise.all([
            api('/hotspot/events/' + encodeURIComponent(eventId)),
            settle(api('/hotspot/events/' + encodeURIComponent(eventId) + '/members')),
            settle(api('/hotspot/events/' + encodeURIComponent(eventId) + '/budget')),
            settle(api('/hotspot/events/' + encodeURIComponent(eventId) + '/assessments?limit=5'))
        ]).then(function (results) {
            state.event = results[0].data || null;
            state.eventMembers = results[1].ok ? (results[1].value.data || null) : null;
            state.eventBudget = results[2].ok ? (results[2].value.data || null) : null;
            state.eventAssessments = results[3].ok ? (results[3].value.data || null) : null;
            setStatus('hs-event-status', 'EVENT · ' + eventId + ' · ' + ((state.event && state.event.status) || ''));
            renderEventDetail();
            return state.event;
        }).catch(function (error) {
            setStatus('hs-event-status', 'ERROR // ' + error.message);
            notify('事件读取失败: ' + error.message, 'warn');
        });
    }

    function renderEventDetail() {
        var event = state.event;
        var root = document.getElementById('hs-event-detail');
        if (!root || !event) return;
        var members = (state.eventMembers && state.eventMembers.items) || [];
        var byStatus = (state.eventMembers && state.eventMembers.by_status) || {};
        var budget = state.eventBudget || {};
        var assessments = (state.eventAssessments && state.eventAssessments.items) || [];
        var memberRows = members.slice(0, 30).map(function (member) {
            var action = member.status === 'proposed'
                ? '<button class="live-link" type="button" data-live-act="event_member" data-bvid="' + esc(member.bvid) +
                  '" data-status="accepted">ACCEPT</button> <button class="live-link" type="button" data-live-act="event_member" data-bvid="' +
                  esc(member.bvid) + '" data-status="rejected">REJECT</button>'
                : '';
            return '<div class="live-user"><div class="live-user-head"><b>' + esc(member.bvid) +
                '</b><span>' + esc(member.status) + '</span></div><div class="live-user-meta">rev ' +
                fmtMaybe(member.revision) + ' · source ' + esc(member.decision_source || '—') + ' ' + action + '</div></div>';
        }).join('');
        var assessmentRows = assessments.map(function (item) {
            return '<div class="live-user"><div class="live-user-head"><b>' +
                esc(item.window_kind || 'assessment') + '</b><span>' + esc(item.status || '') + '</span></div>' +
                '<div class="live-user-meta">' + esc(item.assessment_id || '') + ' · ' +
                esc((item.reason_codes || []).join(',')) + '</div></div>';
        }).join('');
        var budgetText = budget.available === false ? '不可用' :
            ('discovery ' + JSON.stringify(budget.budget && budget.budget.discovery_requests_per_24h || {}) +
             ' · watch ' + JSON.stringify(budget.budget && budget.budget.watch_samples_per_24h || {}));
        var statusBtn = event.status === 'active'
            ? '<button class="live-link" type="button" data-live-act="event_toggle" data-status="paused">PAUSE</button>'
            : '<button class="live-link" type="button" data-live-act="event_toggle" data-status="active">ACTIVATE</button>';
        root.innerHTML = '<div class="live-code-line"><b>' + esc(event.name || event.event_id) + '</b> ' + statusBtn +
            '<br><span class="live-note">' + esc(event.event_id) + ' · ' + esc(event.status) +
            ' · rev ' + fmtMaybe(event.revision) + ' · rule v' + fmtMaybe(event.current_rule_version) + '</span></div>' +
            '<div class="live-note">members ' + members.length + ' · proposed ' + fmtMaybe(byStatus.proposed) +
            ' · accepted ' + fmtMaybe(byStatus.accepted) + ' · rejected ' + fmtMaybe(byStatus.rejected) + '</div>' +
            (members.length ? '<div class="live-code-line"><b>成员决定</b></div>' + memberRows : emptyBox('暂无成员')) +
            '<div class="live-note">BUDGET · ' + esc(budgetText) + '</div>' +
            '<div class="live-actions">' +
            '<button class="btn" type="button" data-live-act="event_opportunity">OPPORTUNITY</button>' +
            '<label class="field live-field"><span class="field-label">PROD H</span>' +
            '<input class="live-input live-input-sm" id="event-brief-prod" type="number" min="0" value="2" aria-label="制作小时"></label>' +
            '<label class="field live-field"><span class="field-label">REVIEW H</span>' +
            '<input class="live-input live-input-sm" id="event-brief-review" type="number" min="0" value="1" aria-label="审核小时"></label>' +
            '<label class="field live-field"><span class="field-label">BUFFER H</span>' +
            '<input class="live-input live-input-sm" id="event-brief-buffer" type="number" min="0" value="0" aria-label="发布缓冲小时"></label>' +
            '<label class="field live-field"><span class="field-label">EXP H</span>' +
            '<input class="live-input live-input-sm" id="event-brief-exp" type="number" min="0" value="4" aria-label="试做小时"></label>' +
            '<input class="live-input" id="event-topic-id" placeholder="TOPIC ID（反馈可选）" aria-label="选题 ID">' +
            '<button class="live-link" type="button" data-live-act="opp_feedback" data-kind="adopted">ADOPT</button>' +
            '<button class="live-link" type="button" data-live-act="opp_feedback" data-kind="rejected">REJECT</button>' +
            '<button class="live-link" type="button" data-live-act="opp_feedback" data-kind="published">PUBLISHED</button></div>' +
            '<div id="hs-opportunity"></div>' +
            (assessmentRows ? '<div class="live-code-line"><b>最近评估</b></div>' + assessmentRows : emptyBox('暂无评估'));
        renderOpportunity();
    }

    function refreshEvents() {
        return api('/hotspot/events?limit=20').then(function (payload) {
            renderEvents(payload);
            setStatus('hs-event-status', 'EVENTS · ' + ((payload.data && payload.data.total) || 0));
        }).catch(function (error) {
            setStatus('hs-event-status', 'ERROR // ' + error.message);
        });
    }

    function createEvent() {
        var name = ((document.getElementById('hs-event-name') || {}).value || '').trim();
        if (!name) { notify('输入事件名称', 'warn'); return Promise.resolve(); }
        return api('/hotspot/events', { method: 'POST', body: { name: name, status: 'draft' } })
            .then(function (payload) {
                var data = payload.data || {};
                var input = document.getElementById('hs-event-name');
                if (input) input.value = '';
                notify('OK // 事件草稿已创建', 'ok');
                return refreshEvents().then(function () { return selectEvent(data.event_id); });
            }).catch(function (error) {
                notify('创建事件失败: ' + error.message, 'warn');
            });
    }

    function eventTask(kind) {
        if (!state.event) { notify('先打开一个事件', 'warn'); return Promise.resolve(); }
        var id = state.event.event_id;
        setStatus('hs-event-status', kind.toUpperCase() + ' · ' + id + ' ...');
        var path = kind === 'discover'
            ? '/hotspot/events/' + encodeURIComponent(id) + '/discover/tasks'
            : '/hotspot/events/' + encodeURIComponent(id) + '/assess/tasks';
        return api(path, { method: 'POST', body: kind === 'assess' ? { window_kind: 'daily24h' } : {} })
            .then(function (payload) {
                var data = payload.data || {};
                if (kind === 'assess') {
                    setStatus('hs-event-status', 'ASSESS · ' + (data.data_status || data.status || 'completed'));
                    return selectEvent(id);
                }
                state.eventTask = data.task_id;
                return pollEventTask(data.task_id, id);
            }).catch(function (error) {
                setStatus('hs-event-status', 'ERROR // ' + error.message);
                notify('事件任务失败: ' + error.message, 'warn');
            });
    }

    function pollEventTask(taskId, eventId) {
        if (!taskId) return Promise.resolve();
        return api('/hotspot/event-tasks/' + encodeURIComponent(taskId)).then(function (payload) {
            var task = payload.data || {};
            setStatus('hs-event-status', 'DISCOVER · ' + (task.status || 'running') + ' · ' + (task.run_id || ''));
            if (task.status === 'completed' || task.status === 'failed') {
                if (task.status === 'completed') notify('OK // 事件发现完成', 'ok');
                return selectEvent(eventId);
            }
            return sleep(1500).then(function () { return pollEventTask(taskId, eventId); });
        }).catch(function (error) {
            setStatus('hs-event-status', 'ERROR // ' + error.message);
        });
    }

    function eventMember(bvid, status) {
        if (!state.event) return Promise.resolve();
        return api('/hotspot/events/' + encodeURIComponent(state.event.event_id) + '/members/decisions', {
            method: 'POST',
            body: { expected_revision: state.event.revision, decisions: [{ bvid: bvid, status: status }] }
        }).then(function () {
            notify('成员已 ' + status, 'ok');
            return selectEvent(state.event.event_id);
        }).catch(function (error) {
            notify('成员决定失败: ' + error.message, 'warn');
        });
    }

    function toggleEvent(status) {
        if (!state.event) return Promise.resolve();
        return api('/hotspot/events/' + encodeURIComponent(state.event.event_id), {
            method: 'PATCH', body: { expected_revision: state.event.revision, status: status }
        }).then(function () {
            notify('事件已 ' + status, 'ok');
            return selectEvent(state.event.event_id);
        }).catch(function (error) {
            notify('事件状态失败: ' + error.message, 'warn');
        });
    }

    function eventBudget() {
        if (!state.event) { notify('先打开一个事件', 'warn'); return Promise.resolve(); }
        return api('/hotspot/events/' + encodeURIComponent(state.event.event_id) + '/budget')
            .then(function (payload) {
                state.eventBudget = payload.data || null;
                renderEventDetail();
                notify('预算已刷新', 'ok');
            }).catch(function (error) {
                notify('预算读取失败: ' + error.message, 'warn');
            });
    }

    function createOpportunity() {
        if (!state.event) { notify('先打开一个事件', 'warn'); return Promise.resolve(); }
        setStatus('hs-event-status', 'OPPORTUNITY · ' + state.event.event_id + ' ...');
        return api('/hotspot/opportunities/tasks', {
            method: 'POST',
            body: {
                creator_brief: {
                    brief_version: 'v1',
                    production_hours: Number((document.getElementById('event-brief-prod') || {}).value) || 0,
                    review_hours: Number((document.getElementById('event-brief-review') || {}).value) || 0,
                    publish_buffer_hours: Number((document.getElementById('event-brief-buffer') || {}).value) || 0,
                    max_experiment_hours: Number((document.getElementById('event-brief-exp') || {}).value) || 0
                },
                event_ids: [state.event.event_id]
            }
        }).then(function (payload) {
            var runId = (payload.data || {}).opportunity_run_id;
            if (!runId) throw new Error('未返回 opportunity_run_id');
            return api('/hotspot/opportunities/' + encodeURIComponent(runId)).then(function (runPayload) {
                state.opportunity = runPayload.data || null;
                renderOpportunity();
                setStatus('hs-event-status', 'OPPORTUNITY · ' + runId);
                notify('OK // 机会列表已生成', 'ok');
            });
        }).catch(function (error) {
            setStatus('hs-event-status', 'ERROR // ' + error.message);
            notify('机会生成失败: ' + error.message, 'warn');
        });
    }

    function renderOpportunity() {
        var root = document.getElementById('hs-opportunity');
        if (!root) return;
        var run = state.opportunity;
        if (!run) { root.innerHTML = ''; return; }
        var candidates = run.candidates || [];
        root.innerHTML = '<div class="live-note">run ' + esc(run.opportunity_run_id) +
            ' · rev ' + fmtMaybe(run.revision) + ' · candidate ' + candidates.length + '</div>' +
            (candidates.length ? candidates.slice(0, 10).map(function (candidate) {
                return '<div class="live-user"><div class="live-user-head"><b>' +
                    esc(candidate.event_id || '—') + '</b><span>' + esc(candidate.action || '—') + '</span></div>' +
                    '<div class="live-user-meta">' + esc(candidate.reason_code || '') + ' · ' +
                    esc(candidate.account_match || '') + ' · ETA ' +
                    esc((candidate.deadline || {}).publish_eta_s || '—') + '</div>' +
                    '<div class="live-user-text">' + esc((candidate.notes || []).join('；') || '') + '</div></div>';
            }).join('') : emptyBox('暂无机会候选（未伪造）'));
    }

    function feedbackOpportunity(kind) {
        var run = state.opportunity;
        if (!run) { notify('先生成机会列表', 'warn'); return Promise.resolve(); }
        var topicId = ((document.getElementById('event-topic-id') || {}).value || '').trim();
        if (!topicId) { notify('反馈需要填写 TOPIC ID', 'warn'); return Promise.resolve(); }
        var body = {
            feedback_id: 'fb-' + Date.now() + '-' + Math.random().toString(16).slice(2),
            expected_revision: Number(run.revision),
            event_id: run.candidates && run.candidates[0] ? (run.candidates[0].event_id || state.event.event_id) : state.event.event_id,
            topic_id: topicId,
            kind: kind,
            reason: 'TUI ' + kind
        };
        return api('/hotspot/opportunities/' + encodeURIComponent(run.opportunity_run_id) + '/feedback', {
            method: 'POST', body: body
        }).then(function (payload) {
            var data = payload.data || {};
            if (Number.isInteger(data.revision)) run.revision = data.revision;
            setStatus('hs-event-status', 'FEEDBACK · ' + kind);
            notify('机会反馈已记录', 'ok');
            renderOpportunity();
        }).catch(function (error) {
            notify('机会反馈失败: ' + error.message, 'warn');
        });
    }

    function renderTopicLibrary(payload) {
        var data = payload && payload.data ? payload.data : payload || {};
        var topics = data.topics || [];
        setHTML('hs-topic-library', topics.length ? topics.map(function (topic) {
            var id = topic.id;
            var status = topic.status || 'pending';
            var next = status === 'pending'
                ? '<button class="live-link" type="button" data-live-act="topic_status" data-id="' + esc(id) + '" data-status="adopted">ADOPT</button> ' +
                  '<button class="live-link" type="button" data-live-act="topic_status" data-id="' + esc(id) + '" data-status="rejected">REJECT</button>'
                : status === 'adopted'
                    ? '<button class="live-link" type="button" data-live-act="topic_status" data-id="' + esc(id) + '" data-status="published">PUBLISH</button>'
                    : '<button class="live-link" type="button" data-live-act="topic_status" data-id="' + esc(id) + '" data-status="pending">REOPEN</button>';
            return '<div class="live-user"><div class="live-user-head"><b>' + esc(short(topic.title || '—', 46)) +
                '</b><span>' + esc(status) + '</span></div><div class="live-user-meta">' +
                esc(topic.zone_name || '—') + ' · ' + esc(topic.created_at || '—') + '</div>' +
                (topic.description ? '<div class="live-user-text">' + esc(short(topic.description, 160)) + '</div>' : '') +
                '<div class="live-actions">' + next + '</div></div>';
        }).join('') : emptyBox('选题库还是空的，先生成一批选题'));
    }

    function setTopicStatus(id, status) {
        return api('/hotspot/topics/' + encodeURIComponent(id), {
            method: 'PUT', body: { status: status }
        }).then(function () {
            notify('OK // 选题已标记为 ' + status, 'ok');
            return loadHotspot(true);
        }).catch(function (error) {
            notify('选题状态更新失败: ' + error.message, 'warn');
        });
    }

    function collectLifecycle() {
        var option = findZone(state.zone);
        var tid = option ? option.tid : 1008;
        setStatus('hotspot-status', 'COLLECT · ' + state.zone);
        return api('/hotspot/collect?tid=' + encodeURIComponent(tid) + '&limit=20', { method: 'POST', body: {} })
            .then(function () {
                function poll() {
                    return api('/hotspot/collect/progress').then(function (payload) {
                        var data = payload.data || {};
                        var text = [data.stage || data.phase || data.status || 'collecting',
                            data.current !== undefined && data.total !== undefined ? data.current + '/' + data.total : '']
                            .filter(Boolean).join(' · ');
                        setStatus('hotspot-status', 'COLLECT · ' + text);
                        if (data.finished || data.done || data.status === 'completed') {
                            notify('OK // 热点采集完成', 'ok');
                            return loadHotspot(true);
                        }
                        return sleep(1500).then(poll);
                    });
                }
                return poll();
            }).catch(function (error) {
                setStatus('hotspot-status', 'ERROR // ' + error.message);
                notify('采集失败: ' + error.message, 'warn');
            });
    }

    function renderTags(result) {
        var data = result && result.data ? result.data : result || {};
        var words = data.word_frequency || {};
        var pairs = Object.keys(words).map(function (key) { return [key, num(words[key])]; })
            .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 20);
        setHTML('hs-tags', pairs.length ? pairs.map(function (pair) {
            return '<span class="tag' + (pair[1] >= 4 ? ' hot' : '') + '">' + esc(pair[0]) +
                ' <i>' + fmt(pair[1]) + '</i></span>';
        }).join('') : emptyBox('这次采集没有返回词频'));
    }

    function renderActivities(result) {
        var data = result && result.data ? result.data : result || {};
        var items = data.activities || [];
        setHTML('hs-activities', items.length ? items.slice(0, 12).map(function (item) {
            var link = item.link ? '<a class="live-link" href="' + esc(item.link) + '" target="_blank" rel="noopener noreferrer">OPEN</a>' : '';
            return '<div class="kv"><span class="k">' + esc(short(item.title || '—', 18)) + '</span>' +
                '<i class="dots" aria-hidden="true"></i><span class="v">' + esc(item.status || '') +
                (item.source ? ' · ' + esc(item.source) : '') + ' ' + link + '</span></div>';
        }).join('') : emptyBox('暂无活动数据'));
    }

    function renderTopics(result) {
        var data = result && result.data ? result.data : result || {};
        if (data.accepted || data.status === 'running') {
            setHTML('hs-topics', '<p class="live-note">[生成中] 请求已受理，稍后重试或到选题库查看。</p>');
            return;
        }
        var topics = data.topics || [];
        setHTML('hs-topics', topics.length ? topics.map(function (topic, index) {
            return '<div class="live-code-line"><b>' + (index + 1) + '. ' + esc(topic.title || '') + '</b><br>' +
                esc(topic.description || '') + '<br><span class="live-note">难度 ' +
                esc(topic.difficulty || '—') + ' · ' + esc((topic.keywords || []).join(', ')) + '</span></div>';
        }).join('') : emptyBox(data.reason || '没有生成选题'));
    }

    function setStatus(id, text) {
        var el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function hotspotAction(action) {
        if (action === 'hotspot_refresh') {
            state.hotspot = null;
            state.loading.hotspot = false;
            return loadHotspot(true);
        }
        if (action === 'hotspot_tags') return runTagCloud();
        if (action === 'hotspot_activities') return runActivities();
        if (action === 'hotspot_topics') return runTopics();
        return Promise.resolve();
    }

    function runTagCloud() {
        var zone = state.zone;
        state.hotspot = state.hotspot || {};
        setStatus('hotspot-status', 'SCANNING · ' + zone);
        return api('/hotspot/tag-cloud/tasks', {
            method: 'POST', body: { zone_name: zone, limit: 100, top_n: 50 }
        }).then(function (started) {
            var taskId = started.task_id;
            function poll() {
                return api('/hotspot/tag-cloud/tasks/' + encodeURIComponent(taskId)).then(function (payload) {
                    var task = payload.data || {};
                    setStatus('hotspot-status', (task.status || 'running') + ' · ' + (task.progress || 0) + '% · ' + (task.message || ''));
                    if (task.status === 'completed') {
                        state.hotspot.tags = { data: task.result || {} };
                        renderTags(state.hotspot.tags);
                        notify('OK // 词云完成 · ' + zone, 'ok');
                        return null;
                    }
                    if (task.status === 'failed') throw new Error(task.message || '词云任务失败');
                    return sleep(1000).then(poll);
                });
            }
            return poll();
        }).catch(function (error) {
            setStatus('hotspot-status', 'ERROR // ' + error.message);
            notify('词云失败: ' + error.message, 'warn');
        });
    }

    function runActivities() {
        var zone = state.zone;
        state.hotspot = state.hotspot || {};
        var apiZone = zone === '游戏' ? 'game' : zone === '动画' ? 'anime' : zone === '绘画' ? 'paint' : 'all';
        setStatus('hotspot-status', 'ACTIVITIES · ' + zone);
        return api('/hotspot/activities', {
            method: 'POST', body: { include_ugc: true, zone: apiZone }
        }).then(function (result) {
            state.hotspot.activities = result;
            renderActivities(result);
            setStatus('hotspot-status', 'ACTIVITIES OK · ' + zone);
            notify('OK // 活动情报已更新', 'ok');
        }).catch(function (error) {
            setStatus('hotspot-status', 'ERROR // ' + error.message);
            notify('活动拉取失败: ' + error.message, 'warn');
        });
    }

    function runTopics() {
        var direction = (document.getElementById('hs-direction') || {}).value || '';
        var count = num((document.getElementById('hs-count') || {}).value) || 6;
        state.hotspot = state.hotspot || {};
        if (!direction.trim()) {
            notify('请输入创作方向', 'warn');
            return Promise.resolve();
        }
        setStatus('hs-topic-status', 'GENERATING...');
        return api('/hotspot/topics/generate', {
            method: 'POST',
            body: { direction: direction.trim(), zone_name: state.zone, count: count, use_llm: true }
        }).then(function (result) {
            state.hotspot.topics = result;
            renderTopics(result);
            setStatus('hs-topic-status', 'OK · ' + ((result.data || {}).generation_mode || 'generated'));
        }).catch(function (error) {
            setStatus('hs-topic-status', 'ERROR // ' + error.message);
            notify('选题失败: ' + error.message, 'warn');
        });
    }

    function addWatch(bvid) {
        if (!bvid) {
            notify('请输入 BV 号', 'warn');
            return Promise.resolve();
        }
        return api('/hotspot/watch', { method: 'POST', body: { bvid: bvid } })
            .then(function () {
                notify('OK // 已加入跟踪 ' + bvid, 'ok');
                return loadHotspot(true);
            }).catch(function (error) {
                notify('加入跟踪失败: ' + error.message, 'warn');
            });
    }

    function releaseWatch(bvid) {
        return api('/hotspot/watch/' + encodeURIComponent(bvid) + '/release', { method: 'POST' })
            .then(function () {
                notify('OK // 已释放 ' + bvid, 'ok');
                return loadHotspot(true);
            }).catch(function (error) {
                notify('释放失败: ' + error.message, 'warn');
            });
    }

    /* ------------------------------------------------------------------ */
    /* 评论监控                                                            */
    /* ------------------------------------------------------------------ */
    function loadWatch(force) {
        if (state.loading.watch) return Promise.resolve();
        if (!force && state.watch && Date.now() - state.watchAt < 15000) {
            renderWatch();
            return Promise.resolve();
        }
        state.loading.watch = true;
        if (!document.getElementById('watch-metrics')) {
            renderWatchShell();
        }
        return Promise.all([
            settle(api('/comment/dashboard')),
            settle(api('/comment/alerts?limit=20')),
            settle(api('/comment/resident/status'))
        ]).then(function (results) {
            state.watch = {
                dashboard: results[0].ok ? results[0].value : null,
                alerts: results[1].ok ? results[1].value : null,
                resident: results[2].ok ? results[2].value : null,
                account: state.watch ? state.watch.account : null
            };
            state.watchAt = Date.now();
            renderWatch();
        }).catch(function (error) {
            setHTML('watch', errorBox(error.message));
        }).then(function () {
            state.loading.watch = false;
        });
    }

    function renderWatchShell() {
        var html = '' +
            '<p class="page-sub">真实评论大屏与预警。输入 BV 可执行采集 / 监控；不填 BV 时读取最近落库视频。</p>' +
            '<div class="live-actions">' +
            '<input class="live-input" id="watch-bvid" placeholder="BV1..." aria-label="评论视频 BV 号">' +
            '<select class="live-select" id="watch-strategy" aria-label="采集策略">' +
            '<option value="normal">NORMAL · 快速</option><option value="full">FULL · 全面</option></select>' +
            '<button class="btn btn-primary" type="button" data-live-act="watch_monitor" data-label="开始监控">MONITOR</button>' +
            '<button class="btn" type="button" data-live-act="watch_collect" data-label="采集评论">COLLECT</button>' +
            '<button class="btn" type="button" data-live-act="watch_refresh" data-label="刷新评论">REFRESH</button>' +
            '<button class="btn" type="button" data-live-act="watch_export" data-label="导出评论">EXPORT</button>' +
            '<span class="hint" id="watch-status">LIVE · /api/comment</span>' +
            '</div>' +
            '<div class="metrics" id="watch-metrics">' +
            '<div class="metric"><span class="m-code">RAW</span><span class="m-val">0</span><span class="m-label">原始评论</span></div>' +
            '<div class="metric"><span class="m-code">CLEANED</span><span class="m-val">0</span><span class="m-label">清洗后</span></div>' +
            '<div class="metric"><span class="m-code">REMOVED</span><span class="m-val">0</span><span class="m-label">去重移除</span></div>' +
            '<div class="metric"><span class="m-code">NEGATIVE</span><span class="m-val">0%</span><span class="m-label">负面占比</span></div>' +
            '</div>' +
            pane('RESIDENT', '常驻监控', '/api/comment/resident', '<div id="watch-resident"></div>') +
            pane('TOOLS', '评论工具', 'LOCAL · REAL',
                '<div class="live-actions">' +
                '<input class="live-input live-input-wide" id="watch-keyword" placeholder="自定义预警词，逗号分隔" aria-label="自定义预警关键词">' +
                '<button class="btn" type="button" data-live-act="watch_keyword">ADD KEYWORD</button></div>' +
                '<div class="live-actions">' +
                '<input class="live-input" id="watch-account-uid" placeholder="UP UID" aria-label="账号监控 UID">' +
                '<input class="live-input live-input-sm" id="watch-account-limit" type="number" min="1" max="50" value="10" aria-label="账号监控视频数">' +
                '<select class="live-select" id="watch-account-strategy" aria-label="账号监控采集策略">' +
                '<option value="normal">NORMAL</option><option value="full">FULL</option></select>' +
                '<button class="btn" type="button" data-live-act="watch_account">MONITOR ACCOUNT</button></div>' +
                '<div class="live-note" id="watch-tools-status">关键词仅当前会话生效；账号监控会逐个采集最近视频。</div>' +
                '<div id="watch-account-result"></div>') +
            pane('SEMANTIC', '语义折叠（实验）', 'LLM · OPT-IN',
                '<div class="live-actions">' +
                '<input class="live-input live-input-wide" id="semantic-bvid" placeholder="BV1..." aria-label="语义折叠目标 BV">' +
                '<button class="btn" type="button" data-live-act="semantic_preflight">PREFLIGHT</button>' +
                '<button class="btn btn-primary" type="button" data-live-act="semantic_start">START</button>' +
                '<button class="btn" type="button" data-live-act="semantic_cancel">CANCEL</button>' +
                '<button class="btn" type="button" data-live-act="semantic_resume">RESUME</button></div>' +
                '<label class="live-check"><input type="checkbox" id="semantic-consent"> 我已知悉：分组由大模型给出、准确性未经验证，费用由服务商结算</label>' +
                '<label class="live-check"><input type="checkbox" id="semantic-auto-fold" checked> 自动折叠等价评论（仅展示）</label>' +
                '<label class="field live-field"><span class="field-label">BUDGET</span>' +
                '<select class="live-select" id="semantic-budget"><option value="default">DEFAULT</option><option value="unlimited">UNLIMITED</option></select></label>' +
                '<label class="live-check"><input type="checkbox" id="semantic-unlimited-confirm"> 确认无限制预算风险</label>' +
                '<div class="live-note" id="semantic-status">IDLE · 首次使用请先 PREFLIGHT 并勾选同意；开始分析会真实调用大模型。</div>' +
                '<div id="semantic-result"></div>') +
            '<div class="split">' +
            pane('SENTIMENT', '情感分布', 'LIVE', '<div id="watch-sentiment"></div>') +
            pane('TREND', '评论走势', 'DATE', '<div id="watch-trend"></div>') +
            '</div>' +
            '<div class="split">' +
            pane('ALERTS', '预警记录', 'RECENT 20', '<div id="watch-alerts"></div>') +
            pane('CLEANING', '清洗原因', 'DEDUP', '<div id="watch-cleaning"></div>') +
            '</div>' +
            pane('VOICE', '高声量评论', 'TOP 10', '<div id="watch-voice"></div>') +
            pane('WORDS', '高频词', 'TOP 8', '<div id="watch-words"></div>');
        setHTML('watch', html);
        setMeta('watch', 'LIVE · /api/comment');
    }

    function renderWatch() {
        var data = state.watch || {};
        var dash = (data.dashboard && data.dashboard.data) || {};
        var visual = dash.visualization || {};
        var dedup = visual.dedup_statistics || {};
        var sentiment = visual.sentiment_distribution || {};
        var alerts = (data.alerts && data.alerts.alerts) || [];
        var top = visual.top10_voice_comments || [];
        var before = num(dedup.before_count);
        var after = num(dedup.after_count);
        var removed = num(dedup.removed_count);
        var positive = num(sentiment.positive);
        var neutral = num(sentiment.neutral);
        var negative = num(sentiment.negative);
        var total = positive + neutral + negative;
        var video = dash.video;
        var metrics = document.querySelectorAll('#watch-metrics .m-val');
        var labels = document.querySelectorAll('#watch-metrics .m-label');
        if (metrics.length === 4) {
            metrics[0].textContent = fmt(before);
            metrics[1].textContent = fmt(after);
            metrics[2].textContent = fmt(removed);
            metrics[3].textContent = total ? pct(negative, total) : '0%';
        }
        if (labels.length === 4) {
            labels[0].textContent = video ? short(video.title, 22) : '原始评论';
            labels[1].textContent = '清洗后';
            labels[2].textContent = '去重移除';
            labels[3].textContent = '负面占比';
        }
        setStatus('watch-status', video ? ('LIVE · ' + short(video.title, 24)) : 'EMPTY · 暂无已落库评论');
        setMeta('watch', video ? 'LIVE · ' + short(video.bvid, 16) : 'EMPTY · NO COMMENTS');

        var sHtml = total ? [
            ['正面', positive, 'ok'], ['中性', neutral, ''], ['负面', negative, 'danger']
        ].map(function (row) {
            return '<div class="bar-row"><span class="bar-label">' + row[0] + '</span>' +
                bar(row[1], total, row[2] === 'ok' ? 'is-ok' : row[2] === 'danger' ? 'is-warn' : '') +
                '<span class="bar-val">' + fmt(row[1]) + ' · ' + pct(row[1], total) + '</span></div>';
        }).join('') : emptyBox('暂无情感数据');
        setHTML('watch-sentiment', sHtml);

        var counts = visual.date_comment_counts || [];
        var values = counts.map(function (item) { return num(item.count); });
        var trend = counts.length ? '<pre class="spark">' + spark(values) + '</pre>' +
            '<div class="spark-foot"><span>' + esc(counts[0].date) + '</span><span>' + esc(counts[counts.length - 1].date) + '</span></div>' +
            '<div class="spark-stats"><span>PEAK ' + fmt(Math.max.apply(null, values.concat([0]))) + '</span>' +
            '<span>AVG ' + fmt(values.reduce(function (a, b) { return a + b; }, 0) / Math.max(1, values.length)) + '</span></div>' : emptyBox('暂无日期评论数据');
        setHTML('watch-trend', trend);

        setHTML('watch-alerts', alerts.length ? alerts.slice(0, 8).map(function (alert) {
            var read = alert.is_read ? '<span class="stat-dim">READ</span>' :
                '<button class="live-link" type="button" data-live-act="alert_read" data-id="' +
                esc(alert.id || '') + '">MARK READ</button>';
            return '<div class="kv"><span class="k">' + esc(hm(alert.created_at)) + '</span>' +
                '<i class="dots" aria-hidden="true"></i><span class="v">' +
                '<span class="stat-' + (alert.level === 'high' ? 'danger' : alert.level === 'medium' ? 'warn' : 'dim') + '">' +
                esc(alert.level || '—') + '</span> ' + esc(short(alert.reason || alert.comment_text || '', 34)) +
                ' ' + read + '</span></div>';
        }).join('') : emptyBox('暂无预警记录'));

        var reasons = dedup.reasons || {};
        var reasonLabels = {
            same_user_repeat: '同用户重复', cross_user_aggregation: '跨用户聚合',
            fuzzy_similarity: '相似内容', time_window_hotspot: '时间窗热点'
        };
        var reasonKeys = Object.keys(reasonLabels);
        var maxReason = Math.max.apply(null, reasonKeys.map(function (key) { return num(reasons[key]); }).concat([1]));
        setHTML('watch-cleaning', reasonKeys.some(function (key) { return num(reasons[key]) > 0; }) ?
            reasonKeys.map(function (key) {
                return '<div class="bar-row"><span class="bar-label">' + reasonLabels[key] + '</span>' +
                    bar(num(reasons[key]), maxReason) + '<span class="bar-val">' + fmt(reasons[key] || 0) + '</span></div>';
            }).join('') : emptyBox('没有清洗记录'));

        setHTML('watch-voice', top.length ? '<table class="tbl"><thead><tr><th>USER</th><th>LIKE</th><th>CONTENT</th><th>SENTIMENT</th></tr></thead><tbody>' +
            top.map(function (item) {
                return '<tr><td>' + esc(short(item.uname || item.uid || '—', 14)) + '</td><td>' + fmt(item.like) +
                    '</td><td>' + esc(short(item.content || '', 42)) + '</td><td>' + esc(item.sentiment || '—') + '</td></tr>';
            }).join('') + '</tbody></table>' : emptyBox('暂无评论'));

        var words = {};
        top.forEach(function (item) {
            String(item.content || '').split(/[^\u4e00-\u9fa5A-Za-z0-9]+/).forEach(function (word) {
                if (word.length >= 2 && word.length <= 12) words[word] = (words[word] || 0) + 1;
            });
        });
        var pairs = Object.keys(words).map(function (key) { return [key, words[key]]; })
            .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 8);
        setHTML('watch-words', pairs.length ? pairs.map(function (pair) {
            return '<span class="tag">' + esc(pair[0]) + ' <i>' + pair[1] + '</i></span>';
        }).join(' ') : emptyBox('暂无高频词'));
        renderResident();
        renderWatchAccount();
        var semanticInput = document.getElementById('semantic-bvid');
        if (semanticInput && !semanticInput.value) {
            semanticInput.value = state.semantic.bvid || (video ? video.bvid : '');
        }
        renderSemantic();
    }

    function renderResident() {
        var data = (state.watch && state.watch.resident && state.watch.resident.data) || {};
        var status = data.status || 'stopped';
        var targets = data.target_bvids || [];
        var stateText = data.enabled ? (data.paused ? 'PAUSED' : 'RUNNING') : 'STOPPED';
        setHTML('watch-resident',
            '<div class="kv"><span class="k">state</span><i class="dots"></i><span class="v">' +
            esc(stateText) + ' · ' + esc(status) + '</span></div>' +
            '<div class="kv"><span class="k">targets</span><i class="dots"></i><span class="v">' +
            (targets.length ? esc(targets.join(', ')) : '未设置') + '</span></div>' +
            '<div class="kv"><span class="k">collected</span><i class="dots"></i><span class="v">' +
            fmt(data.total_collected) + ' · failures ' + fmt(data.consecutive_failures) + '</span></div>' +
            '<div class="live-actions">' +
            '<button class="btn btn-primary" type="button" data-live-act="resident_enable">ENABLE</button>' +
            '<button class="btn" type="button" data-live-act="resident_pause">PAUSE</button>' +
            '<button class="btn" type="button" data-live-act="resident_stop">STOP</button>' +
            '<span class="hint">' + esc(data.last_error || 'toggle with real POST endpoints') + '</span>' +
            '</div>');
    }

    function residentAction(action) {
        var bvid = ((document.getElementById('watch-bvid') || {}).value || '').trim();
        var url;
        var body;
        if (action === 'resident_enable') {
            url = '/comment/resident/enable';
            body = { bvids: bvid ? [bvid] : null };
        } else if (action === 'resident_pause') {
            url = '/comment/resident/pause';
            body = {};
        } else {
            url = '/comment/resident/stop';
            body = {};
        }
        setStatus('watch-status', action.toUpperCase() + '...');
        return api(url, { method: 'POST', body: body }).then(function () {
            notify('OK // ' + action, 'ok');
            return loadWatch(true);
        }).catch(function (error) {
            setStatus('watch-status', 'ERROR // ' + error.message);
            notify('常驻监控操作失败: ' + error.message, 'warn');
        });
    }

    function markAlertRead(alertId) {
        var id = parseInt(alertId, 10);
        if (!id) { notify('无效预警 ID', 'warn'); return Promise.resolve(); }
        return api('/comment/alerts/' + id + '/read', { method: 'PUT', body: {} }).then(function () {
            var alerts = (state.watch && state.watch.alerts && state.watch.alerts.alerts) || [];
            alerts.forEach(function (alert) { if (Number(alert.id) === id) alert.is_read = true; });
            renderWatch();
            notify('预警已标记为已读', 'ok');
        }).catch(function (error) {
            notify('标记失败: ' + error.message, 'warn');
        });
    }

    function addKeyword() {
        var raw = ((document.getElementById('watch-keyword') || {}).value || '').trim();
        var words = raw.split(/[,，;；\s]+/).map(function (word) { return word.trim(); }).filter(Boolean);
        if (!words.length) { notify('输入至少一个关键词', 'warn'); return Promise.resolve(); }
        setStatus('watch-tools-status', 'KEYWORD · 写入中...');
        return api('/comment/keywords', { method: 'POST', body: { keywords: words } }).then(function (payload) {
            var added = (payload && payload.keywords) || words;
            setStatus('watch-tools-status', 'KEYWORD · 已添加 ' + added.length + ' 个');
            var input = document.getElementById('watch-keyword');
            if (input) input.value = '';
            notify('OK // 已添加预警关键词', 'ok');
        }).catch(function (error) {
            setStatus('watch-tools-status', 'ERROR // ' + error.message);
            notify('关键词添加失败: ' + error.message, 'warn');
        });
    }

    function renderWatchAccount() {
        var account = state.watch && state.watch.account;
        var root = document.getElementById('watch-account-result');
        if (!root) return;
        if (!account) {
            root.innerHTML = '';
            return;
        }
        var videos = account.videos || [];
        root.innerHTML = '<div class="live-code-line"><b>' + esc(account.username || ('UID ' + (account.uid || '—'))) +
            '</b><br><span class="live-note">videos ' + fmtMaybe(account.monitored_videos) +
            ' · alerts ' + fmtMaybe(account.total_alerts) + ' · sentiment ' + esc(account.overall_sentiment || '—') +
            '</span></div>' + (videos.length ? videos.map(function (video) {
                var alerts = video.alerts || [];
                return '<div class="live-user"><div class="live-user-head"><b>' +
                    esc(short(video.title || video.bvid || '—', 48)) + '</b><span>' +
                    esc(video.status || '—') + '</span></div><div class="live-user-meta">' +
                    'collected ' + fmtMaybe(video.collected_count) + ' · processed ' +
                    fmtMaybe(video.processed_count) + ' · alerts ' + alerts.length +
                    '</div></div>';
            }).join('') : emptyBox('账号下暂无视频监控结果'));
    }

    function monitorAccount() {
        if (!state.watch) return Promise.resolve();
        var uid = ((document.getElementById('watch-account-uid') || {}).value || '').trim();
        var limit = parseInt((document.getElementById('watch-account-limit') || {}).value, 10) || 10;
        var strategy = (document.getElementById('watch-account-strategy') || {}).value || 'normal';
        if (!/^\d+$/.test(uid)) { notify('输入有效的 UP UID', 'warn'); return Promise.resolve(); }
        setStatus('watch-tools-status', 'ACCOUNT · 监控 ' + uid + ' · ' + limit + ' 个视频...');
        setHTML('watch-account-result', '<p class="live-loading">MONITORING ACCOUNT ' + esc(uid) + '...</p>');
        return api('/comment/monitor/account', {
            method: 'POST', body: { uid: parseInt(uid, 10), video_limit: limit, strategy: strategy }
        }).then(function (payload) {
            state.watch.account = payload.data || null;
            setStatus('watch-tools-status', 'ACCOUNT · 完成');
            renderWatchAccount();
            notify('OK // 账号监控完成', 'ok');
        }).catch(function (error) {
            setStatus('watch-tools-status', 'ERROR // ' + error.message);
            setHTML('watch-account-result', errorBox(error.message));
            notify('账号监控失败: ' + error.message, 'warn');
        });
    }

    function stopWatchProgress() {
        if (state.watchProgressTimer) {
            window.clearInterval(state.watchProgressTimer);
            state.watchProgressTimer = null;
        }
        state.watchProgressBvid = '';
    }

    function startWatchProgress(bvid) {
        stopWatchProgress();
        state.watchProgressBvid = bvid;
        function tick() {
            if (state.watchProgressBvid !== bvid) return;
            api('/comment/monitor/progress?bvid=' + encodeURIComponent(bvid), { method: 'GET' })
                .then(function (payload) {
                    if (state.watchProgressBvid !== bvid) return;
                    var data = payload.data || {};
                    var text = [data.phase || 'collecting', fmt(data.collected) + (data.limit ? ' / ' + fmt(data.limit) : '')]
                        .filter(Boolean).join(' · ');
                    setStatus('watch-status', 'MONITORING · ' + text);
                }).catch(function () { /* 进度是辅助信息，失败不阻断主请求 */ });
        }
        tick();
        state.watchProgressTimer = window.setInterval(tick, 1500);
    }

    function watchAction(action, element) {
        if (action === 'watch_refresh') return loadWatch(true);
        if (action === 'watch_export') return exportWatchCsv();
        if (action === 'watch_collect') return runWatchCollect('collect');
        if (action === 'watch_monitor') return runWatchCollect('monitor');
        if (action === 'watch_keyword') return addKeyword();
        if (action === 'watch_account') return monitorAccount();
        if (action === 'resident_enable' || action === 'resident_pause' || action === 'resident_stop') {
            return residentAction(action);
        }
        return Promise.resolve();
    }

    function runWatchCollect(kind) {
        var bvid = ((document.getElementById('watch-bvid') || {}).value || '').trim();
        if (!bvid) {
            notify('请输入 BV 号', 'warn');
            return Promise.resolve();
        }
        var strategy = ((document.getElementById('watch-strategy') || {}).value || 'normal');
        setStatus('watch-status', (kind === 'monitor' ? 'MONITORING' : 'COLLECTING') + ' · ' + bvid);
        startWatchProgress(bvid);
        var endpoint = kind === 'monitor' ? '/comment/monitor' : '/comment/collect';
        var body = kind === 'monitor'
            ? { bvid: bvid, enable_dedup: true, enable_sentiment: true, strategy: strategy }
            : { bvid: bvid, strategy: strategy, max_count: 100 };
        return api(endpoint, { method: 'POST', body: body }).then(function (result) {
            stopWatchProgress();
            if (kind === 'monitor' && result.data) {
                state.watch = state.watch || {};
                state.watch.dashboard = { data: { video: { bvid: bvid, title: bvid }, visualization: result.data.visualization || {} } };
                renderWatch();
            }
            setStatus('watch-status', 'OK · ' + kind + ' · ' + bvid);
            notify('OK // ' + (kind === 'monitor' ? '监控完成' : '采集完成') + ' · ' + bvid, 'ok');
            return loadWatch(true);
        }).catch(function (error) {
            stopWatchProgress();
            setStatus('watch-status', 'ERROR // ' + error.message);
            notify('操作失败: ' + error.message, 'warn');
        });
    }

    function exportWatchCsv() {
        var data = state.watch || {};
        var dash = (data.dashboard && data.dashboard.data) || {};
        var visual = dash.visualization || {};
        var rows = [['uname', 'content', 'like', 'sentiment']];
        (visual.top10_voice_comments || []).forEach(function (item) {
            rows.push([item.uname || '', item.content || '', item.like || 0, item.sentiment || '']);
        });
        if (rows.length === 1) {
            notify('暂无评论可导出', 'warn');
            return Promise.resolve();
        }
        var csv = rows.map(function (row) {
            return row.map(function (cell) {
                return '"' + String(cell).replace(/"/g, '""') + '"';
            }).join(',');
        }).join('\n');
        var blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'fishtool-comments-' + new Date().toISOString().slice(0, 10) + '.csv';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        notify('OK // 评论 CSV 已导出', 'ok');
        return Promise.resolve();
    }

    /* ------------------------------------------------------------------ */
    /* 评论语义折叠                                                        */
    /* ------------------------------------------------------------------ */
    var SEMANTIC_CONSENT = 'semantic-fold-v1';

    function semanticStatus(text) {
        var el = document.getElementById('semantic-status');
        if (el) el.textContent = text;
    }

    function semanticLocalHeaders() {
        return writeHeaders('/comment/semantic/local-token');
    }

    function semanticWrite(path, method, body) {
        return semanticLocalHeaders().then(function (headers) {
            return api(path, { method: method, headers: headers, body: body || {} });
        });
    }

    function semanticPreflight(bvid) {
        var target = bvid || ((document.getElementById('semantic-bvid') || {}).value || '').trim();
        if (!target) { notify('先输入语义折叠的 BV 号', 'warn'); return Promise.resolve(null); }
        state.semantic.bvid = target;
        semanticStatus('PREFLIGHT · ' + target + ' ...');
        return Promise.all([
            api('/comment/semantic/preflight?bvid=' + encodeURIComponent(target) + '&limit=200'),
            api('/comment/semantic/preferences?bvid=' + encodeURIComponent(target)),
            settle(api('/comment/semantic/latest?bvid=' + encodeURIComponent(target)))
        ]).then(function (results) {
            state.semantic.preflight = results[0].data || null;
            state.semantic.preference = results[1].data || null;
            var latest = results[2].ok ? (results[2].value.data || null) : null;
            state.semantic.task = latest || null;
            state.semantic.result = null;
            state.semantic.page = 1;
            if (latest && (latest.status === 'completed' || latest.status === 'partial')) {
                return loadSemanticResult(latest.task_id, 1);
            }
            renderSemantic();
            semanticStatus(latest ? ('TASK · ' + (latest.status || 'unknown')) : 'READY · 已读取预检，勾选同意后可开始');
            return null;
        }).catch(function (error) {
            semanticStatus('ERROR // ' + error.message);
            notify('语义预检失败: ' + error.message, 'warn');
            return null;
        });
    }

    function renderSemantic() {
        var pf = state.semantic.preflight;
        var result = state.semantic.result;
        var area = document.getElementById('semantic-result');
        if (!area) return;
        if (!pf) {
            area.innerHTML = emptyBox('先 PREFLIGHT 读取原始评论范围与模型状态');
            return;
        }
        var counts = pf.counts || {};
        var identity = pf.provider_identity || {};
        var head = '<div class="live-code-line"><b>' +
            (pf.model_configured === false ? '模型未配置' : esc(identity.model || '模型')) + '</b><br>' +
            '<span class="live-note">' + esc(identity.api_base || '—') +
            ' · 原始 ' + fmtMaybe(counts.raw_total) + ' · 范围 ' + fmtMaybe(counts.selected) +
            ' · 可参与 ' + fmtMaybe(counts.eligible) + '</span></div>';
        if (state.semantic.task && !result) {
            head += '<div class="live-loading">TASK ' + esc(state.semantic.task.status || '') + ' · ' +
                esc((state.semantic.task.phase || '')) + ' · ' + fmtMaybe(state.semantic.task.progress) + '%</div>';
        }
        area.innerHTML = head + (result ? semanticResultHtml(result) : emptyBox('尚未生成语义折叠结果'));
    }

    function semanticResultHtml(result) {
        var groups = result.groups || [];
        var items = result.items || [];
        var autoFold = !!(document.getElementById('semantic-auto-fold') || {}).checked;
        var html = '<div class="live-note">分组 ' + groups.length + ' · 本页原文 ' + items.length +
            ' · revision ' + esc(result.revision) + '</div>';
        if (groups.length) {
            html += groups.map(function (group) {
                var members = group.members || [];
                var rep = group.representative_content ||
                    ((members.filter(function (m) { return m.is_representative; })[0] || members[0] || {}).content);
                var gid = group.group_id || '';
                return '<div class="live-user"><div class="live-user-head"><b>' +
                    fmtMaybe(group.member_count !== undefined ? group.member_count : members.length) +
                    ' 条等价</b><span>' + esc(group.reason || '') + '</span></div>' +
                    '<div class="live-user-text">代表：' + esc(short(rep || '—', 220)) + '</div>' +
                    '<details><summary>全部成员（' + members.length + '）</summary>' +
                    members.map(function (member) {
                        var rpid = String(member.rpid || '');
                        return '<div class="live-user-text">' + esc(short(member.content || '', 220)) +
                            (member.keep_separate ? ' [独立]' : '') +
                            (member.stale ? ' [内容变化]' : '') +
                            (member.is_representative ? ' [代表]' : '') +
                            ' <button class="live-link" type="button" data-live-act="semantic_decide" data-action="' +
                            (member.keep_separate ? 'allow_member' : 'split_member') + '" data-rpid="' +
                            esc(rpid) + '">' + (member.keep_separate ? 'ALLOW' : 'SPLIT') + '</button></div>';
                    }).join('') + '</details>' +
                    '<div class="live-actions">' +
                    '<button class="live-link" type="button" data-live-act="semantic_decide" data-action="undo_group" data-group="' +
                    esc(gid) + '">UNDO FOLD</button>' +
                    '<button class="live-link" type="button" data-live-act="semantic_decide" data-action="allow_group" data-group="' +
                    esc(gid) + '">ALLOW FOLD</button></div></div>';
            }).join('');
        } else {
            html += emptyBox('没有等价分组');
        }
        if (items.length) {
            html += '<div class="live-code-line"><b>本页原文</b></div>' + items.map(function (item) {
                return '<div class="live-user"><div class="live-user-head"><b>' +
                    esc(item.uname || ('UID ' + (item.uid || '—'))) + '</b><span>' +
                    esc(item.sentiment || '') + (item.keep_separate ? ' · 独立' : '') + '</span></div>' +
                    '<div class="live-user-text">' + esc(short(item.content || '', 260)) + '</div></div>';
            }).join('');
        }
        var pagination = result.pagination || {};
        if (pagination.total_pages > 1) {
            html += '<div class="live-actions">' +
                '<button class="btn" type="button" data-live-act="semantic_page" data-page="' +
                Math.max(1, state.semantic.page - 1) + '">PREV</button>' +
                '<span class="live-note">' + state.semantic.page + ' / ' + pagination.total_pages + '</span>' +
                '<button class="btn" type="button" data-live-act="semantic_page" data-page="' +
                Math.min(pagination.total_pages, state.semantic.page + 1) + '">NEXT</button></div>';
        }
        return html;
    }

    function loadSemanticResult(taskId, page) {
        state.semantic.page = page || 1;
        return api('/comment/semantic/tasks/' + encodeURIComponent(taskId) + '/result?page=' +
            state.semantic.page + '&page_size=50').then(function (payload) {
            state.semantic.result = payload.data || null;
            renderSemantic();
            return state.semantic.result;
        }).catch(function (error) {
            semanticStatus('ERROR // ' + error.message);
            return null;
        });
    }

    function semanticStart() {
        var bvid = ((document.getElementById('semantic-bvid') || {}).value || '').trim();
        if (!bvid) { notify('输入语义折叠 BV 号', 'warn'); return Promise.resolve(); }
        if (!(document.getElementById('semantic-consent') || {}).checked) {
            notify('请先勾选实验性功能与费用说明', 'warn');
            return Promise.resolve();
        }
        var budgetMode = (document.getElementById('semantic-budget') || {}).value || 'default';
        if (budgetMode === 'unlimited' && !(document.getElementById('semantic-unlimited-confirm') || {}).checked) {
            notify('UNLIMITED 预算需要勾选确认', 'warn');
            return Promise.resolve();
        }
        var ready = state.semantic.preflight && state.semantic.bvid === bvid
            ? Promise.resolve(true)
            : semanticPreflight(bvid).then(function () {
                return !!(state.semantic.preflight && state.semantic.preflight.model_configured !== false);
            });
        return ready.then(function (ok) {
            if (!ok) { notify('模型未配置或预检失败', 'warn'); return null; }
            var pf = state.semantic.preflight;
            var pref = state.semantic.preference || {};
            var ensureConsent = pf.consent_needed
                ? semanticWrite('/comment/semantic/preferences', 'PUT', {
                    bvid: bvid,
                    expected_revision: Number(pref.revision) || 0,
                    auto_fold: !!(document.getElementById('semantic-auto-fold') || {}).checked,
                    consent_version: SEMANTIC_CONSENT,
                    model_identity: pf.provider_identity || null
                })
                : Promise.resolve();
            return ensureConsent.then(function (consentPayload) {
                if (consentPayload && consentPayload.data) {
                    state.semantic.preference = consentPayload.data;
                    if (state.semantic.preflight) state.semantic.preflight.consent_needed = false;
                }
                return semanticLocalHeaders();
            }).then(function (headers) {
                var requestKey = 'semantic-' + Date.now() + '-' + Math.random().toString(16).slice(2);
                return api('/comment/semantic/tasks', {
                    method: 'POST', headers: headers,
                    body: {
                        bvid: bvid, limit: 200, consent_version: SEMANTIC_CONSENT, request_key: requestKey,
                        budget_mode: budgetMode
                    }
                });
            }).then(function (payload) {
                state.semantic.task = payload.data || null;
                state.semantic.result = null;
                semanticStatus('RUNNING · ' + bvid + ' · 大模型分组中（全局串行）');
                renderSemantic();
                return pollSemantic(payload.data && payload.data.task_id);
            }).catch(function (error) {
                semanticStatus('ERROR // ' + error.message);
                notify('语义折叠失败: ' + error.message, 'warn');
            });
        });
    }

    function pollSemantic(taskId) {
        if (!taskId) return Promise.resolve();
        return api('/comment/semantic/tasks/' + encodeURIComponent(taskId)).then(function (payload) {
            var task = payload.data || {};
            state.semantic.task = task;
            semanticStatus((task.status || 'running') + ' · ' + (task.phase || '') + ' · ' +
                fmtMaybe(task.progress) + '%' + (task.message ? ' · ' + task.message : ''));
            if (task.status === 'completed' || task.status === 'partial') {
                return loadSemanticResult(taskId, 1).then(function () {
                    semanticStatus('DONE · ' + (task.status || 'completed'));
                    return null;
                });
            }
            if (task.status === 'failed' || task.status === 'cancelled' || task.status === 'interrupted') {
                renderSemantic();
                return null;
            }
            return sleep(2000).then(function () { return pollSemantic(taskId); });
        }).catch(function (error) {
            semanticStatus('ERROR // ' + error.message);
            return null;
        });
    }

    function semanticCancel() {
        var task = state.semantic.task;
        if (!task || !task.task_id) { notify('没有可取消的语义任务', 'warn'); return Promise.resolve(); }
        return semanticWrite('/comment/semantic/tasks/' + encodeURIComponent(task.task_id) + '/cancel',
            'POST', { expected_revision: Number(task.revision) || 0 }).then(function () {
            semanticStatus('CANCELLED · ' + task.task_id);
            notify('已请求取消语义任务', 'warn');
        }).catch(function (error) {
            notify('取消失败: ' + error.message, 'warn');
        });
    }

    function semanticResume() {
        var task = state.semantic.task;
        if (!task || !task.task_id) { notify('没有可继续的语义任务', 'warn'); return Promise.resolve(); }
        return semanticWrite('/comment/semantic/tasks/' + encodeURIComponent(task.task_id) + '/resume',
            'POST', { expected_revision: Number(task.revision) || 0, acknowledge_unknown: true }).then(function (payload) {
            state.semantic.task = payload.data || task;
            return pollSemantic(task.task_id);
        }).catch(function (error) {
            notify('继续失败: ' + error.message, 'warn');
        });
    }

    function semanticDecide(action, groupId, rpid) {
        var result = state.semantic.result;
        var task = state.semantic.task;
        var bvid = state.semantic.bvid;
        if (!result || !task || !bvid) return Promise.resolve();
        var requestKey = 'semantic-decision-' + Date.now() + '-' + Math.random().toString(16).slice(2);
        var body = {
            bvid: bvid,
            task_id: task.task_id,
            action: action,
            expected_revision: Number(result.revision) || 0,
            request_key: requestKey
        };
        if (groupId) body.group_id = groupId;
        if (rpid) body.rpid = rpid;
        return semanticWrite('/comment/semantic/decisions', 'POST', body).then(function () {
            notify('语义决策已保存', 'ok');
            return loadSemanticResult(task.task_id, state.semantic.page);
        }).catch(function (error) {
            notify('语义决策失败: ' + error.message, 'warn');
        });
    }

    /* ------------------------------------------------------------------ */
    /* 配置                                                                */
    /* ------------------------------------------------------------------ */
    function loadConfig(force) {
        if (state.loading.config) return Promise.resolve();
        if (!force && state.config) return Promise.resolve();
        state.loading.config = true;
        if (!document.getElementById('config-form')) renderConfigShell();
        return Promise.all([
            settle(api('/config/llm')),
            settle(api('/config/')),
            settle(api('/config/llm/usage?days=7'))
        ]).then(function (results) {
            state.config = {
                llm: results[0].ok ? results[0].value : null,
                all: results[1].ok ? results[1].value : null,
                usage: results[2].ok ? results[2].value : null
            };
            applyConfig();
        }).catch(function (error) {
            setHTML('config', errorBox(error.message));
        }).then(function () {
            state.loading.config = false;
        });
    }

    function renderConfigShell() {
        var html = '' +
            '<p class="page-sub">真实读取 /api/config/llm 与 /api/config/。密钥输入框留空表示沿用已保存密钥。</p>' +
            '<div class="live-actions">' +
            '<button class="btn btn-primary" type="button" data-live-act="config_save" data-label="保存配置">SAVE</button>' +
            '<button class="btn" type="button" data-live-act="config_test" data-label="测试模型">TEST / MODELS</button>' +
            '<button class="btn" type="button" data-live-act="config_reload" data-label="重新加载配置">RELOAD</button>' +
            '<span class="hint" id="config-status">LIVE · /api/config</span>' +
            '</div>' +
            '<div class="split">' +
            pane('MODEL', '大模型参数', 'OPENAI COMPATIBLE',
                '<form id="config-form" data-live-form="config">' +
                configField('provider', 'PROVIDER', 'text') +
                configField('base_url', 'BASE URL', 'text') +
                configField('model', 'MODEL', 'text') +
                configField('api_key', 'API KEY', 'password') +
                configField('temperature', 'TEMPERATURE', 'number') +
                configField('max_tokens', 'MAX TOKENS', 'number') +
                configField('daily_limit', 'DAILY LIMIT', 'number') +
                '</form>') +
            pane('RUNTIME', '运行开关', 'PUT /config/', '<div id="config-toggles">' + emptyBox('加载中') + '</div>') +
            '</div>' +
            pane('USAGE', 'Token 用量', '7 DAYS', '<div id="config-usage">' + emptyBox('加载中') + '</div>') +
            pane('MODELS', '可用模型', 'POST /llm/models', '<div id="config-models">' + emptyBox('点击 TEST / MODELS 获取') + '</div>') +
            pane('ADVANCED', '任意配置键', 'PUT /config/',
                '<div class="live-actions"><input class="live-input" id="cfg-key" placeholder="key，例如 monitor.enable" aria-label="配置键">' +
                '<input class="live-input live-input-wide" id="cfg-value" placeholder="value（JSON 或纯文本）" aria-label="配置值">' +
                '<button class="btn" type="button" data-live-act="config_key">WRITE KEY</button></div>' +
                '<p class="live-note">按键写回并热重载；llm 地址/密钥与 secrets.* 仍由后端拒绝。</p>');
        setHTML('config', html);
        setMeta('config', 'LIVE · /api/config');
    }

    function configField(name, label, type) {
        var autocomplete = name === 'api_key' ? ' autocomplete="new-password"' : ' autocomplete="off"';
        return '<label class="field live-field"><span class="field-label">' + esc(label) + '</span>' +
            '<input id="cfg-' + name + '" type="' + esc(type) + '"' + autocomplete +
            (type === 'number' ? ' step="any"' : '') + ' aria-label="' + esc(label) + '"></label>';
    }

    function applyConfig() {
        var data = state.config || {};
        var llm = (data.llm && data.llm.config) || {};
        var all = (data.all && data.all.config) || {};
        var usage = data.usage || {};
        var fields = {
            provider: llm.provider || 'openai',
            base_url: llm.api_base || '',
            model: llm.model || '',
            api_key: '',
            temperature: llm.temperature,
            max_tokens: llm.max_tokens,
            daily_limit: llm.daily_token_limit
        };
        Object.keys(fields).forEach(function (key) {
            var input = document.getElementById('cfg-' + key);
            if (!input) return;
            input.value = fields[key] === undefined || fields[key] === null ? '' : String(fields[key]);
            if (key === 'api_key') input.placeholder = llm.api_key_masked ? ('已保存 ' + llm.api_key_masked) : '未保存';
        });
        var toggles = [
            ['deduplication.enable_fuzzy_match', '自动去重 (模糊匹配)', 'deduplication.enable_fuzzy_match'],
            ['monitor.alert_thresholds.keyword_match', '风险词预警', 'monitor.alert_thresholds.keyword_match'],
            ['monitor.enable', '常驻评论监控', 'monitor.enable']
        ];
        setHTML('config-toggles', toggles.map(function (row) {
            var parts = row[0].split('.');
            var value = all;
            parts.forEach(function (part) { value = value && typeof value === 'object' ? value[part] : undefined; });
            var on = !!value;
            return '<button class="toggle" type="button" data-live-toggle="' + esc(row[0]) + '" aria-pressed="' + (on ? 'true' : 'false') + '" data-label="' + esc(row[1]) + '">' +
                '<span class="tbox">' + (on ? '[x]' : '[ ]') + '</span><span class="tk">' + esc(row[1]) + '</span>' +
                '<span class="tv">' + (on ? 'ON' : 'OFF') + '</span></button>';
        }).join(''));
        var today = usage.today || {};
        var limit = num(today.limit) || num(llm.daily_token_limit);
        var used = num(today.total_tokens);
        var daily = usage.daily_usage || [];
        setHTML('config-usage', '<div class="bar-row"><span class="bar-label">TODAY</span>' +
            bar(used, limit || Math.max(used, 1)) +
            '<span class="bar-val">' + (limit ? fmt(used) + ' / ' + fmt(limit) : fmt(used) + ' / ∞') + '</span></div>' +
            (daily.length ? '<table class="tbl"><thead><tr><th>DATE</th><th>TOKENS</th><th>REQUESTS</th></tr></thead><tbody>' +
                daily.map(function (day) {
                    return '<tr><td>' + esc(day.date) + '</td><td>' + fmt(day.total_tokens) + '</td><td>' + fmt(day.request_count) + '</td></tr>';
                }).join('') + '</tbody></table>' : emptyBox('最近 7 天没有模型调用')));
        setMeta('config', 'LIVE · ' + (llm.model || '未配置模型'));
    }

    function collectConfigPayload() {
        function value(id) {
            var input = document.getElementById('cfg-' + id);
            return input ? input.value.trim() : '';
        }
        return {
            provider: value('provider'),
            base_url: value('base_url'),
            model: value('model'),
            api_key: value('api_key'),
            temperature: value('temperature'),
            max_tokens: value('max_tokens'),
            daily_limit: value('daily_limit')
        };
    }

    function putConfig(key, value) {
        return api('/config/', { method: 'PUT', body: { key: key, value: value } });
    }

    function saveConfig() {
        var payload = collectConfigPayload();
        if (!payload.model) {
            notify('Model 不能为空', 'warn');
            return Promise.resolve();
        }
        setStatus('config-status', 'SAVING...');
        var work;
        if (payload.api_key) {
            var body = {
                api_key: payload.api_key,
                base_url: payload.base_url,
                model: payload.model
            };
            if (payload.daily_limit !== '' && Number.isInteger(Number(payload.daily_limit))) {
                body.daily_token_limit = Number(payload.daily_limit);
            }
            work = api('/config/llm', { method: 'POST', body: body });
        } else {
            work = Promise.resolve();
        }
        var extra = [
            ['llm.api_base', payload.base_url],
            ['llm.model', payload.model],
            ['llm.provider', payload.provider],
            ['llm.temperature', Number(payload.temperature)],
            ['llm.max_tokens', Number(payload.max_tokens)],
            ['llm.daily_token_limit', Number(payload.daily_limit)]
        ].filter(function (pair) {
            return pair[1] !== '' && pair[1] !== null && !Number.isNaN(pair[1]);
        });
        extra.forEach(function (pair) {
            work = work.then(function () { return putConfig(pair[0], pair[1]); });
        });
        return work.then(function () {
            notify('OK // 配置已保存', 'ok');
            setStatus('config-status', 'SAVED');
            state.config = null;
            return loadConfig(true);
        }).catch(function (error) {
            setStatus('config-status', 'ERROR // ' + error.message);
            notify('保存失败: ' + error.message, 'warn');
        });
    }

    function reloadConfig() {
        setStatus('config-status', 'RELOAD · 读取磁盘配置...');
        return api('/config/reload', { method: 'POST', body: {} }).then(function () {
            notify('配置已重新加载', 'ok');
            return loadConfig(true);
        }).catch(function (error) {
            setStatus('config-status', 'ERROR // ' + error.message);
            notify('重载失败: ' + error.message, 'warn');
        });
    }

    function saveConfigKey() {
        var key = ((document.getElementById('cfg-key') || {}).value || '').trim();
        var raw = ((document.getElementById('cfg-value') || {}).value || '').trim();
        if (!key) { notify('输入配置键', 'warn'); return Promise.resolve(); }
        var value = raw;
        if (/^[\[{]/.test(raw) || /^(true|false|null|-?\d+(\.\d+)?)$/.test(raw)) {
            try { value = JSON.parse(raw); } catch (error) { notify('JSON 值无效', 'warn'); return Promise.resolve(); }
        }
        return api('/config/', { method: 'PUT', body: { key: key, value: value } })
            .then(function () {
                notify('配置键已写入: ' + key, 'ok');
                return loadConfig(true);
            }).catch(function (error) {
                notify('写入失败: ' + error.message, 'warn');
            });
    }

    function testModels() {
        var payload = collectConfigPayload();
        var body = { base_url: payload.base_url };
        if (payload.api_key) body.api_key = payload.api_key;
        setStatus('config-status', 'TESTING...');
        return api('/config/llm/models', { method: 'POST', body: body }).then(function (result) {
            if (!result.success || !(result.models || []).length) {
                setHTML('config-models', emptyBox(result.message || '没有获取到模型'));
                setStatus('config-status', 'FAILED // ' + (result.message || ''));
                return;
            }
            setHTML('config-models', '<div class="tagline">' + result.models.map(function (model) {
                return '<span class="tag">' + esc(model) + '</span>';
            }).join('') + '</div>');
            setStatus('config-status', 'MODELS OK · ' + result.models.length);
            notify('OK // 模型列表已更新', 'ok');
        }).catch(function (error) {
            setStatus('config-status', 'ERROR // ' + error.message);
            notify('模型测试失败: ' + error.message, 'warn');
        });
    }

    function toggleConfig(button) {
        var key = button.getAttribute('data-live-toggle');
        var on = button.getAttribute('aria-pressed') !== 'true';
        var box = button.querySelector('.tbox');
        var tv = button.querySelector('.tv');
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (box) box.textContent = on ? '[x]' : '[ ]';
        if (tv) tv.textContent = on ? 'ON' : 'OFF';
        setStatus('config-status', 'SAVING ' + key + '...');
        return putConfig(key, on).then(function () {
            setStatus('config-status', 'OK · ' + key + ' ' + (on ? 'ON' : 'OFF'));
            notify('OK // ' + key + ' ' + (on ? 'ON' : 'OFF'), 'ok');
        }).catch(function (error) {
            button.setAttribute('aria-pressed', on ? 'false' : 'true');
            if (box) box.textContent = on ? '[ ]' : '[x]';
            if (tv) tv.textContent = on ? 'OFF' : 'ON';
            setStatus('config-status', 'ERROR // ' + error.message);
            notify('开关保存失败: ' + error.message, 'warn');
        });
    }

    /* ------------------------------------------------------------------ */
    /* 日志                                                                */
    /* ------------------------------------------------------------------ */
    function exportLogs() {
        var levels = { WARNING: 1, ERROR: 1, CRITICAL: 1 };
        var rows = (state.logs || []).filter(function (record) {
            return levels[String(record.level || '').toUpperCase()];
        });
        var text = rows.length ? rows.map(function (record) {
            return '[' + (record.timestamp || '') + '] [' + (record.level || '') + '] [' +
                (record.source || '') + '] ' + (record.message || '');
        }).join('\n') : '暂无 WARNING / ERROR 日志';
        var blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        var link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = 'bili_ops_errors_' + new Date().toISOString().slice(0, 19).replace(/:/g, '-') + '.txt';
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
        notify('问题日志已导出 · ' + rows.length + ' 条', 'ok');
    }

    function loadLogs(force) {
        if (state.loading.logs) return Promise.resolve();
        var list = document.getElementById('log-list');
        if (!state.logs.length && list) {
            list.innerHTML = '<p class="live-loading">LOADING LOGS...</p>';
        }
        state.loading.logs = true;
        return api('/logs/?log_type=all&levels=DEBUG,INFO,WARNING,ERROR&limit=200').then(function (result) {
            state.logs = result.records || [];
            state.logsAt = Date.now();
            renderLogs();
        }).catch(function (error) {
            if (list) list.innerHTML = '<div class="live-error">ERROR // ' + esc(error.message) + '</div>';
            setMeta('logs', 'ERROR · LOGS');
        }).then(function () {
            state.loading.logs = false;
        });
    }

    function renderLogs() {
        var list = document.getElementById('log-list');
        if (!list) return;
        var records = state.logs || [];
        list.innerHTML = records.length ? records.map(function (record) {
            var level = levelKey(record.level);
            return '<div class="log-line" data-level="' + level + '">' +
                '<span class="log-t">' + esc(hm(record.timestamp)) + '</span>' +
                '<span class="log-lv ' + level + '">' + esc(levelLabel(record.level)) + '</span>' +
                '<span class="log-mod">' + esc(short(record.source || 'app', 18)) + '</span>' +
                '<span class="log-msg">' + esc(record.message || '') + '</span></div>';
        }).join('') : '<p class="live-empty">EMPTY // 暂无日志</p>';
        var total = document.getElementById('log-total');
        if (total) total.textContent = String(records.length);
        var paneMeta = document.querySelector('#page-logs .pane-meta');
        if (paneMeta) paneMeta.textContent = records.length + ' LINES · LIVE';
        var foot = document.querySelector('#page-logs .pane-foot-r');
        if (foot) foot.textContent = 'AUTO REFRESH 5s · LIVE';
        var sub = document.querySelector('#page-logs .page-sub');
        if (sub) sub.textContent = '应用、爬虫与风控日志。真实读取 /api/logs，级别过滤与关键字检索在前端即时生效。';
        setMeta('logs', 'LIVE · ' + records.length + ' RECORDS');
        if (window.FishTui && window.FishTui.applyLogFilter) {
            window.FishTui.applyLogFilter();
        }
    }

    function refreshCurrent() {
        var page = window.FishTui ? window.FishTui.currentPage() : 'welcome';
        if (page === 'welcome') return loadWelcome(true);
        if (page === 'hotspot') return loadHotspot(true);
        if (page === 'watch') return loadWatch(true);
        if (page === 'config') return loadConfig(true);
        if (page === 'logs') return loadLogs(true);
        if (page === 'bench') return loadBench(true);
        if (page === 'diag') return loadDiag(true);
        if (page === 'draw') return loadLottery(true);
        if (page === 'patrol') { loadPatrolPage(); return Promise.resolve(); }
        return Promise.resolve();
    }

    /* ------------------------------------------------------------------ */
    /* 扫码登录                                                            */
    /* ------------------------------------------------------------------ */
    function logout() {
        return api('/auth/logout', { method: 'POST', body: {} }).then(function () {
            state.welcome = null;
            state.watch = null;
            state.watchAt = 0;
            notify('B站登录态已退出', 'ok');
            return loadWelcome(true);
        }).catch(function (error) {
            notify('退出失败: ' + error.message, 'warn');
        });
    }

    function openLogin() {
        var mask = document.getElementById('login-mask');
        if (!mask) return;
        state.loginReturn = document.activeElement;
        state.loginOpen = true;
        mask.hidden = false;
        /* 打开后先聚焦真正的操作按钮；Esc/关闭按钮仍在焦点环里。 */
        var refresh = document.getElementById('login-refresh');
        if (refresh) refresh.focus();
        else {
            var close = document.getElementById('login-close');
            if (close) close.focus();
        }
        startLoginQr();
    }

    function closeLogin() {
        var mask = document.getElementById('login-mask');
        if (!mask) return;
        state.loginOpen = false;
        mask.hidden = true;
        if (state.loginTimer) {
            window.clearInterval(state.loginTimer);
            state.loginTimer = null;
        }
        var status = document.getElementById('login-status');
        if (status) status.textContent = '已关闭';
        if (state.loginReturn && state.loginReturn.focus) {
            state.loginReturn.focus();
        } else if (window.FishTui) {
            var tab = document.getElementById('tab-welcome');
            if (tab) tab.focus();
        }
    }

    function startLoginQr() {
        var img = document.getElementById('login-qr');
        var status = document.getElementById('login-status');
        if (state.loginTimer) {
            window.clearInterval(state.loginTimer);
            state.loginTimer = null;
        }
        if (status) status.textContent = '正在获取 B站二维码...';
        api('/auth/qrcode').then(function (result) {
            if (!result || !result.qrcode_base64) throw new Error('二维码为空');
            state.loginKey = result.qrcode_key;
            if (img) img.src = 'data:image/png;base64,' + result.qrcode_base64;
            if (status) status.textContent = '等待扫码...';
            state.loginTimer = window.setInterval(pollLogin, 2000);
        }).catch(function (error) {
            if (status) status.textContent = '二维码获取失败: ' + error.message;
        });
    }

    function pollLogin() {
        if (!state.loginOpen || !state.loginKey) return;
        api('/auth/poll', { method: 'POST', body: { qrcode_key: state.loginKey } }).then(function (result) {
            var status = document.getElementById('login-status');
            var text = result.status || '';
            var message = result.message || text;
            if (status) status.textContent = message;
            if (text === 'confirmed') {
                if (status) status.textContent = '登录成功，正在刷新首页...';
                window.setTimeout(function () {
                    closeLogin();
                    loadWelcome(true);
                    notify('OK // B站登录成功', 'ok');
                }, 500);
            } else if (text === 'expired' || text === 'error') {
                if (state.loginTimer) {
                    window.clearInterval(state.loginTimer);
                    state.loginTimer = null;
                }
            }
        }).catch(function (error) {
            var status = document.getElementById('login-status');
            if (status) status.textContent = '轮询失败: ' + error.message;
        });
    }

    function onLoginKey(event) {
        if (!state.loginOpen) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            closeLogin();
            return;
        }
        if (event.key === 'Tab') {
            var items = Array.prototype.slice.call(
                document.querySelectorAll('#login-mask button:not([disabled])')
            ).filter(function (el) { return el.getClientRects().length > 0; });
            if (items.length) {
                event.preventDefault();
                event.stopPropagation();
                var idx = items.indexOf(document.activeElement);
                var next = event.shiftKey
                    ? (idx <= 0 ? items.length - 1 : idx - 1)
                    : (idx === -1 || idx >= items.length - 1 ? 0 : idx + 1);
                items[next].focus();
            }
            return;
        }
        if (event.key === 'ArrowLeft' || event.key === 'ArrowUp' ||
                event.key === 'ArrowRight' || event.key === 'ArrowDown') {
            var controls = Array.prototype.slice.call(
                document.querySelectorAll('#login-mask button:not([disabled])')
            ).filter(function (el) { return el.getClientRects().length > 0; });
            if (controls.length) {
                event.preventDefault();
                event.stopPropagation();
                var index = controls.indexOf(document.activeElement);
                var step = (event.key === 'ArrowLeft' || event.key === 'ArrowUp') ? -1 : 1;
                var target = index === -1 ? 0 : (index + step + controls.length) % controls.length;
                controls[target].focus();
            }
            return;
        }
        // 浮层打开时不让方向键 / 快捷键落到后面的页面。
        event.stopPropagation();
    }

    /* ------------------------------------------------------------------ */
    /* WS                                                                  */
    /* ------------------------------------------------------------------ */
    function connectSocket() {
        if (!window.location.host || typeof WebSocket !== 'function') return;
        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var socket;
        try {
            socket = new WebSocket(protocol + '//' + window.location.host + '/ws');
        } catch (error) {
            window.setTimeout(connectSocket, 3000);
            return;
        }
        socket.addEventListener('open', function () { state.ws = true; });
        socket.addEventListener('message', function (event) {
            var payload;
            try { payload = JSON.parse(event.data); } catch (error) { return; }
            if (!payload || payload.type !== 'toast') return;
            var data = payload.data || {};
            notify((data.title ? '[' + data.title + '] ' : '') + (data.message || ''), data.level === 'error' ? 'warn' : 'ok');
        });
        socket.addEventListener('close', function () {
            state.ws = false;
            window.setTimeout(connectSocket, 5000);
        });
        socket.addEventListener('error', function () {
            try { socket.close(); } catch (error) { /* ignore */ }
        });
    }

    /* ------------------------------------------------------------------ */
    /* 04 拆解                                                             */
    /* ------------------------------------------------------------------ */
    function textOf(value) {
        if (Array.isArray(value)) {
            return value.map(textOf).filter(Boolean).join('；');
        }
        if (value && typeof value === 'object') {
            return Object.keys(value).map(function (key) {
                return key + '：' + textOf(value[key]);
            }).filter(Boolean).join('；');
        }
        return String(value === null || value === undefined ? '' : value).trim();
    }

    function fmtMaybe(value) {
        return value === null || value === undefined || value === '' ? '—' : fmt(value);
    }

    function pctMaybe(value) {
        return value === null || value === undefined || value === '' ? '—' : (String(value) + '%');
    }

    function loadAnalysisCategories() {
        if (state.analysisCategories.length) return Promise.resolve(state.analysisCategories);
        return api('/analysis/categories').then(function (payload) {
            state.analysisCategories = (payload && payload.data) || [];
            return state.analysisCategories;
        }).catch(function () {
            state.analysisCategories = [];
            return [];
        });
    }

    function categoryOptions(selected, blankLabel) {
        var html = '';
        if (blankLabel !== undefined) {
            html += '<option value="">' + esc(blankLabel) + '</option>';
        }
        (state.analysisCategories || []).forEach(function (item) {
            html += '<option value="' + esc(item.name) + '"' +
                (String(item.name) === String(selected) ? ' selected' : '') + '>' +
                esc(item.name) + '</option>';
        });
        return html || '<option value="">(NO CATEGORIES)</option>';
    }

    function llmStatusMarkup(llm) {
        if (!llm) return '<p class="live-note">LLM 状态暂不可读；拆解仍会拉取真实账号数据。</p>';
        if (llm.configured) {
            return kv('status', '<span class="is-ok">READY</span>') +
                kv('model', esc(llm.model || '—')) +
                kv('base', esc(short(llm.api_base || '—', 42)));
        }
        return '<p class="live-note">LLM 未配置：拆解仍会返回真实 UP 数据，策略分析会跳过。</p>' +
            '<p class="live-note">' + esc(short(llm.message || 'configure an LLM in 03 CONFIG', 180)) + '</p>';
    }

    function loadBench(force) {
        if (state.loading.bench) return Promise.resolve();
        if (!force && state.bench && state.bench.ready) return Promise.resolve();
        state.loading.bench = true;
        var root = liveRoot('bench');
        if (!state.bench && root) loading('bench', 'READING ANALYSIS CATEGORIES...');
        return Promise.all([
            settle(loadAnalysisCategories()),
            settle(api('/analysis/llm-status'))
        ]).then(function (results) {
            state.bench = state.bench || {};
            state.bench.categories = results[0].ok ? results[0].value : [];
            state.bench.llm = results[1].ok ? (results[1].value.data || {}) : null;
            state.bench.ready = true;
            state.bench.status = state.bench.status || 'IDLE · 选择分区加载榜单，或直接输入 UID / 主页链接';
            renderBench();
        }).catch(function (error) {
            setHTML('bench', errorBox(error.message));
        }).then(function () {
            state.loading.bench = false;
        });
    }

    function renderBench() {
        var data = state.bench || {};
        var categories = state.analysisCategories || [];
        var selected = data.category || (categories[0] && categories[0].name) || '';
        var limit = data.limit || 10;
        var target = data.target || '';
        var html = '' +
            '<p class="page-sub">真实头部 UP 拆解 · 分区榜单、账号数据与 LLM 策略分析。榜单和拆解会按需请求 B站，不做后台轮询。</p>' +
            '<div class="live-actions">' +
            '<label class="field live-field"><span class="field-label">ZONE</span>' +
            '<select class="live-select" id="bench-zone" aria-label="分区">' + categoryOptions(selected) + '</select></label>' +
            '<label class="field live-field"><span class="field-label">LIMIT</span>' +
            '<input class="live-input live-input-sm" id="bench-limit" type="number" min="1" max="50" value="' + esc(limit) + '" aria-label="数量"></label>' +
            '<button class="btn" type="button" data-live-act="bench_top">LOAD TOP</button>' +
            '<button class="btn" type="button" data-live-act="bench_refresh">RESET</button>' +
            '<span class="hint">REAL API · /api/analysis</span></div>' +
            '<div class="live-note" id="bench-status">' + esc(data.status || 'IDLE') + '</div>' +
            '<div class="split split-13">' +
            pane('LLM', '策略分析', '/analysis/llm-status', llmStatusMarkup(data.llm)) +
            pane('TARGET', '拆解对象', 'UID / URL',
                '<div class="live-actions"><input class="live-input live-input-wide" id="bench-target" type="text" ' +
                'placeholder="UID / space.bilibili.com/..." value="' + esc(target) + '" aria-label="UP主 UID 或主页链接">' +
                '<button class="btn btn-primary" type="button" data-live-act="bench_analyze">ANALYZE</button></div>' +
                '<p class="live-note">拆解会抓取主页与视频，再调用 LLM 策略分析。LLM 不可用时仍保留原始数据。</p>') +
            '</div>' +
            '<div id="bench-top"></div>' +
            '<div id="bench-analysis"></div>';
        setHTML('bench', html);
        renderBenchTop();
        renderBenchAnalysis();
        setMeta('bench', data.result ? 'LIVE · ANALYZED' : 'LIVE · READY');
    }

    function renderBenchTop() {
        var data = (state.bench && state.bench.top) || null;
        var ups = (data && data.up_list) || [];
        var body;
        if (!data) {
            body = emptyBox('尚未加载分区榜单');
        } else if (!ups.length) {
            body = emptyBox('这个分区没有返回可用的 UP 主');
        } else {
            body = '<table class="tbl"><thead><tr><th>#</th><th>账号</th><th class="t-r">粉丝</th>' +
                '<th class="t-r">单稿播放</th><th class="t-r">UID</th><th></th></tr></thead><tbody>' +
                ups.map(function (up, index) {
                    var uid = up.uid || up.mid || '';
                    return '<tr><td class="dim">' + String(index + 1).padStart(2, '0') + '</td>' +
                        '<td class="strong">' + esc(up.name || '未知 UP') + '</td>' +
                        '<td class="t-r mono">' + fmtMaybe(up.follower_count !== undefined ? up.follower_count : up.fans) + '</td>' +
                        '<td class="t-r mono">' + fmtMaybe(up.play !== undefined ? up.play : up.total_play) + '</td>' +
                        '<td class="t-r mono">' + esc(uid || '—') + '</td>' +
                        '<td class="t-r"><button class="btn" type="button" data-live-act="bench_select" data-uid="' +
                        esc(uid) + '">ANALYZE</button></td></tr>';
                }).join('') + '</tbody></table>';
        }
        setHTML('bench-top', pane('TOP UPS', '同赛道头部账号', data ? ('REAL · ' + esc(data.category || '')) : 'BY 7D RANK',
            body));
    }

    function renderBenchAnalysis() {
        var result = state.bench && state.bench.result;
        if (!result) {
            setHTML('bench-analysis', pane('ANALYSIS', '策略拆解', 'WAITING',
                emptyBox('尚未选择拆解对象')));
            return;
        }
        var envelope = result.up_data || {};
        var up = Object.assign({}, envelope, envelope.data || {});
        var analysis = result.analysis || {};
        var name = up.name || up.uname || '未知 UP';
        var uid = up.uid || up.mid || '—';
        var videos = up.video_list || up.videos || [];
        var strategy;
        if (analysis.success === true && analysis.analysis) {
            var dimensions = ['选题方向', '标题套路', '封面风格', '发布节奏', '互动引导'];
            strategy = pane('PLAYBOOK', '运营策略', 'LLM · ' + (analysis.model || 'model'),
                dimensions.map(function (dimension) {
                    var fields = analysis.analysis[dimension] || {};
                    return '<div class="live-code-line"><b>' + esc(dimension) + '</b><br>' +
                        '<span class="live-note">核心观察</span> ' + esc(textOf(fields['核心观察']) || '—') + '<br>' +
                        '<span class="live-note">具体打法</span> ' + esc(textOf(fields['具体打法']) || '—') + '<br>' +
                        '<span class="live-note">关键要点</span> ' + esc(textOf(fields['关键要点']) || '—') +
                        '</div>';
                }).join('') +
                (analysis.raw_text ? '<details class="live-raw"><summary>RAW OUTPUT</summary><pre class="live-pre">' +
                    esc(analysis.raw_text) + '</pre></details>' : ''));
        } else {
            strategy = pane('PLAYBOOK', '运营策略', 'LLM',
                '<p class="live-note">' + esc(analysis.message || '暂无策略分析；账号原始数据已保留。') + '</p>');
        }
        var summary = pane('UP', '账号数据', 'REAL',
            kv('name', esc(name)) +
            kv('uid', esc(uid)) +
            kv('fans', fmtMaybe(up.fans !== undefined ? up.fans : up.follower_count)) +
            kv('charge', fmtMaybe(up.charge_count)) +
            kv('total play', fmtMaybe(up.total_play !== undefined ? up.total_play : up.play)) +
            kv('videos', fmtMaybe(up.video_count)) +
            (up.sign ? '<p class="live-note">' + esc(short(up.sign, 180)) + '</p>' : ''));
        var videoHtml = videos.length ? pane('VIDEOS', '近期稿件', 'TOP ' + Math.min(videos.length, 8),
            '<table class="tbl"><thead><tr><th>#</th><th>标题</th><th class="t-r">播放</th></tr></thead><tbody>' +
            videos.slice(0, 8).map(function (video, index) {
                var play = video.play !== undefined ? video.play : ((video.stat || {}).view);
                return '<tr><td class="dim">' + String(index + 1).padStart(2, '0') + '</td>' +
                    '<td class="strong">' + esc(short(video.title || '—', 48)) + '</td>' +
                    '<td class="t-r mono">' + fmtMaybe(play) + '</td></tr>';
            }).join('') + '</tbody></table>') : '';
        setHTML('bench-analysis', summary + strategy + videoHtml);
        setMeta('bench', 'LIVE · ' + short(name, 10));
    }

    function benchTop() {
        if (!state.bench) return;
        var zone = (document.getElementById('bench-zone') || {}).value || '';
        var limit = parseInt((document.getElementById('bench-limit') || {}).value, 10) || 10;
        if (!zone) { notify('先选择分区', 'warn'); return; }
        state.bench.category = zone;
        state.bench.limit = limit;
        setStatus('bench-status', 'LOADING TOP · ' + zone);
        return api('/analysis/category-top-ups', {
            method: 'POST', body: { category: zone, limit: limit }
        }).then(function (payload) {
            state.bench.top = payload.data || {};
            state.bench.status = 'LIVE · ' + zone + ' · ' + ((state.bench.top.up_list || []).length) + ' UPS';
            setStatus('bench-status', state.bench.status);
            renderBenchTop();
            setMeta('bench', 'LIVE · TOP');
            notify('OK // 榜单已加载', 'ok');
        }).catch(function (error) {
            state.bench.status = 'ERROR // ' + error.message;
            setStatus('bench-status', state.bench.status);
            notify('榜单加载失败: ' + error.message, 'warn');
        });
    }

    function benchAnalyze(uidOrUrl) {
        if (!state.bench) return;
        var target = uidOrUrl || ((document.getElementById('bench-target') || {}).value || '').trim();
        if (!target) { notify('输入 UID 或主页链接', 'warn'); return; }
        state.bench.target = target;
        state.bench.result = null;
        state.bench.status = 'RUNNING · 创建拆解任务...';
        setStatus('bench-status', state.bench.status);
        setHTML('bench-analysis', pane('ANALYSIS', '策略拆解', 'RUNNING',
            '<p class="live-loading">CREATING TASK...</p>'));
        return api('/analysis/analyze-up/tasks', {
            method: 'POST', body: { uid_or_url: target }
        }).then(function (started) {
            state.benchTask = started.task_id;
            return pollBenchTask(started.task_id);
        }).catch(function (error) {
            state.bench.status = 'ERROR // ' + error.message;
            setStatus('bench-status', state.bench.status);
            setHTML('bench-analysis', pane('ANALYSIS', '策略拆解', 'ERROR', errorBox(error.message)));
        });
    }

    function pollBenchTask(taskId) {
        if (state.benchTask !== taskId) return Promise.resolve();
        return api('/analysis/analyze-up/tasks/' + encodeURIComponent(taskId)).then(function (payload) {
            var task = payload.data || {};
            var message = (task.stage || task.status || 'running') + ' · ' + (task.progress || 0) + '% · ' +
                (task.message || '');
            state.bench.status = message;
            setStatus('bench-status', message);
            if (task.status === 'completed') {
                state.benchTask = '';
                state.bench.result = task.result || {};
                state.bench.status = 'LIVE · 拆解完成';
                setStatus('bench-status', state.bench.status);
                renderBenchAnalysis();
                notify('OK // UP 拆解完成', 'ok');
                return null;
            }
            if (task.status === 'failed') {
                state.benchTask = '';
                state.bench.status = 'ERROR // ' + (task.message || '拆解失败');
                setStatus('bench-status', state.bench.status);
                setHTML('bench-analysis', pane('ANALYSIS', '策略拆解', 'ERROR',
                    errorBox(task.message || '拆解失败')));
                return null;
            }
            setHTML('bench-analysis', pane('ANALYSIS', '策略拆解', 'RUNNING',
                '<p class="live-loading">' + esc(message) + '</p>'));
            return sleep(1200).then(function () { return pollBenchTask(taskId); });
        }).catch(function (error) {
            state.benchTask = '';
            state.bench.status = 'ERROR // ' + error.message;
            setStatus('bench-status', state.bench.status);
            setHTML('bench-analysis', pane('ANALYSIS', '策略拆解', 'ERROR', errorBox(error.message)));
        });
    }

    /* ------------------------------------------------------------------ */
    /* 05 自诊                                                             */
    /* ------------------------------------------------------------------ */
    function authUid() {
        var auth = (state.welcome && state.welcome.auth) || {};
        var user = auth.user || {};
        return user.uid || '';
    }

    function loadDiag(force) {
        if (state.loading.diag) return Promise.resolve();
        if (!force && state.diag && state.diag.ready) return Promise.resolve();
        state.loading.diag = true;
        return Promise.all([
            settle(loadAnalysisCategories()),
            settle(api('/analysis/llm-status'))
        ]).then(function (results) {
            state.diag = state.diag || {};
            state.diag.categories = results[0].ok ? results[0].value : [];
            state.diag.llm = results[1].ok ? (results[1].value.data || {}) : null;
            state.diag.ready = true;
            if (!state.diag.uid) state.diag.uid = authUid();
            state.diag.status = state.diag.status || 'IDLE · 输入自己的 UID 后运行自诊';
            renderDiag();
        }).catch(function (error) {
            setHTML('diag', errorBox(error.message));
        }).then(function () {
            state.loading.diag = false;
        });
    }

    function renderDiag() {
        var data = state.diag || {};
        var uid = data.uid || authUid();
        var category = data.category || '';
        var html = '' +
            '<p class="page-sub">真实账号自诊 · 账号公开数据、互动效率、更新节奏、数据可用性与 AI 调研；报告可直接打印。</p>' +
            '<div class="live-actions">' +
            '<label class="field live-field"><span class="field-label">UID</span>' +
            '<input class="live-input" id="diag-uid" type="number" min="1" value="' + esc(uid) + '" aria-label="自己的 UID"></label>' +
            '<label class="field live-field"><span class="field-label">ZONE</span>' +
            '<select class="live-select" id="diag-zone" aria-label="对比分区">' + categoryOptions(category, '不使用分区') + '</select></label>' +
            '<button class="btn btn-primary" type="button" data-live-act="diag_run">RUN DIAGNOSIS</button>' +
            '<button class="btn" type="button" data-live-act="diag_report">REPORT</button>' +
            '<span class="hint">REAL API · /api/analysis/self-diagnosis</span></div>' +
            '<div class="live-note" id="diag-status">' + esc(data.status || 'IDLE') + '</div>' +
            '<div id="diag-result"></div>' +
            pane('RANKING', '同行排名 · 有限参评集合', 'LOCAL TOKEN',
                '<div class="live-actions">' +
                '<input class="live-input live-input-wide" id="diag-peers" placeholder="同行 UID，逗号/空格分隔" value="' +
                esc((state.ranking.peers || []).join(',')) + '" aria-label="同行 UID 列表">' +
                '<button class="btn btn-primary" type="button" data-live-act="ranking_start">START RANK</button>' +
                '<button class="btn" type="button" data-live-act="ranking_discover">DISCOVER</button>' +
                '<button class="btn" type="button" data-live-act="ranking_cancel">CANCEL</button>' +
                '<button class="btn" type="button" data-live-act="ranking_retry">RETRY</button></div>' +
                '<div class="live-actions">' +
                '<label class="field live-field"><span class="field-label">RID</span>' +
                '<input class="live-input live-input-sm" id="diag-rid" type="number" min="1" value="4" aria-label="分区 rid"></label>' +
                '<label class="field live-field"><span class="field-label">SCOPE</span>' +
                '<select class="live-select" id="diag-scope"><option value="all_public_uploads">全部公开投稿</option>' +
                '<option value="exact_raw_tid">仅原始 tid</option></select></label>' +
                '<label class="field live-field"><span class="field-label">RAW TID</span>' +
                '<input class="live-input live-input-sm" id="diag-raw-tid" type="number" min="1" aria-label="原始 tid"></label>' +
                '<label class="live-check"><input type="checkbox" id="diag-size-match"> SAME FOLLOWER BAND</label></div>' +
                '<div class="live-note" id="diag-ranking-status">IDLE · 手动指定同行或按分区发现候选；排名只针对本次参评账号。</div>' +
                '<div id="diag-ranking-candidates"></div><div id="diag-ranking-result"></div>');
        setHTML('diag', html);
        renderDiagResult();
        renderRanking();
        renderRankingCandidates();
        setMeta('diag', data.result ? 'LIVE · DIAGNOSED' : 'LIVE · READY');
    }

    function parseRankingUids(text, targetUid) {
        var seen = {};
        return String(text || '').split(/[\s,，;；]+/).map(function (part) {
            if (!/^\d+$/.test(part)) return null;
            var value = parseInt(part, 10);
            if (!value || value === targetUid || seen[value]) return null;
            seen[value] = true;
            return value;
        }).filter(function (value) { return value !== null; });
    }

    function rankingStatus(text) {
        var el = document.getElementById('diag-ranking-status');
        if (el) el.textContent = text;
    }

    function renderRankingCandidates() {
        var list = (state.ranking.candidates || []);
        var root = document.getElementById('diag-ranking-candidates');
        if (!root) return;
        if (!list.length) { root.innerHTML = ''; return; }
        root.innerHTML = '<div class="live-note">候选作者（仅发现，不等同排名）</div>' +
            '<div class="live-actions">' + list.map(function (item) {
                return '<button class="btn" type="button" data-live-act="ranking_pick" data-uid="' +
                    esc(item.uid) + '">' + esc(short(item.name || ('UID ' + item.uid), 18)) + '</button>';
            }).join('') + '</div>';
    }

    function renderRanking() {
        var result = state.ranking && state.ranking.result;
        var root = document.getElementById('diag-ranking-result');
        if (!root) return;
        if (!result) {
            root.innerHTML = emptyBox('尚未发起同行排名');
            return;
        }
        if (result.schema_version !== 3 || result.unit !== 'creator') {
            root.innerHTML = errorBox('返回结果不是有效的 creator 排名契约');
            return;
        }
        var target = result.target || {};
        var rows = result.leaderboard || [];
        var excluded = result.excluded_peers || [];
        var stateLabels = {
            complete: '完整比较', partial: '部分参评', insufficient_peers: '有效同行不足',
            target_unavailable: '目标指标不可用'
        };
        function rankText(row) {
            if (!row || row.rank === null || row.rank === undefined) return '无法计算';
            return row.rank_end && row.rank_end !== row.rank ?
                ('第 ' + row.rank + '–' + row.rank_end + ' 名') : ('第 ' + row.rank + ' 名');
        }
        root.innerHTML = '<div class="live-code-line"><b>' +
            esc(stateLabels[result.comparison_state] || result.comparison_state || 'RANKED') + '</b><br>' +
            '<span class="live-note">有效 ' + fmtMaybe(result.valid_peer_count) + ' / 请求 ' +
            fmtMaybe(result.requested_peer_count) + ' · 百分位 ' +
            (target.percentile === null || target.percentile === undefined ? '无法计算' : target.percentile + '%') +
            ' · 快照 ' + esc(short(result.snapshot_hash || '—', 20)) + '</span></div>' +
            '<table class="tbl"><thead><tr><th>UID</th><th class="t-r">稿件</th><th class="t-r">中位播放</th>' +
            '<th>名次</th></tr></thead><tbody>' + rows.map(function (row) {
                return '<tr><td class="mono">' + esc(row.uid) + (row.is_target ? ' (YOU)' : '') + '</td>' +
                    '<td class="t-r mono">' + fmtMaybe(row.selected_count) + '</td>' +
                    '<td class="t-r mono">' + fmtMaybe(row.metric_value) + '</td>' +
                    '<td>' + esc(rankText(row)) + '</td></tr>';
            }).join('') + '</tbody></table>' +
            (excluded.length ? '<p class="live-note">排除：' + excluded.map(function (item) {
                return 'UID ' + esc(item.uid) + ' ' + esc(item.reason || '');
            }).join(' · ') + '</p>' : '');
    }

    function discoverRanking() {
        var rid = parseInt((document.getElementById('diag-rid') || {}).value, 10);
        if (!rid) { notify('输入分区 rid', 'warn'); return Promise.resolve(); }
        rankingStatus('DISCOVER · 分区 ' + rid + '...');
        return writeHeaders('/analysis/benchmark/local-token').then(function (headers) {
            return api('/analysis/benchmark/candidates', {
                method: 'POST', headers: headers,
                body: { taxonomy: 'pid_v2', rid: rid, limit: 20 }
            });
        }).then(function (payload) {
            var data = payload.data || {};
            state.ranking.candidates = data.candidates || [];
            state.ranking.discoveryToken = data.discovery_token || null;
            state.ranking.candidateUids = state.ranking.candidates.map(function (item) { return item.uid; });
            rankingStatus('CANDIDATES · ' + state.ranking.candidates.length);
            renderRankingCandidates();
            notify('候选作者已发现', 'ok');
        }).catch(function (error) {
            rankingStatus('ERROR // ' + error.message);
            notify('候选发现失败: ' + error.message, 'warn');
        });
    }

    function startRanking() {
        var uid = parseInt((document.getElementById('diag-uid') || {}).value, 10);
        var peers = parseRankingUids((document.getElementById('diag-peers') || {}).value, uid);
        if (!uid) { notify('先输入目标 UID', 'warn'); return Promise.resolve(); }
        if (!peers.length) { notify('至少输入 1 个同行 UID', 'warn'); return Promise.resolve(); }
        if (peers.length > 50) { notify('同行最多 50 个', 'warn'); return Promise.resolve(); }
        var scope = (document.getElementById('diag-scope') || {}).value || 'all_public_uploads';
        var rawTid = parseInt((document.getElementById('diag-raw-tid') || {}).value, 10);
        if (scope === 'exact_raw_tid' && !rawTid) {
            notify('exact_raw_tid 需要填写 raw tid', 'warn');
            return Promise.resolve();
        }
        state.ranking.uid = uid;
        state.ranking.peers = peers;
        var body = {
            target_uid: uid, peer_uids: peers, content_scope: scope,
            size_match: (document.getElementById('diag-size-match') || {}).checked ? 'same_follower_band' : 'none'
        };
        if (scope === 'exact_raw_tid') body.raw_tid = rawTid;
        var allDiscovered = state.ranking.discoveryToken && state.ranking.candidateUids &&
            peers.every(function (peer) { return state.ranking.candidateUids.indexOf(peer) !== -1; });
        if (allDiscovered) body.discovery_token = state.ranking.discoveryToken;
        rankingStatus('RANK · 创建任务...');
        return writeHeaders('/analysis/benchmark/local-token').then(function (headers) {
            return api('/analysis/benchmark/tasks', { method: 'POST', headers: headers, body: body });
        }).then(function (payload) {
            var data = payload.data || {};
            state.ranking.runId = data.run_id;
            state.ranking.result = null;
            return pollRanking(data.run_id);
        }).catch(function (error) {
            rankingStatus('ERROR // ' + error.message);
            notify('发起排名失败: ' + error.message, 'warn');
        });
    }

    function pollRanking(runId) {
        if (!runId) return Promise.resolve();
        return api('/analysis/benchmark/tasks/' + encodeURIComponent(runId)).then(function (payload) {
            var task = payload.data || {};
            var text = (task.status || 'running') + ' · ' + (task.progress || 0) + '% · ' + (task.message || '');
            rankingStatus(text);
            if (task.status === 'completed') {
                state.ranking.result = task.result || null;
                renderRanking();
                setMeta('diag', 'LIVE · RANKED');
                notify('OK // 同行排名完成', 'ok');
                return null;
            }
            if (task.status === 'failed' || task.status === 'cancelled' || task.status === 'interrupted') {
                rankingStatus('ERROR // ' + (task.status + ' ' + (task.message || '')));
                return null;
            }
            return sleep(1200).then(function () { return pollRanking(runId); });
        }).catch(function (error) {
            rankingStatus('ERROR // ' + error.message);
        });
    }

    function cancelRanking() {
        if (!state.ranking.runId) { notify('没有可取消的排名任务', 'warn'); return Promise.resolve(); }
        return writeHeaders('/analysis/benchmark/local-token').then(function (headers) {
            return api('/analysis/benchmark/tasks/' + encodeURIComponent(state.ranking.runId) + '/cancel',
                { method: 'POST', headers: headers, body: {} });
        }).then(function () {
            rankingStatus('CANCELLED');
            notify('已请求取消排名', 'warn');
        }).catch(function (error) {
            notify('取消失败: ' + error.message, 'warn');
        });
    }

    function retryRanking() {
        if (!state.ranking.runId) { notify('没有可重试的排名', 'warn'); return Promise.resolve(); }
        return writeHeaders('/analysis/benchmark/local-token').then(function (headers) {
            return api('/analysis/benchmark/runs/' + encodeURIComponent(state.ranking.runId) + '/retry',
                { method: 'POST', headers: headers, body: {} });
        }).then(function (payload) {
            var data = payload.data || {};
            state.ranking.runId = data.run_id;
            state.ranking.result = null;
            return pollRanking(data.run_id);
        }).catch(function (error) {
            notify('重试失败: ' + error.message, 'warn');
        });
    }

    function pickRanking(uid) {
        var value = parseInt(uid, 10);
        if (!value) return;
        if (state.ranking.peers.indexOf(value) === -1) state.ranking.peers.push(value);
        var input = document.getElementById('diag-peers');
        if (input) input.value = state.ranking.peers.join(',');
        rankingStatus('PEERS · ' + state.ranking.peers.length);
    }

    function runDiag() {
        if (!state.diag) return;
        var uid = parseInt((document.getElementById('diag-uid') || {}).value, 10);
        var category = (document.getElementById('diag-zone') || {}).value || '';
        if (!uid) { notify('输入有效的 B站 UID', 'warn'); return; }
        state.diag.uid = uid;
        state.diag.category = category;
        state.diag.status = 'RUNNING · 正在抓取账号公开数据...';
        setStatus('diag-status', state.diag.status);
        setHTML('diag-result', pane('DIAGNOSIS', '账号自诊', 'RUNNING',
            '<p class="live-loading">FETCHING PROFILE / VIDEOS / METRICS...</p>'));
        var body = { uid: uid, category: category || null };
        if (state.ranking.runId && Number(state.ranking.uid) === uid) {
            body.benchmark_run_id = state.ranking.runId;
        }
        return api('/analysis/self-diagnosis', {
            method: 'POST', body: body
        }).then(function (payload) {
            state.diag.result = payload.data || {};
            if (state.diag.result.creator_ranking) {
                state.ranking.result = state.diag.result.creator_ranking;
                if (state.diag.result.creator_ranking.benchmark_run_id) {
                    state.ranking.runId = state.diag.result.creator_ranking.benchmark_run_id;
                }
                renderRanking();
            }
            state.diag.status = 'LIVE · 自诊完成';
            setStatus('diag-status', state.diag.status);
            renderDiagResult();
            notify('OK // 账号自诊完成', 'ok');
        }).catch(function (error) {
            state.diag.status = 'ERROR // ' + error.message;
            setStatus('diag-status', state.diag.status);
            setHTML('diag-result', pane('DIAGNOSIS', '账号自诊', 'ERROR', errorBox(error.message)));
        });
    }

    function renderDiagResult() {
        var result = state.diag && state.diag.result;
        if (!result) {
            setHTML('diag-result', pane('DIAGNOSIS', '自诊结果', 'WAITING', emptyBox('尚未运行自诊')));
            return;
        }
        var self = result.self_data || {};
        var basic = self.basic_info || {};
        var fans = self.fan_stats || {};
        var videos = self.video_stats || {};
        var engagement = self.engagement_metrics || {};
        var rhythm = self.post_rhythm || {};
        var availability = self.data_availability || {};
        var benchmark = result.benchmark || {};
        var ai = result.ai_report || {};
        var tags = self.tag_cloud || {};
        var name = basic.name || ('UID ' + (self.uid || state.diag.uid));
        var metrics = '<div class="metrics">' +
            diagMetric('FANS', fmtMaybe(fans.follower)) +
            diagMetric('FOLLOWING', fmtMaybe(fans.following)) +
            diagMetric('CHARGE', fmtMaybe(fans.charge_count)) +
            diagMetric('GUARD', fmtMaybe(fans.guard_count)) +
            diagMetric('VIDEOS', fmtMaybe(videos.total_count)) +
            diagMetric('TOTAL PLAY', fmtMaybe(videos.total_play)) +
            diagMetric('AVG PLAY', fmtMaybe(videos.avg_play)) +
            diagMetric('30D POSTS', fmtMaybe(rhythm.recent_30d_count)) +
            '</div>';
        var overview = pane('ACCOUNT', name, 'UID ' + esc(self.uid || state.diag.uid),
            '<p class="live-note">Lv.' + esc(basic.level || '—') + ' · ' +
            esc(basic.sign || '无签名') + ' · fetched ' + esc(dateOf(self.fetched_at)) + '</p>' + metrics);
        var available = Object.keys(availability).filter(function (key) { return availability[key]; });
        var availabilityHtml = '<div class="tagline">' + Object.keys(availability).map(function (key) {
            return '<span class="tag' + (availability[key] ? ' hot' : '') + '">' +
                (availability[key] ? 'OK ' : 'NA ') + esc(key) + '</span>';
        }).join('') + '</div>';
        var engagementHtml = pane('ENGAGEMENT', '互动效率', '%',
            kv('粉丝触达率', pctMaybe(engagement.play_to_fans_ratio)) +
            kv('评论率', pctMaybe(engagement.comment_to_play_ratio)) +
            kv('收藏率', pctMaybe(engagement.favorite_to_play_ratio)) +
            kv('最长断更', fmtMaybe(rhythm.longest_gap_days) + ' 天') +
            kv('周均投稿', fmtMaybe(rhythm.videos_per_week) + ' 条') +
            kv('数据可用', fmtMaybe(available.length) + ' 项'));
        var benchmarkHtml = pane('BENCHMARK', '分区基准', 'REAL',
            benchmark.has_benchmark ?
                kv('category', esc(benchmark.category || '—')) +
                kv('sample', fmtMaybe(benchmark.sample_size)) +
                kv('position', esc((benchmark.comparison || {}).position || '—')) +
                kv('vs avg', pctMaybe((benchmark.comparison || {}).vs_avg)) :
                '<p class="live-note">' + esc(benchmark.message || '未选择分区或暂无可用基准。') + '</p>');
        var aiHtml = pane('AI', '调研与点评', ai.success ? 'LLM' : 'UNAVAILABLE',
            ai.success ?
                '<p class="live-note">' + esc(short(ai.report || '', 1200)) + '</p>' +
                (ai.commentary ? '<p class="live-note">' + esc(short(ai.commentary, 1200)) + '</p>' : '') +
                ((ai.highlights || []).length ? '<div class="tagline">' + ai.highlights.map(function (item) {
                    return '<span class="tag">' + esc(item) + '</span>';
                }).join('') + '</div>' : '') :
                '<p class="live-note">' + esc(ai.message || 'AI 调研不可用；公开数据仍然完整。') + '</p>');
        var tagEntries = Object.keys(tags.word_frequency || {}).map(function (key) {
            return [key, num(tags.word_frequency[key])];
        }).sort(function (a, b) { return b[1] - a[1]; }).slice(0, 20);
        var tagHtml = pane('CONTENT DNA', '视频标签', 'TOP ' + tagEntries.length,
            tagEntries.length ? '<div class="tagline">' + tagEntries.map(function (pair) {
                return '<span class="tag' + (pair[1] >= 3 ? ' hot' : '') + '">' + esc(pair[0]) +
                    ' <i>' + fmt(pair[1]) + '</i></span>';
            }).join('') + '</div>' : emptyBox('暂无可用视频标签'));
        var detail = pane('DETAIL', '数据可用性', 'REAL', availabilityHtml);
        setHTML('diag-result', overview + '<div class="split">' + engagementHtml + benchmarkHtml + '</div>' +
            '<div class="split">' + aiHtml + tagHtml + '</div>' + detail);
        setMeta('diag', 'LIVE · ' + short(name, 12));
    }

    function diagMetric(code, value) {
        return '<div class="metric"><span class="m-code">' + esc(code) + '</span>' +
            '<span class="m-val">' + esc(value) + '</span><span class="m-label">REAL DATA</span></div>';
    }

    function isDesktopShell() {
        return window.__FISHTOOL_DESKTOP__ === true;
    }

    function printReportHtml(html) {
        var frame = document.createElement('iframe');
        frame.setAttribute('aria-hidden', 'true');
        frame.setAttribute('sandbox', 'allow-same-origin allow-modals');
        frame.style.position = 'fixed';
        frame.style.right = '0';
        frame.style.bottom = '0';
        frame.style.width = '0';
        frame.style.height = '0';
        frame.style.border = '0';
        frame.srcdoc = html;
        frame.onload = function () {
            try {
                frame.contentWindow.focus();
                frame.contentWindow.print();
            } catch (error) {
                notify('打印预览失败: ' + error.message, 'warn');
            }
            window.setTimeout(function () { frame.remove(); }, 60000);
        };
        document.body.appendChild(frame);
    }

    function requestDesktopSavePdf(html, filename) {
        return new Promise(function (resolve, reject) {
            var token = 'report-' + Date.now() + '-' + Math.random().toString(16).slice(2);
            var chunkSize = 4000;
            var chars = Array.from(html);
            var total = Math.max(1, Math.ceil(chars.length / chunkSize));
            var settled = false;
            var timer = window.setTimeout(function () {
                if (settled) return;
                settled = true;
                delete window.__fishToolSavePdfResult;
                reject(new Error('保存超时，请重试'));
            }, 600000);
            var finish = function (ok, detail) {
                if (settled) return;
                settled = true;
                window.clearTimeout(timer);
                delete window.__fishToolSavePdfResult;
                if (ok) resolve(detail);
                else if (detail === 'cancelled') reject({ cancelled: true });
                else reject(new Error(detail || '保存失败'));
            };
            window.__fishToolSavePdfResult = function (id, ok, detail) {
                if (id !== token) return;
                finish(ok, detail);
            };
            try {
                for (var index = 0; index < total; index += 1) {
                    console.log('FISHTOOL_SAVE_PDF_CHUNK:' + JSON.stringify({
                        token: token, filename: filename, index: index, total: total,
                        chunk: chars.slice(index * chunkSize, (index + 1) * chunkSize).join('')
                    }));
                }
            } catch (error) {
                finish(false, String(error));
            }
        });
    }

    function diagReport() {
        var result = state.diag && state.diag.result;
        if (!result) { notify('请先运行自诊', 'warn'); return; }
        var payload = {
            uid: state.diag.uid,
            category: state.diag.category || null,
            self_data: result.self_data,
            benchmark_data: result.benchmark,
            creator_ranking: result.creator_ranking,
            benchmark_status: result.benchmark_status,
            ai_report: result.ai_report,
            charts: {}
        };
        setStatus('diag-status', 'REPORT · 生成 HTML...');
        return api('/analysis/report-html', { method: 'POST', body: payload }).then(function (response) {
            var data = response.data || {};
            if (!data.html) throw new Error('报告内容为空');
            if (isDesktopShell()) {
                return requestDesktopSavePdf(data.html, data.filename || ('diagnosis_report_' + payload.uid + '.pdf'))
                    .then(function () { notify('报告已保存', 'ok'); });
            }
            printReportHtml(data.html);
            notify('已打开打印预览，请选择“另存为 PDF”', 'ok');
            return null;
        }).catch(function (error) {
            if (error && error.cancelled) notify('已取消保存', 'warn');
            else notify('报告失败: ' + error.message, 'warn');
        }).then(function () {
            setStatus('diag-status', 'LIVE · 自诊完成');
        });
    }

    /* ------------------------------------------------------------------ */
    /* 06 抽奖                                                             */
    /* ------------------------------------------------------------------ */
    function loadLottery(force) {
        if (!state.lottery) {
            state.lottery = {
                target: '', mode: 'standard', focus: '', quickUid: '', preview: null, filter: null, draw: null,
                quick: null, verify: null, winners: [],
                status: 'IDLE · 先预览目标，再筛选或开奖',
                active: null, interrupted: null
            };
        }
        if (force) state.lottery.status = state.lottery.status || 'IDLE';
        renderLottery();
    }

    function renderLottery() {
        var data = state.lottery;
        var target = data.target || '';
        var mode = data.mode || 'standard';
        var winners = data.winners || [];
        var html = '' +
            '<p class="page-sub">真实抽奖 · 目标预览、评论真人筛选、随机开奖与中奖名单校验。所有结果来自 /api/lottery，不生成示意名单。</p>' +
            '<div class="live-actions">' +
            '<label class="field live-field"><span class="field-label">TARGET</span>' +
            '<input class="live-input live-input-wide" id="draw-target" type="text" placeholder="BV号 / 动态完整链接" value="' + esc(target) + '" aria-label="视频或动态目标"></label>' +
            '<label class="field live-field"><span class="field-label">COMMENTS</span>' +
            '<select class="live-select" id="draw-mode" aria-label="评论采集方式">' +
            '<option value="standard"' + (mode === 'standard' ? ' selected' : '') + '>常规</option>' +
            '<option value="up_fulllist"' + (mode === 'up_fulllist' ? ' selected' : '') + '>楼中楼</option></select></label>' +
            '<label class="field live-field"><span class="field-label">FOCUS</span>' +
            '<input class="live-input" id="draw-focus" type="text" placeholder="AI 判定侧重点（可选）" value="' + esc(data.focus || '') + '" aria-label="AI判定侧重点"></label>' +
            '</div>' +
            '<div class="live-actions">' +
            '<button class="btn btn-primary" type="button" data-live-act="draw_preview">PREVIEW</button>' +
            '<button class="btn" type="button" data-live-act="draw_filter">SCAN REAL</button>' +
            '<button class="btn" type="button" data-live-act="draw_run">DRAW</button>' +
            '<button class="btn" type="button" data-live-act="draw_cancel">CANCEL</button>' +
            '<button class="btn" type="button" data-live-act="draw_resume">RESUME</button>' +
            '</div>' +
            '<div class="live-actions">' +
            '<label class="field live-field"><span class="field-label">WINNERS</span>' +
            '<input class="live-input live-input-sm" id="draw-winner-count" type="number" min="1" max="100" value="' + esc(data.winnerCount || 1) + '" aria-label="中奖人数"></label>' +
            '<label class="live-check"><input type="checkbox" id="draw-unique"' + (data.unique === false ? '' : ' checked') + '> UNIQUE UID</label>' +
            '<label class="live-check"><input type="checkbox" id="draw-vip"' + (data.vipOnly ? ' checked' : '') + '> VIP ONLY</label>' +
            '<label class="live-check"><input type="checkbox" id="draw-real"' + (data.realOnly ? ' checked' : '') + '> REAL ONLY</label>' +
            '<label class="field live-field"><span class="field-label">MIN LEVEL</span>' +
            '<select class="live-select" id="draw-min-level">' + minLevelOptions(data.minLevel) + '</select></label>' +
            '<label class="field live-field"><span class="field-label">FROM</span>' +
            '<input class="live-input live-input-date" id="draw-start" type="date" value="' + esc(data.dateStart || '') + '" aria-label="评论开始日期"></label>' +
            '<label class="field live-field"><span class="field-label">TO</span>' +
            '<input class="live-input live-input-date" id="draw-end" type="date" value="' + esc(data.dateEnd || '') + '" aria-label="评论结束日期"></label>' +
            '</div>' +
            '<div class="live-actions">' +
            '<label class="field live-field"><span class="field-label">QUICK UID</span>' +
            '<input class="live-input" id="draw-quick-uid" type="number" min="1" placeholder="用户 UID" aria-label="快速筛选 UID"></label>' +
            '<button class="btn" type="button" data-live-act="draw_quick">QUICK FILTER</button>' +
            '<span class="hint" id="draw-quick-status">本地画像优先 · 缺失时低频补取</span></div>' +
            '<div id="draw-quick-result"></div>' +
            '<div class="live-note" id="draw-status">' + esc(data.status || 'IDLE') + '</div>' +
            '<div id="draw-preview"></div>' +
            '<div id="draw-filter"></div>' +
            '<div id="draw-result"></div>' +
            '<div id="draw-verify-result"></div>';
        setHTML('draw', html);
        renderDrawPreview();
        renderDrawFilter();
        renderDrawResult();
        renderDrawQuick();
        renderVerify();
        setMeta('draw', data.draw || data.filter ? 'LIVE · RESULT' : (data.preview ? 'LIVE · TARGET' : 'LIVE · READY'));
    }

    function minLevelOptions(selected) {
        var html = '<option value=""' + (selected === null || selected === undefined || selected === '' ? ' selected' : '') + '>不限</option>';
        for (var level = 0; level <= 6; level += 1) {
            html += '<option value="' + level + '"' + (String(selected) === String(level) ? ' selected' : '') + '>Lv.' + level + '</option>';
        }
        return html;
    }

    function drawInputs() {
        var target = ((document.getElementById('draw-target') || {}).value || '').trim();
        var count = parseInt((document.getElementById('draw-winner-count') || {}).value, 10) || 1;
        var min = (document.getElementById('draw-min-level') || {}).value;
        return {
            target: target,
            mode: (document.getElementById('draw-mode') || {}).value || 'standard',
            focus: ((document.getElementById('draw-focus') || {}).value || '').trim(),
            quickUid: ((document.getElementById('draw-quick-uid') || {}).value || '').trim(),
            count: count,
            unique: !!(document.getElementById('draw-unique') || {}).checked,
            vip: !!(document.getElementById('draw-vip') || {}).checked,
            real: !!(document.getElementById('draw-real') || {}).checked,
            minLevel: min === '' || min === undefined ? null : parseInt(min, 10),
            dateStart: (document.getElementById('draw-start') || {}).value || '',
            dateEnd: (document.getElementById('draw-end') || {}).value || ''
        };
    }

    function rememberDrawInputs(inputs) {
        if (!state.lottery) return;
        state.lottery.target = inputs.target;
        state.lottery.mode = inputs.mode;
        state.lottery.focus = inputs.focus;
        state.lottery.quickUid = inputs.quickUid;
        state.lottery.winnerCount = inputs.count;
        state.lottery.unique = inputs.unique;
        state.lottery.vipOnly = inputs.vip;
        state.lottery.realOnly = inputs.real;
        state.lottery.minLevel = inputs.minLevel;
        state.lottery.dateStart = inputs.dateStart;
        state.lottery.dateEnd = inputs.dateEnd;
    }

    function renderDrawPreview() {
        var data = state.lottery && state.lottery.preview;
        if (!data) {
            setHTML('draw-preview', pane('TARGET', '目标预览', 'WAITING', emptyBox('先输入目标并 PREVIEW')));
            return;
        }
        setHTML('draw-preview', pane('TARGET', '目标预览', 'REAL',
            kv('title', esc(short(data.title || '—', 80))) +
            kv('author', esc(data.author || data.owner || '—')) +
            kv('type', esc(data.type || data.target_type || '—')) +
            kv('published', esc(data.pubdate || data.publish_time || '—'))));
    }

    function drawPreview() {
        if (!state.lottery) return;
        var inputs = drawInputs();
        rememberDrawInputs(inputs);
        if (!inputs.target) { notify('输入视频 BV 号或动态链接', 'warn'); return; }
        state.lottery.status = 'PREVIEW · 读取目标信息...';
        setStatus('draw-status', state.lottery.status);
        return api('/lottery/preview', { method: 'POST', body: { target: inputs.target } })
            .then(function (payload) {
                state.lottery.preview = payload.data || {};
                state.lottery.status = 'LIVE · 目标已确认';
                setStatus('draw-status', state.lottery.status);
                renderDrawPreview();
                setMeta('draw', 'LIVE · TARGET');
                notify('OK // 目标预览完成', 'ok');
            }).catch(function (error) {
                state.lottery.status = 'ERROR // ' + error.message;
                setStatus('draw-status', state.lottery.status);
                notify('预览失败: ' + error.message, 'warn');
            });
    }

    function drawFilter() {
        if (!state.lottery) return;
        var inputs = drawInputs();
        rememberDrawInputs(inputs);
        if (!inputs.target) { notify('输入视频 BV 号或动态链接', 'warn'); return; }
        state.lottery.status = 'FILTER · 创建真人筛选任务...';
        setStatus('draw-status', state.lottery.status);
        setHTML('draw-filter', pane('FILTER', '真人筛选', 'RUNNING',
            '<p class="live-loading">CREATING FILTER TASK...</p>'));
        return api('/lottery/filter/tasks', {
            method: 'POST',
            body: { target: inputs.target, focus_template: inputs.focus || null, comment_mode: inputs.mode }
        }).then(function (started) {
            state.lottery.active = { kind: 'filter', id: started.task_id };
            state.lottery.interrupted = null;
            return pollLotteryTask('filter', started.task_id);
        }).catch(function (error) {
            state.lottery.status = 'ERROR // ' + error.message;
            setStatus('draw-status', state.lottery.status);
            setHTML('draw-filter', pane('FILTER', '真人筛选', 'ERROR', errorBox(error.message)));
        });
    }

    function drawRun() {
        if (!state.lottery) return;
        var inputs = drawInputs();
        rememberDrawInputs(inputs);
        if (!inputs.target) { notify('输入视频 BV 号或动态链接', 'warn'); return; }
        state.lottery.status = 'DRAW · 创建抽奖任务...';
        setStatus('draw-status', state.lottery.status);
        setHTML('draw-result', pane('DRAW', '抽奖结果', 'RUNNING',
            '<p class="live-loading">CREATING DRAW TASK...</p>'));
        var body = {
            target: inputs.target,
            winner_count: inputs.count,
            unique_users: inputs.unique,
            vip_only: inputs.vip,
            min_level: inputs.minLevel,
            real_only: inputs.real,
            comment_mode: inputs.mode,
            focus_template: inputs.focus || null
        };
        if (inputs.dateStart) body.date_start = inputs.dateStart;
        if (inputs.dateEnd) body.date_end = inputs.dateEnd;
        return api('/lottery/draw/tasks', { method: 'POST', body: body })
            .then(function (started) {
                state.lottery.active = { kind: 'draw', id: started.task_id };
                state.lottery.interrupted = null;
                return pollLotteryTask('draw', started.task_id);
            }).catch(function (error) {
                state.lottery.status = 'ERROR // ' + error.message;
                setStatus('draw-status', state.lottery.status);
                setHTML('draw-result', pane('DRAW', '抽奖结果', 'ERROR', errorBox(error.message)));
            });
    }

    function pollLotteryTask(kind, taskId) {
        if (state.lottery && state.lottery.active && state.lottery.active.id !== taskId) {
            return Promise.resolve();
        }
        return api('/lottery/tasks/' + encodeURIComponent(taskId)).then(function (payload) {
            var task = payload.data || {};
            var message = (task.stage || task.status || 'running') + ' · ' + (task.progress || 0) + '% · ' +
                (task.message || '');
            if (state.lottery) {
                state.lottery.status = message;
                setStatus('draw-status', message);
            }
            if (task.status === 'completed') {
                if (state.lottery) {
                    state.lottery[kind] = task.result || {};
                    state.lottery.active = null;
                    state.lottery.interrupted = null;
                    state.lottery.status = 'LIVE · ' + (kind === 'filter' ? '筛选完成' : '抽奖完成');
                    if (kind === 'draw') state.lottery.winners = task.result && task.result.winners || [];
                }
                setStatus('draw-status', state.lottery.status);
                if (kind === 'filter') {
                    setMeta('draw', 'LIVE · FILTER');
                    renderDrawFilter();
                } else {
                    renderDrawResult();
                }
                notify('OK // ' + (kind === 'filter' ? '真人筛选完成' : '抽奖完成'), 'ok');
                return null;
            }
            if (task.status === 'interrupted') {
                if (state.lottery) {
                    state.lottery.active = null;
                    state.lottery.interrupted = { kind: kind, id: taskId };
                    state.lottery.status = 'INTERRUPTED · 服务重启打断；可点 RESUME 继续';
                }
                setStatus('draw-status', state.lottery.status);
                setHTML(kind === 'filter' ? 'draw-filter' : 'draw-result',
                    pane(kind === 'filter' ? 'FILTER' : 'DRAW', kind === 'filter' ? '真人筛选' : '抽奖结果',
                        'INTERRUPTED', '<p class="live-note">任务被服务重启打断，已完成部分会复用缓存；点 RESUME 继续。</p>'));
                return null;
            }
            if (task.status === 'failed' || task.status === 'cancelled') {
                if (state.lottery) {
                    state.lottery.active = null;
                    state.lottery.status = (task.status === 'cancelled' ? 'CANCELLED' : 'ERROR') + ' // ' +
                        (task.message || '任务结束');
                }
                setStatus('draw-status', state.lottery.status);
                setHTML(kind === 'filter' ? 'draw-filter' : 'draw-result',
                    pane(kind === 'filter' ? 'FILTER' : 'DRAW', kind === 'filter' ? '真人筛选' : '抽奖结果',
                        task.status === 'cancelled' ? 'CANCELLED' : 'ERROR', errorBox(task.message || '任务结束')));
                return null;
            }
            setHTML(kind === 'filter' ? 'draw-filter' : 'draw-result',
                pane(kind === 'filter' ? 'FILTER' : 'DRAW', kind === 'filter' ? '真人筛选' : '抽奖结果',
                    'RUNNING', '<p class="live-loading">' + esc(message) + '</p>'));
            if (state.lotteryTimer) window.clearTimeout(state.lotteryTimer);
            state.lotteryTimer = window.setTimeout(function () { pollLotteryTask(kind, taskId); }, 1200);
            return null;
        }).catch(function (error) {
            if (state.lottery) {
                state.lottery.active = null;
                state.lottery.status = 'ERROR // ' + error.message;
            }
            setStatus('draw-status', state.lottery ? state.lottery.status : 'ERROR');
            notify('任务查询失败: ' + error.message, 'warn');
        });
    }

    function cancelLottery() {
        if (!state.lottery || !state.lottery.active) {
            notify('当前没有运行中的抽奖任务', 'warn');
            return;
        }
        var active = state.lottery.active;
        return api('/lottery/tasks/' + encodeURIComponent(active.id) + '/cancel', { method: 'POST', body: {} })
            .then(function () {
                notify('已请求取消，等待任务收尾', 'warn');
            }).catch(function (error) {
                notify('取消失败: ' + error.message, 'warn');
            });
    }

    function resumeLottery() {
        if (!state.lottery || !state.lottery.interrupted) {
            notify('没有可继续的抽奖任务', 'warn');
            return;
        }
        var interrupted = state.lottery.interrupted;
        return api('/lottery/tasks/' + encodeURIComponent(interrupted.id) + '/resume', { method: 'POST', body: {} })
            .then(function (started) {
                state.lottery.interrupted = null;
                state.lottery.active = { kind: interrupted.kind, id: started.task_id || interrupted.id };
                return pollLotteryTask(interrupted.kind, started.task_id || interrupted.id);
            }).catch(function (error) {
                notify('继续失败: ' + error.message, 'warn');
            });
    }

    function drawUserLine(user, label) {
        var name = user.uname || user.name || ('UID ' + (user.uid || '—'));
        var uid = user.uid || '—';
        var meta = [];
        if (user.level !== undefined && user.level !== null) meta.push('Lv.' + user.level);
        if (user.is_vip) meta.push('VIP');
        if (user.classification) meta.push(user.classification);
        if (user.reason) meta.push(short(user.reason, 120));
        if (label) meta.unshift(label);
        return '<div class="live-user"><div class="live-user-head"><b>' + esc(short(name, 36)) +
            '</b><span>UID ' + esc(uid) + '</span></div><div class="live-user-meta">' +
            esc(meta.join(' · ')) + '</div>' +
            (user.content ? '<div class="live-user-text">' + esc(short(user.content, 180)) + '</div>' : '') +
            '</div>';
    }

    function usersBlock(users, label) {
        if (!users || !users.length) return emptyBox('暂无' + label);
        return users.map(function (user) { return drawUserLine(user, ''); }).join('');
    }

    function renderDrawFilter() {
        var result = state.lottery && state.lottery.filter;
        if (!result) {
            setHTML('draw-filter', pane('FILTER', '真人筛选', 'WAITING', emptyBox('尚未运行真人筛选')));
            return;
        }
        var real = result.real_users || [];
        var suspicious = result.suspicious_users || [];
        var unknown = result.indeterminate_users || [];
        setHTML('draw-filter', pane('FILTER', '真人筛选', 'REAL',
            kv('comments', fmtMaybe(result.comment_count)) +
            kv('users', fmtMaybe(result.user_count)) +
            kv('real / suspicious / unknown', real.length + ' / ' + suspicious.length + ' / ' + unknown.length) +
            kv('source', esc(result.data_source || result.comment_mode || '—')) +
            '<div class="live-code-line"><b>真人候选</b></div>' + usersBlock(real, '真人候选') +
            '<div class="live-code-line"><b>疑似抽奖号</b></div>' + usersBlock(suspicious, '疑似账号') +
            '<div class="live-code-line"><b>资料不足</b></div>' + usersBlock(unknown, '未知账号')));
    }

    function renderDrawResult() {
        var result = state.lottery && state.lottery.draw;
        if (!result) {
            setHTML('draw-result', pane('DRAW', '抽奖结果', 'WAITING', emptyBox('尚未运行抽奖')));
            return;
        }
        var winners = result.winners || [];
        var candidates = result.candidates || [];
        state.lottery.winners = winners;
        var excluded = result.excluded || {};
        var realFilter = result.real_filter || {};
        setHTML('draw-result', pane('DRAW', '抽奖结果', 'REAL',
            kv('candidates', fmtMaybe(result.candidate_count)) +
            kv('excluded level / vip / date', fmtMaybe(excluded.level) + ' / ' + fmtMaybe(excluded.vip) + ' / ' + fmtMaybe(excluded.date)) +
            kv('real filter', realFilter.enabled ? 'ON · ' + JSON.stringify(realFilter.stats || {}) : 'OFF') +
            kv('source', esc(result.data_source || result.comment_mode || '—')) +
            '<div class="live-code-line"><b>中奖名单</b></div>' + usersBlock(winners, '中奖名单')) +
            pane('CANDIDATES', '候选用户', 'REAL · ' + fmt(candidates.length),
                usersBlock(candidates.slice(0, 50), '候选用户') +
                (candidates.length > 50 ? '<p class="live-note">仅显示前 50 位，共 ' + fmt(candidates.length) + ' 位候选。</p>' : '')));
        setMeta('draw', 'LIVE · ' + winners.length + ' WINNERS');
    }

    function patrolPanelHtml() {
        return pane('SCAN', '巡检目标', 'REAL · LOCAL TOKEN',
            '<div class="live-actions">' +
            '<input class="live-input live-input-wide" id="patrol-target" placeholder="视频 BV 号 / 动态链接" aria-label="巡检目标">' +
            '<label class="field live-field"><span class="field-label">MAX</span>' +
            '<input class="live-input live-input-sm" id="patrol-max" type="number" min="1" max="2000" value="500" aria-label="巡检评论上限"></label>' +
            '<label class="field live-field"><span class="field-label">MODE</span>' +
            '<select class="live-select" id="patrol-mode"><option value="time">按时间</option><option value="hot">按热度</option></select></label>' +
            '<button class="btn" type="button" data-live-act="patrol_preflight">PREFLIGHT</button>' +
            '<button class="btn btn-primary" type="button" data-live-act="patrol_scan">SCAN</button>' +
            '<button class="btn" type="button" data-live-act="patrol_judge">AI JUDGE</button></div>' +
            '<div class="live-note" id="patrol-status">IDLE · 先 preflight/scan；只有 REPORT 按钮才会真实提交举报。</div>') +
            pane('REPORT', '举报动作', 'EXPLICIT · CONSENT',
            '<div class="live-actions">' +
            '<button class="btn" type="button" data-live-act="patrol_report_dry">REPORT DRY</button>' +
            '<button class="btn" type="button" data-live-act="patrol_report_native">REPORT</button>' +
            '<button class="btn" type="button" data-live-act="patrol_report_proxy">REPORT PROXY</button>' +
            '<button class="btn" type="button" data-live-act="patrol_ledger">LEDGER</button>' +
            '<button class="btn" type="button" data-live-act="patrol_export">EXPORT CSV</button></div>' +
            '<label class="live-check"><input type="checkbox" id="patrol-consent"> 我已知悉 AI 甄别与举报风险；开启会先写入 preferences 同意版本</label>' +
            '<label class="live-check"><input type="checkbox" id="patrol-proxy"> PROXY REPORT</label>' +
            '<label class="live-check"><input type="checkbox" id="patrol-replies" checked> INCLUDE REPLIES</label>' +
            '<button class="btn" type="button" data-live-act="patrol_prefs">SAVE PREF</button>') +
            pane('PREF', '巡检偏好', 'LOCAL',
            '<div class="live-actions">' +
            '<input class="live-input" id="patrol-whitelist" placeholder="白名单 UID，逗号分隔" aria-label="巡检白名单 UID">' +
            '<input class="live-input live-input-wide" id="patrol-user-keywords" placeholder=\'[{"word":"抽奖","field":"text","weight":2}]\' aria-label="巡检用户关键词 JSON">' +
            '<input class="live-input" id="patrol-default-content" placeholder="默认举报说明（2-200 字）" aria-label="默认举报说明"></div>') +
            '<div id="patrol-summary"></div>' +
            '<div id="patrol-hits"></div>' +
            '<div id="patrol-ledger"></div>';
    }

    function loadPatrolPage() {
        var root = liveRoot('patrol');
        if (!root) return;
        if (!document.getElementById('patrol-summary')) {
            setHTML('patrol', '<p class="page-sub">真实 AI 伪人巡检 · 扫描评论、规则 + LLM 甄别、人工复核、原生/代理举报与台账；命中结果按可信档位展示。</p>' + patrolPanelHtml());
        } else {
            renderPatrol();
        }
        setMeta('patrol', 'LIVE · PATROL');
    }

    function patrolStatus(text) {
        var el = document.getElementById('patrol-status');
        if (el) el.textContent = text;
    }

    function patrolTarget() {
        return (((document.getElementById('patrol-target') || {}).value || '') ||
            (((document.getElementById('draw-target') || {}).value || ''))).trim();
    }

    function patrolWrite(path, method, body) {
        return writeHeaders('/comment/semantic/local-token').then(function (headers) {
            return api(path, { method: method, headers: headers, body: body || {} });
        });
    }

    function patrolPreflight() {
        var target = patrolTarget();
        if (!target) { notify('先在上方 TARGET 输入 BV 或动态链接', 'warn'); return Promise.resolve(); }
        patrolStatus('PREFLIGHT · ' + target + ' ...');
        return Promise.all([
            api('/lottery/patrol/preflight?target=' + encodeURIComponent(target)),
            api('/lottery/patrol/preferences')
        ]).then(function (results) {
            state.patrol.preflight = results[0].data || null;
            state.patrol.prefs = results[1].data || null;
            patrolStatus('READY · 巡检 preflight 已读取');
            renderPatrol();
            return state.patrol.preflight;
        }).catch(function (error) {
            patrolStatus('ERROR // ' + error.message);
            notify('巡检预检失败: ' + error.message, 'warn');
            return null;
        });
    }

    function patrolEnsureConsent(needProxy) {
        var prefs = state.patrol.prefs || {};
        var needs = !prefs.ai_consent_ok || !prefs.risk_notice_ok || (needProxy && !prefs.proxy_consent_ok);
        if (!needs) return Promise.resolve(true);
        if (!(document.getElementById('patrol-consent') || {}).checked) {
            notify('先勾选 AI 甄别与举报风险说明', 'warn');
            return Promise.resolve(false);
        }
        var body = {
            expected_revision: Number(prefs.revision) || 0,
            ai_consent: 'agree',
            risk_notice: 'agree'
        };
        if (needProxy) body.proxy_consent = 'agree';
        return patrolWrite('/lottery/patrol/preferences', 'PUT', body)
            .then(function (payload) {
                state.patrol.prefs = payload.data || state.patrol.prefs;
                return true;
            }).catch(function (error) {
                notify('写入巡检同意失败: ' + error.message, 'warn');
                return false;
            });
    }

    function patrolPrefs() {
        var proxy = !!(document.getElementById('patrol-proxy') || {}).checked;
        var replies = !!(document.getElementById('patrol-replies') || {}).checked;
        if (proxy && !(document.getElementById('patrol-consent') || {}).checked) {
            notify('开启代理举报前先勾选风险说明', 'warn');
            return Promise.resolve();
        }
        var whitelist = ((document.getElementById('patrol-whitelist') || {}).value || '')
            .split(/[\s,，;；]+/).map(function (item) { return parseInt(item, 10); })
            .filter(function (value) { return value > 0; });
        var keywordRaw = ((document.getElementById('patrol-user-keywords') || {}).value || '').trim();
        var keywords = null;
        if (keywordRaw) {
            try {
                keywords = JSON.parse(keywordRaw);
                if (!Array.isArray(keywords)) throw new Error('must be array');
            } catch (error) {
                notify('用户关键词必须是 JSON 数组', 'warn');
                return Promise.resolve();
            }
        }
        var defaultContent = ((document.getElementById('patrol-default-content') || {}).value || '').trim();
        var prefs = state.patrol.prefs;
        var ensure = prefs ? Promise.resolve(prefs) : api('/lottery/patrol/preferences').then(function (payload) {
            state.patrol.prefs = payload.data || {};
            return state.patrol.prefs;
        });
        return ensure.then(function (current) {
            var body = {
                expected_revision: Number(current.revision) || 0,
                proxy_enabled: proxy,
                include_replies: replies,
                whitelist_mids: whitelist
            };
            if (keywords) body.user_keywords = keywords;
            if (defaultContent) body.default_report_content = defaultContent;
            if (proxy) body.proxy_consent = 'agree';
            return patrolWrite('/lottery/patrol/preferences', 'PUT', body);
        }).then(function (payload) {
            state.patrol.prefs = payload.data || state.patrol.prefs;
            notify('巡检偏好已保存', 'ok');
        }).catch(function (error) {
            notify('巡检偏好保存失败: ' + error.message, 'warn');
        });
    }

    function patrolScan() {
        var target = patrolTarget();
        if (!target) { notify('先输入巡检目标', 'warn'); return Promise.resolve(); }
        var max = parseInt((document.getElementById('patrol-max') || {}).value, 10) || 500;
        var mode = (document.getElementById('patrol-mode') || {}).value || 'time';
        var includeReplies = !!(document.getElementById('patrol-replies') || {}).checked;
        patrolStatus('SCAN · ' + target + ' · max ' + max);
        return patrolWrite('/lottery/patrol/scans', 'POST', {
            target: target, max_comments: max, include_replies: includeReplies, mode: mode
        }).then(function (payload) {
            var taskId = payload.task_id;
            state.patrol.task = { task_id: taskId, kind: 'scan' };
            return pollPatrolTask(taskId, 'scan');
        }).catch(function (error) {
            patrolStatus('ERROR // ' + error.message);
            notify('巡检扫描失败: ' + error.message, 'warn');
        });
    }

    function patrolJudge() {
        if (!state.patrol.scanId) { notify('先完成一次巡检扫描', 'warn'); return Promise.resolve(); }
        return patrolEnsureConsent().then(function (ok) {
            if (!ok) return null;
            patrolStatus('JUDGE · scan ' + state.patrol.scanId + ' ...');
            return patrolWrite('/lottery/patrol/scans/' + encodeURIComponent(state.patrol.scanId) + '/judge', 'POST', {
                scope: 'gray', limit: 100, focus: '', request_key: 'patrol-' + Date.now() + '-' + Math.random().toString(16).slice(2)
            });
        }).then(function (payload) {
            if (!payload) return null;
            state.patrol.task = { task_id: payload.task_id, kind: 'judge' };
            return pollPatrolTask(payload.task_id, 'judge');
        }).catch(function (error) {
            patrolStatus('ERROR // ' + error.message);
            notify('AI 甄别失败: ' + error.message, 'warn');
        });
    }

    function pollPatrolTask(taskId, kind) {
        if (!taskId) return Promise.resolve();
        return api('/lottery/patrol/tasks/' + encodeURIComponent(taskId)).then(function (payload) {
            var task = payload.data || {};
            patrolStatus(kind.toUpperCase() + ' · ' + (task.status || 'running') + ' · ' +
                fmtMaybe(task.progress) + '% · ' + (task.message || ''));
            if (task.status === 'completed') {
                var scanId = task.scan_id || (task.result && task.result.scan_id);
                if (scanId) {
                    state.patrol.scanId = scanId;
                    return loadPatrolScan(scanId).then(function () {
                        patrolStatus('DONE · scan ' + scanId);
                        return null;
                    });
                }
                patrolStatus('DONE · ' + kind);
                renderPatrol();
                return null;
            }
            if (task.status === 'failed' || task.status === 'cancelled' || task.status === 'interrupted') {
                patrolStatus('ERROR // ' + (task.message || task.status));
                return null;
            }
            return sleep(1500).then(function () { return pollPatrolTask(taskId, kind); });
        }).catch(function (error) {
            patrolStatus('ERROR // ' + error.message);
        });
    }

    function loadPatrolScan(scanId) {
        return Promise.all([
            settle(api('/lottery/patrol/scans/' + encodeURIComponent(scanId))),
            settle(api('/lottery/patrol/scans/' + encodeURIComponent(scanId) + '/comments?page=1&page_size=50&sort=score_desc'))
        ]).then(function (results) {
            state.patrol.scan = results[0].ok ? (results[0].value.data || null) : null;
            state.patrol.comments = results[1].ok ? (results[1].value.data || null) : null;
            renderPatrol();
        });
    }

    function renderPatrol() {
        var summaryRoot = document.getElementById('patrol-summary');
        var hitsRoot = document.getElementById('patrol-hits');
        var ledgerRoot = document.getElementById('patrol-ledger');
        if (!hitsRoot) return;
        var pf = state.patrol.preflight;
        var scan = state.patrol.scan;
        var comments = (state.patrol.comments && state.patrol.comments.items) || [];
        var reports = state.patrol.reports;
        var prefs = state.patrol.prefs;
        var proxyBox = document.getElementById('patrol-proxy');
        var replyBox = document.getElementById('patrol-replies');
        if (prefs && proxyBox) proxyBox.checked = !!prefs.proxy_enabled;
        if (prefs && replyBox) replyBox.checked = prefs.include_replies !== false;
        if (prefs) {
            var whitelist = document.getElementById('patrol-whitelist');
            var keywords = document.getElementById('patrol-user-keywords');
            var defaultContent = document.getElementById('patrol-default-content');
            if (whitelist && !whitelist.value) whitelist.value = (prefs.whitelist_mids || []).join(',');
            if (keywords && !keywords.value) keywords.value = JSON.stringify(prefs.user_keywords || []);
            if (defaultContent && !defaultContent.value) defaultContent.value = prefs.default_report_content || '';
        }
        var summary = '';
        if (pf) {
            summary += pane('PREFLIGHT', '扫描预检', 'REAL',
                '<div class="live-note">' + esc(pf.target || '') + ' · comments ' +
                fmtMaybe(pf.comment_count || pf.available_comments || '—') + ' · scan ' +
                (pf.scan_available === false ? '不可用' : '可用') + '</div>');
        }
        if (scan) {
            summary += pane('SCAN', '扫描摘要', 'REAL',
                '<div class="live-note">ID ' + esc(scan.scan_id || scan.id || state.patrol.scanId) +
                ' · comments ' + fmtMaybe(scan.comment_count || scan.total) + ' · tiers ' +
                esc(JSON.stringify(scan.tier_counts || scan.tiers || {})) + '</div>');
        }
        var hits = '';
        if (comments.length) {
            hits = pane('HITS', '命中评论', 'SHOWING ' + Math.min(6, comments.length) + ' / ' + comments.length,
                comments.slice(0, 6).map(function (item) {
                    return '<div class="live-user"><div class="live-user-head"><b>' +
                        esc(short(item.uname || ('UID ' + (item.mid || '—')), 28)) + '</b><span>' +
                        esc(item.tier || '—') + ' · ' + fmtMaybe(item.final_score) + '%</span></div>' +
                        '<div class="live-user-meta">rpid ' + esc(item.rpid) + ' · ' + esc(item.report_status || '—') + '</div>' +
                        '<div class="live-user-text">' + esc(short(item.content || '', 180)) + '</div></div>';
                }).join('') +
                (comments.length > 6 ? '<div class="live-note">还有 ' + (comments.length - 6) + ' 条未展开；用 EXPORT CSV 查看完整结果。</div>' : ''));
        } else if (state.patrol.scanId) {
            hits = pane('HITS', '命中评论', 'EMPTY', emptyBox('扫描结果暂无评论'));
        }
        var ledger = '';
        if (Array.isArray(reports)) {
            ledger = pane('LEDGER', '举报台账', 'REAL · ' + reports.length,
                reports.length ? reports.slice(0, 6).map(function (report) {
                    return '<div class="live-user"><div class="live-user-head"><b>#' + esc(report.report_id) +
                        '</b><span>' + esc(report.status || '—') + '</span></div><div class="live-user-meta">' +
                        esc(report.mode || '') + ' · ' + esc(report.target_uname || '') + ' · ' +
                        esc(report.created_at || '') + ' <button class="live-link" type="button" data-live-act="patrol_mark" data-id="' +
                        esc(report.report_id) + '">DONE</button></div></div>';
                }).join('') + (reports.length > 6 ? '<div class="live-note">还有 ' + (reports.length - 6) + ' 条台账未展开。</div>' : '') : emptyBox('台账暂无举报记录'));
        }
        if (summaryRoot) summaryRoot.innerHTML = summary;
        hitsRoot.innerHTML = hits;
        if (ledgerRoot) ledgerRoot.innerHTML = ledger;
    }

    function patrolReport(mode) {
        if (!state.patrol.scanId) { notify('先完成巡检扫描', 'warn'); return Promise.resolve(); }
        var comments = (state.patrol.comments && state.patrol.comments.items) || [];
        var candidates = comments.filter(function (item) {
            return ['bot', 'suspect'].indexOf(item.tier) !== -1 && item.report_status !== 'reported';
        }).slice(0, 10);
        var rpids = candidates.map(function (item) { return parseInt(item.rpid, 10); }).filter(Boolean);
        if (!rpids.length) { notify('没有可举报的疑似评论', 'warn'); return Promise.resolve(); }
        return patrolEnsureConsent(mode === 'proxy').then(function (ok) {
            if (!ok) return null;
            patrolStatus('REPORT · ' + mode + ' · ' + rpids.length + ' 条');
            return patrolWrite('/lottery/patrol/scans/' + encodeURIComponent(state.patrol.scanId) + '/report', 'POST', {
                rpids: rpids, mode: mode, reason: null, content: null,
                confirmed_rpids: mode === 'native' ? rpids : [],
                acknowledge_unknown_rpids: [],
                accept_account_change: false,
                request_key: 'patrol-report-' + Date.now() + '-' + Math.random().toString(16).slice(2)
            });
        }).then(function (payload) {
            if (!payload) return null;
            if (payload.task_id) {
                state.patrol.task = { task_id: payload.task_id, kind: 'report' };
                return pollPatrolTask(payload.task_id, 'report');
            }
            patrolStatus('REPORT · 已提交');
            return patrolLedger();
        }).catch(function (error) {
            patrolStatus('ERROR // ' + error.message);
            notify('举报失败: ' + error.message, 'warn');
        });
    }

    function patrolLedger() {
        return api('/lottery/patrol/reports?days=30').then(function (payload) {
            state.patrol.reports = payload.data || [];
            renderPatrol();
            patrolStatus('LEDGER · ' + state.patrol.reports.length + ' 条');
        }).catch(function (error) {
            patrolStatus('ERROR // ' + error.message);
        });
    }

    function patrolMark(reportId) {
        return patrolWrite('/lottery/patrol/reports/' + encodeURIComponent(reportId) + '/mark', 'POST', { done: true })
            .then(function () { notify('台账已标记完成', 'ok'); return patrolLedger(); })
            .catch(function (error) { notify('台账更新失败: ' + error.message, 'warn'); });
    }

    function patrolExport() {
        if (!state.patrol.scanId) { notify('先完成巡检扫描', 'warn'); return; }
        var link = document.createElement('a');
        link.href = '/api/lottery/patrol/scans/' + encodeURIComponent(state.patrol.scanId) + '/export?include_ids=false';
        link.download = 'patrol_' + state.patrol.scanId + '.csv';
        document.body.appendChild(link);
        link.click();
        link.remove();
        notify('巡检 CSV 开始下载', 'ok');
    }

    function renderDrawQuick() {
        var data = state.lottery && state.lottery.quick;
        if (!data) {
            setHTML('draw-quick-result', '');
            return;
        }
        var assessment = data.assessment || {};
        var profile = data.profile || {};
        var user = Object.assign({}, profile, assessment);
        setHTML('draw-quick-result', pane('QUICK', 'UID 快速筛选', 'REAL',
            kv('uid', esc(profile.uid || assessment.uid || '—')) +
            kv('classification', esc(assessment.classification || assessment.verdict || '—')) +
            kv('confidence', esc(assessment.confidence !== undefined ? assessment.confidence : '—')) +
            drawUserLine(user, '')));
    }

    function drawQuick() {
        if (!state.lottery) return;
        var inputs = drawInputs();
        rememberDrawInputs(inputs);
        if (!/^\d+$/.test(inputs.quickUid)) { notify('输入有效的用户 UID', 'warn'); return; }
        setStatus('draw-quick-status', 'QUICK · 正在核验 UID ' + inputs.quickUid + '...');
        return api('/lottery/quick-filter', {
            method: 'POST', body: { uid: parseInt(inputs.quickUid, 10), focus_template: inputs.focus || null }
        }).then(function (payload) {
            state.lottery.quick = payload.data || {};
            setStatus('draw-quick-status', 'QUICK · 完成');
            renderDrawQuick();
            notify('OK // 快速筛选完成', 'ok');
        }).catch(function (error) {
            setStatus('draw-quick-status', 'ERROR // ' + error.message);
            notify('快速筛选失败: ' + error.message, 'warn');
        });
    }

    function drawVerify() {
        if (!state.lottery || !(state.lottery.winners || []).length) {
            notify('先完成一次抽奖', 'warn');
            return;
        }
        setStatus('draw-status', 'VERIFY · 校验中奖名单...');
        return api('/lottery/verify-winners', {
            method: 'POST', body: { winners: state.lottery.winners, focus_template: (state.lottery.focus || null) }
        }).then(function (payload) {
            state.lottery.verify = payload.data || {};
            state.lottery.status = 'LIVE · 校验完成';
            setStatus('draw-status', state.lottery.status);
            renderVerify();
            notify('OK // 中奖名单已校验', 'ok');
        }).catch(function (error) {
            state.lottery.status = 'ERROR // ' + error.message;
            setStatus('draw-status', state.lottery.status);
            notify('校验失败: ' + error.message, 'warn');
        });
    }

    function renderVerify() {
        var data = state.lottery && state.lottery.verify;
        if (!data) {
            setHTML('draw-verify-result', '');
            return;
        }
        var results = data.results || [];
        setHTML('draw-verify-result', pane('VERIFY', '中奖名单校验', 'REAL',
            kv('winners / real / suspicious / unknown',
                fmtMaybe(data.winner_count) + ' / ' + fmtMaybe(data.real_count) + ' / ' +
                fmtMaybe(data.suspicious_count) + ' / ' + fmtMaybe(data.indeterminate_count)) +
            '<div class="live-code-line"><b>校验明细</b></div>' +
            usersBlock(results, '校验结果')));
    }

    /* ------------------------------------------------------------------ */
    /* 事件分发 / 初始化                                                   */
    /* ------------------------------------------------------------------ */
    function handleLiveAct(action, element) {
        if (action === 'login') { openLogin(); return; }
        if (action === 'logout') { logout(); return; }
        if (action === 'classic') {
            if (window.fishtoolDesktop && window.fishtoolDesktop.openClassic) {
                window.fishtoolDesktop.openClassic();
            } else {
                window.location.href = '/';
            }
            return;
        }
        if (action === 'hotspot_tags' || action === 'hotspot_activities' || action === 'hotspot_topics' || action === 'hotspot_refresh') {
            hotspotAction(action);
            return;
        }
        if (action === 'lifecycle_collect') { collectLifecycle(); return; }
        if (action === 'event_create') { createEvent(); return; }
        if (action === 'event_refresh') { refreshEvents(); return; }
        if (action === 'event_select' && element) { selectEvent(element.getAttribute('data-id')); return; }
        if (action === 'event_discover') { eventTask('discover'); return; }
        if (action === 'event_assess') { eventTask('assess'); return; }
        if (action === 'event_budget') { eventBudget(); return; }
        if (action === 'event_opportunity') { createOpportunity(); return; }
        if (action === 'opp_feedback' && element) {
            feedbackOpportunity(element.getAttribute('data-kind'));
            return;
        }
        if (action === 'event_member' && element) {
            eventMember(element.getAttribute('data-bvid'), element.getAttribute('data-status'));
            return;
        }
        if (action === 'event_toggle' && element) { toggleEvent(element.getAttribute('data-status')); return; }
        if (action === 'topic_status' && element) {
            setTopicStatus(element.getAttribute('data-id'), element.getAttribute('data-status'));
            return;
        }
        if (action === 'watch_release' && element) {
            releaseWatch(element.getAttribute('data-bvid'));
            return;
        }
        if (action === 'alert_read' && element) {
            markAlertRead(element.getAttribute('data-id'));
            return;
        }
        if (action === 'semantic_preflight') { semanticPreflight(); return; }
        if (action === 'semantic_start') { semanticStart(); return; }
        if (action === 'semantic_cancel') { semanticCancel(); return; }
        if (action === 'semantic_resume') { semanticResume(); return; }
        if (action === 'semantic_page' && element) {
            var page = parseInt(element.getAttribute('data-page'), 10) || 1;
            var task = state.semantic.task;
            if (task && task.task_id) loadSemanticResult(task.task_id, page);
            return;
        }
        if (action === 'semantic_decide' && element) {
            semanticDecide(element.getAttribute('data-action'), element.getAttribute('data-group'), element.getAttribute('data-rpid'));
            return;
        }
        if (action === 'watch_refresh' || action === 'watch_export' || action === 'watch_collect' || action === 'watch_monitor' ||
            action === 'watch_keyword' || action === 'watch_account' ||
            action === 'resident_enable' || action === 'resident_pause' || action === 'resident_stop') {
            watchAction(action, element);
            return;
        }
        if (action === 'config_save') { saveConfig(); return; }
        if (action === 'config_test') { testModels(); return; }
        if (action === 'config_reload') { reloadConfig(); return; }
        if (action === 'config_key') { saveConfigKey(); return; }
        if (action === 'logs_refresh') { loadLogs(true); return; }
        if (action === 'logs_export') { exportLogs(); return; }
        if (action === 'bench_top') { benchTop(); return; }
        if (action === 'bench_refresh') {
            if (state.bench) {
                state.bench.top = null;
                state.bench.result = null;
                state.bench.status = 'IDLE · 已重置，选择分区加载榜单';
                state.benchTask = '';
                renderBench();
            }
            return;
        }
        if (action === 'bench_analyze') { benchAnalyze(); return; }
        if (action === 'bench_select' && element) {
            var uid = element.getAttribute('data-uid') || '';
            var input = document.getElementById('bench-target');
            if (input) input.value = uid;
            benchAnalyze(uid);
            return;
        }
        if (action === 'diag_run') { runDiag(); return; }
        if (action === 'diag_report') { diagReport(); return; }
        if (action === 'ranking_start') { startRanking(); return; }
        if (action === 'ranking_discover') { discoverRanking(); return; }
        if (action === 'ranking_cancel') { cancelRanking(); return; }
        if (action === 'ranking_retry') { retryRanking(); return; }
        if (action === 'ranking_pick' && element) { pickRanking(element.getAttribute('data-uid')); return; }
        if (action === 'draw_preview') { drawPreview(); return; }
        if (action === 'draw_filter') { drawFilter(); return; }
        if (action === 'draw_run') { drawRun(); return; }
        if (action === 'draw_quick') { drawQuick(); return; }
        if (action === 'draw_cancel') { cancelLottery(); return; }
        if (action === 'draw_resume') { resumeLottery(); return; }
        if (action === 'draw_verify') { drawVerify(); return; }
        if (action === 'patrol_preflight') { patrolPreflight(); return; }
        if (action === 'patrol_prefs') { patrolPrefs(); return; }
        if (action === 'patrol_scan') { patrolScan(); return; }
        if (action === 'patrol_judge') { patrolJudge(); return; }
        if (action === 'patrol_report_dry') { patrolReport('dry_run'); return; }
        if (action === 'patrol_report_native') { patrolReport('native'); return; }
        if (action === 'patrol_report_proxy') { patrolReport('proxy'); return; }
        if (action === 'patrol_ledger') { patrolLedger(); return; }
        if (action === 'patrol_export') { patrolExport(); return; }
        if (action === 'patrol_mark' && element) { patrolMark(element.getAttribute('data-id')); return; }
    }

    function onDocumentClick(event) {
        var el = event.target;
        while (el && el !== document) {
            if (el.getAttribute && el.getAttribute('data-live-act')) {
                event.preventDefault();
                handleLiveAct(el.getAttribute('data-live-act'), el);
                return;
            }
            if (el.getAttribute && el.getAttribute('data-live-toggle')) {
                event.preventDefault();
                toggleConfig(el);
                return;
            }
            el = el.parentNode;
        }
    }

    function onDocumentSubmit(event) {
        var form = event.target;
        if (!form || !form.getAttribute || !form.getAttribute('data-live-form')) return;
        event.preventDefault();
        var kind = form.getAttribute('data-live-form');
        if (kind === 'watch_add') {
            addWatch(((document.getElementById('hs-bvid') || {}).value || '').trim());
        } else if (kind === 'topics') {
            runTopics();
        } else if (kind === 'config') {
            saveConfig();
        }
    }

    function enter(key) {
        if (key === 'welcome') {
            if (!state.welcome) loadWelcome(false);
            return;
        }
        if (key === 'hotspot') {
            if (!state.hotspot) loadHotspot(false);
            return;
        }
        if (key === 'watch') {
            if (!state.watch) loadWatch(false);
            return;
        }
        if (key === 'config') {
            if (!state.config) loadConfig(false);
            return;
        }
        if (key === 'logs') {
            loadLogs(false);
            return;
        }
        if (key === 'bench') {
            loadBench(false);
            return;
        }
        if (key === 'diag') {
            loadDiag(false);
            return;
        }
        if (key === 'draw') {
            loadLottery(false);
            return;
        }
        if (key === 'patrol') {
            loadPatrolPage();
            return;
        }
    }

    function init() {
        document.addEventListener('click', onDocumentClick);
        document.addEventListener('submit', onDocumentSubmit);
        document.addEventListener('keydown', onLoginKey, true);
        var loginClose = document.getElementById('login-close');
        if (loginClose) loginClose.addEventListener('click', closeLogin);
        var loginRefresh = document.getElementById('login-refresh');
        if (loginRefresh) loginRefresh.addEventListener('click', startLoginQr);
        var loginCancel = document.getElementById('login-cancel');
        if (loginCancel) loginCancel.addEventListener('click', closeLogin);
        var loginMask = document.getElementById('login-mask');
        if (loginMask) {
            loginMask.addEventListener('click', function (event) {
                if (event.target === loginMask) closeLogin();
            });
        }
        connectSocket();
        loadWelcome(false);
        state.logTimer = window.setInterval(function () {
            if (window.FishTui && window.FishTui.currentPage() === 'logs' && !document.hidden) {
                loadLogs(false);
            }
        }, 5000);
    }

    window.FishLive = {
        init: init,
        enter: enter,
        whoami: function () {
            var auth = (state.welcome && state.welcome.auth) || {};
            if (auth.logged_in && auth.user) {
                return 'operator · ' + (auth.user.username || ('UID ' + auth.user.uid));
            }
            return 'operator · not logged in';
        },
        logout: logout,
        quit: function () {
            if (window.fishtoolDesktop && window.fishtoolDesktop.close) {
                window.fishtoolDesktop.close();
                return;
            }
            notify('INFO // 浏览器中请关闭标签页；桌面端会最小化到托盘', 'warn');
        },
        action: function (name) {
            var page = window.FishTui ? window.FishTui.currentPage() : '';
            if (name === 'refresh') {
                refreshCurrent();
                return true;
            }
            if (name === 'scan') {
                if (page === 'hotspot') { runTagCloud(); return true; }
                if (page === 'watch') { runWatchCollect('monitor'); return true; }
                if (page === 'bench') { benchTop(); return true; }
                if (page === 'diag') { runDiag(); return true; }
                if (page === 'draw') { drawFilter(); return true; }
                if (page === 'patrol') { patrolScan(); return true; }
                refreshCurrent();
                return true;
            }
            if (name === 'save' && page === 'config') { saveConfig(); return true; }
            if (name === 'test' && page === 'config') { testModels(); return true; }
            if (name === 'export' && page === 'watch') { exportWatchCsv(); return true; }
            if (name === 'export' && page === 'diag') { diagReport(); return true; }
            if (name === 'export' && page === 'patrol') { patrolExport(); return true; }
            if (name === 'login') { openLogin(); return true; }
            return false;
        }
    };
})();
