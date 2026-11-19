"""List the sections exposed by the configured storefront."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "crawler"))
from scrape import Browser, slugify


def main() -> None:
    browser = Browser(sys.argv[1], float(sys.argv[2]), hide_window=True)
    try:
        nav = browser.data("/home/__data.json")[0]["initData"]["navList"]
        sections = sorted({
            f"{slugify(category['categoryName'])}/{slugify(sub['subCategoryName'])}"
            for department in nav
            for category in department["categories"]
            for sub in category["subCategories"]
        })
        print(json.dumps(sections))
    finally:
        browser.close()


if __name__ == "__main__":
    main()
