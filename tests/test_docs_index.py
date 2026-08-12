import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docs_homepage_mentions_current_platform_sources() -> None:
    html = (ROOT / "docs/index.html").read_text(encoding="utf-8")
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]

    assert "Reddit 推荐" in html
    assert "sourceRedditTitle" in html
    assert "sourceRedditText" in html
    assert "Bangumi 推荐" in html
    assert "sourceBangumiTitle" in html
    assert "sourceBangumiText" in html
    assert "V2EX 推荐" in html
    assert "sourceV2exTitle" in html
    assert "sourceV2exText" in html
    assert "sourceLinuxdoTitle" in html
    assert "sourceLinuxdoText" in html
    assert "sourceWeiboTitle" in html
    assert "sourceWeiboText" in html
    assert "十一类平台来源与开放 Web" in html
    assert "Eleven platform sources and the open web" in html
    assert "登录微博后可在初始化时只读导入收藏、关注和互动" in html
    assert f'"softwareVersion": "{project_version}"' in html


def test_docs_homepage_mentions_macos_first_launch_security_bypass() -> None:
    html = (ROOT / "docs/index.html").read_text(encoding="utf-8")

    assert "OpenBiliClaw-macos-v*-arm64.dmg" in html
    assert "Control-click" in html
    assert "隐私与安全性" in html
    assert "已损坏" in html
    assert "xattr -dr com.apple.quarantine /Applications/OpenBiliClaw.app" in html
    assert "README bypass steps" not in html


def test_docs_homepage_does_not_call_github_rest_from_the_browser() -> None:
    html = (ROOT / "docs/index.html").read_text(encoding="utf-8")

    assert "api.github.com" not in html
    assert "stargazers_count" not in html
    assert "https://github.com/whiteguo233/OpenBiliClaw" in html
