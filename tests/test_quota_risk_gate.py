"""批 0 验收：配额接线 + 分域冷却 + IP 级熔断（规格 §2.4 / §2.6）。

覆盖任务给定的 8 条硬性用例：

1. 空参兼容（不传 domain / category 时行为与改造前完全一致）；
2. 配额接线后真实调用点确实记账（``quota_store.used`` 增长）；
3. 单域 412 只冷却该域，另一域照常放行；
4. 连续 3 次 412 冷却翻倍、封顶 2 小时；
5. 冷却状态跨进程重启仍生效（真实子进程）；
6. 429 取该域较晚恢复时刻；
7. IP 熔断触发后全局停采，且不能自动解除；
8. 连续 412 计数只在冷却期满后首次成功时清零。

另含两条保守方向用例（状态读取失败按「计入」处理）与状态表注册校验。

测试策略：数据库一律使用 ``tmp_path`` 下的真实 SQLite（SQLAlchemy ORM），
不用 Mock session；时间用 ``monkeypatch`` 冻结 ``request_budget.time``，
避免跨秒 / 跨小时抖动。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect

import core.database.api as db_api
import core.request_budget as request_budget
from bilibili.api.client import BilibiliAPICore
from core import quota_store
from core.database import Base, DatabaseManager
from core.database.models_quota import DomainCooldown, HttpRiskEvent, IpCircuitState
from core.exceptions import BilibiliAPIError
from core.request_budget import RequestQuotaExceeded

#: 固定时间基准，避免用例跨小时 / 跨边界抖动。
NOW = 1_700_000_000

#: 仓库根目录（子进程重启用例需要能 import core.*）。
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def risk_env(tmp_path, monkeypatch):
    """把全局 ``db_manager`` 指向 tmp 目录下的真实 SQLite 库。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 打桩器。

    Returns:
        dict: ``{'path': 库文件路径}``。
    """
    db_path = tmp_path / 'risk_gate.db'
    monkeypatch.setattr(db_api, 'db_manager', DatabaseManager(str(db_path)))
    # 冷却 / 配额账本都是 lru_cache，用例间必须清缓存，避免读到别的用例的 monkeypatch。
    request_budget.load_cooldown_limits.cache_clear()
    request_budget.load_quota_limits.cache_clear()
    return {'path': db_path}


@pytest.fixture()
def frozen_now(monkeypatch):
    """把 ``request_budget`` 里的 ``time()`` 冻结到 NOW。

    Args:
        monkeypatch: pytest 打桩器。

    Returns:
        int: 固定时间戳 NOW。
    """
    monkeypatch.setattr(request_budget, 'time', lambda: NOW)
    return NOW


class _FakeResponse:
    """实现 aiohttp 响应所需的异步上下文协议。"""

    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self.payload = payload if payload is not None else {'code': 0, 'data': {'ok': True}}
        self.headers = headers or {}

    async def __aenter__(self):
        """进入异步响应上下文并返回自身。"""
        return self

    async def __aexit__(self, *_args):
        """退出异步响应上下文。"""
        return False

    async def json(self):
        """返回预设 JSON 负载。"""
        return self.payload

    async def text(self):
        """返回原始响应文本。"""
        return 'invalid'


class _FakeSession:
    """按顺序返回预设响应的会话替身。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers = {}
        self.closed = False
        self.calls = []

    def request(self, **kwargs):
        """记录请求参数并返回下一个响应。"""
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _build_client(*responses, **kwargs) -> BilibiliAPICore:
    """构造绕过真实网络初始化的请求器。

    Args:
        *responses: 依次返回的假响应。
        **kwargs: 透传给 ``BilibiliAPICore``（cookie / domain / quota_category 等）。

    Returns:
        BilibiliAPICore: 可直接 ``asyncio.run(client.request(...))`` 的客户端。
    """
    client = BilibiliAPICore(**kwargs)
    client.session = _FakeSession(*responses)
    client.headers = {'User-Agent': 'pytest'}
    # init_session 打桩：指纹请求会额外触发一次 HTTP 尝试，这里不参与本组用例。
    client.init_session = AsyncMock()
    client.wbi_signer = SimpleNamespace(sign_params=AsyncMock(side_effect=lambda params, _session: params))
    return client


