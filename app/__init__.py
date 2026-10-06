"""考古发掘层位记录系统 —— Flask 应用工厂。"""

import os

from flask import Flask

from . import db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    app.config.update(
        DATABASE_PATH=os.environ.get("DATABASE_PATH", os.path.join(app.root_path, "..", "archaeo.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-in-production"),
        SEED_DEMO=os.environ.get("SEED_DEMO", "0") == "1",
        APP_NAME="考古发掘层位记录系统",
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    from . import routes

    app.register_blueprint(routes.bp)

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """建表并在开启 SEED_DEMO 时写入演示数据。"""
        database = db.get_db()
        db.init_db(database)
        if app.config["SEED_DEMO"]:
            from . import seed

            seed.seed_if_empty(database)
        print("数据库初始化完成:", app.config["DATABASE_PATH"])

    return app
