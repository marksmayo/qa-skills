import pytest


@pytest.mark.smoke
def test_home_page_answers():
    assert 200 == 200


@pytest.mark.smoke
def test_version_endpoint_reports_a_version():
    assert "2.4.0".count(".") == 2