# --------------------------------------------------------------------------- #
# 状态表注册
# --------------------------------------------------------------------------- #


def test_risk_state_tables_are_registered_and_created(risk_env):
    """三个风控状态表都必须注册进 metadata，create_all 才会真的建表。"""
    assert DomainCooldown.__tablename__ == 'domain_cooldown'
    assert HttpRiskEvent.__tablename__ == 'http_risk_events'
    assert IpCircuitState.__tablename__ == 'ip_circuit_state'

    tables = set(Base.metadata.tables)
    for name in ('domain_cooldown', 'http_risk_events', 'ip_circuit_state'):
        assert name in tables
        assert name in set(inspect(db_api.db_manager.engine).get_table_names())

    # domain_cooldown 主键 = domain（一行一域）
    assert {column.name for column in DomainCooldown.__table__.primary_key.columns} == {'domain'}


# --------------------------------------------------------------------------- #
# 用例 1：空参兼容
# --------------------------------------------------------------------------- #


def test_empty_args_keep_old_behavior_even_when_cooling_and_circuit_open(risk_env, frozen_now):
    """空参调用（既有调用方）保持改造前行为：冷却中、熔断已开都不拦截、不写表。"""
    quota_store.set_domain_cooldown('cookie', cooldown_until_epoch=NOW + 900, consecutive_412=1, now=NOW)
    quota_store.open_ip_circuit(cooldown_until_epoch=NOW + 3600, reason='preexisting', now=NOW)

    for _ in range(3):
        request_budget.before_http_attempt()

    # 既没有新增风控事件，也没有新增配额计数。
    assert quota_store.count_risk_events('cookie', since_epoch=0, codes=(412,)) == 0
    assert quota_store.used('cookie', 'watch', NOW) == 0
    assert quota_store.used('no_cookie', 'discovery', NOW) == 0
    # 熔断状态不被空参调用改写。
    assert quota_store.get_ip_circuit()['reason'] == 'preexisting'


def test_empty_args_still_consume_attempt_budget(risk_env, frozen_now, monkeypatch):
    """空参调用仍只做任务内预算计数（原有能力）行为不变。"""
    from time import monotonic

    from core.request_budget import AttemptBudget, RequestBudgetExceeded

    monkeypatch.setattr(request_budget, 'load_cooldown_limits', lambda: None)
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
# 用例 3：单域 412 只冷却该域
# --------------------------------------------------------------------------- #


def test_single_domain_412_cools_only_that_domain(risk_env, frozen_now):
    """412 只冷却本域（15 分钟），另一域照常放行并正常记账。"""
    request_budget.report_http_412('cookie', now=NOW)

    assert quota_store.get_domain_cooldown('cookie') == (NOW + 900, 1)
    assert quota_store.get_domain_cooldown('no_cookie') is None

    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('cookie', 'watch')
    assert excinfo.value.reason == 'domain:cooling'
    # 被冷却拒发的那次不记账。
    assert quota_store.used('cookie', 'watch', NOW) == 0

    # 另一域照常放行，并且正常计入配额。
    request_budget.before_http_attempt('no_cookie', 'discovery')
    assert quota_store.used('no_cookie', 'discovery', NOW) == 1


def test_domain_cooldown_read_failure_refuses_send(risk_env, frozen_now, monkeypatch):
    """冷却状态读取失败按「计入」处理：保守拒发，绝不放行。"""
    monkeypatch.setattr(
        quota_store,
        'get_domain_cooldown',
        lambda domain: (_ for _ in ()).throw(RuntimeError('database is locked')),
    )
    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('cookie', 'watch')
    assert excinfo.value.reason == 'domain_cooldown_unavailable'


def test_circuit_read_failure_refuses_send(risk_env, frozen_now, monkeypatch):
    """熔断状态读取失败同样保守拒发。"""
    monkeypatch.setattr(
        quota_store,
        'get_ip_circuit',
        lambda: (_ for _ in ()).throw(RuntimeError('database is locked')),
    )
    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('cookie', 'watch')
    assert excinfo.value.reason == 'ip_circuit_unavailable'


# --------------------------------------------------------------------------- #
# 用例 4：连续 3 次 412 冷却翻倍、封顶 2 小时
# --------------------------------------------------------------------------- #


