from playwright.sync_api import sync_playwright
import json
from datetime import datetime
URL = "https://kubrick.life/pathsofglory1/"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_selector("body")

    page_title = page.title()

    headings = page.locator("h1, h2, h3").all_text_contents()

    paragraphs = page.locator("p").all_text_contents()
    headings = [text.strip() for text in headings if text.strip()]
    paragraphs = [text.strip() for text in paragraphs if text.strip()]

    data = {
        "url": URL,
        "page_title": page_title,
        "headings": headings,
        "paragraphs": paragraphs,
        "collection_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    with open("kubrick_data.json", "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)

    browser.close()

print("Збір даних завершено.")
print("Дані збережено у файлі kubrick_data.json")

