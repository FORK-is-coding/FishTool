/* FishTool 首次启动开场动画（是否播放由 <head> 里的 intro-gate.js 决定，见 INTRO_PLAN 2.5）
   时间驱动：state = f(t)，每帧由 t 计算全部画面状态（做法同 RhineLabUI），播放、跳过、逐帧定位结果一致。
   3D：three.js r160（UMD，Chromium 83 实测 60fps）。磁带盒外壳 MeshPhysicalMaterial transmission + roughness 做磨砂半透明；
   边缘条形码、居中 LOGO 底纹为纯色贴图；文档卡用色块模拟文字，固定 16:10，只做等比缩放，不变形。
   退场：官网 sectionDivider 同类推移（深色斜纹层自右推入 → 标题自右 2rem 推入 → 整层向左推出），
   欢迎页元素按官网顺序入场：装饰自右 110%→0（400ms），标题与正文自左 -100%→0（300ms，晚 300ms），插画 15rem→0（5s）。
   只用 ES2019 语法（Chromium 83）：不用逻辑赋值、可选链、空值合并、私有字段、数组 at 方法和字符串全量替换方法。
   页面元素查找全部容错：欢迎页结构缺失时只少一段入场动效，开场照常结束，主界面照常可用。 */
(function () {
    'use strict';
    var KEY = 'fishtool.intro.v1';
    // three.js 与 LOGO 数据按需加载：不播放时不下载（three r160 UMD 约 650KB）
    var SCRIPT = document.currentScript;
    var ASSET = {
        three: (SCRIPT && SCRIPT.getAttribute('data-three')) || '/static/vendor/three.min.js',
        logo: (SCRIPT && SCRIPT.getAttribute('data-logo')) || '/static/intro/logo-mark.js'
    };
    var FPS = 25;
    // 结束时刻不在这里：由 renderExit() 按入场最后一项算出（正常播放 7.95s）
    var T = { boot: 0, spin: 0.9, eject: 3.2, zoom: 4.6, cover: 5.6, push: 6.0 };
    var STEPS = [
        { at: 0.0, no: '01', name: 'BOOT', cap: 'FISHTOOL // OPERATIONS CONSOLE' },
        { at: 0.9, no: '02', name: 'LINK', cap: 'LINK LOCAL SERVICE  ·  本地服务已就绪' },
        { at: 2.2, no: '03', name: 'READ', cap: 'READING ARCHIVE CASSETTE' },
        { at: 3.2, no: '04', name: 'EJECT', cap: 'EXTRACTING DOCUMENT  ·  01 / 05' },
        { at: 4.6, no: '05', name: 'OPEN', cap: 'OPEN  ·  WELCOME' }
    ];
    var LITE_END = 2.4;

    function clamp(x, a, b) { return x < a ? a : x > b ? b : x; }
    function smooth(t) { t = clamp(t, 0, 1); return t * t * t * (10 + t * (-15 + 6 * t)); }
    function easeOutQuad(t) { t = clamp(t, 0, 1); return 1 - (1 - t) * (1 - t); }
    function easeInOutCubic(t) { t = clamp(t, 0, 1); return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
    // 官网插画漂移 cubicBezier(0,1,0,.95) 的近似：快速冲出、长尾减速
    function easeExpoOut(t) { t = clamp(t, 0, 1); return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); }
    function seg(t, a, b) { return clamp((t - a) / (b - a), 0, 1); }
    function lerp(a, b, k) { return a + (b - a) * k; }

    function params() { return new URLSearchParams(location.search.replace(/;/g, '&')); }
    function forcedMode() { var f = params().get('intro'); return f === 'full' || f === 'lite' ? f : null; }
    function reducedMotion() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }
    // 只有首次自动播放才写「已播放」标记；URL 强制播放和欢迎页重播都不写
    function markPlayed() { if (forcedMode() || replaying) return; try { window.localStorage.setItem(KEY, '1'); } catch (e) { /* 不可写时只本次不再播 */ } }

    /* ---------------- 共享的文档版式：3D 贴图与 2D 铺满层用同一份色块，交接时不跳 ---------------- */
    var DOC_W = 1600, DOC_H = 1000;   // 16:10
    var DOC_BLOCKS = [
        // [x, y, w, h, color]：以 1600x1000 为基准
        [0, 0, 22, 1000, '#191919'], [0, 0, 22, 74, '#ff1aac'], [0, 74, 22, 74, '#00ffa2'], [0, 148, 22, 74, '#fffa00'],
        [96, 84, 300, 16, '#2b2b2b'], [96, 120, 180, 10, '#b8b8b4'], [1280, 84, 224, 40, '#2b2b2b'],
        [96, 196, 640, 46, '#262626'], [96, 262, 420, 46, '#262626'],
        [96, 362, 880, 12, '#cfcfcb'], [96, 392, 820, 12, '#cfcfcb'], [96, 422, 860, 12, '#cfcfcb'], [96, 452, 560, 12, '#cfcfcb'],
        [96, 528, 860, 12, '#d6d6d2'], [96, 558, 800, 12, '#d6d6d2'], [96, 588, 840, 12, '#d6d6d2'], [96, 618, 400, 12, '#d6d6d2'],
        [1060, 196, 444, 300, '#e3e3df'], [1060, 196, 444, 300, 'hatch'], [1060, 520, 300, 10, '#c4c4c0'], [1060, 546, 220, 10, '#c4c4c0'],
        [96, 720, 260, 120, '#e8e8e4'], [380, 720, 260, 120, '#e8e8e4'], [664, 720, 260, 120, '#e8e8e4'],
        [120, 744, 60, 30, '#2b2b2b'], [404, 744, 90, 30, '#2b2b2b'], [688, 744, 40, 30, '#2b2b2b'],
        [96, 900, 1408, 2, '#cfcfcb'], [96, 926, 200, 10, '#b8b8b4'], [1304, 920, 200, 22, 'bar']
    ];

    function drawDoc(g, w, h) {
        var sx = w / DOC_W, sy = h / DOC_H;
        g.fillStyle = '#f4f4f2'; g.fillRect(0, 0, w, h);
        // 纸面颗粒（固定种子的伪随机，每次画出来都一样）
        var sd = 20261031;
        function rnd() { sd = (sd * 1103515245 + 12345) % 2147483648; return sd / 2147483648; }
        for (var n = 0; n < 2600; n++) { g.fillStyle = 'rgba(0,0,0,' + (rnd() * .035).toFixed(3) + ')'; g.fillRect(rnd() * w, rnd() * h, 1, 1); }
        DOC_BLOCKS.forEach(function (b) {
            var x = b[0] * sx, y = b[1] * sy, bw = b[2] * sx, bh = b[3] * sy;
            if (b[4] === 'hatch') {
                g.save(); g.beginPath(); g.rect(x, y, bw, bh); g.clip(); g.strokeStyle = 'rgba(25,25,25,.10)'; g.lineWidth = Math.max(1, sx * 2);
                for (var k = -bh; k < bw; k += 10 * sx) { g.beginPath(); g.moveTo(x + k, y + bh); g.lineTo(x + k + bh, y); g.stroke(); }
                g.restore();
            } else if (b[4] === 'bar') {
                drawBarcode(g, x, y, bw, bh, '#2b2b2b', 11);
            } else { g.fillStyle = b[4]; g.fillRect(x, y, bw, bh); }
        });
    }

    // 文档画布只画一次并缓存；颗粒噪点用固定种子，重播时画面一致
    var docCanvasCache = null;
    function docCanvas() {
        if (!docCanvasCache) {
            docCanvasCache = document.createElement('canvas');
            docCanvasCache.width = DOC_W; docCanvasCache.height = DOC_H;
            docCanvasCache.className = 'fi-doc-canvas';
            drawDoc(docCanvasCache.getContext('2d'), DOC_W, DOC_H);
        }
        return docCanvasCache;
    }

    function drawBarcode(g, x, y, w, h, color, seed) {
        var r = seed, pos = 0, bars = [];
        function rnd() { r = (r * 9301 + 49297) % 233280; return r / 233280; }
        while (pos < w) { var bw = 1 + Math.floor(rnd() * 3), gap = 1 + Math.floor(rnd() * 3); bars.push([pos, bw]); pos += bw + gap; }
        var k = w / pos; g.fillStyle = color;
        bars.forEach(function (b) { g.fillRect(x + b[0] * k, y, Math.max(1, b[1] * k), h); });
    }

    /* ---------------- DOM ---------------- */
    function build() {
        var el = document.createElement('div');
        el.className = 'fish-intro'; el.id = 'fish-intro';
        el.innerHTML =
            '<div class="fi-bg" aria-hidden="true"></div>' +
            '<canvas class="fi-canvas" aria-hidden="true"></canvas>' +
            // 2D 文档层紧贴 3D 画布之上、所有 UI 之下：交接时左侧进度条与步骤标签不会被盖住
            '<div class="fi-doc" aria-hidden="true"><div class="fi-doc-inner"></div></div>' +
            '<i class="fi-line" aria-hidden="true"></i>' +
            '<div class="fi-rail" aria-hidden="true"><i class="fi-fill"></i><i class="fi-sweep"></i></div>' +
            '<div class="fi-ticks" aria-hidden="true">' + STEPS.map(function () { return '<i></i>'; }).join('') + '</div>' +
            '<div class="fi-step" aria-hidden="true"><span class="fi-step-no">01 / 05</span><b class="fi-step-name">BOOT</b></div>' +
            '<div class="fi-pct" aria-hidden="true"><b>0</b><small>%</small></div>' +
            '<div class="fi-caption" aria-hidden="true"></div>' +
            '<div class="fi-meta" aria-hidden="true">ARCHIVE CASSETTE · FT-0425<br>BUILD 0.2.9</div>' +
            '<div class="fi-divider" aria-hidden="true"><div class="fi-divider-title"><span>FISHTOOL // CONSOLE</span><b>WELCOME</b></div></div>' +
            '<button type="button" class="btn btn-line-dark fi-skip">跳过 SKIP</button>';
        document.body.insertBefore(el, document.body.firstChild);
        // 2D 铺满层与 3D 贴图用同一张画布（docCanvas()），颗粒、条码、斜纹逐像素一致，3D → 2D 交接不跳
        var inner = el.querySelector('.fi-doc-inner');
        inner.appendChild(docCanvas());
        var ticks = el.querySelectorAll('.fi-ticks i');
        for (var i = 0; i < ticks.length; i++) ticks[i].style.top = Math.min(97, STEPS[i].at / T.zoom * 100) + '%';
        return el;
    }

    /* ---------------- 3D 场景 ---------------- */
    var CASE_W = 3.2, CASE_H = 2.0, CASE_D = 0.46;
    // 16:10，与 DOC_W:DOC_H 一致，贴图不拉伸。卡片取外壳的 80% × 80%：大小适中，
    // 芯板只需略大于卡片就能在弹出前把它完全挡住，外壳四周留出约 0.2 的磨砂边，能透出背后的场景纹理
    var CARD_W = 2.56, CARD_H = 1.6;
    var CAM_Z = 8, FOV = 30;

    function canvasTex(w, h, draw) {
        var c = document.createElement('canvas'); c.width = w; c.height = h;
        draw(c.getContext('2d'), w, h);
        var t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
        return t;
    }

    function roundedRect(w, h, r) {
        var s = new THREE.Shape(), x = -w / 2, y = -h / 2;
        s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r);
        s.lineTo(x + w, y + h - r); s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        s.lineTo(x + r, y + h); s.quadraticCurveTo(x, y + h, x, y + h - r);
        s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
        return s;
    }

    function makeScene(canvas, logoImg) {
        var THREE_ = THREE;
        var renderer = new THREE_.WebGLRenderer({ canvas: canvas, antialias: true, alpha: false, powerPreference: 'high-performance' });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
        renderer.toneMapping = THREE_.ACESFilmicToneMapping;
        renderer.toneMappingExposure = 1.05;
        var scene = new THREE_.Scene();
        // 背景：#141414 + 细斜纹 + 大方格对角线，中心略亮。transmission 会把它模糊地透过磨砂外壳
        scene.background = canvasTex(1024, 640, function (g, w, h) {
            g.fillStyle = '#141414'; g.fillRect(0, 0, w, h);
            var rg = g.createRadialGradient(w * .55, h * .5, 10, w * .55, h * .5, w * .62);
            rg.addColorStop(0, 'rgba(60,60,60,.55)'); rg.addColorStop(1, 'rgba(20,20,20,0)');
            g.fillStyle = rg; g.fillRect(0, 0, w, h);
            g.strokeStyle = 'rgba(255,255,255,.035)'; g.lineWidth = 1;
            for (var k = -h; k < w; k += 6) { g.beginPath(); g.moveTo(k, h); g.lineTo(k + h, 0); g.stroke(); }
            g.strokeStyle = 'rgba(255,255,255,.06)'; g.lineWidth = 1.5;
            for (var x = 0; x <= w; x += 104) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); }
            for (var y = 0; y <= h; y += 104) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
            g.strokeStyle = 'rgba(255,255,255,.03)';
            for (var d = -h; d < w; d += 104) { g.beginPath(); g.moveTo(d, 0); g.lineTo(d + h, h); g.stroke(); }
        });
        // 简易影棚环境（核心 API 的 PMREMGenerator，不依赖 examples）：两块柔光板 + 一条顶光，给磨砂外壳与清漆层高光
        var envScene = new THREE_.Scene();
        envScene.add(new THREE_.Mesh(new THREE_.BoxGeometry(30, 30, 30), new THREE_.MeshBasicMaterial({ color: 0x0c0c0c, side: THREE_.BackSide })));
        [[-8, 3, 6, 6, 4, 1.6], [9, 1, 2, 3, 6, 0.9], [0, 10, 0, 12, 2, 1.2]].forEach(function (p) {
            var m = new THREE_.Mesh(new THREE_.PlaneGeometry(p[3], p[4]), new THREE_.MeshBasicMaterial({ color: new THREE_.Color(p[5], p[5], p[5]), side: THREE_.DoubleSide }));
            m.position.set(p[0], p[1], p[2]); m.lookAt(0, 0, 0); envScene.add(m);
        });
        var pmrem = new THREE_.PMREMGenerator(renderer);
        scene.environment = pmrem.fromScene(envScene, 0.04).texture;
        pmrem.dispose();
        scene.add(new THREE_.HemisphereLight(0xffffff, 0x1a1a1a, 0.6));
        var key = new THREE_.DirectionalLight(0xffffff, 1.6); key.position.set(-4, 5, 6); scene.add(key);
        var rim = new THREE_.DirectionalLight(0xfff6d8, 0.9); rim.position.set(6, 2, -4); scene.add(rim);

        var camera = new THREE_.PerspectiveCamera(FOV, 16 / 10, 0.1, 100);
        camera.position.set(0, 0, CAM_Z);

        /* 磁带盒：磨砂半透明外壳 + 内部不透明芯板 + 文档卡 + 侧边条形码 + 居中 LOGO 底纹 */
        var box = new THREE_.Group();
        var shellGeo = new THREE_.ExtrudeGeometry(roundedRect(CASE_W - 0.06, CASE_H - 0.06, 0.14),
            { depth: CASE_D - 0.06, bevelEnabled: true, bevelThickness: 0.03, bevelSize: 0.03, bevelSegments: 4, curveSegments: 10 });
        shellGeo.translate(0, 0, -(CASE_D - 0.06) / 2);
        var shellMat = new THREE_.MeshPhysicalMaterial({
            color: 0xf0f0ee, roughness: 0.46, metalness: 0, transmission: 0.94, thickness: 0.9, ior: 1.42,
            clearcoat: 0.35, clearcoatRoughness: 0.55, attenuationColor: new THREE_.Color(0xe8e8e4), attenuationDistance: 2.4,
            envMapIntensity: 0.9, transparent: false
        });
        var shell = new THREE_.Mesh(shellGeo, shellMat);
        box.add(shell);
        // 芯板 2.66 × 1.68，比文档卡（2.56 × 1.6）大一圈：文档卡放在芯板后面，弹出前完全被挡住，
        // 透过磨砂外壳看到的是灰色芯板和两个卷轴孔，不会透出文档的色块和三色段；芯板外的一圈能透出背景纹理
        var core = new THREE_.Mesh(new THREE_.BoxGeometry(2.66, 1.68, 0.06),
            new THREE_.MeshStandardMaterial({ color: 0x9a9a96, roughness: 0.85, metalness: 0.05 }));
        core.position.z = -0.08; box.add(core);
        // 芯板上两个卷轴孔（磁带盒的识别特征），深灰，透过磨砂只看到柔和的形
        [-0.62, 0.62].forEach(function (x) {
            var hub = new THREE_.Mesh(new THREE_.CylinderGeometry(0.28, 0.28, 0.1, 40), new THREE_.MeshStandardMaterial({ color: 0x3c3c3c, roughness: 0.6 }));
            hub.rotation.x = Math.PI / 2; hub.position.set(x, 0.05, -0.06); box.add(hub);
        });
        // 居中 LOGO 底纹：纯色剪影，贴在外壳正面
        var logoTex = new THREE_.Texture(logoImg); logoTex.needsUpdate = true; logoTex.colorSpace = THREE_.SRGBColorSpace;
        var logo = new THREE_.Mesh(new THREE_.PlaneGeometry(1.0, 1.0), new THREE_.MeshBasicMaterial({ map: logoTex, color: 0x5a5a58, transparent: true, opacity: 0.55, depthWrite: false }));
        logo.position.set(0, 0, CASE_D / 2 + 0.004); box.add(logo);
        // 侧边条形码：正面靠左的竖向标签 + 左侧端面
        var barTex = canvasTex(128, 512, function (g, w, h) {
            g.fillStyle = '#e8e8e4'; g.fillRect(0, 0, w, h);
            g.save(); g.translate(w, 0); g.rotate(Math.PI / 2); drawBarcode(g, 36, 22, h - 72, w - 52, '#262626', 7); g.restore();
            g.fillStyle = '#262626'; g.font = '600 18px "JetBrains Mono", monospace'; g.save(); g.translate(w - 8, h - 28); g.rotate(-Math.PI / 2); g.fillText('FT-0425', 0, 0); g.restore();
        });
        var barMat = new THREE_.MeshStandardMaterial({ map: barTex, roughness: 0.7 });
        var label = new THREE_.Mesh(new THREE_.PlaneGeometry(0.34, 1.36), barMat);
        label.position.set(-CASE_W / 2 + 0.3, 0, CASE_D / 2 + 0.005); box.add(label);
        var spine = new THREE_.Mesh(new THREE_.PlaneGeometry(0.34, 1.36), barMat);
        spine.rotation.y = -Math.PI / 2; spine.position.set(-CASE_W / 2 - 0.003, 0, 0); spine.scale.x = (CASE_D - 0.08) / 0.34; box.add(spine);
        // 文档卡：16:10 薄片，装在外壳与芯板之间，从右侧弹出
        // 与 2D 铺满层共用同一张画布（拷贝一份给 WebGL 上传，DOM 里那份不受影响）
        var docTex = canvasTex(DOC_W, DOC_H, function (g) { g.drawImage(docCanvas(), 0, 0); });
        var card = new THREE_.Mesh(new THREE_.BoxGeometry(CARD_W, CARD_H, 0.012),
            [new THREE_.MeshStandardMaterial({ color: 0xdedede, roughness: .8 }), new THREE_.MeshStandardMaterial({ color: 0xdedede, roughness: .8 }),
             new THREE_.MeshStandardMaterial({ color: 0xdedede, roughness: .8 }), new THREE_.MeshStandardMaterial({ color: 0xdedede, roughness: .8 }),
             new THREE_.MeshBasicMaterial({ map: docTex, toneMapped: false }), new THREE_.MeshStandardMaterial({ color: 0xcfcfcb, roughness: .9 })]);
        // 文档卡夹在芯板背面与外壳背板之间（z -0.15），从右侧滑出
        var cardHome = new THREE_.Vector3(0, 0, -0.15);
        card.position.copy(cardHome);
        box.add(card);
        scene.add(box);

        function resize() {
            var w = canvas.clientWidth || window.innerWidth, h = canvas.clientHeight || window.innerHeight;
            renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
        }
        resize();
        return { renderer: renderer, scene: scene, camera: camera, box: box, card: card, cardHome: cardHome, resize: resize,
                 dispose: function () {
                     scene.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) { (Array.isArray(o.material) ? o.material : [o.material]).forEach(function (m) { if (m.map) m.map.dispose(); m.dispose(); }); } });
                     if (scene.background && scene.background.dispose) scene.background.dispose();
                     if (scene.environment && scene.environment.dispose) scene.environment.dispose();
                     renderer.dispose();
                     // 主动释放 WebGL 上下文：反复重播时不会累积到浏览器的上下文上限（Chromium 约 16 个）
                     renderer.forceContextLoss();
                 } };
    }

    // 文档卡铺满屏幕时的位姿：正对相机，距离取「宽、高都能盖住视口」的较小值（等同 CSS background-size: cover）
    function coverDistance(cam) {
        var t = Math.tan(cam.fov * Math.PI / 360);
        return Math.min(CARD_H / (2 * t), CARD_W / (2 * t * cam.aspect));
    }

    var tmpQ, tmpP, tmpS, coverQ, coverP;
    function poseScene(s, t) {
        var box = s.box, card = s.card;
        var bob = Math.sin(t * 1.7) * 0.06;
        var rise = smooth(seg(t, 0.05, 1.0));
        // 倾斜悬浮旋转：先让左侧带条形码的端面朝向镜头，再转到正面略偏右（右缘是文档卡弹出的一侧）；
        // 全程叠加缓慢摆动和上下浮动。rotation.y 为正时 -x 端面转向镜头。
        // 3.2s 之后不停转：以 0.07 rad/s（约 4°/s）继续缓慢自转直到铺满，方向是让盒子逐渐转向正面，
        // 弹出的文档卡随盒子一起转，透视越来越小，始终留在视口内（铺满位姿按世界坐标计算，自动抵消这部分旋转）
        var spin = 1.1 - 1.45 * easeInOutCubic(seg(t, 0.3, 3.2)) + 0.07 * Math.max(0, t - 3.2) + 0.05 * Math.sin(t * 0.9);
        var settle = smooth(seg(t, 3.2, 4.4));
        var z = easeInOutCubic(seg(t, T.zoom, T.cover));
        box.rotation.set(-0.30 + 0.06 * settle, spin, 0.16 - 0.06 * settle);
        // 弹出前磁带盒左移并略微后退，给弹出的文档卡留出画面右侧空间（卡片全程留在视口内，四周至少留 6% 边距）
        box.position.set(-1.35 * settle, lerp(-0.7, 0, rise) + bob * (1 - 0.5 * settle) - 0.25 * settle - 0.4 * z, lerp(-1.2, 0, rise) - 0.9 * settle - 1.6 * z);
        // 弹出：像唱片出封套，沿盒子自身 x 轴滑出，末端带一点回弹
        var e = seg(t, 3.3, 4.2);
        // 抽出约 80% 卡宽（2.05），尾部仍压在盒内，像唱片半出封套
        var out = 2.05 * smooth(e) + 0.1 * Math.sin(Math.PI * clamp((t - 3.95) / 0.35, 0, 1)) * (t > 3.95 ? 1 : 0);
        card.position.set(s.cardHome.x + out, s.cardHome.y, s.cardHome.z);
        card.rotation.set(0, 0, 0);
        // 放大铺满：把卡片的世界位姿从「挂在盒子上」平滑过渡到「正对相机、恰好铺满」。
        // 只插值位置和朝向，缩放恒为 1，卡片始终保持 16:10，不会被挤压变形；铺满靠拉近距离，等同 CSS 的 cover。
        if (z > 0) {
            if (!tmpQ) { tmpQ = new THREE.Quaternion(); tmpP = new THREE.Vector3(); tmpS = new THREE.Vector3(); coverQ = new THREE.Quaternion(); coverP = new THREE.Vector3(); }
            box.updateMatrixWorld(true);
            card.updateMatrixWorld(true);
            card.matrixWorld.decompose(tmpP, tmpQ, tmpS);
            coverP.set(0, 0, CAM_Z - coverDistance(s.camera) - 0.006);
            tmpP.lerp(coverP, z); tmpQ.slerp(coverQ, z);
            var inv = new THREE.Matrix4().copy(box.matrixWorld).invert();
            var m = new THREE.Matrix4().compose(tmpP, tmpQ, new THREE.Vector3(1, 1, 1)).premultiply(inv);
            m.decompose(card.position, card.quaternion, card.scale);
            card.scale.set(1, 1, 1);
        }
    }

    function loadScript(src) {
        return new Promise(function (ok, bad) {
            var s = document.createElement('script'); s.src = src; s.async = true;
            s.onload = ok; s.onerror = function () { bad(new Error('load ' + src)); };
            document.head.appendChild(s);
        });
    }
    function loadImage(src) {
        return new Promise(function (ok, bad) { var i = new Image(); i.onload = function () { ok(i); }; i.onerror = bad; i.src = src; });
    }

    /* ---------------- 欢迎页向左推移入场（官网 Operator 区块时间线） ---------------- */
    var welcomeTargets = null;
    function collectWelcome() {
        var page = document.getElementById('welcome-page');
        if (!page) return null;
        // 选择器与 templates/index.html 欢迎页的结构一一对应；某项不存在时返回空数组，只是少一段入场动效
        function q(sel) { return [].slice.call(page.querySelectorAll(sel)); }
        page.setAttribute('data-intro-entered', '');
        // 欢迎页从右侧推入时不能让主区出现横向滚动条
        var mc = document.querySelector('.main-content'); if (mc) mc.style.overflowX = 'hidden';
        return {
            page: page,
            deco: q('.hero-hollow, .hero-tape, .hero-meta, .tri-line'),       // 自右 110% → 0，400ms，晚 300ms
            title: q('.hero-title, .hero-side'),                               // 自左 -100% → 0，300ms，晚 600ms
            illus: q('.hero-rings'),                                           // 15rem → 0，5s，晚 300ms
            rest: q('#welcome-page > section:not(.hero)'),                    // 指标行、快速开始、更多
            side: document.querySelector('.sidebar'),
            top: document.querySelector('.app-topbar'),
            main: document.querySelector('.main-content')
        };
    }
    function setX(list, x, op) {
        list.forEach(function (e) { e.style.transform = x ? 'translateX(' + x + ')' : ''; if (op !== undefined) e.style.opacity = op === 1 ? '' : String(op); });
    }
    function poseWelcome(w, u) {
        // u：推移开始后的秒数
        var d = easeOutQuad(seg(u, 0.30, 0.70)), ti = easeOutQuad(seg(u, 0.60, 0.90));
        setX(w.deco, d < 1 ? (110 * (1 - d)).toFixed(2) + '%' : '', d);
        setX(w.title, ti < 1 ? (-100 * (1 - ti)).toFixed(2) + '%' : '', ti);
        // 插画 5s 长尾漂移；finish() 后不再有人逐帧驱动，所以只在开场层存续期间（约 1.6s）推进，结束时直接归位
        var il = easeExpoOut(seg(u, 0.30, 5.30));
        setX(w.illus, il < 1 ? (15 * (1 - il)).toFixed(3) + 'rem' : '', easeOutQuad(seg(u, 0.30, 0.60)));
        var r = easeOutQuad(seg(u, 0.75, 1.15));
        w.rest.forEach(function (e) { e.style.opacity = r < 1 ? String(r) : ''; e.style.transform = r < 1 ? 'translateY(' + (18 * (1 - r)).toFixed(1) + 'px)' : ''; });
        var sd = easeOutQuad(seg(u, 0.45, 0.80));
        if (w.side) w.side.style.transform = sd < 1 ? 'translateX(' + (-60 * (1 - sd)).toFixed(1) + 'px)' : '';
        if (w.top) w.top.style.opacity = sd < 1 ? String(sd) : '';
    }
    function clearWelcome(w) {
        if (!w) return;
        [].concat(w.deco, w.title, w.illus, w.rest, [w.side, w.top]).forEach(function (e) { if (e) { e.style.transform = ''; e.style.opacity = ''; } });
    }

    /* ---------------- 控制器 ---------------- */
    var root, ui, s3d, raf = 0, t0 = 0, clockBase = 0, mode = 'full', state = 'idle', skipAt = -1, logoImg = null, ended = false;
    var lastStep = -1, lastCap = '', returnFocus = null, replaying = false;
    var clockOn = false, pendingSkip = false, skipFocused = false, tailRaf = 0;

    // 调试：?intro_perf=1 记录每帧的 JS 提交耗时、帧间隔（rAF 间隔反映 GPU 是否跟得上）和渲染统计，FishIntro._perf() 读取
    var PERF = params().get('intro_perf') === '1';
    var perfLog = null, perfLast = 0;
    function perfFrame(t, cpuMs) {
        if (!perfLog) perfLog = { seg: {}, info: null, caps: null, gl: null };
        var nowMs = performance.now(), gap = perfLast ? nowMs - perfLast : 0;
        perfLast = nowMs;
        var k = t < 0.9 ? 'boot' : t < 3.2 ? 'spin' : t < 4.6 ? 'eject' : 'zoom';
        var s = perfLog.seg[k] || (perfLog.seg[k] = { n: 0, cpu: 0, cpuMax: 0, gaps: [] });
        s.n++; s.cpu += cpuMs; if (cpuMs > s.cpuMax) s.cpuMax = cpuMs; if (gap) s.gaps.push(Math.round(gap * 10) / 10);
        if (!perfLog.info && s3d) {
            var r = s3d.renderer, i = r.info, gl = r.getContext(), dbg = gl.getExtension('WEBGL_debug_renderer_info');
            var size = new THREE.Vector2(); r.getDrawingBufferSize(size);
            perfLog.info = { calls: i.render.calls, triangles: i.render.triangles, geometries: i.memory.geometries, textures: i.memory.textures, programs: i.programs ? i.programs.length : null };
            perfLog.gl = { renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER), drawingBuffer: [size.x, size.y], pixelRatio: r.getPixelRatio(), antialias: gl.getContextAttributes().antialias, samples: gl.getParameter(gl.SAMPLES) };
        }
    }

    // 调试：?intro_t=秒 从指定时刻开始；再加 ?intro_hold=1 冻结在该时刻（截图、逐帧对照用）
    var HOLD = params().get('intro_hold') === '1';
    function now() { return HOLD ? clockBase : (performance.now() - t0) / 1000 + clockBase; }

    function renderUI(t) {
        var end = mode === 'lite' ? LITE_END : T.zoom;
        var p = clamp(t / end, 0, 1);
        ui.fill.style.transform = 'scaleY(' + p.toFixed(4) + ')';
        var si = 0;
        for (var i = 0; i < STEPS.length; i++) if ((mode === 'lite' ? i / STEPS.length * LITE_END : STEPS[i].at) <= t) si = i;
        if (si !== lastStep) {
            ui.prevStep = lastStep;
            lastStep = si;
            ui.no.textContent = STEPS[si].no + ' / 0' + STEPS.length; ui.name.textContent = STEPS[si].name;
            for (var k = 0; k < ui.ticks.length; k++) ui.ticks[k].className = k < si ? 'is-past' : k === si ? 'is-now' : '';
            ui.sweepAt = t;
        }
        // 换步转场（240ms，全部由时钟驱动，不依赖 CSS transition）：
        //   亮线自上而下扫过轨道；当前刻度 0→4px 右移并由灰转黄；上一刻度 4→0px 回位；
        //   步骤标签自下 8px 滑入并淡入（clip 揭示），与官网加载页的逐步揭示同类
        var sa = ui.sweepAt || 0, sw = seg(t, sa, sa + 0.24), sk = easeOutQuad(sw);
        ui.sweep.style.opacity = sw > 0 && sw < 1 ? '1' : '0';
        ui.sweep.style.top = (sw * 100).toFixed(2) + '%';
        var cur = ui.ticks[si], prv = ui.prevStep >= 0 ? ui.ticks[ui.prevStep] : null;
        if (cur) { cur.style.transform = 'translateX(' + (4 * sk).toFixed(2) + 'px)'; cur.style.width = (8 + 6 * sk).toFixed(2) + 'px'; }
        if (prv && prv !== cur) { prv.style.transform = 'translateX(' + (4 * (1 - sk)).toFixed(2) + 'px)'; prv.style.width = (14 - 6 * sk).toFixed(2) + 'px'; }
        ui.step.style.transform = 'translateY(' + (8 * (1 - sk)).toFixed(2) + 'px)';
        ui.stepClip = sk;
        // 打字机字幕：每步开始后按 40 字/秒出字，光标 0.8s 闪烁
        var st = mode === 'lite' ? 0 : STEPS[si].at, cap = STEPS[si].cap;
        var n = Math.min(cap.length, Math.floor((t - st) * 40));
        var txt = cap.slice(0, n);
        if (txt !== lastCap) { lastCap = txt; ui.cap.textContent = txt; ui.cap.appendChild(ui.caret); }
        ui.caret.style.opacity = Math.floor(t / 0.4) % 2 ? '0' : '1';
        var pp = mode === 'lite' ? p : seg(t, T.eject, T.zoom);
        ui.pct.style.opacity = String(mode === 'lite' ? 1 : smooth(seg(t, T.eject - .1, T.eject + .2)) * (1 - smooth(seg(t, T.zoom, T.zoom + .3))));
        ui.pctN.textContent = String(Math.round(pp * 100));
        // 文档卡铺满前，左下字幕与右下编号淡出，避免浅色文字压在浅色纸面上
        var docIn = mode === 'full' ? 1 - smooth(seg(t, T.zoom + 0.4, T.cover)) : 1;
        ui.cap.style.opacity = String(docIn);
        ui.meta.style.opacity = String(docIn);
        // 跳过按钮 0.8s 出现、0.85s 获得焦点，都按动画时钟（资源加载期间不计时，避免焦点落在还不可见的按钮上）。
        // 文档铺满后（纸面是浅色）换成浅色按钮；左上步骤标签同时淡出
        var onPaper = mode === 'full' && t >= T.cover - 0.2;
        ui.skip.className = 'btn ' + (onPaper ? 'btn-line' : 'btn-line-dark') + ' fi-skip' + (t > 0.8 ? ' is-on' : '');
        if (!skipFocused && t > 0.85 && skipAt < 0) { skipFocused = true; try { ui.skip.focus({ preventScroll: true }); } catch (e) { ui.skip.focus(); } }
        ui.step.style.opacity = String(docIn * (0.25 + 0.75 * (ui.stepClip === undefined ? 1 : ui.stepClip)));
    }

    function renderExit(t, a) {
        // a：退场开始时刻。正常播放 a = T.push；跳过时 a = 跳过时刻，并走短版（时长压缩到 0.5s 左右）
        // 简版（减少动态效果）也走短版：不做标题推入，不做 5s 漂移
        var short = skipAt >= 0 || mode === 'lite', k = short ? 0.42 : 1;
        var cover = easeOutQuad(seg(t, a, a + 0.4 * k));                    // 深色斜纹层自右推入：.4s ease
        var titleIn = easeOutQuad(seg(t, a + 0.2 * k, a + 0.5 * k));         // 标题自右 2rem 推入：.3s，晚 .2s
        var push = easeInOutCubic(seg(t, a + (short ? 0.18 : 0.75), a + (short ? 0.5 : 1.25)));   // 整层向左推出
        ui.divider.style.transform = 'translateX(' + (100 * (1 - cover) - 100 * push).toFixed(3) + '%)';
        ui.divTitle.style.opacity = short ? '0' : String(titleIn * (1 - push));
        ui.divTitle.style.transform = 'translateX(' + (2 * (1 - titleIn)).toFixed(3) + 'rem)';
        // 推出的同时，开场层其余部分一起向左推出，欢迎页从右侧推入
        var shift = (-100 * push).toFixed(3) + '%';
        ui.stage.forEach(function (e) { e.style.transform = 'translateX(' + shift + ')'; });
        if (push > 0 && document.documentElement.classList.contains('intro-pending')) {
            // 这一刻定义为「进入主界面」：主界面已可见、可点击（开场层 pointer-events: none）
            window.__fishIntroEnteredAt = performance.now();
            document.documentElement.classList.remove('intro-pending');
            welcomeTargets = collectWelcome();
            root.classList.add('is-leaving');
        }
        if (welcomeTargets) {
            ui.uStart = a + (short ? 0.18 : 0.75);
            // 跳过 / 简版时欢迎页入场整体提速 3.5 倍（约 0.33s 走完）
            var u = (t - ui.uStart) * (short ? 3.5 : 1);
            welcomeTargets.page.style.transform = push < 1 ? 'translateX(' + (100 * (1 - push)).toFixed(3) + '%)' : '';
            poseWelcome(welcomeTargets, u);
            root.style.background = push > 0 ? 'transparent' : '';
            if (push >= 1) ui.divider.style.visibility = 'hidden';
        }
        // 结束时刻 = 入场最后一项（其余区块，u = 1.15）完成：
        //   正常播放 a + 0.75 + 1.15 + 0.05 = 7.95s；
        //   跳过 / 简版入场提速 3.5 倍：a + 0.18 + 1.15 / 3.5 ≈ a + 0.51s，入场在开场层移除前真正走完，不会跳变
        return push >= 1 && t > a + (short ? 0.18 + 1.15 / 3.5 + 0.01 : 0.75 + 1.15 + 0.05);
    }

    function frame() {
        raf = 0;
        if (state !== 'playing') return;
        // 控制器接手后 head 里的 10s 兜底已取消：任何一帧抛错都立即收尾，不让遮罩停在主界面上
        try { step(); } catch (e) { abort(e); }
    }

    function abort(err) {
        if (window.console && console.error) console.error('[FishIntro] 开场动画异常，已直接进入主界面：', err);
        try { finish(); } catch (e) { /* finish 自身出错时由下面的强制清理兜底 */ }
        state = 'done';
        if (raf) { cancelAnimationFrame(raf); raf = 0; }
        document.documentElement.classList.remove('intro-pending');
        window.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', layoutDoc);
        var el = document.getElementById('fish-intro');
        if (el && el.parentNode) el.parentNode.removeChild(el);
        root = null;
        // 推移中途出错时把欢迎页与主区的内联位移还原，主界面停在正常状态
        try {
            clearWelcome(welcomeTargets);
            if (welcomeTargets) { welcomeTargets.page.style.transform = ''; if (welcomeTargets.main) welcomeTargets.main.style.overflowX = ''; }
        } catch (e) { /* 只做尽力还原，不再向外抛 */ }
    }

    function step() {
        var t = now();
        if (mode === 'full') {
            // 3D 卡片铺满后切到 2D 文档层（同一份色块版式），之后 3D 不再渲染，省电
            if (s3d && t < T.cover + 0.04) {
                var pf0 = PERF ? performance.now() : 0;
                poseScene(s3d, Math.min(t, T.cover)); s3d.renderer.render(s3d.scene, s3d.camera);
                if (PERF) perfFrame(t, performance.now() - pf0);
            }
            ui.canvas.style.opacity = String(smooth(seg(t, 0, 0.5)));
            ui.doc.style.opacity = t >= T.cover ? '1' : '0';
        } else {
            // 简版：无 3D、无扫描、无擦除；只有进度条与字幕，结束时同样向左推移
            ui.doc.style.opacity = '0';
        }
        renderUI(t);
        var a = skipAt >= 0 ? skipAt : (mode === 'lite' ? LITE_END : T.push);
        var done = t >= a ? renderExit(t, a) : false;
        if (done || t > 10) { finish(); return; }
        raf = requestAnimationFrame(frame);
    }

    function layoutDoc() {
        // 2D 文档层：16:10 等比 cover 铺满视口，与 3D 铺满时的取景一致
        var vw = window.innerWidth, vh = window.innerHeight, r = DOC_W / DOC_H, w, h;
        if (vw / vh > r) { w = vw; h = vw / r; } else { h = vh; w = vh * r; }
        ui.docInner.style.width = w + 'px'; ui.docInner.style.height = h + 'px';
        ui.docInner.style.left = ((vw - w) / 2) + 'px'; ui.docInner.style.top = ((vh - h) / 2) + 'px';
        if (s3d) s3d.resize();
    }

    function skip() {
        if (state !== 'playing' || skipAt >= 0) return;   // 幂等：Enter/空格会同时触发 keydown 与 click
        // 资源还在加载（时钟未启动）时按下跳过：记下来，时钟启动的同一帧直接进入短版退场
        if (!clockOn) { pendingSkip = true; return; }
        var t = now();
        var a = mode === 'lite' ? LITE_END : T.push;
        if (t >= a) return;                                // 已在退场中
        skipAt = t;
    }

    function onKey(e) {
        if (state !== 'playing') return;
        // 进入退场（跳过后或推移开始后）主界面已可操作，不再拦截 Enter / 空格 / Esc
        if (skipAt >= 0 || (clockOn && now() >= (mode === 'lite' ? LITE_END : T.push))) return;
        if (e.key === 'Escape' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); skip(); }
    }

    function finish() {
        if (state === 'done') return;
        state = 'done';
        if (raf) cancelAnimationFrame(raf);
        clearTimeout(window.__fishIntroGate);
        markPlayed();
        document.documentElement.classList.remove('intro-pending');
        window.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', layoutDoc);
        if (welcomeTargets) {
            var w = welcomeTargets, u0 = now() - (ui.uStart || now());
            if (w.main) w.main.style.overflowX = '';
            w.page.style.transform = '';
            // 插画的 5s 长尾漂移在开场层移除后继续由一个轻量循环推进（只写一个 transform），跳过时直接归位
            if (skipAt < 0 && mode === 'full' && u0 < 5.3) {
                var tb = performance.now() - u0 * 1000;
                (function tail() {
                    tailRaf = 0;
                    var u = (performance.now() - tb) / 1000;
                    var il = easeExpoOut(seg(u, 0.30, 5.30));
                    setX(w.illus, il < 1 ? (15 * (1 - il)).toFixed(3) + 'rem' : '', 1);
                    if (u < 5.3) tailRaf = requestAnimationFrame(tail);
                })();
                poseWelcome(w, 99); setX(w.illus, (15 * (1 - easeExpoOut(seg(u0, .3, 5.3)))).toFixed(3) + 'rem', 1);
                [].concat(w.deco, w.title, w.rest, [w.side, w.top]).forEach(function (e) { if (e) { e.style.transform = ''; e.style.opacity = ''; } });
            } else { poseWelcome(w, 99); clearWelcome(w); }
            // 欢迎页的 rise 入场在开场期间被关掉；用户第一次切页时恢复，回到欢迎页时 rise 照常播放
            var nav0 = window.navigateTo;
            if (typeof nav0 === 'function' && !nav0.__introWrapped) {
                var wrapped = function () { w.page.removeAttribute('data-intro-entered'); window.navigateTo = nav0; return nav0.apply(this, arguments); };
                wrapped.__introWrapped = true; window.navigateTo = wrapped;
            }
        }
        if (s3d) { s3d.dispose(); s3d = null; }
        if (root && root.parentNode) root.parentNode.removeChild(root);
        root = null;
        // 焦点：重播时还给触发它的按钮；首次自动播放给欢迎页导航项
        var back = returnFocus && document.body.contains(returnFocus) ? returnFocus : document.querySelector('.nav-item[data-page="welcome"]');
        returnFocus = null;
        if (back && back.focus) back.focus();
    }

    // 对外入口：启动阶段（建 DOM、注册监听）出错同样立即收尾
    function start(forceMode) {
        try { begin(forceMode); } catch (e) { abort(e); }
    }

    function begin(forceMode) {
        if (state === 'playing') return;
        mode = forceMode || forcedMode() || (reducedMotion() ? 'lite' : 'full');
        if (!document.documentElement.classList.contains('intro-pending')) document.documentElement.classList.add('intro-pending');
        // 每次都新建开场层：残留节点先移除，下面 ui 里的查找结果一定存在
        var stale = document.getElementById('fish-intro');
        if (stale && stale.parentNode) stale.parentNode.removeChild(stale);
        root = build();
        ui = {
            canvas: root.querySelector('.fi-canvas'), fill: root.querySelector('.fi-fill'), sweep: root.querySelector('.fi-sweep'),
            ticks: root.querySelectorAll('.fi-ticks i'), no: root.querySelector('.fi-step-no'), name: root.querySelector('.fi-step-name'),
            cap: root.querySelector('.fi-caption'), caret: document.createElement('i'), pct: root.querySelector('.fi-pct'),
            pctN: root.querySelector('.fi-pct b'), skip: root.querySelector('.fi-skip'), doc: root.querySelector('.fi-doc'), meta: root.querySelector('.fi-meta'),
            step: root.querySelector('.fi-step'), tickIs: [],
            docInner: root.querySelector('.fi-doc-inner'), divider: root.querySelector('.fi-divider'), divTitle: root.querySelector('.fi-divider-title')
        };
        ui.stage = [].slice.call(root.children).filter(function (e) { return e !== ui.divider; });
        layoutDoc();
        ui.skip.addEventListener('click', skip);
        root.addEventListener('click', function (e) { if (e.target !== ui.skip) skip(); });
        window.addEventListener('keydown', onKey, true);
        window.addEventListener('resize', layoutDoc);
        state = 'playing'; skipAt = -1; lastStep = -1; lastCap = ''; welcomeTargets = null; ui.prevStep = -1;
        clockOn = false; pendingSkip = false; skipFocused = false;
        // 开场控制器已接手：取消 head 里的 10s 兜底（它只用于本脚本加载失败的情况），超时改由 frame() 内 t > 10 的看门狗负责
        clearTimeout(window.__fishIntroGate);
        // 上一次播放留下的测绘环长尾漂移循环，在新一次开始前停掉
        if (tailRaf) { cancelAnimationFrame(tailRaf); tailRaf = 0; }
        // 调试：从指定秒数开始（截图、逐帧对照用）；重播总是从头开始，且不受 intro_hold 冻结
        var seek = replaying ? 0 : (Number(params().get('intro_t')) || 0);
        if (replaying) HOLD = false;
        function go() {
            t0 = performance.now(); clockBase = seek; clockOn = true;
            if (pendingSkip) skipAt = seek;
            raf = requestAnimationFrame(frame);
        }
        if (mode === 'lite') { go(); return; }
        // 完整版：先按需加载 three.js 与 LOGO，1.5s 内没准备好就降级为简版，不让用户对着黑屏等
        var gave = false;
        var guard = setTimeout(function () { if (!gave) { gave = true; mode = 'lite'; go(); } }, 1500);
        var need = [];
        if (typeof THREE === 'undefined') need.push(loadScript(ASSET.three));
        if (!window.FISH_LOGO) need.push(loadScript(ASSET.logo));
        Promise.all(need).then(function () { return loadImage(window.FISH_LOGO); }).then(function (img) {
            if (gave) return;
            gave = true; clearTimeout(guard);
            logoImg = img;
            try { s3d = makeScene(ui.canvas, logoImg); } catch (e) { s3d = null; mode = 'lite'; }
            go();
        }).catch(function () { if (!gave) { gave = true; clearTimeout(guard); mode = 'lite'; go(); } });
    }

    function shouldAutoPlay() { return document.documentElement.classList.contains('intro-pending'); }

    // 调试：文档卡四角投影到屏幕的像素坐标，用来核对「卡片不变形」「恰好铺满」
    function probe(tq) {
        if (!s3d) return null;
        var c = s3d.card, cam = s3d.camera, vw = window.innerWidth, vh = window.innerHeight, pts = [];
        // 不依赖渲染循环（后台标签页里 rAF 会暂停）：按给定时刻直接摆姿态并更新矩阵
        if (typeof tq === 'number') poseScene(s3d, Math.min(tq, T.cover));
        s3d.scene.updateMatrixWorld(true);
        cam.updateMatrixWorld(true);
        c.updateMatrixWorld(true);
        [[-1, -1], [1, -1], [1, 1], [-1, 1]].forEach(function (k) {
            var v = new THREE.Vector3(k[0] * CARD_W / 2, k[1] * CARD_H / 2, 0.006).applyMatrix4(c.matrixWorld).project(cam);
            pts.push([Math.round((v.x + 1) / 2 * vw), Math.round((1 - v.y) / 2 * vh)]);
        });
        var ws = new THREE.Vector3(); c.getWorldScale(ws);
        var xs = pts.map(function (p) { return p[0]; }), ys = pts.map(function (p) { return p[1]; });
        var dx1 = Math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]), dy1 = Math.hypot(pts[3][0] - pts[0][0], pts[3][1] - pts[0][1]);
        return { t: typeof tq === 'number' ? tq : Math.round(now() * 100) / 100, corners: pts, screenRatio: Math.round(dx1 / dy1 * 1000) / 1000, worldScale: [ws.x, ws.y, ws.z].map(function (x) { return Math.round(x * 1000) / 1000; }),
                 bbox: [Math.min.apply(0, xs), Math.min.apply(0, ys), Math.max.apply(0, xs), Math.max.apply(0, ys)], viewport: [vw, vh],
                 cardVsCase: Math.round(CARD_W / CASE_W * 100) + '% x ' + Math.round(CARD_H / CASE_H * 100) + '%' };
    }

    // 重播：欢迎页角落的按钮调用。切回欢迎页并滚到顶部，从头播放；结束后焦点回到按钮
    // 按住 Shift 点击（或传 'full'）可在「减少动态效果」开启时强制看完整版
    function replay(trigger, forceMode) {
        if (state === 'playing') return;
        if (trigger && trigger.type === 'click') { forceMode = trigger.shiftKey ? 'full' : forceMode; trigger = trigger.currentTarget; }
        returnFocus = trigger && trigger.focus ? trigger : document.activeElement;
        replaying = true;
        var mc = document.querySelector('.main-content');
        if (typeof window.navigateTo === 'function') window.navigateTo('welcome');
        if (mc) mc.scrollTop = 0;
        start(forceMode === 'full' || forceMode === 'lite' ? forceMode : (reducedMotion() ? 'lite' : 'full'));
    }

    // 重播按钮的键盘聚焦样式（代替 Chromium 83 不支持的 :focus-visible）：最近一次输入是 Tab 键时才加 .is-kbd
    (function () {
        var kbd = false;
        document.addEventListener('keydown', function (e) { if (e.key === 'Tab') kbd = true; }, true);
        document.addEventListener('mousedown', function () { kbd = false; }, true);
        document.addEventListener('focusin', function (e) {
            if (e.target && e.target.classList && e.target.classList.contains('intro-replay') && kbd) e.target.classList.add('is-kbd');
        });
        document.addEventListener('focusout', function (e) {
            if (e.target && e.target.classList && e.target.classList.contains('intro-replay')) e.target.classList.remove('is-kbd');
        });
    })();

    window.FishIntro = { start: start, skip: skip, replay: replay, _state: function () { return state; }, _probe: probe,
                         _perf: function () { return perfLog; } };
    if (shouldAutoPlay()) {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { start(); });
        else start();
    }
})();
