from __future__ import annotations

import asyncio

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import foreign_media
from backend.agents.discovery.foreign_media import ForeignMediaSourceAdapter, foreign_language_plan_for_profile
from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile
from backend.agents.discovery.web_search import SearchHit
from backend.agents.editor import prepare_issue_articles
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.enrichment import enrich_article_with_model, enrich_articles
from backend.agents.model import ModelClientError
from backend.app.db import database
from backend.app.main import create_app
from backend.app.services import foreign_article_translation
from fastapi.testclient import TestClient


def _runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(tmp_path / "data" / "db" / "morning_dispatch.sqlite3"))
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(tmp_path / "missing-hermes.env"))


def test_trusted_language_config_matches_supported_language_set() -> None:
    languages = {item["code"]: item for item in trusted_language_options()}

    assert len(languages) == 49
    assert languages["zh"]["name"] == "Mandarin Chinese"
    assert languages["yue"]["scripts"] == ["Hans", "Hant"]
    assert languages["th"]["scripts"] == ["Thai"]
    assert languages["tpi"]["name"] == "Tok Pisin"
    assert languages["tvl"]["name"] == "Vaiaku"


def test_foreign_language_plan_derives_entity_languages(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict(
        {
            "statement": "Track SK Hynix and Kioxia memory news from local media.",
            "scope": "Memory makers and HBM supply signals",
            "source_selection": {"web_search": True, "foreign_media": True},
        }
    )

    plan = asyncio.run(foreign_language_plan_for_profile(profile))

    assert [item["code"] for item in plan] == ["ko", "ja"]
    assert any("SK하이닉스" in item["native_query"] for item in plan)
    assert any("キオクシア" in item["native_query"] for item in plan)


def test_foreign_language_plan_does_not_treat_english_exclusion_as_language_request(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict(
        {
            "statement": "Track SK Hynix and Kioxia from local media, avoiding English-language pages.",
            "scope": "Native-language semiconductor coverage",
            "exclusions": ["English-language pages", "Yahoo", "MSN"],
            "source_selection": {"foreign_media": True},
        }
    )

    plan = asyncio.run(foreign_language_plan_for_profile(profile))

    assert "en" not in [item["code"] for item in plan]
    assert [item["code"] for item in plan] == ["ko", "ja"]


def test_foreign_media_adapter_emits_translation_payloads(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        calls.append({"query": query, "limit": limit, "language": language})
        return [
            SearchHit(
                title="SK하이닉스 HBM 투자 확대",
                url="https://example.kr/news/1",
                snippet="SK하이닉스가 HBM 생산 투자를 확대했다.",
                score=0.8,
                provider="fake",
            )
        ]

    monkeypatch.setattr(foreign_media, "search_web", fake_search_web)
    profile = TopicProfile.from_dict(
        {
            "statement": "Track SK Hynix.",
            "scope": "SK Hynix HBM investment signals",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": [
                {
                    "code": "ko",
                    "name": "Korean",
                    "native_query": "SK하이닉스 HBM 투자 최신 뉴스",
                    "native_entity_terms": ["SK하이닉스"],
                    "reason": "because you're tracking SK Hynix",
                }
            ],
        }
    )

    candidates = asyncio.run(
        ForeignMediaSourceAdapter().query(profile, SourceAdapterContext(exploration_id="explore-1"))
    )

    assert calls[0]["language"] == "ko"
    assert calls[0]["limit"] == 20
    assert candidates[0].adapter == "foreign_media"
    assert candidates[0].payload.source_type == "foreign_web"
    assert candidates[0].payload.metadata["needs_translation"] is True
    assert candidates[0].payload.metadata["source_language"] == "ko"
    assert candidates[0].payload.metadata["foreign_quality"]["decision"] == "include"


def test_foreign_media_accepts_up_to_ten_languages(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    plan = [
        {"code": code, "name": name, "native_query": f"{name} semiconductor news"}
        for code, name in (
            ("ko", "Korean"),
            ("ja", "Japanese"),
            ("zh", "Mandarin Chinese"),
            ("de", "German"),
            ("fr", "French"),
            ("es", "Spanish"),
            ("pt", "Portuguese"),
            ("it", "Italian"),
            ("nl", "Dutch"),
            ("pl", "Polish"),
            ("tr", "Turkish"),
        )
    ]
    profile = TopicProfile.from_dict(
        {
            "statement": "Track semiconductor supply chains in foreign media.",
            "scope": "Native-language semiconductor coverage",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": plan,
        }
    )

    selected = asyncio.run(foreign_language_plan_for_profile(profile))

    assert len(selected) == 10
    assert [item["code"] for item in selected][-1] == "pl"


def test_foreign_media_adapter_filters_english_and_low_quality_results(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        assert language == "ko"
        return [
            SearchHit(
                title="SK하이닉스, HBM 투자 확대",
                url="https://www.thelec.kr/news/articleView.html?idxno=1",
                snippet="SK하이닉스가 HBM 생산 투자를 확대했다.",
                score=0.65,
                provider="fake",
            ),
            SearchHit(
                title="Why Micron and SK Hynix Could Quietly Become the Real AI Winners",
                url="https://finance.yahoo.com/news/micron-hynix-ai-memory-120000000.html",
                snippet="English-language syndicated market commentary about memory stocks and AI winners.",
                score=0.95,
                provider="fake",
            ),
            SearchHit(
                title="SanDisk-Kioxia Alliance Through 2034 - Maxthon",
                url="https://blog.maxthon.com/2026/02/08/sandisk-kioxia-alliance-through-2034",
                snippet="A browser blog post about NAND.",
                score=0.9,
                provider="fake",
            ),
        ]

    monkeypatch.setattr(foreign_media, "search_web", fake_search_web)
    profile = TopicProfile.from_dict(
        {
            "statement": "Track SK Hynix memory coverage from Korean sources.",
            "scope": "Korean HBM investment signals",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": [
                {
                    "code": "ko",
                    "name": "Korean",
                    "native_query": "SK하이닉스 HBM 투자 최신 뉴스",
                    "native_entity_terms": ["SK하이닉스"],
                }
            ],
        }
    )

    candidates = asyncio.run(ForeignMediaSourceAdapter().query(profile, SourceAdapterContext(exploration_id="ko-quality")))

    assert len(candidates) == 1
    assert candidates[0].payload.original_url == "https://www.thelec.kr/news/articleView.html?idxno=1"
    assert candidates[0].payload.metadata["foreign_quality"]["reason"] == "preferred local business/technology source"


def test_foreign_media_adapter_prefers_taiwanese_native_sources(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        assert language == "zh"
        return [
            SearchHit(
                title="台積電先進製程需求升溫",
                url="https://technews.tw/2026/05/01/tsmc-ai-demand/",
                snippet="台積電先進製程與 AI 需求相關報導。",
                score=0.6,
                provider="fake",
            ),
            SearchHit(
                title="TSMC shares rise as AI demand improves",
                url="https://example.com/markets/tsmc-ai-demand",
                snippet="English market recap with no Chinese source text in title or summary.",
                score=0.9,
                provider="fake",
            ),
        ]

    monkeypatch.setattr(foreign_media, "search_web", fake_search_web)
    profile = TopicProfile.from_dict(
        {
            "statement": "Track TSMC using Taiwan media.",
            "scope": "Taiwan semiconductor coverage",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": [
                {
                    "code": "zh",
                    "name": "Mandarin Chinese",
                    "native_query": "台積電 AI 需求 半導體 最新",
                    "native_entity_terms": ["台積電"],
                }
            ],
        }
    )

    candidates = asyncio.run(ForeignMediaSourceAdapter().query(profile, SourceAdapterContext(exploration_id="tw-quality")))

    assert len(candidates) == 1
    assert candidates[0].payload.original_url == "https://technews.tw/2026/05/01/tsmc-ai-demand/"
    assert candidates[0].payload.metadata["foreign_quality"]["reason"] == "preferred local business/technology source"


def test_foreign_metadata_translation_preserves_original() -> None:
    class FakeModel:
        config = type("Config", (), {"model": "fake-gemma"})()

        async def complete_json(self, **_kwargs):
            return {
                "can_translate": True,
                "confidence": "high",
                "detected_language": "ko",
                "title_en": "SK Hynix expands HBM investment",
                "body_en": "SK Hynix expanded investment in HBM production.",
                "drop_reason": "",
            }

    payload = NormalizedPayload(
        source_type="foreign_web",
        source_name="Example Korea",
        raw_text="SK하이닉스가 HBM 생산 투자를 확대했다.",
        original_url="https://example.kr/news/1",
        metadata={
            "needs_translation": True,
            "source_language": "ko",
            "source_language_name": "Korean",
            "original_search_title": "SK하이닉스 HBM 투자 확대",
            "original_search_summary": "SK하이닉스가 HBM 생산 투자를 확대했다.",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://example.kr/news/1",
        final_url="https://example.kr/news/1",
        title="SK하이닉스 HBM 투자 확대",
        text="SK하이닉스가 HBM 생산 투자를 확대했다.",
        excerpt="SK하이닉스가 HBM 생산 투자를 확대했다.",
        domain="example.kr",
        status="fetched",
        link_score=0.8,
    )

    translated = asyncio.run(enrich_article_with_model(result, model_client=FakeModel()))

    assert translated.title == "SK Hynix expands HBM investment"
    assert translated.editor_summary == "SK Hynix expanded investment in HBM production."
    assert translated.metadata["translation"]["translated"] is True
    assert translated.metadata["translation"]["original_title"] == "SK하이닉스 HBM 투자 확대"


def test_full_translation_is_baked_into_modal_without_lazy_fetch() -> None:
    """A fully-translated foreign article delivers its whole body in the modal,
    marked loaded so the on-open re-translation fetch is skipped."""
    payload = NormalizedPayload(
        source_type="foreign_web",
        source_name="sedaily.com",
        original_url="https://sedaily.com/news/1",
        metadata={"source_language": "ko", "source_language_name": "Korean"},
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://sedaily.com/news/1",
        final_url="https://sedaily.com/news/1",
        title="Samsung 1c DRAM update",
        text=(
            "First translated paragraph about Samsung 1c DRAM.\n\n"
            "Second translated paragraph covering the full body details."
        ),
        excerpt="Short summary only.",
        editor_summary="Short summary only.",
        domain="sedaily.com",
        status="fetched",
        metadata={
            "translation": {
                "translated": True,
                "mode": "assess_and_translate",
                "source_language": "ko",
                "source_language_name": "Korean",
                "confidence": "high",
                "translator": "fake-gemma",
                "original_title": "삼성 1c DRAM",
                "original_summary": "요약",
                "original_body": "원문 본문 전체",
            }
        },
    )

    html = database._render_foreign_article_modal(result, "foreign-abc", "expl-1")

    assert 'data-foreign-loaded="true"' in html
    assert "First translated paragraph about Samsung 1c DRAM." in html
    assert "Second translated paragraph covering the full body details." in html
    assert "원문 본문 전체" in html  # full original preserved for the Original tab
    assert "Short summary only." not in html  # the summary stub is replaced by the full body


def test_metadata_only_translation_modal_still_lazy_loads() -> None:
    """Metadata-only translations have no full body, so the modal must keep the
    lazy fetch (no loaded flag) and fall back to the summary stub."""
    payload = NormalizedPayload(
        source_type="foreign_web",
        source_name="sedaily.com",
        original_url="https://sedaily.com/news/2",
        metadata={"source_language": "ko", "source_language_name": "Korean"},
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://sedaily.com/news/2",
        final_url="https://sedaily.com/news/2",
        title="Headline",
        text="",
        excerpt="Translated summary stub.",
        editor_summary="Translated summary stub.",
        domain="sedaily.com",
        status="fetched",
        metadata={
            "translation": {
                "translated": True,
                "mode": "metadata",
                "source_language": "ko",
                "source_language_name": "Korean",
            }
        },
    )

    html = database._render_foreign_article_modal(result, "foreign-def", "expl-1")

    assert 'data-foreign-loaded="true"' not in html
    assert "Translated summary stub." in html


def test_foreign_translation_assess_failed_drops_article() -> None:
    """When complete_json raises, the article is dropped with mode='assess_failed'."""

    class FakeFallbackModel:
        config = type("Config", (), {"model": "fake-gemma"})()

        async def complete_json(self, **_kwargs):
            raise ModelClientError("bad json", status="parse_error")

    payload = NormalizedPayload(
        source_type="foreign_web",
        source_name="Example Korea",
        raw_text="SK하이닉스가 HBM 생산 투자를 확대했다.",
        original_url="https://example.kr/news/1",
        metadata={
            "needs_translation": True,
            "source_language": "ko",
            "source_language_name": "Korean",
            "original_search_title": "SK하이닉스 HBM 투자 확대",
            "original_search_summary": "SK하이닉스가 HBM 생산 투자를 확대했다.",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://example.kr/news/1",
        final_url="https://example.kr/news/1",
        title="SK하이닉스 HBM 투자 확대",
        text="SK하이닉스가 HBM 생산 투자를 확대했다.",
        excerpt="SK하이닉스가 HBM 생산 투자를 확대했다.",
        domain="example.kr",
        status="fetched",
        link_score=0.8,
    )

    demoted = asyncio.run(enrich_article_with_model(result, model_client=FakeFallbackModel()))

    # Article is dropped — original title preserved in translation metadata
    assert demoted.title == "SK하이닉스 HBM 투자 확대"
    assert demoted.tier == "dropped"
    assert demoted.metadata["translation"]["translated"] is False
    assert demoted.metadata["translation"]["mode"] == "assess_failed"
    assert demoted.metadata["translation"]["original_title"] == "SK하이닉스 HBM 투자 확대"


def test_non_english_web_result_is_translated_even_outside_model_item_budget() -> None:
    class FakeModel:
        config = type("Config", (), {"model": "fake-gemma"})()

        async def complete_json(self, **_kwargs):
            return {
                "can_translate": True,
                "confidence": "high",
                "detected_language": "th",
                "title_en": "SK Hynix Q4 results beat expectations",
                "body_en": "SK Hynix's strong HBM results may support Micron and Sandisk through higher memory prices.",
                "drop_reason": "",
            }

    thai_title = "ผลประกอบการไตรมาส 4 ของ SK Hynix ที่รายงานดีเกินคาด"
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name=thai_title,
        raw_text="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        original_url="https://www.zyo71.com/2026/01/4-sk-hynix-sndk-mu.html",
        metadata={"link_quality_score": 0.95},
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url=payload.original_url,
        final_url=payload.original_url,
        title=thai_title,
        text="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        excerpt="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        domain="zyo71.com",
        status="fetched",
        link_score=0.95,
    )

    translated = asyncio.run(enrich_articles([result], model_client=FakeModel(), model_max_items=0))[0]

    assert translated.title == "SK Hynix Q4 results beat expectations"
    assert translated.editor_summary.startswith("SK Hynix's strong HBM results")
    assert translated.metadata["translation"]["translated"] is True
    assert translated.metadata["translation"]["source_language"] == "th"
    assert translated.metadata["translation"]["source_language_name"] == "Thai"
    assert translated.metadata["translation"]["original_title"] == thai_title


def test_script_detection_uses_trusted_language_scripts() -> None:
    hindi_title = "माइक्रोन और हाइनिक्स मेमोरी बाजार की ताजा खबर"
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name=hindi_title,
        raw_text="मेमोरी बाजार में मांग और कीमतों पर नई रिपोर्ट प्रकाशित हुई।",
        original_url="https://example.in/news/memory",
        metadata={"link_quality_score": 0.88},
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url=payload.original_url,
        final_url=payload.original_url,
        title=hindi_title,
        text="मेमोरी बाजार में मांग और कीमतों पर नई रिपोर्ट प्रकाशित हुई।",
        excerpt="मेमोरी बाजार में मांग और कीमतों पर नई रिपोर्ट प्रकाशित हुई।",
        domain="example.in",
        status="fetched",
        link_score=0.88,
    )

    untranslated = asyncio.run(enrich_article_with_model(result, model_client=None))

    assert untranslated.metadata["translation"]["source_language"] == "hi"
    assert untranslated.metadata["translation"]["source_language_name"] == "Hindi"


def test_untranslated_non_english_web_result_is_dropped() -> None:
    """When no model is available to translate a foreign article, it is dropped entirely."""
    thai_title = "ผลประกอบการไตรมาส 4 ของ SK Hynix ที่รายงานดีเกินคาด"
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name=thai_title,
        raw_text="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        original_url="https://www.zyo71.com/2026/01/4-sk-hynix-sndk-mu.html",
        metadata={"link_quality_score": 0.95},
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url=payload.original_url,
        final_url=payload.original_url,
        title=thai_title,
        text="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        excerpt="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        domain="zyo71.com",
        status="fetched",
        link_score=0.95,
    )

    untranslated = asyncio.run(enrich_article_with_model(result, model_client=None))

    assert untranslated.tier == "dropped"
    assert untranslated.metadata["translation"]["translated"] is False
    assert untranslated.metadata["translation"]["source_language"] == "th"

    prepared = prepare_issue_articles(
        {"interest": "SK Hynix HBM memory Micron Sandisk", "threshold": 0.05},
        [untranslated],
    )

    assert prepared == []


def test_translated_web_story_gets_translation_badge_and_modal() -> None:
    thai_title = "ผลประกอบการไตรมาส 4 ของ SK Hynix ที่รายงานดีเกินคาด"
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name=thai_title,
        raw_text="ผลกระทบเชิงบวกชัดเจน เพราะ SK Hynix เป็นผู้นำ HBM สำหรับ AI",
        original_url="https://www.zyo71.com/2026/01/4-sk-hynix-sndk-mu.html",
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url=payload.original_url,
        final_url=payload.original_url,
        title="SK Hynix Q4 results beat expectations",
        text="Translated body",
        excerpt="Translated summary",
        editor_summary="Translated summary",
        domain="zyo71.com",
        status="fetched",
        link_score=0.95,
        metadata={
            "translation": {
                "translated": True,
                "source_language": "th",
                "source_language_name": "Thai",
                "original_title": thai_title,
                "original_summary": payload.raw_text,
            }
        },
    )

    html = database.render_ingested_issue(
        "Translated web brief",
        "One translated web story.",
        [],
        [result],
        lookback_hours=24,
        issue_id="explore-1",
    )

    assert '<span class="source-type web">Web</span>' in html
    assert "TH -&gt; EN" in html or "TH -&amp;gt; EN" in html
    assert "data-foreign-article-target" in html
    assert "foreign-modal" in html
    assert f"via {thai_title}" not in html
    assert "via SK Hynix Q4 results beat expectations" in html


def test_foreign_story_renders_lazy_translation_modal() -> None:
    payload = NormalizedPayload(
        source_type="foreign_web",
        source_name="Example Korea",
        raw_text="",
        original_url="https://example.kr/news/1",
        metadata={
            "source_language": "ko",
            "source_language_name": "Korean",
            "original_search_title": "원문 제목",
            "original_search_summary": "원문 요약",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://example.kr/news/1",
        final_url="https://example.kr/news/1",
        title="Translated title",
        text="Translated summary",
        excerpt="Translated summary",
        editor_summary="Translated summary",
        domain="example.kr",
        status="fetched",
        tier="lead",
        relevance_score=0.9,
        metadata={
            "translation": {
                "translated": True,
                "source_language": "ko",
                "source_language_name": "Korean",
                "original_title": "원문 제목",
                "original_summary": "원문 요약",
            }
        },
    )

    html = database.render_ingested_issue(
        "Foreign media brief",
        "One translated story.",
        [],
        [result],
        lookback_hours=24,
        issue_id="explore-1",
    )

    assert "data-foreign-article-target" in html
    assert "foreign-modal" in html
    assert "KO -&gt; EN" in html or "KO -&amp;gt; EN" in html
    assert "via 원문 제목" not in html
    assert "via Translated title" in html
    assert "/foreign-article/translation" in html
    assert ".split(/\\n{2,}/)" in html
    assert '.join("\\n\\n")' in html
    assert ".split(/\n{2,}/)" not in html
    assert '.join("\n\n")' not in html


def test_foreign_article_translation_endpoint_requires_saved_brief_url(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    database.init_database()
    profile = database.upsert_topic_profile(
        {
            "statement": "Track Korean memory news",
            "scope": "Korean HBM signals",
            "source_selection": {"foreign_media": True},
        }
    )
    exploration = database.create_exploration(
        topic_id=profile["topic_id"],
        mode="show_now",
        source_selection={"foreign_media": True},
        status="complete",
    )
    output_dir = tmp_path / "data" / "digest-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    brief_path = output_dir / "brief.html"
    brief_path.write_text('<a href="https://example.kr/news/1">Story</a>', encoding="utf-8")
    database.update_exploration_status(
        exploration["exploration_id"],
        status="complete",
        brief_ref=str(brief_path),
    )

    async def fake_translate(payload: dict):
        return {"status": "translated", "url": payload["url"], "translated_body": "English body."}

    monkeypatch.setattr(foreign_article_translation, "translate_foreign_article", fake_translate)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        allowed = client.post(
            f"/api/explore/explorations/{exploration['exploration_id']}/foreign-article/translation",
            json={"url": "https://example.kr/news/1"},
        )
        blocked = client.post(
            f"/api/explore/explorations/{exploration['exploration_id']}/foreign-article/translation",
            json={"url": "https://not-in-brief.example/news"},
        )

    assert allowed.status_code == 200
    assert allowed.json()["translated_body"] == "English body."
    assert blocked.status_code == 403


def test_foreign_article_translation_uses_text_fallback_when_json_fails(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    class FakeFallbackModel:
        async def complete_json(self, **_kwargs):
            raise ModelClientError("bad json", status="parse_error")

        async def complete(self, **_kwargs):
            return (
                "Title: SK Hynix company analysis report\n"
                "Body:\n"
                "SK Hynix is expanding high-value memory sales around HBM and server DRAM. "
                "The report says capacity investment supports growth, but supply discipline and demand durability remain important risks."
            )

    korean_body = "SK하이닉스는 고부가 메모리 판매를 확대하고 있다. " * 12
    fetched_result = ArticleFetchResult(
        payload=NormalizedPayload(
            source_type="foreign_web",
            source_name="Example Korea",
            raw_text="SK하이닉스 종합 기업분석 리포트",
            original_url="https://example.kr/news/1",
        ),
        original_url="https://example.kr/news/1",
        final_url="https://example.kr/news/1",
        title="SK하이닉스 종합 기업분석 리포트",
        text=korean_body,
        excerpt=korean_body,
        domain="example.kr",
        status="fetched",
    )

    async def fake_fetch_articles(*_args, **_kwargs):
        return [fetched_result]

    monkeypatch.setattr(foreign_article_translation, "fetch_articles_for_payloads", fake_fetch_articles)
    monkeypatch.setattr(
        foreign_article_translation.ModelClient,
        "from_settings",
        staticmethod(lambda _settings: FakeFallbackModel()),
    )

    response = asyncio.run(
        foreign_article_translation.translate_foreign_article(
            {
                "url": "https://example.kr/news/1",
                "title": "SK하이닉스 종합 기업분석 리포트",
                "summary": "SK하이닉스는 고부가 메모리 판매를 확대하고 있다.",
                "source_language": "ko",
                "source_language_name": "Korean",
            }
        )
    )

    assert response["status"] == "translated"
    assert response["translated_title"] == "SK Hynix company analysis report"
    assert "high-value memory sales" in response["translated_body"]
    assert response["translation_quality"] == "medium"
    assert response["mode"] == "fallback_text"


def test_foreign_article_translation_preserves_quality_and_paragraphs(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    class FakeJsonModel:
        config = type("Config", (), {"model": "fake-gemma"})()

        async def complete_json(self, **_kwargs):
            return {
                "title_en": "TSMC demand improves",
                "body_en": "TSMC demand improved in AI chips.\n\nManagement still flagged export and margin risks.",
                "quality": "low",
            }

    fetched_result = ArticleFetchResult(
        payload=NormalizedPayload(
            source_type="foreign_web",
            source_name="TechNews",
            raw_text="台積電需求升溫",
            original_url="https://technews.tw/2026/05/01/tsmc-ai-demand/",
        ),
        original_url="https://technews.tw/2026/05/01/tsmc-ai-demand/",
        final_url="https://technews.tw/2026/05/01/tsmc-ai-demand/",
        title="台積電需求升溫",
        text="台積電 AI 晶片需求升溫。\n\n管理層仍提醒出口與毛利風險。",
        excerpt="台積電 AI 晶片需求升溫。",
        domain="technews.tw",
        status="fetched",
    )

    async def fake_fetch_articles(*_args, **_kwargs):
        return [fetched_result]

    monkeypatch.setattr(foreign_article_translation, "fetch_articles_for_payloads", fake_fetch_articles)
    monkeypatch.setattr(
        foreign_article_translation.ModelClient,
        "from_settings",
        staticmethod(lambda _settings: FakeJsonModel()),
    )

    response = asyncio.run(
        foreign_article_translation.translate_foreign_article(
            {
                "url": "https://technews.tw/2026/05/01/tsmc-ai-demand/",
                "title": "台積電需求升溫",
                "summary": "台積電 AI 晶片需求升溫。",
                "source_language": "zh",
                "source_language_name": "Mandarin Chinese",
            }
        )
    )

    assert response["translation_quality"] == "low"
    assert response["translator"] == "fake-gemma"
    assert response["mode"] == "json"
    assert "\n\nManagement still flagged" in response["translated_body"]
    assert "Translation confidence is low" in response["notice"]


def test_foreign_article_translation_model_unavailable_is_not_cached(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    class FakeJsonModel:
        config = type("Config", (), {"model": "fake-gemma"})()

        async def complete_json(self, **_kwargs):
            return {
                "title_en": "Kioxia demand improves",
                "body_en": "Kioxia demand improved.",
                "quality": "high",
            }

    fetched_result = ArticleFetchResult(
        payload=NormalizedPayload(
            source_type="foreign_web",
            source_name="Example Japan",
            raw_text="キオクシア需要改善",
            original_url="https://example.jp/news/1",
        ),
        original_url="https://example.jp/news/1",
        final_url="https://example.jp/news/1",
        title="キオクシア需要改善",
        text="キオクシアの需要改善とNAND市況の回復について詳しく報じた本文です。" * 12,
        excerpt="キオクシア需要改善。",
        domain="example.jp",
        status="fetched",
    )

    async def fake_fetch_articles(*_args, **_kwargs):
        return [fetched_result]

    monkeypatch.setattr(foreign_article_translation, "fetch_articles_for_payloads", fake_fetch_articles)
    monkeypatch.setattr(foreign_article_translation.ModelClient, "from_settings", staticmethod(lambda _settings: None))

    unavailable = asyncio.run(
        foreign_article_translation.translate_foreign_article(
            {
                "url": "https://example.jp/news/1",
                "title": "キオクシア需要改善",
                "summary": "キオクシア需要改善。",
                "source_language": "ja",
                "source_language_name": "Japanese",
            }
        )
    )

    monkeypatch.setattr(
        foreign_article_translation.ModelClient,
        "from_settings",
        staticmethod(lambda _settings: FakeJsonModel()),
    )
    translated = asyncio.run(
        foreign_article_translation.translate_foreign_article(
            {
                "url": "https://example.jp/news/1",
                "title": "キオクシア需要改善",
                "summary": "キオクシア需要改善。",
                "source_language": "ja",
                "source_language_name": "Japanese",
            }
        )
    )

    assert unavailable["status"] == "translation_unavailable"
    assert translated["status"] == "translated"
    assert translated["cached"] is False
