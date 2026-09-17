from chess_tracker.web.demo_data import (DEMO_USERNAME, demo_dashboard_data,
                                          render_demo_dashboard_html)


def test_demo_dashboard_data_has_one_user_flagged_as_demo():
    data = demo_dashboard_data()
    assert data["lists"]["users"] == [DEMO_USERNAME]
    assert data["meta"]["demo"] is True


def test_demo_dashboard_data_every_fact_table_is_non_empty():
    data = demo_dashboard_data()
    fact_tables = [k for k in data if k.endswith("Facts")]
    assert fact_tables  # sanity: we're actually checking something
    for key in fact_tables:
        assert data[key]["data"], f"{key} is empty -- a dashboard panel would render blank"


def test_demo_dashboard_data_has_at_least_one_eco_meeting_the_min_games_threshold():
    # otherwise the "openings you play often" panel would be empty
    data = demo_dashboard_data()
    assert data["lists"]["ecos"]


def test_render_demo_dashboard_html_has_no_leftover_placeholders():
    html = render_demo_dashboard_html()
    assert "__DATA_JSON__" not in html
    assert "__GENERATED_AT__" not in html
    assert DEMO_USERNAME in html
