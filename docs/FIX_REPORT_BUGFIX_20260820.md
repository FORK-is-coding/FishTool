# FIX REPORT - BUGFIX 20260820

## Scope

Project: `D:\tasks\cola\bili_ops_toolbox`

All six reported runtime defects were fixed and verified with a focused regression script plus a live FastAPI service on `127.0.0.1:8012`.

## Bug 1 - AI topic hot_tags undefined

Fix:
- `modules/hotspot/topic_generator.py`: the no-tag result now always includes `hot_tags: []`.
- `web/frontend/static/js/app.js`: rendering uses `(result.hot_tags || [])` before `slice`.

Evidence:
- `python verify_bugfix_20260820.py` passed the empty-tag branch and asserted `hot_tags == []`.

## Bug 2 - Comment monitor count undefined

Fix:
- `modules/comment/monitor.py`: the no-comment branch always returns `collected_count: 0` and `processed_count: 0`.
- Frontend uses null-safe defaults and displays the backend error message.
- Error message explicitly asks the user to inspect Cookie/login state and risk control.

Evidence:
- Live `POST /api/comment/monitor` returned HTTP 200 with `success:false`, `collected_count:0`, and `processed_count:0`.
- Live logs show the root cause: Cookie pool contains zero cookies; WBI key refresh was rejected as not logged in. This is configuration/login state, not a silent collection failure.

## Bug 3 - Tag cloud contract, progress, false 0/0

Fix:
- `web/routers/hotspot.py`: response normalization guarantees `video_count`, `tag_count`, `progress: 100`, and `stage: completed`.
- `web/frontend/static/js/app.js`: shows a native progress bar before the request and a completed progress bar with the returned statistics.
- `modules/hotspot/tag_cloud.py`: no-video collection now raises a clear Cookie/network/risk-control error instead of returning misleading `0/0` data.

Evidence:
- Regression script exercised the no-video branch and asserted a clear `RuntimeError` containing the Cookie guidance.
- Live `POST /api/hotspot/tag-cloud` returned HTTP 400 when data collection could not proceed; it no longer returns a fake successful empty word cloud.

## Bug 4 - Category top-UP parsing and silent ranking failures

Fix:
- `modules/up_analyzer/data_fetcher.py`: supports aliases such as `游戏区` and `游戏分区`.
- Unsupported categories, empty ranking responses, and ranking exceptions now raise explicit errors rather than returning `[]` and presenting zero UPs.
- Error notes that the current `/x/web-interface/ranking/v2` call does not require WBI signing; Cookie/login state and risk control are the first checks.

Evidence:
- Regression script called `fetch_category_top_ups("游戏区")` with an empty ranking stub and verified the explicit Cookie/risk-control error.
- Live invalid-category request produced HTTP 500 and service logs contain `不支持的B站分区` instead of a zero-count success response.

## Bug 5 - Invalid Qwen model and opaque model_not_found

Fix:
- `dist/config/config.yaml`: changed `model: qwen3.7` to valid `model: qwen-plus`.
- Added an inline configuration reminder that DashScope API Key must be supplied through the configuration page or environment and must not be committed.
- `llm/client.py`: translates `model_not_found` / `model does not exist` into an actionable Chinese message naming valid Qwen models.

Evidence:
- Regression script simulated HTTP 400 `model_not_found` and asserted the Chinese `LLM模型不存在` response.
- Note: the repository currently has no configured API key, so a real remote LLM completion cannot be accepted until a valid DashScope key is configured.

## Bug 6 - db_manager NoneType

Fix:
- `web/main.py`: FastAPI lifespan now calls `init_database()`.
- `modules/self_diagnosis/self_analyzer.py`, `bilibili/cookie_pool.py`, and `llm/client.py` now use module-level `get_session()` rather than importing a stale `db_manager` reference.
- Removed the unused direct `db_manager` import in `web/routers/analysis.py`.

Evidence:
- Regression script resets `core.database.db_manager` to `None`, enters the actual FastAPI lifespan, and asserts initialization succeeds.
- Live startup log contains `数据库表创建完成` before routes are served.

## Verification Commands

```text
python verify_bugfix_20260820.py
# Result: ALL_BUGFIX_VERIFICATIONS_PASSED

python -m uvicorn web.main:app --host 127.0.0.1 --port 8012
# Live checks: GET /health = 200, GET / = 200
```

## Residual Operational Requirement

Bilibili collection and real LLM requests still require valid user-provided credentials:
- Add a valid Bilibili logged-in Cookie through the application login/configuration flow.
- Configure a valid DashScope API Key before using LLM strategy analysis.

The code now reports these prerequisites explicitly instead of silently returning empty data or producing frontend `undefined` errors.
