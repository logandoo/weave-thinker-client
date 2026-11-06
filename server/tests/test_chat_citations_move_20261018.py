"""P5.3 move-only seam: chat citation callables stay identity-equal."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_chat_citation_callables_are_reexported():
    from app.api import chat, chat_citations

    assert chat._render_answer_citations is chat_citations._render_answer_citations
    assert chat._sanitize_cited_content is chat_citations._sanitize_cited_content
    assert chat._build_content_fallback is chat_citations._build_content_fallback
