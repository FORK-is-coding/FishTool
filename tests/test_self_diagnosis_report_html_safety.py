"""自诊报告 HTML 安全契约（issue #14）。

``generate_html_report`` 的产物同时交给桌面 QTextDocument、浏览器打印和 pdfkit。
报告 Markdown 含 B 站外部文本（UP 昵称、视频标题等），必须：
- 内嵌 HTML（iframe/script/img）转义为文本；
- Markdown 图片语法生成的 ``<img>`` 被剥离（Qt 会按 file:// 加载本地图片）；
- 上游自己插入的图表 data URI 仍正常嵌入；
- pdfkit 不开启 ``enable-local-file-access``。

不需要真实 wkhtmltopdf：pdfkit 用记录替身注入 ``sys.modules``。
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from modules.self_diagnosis.report_generator import ReportGenerator

CHART = (
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA'
    'DUlEQVR42mP8z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg=='
)


def _self_data(name: str, title: str = '正常标题') -> dict:
    """构造最小账号数据，外部文本放在昵称和最高播放视频标题里。"""
    return {
        'uid': 42,
        'basic_info': {'name': name, 'level': 6},
        'fan_stats': {'follower': 100, 'following': 3},
        'video_stats': {
            'max_play_video': {'title': title, 'play': 10, 'bvid': 'BV1xx411c7mD'},
        },
        'post_rhythm': {},
        'engagement_metrics': {},
        'data_availability': {},
    }


def _img_srcs(html: str) -> list[str]:
    """提取 HTML 中全部 <img> 的 src。"""
    return re.findall(r'<img\b[^>]*\bsrc="([^"]*)"', html, flags=re.IGNORECASE)


def test_html_report_escapes_inline_html(tmp_path) -> None:
    """昵称/标题里的 iframe、script、img 标签只以转义文本出现。"""
    generator = ReportGenerator(output_dir=str(tmp_path))
    html = generator.generate_html_report(_self_data(
        '攻击者<iframe src="file:///C:/Windows/win.ini"></iframe>',
        '<script>alert(1)</script><img src=file:///C:/secret.png>',
    ), None)

    assert '<iframe' not in html
    assert '<script' not in html
    assert '<img' not in html
    assert '&lt;iframe' in html
    assert '&lt;script&gt;' in html


def test_html_report_strips_markdown_image_syntax(tmp_path) -> None:
    """Markdown 图片语法不能产生本地 <img>；图表 data URI 仍正常嵌入。"""
    generator = ReportGenerator(output_dir=str(tmp_path))
    html = generator.generate_html_report(
        _self_data('攻击者![x](file:///C:/Windows/win.png)', '![y](C:/secret.png)'),
        None,
        charts={'volume': CHART, 'tag_cloud': CHART},
    )

    srcs = _img_srcs(html)
    assert srcs == [CHART, CHART]
    assert 'file:///C:/Windows/win.png' not in html
    assert '数据可视化' in html
    assert html.index('数据可视化') < html.index('数据可得性清单')


@pytest.fixture()
def recorded_pdfkit(monkeypatch) -> list:
    """把 pdfkit 换成记录替身，返回调用记录。"""
    calls: list = []

    def fake_from_string(html, output_path, options=None, **kwargs):
        calls.append({'html': html, 'options': dict(options or {})})
        Path(output_path).write_bytes(b'%PDF-1.4 fake')
        return True

    fake_pdfkit = types.ModuleType('pdfkit')
    fake_pdfkit.from_string = fake_from_string
    # generate_pdf_report 在函数内 import pdfkit，替换 sys.modules 即可生效。
    monkeypatch.setitem(sys.modules, 'pdfkit', fake_pdfkit)
    return calls


def test_pdf_report_disables_local_file_access(tmp_path, recorded_pdfkit) -> None:
    """pdfkit options 不含 enable-local-file-access，HTML 同样经过转义。"""
    generator = ReportGenerator(output_dir=str(tmp_path))
    filepath = generator.generate_pdf_report(
        _self_data('攻击者<iframe src="file:///etc/passwd"></iframe>'),
        None,
        filename='safety.pdf',
    )

    assert filepath.endswith('.pdf')
    assert len(recorded_pdfkit) == 1
    options = recorded_pdfkit[0]['options']
    assert 'enable-local-file-access' not in options
    assert options.get('encoding') == 'UTF-8'
    assert '<iframe' not in recorded_pdfkit[0]['html']
    assert '&lt;iframe' in recorded_pdfkit[0]['html']
