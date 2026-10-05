from blastwatch.db import make_engine


def test_postgresql_url_uses_psycopg_driver():
    engine = make_engine("postgresql://user:password@localhost/blastwatch")

    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()
