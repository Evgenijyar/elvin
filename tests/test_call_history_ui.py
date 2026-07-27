from pathlib import Path


def test_call_history_ui_contract() -> None:
    html = Path("src/elvin/web/index.html").read_text("utf-8")
    js = Path("src/elvin/web/static/app.js").read_text("utf-8")
    css = Path("src/elvin/web/static/app.css").read_text("utf-8")

    assert 'data-page="history"' in html
    assert 'id="historyPage"' in html
    assert 'id="callHistoryFilters"' in html
    assert 'id="callHistoryList"' in html
    assert 'id="callPhoneSearch"' in html
    assert 'id="callDateFrom"' in html
    assert 'id="callDateTo"' in html
    assert 'id="todayCallFilters"' in html
    assert 'id="loadMoreCalls"' in html
    assert 'loadCallHistory' in js
    assert 'state.expandedCallId === callId ? null : callId' in js
    assert '/api/calls?' in js
    assert 'append: true' in js
    assert 'offset' in js
    assert 'new Map' in js
    assert 'CALL_HISTORY_FILTERS_SESSION_KEY' in js
    assert 'sessionStorage.setItem' in js
    assert 'Intl.DateTimeFormat().resolvedOptions().timeZone' in js
    assert 'loadCallHistory({ silent: true })' not in js
    assert 'requestId !== state.callHistoryRequestId' in js
    assert '.call-card-details' in css
    assert '.call-history-footer' in css
    assert 'button:not(:disabled):active' in css
