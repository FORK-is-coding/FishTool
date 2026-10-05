"""
热点信号采集器。

职责边界：
- 拉取指定分区排行榜（ranking/v2），对榜内视频逐个采集 view 快照；
- 按采样策略采集热评与弹幕内容，提取高频词作为辅助信号；
- 将标题 / 评论 / 弹幕 / 增速信号写入 hotspot_signal 表，快照写入 videos / video_stats；
- 全程受风控预算控制，并对外暴露分页批次进度供前端轮询。

边界约定：
- 采集器不感知生命周期算法，只负责把原始信号落库；
- 展示层进度轮询统一读取 get_progress()，不直接触碰内部状态。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Any

from bilibili.api import BilibiliAPI
from core.data_quality import utc_now_epoch_s
from core.database import Video, VideoStats, get_session
from core.logger import get_logger
from modules.comment.collector import CommentCollector
from modules.comment.sentiment import SentimentAnalyzer
from modules.hotspot.snapshot_store import persist_snapshot

from .risk_control import LogicalAdmission, RequestBudget, validated_issuer
from .signal_store import HotspotSignalStore
from .tag_cloud import TagCloudGenerator

logger = get_logger(__name__)


# 模块级进度状态：单进程内唯一，前端轮询 collect/progress 读取。
# status: idle / running / completed / failed
_progress: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "message": "尚未开始采集",
    "estimate_seconds": None,
    "failed_items": [],
    "updated_at": None,
}


def get_progress() -> dict[str, Any]:
    """返回当前采集进度的浅拷贝，供 API 层序列化。"""
    return dict(_progress)


def _set_progress(status: str, progress: int, message: str, estimate_seconds: float | None = None, failed_items: list[dict[str, str]] | None = None) -> None:
    """更新模块级采集进度，并记录更新时间。

    Args:
        status: 状态标识（idle/running/completed/failed）。
        progress: 0-100 的整数进度。
        message: 面向用户的进度描述。
        estimate_seconds: 预估剩余秒数，未知时为 None。
        failed_items: 失败视频明细，供前端"重试/查看失败"交互使用。
    """
    _progress.update(
        status=status,
        progress=max(0, min(100, int(progress))),
        message=message,
        estimate_seconds=round(estimate_seconds) if estimate_seconds is not None else None,
        failed_items=failed_items or [],
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )


class HotspotCollector:
    """热点信号采集器，负责榜单 -> 快照 -> 信号落库的完整链路。"""

    def __init__(
        self,
        api: BilibiliAPI,
        budget: RequestBudget | None = None,
        store: HotspotSignalStore | None = None,
    ) -> None:
        """初始化采集器。

        Args:
            api: 已初始化的 BilibiliAPI 客户端（含 cookie 池与 WBI 签名）。
            budget: 可选的请求预算器，缺省时新建默认预算（20/300/3000）。
            store: 信号存储服务，缺省时新建默认实例。
        """
        self.api = api
        # 显式 ``is not None`` 判断（不用真值判断）：测试替身 / 自定义 budget 若定义 falsey
        # （如 __bool__ 返回 False），也不会被意外替换成一个新默认预算（07 执行案 §8.1）。
        self.budget = budget if budget is not None else RequestBudget()
        self.store = store or HotspotSignalStore()
        # 复用项目现有评论采集器：自带 4 秒限频、429 退避与按 rpid 去重落库。
        self.comment_collector = CommentCollector(api=api)
        # 复用现有情感分析器的关键词提取（2-4 字中文子串词频），避免重复实现。
        self.sentiment = SentimentAnalyzer()

    # ---------- 对外入口 ----------

    async def collect(
        self,
        tid: int,
        limit: int = 20,
        min_view: int = 0,
        sample_comments: bool = True,
        sample_danmaku: bool = True,
    ) -> dict[str, Any]:
        """执行一轮完整采集。

        增量策略：
        - 榜单支持分页：limit<=50 拉一页；limit>50 时按 50/页翻页补齐；
        - 评论/弹幕采样只针对"本次首次入库的新视频"，老视频不重复采样，
          避免每天重复请求打穿单 cookie 预算；
        - 单视频失败不中断整轮，失败明细随结果返回供前端展示。

        Args:
            tid: 目标一级分区 ID（新 pid_v2 体系：游戏=1008、动画=1005 等）。
            limit: 榜单采集数量上限，可超过 50 触发翻页，控制在 1-200。
            min_view: 仅绘画分区生效，搜索主采播放量下限，低于该值的视频跳过。
            sample_comments: 是否采样热评并提取关键词。
            sample_danmaku: 是否采样弹幕内容并提取关键词。

        Returns:
            采集结果统计：处理的视频数、成功数、失败数、信号落库数、
            失败明细、本次新增视频数等。

        Raises:
            任何 B 站接口异常会向上抛出，由 API 层转为用户可读错误。
        """
        started = time.monotonic()
        _set_progress("running", 2, "准备拉取分区榜单")
        try:
            # 绘画新 pid_v2=1006 的 ranking/v2 榜单返回 2021~2025 老数据（7天窗口滤光），
            # 仍走方案 C（搜索主采 + 官号补漏）。
            if tid == TagCloudGenerator.PAINT_TID:
                _set_progress("running", 3, "绘画分区走方案C：搜索主采 + 重点画师账号补漏")
                paint_gen = TagCloudGenerator(self.api)
                videos = await paint_gen.get_paint_videos(limit, min_view=min_view)
                source_mark = "paint_c"
            else:
                videos = await self._fetch_ranking_paged(tid, limit)
                source_mark = "ranking"
                # 非绘画区统一应用 7 天时间窗过滤（与绘画区口径一致）：
                # ranking/v2 榜单可能混入超窗老视频，采集层先按 pubdate 硬过滤，
                # 避免 2025 等过期内容进入生命周期卡片。
                window_days = getattr(TagCloudGenerator, "PAINT_WINDOW_DAYS", 7)
                cutoff = int((datetime.now() - timedelta(days=window_days)).timestamp())
                raw_count = len(videos)
                videos = [
                    v for v in videos
                    if TagCloudGenerator._published_timestamp(v) >= cutoff
                ]
                if raw_count and len(videos) < raw_count:
                    logger.warning(
                        "分区 tid=%s 榜单 %s 条中超 7 天时间窗 %s 条，已过滤；若过滤后为空，说明该分区榜单当前无 7 天内数据",
                        tid, raw_count, raw_count - len(videos),
                    )
            total = len(videos)
            logger.info("热点采集开始: tid=%s, 榜单视频数=%s", tid, total)
            if total == 0:
                _set_progress("completed", 100, "榜单为空，没有可采集的视频")
                return {"total": 0, "ok": 0, "failed": 0, "signals": 0, "elapsed_seconds": 0}

            # 当天已有快照的 bvid 视为老视频，只做 view 快照、不重复采样评论/弹幕。
            existing = self._existing_bvids_today()
            # 本轮采集批次标识：同一轮所有视频共用，便于按批次追溯样本来源。
            run_id = f"{datetime.now():%Y%m%dT%H%M%S}_{source_mark}_{tid}"

            ok = 0
            failed = 0
            signal_count = 0
            failed_items: list[dict[str, str]] = []
            for index, item in enumerate(videos, start=1):
                # 分页批次进度：10% 起按视频逐个推进到 75%。
                ratio = 10 + (index / total) * 65
                _set_progress("running", ratio, f"正在采集第 {index}/{total} 个视频")
                try:
                    # 单视频采集统一走公开入口 collect_one，与整榜共用同一份逻辑。
                    saved = await self.collect_one(
                        item.get("bvid") or "",
                        collection_tid=tid,
                        source=source_mark,
                        run_id=run_id,
                    )
                    signal_count += saved
                    ok += 1
                except Exception as exc:
                    failed += 1
                    failed_items.append({"bvid": str(item.get("bvid") or ""), "error": str(exc)})
                    logger.warning("视频采集失败 %s: %s", item.get("bvid"), exc)
                    # 单视频失败不中断整轮采集，由展示层提示部分失败。

            # 只对本次新增视频做评论/弹幕采样，老视频跳过以节省预算。
            fresh_videos = [item for item in videos if str(item.get("bvid") or "") not in existing]
            comment_keywords: list[tuple[str, int]] = []
            danmaku_keywords: list[tuple[str, int]] = []
            if sample_comments and fresh_videos:
                _set_progress("running", 80, "正在采样新增视频热评并提取关键词")
                comment_keywords = await self._sample_comments(fresh_videos)
                signal_count += self._save_signal(
                    source="comment",
                    tid=tid,
                    payload={"keywords": [k for k, _ in comment_keywords]},
                )
            if sample_danmaku and fresh_videos:
                _set_progress("running", 90, "正在采样新增视频弹幕并提取关键词")
                danmaku_keywords = await self._sample_danmaku(fresh_videos)
                signal_count += self._save_signal(
                    source="danmaku",
                    tid=tid,
                    payload={"keywords": [k for k, _ in danmaku_keywords]},
                )

            elapsed = time.monotonic() - started
            _set_progress(
                "completed",
                100,
                f"采集完成：成功 {ok} 个，失败 {failed} 个，新增 {len(fresh_videos)} 个，信号 {signal_count} 条，耗时 {elapsed:.0f}s",
                failed_items=failed_items,
            )
            return {
                "total": total,
                "ok": ok,
                "failed": failed,
                "signals": signal_count,
                "fresh_count": len(fresh_videos),
                "failed_items": failed_items,
                "elapsed_seconds": round(elapsed, 1),
            }
        except Exception as exc:
            _set_progress("failed", 0, f"采集失败：{exc}")
            logger.error("热点采集异常: %s", exc)
            raise

    async def collect_one(
        self,
        bvid: str,
        *,
        collection_tid: int | None = None,
        source: str = "ranking",
        run_id: str | None = None,
        logical_admission: LogicalAdmission | None = None,
    ) -> int:
        """采集单个视频的 view 快照并落库（公开单视频入口）。

        本方法是 ``collect()`` 内层循环对**单个视频**所做两步动作的等价抽取：
        ``_fetch_view``（拉详情，含预算 / 限频）→ ``_save_snapshot``（落
        ``videos`` / ``video_stats`` 并写 title 信号）。``collect()`` 反过来调用本方法，
        保证「一份逻辑两个入口」，避免整榜与单视频两条路径随时间漂移。

        边界（对齐模块开篇「采集器不感知生命周期算法」）：本方法只做「采一条快照」，
        不出现任何算法 / 阶段 / watch 概念，参数只含 bvid、归属分区与来源标签；
        ``run_id`` 是采集批次标识（非算法概念），仅用于与整榜采集保持同一批次血缘。

        Args:
            bvid: 视频 BV 号。
            collection_tid: 该视频归属的采集分区 ID（详情接口的 tid 可能是二级分区，
                生命周期列表按一级分区筛选时必须用这个归属 ID），可为 None。
            source: 快照来源标签（如 ranking / paint_c / watch）。
            run_id: 采集批次标识；缺省 None 表示单条独立采集，不并入整榜批次。
            logical_admission: 可选的单请求准入凭证（07 执行案 §8.1）。watch 编排层在
                逐目标 ``reserve`` 后由 ``collect_admitted`` 透传进来；直接调用本入口
                （独立采集 / 整榜）时为 None，仍走原 ``acquire()`` 扣费。

        Returns:
            ``_save_snapshot`` 返回的信号条数（int）。

        Raises:
            异常: 详情拉取 / 落库失败时原样上抛，由上层决定失败隔离口径。
        """
        view_data = await self._fetch_view(
            {"bvid": str(bvid or "").strip()}, logical_admission=logical_admission
        )
        return await self._save_snapshot(
            view_data,
            source=source,
            collection_tid=collection_tid,
            run_id=run_id,
        )

    # ---------- 数据获取 ----------

    async def _fetch_ranking_paged(self, tid: int, limit: int) -> list[dict[str, Any]]:
        """分页拉取分区榜单并合并去重。

        ranking/v2 单页最多 50 条，limit 超过 50 时按 50/页翻页补齐；
        翻页受预算控制，超预算时按已获取部分返回，不阻塞整轮采集。

        Args:
            tid: 分区 ID。
            limit: 目标采集条数上限。

        Returns:
            合并去重后的榜单视频列表（按榜单顺序，去重保留先出现者）。
        """
        limit = max(1, min(int(limit), 200))
        pages = (limit + 49) // 50
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            try:
                ranking = await self._fetch_ranking(tid, page=page)
            except Exception as exc:
                # 翻页失败时保留已获取数据，避免一页失败全盘放弃。
                logger.warning("榜单第 %s 页拉取失败: %s", page, exc)
                break
            for item in (ranking.get("list") or []):
                bvid = str(item.get("bvid") or "")
                if bvid and bvid in seen:
                    continue
                seen.add(bvid)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        return merged

    async def _fetch_ranking(self, tid: int, page: int = 1) -> dict[str, Any]:
        """拉取分区日榜，等待预算后再请求。

        Args:
            tid: 分区 ID。
            page: 页码，从 1 开始，单页最多 50 条。

        Returns:
            含 list 字段的榜单数据。
        """
        await self.budget.acquire()
        data = await self.api.get_ranking(rid=tid, day=1, original=0, page=page)
        return data.get("data") or {}

    async def _fetch_view(
        self, item: dict[str, Any], *, logical_admission: LogicalAdmission | None = None
    ) -> dict[str, Any]:
        """按 BV 号拉取视频详情，返回含 stat/owner/tid 的 data。

        Args:
            item: 含 ``bvid`` 的最小请求载体。
            logical_admission: 可选单请求准入凭证。传入时用 ``issuer.redeem(...)`` 把
                同一份 L 层票据兑换一次，**不再**调用 ``budget.acquire()``（避免同一次
                逻辑操作被扣两次）；为 None 时保留原 ``acquire()`` 行为（独立采集照旧扣费）。
                这里**不**凭 ``source == 'watch'`` 跳闸——source 只是来源标签，不是已扣费的证明。
        """
        bvid = item.get("bvid") or ""
        if logical_admission is None:
            await self.budget.acquire()
        else:
            # 受控取发行方：不接受任意「有 redeem 属性」的伪凭证。
            issuer = validated_issuer(logical_admission)
            # 只把这一条 reservation 变 committed，绝不追加第二条总量消费。
            issuer.redeem(logical_admission, operation_key=bvid, now_mono=issuer.clock())
        data = await self.api.get(
            f"{self.api.BASE_URL}/x/web-interface/view",
            params={"bvid": bvid},
            need_sign=False,
        )
        return data or {}

    async def _sample_comments(self, videos: list[dict[str, Any]]) -> list[tuple[str, int]]:
        """采样前若干个视频的热评，汇总提取高频词。

        复用 CommentCollector 的快速策略（仅热评，自带限频与去重落库），
        不进行全量翻页，避免请求量打穿预算；关键词提取复用 SentimentAnalyzer。
        """
        all_comments: list[dict[str, Any]] = []
        for item in videos[:5]:
            bvid = item.get("bvid") or ""
            if not bvid:
                continue
            try:
                await self.budget.acquire()
                comments = await self.comment_collector.collect_video_comments(
                    bvid, strategy=CommentCollector.STRATEGY_FAST
                )
                all_comments.extend(comments)
            except Exception as exc:
                logger.warning("热评采样失败 %s: %s", bvid, exc)
        return self.sentiment.extract_keywords(all_comments, top_n=10)

    async def _sample_danmaku(self, videos: list[dict[str, Any]]) -> list[tuple[str, int]]:
        """采样弹幕内容。

        使用 /x/v1/dm/list.so 老 XML 接口（无需 protobuf 解析），
        返回普通弹幕池中的文本，按词频提取高频词作为辅助信号。
        """
        texts: list[str] = []
        for item in videos[:5]:
            aid = item.get("aid") or 0
            if not aid:
                continue
            try:
                await self.budget.acquire()
                await self.api.init_session()
                async with self.api.session.get(
                    f"{self.api.BASE_URL}/x/v1/dm/list.so",
                    params={"oid": aid},
                    timeout=15,
                ) as resp:
                    if resp.status != 200:
                        continue
                    raw = await resp.text()
                    # XML <d p="...">弹幕文本</d>，直接正则提取文本节点。
                    texts.extend(re.findall(r"<d[^>]*>(.*?)</d>", raw)[:50])
            except Exception as exc:
                logger.warning("弹幕采样失败 aid=%s: %s", aid, exc)
        # 弹幕文本与评论结构不同，包装成 content 字段后复用同一关键词提取器。
        return self.sentiment.extract_keywords(
            [{"content": text} for text in texts], top_n=10
        )

    # ---------- 落库 ----------

    def _existing_bvids_today(self) -> set[str]:
        """查询当天已有快照的 bvid 集合，用于增量采样判断。

        Returns:
            当天已入库视频的 bvid 集合；查询失败时返回空集合（保守视为全新增）。
        """
        session = get_session()
        try:
            start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            rows = (
                session.query(Video.bvid)
                .join(VideoStats, Video.id == VideoStats.video_id)
                .filter(VideoStats.snapshot_time >= start)
                .all()
            )
            return {bvid for (bvid,) in rows if bvid}
        except Exception as exc:
            logger.warning("查询今日已有快照失败: %s", exc)
            return set()
        finally:
            session.close()

    # 需要判定质量的统计字段：播放量与六项互动量同源返回，待遇必须一致。
    _STAT_KEYS = ("view", "danmaku", "reply", "favorite", "coin", "share", "like")

    @staticmethod
    def _read_stat_int(stat: dict[str, Any], key: str) -> int | None:
        """读取接口统计值，仅非负整数视为有效。

        Args:
            stat: view 接口返回的 stat 字典。
            key: 字段名。

        Returns:
            有效时返回整数；缺失、布尔或非法字符串返回 ``None``，
            避免把“没拿到”静默当成真实 0。
        """
        raw = stat.get(key)
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int) and raw >= 0:
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
        return None

    @classmethod
    def _classify_stat_quality(cls, stat: dict[str, Any]) -> tuple[str, str]:
        """判定一条统计记录的完整度。

        Args:
            stat: view 接口返回的 stat 字典。

        Returns:
            ``(view_status, stat_status)``；前者为 ok/missing，
            后者为 ok/partial/missing。缺失一律不得视为真实 0。
        """
        missing = [key for key in cls._STAT_KEYS if cls._read_stat_int(stat, key) is None]
        view_status = "missing" if "view" in missing else "ok"
        if not missing:
            stat_status = "ok"
        elif len(missing) == len(cls._STAT_KEYS):
            stat_status = "missing"
        else:
            stat_status = "partial"
        return view_status, stat_status

    async def _save_snapshot(
        self,
        view_data: dict[str, Any],
        source: str,
        collection_tid: int | None = None,
        run_id: str | None = None,
    ) -> int:
        """将 view 详情写入 videos / video_stats，并返回写入的信号条数。

        Args:
            view_data: B站视频详情响应中的 data 字典。
            source: 本轮采集来源标记。
            collection_tid: 用户选择的采集分区 ID。详情接口的 tid 可能是
                二级分区，生命周期列表按一级分区筛选时必须使用该归属 ID。

        Returns:
            成功写入的标题信号数量，失败时由上层统一处理异常。
        """
        bvid = str(view_data.get("bvid") or "").strip()
        if not bvid:
            return 0
        captured_epoch_s = utc_now_epoch_s()
        session = get_session()
        try:
            # 完整写入内核在自有事务内完成；成功后再单独落标题信号。
            with session.begin():
                row = persist_snapshot(
                    session,
                    view_data,
                    source=source,
                    run_id=run_id,
                    captured_epoch_s=captured_epoch_s,
                    collection_tid=collection_tid,
                )
            video = session.query(Video).filter(Video.id == row.video_id).first()
            signal_tid = int((video.tid if video is not None else 0) or 0)
            self._save_signal(
                source="title",
                tid=signal_tid,
                payload={
                    "bvid": bvid,
                    "title": (video.title if video is not None else "") or "",
                    "view": (video.view if video is not None else None),
                },
            )
            return 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _save_signal(self, source: str, tid: int, payload: dict[str, Any]) -> int:
        """写入一条 hotspot_signal 记录，返回成功条数（0 或 1）。"""
        if not payload:
            return 0
        self.store.save_many([
            {
                "tid": tid,
                "bvid": str(payload.get("bvid") or ""),
                "source": source,
                "value": payload,
            }
        ])
        return 1
