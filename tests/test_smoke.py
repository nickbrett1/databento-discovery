"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import databento_discovery

    assert databento_discovery.__version__
