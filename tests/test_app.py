"""End-to-end view tests via Streamlit's AppTest.

Run with:  CSV_EDITOR_TESTMODE=1 python -m pytest tests/ -q
(TESTMODE disables dataframe cell-selection, which AppTest can't serialize.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CSV_EDITOR_TESTMODE", "1")

from streamlit.testing.v1 import AppTest

from providers.auth.base import User

NL = chr(10)
CR = chr(13)

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def fresh(**session):
    # Run once to create session state, log the user in and let the data
    # load (which resets `edits`), then seed the remaining state.
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["user"] = session.pop("user", _user())
    at = at.run()
    for k, v in session.items():
        at.session_state[k] = v
    return at.run()


def test_login_success_and_failure():
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_input(key="login_email").set_value("admin")
    at.text_input(key="login_password").set_value("nope")
    at = at.button[0].set_value(True).run()
    assert at.session_state["user"] is None
    assert any("Incorrect" in e.value for e in at.error)

    at.text_input(key="login_email").set_value("admin")
    at.text_input(key="login_password").set_value("admin")
    at = at.button[0].set_value(True).run()
    assert at.session_state["user"] is not None


def test_esc_encodes_newlines_so_markdown_cannot_split_the_grid():
    """Regression: st.markdown(unsafe_allow_html=True) parses Markdown
    before the HTML reaches the DOM, and a blank line inside a raw-HTML
    block terminates that block -- one multi-paragraph cell used to
    shatter the whole table onto the page as visible text."""
    import html as _html

    from app import esc

    source = "para one" + NL + NL + 'para "two" & <three>'
    out = esc(source)
    assert NL not in out and CR not in out
    assert "&#10;&#10;" in out
    assert "&quot;two&quot;" in out and "&lt;three&gt;" in out
    # the browser decodes it back, so the textarea editor still sees the
    # original text and a round-tripped edit loses nothing
    assert _html.unescape(out) == source


def test_rendered_grid_is_a_single_unbroken_html_block():
    """The real 988 rows carry newlines in 10 of the 17 columns; the
    emitted grid markup must still be one physical line."""
    at = fresh(user=_user())
    assert not at.exception
    # startswith, not "in": the injected <style> block also mentions
    # the .de-grid-wrap selector
    grid = [md.value for md in at.markdown
            if md.value.startswith('<div class="de-grid-wrap"')]
    assert len(grid) == 1
    assert NL not in grid[0] and CR not in grid[0]
    assert grid[0].count("<tr>") == 291          # 290 data rows + the header
    assert "&#10;" in grid[0]                    # multi-paragraph cells survived


def _grid_script(autosize):
    """Capture the JS render_grid_script() hands to st.iframe."""
    import streamlit as st

    import app as app_module

    captured = {}
    original = st.iframe
    st.iframe = lambda html, **kw: captured.setdefault("html", html)
    try:
        app_module.render_grid_script(autosize=autosize)
    finally:
        st.iframe = original
    return captured["html"]


def test_grid_script_rebinds_its_listeners_on_every_run():
    """Regression: the handlers used to be installed behind one-shot
    flags (P.__deTableBound / P.__deKeys). Streamlit destroys this iframe
    -- and the realm the handlers close over -- on a view change, so the
    review page saw the flag the editing page had set, skipped binding,
    and deferred to a dead handler: its grid rendered but would not open
    a cell editor."""
    for autosize in (True, False):
        js = _grid_script(autosize)
        assert "__deTableBound" not in js and "__deKeys " not in js
        assert "if (!P.__de" not in js          # no one-shot guards left
        for event, ref in (("dblclick", "__deTableHandler"),
                           ("keydown", "__deKeysHandler")):
            assert f"removeEventListener('{event}', P.{ref}" in js
            assert f"addEventListener('{event}', P.{ref}" in js


def test_non_autosized_grid_releases_the_editing_pages_height():
    """--de-grid-h is fitted to the 290-row editing grid; the review grid
    inherits it as dead space unless the script clears it."""
    review = _grid_script(False)
    assert "removeProperty('--de-grid-h')" in review
    assert "__deFitObserver = null" in review

    editing = _grid_script(True)
    assert "setProperty('--de-grid-h'" in editing


def _user():
    return User(username="admin", display_name="admin", provider="mock")


def test_review_validation_gates_publish():
    at = fresh(
        user=_user(),
        view="review",
        edits={(2, "website"): "broken-url", (2, "latitude"): "95", (5, "city_town"): "Toronto"},
    )
    assert not at.exception
    body = " ".join(md.value for md in at.markdown)
    assert "Review changes" in body and "invalid" in body
    publish = [b for b in at.button if "Publish" in (b.label or "")][0]
    assert publish.disabled

    at.session_state["edits"][(2, "website")] = "https://fixed.example.com"
    at.session_state["edits"][(2, "latitude")] = "43.65"
    at = at.run()
    body = " ".join(md.value for md in at.markdown)
    assert "2 rows · 3 cells changed" in body
    publish = [b for b in at.button if "Publish" in (b.label or "")][0]
    assert not publish.disabled


def test_search_keeps_hidden_edits_and_badge():
    at = fresh(
        user=_user(),
        edits={(2, "email"): "a@b.co", (5, "city"): "Portland", (9, "seats"): "7"},
        search="portland",
    )
    assert not at.exception
    body = " ".join(md.value for md in at.markdown)
    labels = " ".join(b.label or "" for b in at.button)
    assert "matching" in body
    assert "Clear search" in labels
    assert "(3)" in labels  # badge count unchanged by filtering


def test_editing_view_renders():
    at = fresh(user=_user())
    assert not at.exception
    body = " ".join(md.value for md in at.markdown)
    assert "geo_coded_988_data.parquet" in body and "290 rows" in body.replace(",", "")


def test_clear_search_undo_redo_and_logout():
    at = fresh(edits={(2, "website"): "https://x.ca"}, search="toronto")
    clear = [b for b in at.button if (b.label or "") == "Clear search"][0]
    at = clear.click().run()
    assert not at.exception
    assert at.session_state["search"] == ""

    # simulate two recorded actions the way harvest_editor records them
    at.session_state["undo_stack"] = [
        {"row": 2, "col": "website", "inst": "DELETE"},        # created edit
        {"row": 2, "col": "website", "inst": "MODIFY", "value": "https://x.ca"},
    ]
    at.session_state["edits"] = {(2, "website"): "https://second.ca"}
    at = at.run()

    undo_btn = [b for b in at.button if "Undo" in (b.label or "")][0]
    assert not undo_btn.disabled
    at = undo_btn.click().run()
    assert at.session_state["edits"] == {(2, "website"): "https://x.ca"}
    at = [b for b in at.button if "Undo" in (b.label or "")][0].click().run()
    assert at.session_state["edits"] == {}          # DELETE removed the edit

    redo_btn = [b for b in at.button if "Redo" in (b.label or "")][0]
    assert not redo_btn.disabled
    at = redo_btn.click().run()
    assert at.session_state["edits"] == {(2, "website"): "https://x.ca"}
    at = [b for b in at.button if "Redo" in (b.label or "")][0].click().run()
    assert at.session_state["edits"] == {(2, "website"): "https://second.ca"}

    lo = [b for b in at.button if (b.label or "") == "Log out"][0]
    at = lo.click().run()
    assert at.session_state["user"] is None


def test_required_blank_warns_but_does_not_block_publish():
    # Blanking a required cell (phone_1) is flagged amber but publish stays
    # enabled; a format error (website) is what disables it.
    at = fresh(view="review", edits={(3, "phone_1"): ""})
    body = " ".join(md.value for md in at.markdown)
    assert "required cell" in body and "blank" in body
    publish = [b for b in at.button if "Publish" in (b.label or "")][0]
    assert not publish.disabled

    at.session_state["edits"][(3, "website")] = "no-scheme.ca"
    at = at.run()
    publish = [b for b in at.button if "Publish" in (b.label or "")][0]
    assert publish.disabled
