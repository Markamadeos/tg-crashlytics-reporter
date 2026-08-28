import pytest

from crashdigest.versions import AppVersion, RecentVersions, build_rank, recent_versions


def v(display, build):
    return AppVersion(
        display_version=display,
        build_version=build,
        display_name=f"{display} ({build})",
    )


def test_build_rank_parses_numeric_builds():
    assert build_rank("2026072901") == 2026072901
    assert build_rank("10420") == 10420


def test_build_rank_of_non_numeric_is_lowest():
    assert build_rank("v1-beta") == -1
    assert build_rank("") == -1


def test_two_build_schemes_order_correctly():
    """Десятизначные YYYYMMDDNN всегда новее пятизначных последовательных."""
    versions = [v("4.7.0", "10430"), v("5.14.0", "2026072901"), v("3.11.0", "10375")]
    assert recent_versions(versions, 3).display_versions == ("5.14.0", "4.7.0", "3.11.0")


def test_version_ranked_by_its_newest_build():
    """У одного displayVersion несколько билдов — версию двигает самый свежий.

    Старый билд 5.15.0 намеренно СТАРШЕ билда 5.14.0: при наивном «последний
    встреченный побеждает» 5.15.0 получила бы ранг 2026062001 и уехала бы
    ниже 5.14.0. Просто поставить свежий билд первым недостаточно — если
    старый билд всё равно новее конкурента, перезапись не меняет порядок.
    """
    versions = [
        v("5.15.0", "2026082501"),
        v("5.15.0", "2026062001"),
        v("5.14.0", "2026072901"),
    ]
    assert recent_versions(versions, 2).display_versions == ("5.15.0", "5.14.0")


def test_all_builds_of_selected_versions_go_into_filter():
    versions = [
        v("5.15.0", "2026082001"),
        v("5.15.0", "2026082501"),
        v("5.14.0", "2026072901"),
        v("5.13.1", "2026063001"),
    ]
    result = recent_versions(versions, 2)
    assert set(result.display_names) == {
        "5.15.0 (2026082001)",
        "5.15.0 (2026082501)",
        "5.14.0 (2026072901)",
    }


def test_fewer_versions_than_requested():
    assert recent_versions([v("1.0.0", "5")], 3).display_versions == ("1.0.0",)


def test_empty_input():
    assert recent_versions([], 3) == RecentVersions(display_versions=(), display_names=())


def test_count_must_be_positive():
    with pytest.raises(ValueError):
        recent_versions([v("1.0.0", "5")], 0)


def test_unicode_digits_do_not_crash_build_rank():
    """`"²".isdigit()` истинно, а `int("²")` бросает — откат на -1 обязан работать."""
    assert build_rank("1²3") == -1
