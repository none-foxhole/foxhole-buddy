import logging
import os
from foxhole_buddy.utils.env import load_env_file, required_env
from foxhole_buddy.core.store import StockpileStore
from foxhole_buddy.core.bot import StockpileBot

def main() -> None:
    load_env_file()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    token = required_env("DISCORD_TOKEN")
    # SQLite database path; the legacy flat-JSON store (DATA_FILE) is imported on
    # first boot and then renamed so it is never migrated twice.
    db_file = os.getenv("DB_FILE", "data/foxhole.db")
    legacy_json = os.getenv("DATA_FILE", "data/stockpiles.json")

    bot = StockpileBot(store=StockpileStore(db_file, migrate_from=legacy_json))
    bot.run(token)

if __name__ == "__main__":
    main()
