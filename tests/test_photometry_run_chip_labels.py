"""``run_chip_labels``: one distinct, readable label per photometry run."""

from __future__ import annotations

from muscat_db.photometry import RunDescriptor, run_chip_labels


def test_a_unique_run_name_is_kept_as_is():
    runs = [RunDescriptor(run_id="tel06-default", run_name="default", telescope="1m0-06"),
            RunDescriptor(run_id="r2", run_name="aperture test")]
    assert run_chip_labels(runs) == {"tel06-default": "default", "r2": "aperture test"}


def test_a_shared_run_name_is_qualified_by_the_runs_scope():
    runs = [RunDescriptor(run_id="tel06-default", run_name="default", telescope="1m0-06"),
            RunDescriptor(run_id="tel08-default", run_name="default", telescope="1m0-08"),
            RunDescriptor(run_id="lsc-central_2k_2x2-default", run_name="default",
                          site="lsc", mode="central_2k_2x2")]
    assert run_chip_labels(runs) == {
        "tel06-default": "default · 1m0-06",
        "tel08-default": "default · 1m0-08",
        "lsc-central_2k_2x2-default": "default · lsc central_2k_2x2",
    }


def test_runs_with_the_same_name_and_scope_fall_back_to_the_run_id():
    runs = [RunDescriptor(run_id="a", run_name="default"),
            RunDescriptor(run_id="b", run_name="default")]
    assert run_chip_labels(runs) == {"a": "a", "b": "b"}


def test_the_legacy_run_is_labelled_legacy():
    runs = [RunDescriptor(run_id="", run_name="legacy", is_legacy=True),
            RunDescriptor(run_id="r1", run_name="")]
    assert run_chip_labels(runs) == {"": "legacy", "r1": "r1"}


def test_transit_fit_runs_are_labelled_the_same_way():
    from muscat_db.transit_fit import RunDescriptor as FitRun

    runs = [FitRun(run_id="tel06-default", site="", telescope="1m0-06", mode="full",
                   run_name="default", mtime=0.0, is_legacy=False),
            FitRun(run_id="tel08-default", site="", telescope="1m0-08", mode="full",
                   run_name="default", mtime=0.0, is_legacy=False)]
    assert run_chip_labels(runs) == {
        "tel06-default": "default · 1m0-06 full",
        "tel08-default": "default · 1m0-08 full",
    }