def test_consecutive_412_doubles_then_caps_at_two_hours(risk_env, monkeypatch):
    """连续 412：15 -> 15 -> 30 -> 60 -> 120 -> 120 分钟（封顶 2 小时）。"""
    clock = {'now': NOW}
    monkeypatch.setattr(request_budget, 'time', lambda: clock['now'])

    expected_durations = [900, 900, 1800, 3600, 7200, 7200]
    for index, duration in enumerate(expected_durations, start=1):
        request_budget.report_http_412('cookie')
        until, consecutive = quota_store.get_domain_cooldown('cookie')
        assert consecutive == index, f'第 {index} 次 412 的连续计数应为 {index}'
        assert until == clock['now'] + duration, f'第 {index} 次 412 的冷却时长应为 {duration}s'
        # 冷却期满后放行（但没有成功请求 -> 计数不清零，下一次继续翻倍）
        clock['now'] = until

    assert quota_store.get_domain_cooldown('cookie')[1] == len(expected_durations)


# --------------------------------------------------------------------------- #
# 用例 6：429 取该域较晚恢复时刻
# --------------------------------------------------------------------------- #


def test_429_takes_later_recovery_of_that_domain(risk_env, frozen_now):
    """429 只推后该域恢复时刻（取较晚者），且不动「连续 412」计数。"""
    request_budget.report_http_412('cookie', now=NOW)          # 该域 -> NOW + 900
    request_budget.report_status_429('cookie', 600, now=NOW)   # NOW + 600 更早 -> 保持 900
    assert quota_store.get_domain_cooldown('cookie')[0] == NOW + 900

    request_budget.report_status_429('cookie', 1800, now=NOW)  # NOW + 1800 更晚 -> 采用
    until, consecutive = quota_store.get_domain_cooldown('cookie')
    assert until == NOW + 1800
    assert consecutive == 1, '429 不是 412，不得推进连续 412 计数'

    # 另一域完全不受影响（各自冷却）。
    assert quota_store.get_domain_cooldown('no_cookie') is None


def test_429_shared_recovery_is_visible_to_all_callers_of_that_domain(risk_env, frozen_now):
    """该域 429 之后，任何调用方发起的请求都会被冷却拒发（共享恢复时刻）。"""
    request_budget.report_status_429('cookie', 600, now=NOW)
    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('cookie', 'ranking')
    assert excinfo.value.reason == 'domain:cooling'
    # 免 Cookie 域照常放行。
    request_budget.before_http_attempt('no_cookie', 'ranking')


# --------------------------------------------------------------------------- #
# 用例 5：冷却状态跨进程重启仍生效
# --------------------------------------------------------------------------- #


