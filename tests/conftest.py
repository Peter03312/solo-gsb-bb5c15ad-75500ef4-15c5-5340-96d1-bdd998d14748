import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.db import get_db, init_db


@pytest.fixture()
def app():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    app = create_app({"TESTING": True, "DATABASE_PATH": db_path, "SEED_DEMO": False})
    with app.app_context():
        init_db(get_db())
    yield app
    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture()
def conn(app):
    with app.app_context():
        yield get_db()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def grid_with_layers(conn):
    """T01 下 L1(seq1) / L2(seq2) / L3(seq3)。"""
    from app import services

    gid = services.create_grid(conn, "T01", "测试遗址", "", "tester").id
    l1 = services.create_layer(conn, gid, "L1", 1, "表土", "", "", "tester").id
    l2 = services.create_layer(conn, gid, "L2", 2, "文化层", "", "", "tester").id
    l3 = services.create_layer(conn, gid, "L3", 3, "生土", "", "", "tester").id
    return {"grid": gid, "l1": l1, "l2": l2, "l3": l3}
