"""01 正确排名：只采排名必需公开资料的采集器（规格 §5.2 / §8）。

设计约束（逐条对应规格）：
- 不跑整套自诊：只请求基础资料 / 粉丝数 / 投稿列表 / 单稿详情，不请求舰长、充电、标签、LLM；
- 消费 03 的 ``get_user_info`` 兼容 wrapper 新增的 ``_meta.field_status`` / ``_meta.source``：
  即使旧兼容 ``data`` 里有 0，只要 meta 标注 missing，就**不得**当成真实粉丝数；
- 粉丝优先取 ``get_user_relation_stat`` 本次明确返回的字段；
- 详情只走 ``/x/web-interface/view?bvid=``，且该端点经 ``api.get`` 返回**已解包 data**（无第二层 data）；
- 详情必须是同一次响应的 ``stat.view``；真实 0 有效，缺失即 missing，绝不回退到投稿列表旧值；
- 选中稿件缺播放 -> ``missing_selected_metrics``，**不许从后面补更好稿件**；
- 每作者必须能证明选稿完整（覆盖到窗口旧边界 / 已取到最新 10 条 / 列表遍历结束），
  否则 ``incomplete_selection``，分页出错、乱序、重复页均不得排名；
- 本模块不做任何排名计算，只产出事实样本。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.request_budget import RequestBudgetExceeded

from .contracts import BenchmarkPolicy, CreatorSample
from .metrics import median_twice

logger = get_logger(__name__)

DAY_S = 86400

#: 详情端点（``api.get`` 返回已解包 data，无第二层）
VIEW_ENDPOINT = '/x/web-interface/view'
#: 榜单端点
RANKING_ENDPOINT = '/x/web-interface/ranking/v2'

#: 候选发现的固定预算（规格 §8）
DEFAULT_DISCOVERY_MAX_PAGES = 10
DEFAULT_DISCOVERY_MAX_LOGICAL_CALLS = 20
DEFAULT_DISCOVERY_DEADLINE_S = 120.0


def _default_clock() -> int:
    """默认时钟：UTC Unix 秒。"""
    return int(time.time())


def _as_int(value: Any) -> Optional[int]:
    """宽松但严格区分类型的整数解析。

    只接受 int / 整数值 float / 纯数字字符串；``None`` 与 ``bool`` 一律视为缺失，
    避免把 ``True`` 当成 1、把 ``None`` 当成 0。

    Args:
        value: 原始字段值。

    Returns:
        Optional[int]: 解析结果；无法安全解析时返回 None。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        body = text[1:] if text[0] in '+-' else text
        if body.isdigit():
            return int(text)
    return None


def _charge_logical(budget: Any) -> None:
    """向逻辑调用预算记一笔（budget 为空时完全不干预）。

    Args:
        budget: 可选预算对象；只要求提供 ``spend_logical()``。

    Returns:
        无。

    Raises:
        RequestBudgetExceeded: 预算对象判定超限时由 ``spend_logical`` 抛出。
    """
    if budget is None:
        return
    spend = getattr(budget, 'spend_logical', None)
    if callable(spend):
        spend()


def _entry_view(item: Any) -> Optional[int]:
    """从投稿列表条目中提取详情前的播放值（仅作列表层参考，不作排名成绩）。

    Args:
        item: 列表条目 dict。

    Returns:
        Optional[int]: 列表层播放值；缺失返回 None。
    """
    if not isinstance(item, dict):
        return None
    return _as_int(item.get('play'))


