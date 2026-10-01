"""配额持久化落地验收（任务：FishTool 配额持久化，规格 §2.6 / §2.2）。

覆盖任务给定的 5 条硬性用例 + 3 条红线回归：

1. 分类到顶拒发；
2. 总闸不再做运行时判定（改为加载期配置校验）；
3. 重启进程后计数仍在（真实子进程重启验证）；
4. 25 小时前的桶被裁掉、不计入；
5. 同桶 UPSERT 两次 count=2；
6. 无配额上下文（既有调用方）行为与改造前完全一致；
7. 写库失败直接拒发（宁严不松）；
8. 调度循环每轮开头先 prune 再调度（顺序固定）。

测试策略：
- 数据库一律使用 ``tmp_path`` 下的真实 SQLite（SQLAlchemy ORM），不用 Mock session；
- 配额账本用「注入 QuotaLimits」的方式控制上限，另有一个用例单独校验
  ``config/budget.yaml`` 里的真实数字与规格一致（防止数字对不上）；
- 「重启进程」用 ``subprocess`` 起全新解释器写入同一个库文件，父进程再读回，
  不是「同进程换个 engine」的近似。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import inspect

import core.database.api as db_api
import core.monitor_service as monitor_service
from core import quota_store
from core import request_budget
from core.database import Base, DatabaseManager, HttpQuotaBucket, get_session
from core.request_budget import (
    AttemptBudget,
    QuotaConfigError,
    QuotaLimits,
    RequestBudgetExceeded,
    RequestQuotaExceeded,
)

# 固定时间基准，避免用例跨小时边界抖动。
NOW = 1_700_000_000

#: 仓库根目录（子进程重启用例需要能 import core.*）。
REPO_ROOT = Path(__file__).resolve().parent.parent


def _limits(global_limit: int, **categories) -> QuotaLimits:
    """按 ``类别=(域, 上限)`` 构造配额账本快照。

    Args:
        global_limit: 单账号总闸。
        **categories: 形如 ``discovery=('no_cookie', 480)``。

    Returns:
        QuotaLimits: 可直接注入 request_budget 的账本。
    """
    return QuotaLimits(
        global_limit=global_limit,
        category_limits={name: spec[1] for name, spec in categories.items()},
        category_domains={name: spec[0] for name, spec in categories.items()},
    )


def _use_budget_yaml(monkeypatch, tmp_path, global_limit: int, categories: dict) -> None:
    """把 ``request_budget`` 的账本路径临时指向一份自造 YAML，并清 ``lru_cache``。

    Args:
        monkeypatch: pytest 打桩器。
        tmp_path: pytest 临时目录。
        global_limit: 单账号总闸。
        categories: 形如 ``{'discovery': ('no_cookie', 480)}`` 的 ``类别 -> (域, 上限)``。

    Returns:
        无。
    """
    lines = [
        'version: 1',
        'quota:',
        f'  global_limit: {global_limit}',
        '  window_hours: 24',
        '  retention_hours: 25',
        '  categories:',
    ]
    for name, (domain, limit) in categories.items():
        lines += [f'    {name}:', f'      domain: {domain}', f'      limit: {limit}']
    yaml_path = tmp_path / 'budget.yaml'
    yaml_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    monkeypatch.setattr(request_budget, '_budget_config_path', lambda: yaml_path)
    request_budget.load_quota_limits.cache_clear()


@pytest.fixture()
def quota_env(tmp_path, monkeypatch):
    """把全局 ``db_manager`` 指向 tmp 目录下的真实 SQLite 库。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 打桩器。

    Returns:
        dict: ``{'path': 库文件路径}``。
    """
    db_path = tmp_path / 'quota.db'
    monkeypatch.setattr(db_api, 'db_manager', DatabaseManager(str(db_path)))
    return {'path': db_path}


@pytest.fixture()
def frozen_now(monkeypatch):
    """把 request_budget 里的 ``time()`` 冻结到 NOW，保证桶归属稳定。

    Args:
        monkeypatch: pytest 打桩器。

    Returns:
        int: 固定时间戳 NOW。
    """
    monkeypatch.setattr(request_budget, 'time', lambda: NOW)
    return NOW


def _buckets():
    """读取配额表全部行（按桶编号排序）。

    Returns:
        list[HttpQuotaBucket]: ORM 行列表。
    """
    session = get_session()
    try:
        return list(session.query(HttpQuotaBucket).order_by(HttpQuotaBucket.hour_bucket).all())
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 建表与注册
# --------------------------------------------------------------------------- #


def test_quota_table_is_registered_and_created(quota_env):
    """HttpQuotaBucket 必须注册进 metadata，create_all 才会真的建表。"""
    assert HttpQuotaBucket.__tablename__ == 'http_quota_buckets'
    assert 'http_quota_buckets' in Base.metadata.tables
    assert 'http_quota_buckets' in set(inspect(db_api.db_manager.engine).get_table_names())
    # 主键 = (domain, category, hour_bucket)，兼唯一约束
    primary_key = {column.name for column in HttpQuotaBucket.__table__.primary_key.columns}
    assert primary_key == {'domain', 'category', 'hour_bucket'}


# --------------------------------------------------------------------------- #
# 用例 5：同桶 UPSERT 两次 count=2
# --------------------------------------------------------------------------- #


def test_same_hour_bucket_upsert_twice_counts_2(quota_env):
    """同一 (域, 类别, 小时桶) 两次 bump：只有一行、count=2、used=2。"""
    quota_store.bump('cookie', 'watch', NOW)
    quota_store.bump('cookie', 'watch', NOW)

    assert quota_store.used('cookie', 'watch', NOW) == 2
    rows = _buckets()
    assert len(rows) == 1
    assert rows[0].count == 2
    assert rows[0].hour_bucket == NOW // 3600
    assert (rows[0].domain, rows[0].category) == ('cookie', 'watch')


# --------------------------------------------------------------------------- #
# 用例 1：分类到顶拒发
# --------------------------------------------------------------------------- #


def test_category_limit_rejects_before_write(quota_env, frozen_now, monkeypatch):
    """分类已用 >= 分类上限时抛 category 拒绝，且拒绝当次不写库。"""
    monkeypatch.setattr(
        request_budget,
        'load_quota_limits',
        lambda: _limits(1800, discovery=('no_cookie', 2)),
    )

    request_budget.before_http_attempt('no_cookie', 'discovery')
    request_budget.before_http_attempt('no_cookie', 'discovery')

    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('no_cookie', 'discovery')

    assert excinfo.value.reason == 'category:discovery'
    # 配额拒绝仍然属于 RequestBudgetExceeded，上层「原样抛出、不进重试」逻辑不变。
    assert isinstance(excinfo.value, RequestBudgetExceeded)
    # 被拒绝的那次没有写库：已用量停在上限值。
    assert quota_store.used('no_cookie', 'discovery', NOW) == 2


# --------------------------------------------------------------------------- #
# 用例 2：总闸不再做运行时判定（改为加载期配置校验）
# --------------------------------------------------------------------------- #


def test_runtime_no_longer_rejects_on_global_gate(quota_env, frozen_now, monkeypatch):
    """运行时已无总闸分支：分类未到顶就一律放行，不再出现 ``global`` 拒因。

    旧设计里「分类未到顶但两域合计撞总闸」会抛 reason='global'；但五类都是硬上限、
    合计够不到总闸，那一支是死代码，现已删除，总闸前移为加载期配置校验。
    """
    # 故意把总闸调得比分类合计还小：旧设计这里会因「合计撞总闸」拒发；
    # 新设计运行时不再看总闸，只按分类判定 → 应当全部放行。
    monkeypatch.setattr(
        request_budget,
        'load_quota_limits',
        lambda: _limits(2, discovery=('no_cookie', 480), watch=('cookie', 1000)),
    )

    for _ in range(5):
        request_budget.before_http_attempt('cookie', 'watch')
    assert quota_store.used('cookie', 'watch', NOW) == 5

    # 分类判定行为不变：到顶照样拒发（reason 仍是 category:<类别>）。
    monkeypatch.setattr(
        request_budget,
        'load_quota_limits',
        lambda: _limits(2000, watch=('cookie', 5)),
    )
    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('cookie', 'watch')
    assert excinfo.value.reason == 'category:watch'


# --------------------------------------------------------------------------- #
# 用例 9：总闸配置校验（sum(分类) == / < / > 总闸 三种口径）
# --------------------------------------------------------------------------- #


def test_quota_gate_equal_to_category_sum_loads_ok():
    """sum(分类上限) == 总闸 → 正常加载，不报错（真实账本就是 1800 口径）。"""
    request_budget.load_quota_limits.cache_clear()
    limits = request_budget.load_quota_limits()

    assert limits is not None, 'config/budget.yaml 应可被读取'
    assert sum(limits.category_limits.values()) == limits.global_limit == 1800


def test_quota_gate_exceeded_by_category_sum_raises_on_load(monkeypatch, tmp_path):
    """sum(分类上限) > 总闸 → 加载即抛配置错误，报错能看到各项、合计与超出量。"""
    _use_budget_yaml(monkeypatch, tmp_path, 1800, {
        'discovery': ('no_cookie', 480),
        'watch': ('cookie', 1000),
        'ranking': ('cookie', 150),
        'maintenance': ('cookie', 72),
        'flex': ('cookie', 98),
        'extra': ('cookie', 200),   # 合计 2000 > 总闸 1800
    })

    with pytest.raises(QuotaConfigError) as excinfo:
        request_budget.load_quota_limits()

    message = str(excinfo.value)
    # 报错必须列清：每个分类的值、合计值、总闸值、超出量。
    for name, limit in (('discovery', 480), ('watch', 1000), ('ranking', 150),
                        ('maintenance', 72), ('flex', 98), ('extra', 200)):
        assert f'{name}={limit}' in message
    assert '合计 2000' in message
    assert '总闸 1800' in message
    assert '超出 200' in message


def test_quota_gate_larger_than_category_sum_loads_ok(monkeypatch, tmp_path):
    """sum(分类上限) < 总闸（留安全垫）→ 正常加载，不报错。"""
    _use_budget_yaml(monkeypatch, tmp_path, 3000, {
        'discovery': ('no_cookie', 480),
        'watch': ('cookie', 1000),
    })

    limits = request_budget.load_quota_limits()

    assert limits is not None
    assert limits.global_limit == 3000
    assert dict(limits.category_limits) == {'discovery': 480, 'watch': 1000}


# --------------------------------------------------------------------------- #
# 用例 3：重启进程后计数仍在（真实子进程）
# --------------------------------------------------------------------------- #


def test_counts_survive_real_process_restart(quota_env, frozen_now, monkeypatch):
    """子进程写满分类上限后退出；父进程读回计数并继续拒发。"""
    db_path = quota_env['path']
    monkeypatch.setattr(
        request_budget,
        'load_quota_limits',
        lambda: _limits(1800, discovery=('no_cookie', 3)),
    )

    # 在一个全新解释器里 bump 3 次（模拟真实进程重启：进程内状态必然丢失）。
    child_code = (
        "import sys; sys.path.insert(0, r'{root}');"
        "from core.database import init_database;"
        "init_database(r'{db}');"
        "from core import quota_store;"
        "quota_store.bump('no_cookie', 'discovery', {now});"
        "quota_store.bump('no_cookie', 'discovery', {now});"
        "quota_store.bump('no_cookie', 'discovery', {now});"
        "print(quota_store.used('no_cookie', 'discovery', {now}))"
    ).format(root=REPO_ROOT, db=db_path, now=NOW)
    completed = subprocess.run(
        [sys.executable, '-c', child_code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith('3')

    # 父进程（等于重启后的新进程）读回计数，并仍然拒发。
    assert quota_store.used('no_cookie', 'discovery', NOW) == 3
    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('no_cookie', 'discovery')
    assert excinfo.value.reason == 'category:discovery'


# --------------------------------------------------------------------------- #
# 用例 4：25 小时前的桶被裁掉、不计入
# --------------------------------------------------------------------------- #


def test_prune_drops_bucket_older_than_25_hours(quota_env):
    """25 小时前的桶不计入 24h 窗口，并且会被 prune 真的删掉。"""
    old_bucket = (NOW - 25 * 3600) // 3600
    recent_bucket = (NOW - 23 * 3600) // 3600
    quota_store.bump('cookie', 'watch', NOW - 25 * 3600)
    quota_store.bump('cookie', 'watch', NOW - 23 * 3600)

    # 24h 滚动窗口本来就看不到 25 小时前的桶。
    assert quota_store.used('cookie', 'watch', NOW) == 1

    assert quota_store.prune(NOW) == 1
    remaining = {row.hour_bucket for row in _buckets()}
    assert old_bucket not in remaining
    assert recent_bucket in remaining
    assert quota_store.used('cookie', 'watch', NOW) == 1
    # 幂等：没有更多可裁的桶。
    assert quota_store.prune(NOW) == 0


# --------------------------------------------------------------------------- #
# 红线 6：无配额上下文时保持原样不拦截
# --------------------------------------------------------------------------- #


def test_without_quota_context_keeps_old_behavior(quota_env, frozen_now, monkeypatch):
    """账本再严格，既有调用方（不传域/类别）也完全不拦截、不写配额表。"""
    monkeypatch.setattr(
        request_budget,
        'load_quota_limits',
        lambda: _limits(1, discovery=('no_cookie', 1)),
    )

    for _ in range(5):
        request_budget.before_http_attempt()

    assert quota_store.used('no_cookie', 'discovery', NOW) == 0
    assert _buckets() == []

    # 任务内预算（原有能力）行为不变。
    budget = AttemptBudget(max_attempts=2, deadline_monotonic=monotonic() + 60)
    token = request_budget.current_attempt_budget.set(budget)
    try:
        request_budget.before_http_attempt()
        request_budget.before_http_attempt()
        with pytest.raises(RequestBudgetExceeded):
            request_budget.before_http_attempt()
    finally:
        request_budget.current_attempt_budget.reset(token)
    assert budget.attempts == 2


# --------------------------------------------------------------------------- #
# 红线 7：写库失败直接拒发
# --------------------------------------------------------------------------- #


def test_store_write_failure_refuses_send(quota_env, frozen_now, monkeypatch):
    """bump 抛异常时必须转成配额拒绝，绝不放行。"""
    monkeypatch.setattr(
        request_budget,
        'load_quota_limits',
        lambda: _limits(1800, discovery=('no_cookie', 480)),
    )

    def _boom(*args, **kwargs):
        """模拟写库失败。"""
        raise RuntimeError('database is locked')

    monkeypatch.setattr(quota_store, 'bump', _boom)

    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('no_cookie', 'discovery')
    assert excinfo.value.reason == 'store_write_failed'


# --------------------------------------------------------------------------- #
# 红线 8：调度循环每轮开头先 prune 再调度
# --------------------------------------------------------------------------- #


def test_scheduler_housekeeping_order_is_prune_then_log(quota_env, monkeypatch):
    """一轮维护的顺序：prune 配额桶 → 清 watch 到期行（暂无落点）→ 打点。"""
    calls = []
    monkeypatch.setattr(quota_store, 'prune', lambda now: calls.append(('prune', now)))
    monkeypatch.setattr(monitor_service, 'log_quota_usage', lambda now: calls.append(('log', now)))

    monitor = monitor_service.ResidentCommentMonitor(monitor_factory=lambda: None, config=object())
    monitor._quota_housekeeping()

    assert [name for name, _ in calls] == ['prune', 'log']


def test_monitor_loop_housekeeps_before_dispatch(quota_env, monkeypatch):
    """调度循环每轮「开头」就先做配额维护，再取值调度。"""
    calls = []
    monkeypatch.setattr(
        monitor_service.ResidentCommentMonitor,
        '_quota_housekeeping',
        lambda self: calls.append('housekeeping'),
    )
    # enabled=False 时循环会在本轮取值后退出，因此「housekeeping 恰好在取值之前」可被观测。
    monkeypatch.setattr(
        monitor_service.ResidentCommentMonitor,
        'snapshot',
        lambda self: {'enabled': False, 'paused': False, 'target_bvids': []},
    )

    monitor = monitor_service.ResidentCommentMonitor(monitor_factory=lambda: None, config=object())
    asyncio.run(asyncio.wait_for(monitor._monitor_loop(), timeout=5))

    assert calls == ['housekeeping']


# --------------------------------------------------------------------------- #
# 数字校验：budget.yaml 与规格一一对应（防止「数字对不上」）
# --------------------------------------------------------------------------- #


def _seed(rows) -> None:
    """直接写配额桶，用于低成本构造「已用状态」。

    Args:
        rows: 形如 ``[('no_cookie', 'discovery', 480), ...]`` 的 (域, 类别, 次数) 列表。

    Returns:
        无。
    """
    session = get_session()
    try:
        for domain, category, count in rows:
            session.add(HttpQuotaBucket(
                domain=domain, category=category, hour_bucket=NOW // 3600, count=count,
            ))
        session.commit()
    finally:
        session.close()


def test_real_budget_yaml_blocks_at_category_limit(quota_env, frozen_now):
    """端到端：用真实 config/budget.yaml，discovery 已用 480 时第 481 次被拒。"""
    request_budget.load_quota_limits.cache_clear()
    _seed([('no_cookie', 'discovery', 480)])

    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('no_cookie', 'discovery')

    assert excinfo.value.reason == 'category:discovery'
    # 拒绝当次不写库，已用量不变。
    assert quota_store.used('no_cookie', 'discovery', NOW) == 480


def test_sqlite_connection_uses_wal_and_busy_timeout(quota_env):
    """SQLite 连接必须开 WAL 且 busy_timeout=5000（规格 §2.6）。"""
    connection = db_api.db_manager.engine.raw_connection()
    try:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    finally:
        connection.close()


def test_budget_yaml_numbers_match_spec():
    """总闸 1800、五类上限 480/1000/150/72/98，窗口 24h、保留 25h。"""
    request_budget.load_quota_limits.cache_clear()
    quota_store._window_params.cache_clear()

    limits = request_budget.load_quota_limits()
    assert limits is not None, 'config/budget.yaml 应可被读取'
    assert limits.global_limit == 1800
    assert dict(limits.category_limits) == {
        'discovery': 480,
        'watch': 1000,
        'ranking': 150,
        'maintenance': 72,
        'flex': 98,
    }
    assert dict(limits.category_domains) == {
        'discovery': 'no_cookie',
        'watch': 'cookie',
        'ranking': 'cookie',
        'maintenance': 'cookie',
        'flex': 'cookie',
    }
    assert sum(limits.category_limits.values()) == limits.global_limit

    params = dict(quota_store._window_params())
    assert params == {'window_hours': 24, 'retention_hours': 25}


def test_budget_yaml_has_domain_cooldown_params():
    """分域冷却参数必须落在 yaml，业务代码里不得硬编码。"""
    import yaml

    with (REPO_ROOT / 'config' / 'budget.yaml').open('r', encoding='utf-8') as handle:
        raw = yaml.safe_load(handle)
    domains = raw['domains']
    assert set(domains) == {'cookie', 'no_cookie'}
    for name in domains:
        assert set(domains[name]) == {
            'cooldown_base_s', 'cooldown_max_s', 'min_interval_s', 'max_concurrency',
        }
    assert raw['ip_circuit_breaker']['cooldown_s'] == 3600
