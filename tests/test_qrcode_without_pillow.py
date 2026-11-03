"""二维码出图不依赖 Pillow。

回归：环境里有 qrcode 但没有 Pillow 时，qrcode 默认工厂回退为 PyPNGImage，
旧代码 ``img.save(buf, format='PNG')`` 抛 TypeError，``GET /api/auth/qrcode`` 返回 500。
这里在装了 Pillow 的环境里模拟缺失，CI 上也能走到同一条回退路径。
"""
import asyncio
import sys

import pytest
import qrcode

import bilibili.auth as auth_module
from bilibili.api import BilibiliAPI
from bilibili.auth import QRCodeLogin

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCAN_URL = "https://passport.bilibili.com/h5-app/passport/login/scan"


@pytest.fixture
def without_pillow(monkeypatch):
    """让 ``import PIL`` 失败，并清掉 qrcode 缓存的 PIL 引用。

    qrcode 在 ``qrcode.compat.pil`` 里缓存 ``Image``，默认工厂据此选 PilImage 或 PyPNGImage；
    只屏蔽 PIL 不清这个缓存，qrcode 仍会走 PilImage，测不到回退路径。
    """
    for name in list(sys.modules):
        if name == "PIL" or name.startswith("PIL.") or name in ("qrcode.image.pil", "qrcode.compat.pil"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "PIL", None)


@pytest.fixture
def passport_stub(monkeypatch):
    """替换 B站 passport 请求，不走网络。"""

    async def fake_get(self, url, params=None, **kwargs):
        return {"url": SCAN_URL, "qrcode_key": "k"}

    async def fake_noop(self, *args, **kwargs):
        return None

    monkeypatch.setattr(BilibiliAPI, "get", fake_get)
    monkeypatch.setattr(BilibiliAPI, "init_session", fake_noop)
    monkeypatch.setattr(BilibiliAPI, "close", fake_noop)


def test_without_pillow_fixture_hits_qrcode_fallback(without_pillow):
    """前提检查：fixture 生效后 qrcode 自己判定没有 Pillow（默认工厂会回退为 PyPNGImage）。"""
    import qrcode.compat.pil as compat_pil

    assert compat_pil.Image is None


def test_generate_qrcode_passes_pure_png_factory(monkeypatch, passport_stub):
    """generate_qrcode 显式指定 PyPNGImage，不依赖 qrcode 的默认工厂选择。"""
    from qrcode.image.pure import PyPNGImage

    seen = {}
    real_qrcode = qrcode.QRCode

    class SpyQRCode(real_qrcode):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(auth_module.qrcode, "QRCode", SpyQRCode)
    asyncio.run(QRCodeLogin().generate_qrcode())

    assert seen.get("image_factory") is PyPNGImage


def test_generate_qrcode_without_pillow(without_pillow, passport_stub):
    """缺 Pillow 时仍返回合法 PNG。"""
    with pytest.raises(ImportError):
        import PIL  # noqa: F401

    url, image = asyncio.run(QRCodeLogin().generate_qrcode())

    assert url == SCAN_URL
    assert image.startswith(PNG_SIGNATURE)


def test_qrcode_png_matches_pillow_rendering(passport_stub):
    """PyPNG 出的图与原先 Pillow 出的图尺寸、像素一致（同样的 box_size=10、border=4）。"""
    pil_image = pytest.importorskip("PIL.Image")
    from io import BytesIO

    from qrcode.image.pil import PilImage

    _, image = asyncio.run(QRCodeLogin().generate_qrcode())
    ours = pil_image.open(BytesIO(image)).convert("1")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
        image_factory=PilImage,
    )
    qr.add_data(SCAN_URL)
    qr.make(fit=True)
    reference = qr.make_image(fill_color="black", back_color="white").get_image().convert("1")

    assert ours.size == reference.size
    assert ours.tobytes() == reference.tobytes()