def test_cooldown_survives_real_process_restart(risk_env, frozen_now):
    """子进程写入冷却后退出；父进程（= 重启后的新进程）读回并继续拒发。"""
    db_path = risk_env['path']
    child_code = (
        "import sys; sys.path.insert(0, r'{root}');"
        "from core.database import init_database;"
        "init_database(r'{db}');"
        "from core import request_budget, quota_store;"
        "request_budget.report_http_412('cookie', now={now});"
        "print(quota_store.get_domain_cooldown('cookie'))"
    ).format(root=REPO_ROOT, db=db_path, now=NOW)
    completed = subprocess.run(
        [sys.executable, '-c', child_code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith(f'({NOW + 900}, 1)')

    # 父进程读回冷却状态，并仍然拒发（冷却不是内存状态）。
    assert quota_store.get_domain_cooldown('cookie') == (NOW + 900, 1)
    with pytest.raises(RequestQuotaExceeded) as excinfo:
        request_budget.before_http_attempt('cookie', 'watch')
    assert excinfo.value.reason == 'domain:cooling'


# --------------------------------------------------------------------------- #
# 用例 7：IP 级熔断
# --------------------------------------------------------------------------- #


def test_ip_circuit_requires_both_domains_to_trip(risk_env, frozen_now):
    """只有「两域各自 >= 3 次」才熔断；单域 3 次不熔断。"""
    for _ in range(3):
        request_budget.report_http_412('cookie', now=NOW)
    assert quota_store.get_ip_circuit() is None

    # 另一域累计到第 2 次仍不熔断。
    request_budget.report_business_risk_code('no_cookie', -352, now=NOW)
    request_budget.report_business_risk_code('no_cookie', -412, now=NOW)
    assert quota_store.get_ip_circuit() is None

    # 第 3 次 -> 两域齐备 -> 熔断。
    request_budget.report_http_412('no_cookie', now=NOW)
    circuit = quota_store.get_ip_circuit()
    assert circuit is not None
    assert circuit['cooldown_until_epoch'] == NOW + 3600


def test_ip_circuit_blocks_both_domains_and_never_auto_releases(risk_env, monkeypatch):
    """熔断后两域全部停采；时间推过一周也不自动解除，只有人工清除才解除。"""
    clock = {'now': NOW}
    monkeypatch.setattr(request_budget, 'time', lambda: clock['now'])

    for _ in range(3):
        request_budget.report_http_412('cookie')
        request_budget.report_http_412('no_cookie')
    assert request_budget.ip_circuit_open() is True

    clock['now'] = NOW + 7 * 24 * 3600  # 远远推过 1 小时冷却
    for domain, category in (('cookie', 'watch'), ('no_cookie', 'discovery')):
        with pytest.raises(RequestQuotaExceeded) as excinfo:
            request_budget.before_http_attempt(domain, category)
        assert excinfo.value.reason == 'ip:circuit_open'

    # 人工确认清除 -> 解除停采。
    assert request_budget.clear_ip_circuit() is True
    assert request_budget.ip_circuit_open() is False
    request_budget.before_http_attempt('cookie', 'watch')
    assert quota_store.used('cookie', 'watch', clock['now']) == 1


def test_ip_circuit_state_survives_restart(risk_env, frozen_now):
    """熔断状态同样落盘：换一个进程读取仍然处于停采。"""
    for _ in range(3):
        request_budget.report_http_412('cookie', now=NOW)
        request_budget.report_http_412('no_cookie', now=NOW)

    assert quota_store.get_ip_circuit() is not None
    assert request_budget.ip_circuit_open() is True
    assert quota_store.clear_ip_circuit() is True
    assert request_budget.ip_circuit_open() is False


# --------------------------------------------------------------------------- #
# 用例 8：连续计数只在冷却期满后首次成功时清零
# --------------------------------------------------------------------------- #


def test_consecutive_counter_resets_only_on_first_success_after_expiry(risk_env, monkeypatch):
    """冷却期内 / 刚到期但未成功 都不清零；期满后的首次成功才清零。"""
    clock = {'now': NOW}
    monkeypatch.setattr(request_budget, 'time', lambda: clock['now'])

    for _ in range(3):
        request_budget.report_http_412('cookie')
        clock['now'] += 1
    until, consecutive = quota_store.get_domain_cooldown('cookie')
    assert consecutive == 3

    # 冷却期内上报成功：属「在途响应」，不清零。
    assert request_budget.report_http_success('cookie') is False
    assert quota_store.get_domain_cooldown('cookie')[1] == 3

    # 冷却刚到期、但还没有任何成功请求：仍不清零（否则连续三次翻倍永远触发不到）。
    clock['now'] = until
    assert quota_store.get_domain_cooldown('cookie')[1] == 3

    # 冷却期满后的首次成功：清零。
    assert request_budget.report_http_success('cookie') is True
    assert quota_store.get_domain_cooldown('cookie')[1] == 0

    # 清零后再来一次 412 -> 回到 15 分钟基准。
    request_budget.report_http_412('cookie')
    assert quota_store.get_domain_cooldown('cookie')[0] == clock['now'] + 900


# --------------------------------------------------------------------------- #
# 用例 2：配额接线后真实调用点确实记账
# --------------------------------------------------------------------------- #


def test_client_domain_follows_credentials():
    """客户端凭证域：带凭证 -> cookie；免凭证 -> no_cookie；非法值直接报错。"""
    assert BilibiliAPICore(cookie='SESSDATA=x').domain == 'cookie'
    assert BilibiliAPICore(cookie_pool=object()).domain == 'cookie'
    assert BilibiliAPICore(cookie='').domain == 'no_cookie'
    assert BilibiliAPICore().domain == 'no_cookie'
    # 显式声明优先。
    assert BilibiliAPICore(cookie='SESSDATA=x', domain='no_cookie').domain == 'no_cookie'
    with pytest.raises(ValueError):
        BilibiliAPICore(domain='bogus')


def test_client_request_accounts_declared_quota(risk_env, frozen_now):
    """真实 HTTP 调用点确实记账：声明 quota_category 后 (域, 类别) 用量逐次增长。"""
    client = _build_client(_FakeResponse(), cookie='SESSDATA=x', quota_category='watch')

    asyncio.run(client.request('GET', 'https://example.test', retry_times=1))
    assert quota_store.used('cookie', 'watch', NOW) == 1

    client.session = _FakeSession(_FakeResponse())
    asyncio.run(client.request('GET', 'https://example.test', retry_times=1))
    assert quota_store.used('cookie', 'watch', NOW) == 2

    # 本次请求显式声明的类别优先于客户端缺省类别。
    client.session = _FakeSession(_FakeResponse())
    asyncio.run(client.request('GET', 'https://example.test', retry_times=1, quota_category='ranking'))
    assert quota_store.used('cookie', 'ranking', NOW) == 1
    assert quota_store.used('cookie', 'watch', NOW) == 2


def test_client_without_declared_category_only_runs_risk_gate(risk_env, frozen_now):
    """未声明类别时不记账（免凭证通道由 sources 层显式记账），避免同一次请求计两次。"""
    client = _build_client(_FakeResponse(), cookie='')
    asyncio.run(client.request('GET', 'https://example.test', retry_times=1))

    assert quota_store.used('no_cookie', 'discovery', NOW) == 0
    assert quota_store.used('cookie', 'watch', NOW) == 0


def test_client_reports_412_and_business_risk_to_its_own_domain(risk_env, frozen_now):
    """两个上报点都接上，并按客户端自己的凭证域冷却（主站 cookie / 06 no_cookie）。"""
    # HTTP 412 分支
    client = _build_client(_FakeResponse(status=412), cookie='SESSDATA=x', quota_category='watch')
    with pytest.raises(BilibiliAPIError):
        asyncio.run(client.request('GET', 'https://example.test', retry_times=1))
    assert quota_store.get_domain_cooldown('cookie') == (NOW + 900, 1)
    assert quota_store.get_domain_cooldown('no_cookie') is None

    # 业务码 -352 分支（06 免凭证客户端 -> no_cookie 域）
    client06 = _build_client(_FakeResponse(payload={'code': -352, 'message': 'risk'}), cookie='')
    with pytest.raises(BilibiliAPIError):
        asyncio.run(client06.request('GET', 'https://example.test', retry_times=1))
    assert quota_store.get_domain_cooldown('no_cookie') == (NOW + 900, 1)
    # cookie 域的冷却不被 06 的 412 连坐（分域冷却的意义所在）。
    assert quota_store.get_domain_cooldown('cookie') == (NOW + 900, 1)


def test_client_success_clears_consecutive_after_cooldown_expiry(risk_env, monkeypatch):
    """客户端成功返回时触发「冷却期满后首次成功才清零」的口径。"""
    clock = {'now': NOW}
    monkeypatch.setattr(request_budget, 'time', lambda: clock['now'])

    request_budget.report_http_412('cookie')
    assert quota_store.get_domain_cooldown('cookie')[1] == 1

    clock['now'] = NOW + 900  # 冷却期满
    client = _build_client(_FakeResponse(), cookie='SESSDATA=x', quota_category='watch')
    asyncio.run(client.request('GET', 'https://example.test', retry_times=1))
    assert quota_store.get_domain_cooldown('cookie')[1] == 0


# --------------------------------------------------------------------------- #
# 06 三个入口的显式配额归属
# --------------------------------------------------------------------------- #


def test_discovery_sources_declare_06_quota_pairs():
    """06 三个入口的 (凭证域, 配额类别) 必须显式声明，不得靠账本默认值蒙。"""
    from modules.hotspot.discovery import sources

    assert sources.DISCOVERY_QUOTA == ('no_cookie', 'discovery')
    assert sources.RANKING_ALL_QUOTA == ('no_cookie', 'ranking')
