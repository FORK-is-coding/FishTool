---
name: verify
description: Run FishTool and observe the UP category and comment-alert fixes through its UI and public HTTP endpoints.
---

# FishTool runtime verification

No project verifier existed when the two query bugs were fixed. The observed recipe:

1. Use Python 3.11+ with the non-GUI dependencies in `requirements.txt`. Run the web app (`python app.py web --port <free-local-port>`) against disposable data/config, not a user's live account or database. Keep the bind loopback-only.
2. For deterministic offline verification, provide a local Bilibili HTTP fixture at the transport boundary; do not replace the business methods under review. Keep actual FastAPI routing/lifespan, Bilibili response parsing, SQLAlchemy queries and the frontend. Deny non-loopback outbound traffic.
3. Fixture one ranked video with `stat.view=100`, owner UID 7, relation follower 500, charge count 3, and `/x/space/upstat?mid=7` response `{"code":0,"data":{"archive":{"view":9999}}}`.
4. Open the running UI, dismiss the welcome notice, enter **头部UP拆解**, set count to 1 and click **获取头部UP主**. Capture the rendered `总播放: 9999`; it must not be 0 or the video's 100.
5. Seed two Video rows with different BV IDs and alerts for each. Call `GET /api/comment/alerts?bvid=<first-bv>` over HTTP. Capture only that video's alerts, newest first. Probe unknown BV, numeric ID passed as BV, omitted BV, combined level/is_read/limit and repeated reads.
6. Probe owner-specific upstat zero, legacy `archive_view`, empty response and upstream error followed by another successful owner. Zero must stay zero; one owner's error must not reuse another owner's value or stop later owners.
7. Save response bodies and UI text with the base commit and fixture conditions. Stop owned processes and close owned tabs. A local fixture PASS does not establish live Bilibili availability, GUI desktop behavior or pytest/CI status.

Observed at base `efb5719`: before the fix the UI returned total_play 0 due to NameError and BV-filtered alerts were empty despite seeded rows. After the fix the same surfaces returned 9999 and four matching alerts. The standalone session harness and full captures were retained in the PR1102 workspace and summarized in the PR description.
