"""桌面端诊断报告 PDF 导出。

前端只负责把后端生成的打印 HTML 交给桌面壳；这里负责渲染成 PDF。
不依赖 wkhtmltopdf / pdfkit，避免用户机器缺少外部二进制时降级成 Markdown。
"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtGui import QFont, QTextDocument
from PyQt5.QtPrintSupport import QPrinter

from core.logger import get_logger

logger = get_logger(__name__)


def save_html_as_pdf(html_content: str, output_path: str) -> tuple[bool, str]:
    """把打印 HTML 渲染为 PDF 文件。

    Args:
        html_content: 后端生成的完整 HTML 文档。
        output_path: 用户选择的保存路径。

    Returns:
        tuple[bool, str]: ``(是否成功, 成功时为路径 / 失败时为错误信息)``。
    """
    if not html_content or not str(html_content).strip():
        return False, "报告内容为空，无法生成 PDF。"

    try:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        document = QTextDocument()
        document.setDocumentMargin(0)
        # 显式指定中文字体，避免不同机器回退到缺字字体。
        document.setDefaultFont(QFont("Microsoft YaHei", 10))
        document.setHtml(html_content)

        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(str(path))
        printer.setDocName("B站账号诊断报告")
        printer.setPageSize(QPrinter.A4)
        printer.setPageMargins(14, 14, 14, 14, QPrinter.Millimeter)
        document.print_(printer)

        if not path.exists() or path.stat().st_size <= 0:
            return False, "PDF 文件未生成，请检查目标目录是否可写。"
        logger.info("[报告导出] PDF 已保存: %s", path)
        return True, str(path)
    except Exception as exc:  # noqa: BLE001 - 导出失败必须回传错误文案而不是崩溃
        logger.exception("[报告导出] PDF 生成失败: %s", exc)
        return False, f"PDF 生成失败: {exc}"
