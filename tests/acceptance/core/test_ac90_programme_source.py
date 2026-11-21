import pytest

from driver import Driver


@pytest.mark.ac("AC-90")
def test_status_reports_programme_source_for_programme_delivery_approach(tmp_path):
    with Driver(tmp_path) as driver:
        driver.core_tree()
        driver.pid(omit=("delivery_approach",))
        driver.change_json(
            driver.tracker / "initiatives" / "alpha" / "initiative.json",
            config={"delivery_approach": "agile"},
        )

        status = driver.status()
        project = next(
            (entry for entry in (status or {}).get("projects", [])
             if entry.get("id") == "alpha-p1"),
            None,
        )
        assert project is not None
        assert project["delivery"]["approach"] == "agile"
        assert project["delivery"]["source"] == "programme"
