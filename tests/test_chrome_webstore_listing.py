from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LISTING = ROOT / "docs/chrome-webstore-listing.md"
ASSET_DIR = ROOT / "docs/images/chrome-web-store"
EXPECTED = [
    "01-seven-platform-recommendations.png",
    "02-three-surfaces.png",
    "03-truthful-status-local-data.png",
]


def test_store_listing_names_all_supported_platforms_and_local_backend() -> None:
    text = LISTING.read_text(encoding="utf-8")
    for label in (
        "B站",
        "小红书",
        "抖音",
        "YouTube",
        "X",
        "知乎",
        "Reddit",
        "Bangumi",
        "V2EX",
    ):
        assert label in text
    assert "本地后端" in text
    assert "数据默认保存在你的本机" in text
    assert "只检查 A2 Cookie 是否存在" in text
    assert "不访问、存储或发送 Cookie 值" in text
    assert "不向 V2EX 发帖、回复、感谢、收藏、取消收藏或关注 Node" in text


def test_store_listing_assets_have_stable_order_dimensions_and_visual_detail() -> None:
    assert [path.name for path in sorted(ASSET_DIR.glob("*.png"))] == EXPECTED
    for name in EXPECTED:
        with Image.open(ASSET_DIR / name) as image:
            assert image.size == (1280, 800)
            assert image.mode in {"RGB", "RGBA"}
            colors = image.convert("RGB").resize((64, 40)).getcolors(maxcolors=2560) or []
            assert len(colors) > 80


def test_listing_document_declares_dashboard_upload_order() -> None:
    text = LISTING.read_text(encoding="utf-8")
    offsets = [text.index(name) for name in EXPECTED]
    assert offsets == sorted(offsets)
