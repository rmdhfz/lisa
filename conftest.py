"""This file sets up custom plugins.

https://docs.pytest.org/en/stable/writing_plugins.html

"""
from __future__ import annotations

import typing
from pathlib import Path

from schema import SchemaMissingKeyError  # type: ignore

import azure  # noqa
import lisa
import pytest
from target import Target

if typing.TYPE_CHECKING:
    from typing import Any, Dict, Iterator, List, Optional, Type

    from _pytest.config import Config
    from _pytest.config.argparsing import Parser
    from _pytest.fixtures import SubRequest
    from _pytest.python import Metafunc

    from pytest import Item, Session


@pytest.fixture(scope="session")
def pool(request: SubRequest) -> Iterator[List[Target]]:
    """This fixture tracks all deployed target resources."""
    targets: List[Target] = []
    yield targets
    for t in targets:
        print(f"Created target: {t.features} / {t.parameters}")
        if not request.config.getoption("keep_vms"):
            t.delete()


@pytest.fixture
def target(pool: List[Target], request: SubRequest) -> Iterator[Target]:
    """This fixture provides a connected target for each test.

    It is parametrized indirectly in 'pytest_generate_tests'.

    In this fixture we can check if any existing target matches all
    the requirements. If so, we can re-use that target, and if not, we
    can deallocate the currently running target and allocate a new
    one. When all tests are finished, the pool fixture above will
    delete all created VMs. Coupled with performing discrete
    optimization in the test collection phase and ordering the tests
    such that the test(s) with the lowest common denominator
    requirements are executed first, we have the two-layer scheduling
    as asked.

    However, this feels like putting the cart before the horse to me.
    It would be much simpler in terms of design, implementation, and
    usage that features are specified upfront when the targets are
    specified. Then all this goes away, and tests are skipped when the
    feature is missing, which also leaves users in full control of
    their environments.

    """
    import playbook

    platform: Type[Target] = playbook.PLATFORMS[request.param["platform"]]
    parameters: Dict[str, Any] = request.param["parameters"]
    marker = request.node.get_closest_marker("lisa")
    features = set(marker.kwargs["features"])

    for t in pool:
        # TODO: Implement full feature comparison, etc. and not just
        # proof-of-concept string set comparison.
        if all(
            [
                isinstance(t, platform),
                t.parameters == parameters,
                t.features >= features,
            ]
        ):
            yield t
            break
    else:
        # TODO: Reimplement caching.
        t = platform(parameters, features)
        pool.append(t)
        yield t
    t.connection.close()


def pytest_addoption(parser: Parser) -> None:
    """Pytest hook for adding arbitrary CLI options.

    https://docs.pytest.org/en/latest/example/simple.html
    https://docs.pytest.org/en/latest/reference.html#pytest.hookspec.pytest_addoption

    """
    parser.addoption("--keep-vms", action="store_true", help="Keeps deployed VMs.")
    parser.addoption("--check", action="store_true", help="Run semantic analysis.")
    parser.addoption("--demo", action="store_true", help="Run in demo mode.")
    parser.addoption("--playbook", type=Path, help="Path to playbook.")


TARGETS: List[Dict[str, Any]] = []
TARGET_IDS: List[str] = []


def get_playbook(path: Optional[Path]) -> Dict[str, Any]:
    """Loads and validates the playbook file.

    This imports the playbook module at runtime to ensure all
    subclasses of 'Target' (e.g. all supported platforms, including
    those defined in arbitrary 'conftest.py' files) are defined.

    """
    # TODO: Move to 'playbook.py' and setup 'PLATFORMS' when called so
    # that the import can take place at any time.
    import playbook

    book = dict()
    if path:
        # See https://pyyaml.org/wiki/PyYAMLDocumentation
        import yaml

        try:
            from yaml import CLoader as Loader
        except ImportError:
            from yaml import Loader  # type: ignore

        with open(path) as f:
            book = playbook.schema.validate(yaml.load(f, Loader=Loader))
    else:
        book = playbook.schema.validate({})
    return book


def pytest_configure(config: Config) -> None:
    """Parse provided user inputs to setup configuration.

    Determines the targets based on the playbook and sets default
    configurations based user mode.

    https://docs.pytest.org/en/latest/reference.html#pytest.hookspec.pytest_configure

    """
    book = get_playbook(config.getoption("--playbook"))
    for t in book.get("targets", []):
        TARGETS.append(t)
        TARGET_IDS.append(t["name"])

    # Search ‘_pytest’ for ‘addoption’ to find these.
    options: Dict[str, Any] = {}  # See ‘pytest.ini’ for defaults.
    if config.getoption("--check"):
        options.update(
            {
                "flake8": True,
                "mypy": True,
                "markexpr": "flake8 or mypy",
                "reportchars": "fE",
            }
        )
    if config.getoption("--demo"):
        options.update(
            {
                "html": "demo.html",
                "no_header": True,
                "showcapture": "log",
                "tb": "line",
            }
        )
    for attr, value in options.items():
        setattr(config.option, attr, value)


def pytest_generate_tests(metafunc: Metafunc) -> None:
    """Parametrize the tests based on our inputs.

    Note that this hook is run for each test, so we do the file I/O in
    'pytest_configure' and save the results.

    https://docs.pytest.org/en/latest/reference.html#pytest.hookspec.pytest_generate_tests

    """
    if "target" in metafunc.fixturenames:
        assert TARGETS, "No targets specified!"
        metafunc.parametrize("target", TARGETS, True, TARGET_IDS)


def pytest_collection_modifyitems(
    session: Session, config: Config, items: List[Item]
) -> None:
    """Pytest hook for modifying the selected items (tests).

    https://docs.pytest.org/en/latest/reference.html#pytest.hookspec.pytest_collection_modifyitems

    """
    # Validate all LISA marks.
    for item in items:
        try:
            lisa.validate(item.get_closest_marker("lisa"))
        except SchemaMissingKeyError as e:
            print(f"Test {item.name} failed LISA validation {e}!")
            items[:] = []
            return

    # Optionally select tests based on a playbook.
    included: List[Item] = []
    excluded: List[Item] = []

    # TODO: Remove logging.
    def select(item: Item, times: int, exclude: bool) -> None:
        """Includes or excludes the item as appropriate."""
        if exclude:
            print(f"    Excluding {item}")
            excluded.append(item)
        else:
            print(f"    Including {item} {times} times")
            for _ in range(times - included.count(item)):
                included.append(item)

    book = get_playbook(config.getoption("--playbook"))
    for c in book.get("criteria", []):
        print(f"Parsing criteria {c}")
        for item in items:
            marker = item.get_closest_marker("lisa")
            if not marker:
                # Not all tests will have the LISA marker, such as
                # static analysis tests.
                continue
            i = marker.kwargs
            if any(
                [
                    c["name"] and c["name"] in item.name,
                    c["area"] and c["area"].casefold() == i["area"].casefold(),
                    c["category"]
                    and c["category"].casefold() == i["category"].casefold(),
                    c["priority"] and c["priority"] == i["priority"],
                    c["tags"] and set(c["tags"]) <= set(i["tags"]),
                ]
            ):
                select(item, c["times"], c["exclude"])
    if not included:
        included = items
    items[:] = [i for i in included if i not in excluded]


def pytest_html_report_title(report):  # type: ignore
    report.title = "LISAv3 (Using Pytest) Results"