class RankingProfileCollector:
    """仅采排名必需字段的账号采集器（一个账号一条样本，一人一票）。"""

    def __init__(self, api: Any, request_budget: Any = None, clock: Optional[Callable[[], int]] = None):
        """构造采集器。

        Args:
            api: B 站 API 客户端（复用既有 client / 限频 / Cookie 池，不新建 client）。
            request_budget: 可选逻辑调用预算对象（提供 ``spend_logical()``）；
                HTTP 尝试预算由 ``core.request_budget`` 的 ContextVar 负责，不在此处重复扣数。
            clock: 可注入时钟，返回 UTC Unix 秒；默认取系统时间（便于测试）。
        """
        self.api = api
        self.request_budget = request_budget
        self.clock = clock or _default_clock

    # ------------------------------------------------------------------ 主流程
    async def collect_creator(self, uid: int, policy: BenchmarkPolicy, as_of_s: int) -> CreatorSample:
        """采集单个账号在给定策略下的排名样本（规格 §8 九步）。

        Args:
            uid: 账号 UID。
            policy: 冻结后的排名策略（BenchmarkPolicy）。
            as_of_s: 选稿参考时刻（UTC Unix 秒）。

        Returns:
            CreatorSample: 该账号本轮的事实快照（可能为 valid / insufficient_posts /
            incomplete_selection / missing_selected_metrics / error）。

        Raises:
            RequestBudgetExceeded: 逻辑调用预算或 HTTP 尝试预算耗尽时向上抛出，
                绝不被吞成普通错误后再重试。
        """
        uid = int(uid)
        sample = CreatorSample(uid=uid)
        errors: List[Dict[str, Any]] = sample.errors

        # 步骤 1：基础资料 + 必要粉丝数（meta 优先于兼容 0）
        follower_count, follower_status, name = await self._fetch_profile(uid, errors)
        sample.follower_count = follower_count
        sample.follower_status = follower_status
        sample.name = name

        # 步骤 2-7：投稿列表 + 详情选稿
        picked = await self._collect_selection(uid, policy, as_of_s, errors)

        sample.selected_videos = picked['selected_videos']
        sample.selected_count = len(picked['selected_videos'])
        sample.collected_count = picked['collected_count']
        sample.fetch_complete = picked['fetch_complete']
        sample.stop_reason = picked['stop_reason']
        sample.status = picked['status']
        sample.score_twice = picked['score_twice']
        sample.metric_value = picked['metric_value']
        sample.source_evidence = {
            'list_endpoint': '/x/space/wbi/arc/search',
            'detail_endpoint': VIEW_ENDPOINT,
            'selection_as_of_s': as_of_s,
            'window_days': policy.window_days,
            'minimum_age_days': policy.minimum_age_days,
            'content_scope': policy.content_scope,
            'raw_tid': policy.raw_tid,
            'follower_source': 'relation_stat' if follower_status == 'ok' else 'unavailable',
            'observed_at_s': self.clock(),
        }
        return sample

    # -------------------------------------------------------------- 步骤 1 资料
    async def _fetch_profile(
        self, uid: int, errors: List[Dict[str, Any]]
    ) -> Tuple[Optional[int], str, Optional[str]]:
        """取基础资料与粉丝数，粉丝优先 relation 接口，其次 meta 门控的兼容 data。

        Args:
            uid: 账号 UID。
            errors: 错误累积列表（原地追加）。

        Returns:
            Tuple[Optional[int], str, Optional[str]]: (粉丝数, 粉丝状态, 昵称)。
            粉丝状态取值 ok / missing / invalid / unknown。
        """
        name: Optional[str] = None
        follower_count: Optional[int] = None
        follower_status = 'unknown'

        # 1a) 兼容 wrapper：带 _meta.field_status / _meta.source（03 新增）
        try:
            _charge_logical(self.request_budget)
            payload = await self.api.get_user_info(uid)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - 单账号失败记录后继续，最终由 status 表达
            errors.append({'stage': 'profile', 'message': str(exc)})
            payload = None

        data: Dict[str, Any] = {}
        if isinstance(payload, dict):
            raw_data = payload.get('data')
            if isinstance(raw_data, dict):
                data = raw_data
            name = data.get('name') or None
            meta = payload.get('_meta') if isinstance(payload.get('_meta'), dict) else {}
            field_status = meta.get('field_status') if isinstance(meta.get('field_status'), dict) else {}
            status = field_status.get('follower')
            if status == 'ok':
                # meta 明确 ok 才允许相信 data 里的值（含真实 0）
                follower_count = _as_int(data.get('follower'))
                follower_status = 'ok' if follower_count is not None else 'invalid'
            elif status in ('missing', 'invalid'):
                # 关键：meta 标 missing/invalid 时，兼容 data 的 0 一律不得当真
                follower_count = None
                follower_status = status
            else:
                # 无 meta 的旧路径：只有 data 明确给出可解析数值才接受
                follower_count = _as_int(data.get('follower'))
                follower_status = 'ok' if follower_count is not None else 'missing'

        # 1b) 排名粉丝优先取 relation 接口本次明确返回字段
        try:
            _charge_logical(self.request_budget)
            relation = await self.api.get_user_relation_stat(uid)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append({'stage': 'relation', 'message': str(exc)})
            relation = None

        if isinstance(relation, dict):
            rel_data = relation.get('data') if isinstance(relation.get('data'), dict) else {}
            rel_follower = _as_int(rel_data.get('follower'))
            if rel_follower is not None:
                follower_count = rel_follower
                follower_status = 'ok'

        return follower_count, follower_status, name

    # ------------------------------------------------------ 步骤 2-7 列表与详情
    async def _collect_selection(
        self,
        uid: int,
        policy: BenchmarkPolicy,
        as_of_s: int,
        errors: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """扫描投稿列表并逐条取详情，按规则选出最新最多 10 条有效范围内稿件。

        Args:
            uid: 账号 UID。
            policy: 冻结策略。
            as_of_s: 选稿参考时刻（UTC Unix 秒）。
            errors: 错误累积列表（原地追加）。

        Returns:
            Dict[str, Any]: 含 selected_videos / collected_count / fetch_complete /
            stop_reason / status / score_twice / metric_value 的结果字典。
        """
        # 边界：published_s <= newest 才算「够老」；published_s >= oldest 才算「在窗口内」
        newest_s = as_of_s - policy.minimum_age_days * DAY_S
        oldest_s = as_of_s - policy.window_days * DAY_S
        max_videos = int(policy.max_videos)

        selected: List[Dict[str, Any]] = []
        selected_bvids = set()
        collected_count = 0
        tail_proved = False       # 已覆盖到窗口「旧边界」之后
        exhausted = False         # 列表页已遍历完
        identity_failure = False  # 详情身份不符
        page_failed = False
        aborted = False            # 硬中断：详情失败 / 重复页 / 分页异常等，均不得谎称覆盖完成
        stop_reason: Optional[str] = None
        seen_page_signatures = set()

        page = 1
        while (
            len(selected) < max_videos
            and not tail_proved
            and not exhausted
            and not identity_failure
            and not aborted
        ):
            if page > int(policy.max_pages_per_creator):
                stop_reason = 'page_limit'
                aborted = True
                break
            try:
                entries = await self._fetch_list_page(uid, page, errors)
            except RequestBudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append({'stage': 'list_page', 'page': page, 'message': str(exc)})
                page_failed = collected_count == 0
                stop_reason = 'page_error'
                aborted = True
                break

            if entries is None:
                # 结构校验失败：不能证明覆盖
                page_failed = collected_count == 0
                stop_reason = 'invalid_list_structure'
                aborted = True
                break
            if not entries:
                exhausted = True
                stop_reason = 'exhausted'
                break

            signature = tuple(entry.get('bvid') for entry in entries)
            if signature in seen_page_signatures:
                # 重复页 = 假覆盖，不得排名
                errors.append({'stage': 'list_page', 'page': page, 'message': 'duplicate_page'})
                stop_reason = 'duplicate_page'
                aborted = True
                break
            seen_page_signatures.add(signature)

            for entry in entries:
                collected_count += 1
                bvid = entry.get('bvid')
                if not bvid:
                    errors.append({'stage': 'list_entry', 'page': page, 'message': 'missing_bvid'})
                    continue
                list_published = entry.get('published_s')
                if list_published is not None:
                    if list_published > newest_s:
                        continue  # 肯定太新
                    if list_published < oldest_s:
                        tail_proved = True  # 倒序：之后只会更旧
                        break

                # 需要详情确认（缺发布时间也走这里，不擅自略过）
                detail, reason = await self._fetch_detail(bvid, uid, errors)
                if detail is None:
                    if reason == 'owner_mismatch':
                        identity_failure = True
                    stop_reason = reason or 'detail_error'
                    aborted = True
                    break

                detail_published = detail.get('published_s')
                if detail_published is None:
                    errors.append({'stage': 'detail', 'bvid': bvid, 'message': 'missing_pubdate'})
                    stop_reason = 'missing_pubdate'
                    aborted = True
                    break
                if detail_published > newest_s:
                    continue  # 详情显示仍太新
                if detail_published < oldest_s:
                    tail_proved = True
                    break
                if policy.content_scope == 'exact_raw_tid' and detail.get('raw_tid') != policy.raw_tid:
                    continue  # 原始 tid 不符，不入参评
                if bvid in selected_bvids:
                    continue
                selected.append(detail)
                selected_bvids.add(bvid)
                if len(selected) >= max_videos:
                    break

            page += 1

        if identity_failure:
            return self._selection_payload(selected, collected_count, False, 'owner_mismatch', 'error')

        selected_full = len(selected) >= max_videos
        fetch_complete = bool(tail_proved or exhausted or selected_full)
        if selected_full and not stop_reason:
            stop_reason = 'selected_max'
        elif tail_proved and not stop_reason:
            stop_reason = 'old_tail'

        # 选中稿件缺播放：不替补、不从后面补更好稿件
        views: List[int] = []
        missing_metric = False
        for item in selected:
            if item.get('view_status') != 'ok' or item.get('view_count') is None:
                missing_metric = True
            else:
                views.append(int(item['view_count']))

        if not fetch_complete:
            status = 'error' if page_failed else 'incomplete_selection'
        elif missing_metric:
            status = 'missing_selected_metrics'
        elif len(selected) < int(policy.min_videos):
            status = 'insufficient_posts'
        else:
            status = 'valid'

        score_twice: Optional[int] = None
        metric_value: Optional[float] = None
        if status == 'valid':
            try:
                score_twice = median_twice(views)
                metric_value = score_twice / 2
            except ValueError:
                status = 'insufficient_posts'
                score_twice = None
                metric_value = None

        return self._selection_payload(
            selected, collected_count, fetch_complete, stop_reason, status, score_twice, metric_value
        )

    @staticmethod
    def _selection_payload(
        selected: List[Dict[str, Any]],
        collected_count: int,
        fetch_complete: bool,
        stop_reason: Optional[str],
        status: str,
        score_twice: Optional[int] = None,
        metric_value: Optional[float] = None,
    ) -> Dict[str, Any]:
        """组装选稿结果字典（保持字段稳定，便于 hash 与测试断言）。"""
        return {
            'selected_videos': selected,
            'collected_count': collected_count,
            'fetch_complete': fetch_complete,
            'stop_reason': stop_reason,
            'status': status,
            'score_twice': score_twice,
            'metric_value': metric_value,
        }

    async def _fetch_list_page(
        self, uid: int, page: int, errors: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """拉取一页投稿列表并做结构校验。

        Args:
            uid: 账号 UID。
            page: 页码（从 1 开始）。
            errors: 错误累积列表。

        Returns:
            Optional[List[Dict[str, Any]]]: 规范化后的候选条目列表；
            ``None`` 表示结构非法（不能证明覆盖）；空列表表示该页已无内容。
        """
        _charge_logical(self.request_budget)
        payload = await self.api.get_user_videos(uid, page=page, page_size=30)

        if not isinstance(payload, dict):
            errors.append({'stage': 'list_page', 'page': page, 'message': 'invalid_payload'})
            return None
        data = payload.get('data')
        if not isinstance(data, dict):
            errors.append({'stage': 'list_page', 'page': page, 'message': 'missing_data'})
            return None
        listing = data.get('list')
        if not isinstance(listing, dict):
            errors.append({'stage': 'list_page', 'page': page, 'message': 'missing_list'})
            return None
        vlist = listing.get('vlist')
        if not isinstance(vlist, list):
            errors.append({'stage': 'list_page', 'page': page, 'message': 'missing_vlist'})
            return None
        page_info = data.get('page')
        if isinstance(page_info, dict):
            pn = _as_int(page_info.get('pn'))
            if pn is not None and pn != page:
                errors.append({'stage': 'list_page', 'page': page, 'message': 'page_mismatch'})
                return None

        entries: List[Dict[str, Any]] = []
        for item in vlist:
            if not isinstance(item, dict):
                continue
            bvid = item.get('bvid')
            entries.append({
                'bvid': str(bvid) if bvid else None,
                'published_s': _as_int(item.get('created', item.get('pubdate'))),
                'list_tid': _as_int(item.get('tid')),
                'list_play': _entry_view(item),
            })
        return entries

    async def _fetch_detail(
        self, bvid: str, uid: int, errors: List[Dict[str, Any]]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """请求单稿详情，校验 bvid / owner.mid，并取出同次响应的 stat.view。

        Args:
            bvid: 稿件 BV 号。
            uid: 期望的投稿账号 UID。
            errors: 错误累积列表。

        Returns:
            Tuple[Optional[Dict[str, Any]], Optional[str]]: (详情记录, 失败原因)。
            详情记录字段：bvid / published_s / observed_s / raw_tid / view_count /
            view_status / source。失败时第一项为 None。
        """
        _charge_logical(self.request_budget)
        # 该端点经 api.get 返回已解包 data（无第二层 data）
        try:
            data = await self.api.get(VIEW_ENDPOINT, params={'bvid': bvid})
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - 单稿失败不影响已入选稿件事实，但会标记选稿不完整
            errors.append({'stage': 'detail', 'bvid': bvid, 'message': str(exc)})
            return None, 'detail_error'
        if not isinstance(data, dict):
            errors.append({'stage': 'detail', 'bvid': bvid, 'message': 'invalid_view_response'})
            return None, 'detail_error'
        if str(data.get('bvid') or '') != str(bvid):
            errors.append({'stage': 'detail', 'bvid': bvid, 'message': 'bvid_mismatch'})
            return None, 'detail_error'
        owner = data.get('owner') if isinstance(data.get('owner'), dict) else {}
        owner_mid = _as_int(owner.get('mid'))
        if owner_mid is not None and owner_mid != int(uid):
            # 身份不符：详情不属于该账号，不能作为其成绩
            errors.append({'stage': 'detail', 'bvid': bvid, 'message': 'owner_mismatch'})
            return None, 'owner_mismatch'

        stat = data.get('stat') if isinstance(data.get('stat'), dict) else {}
        view_count = _as_int(stat.get('view'))
        view_status = 'ok' if view_count is not None else 'missing'
        observed_s = self.clock()
        record = {
            'bvid': str(bvid),
            'published_s': _as_int(data.get('pubdate')),
            'observed_s': observed_s,
            'raw_tid': _as_int(data.get('tid')),
            'view_count': view_count,
            'view_status': view_status,
            'source': VIEW_ENDPOINT,
        }
        return record, None


# --------------------------------------------------------------------------
# 候选发现（模块级函数，规格 §5.2）
# --------------------------------------------------------------------------

async def discover_candidates(
    api: Any,
    discovery_scope: Dict[str, Any],
    limit: int = 20,
) -> Dict[str, Any]:
    """从分区榜单发现唯一作者候选（不等于排名，也不拿单视频播放当账号成绩）。

    固定预算：``max_pages=10``、``max_logical_calls=20``、``deadline=120s``；
    用重复页检测防止「服务端并未真正分页」造成的假覆盖。

    Args:
        api: B 站 API 客户端（复用 ``get_ranking``）。
        discovery_scope: 发现范围，含 ``taxonomy``（pid_v2 / legacy_tid）、``rid``、
            ``day``、``original``；缺少时给保守默认。
        limit: 作者上限。

    Returns:
        Dict[str, Any]: 候选与来源证据（endpoint / params / 截断 / 失败 / 发现时间）。
        降级来源会显式置 ``source_changed=True``，不静默把 newlist 叫 ranking。

    Raises:
        RequestBudgetExceeded: 预算耗尽时向上抛出。
    """
    scope = discovery_scope if isinstance(discovery_scope, dict) else {}
    taxonomy = scope.get('taxonomy') or 'pid_v2'
    rid = _as_int(scope.get('rid'))
    day = _as_int(scope.get('day')) or 7
    original = _as_int(scope.get('original')) or 0
    limit = max(1, int(limit or 1))

    request_params: Dict[str, Any] = {
        'rid': rid,
        'day': day,
        'type': 'origin' if original else 'all',
        'pn': 1,
    }
    deadline = time.monotonic() + DEFAULT_DISCOVERY_DEADLINE_S
    authors: List[Dict[str, Any]] = []
    seen_uids = set()
    seen_page_signatures = set()
    failures: List[Dict[str, Any]] = []
    truncated = False
    source_changed = False
    pages_scanned = 0
    logical_calls = 0

    for page in range(1, DEFAULT_DISCOVERY_MAX_PAGES + 1):
        if logical_calls >= DEFAULT_DISCOVERY_MAX_LOGICAL_CALLS or time.monotonic() >= deadline:
            truncated = True
            break
        request_params['pn'] = page
        logical_calls += 1
        try:
            payload = await api.get_ranking(rid=rid, day=day, original=original, page=page)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - 单页失败记录后停止，不循环换账号绕限流
            failures.append({'page': page, 'message': str(exc)})
            break

        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            failures.append({'page': page, 'message': 'invalid_ranking_structure'})
            source_changed = True
            break
        items = data.get('list')
        if not isinstance(items, list):
            failures.append({'page': page, 'message': 'missing_ranking_list'})
            source_changed = True
            break
        if not items:
            break

        signature = tuple(
            str(item.get('bvid')) if isinstance(item, dict) else None for item in items
        )
        if signature in seen_page_signatures:
            # 重复页：服务端未真正分页，停止并标记截断
            failures.append({'page': page, 'message': 'duplicate_page'})
            truncated = True
            break
        seen_page_signatures.add(signature)
        pages_scanned = page

        for item in items:
            if not isinstance(item, dict):
                continue
            owner = item.get('owner') if isinstance(item.get('owner'), dict) else {}
            mid = _as_int(owner.get('mid'))
            if mid is None or mid in seen_uids:
                continue
            seen_uids.add(mid)
            authors.append({
                'uid': mid,
                'name': owner.get('name') or None,
                'source': 'ranking_discovered',
                'taxonomy': taxonomy,
            })
            if len(authors) >= limit:
                truncated = True
                break
        if len(authors) >= limit:
            break

    return {
        'candidates': authors,
        'taxonomy': taxonomy,
        'endpoint': RANKING_ENDPOINT,
        'params': dict(request_params),
        'pages_scanned': pages_scanned,
        'logical_calls': logical_calls,
        'truncated': truncated,
        'source_changed': source_changed,
        'failures': failures,
        'discovered_s': int(time.time()),
    }
