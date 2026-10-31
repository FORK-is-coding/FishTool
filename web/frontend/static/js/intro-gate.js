/* 开场动画首屏判断：在 <head> 同步执行，决定是否先遮住主界面，避免主界面闪一帧。
   规则：?intro=full|lite 必播；?intro=off 或截图模式 ?shot=1 不播；
   否则只在桌面端（desktop/main_window.py 在 DocumentCreation 阶段注入 window.__FISHTOOL_DESKTOP__）
   且 localStorage 没有已播放标记时才播，普通浏览器访问默认不播。
   10s 兜底：app.intro.js 加载失败时也会移除遮罩，主界面始终可用。
   本文件先于 app.core.js 加载，不能调用其中的任何函数；只用 ES2019 语法。 */
(function () {
    try {
        var p = new URLSearchParams(location.search.replace(/;/g, '&')), f = p.get('intro'), play;
        if (f === 'full' || f === 'lite') play = true;
        else if (f === 'off' || p.get('shot') === '1') play = false;
        else if (!window.__FISHTOOL_DESKTOP__) play = false;
        else { try { play = !window.localStorage.getItem('fishtool.intro.v1'); } catch (e) { play = true; } }
        if (!play) return;
        document.documentElement.classList.add('intro-pending');
        window.__fishIntroGate = setTimeout(function () {
            document.documentElement.classList.remove('intro-pending');
            var b = document.getElementById('fish-intro');
            if (b && b.parentNode) b.parentNode.removeChild(b);
        }, 10000);
    } catch (e) { document.documentElement.classList.remove('intro-pending'); }
})();
