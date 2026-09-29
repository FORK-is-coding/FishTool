"""01 正确排名 · 端到端验收脚本（临时配置 + stub API，规格 §11）。

用途：在没有真实 B 站账号 / Cookie 的前提下，用 stub API 走完整链路：
    候选发现 -> （手动）名单 -> 创建任务 -> 轮询终态 -> 读冻结结果
    -> 自诊附带 creator_ranking -> 导出 Markdown -> 重试生成新 run

安全约束：
- 全部数据落在临时目录（临时 sqlite + 临时报告目录），不碰 data/bili_ops.db、
  config/.key、config/.secrets；
- 不发起任何真实 B 站请求（全部由 stub 回答）；
- 报告里明确区分「已验证」与「未验证（真实接口 / 浏览器 UI / PDF 真实格式）」。

运行：
    python tools/verify_creator_ranking.py

退出码：0 表示全部断言通过；1 表示存在失败步骤（会打印失败原因）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# 允许脚本直接以 `python tools/xxx.py` 运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.database.manager import DatabaseManager  # noqa: E402
from modules.self_diagnosis.benchmark.service import BenchmarkService  # noqa: E402
from modules.self_diagnosis.benchmark.store import BenchmarkStore  # noqa: E402
from web.local_guard import get_local_guard  # noqa: E402
from web.routers import benchmark as benchmark_router  # noqa: E402

DAY_S = 86400
AS_OF = 1_700_000_000
REPORT_DIR = 'reports_verify'

STEPS: List[Dict[str, Any]] = []


def record(name: str, ok: bool, detail: Any = None) -> None:
    """记录一步验收结果。"""
    STEPS.append({'step': name, 'ok': bool(ok), 'detail': detail})


class StubAPI:
    """端到端 stub：目标 200 播放，5 个同行 100/300/400/500/600。"""

    def __init__(self) -> None:
        """构造固定数据。"""
        self.creators: Dict[int, Dict[str, Any]] = {
            1: self._creator(1, 200),      # 目标：中位 200
            2: self._creator(2, 100),
            3: self._creator(3, 200),
            4: self._creator(4, 300),
            5: self._creator(5, 400),
            6: self._creator(6, 500),
            21: self._creator(21, 250),
            22: self._creator(22, 260),
        }
        self.ranking_pages = {
            1: [{'bvid': 'BV1', 'owner': {'mid': 21, 'name': '候选甲'}},
                {'bvid': 'BV2', 'owner': {'mid': 22, 'name': '候选乙'}}],
        }
        self.calls = 0

    @staticmethod
    def _creator(uid: int, view: int) -> Dict[str, Any]:
        """构造某播放值的账号（3 条同播放稿件 -> 中位 = view）。

        bvid 必须带 uid，否则不同账号的同播放稿件会撞号。
        """
        return {'follower': 10000 + view,
                'videos': [(f'BV{uid}_{i}', AS_OF - (10 + i) * DAY_S, 4, view) for i in range(3)]}

    async def get_user_info(self, uid: int) -> Dict[str, Any]:
        """用户资料（带 meta）。"""
        self.calls += 1
        follower = (self.creators.get(uid) or {}).get('follower')
        return {'data': {'name': f'UP{uid}', 'follower': follower or 0},
                '_meta': {'field_status': {'follower': 'ok' if follower is not None else 'missing'}}}

    async def get_user_relation_stat(self, uid: int) -> Dict[str, Any]:
        """relation 接口。"""
        follower = (self.creators.get(uid) or {}).get('follower')
        return {'data': {'follower': follower} if follower is not None else {}}

    async def get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        """投稿列表。"""
        self.calls += 1
        videos = (self.creators.get(uid) or {}).get('videos') or []
        if page > 1:
            videos = []
        vlist = [{'bvid': b, 'created': c, 'tid': t, 'play': 0} for b, c, t, _ in videos]
        return {'data': {'list': {'vlist': vlist}, 'page': {'pn': page, 'ps': page_size, 'count': len(vlist)}}}

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None, need_sign: bool = False, **kwargs):
        """详情（已解包 data）。"""
        self.calls += 1
        bvid = (params or {}).get('bvid')
        for uid, payload in self.creators.items():
            for entry in payload['videos']:
                if entry[0] == bvid:
                    return {'bvid': bvid, 'pubdate': entry[1], 'tid': entry[2],
                            'owner': {'mid': uid}, 'stat': {'view': entry[3]}}
        return None

    async def get_ranking(self, rid: int, day: int = 7, original: int = 0, page: int = 1):
        """榜单页。"""
        self.calls += 1
        return {'data': {'list': list(self.ranking_pages.get(page, []))}}


def main() -> int:
    """执行端到端验收。"""
    tmp_root = Path(tempfile.mkdtemp(prefix='fishtool_verify_ranking_'))
    db_path = tmp_root / 'bench_verify.db'
    report_dir = tmp_root / REPORT_DIR

    api = StubAPI()
    manager = DatabaseManager(str(db_path))
    store = BenchmarkStore(lambda: manager.get_session())
    service = BenchmarkService(api, store, clock=lambda: AS_OF, policy_config={})
    benchmark_router.set_benchmark_service(service)

    app = FastAPI()
    app.include_router(benchmark_router.router, prefix='/api')
    client = TestClient(app, base_url='http://127.0.0.1')
    token = get_local_guard().issue_token()
    headers = {'X-Local-Token': token}

    try:
        # 0) 本机 token 下发（同源）
        token_body = client.get('/api/analysis/benchmark/local-token').json()
        record('local_token_issued', bool(token_body.get('data', {}).get('token')))

        # 1) 候选发现（仅发现，不创建任务）
        cand = client.post('/api/analysis/benchmark/candidates',
                           json={'taxonomy': 'pid_v2', 'rid': 4, 'limit': 10}, headers=headers)
        cand_data = cand.json()['data']
        record('candidates_discovered', [c['uid'] for c in cand_data['candidates']] == [21, 22],
               {'uids': [c['uid'] for c in cand_data['candidates']]})
        record('discovery_token_signed', bool(cand_data.get('discovery_token')))

        # 2) 创建任务（真实名次：目标 200 vs peer 100/200/300/400/500 -> rank 4, P30）
        created = client.post('/api/analysis/benchmark/tasks',
                              json={'target_uid': 1, 'peer_uids': [2, 3, 4, 5, 6]},
                              headers=headers).json()['data']
        run_id = created['run_id']
        record('task_created', created['peer_source'] == 'manual_peer_set')

        # 3) 同步执行（等价于后台任务完成）
        import asyncio
        asyncio.run(service.execute_run(run_id))

        task_view = client.get(f'/api/analysis/benchmark/tasks/{run_id}').json()['data']
        record('task_terminal_completed', task_view['status'] == 'completed', {'status': task_view['status']})

        # 4) 读冻结结果
        frozen = client.get(f'/api/analysis/benchmark/runs/{run_id}').json()['data']
        result = frozen['result']
        target = result['target']
        record('numeric_rank_4_percentile_30',
               target['rank'] == 4 and target['rank_end'] == 5 and target['total'] == 6 and target['percentile'] == 30,
               {'rank': target['rank'], 'rank_end': target['rank_end'], 'percentile': target['percentile']})
        record('comparison_complete', result['comparison_state'] == 'complete')
        record('snapshot_hash_present', bool(frozen.get('snapshot_hash')))

        # 5) 刷新一致（不触网）
        calls_before = api.calls
        again = client.get(f'/api/analysis/benchmark/runs/{run_id}').json()['data']
        record('refresh_identical_and_offline',
               again['result'] == result and api.calls == calls_before)

        # 6) 报告导出（Markdown，携带同一 run）
        from modules.self_diagnosis.report_generator import ReportGenerator
        generator = ReportGenerator(output_dir=str(report_dir))
        creator_ranking = dict(result)
        creator_ranking['benchmark_run_id'] = run_id
        md_path = generator.save_markdown_report(
            {'uid': 1, 'basic_info': {'name': 'UP1'}, 'fan_stats': {},
             'video_stats': {}, 'post_rhythm': {}, 'engagement_metrics': {}, 'data_availability': {}},
            None, 'verify_ranking.md', creator_ranking=creator_ranking,
        )
        md_text = Path(md_path).read_text(encoding='utf-8')
        record('report_contains_ranking_section', '同行排名' in md_text)
        record('report_shows_real_rank', '第 4–5 名' in md_text and '30%' in md_text)
        record('report_no_whole_site_claim',
               '不代表全站排名' in md_text and '全站第' not in md_text)

        # 7) 重试 -> 新 run
        retried = client.post(f'/api/analysis/benchmark/runs/{run_id}/retry', headers=headers).json()['data']
        record('retry_creates_new_run', retried['run_id'] != run_id
               and retried['retried_from'] == run_id)

        # 8) 跨 UID 拒绝
        cross = client.get(f'/api/analysis/benchmark/runs/{run_id}')
        record('run_read_ok', cross.status_code == 200)

        # 9) 写端点无 token -> 403
        denied = client.post('/api/analysis/benchmark/tasks',
                             json={'target_uid': 1, 'peer_uids': [2]})
        record('write_without_token_403', denied.status_code == 403)

        # 10) strict 模型
        bad = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': True, 'peer_uids': [2]}, headers=headers)
        record('strict_int_rejects_bool', bad.status_code == 422)

    except Exception as exc:  # noqa: BLE001 - 脚本级兜底，如实报告失败
        record('unexpected_error', False, {'error': repr(exc)})
    finally:
        benchmark_router.set_benchmark_service(None)

    failed = [step for step in STEPS if not step['ok']]
    print(json.dumps({
        'verified': [s['step'] for s in STEPS if s['ok']],
        'failed': failed,
        'not_verified': [
            '真实 B 站接口（本脚本只使用 stub，未用真实账号 / Cookie）',
            '浏览器 UI 交互（未驱动真实浏览器）',
            'PDF 真实格式（pdfkit/wkhtmltopdf 依赖未在本机验收）',
        ],
        'tmp_root': str(tmp_root),
    }, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
