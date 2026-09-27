"""仓库根目录的 pytest 配置（仅做 fixture 暴露，不含隔离逻辑）。

为什么需要这个文件：
    pytest 的 conftest.py 只对"根目录 -> 用例所在目录"这一条路径上的用例生效。
    `tests/conftest.py` 里的 web 生命周期隔离 fixture 因此不会作用到直接躺在
    仓库根目录下的历史遗留测试（test_lottery_tool.py / test_lottery_refactor.py /
    test_core_behavior.py）。为了让这些文件在搬运前后裸跑 `pytest <文件>` 时同样受
    隔离保护（避免再次写脏 data/bili_ops.db），这里把它们注册为插件。

设计约束：
    - 隔离逻辑的唯一实现在 tests/conftest.py，本文件不复制任何实现，避免两份漂移。
    - 不修改任何生产代码，只做测试侧装配。
"""
pytest_plugins = ["tests.conftest"]
