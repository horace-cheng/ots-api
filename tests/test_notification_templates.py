from services.notification.templates import render_template


def test_render_zh_tw_user_registered():
    html, text = render_template("zh-tw", "user_registered", {
        "header_title": "新用戶註冊通知",
        "footer_text": "footer",
        "footer_contact": "contact",
        "lang": "zh-tw",
        "user_email": "user@test.com",
        "env": "dev",
        "admin_dashboard_url": "https://admin.ots.tw/users",
    })
    assert "user@test.com" in html
    assert "dev" in html
    assert "admin.ots.tw" in html


def test_render_en_delivery_complete():
    html, text = render_template("en", "delivery_complete", {
        "header_title": "Translation Complete",
        "footer_text": "footer",
        "footer_contact": "contact",
        "lang": "en",
        "order_id": "ORD-001",
        "source_lang": "Taiwanese",
        "target_lang": "English",
        "output_url": "https://ots.tw/orders/ORD-001",
    })
    assert "ORD-001" in html
    assert "Taiwanese" in html
    assert "English" in html


def test_render_ja_user_registered():
    html, text = render_template("ja", "user_registered", {
        "header_title": "新規ユーザー登録通知",
        "footer_text": "footer",
        "footer_contact": "contact",
        "lang": "ja",
        "user_email": "user@test.com",
        "env": "dev",
    })
    assert "user@test.com" in html
    assert "dev" in html


def test_render_ko_user_registered():
    html, text = render_template("ko", "user_registered", {
        "header_title": "신규 사용자 등록 알림",
        "footer_text": "footer",
        "footer_contact": "contact",
        "lang": "ko",
        "user_email": "user@test.com",
        "env": "dev",
    })
    assert "user@test.com" in html


def test_render_zh_tw_user_disabled():
    html, text = render_template("zh-tw", "user_disabled", {
        "header_title": "帳號已停用",
        "footer_text": "footer",
        "footer_contact": "contact",
    })
    assert "停用" in html


def test_render_zh_tw_user_enabled():
    html, text = render_template("zh-tw", "user_enabled", {
        "header_title": "帳號已啟用",
        "footer_text": "footer",
        "footer_contact": "contact",
        "portal_url": "https://ots.tw",
    })
    assert "啟用" in html
    assert "ots.tw" in html


def test_render_en_user_disabled():
    html, text = render_template("en", "user_disabled", {
        "header_title": "Account Disabled",
        "footer_text": "footer",
        "footer_contact": "contact",
    })
    assert "disabled" in html.lower()


def test_render_en_user_enabled():
    html, text = render_template("en", "user_enabled", {
        "header_title": "Account Re-enabled",
        "footer_text": "footer",
        "footer_contact": "contact",
        "portal_url": "https://ots.tw",
    })
    assert "re-enabled" in html.lower()
    assert "ots.tw" in html


def test_render_ja_user_disabled():
    html, text = render_template("ja", "user_disabled", {
        "header_title": "アカウント無効化",
        "footer_text": "footer",
        "footer_contact": "contact",
    })
    assert "無効化" in html


def test_render_ja_user_enabled():
    html, text = render_template("ja", "user_enabled", {
        "header_title": "アカウント再有効化",
        "footer_text": "footer",
        "footer_contact": "contact",
        "portal_url": "https://ots.tw",
    })
    assert "再有効化" in html


def test_render_ko_user_disabled():
    html, text = render_template("ko", "user_disabled", {
        "header_title": "계정 비활성화",
        "footer_text": "footer",
        "footer_contact": "contact",
    })
    assert "비활성화" in html


def test_render_ko_user_enabled():
    html, text = render_template("ko", "user_enabled", {
        "header_title": "계정 재활성화",
        "footer_text": "footer",
        "footer_contact": "contact",
        "portal_url": "https://ots.tw",
    })
    assert "재활성화" in html


def test_render_unknown_template_raises():
    import jinja2
    try:
        render_template("en", "nonexistent_event", {"lang": "en"})
        assert False, "Should have raised TemplateNotFound"
    except jinja2.exceptions.TemplateNotFound:
        pass


def test_render_en_order_created_lt():
    html, text = render_template("en", "order_created_lt", {
        "header_title": "Order Created (Awaiting Quote)",
        "footer_text": "footer",
        "footer_contact": "contact",
        "order_id": "ORD-LT-001",
        "source_lang": "Taiwanese",
        "target_lang": "English",
        "word_count": "15000",
    })
    assert "ORD-LT-001" in html
    assert "awaiting quote" in html
    assert "15000" in html


def test_render_en_proofreader_assigned():
    html, text = render_template("en", "proofreader_assigned", {
        "header_title": "Proofreader Assignment",
        "footer_text": "footer",
        "footer_contact": "contact",
        "order_id": "ORD-001",
        "source_lang": "Taiwanese",
        "target_lang": "English",
        "portal_url": "https://ots.tw/proofread",
    })
    assert "ORD-001" in html
    assert "Proofreader" in html


def test_render_en_qa_review_required():
    html, text = render_template("en", "qa_review_required", {
        "header_title": "QA Review Required",
        "footer_text": "footer",
        "footer_contact": "contact",
        "order_id": "ORD-001",
        "flag_count": "3",
    })
    assert "ORD-001" in html
    assert "3" in html


def test_render_with_qa_score():
    html, text = render_template("zh-tw", "delivery_complete", {
        "header_title": "翻譯完成",
        "footer_text": "footer",
        "footer_contact": "contact",
        "lang": "zh-tw",
        "order_id": "ORD-001",
        "qa_score": "85",
        "output_url": "https://ots.tw/orders/ORD-001",
    })
    assert "85" in html
