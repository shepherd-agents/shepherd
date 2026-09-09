"""Error feeds and incomplete pagination cannot establish absence."""

import pytest
from shepherd_citation_checker._engine.arxiv_atom import arxiv_view
from shepherd_citation_checker._engine.response_health import payload_error, usable
from shepherd_citation_checker._engine.search_coverage import complete_groups, official_scope
from shepherd_citation_checker._engine.sources import candidates, search_extent


def feed(ids=(), total=0, start=0):
    body = '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">'
    body += f"<o:totalResults>{total}</o:totalResults><o:startIndex>{start}</o:startIndex><o:itemsPerPage>2</o:itemsPerPage>"
    for identifier in ids:
        body += (
            f"<entry><id>https://arxiv.org/abs/{identifier}</id><title>A scientific paper</title>"
            "<author><name>A. Researcher</name></author><published>2024-01-01</published></entry>"
        )
    return {
        "url": f"https://export.arxiv.org/api/query?search_query=ti:example&start={start}&max_results=2",
        "http_status": 200,
        "body": body + "</feed>",
    }


def test_actual_counted_empty_feed_is_complete_official_repository_evidence():
    record = feed()
    assert usable(record)
    assert official_scope(record)
    assert search_extent(record)["complete_page_set"]
    assert complete_groups([record])[0]["total_hits"] == 0


def test_arxiv_pagination_requires_continuous_positions_and_distinct_candidates():
    first = feed(["2401.00001", "2401.00002"], 3)
    last = feed(["2401.00003"], 3, 2)
    assert candidates(first)[0]["authors"] == ["A. Researcher"]
    assert not search_extent(last)["complete_page_set"]
    assert complete_groups([first, last])[0]["unique_candidates"] == 3
    with pytest.raises(ValueError, match="incomplete"):
        complete_groups([first])
    with pytest.raises(ValueError, match="Duplicate candidate"):
        complete_groups([first, feed(["2401.00002"], 3, 2)])


@pytest.mark.parametrize(
    "failure",
    ["html", "wrong_namespace", "missing_count", "error_entry", "bad_offset", "truncated", "negative_count", "doctype"],
)
def test_invalid_atom_does_not_supply_negative_coverage(failure):
    record = feed()
    if failure == "html":
        record["body"] = "<html><body>Try again later</body></html>"
    elif failure == "wrong_namespace":
        record["body"] = record["body"].replace("http://www.w3.org/2005/Atom", "http://example.org")
    elif failure == "missing_count":
        record["body"] = record["body"].replace("<o:totalResults>0</o:totalResults>", "")
    elif failure == "error_entry":
        record = feed(["2401.00001"], 1)
        record["body"] = record["body"].replace("A scientific paper", "Error")
    elif failure == "bad_offset":
        record["url"] = record["url"].replace("start=0", "start=1")
    elif failure == "truncated":
        record["truncated"] = True
    elif failure == "negative_count":
        record["body"] = record["body"].replace("<o:totalResults>0", "<o:totalResults>-1")
    else:
        record["body"] = '<!DOCTYPE feed [<!ENTITY example "expanded">]>' + record["body"]
    assert arxiv_view(record) is None
    assert payload_error(record)
    assert not usable(record)
    assert not official_scope(record)
    with pytest.raises(ValueError, match="Unusable"):
        complete_groups([record])


def test_non_api_arxiv_page_or_foreign_feed_is_not_counted_repository_search():
    for url in ["https://arxiv.org/abs/2401.00001", "https://example.org/api/query?search_query=example"]:
        record = {**feed(), "url": url}
        assert arxiv_view(record) is None
        assert not official_scope(record)
