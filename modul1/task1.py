#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматизований збір пропозицій продажу смартфонів з EK.ua (E-Katalog).

Категорія "Мобильные телефоны": https://ek.ua/list/122/
Сторінки категорії: /list/122/ (1), /list/122/1/ (2), ... /list/122/4/ (5)

Результат у 1НФ: один рядок = одна пропозиція магазину.
Поля CSV: model, description, city, shop, price
  price - число (int або float) без позначення валюти.

Запуск:
    pip install requests beautifulsoup4 playwright
    python -m playwright install chromium     # лише для режиму --browser
    python ek_smartphones_scraper.py                 # 5 сторінок, ek_smartphones.csv
    python ek_smartphones_scraper.py                       # 5 сторінок категорії; до 5 пропозицій на модель
    python ek_smartphones_scraper.py --max-offers 1        # лише перша пропозиція кожної моделі
    python ek_smartphones_scraper.py --listing             # лише категорія (діапазон цін, без міста/магазину)
    python ek_smartphones_scraper.py --offers              # усі пропозиції кожної моделі
    python ek_smartphones_scraper.py --max-models 3        # швидкий тест
    python ek_smartphones_scraper.py --category-url https://ek.ua/ua/list/122/apple/ https://ek.ua/ua/list/122/samsung/
    python ek_smartphones_scraper.py --model APPLE-IPHONE-18-PRO-MAX-256GB --max-offers 0   # одна модель, усі пропозиції
    python ek_smartphones_scraper.py --debug-dir debug   # зберігати HTML для діагностики
"""
import argparse
import csv
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://ek.ua/ua"   # українська версія сайту
CATEGORY = "/list/122/"
FIELDS = ["model", "description", "city", "shop", "price"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
}

log = logging.getLogger("ek")

# ---------------------------------------------------------------- HTTP ----


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class BrowserFetcher:
    """Завантаження сторінок справжнім Chromium (Playwright).
    Вікно браузера видиме: якщо сайт покаже перевірку ("Я не робот" тощо),
    пройдіть її вручну - скрипт зачекає до 2 хвилин."""

    CHALLENGE_MARKERS = ("just a moment", "attention required", "перевірка", "проверка")

    def __init__(self, headless=False):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._ctx = self._browser.new_context(locale="uk-UA", viewport={"width": 1366, "height": 850})
        self._page = self._ctx.new_page()

    OFFER_IMG = "img[src*='logo_shop']"
    MORE_BTN = re.compile(r"(ще|еще|показати|показать)\s*(\d+)?\s*(пропозиц|предлож)", re.I)

    def _count_offers(self):
        return self._page.locator(self.OFFER_IMG).count()

    def _expand_offers(self, expected):
        """Натискає кнопку 'ще N пропозицій' / прокручує, поки не завантажаться всі."""
        last, stale = self._count_offers(), 0
        for _ in range(120):
            if expected and last >= expected:
                break
            self._page.mouse.wheel(0, 30000)
            btn = self._page.get_by_text(self.MORE_BTN).first
            try:
                if btn.count() and btn.is_visible():
                    btn.scroll_into_view_if_needed(timeout=3000)
                    btn.click(timeout=5000)
            except Exception:
                pass
            self._page.wait_for_timeout(1200)
            now = self._count_offers()
            stale = stale + 1 if now <= last else 0
            last = now
            if stale >= 3:
                break
        return last

    def fetch(self, url, expand=False, expected=None, scroll=False):
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + 120
        while time.time() < deadline:
            title = (self._page.title() or "").lower()
            if not any(m in title for m in self.CHALLENGE_MARKERS):
                break
            log.warning("Сторінка просить перевірку - пройдіть її у вікні браузера...")
            time.sleep(3)
        self._page.wait_for_timeout(1500)
        status = resp.status if resp else 0
        if status >= 400:
            raise requests.HTTPError(f"{status} для {url}")
        if expand:
            self._expand_offers(expected)
        if scroll:
            for _ in range(15):
                self._page.mouse.wheel(0, 1600)
                self._page.wait_for_timeout(350)
            self._page.wait_for_timeout(1200)
        return self._page.content()

    def close(self):
        self._browser.close()
        self._pw.stop()


def get_html(session, url, delay, debug_dir=None, tag=None, expand=False, expected=None, scroll=False):
    time.sleep(delay)
    if isinstance(session, BrowserFetcher):
        text = session.fetch(url, expand=expand, expected=expected, scroll=scroll)
    else:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
        text = r.text
    if debug_dir and tag:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / f"{tag}.html").write_text(text, encoding="utf-8")
    return text


# ------------------------------------------------------------- парсинг ----


def clean(text: str) -> str:
    """Прибирає nbsp та зайві пробіли."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_price(text: str):
    """'42 299 грн.' -> 42299 ; '1 234,50 грн' -> 1234.5 ; нема числа -> None."""
    m = re.search(r"(\d[\d\s\u00a0\u202f.,]*)\s*(?:грн|₴)", text or "")
    if not m:
        return None
    raw = re.sub(r"[\s\u00a0\u202f]", "", m.group(1)).rstrip(".,")
    if not raw:
        return None
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def category_path(url_or_path: str) -> str:
    """'https://ek.ua/ua/list/122/apple/' -> '/list/122/apple/'"""
    path = re.sub(r"^https?://[^/]+", "", url_or_path.strip())
    path = re.sub(r"^/(ua|en)(?=/)", "", path)
    return path if path.endswith("/") else path + "/"


def category_page_url(page: int, path: str = CATEGORY) -> str:
    """page=1 -> /list/122/, page=2 -> /list/122/1/ ..."""
    return BASE + (path if page == 1 else f"{path}{page - 1}/")


MODEL_HREF = re.compile(r"^(?:https://ek\.ua)?/(?:ua/)?([A-Z0-9][A-Z0-9\-]*)\.htm$")


def parse_model_links(html: str):
    """Повертає впорядкований список slug-ів моделей зі сторінки категорії."""
    soup = BeautifulSoup(html, "html.parser")
    slugs, seen = [], set()

    def collect(anchors):
        for a in anchors:
            m = MODEL_HREF.match(a.get("href", ""))
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                slugs.append(m.group(1))

    # Основні плитки моделей мають title "Мобильный телефон ..."
    collect(
        a
        for a in soup.find_all("a", href=True)
        if (a.get("title") or "").startswith(("Мобильный телефон", "Мобільний телефон"))
    )
    if not slugs:  # запасний варіант
        collect(soup.find_all("a", href=True))
    return slugs


SPEC_LABELS = ["Экран", "Екран", "Память", "Пам'ять", "Камера", "CPU (GPU)",
               "Аккумулятор", "Акумулятор"]


def parse_model_page(html: str):
    """Назва моделі + короткий опис (характеристики) зі сторінки моделі."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    name = clean(h1.get_text(" ")) if h1 else ""

    text = clean(soup.get_text(" "))
    label_re = "|".join(re.escape(x) for x in SPEC_LABELS)
    marks = [(m.start(), m.end(), m.group(1))
             for m in re.finditer(rf"({label_re}):", text)]
    specs, used = [], set()
    for i, (_, end, label) in enumerate(marks):
        if label in used:
            continue
        used.add(label)
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        value = text[end:nxt].strip(" ,;")
        if label in ("Аккумулятор", "Акумулятор"):
            m = re.match(r"(\d[\d\s]*?\s*м[АA]\s*[·.]?\s*(?:год|ч))", value)
            value = re.sub(r"м[АA]\s*[·.]?\s*год", "мА·год", clean(m.group(1))) if m else value[:12]
        specs.append(f"{label}: {value[:200]}")
        if len(specs) >= 5:
            break
    return name, "; ".join(specs)


CYR_PAREN = re.compile(r"\(([А-Яа-яІіЇїЄєҐґ'’ʼ.,\- ]{2,40})\)")


KNOWN_CITIES = [
    "Київ", "Харків", "Одеса", "Дніпро", "Львів", "Запоріжжя", "Кривий Ріг", "Миколаїв", "Вінниця",
    "Полтава", "Чернігів", "Черкаси", "Житомир", "Суми", "Хмельницький", "Чернівці", "Рівне",
    "Івано-Франківськ", "Тернопіль", "Луцьк", "Ужгород", "Кропивницький", "Біла Церква", "Кам'янське",
    "Мукачево", "Бровари", "Ірпінь", "Буча", "Коломия", "Дрогобич", "Мелітополь", "Нікополь", "Умань",
    "Кременчук", "Маріуполь", "Бердянськ", "Слов'янськ", "Краматорськ", "Дніпропетровськ", "Стрий",
    "Калуш", "Бориспіль", "Вишневе", "Фастів", "Обухів", "Кам'янець-Подільський", "Ковель",
    "Київська область",
    # російські написання
    "Киев", "Харьков", "Одесса", "Днепр", "Днепропетровск", "Львов", "Запорожье", "Кривой Рог",
    "Николаев", "Винница", "Чернигов", "Черкассы", "Сумы", "Хмельницкий", "Черновцы", "Ровно",
    "Ивано-Франковск", "Тернополь", "Луцк", "Кировоград", "Белая Церковь",
]
_CITY_RE = re.compile(
    r"(?<![А-Яа-яІіЇїЄєҐґ'’])(" +
    "|".join(re.escape(c) for c in sorted(KNOWN_CITIES, key=len, reverse=True)) +
    r")(?![А-Яа-яІіЇїЄєҐґ'’])")
_debug_dumped = False


# "Доставка: по Києву" -> Київ (форми місцевого/давального відмінка -> називний)
DELIVERY_FORMS = {
    "Києву": "Київ", "Київ": "Київ", "Харкову": "Харків", "Одесі": "Одеса", "Дніпру": "Дніпро",
    "Львову": "Львів", "Запоріжжю": "Запоріжжя", "Миколаєву": "Миколаїв", "Вінниці": "Вінниця",
    "Полтаві": "Полтава", "Чернігову": "Чернігів", "Черкасах": "Черкаси", "Житомиру": "Житомир",
    "Сумах": "Суми", "Хмельницькому": "Хмельницький", "Чернівцях": "Чернівці", "Рівному": "Рівне",
    "Івано-Франківську": "Івано-Франківськ", "Тернополі": "Тернопіль", "Луцьку": "Луцьк",
    "Ужгороду": "Ужгород", "Кропивницькому": "Кропивницький", "Білій Церкві": "Біла Церква",
    "Кривому Розі": "Кривий Ріг", "Броварах": "Бровари", "Ірпені": "Ірпінь", "Бучі": "Буча",
    "Мукачеві": "Мукачево", "Коломиї": "Коломия", "Дрогобичі": "Дрогобич", "Кременчуці": "Кременчук",
    "Києву": "Київ", "Киеву": "Киев", "Харькове": "Харьков", "Одессе": "Одесса", "Днепре": "Днепр",
    "Львове": "Львов",
}
_DELIV_FORM_RE = re.compile(
    r"(?<![А-Яа-яІіЇїЄєҐґ'’])(" +
    "|".join(re.escape(f) for f in sorted(DELIVERY_FORMS, key=len, reverse=True)) +
    r")(?![А-Яа-яІіЇїЄєҐґ'’])")


def city_from_delivery(text):
    m = re.search(r"(?:Доставка|Доставк[аи])\s*:?\s*(.{0,80})", text or "")
    if not m:
        return ""
    f = _DELIV_FORM_RE.search(m.group(1))
    return DELIVERY_FORMS[f.group(1)] if f else ""


def find_city(head, after_shop, extra=""):
    """1) '(Місто)' у дужках; 2) відоме місто за списком; '' якщо нічого."""
    for chunk in (after_shop, head):
        found = CYR_PAREN.findall(chunk)
        if found:
            return clean(found[0] if chunk is after_shop else found[-1])
    for chunk in (after_shop, extra, head):
        m = _CITY_RE.search(chunk or "")
        if m:
            return m.group(1)
    return ""


def parse_offers(html: str):
    """
    Повертає список (shop, city, price) зі сторінки з пропозиціями.
    Не залежить від CSS-класів: кожна пропозиція містить логотип магазину
    (img з 'logo_shop' у src); піднімаємося до найближчого контейнера з ціною.
    """
    soup = BeautifulSoup(html, "html.parser")
    offers, seen_containers = [], set()
    fulltext = clean(soup.get_text(" "))   # для пошуку "Магазин (Місто)" у короткому списку зверху

    for img in soup.find_all("img", src=re.compile(r"logo_shop")):
        shop = clean(img.get("alt", ""))
        node, container = img, None
        for _ in range(10):  # піднімаємося до найбільшого блоку з одним логотипом (весь рядок)
            node = node.parent
            if node is None or node.name in ("table", "tbody", "body", "html"):
                break
            if len(node.find_all("img", src=re.compile(r"logo_shop"))) > 1:
                break
            txt = node.get_text(" ")
            if len(txt) > 4000:
                break
            if "грн" in txt:
                container = node
                if node.name == "tr":
                    break
        if container is None or id(container) in seen_containers:
            continue
        seen_containers.add(id(container))

        text = clean(container.get_text(" "))
        parts = re.split(r"Поскаржит\w*|Пожаловат\w*", text, maxsplit=1)
        head, tail = (parts[0], parts[1]) if len(parts) == 2 else (text, "")
        after_shop = head[head.rfind(shop) + len(shop):] if shop and shop in head else head
        attrs = " ".join(clean(t.get("title", "")) for t in container.find_all(True) if t.get("title"))
        city = find_city(head, after_shop, attrs)
        if not city and shop:   # короткий список: "АЛЛО (Київ) →"
            m = re.search(re.escape(shop) + r"\s*\(\s*([А-Яа-яІіЇїЄєҐґ'’.\- ]{2,30}?)\s*\)", fulltext)
            city = clean(m.group(1)) if m else ""
        if not city:           # "Доставка: по Києву"
            city = city_from_delivery(text)
        global _debug_dumped
        if not city and not _debug_dumped:
            _debug_dumped = True
            Path("debug_offer_row.html").write_text(str(container), encoding="utf-8")
            log.warning("Місто не знайдено. Розмітку рядка збережено у debug_offer_row.html")
        price = parse_price(tail) if tail else None
        if price is None:
            price = parse_price(text)

        if not shop:  # запасний варіант: назва з посилання
            a = container.find("a")
            shop = clean(a.get_text(" ")) if a else ""
        if shop and price is not None:
            offers.append((shop, city, price))
    return offers


def offers_count_hint(html: str):
    m = re.search(r"(?:Где купить|Де купити|Все цены|Усі ціни)\s*(\d+)", clean(BeautifulSoup(html, "html.parser").get_text(" ")))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- логіка ---


def is_closed(exc) -> bool:
    return "has been closed" in str(exc) or type(exc).__name__ == "TargetClosedError"


def scrape(pages, delay, max_models, debug_dir, browser=False, models=None,
           categories=None, max_offers=20, out=None, first_only=False):
    session = BrowserFetcher() if browser else make_session()
    rows = []

    slugs = []
    for ci, cat in enumerate(categories or [CATEGORY] if not models else []):
        for p in range(1, pages + 1):
            url = category_page_url(p, cat)
            log.info("Категорія %s, сторінка %d: %s", cat, p, url)
            try:
                html = get_html(session, url, delay, debug_dir, f"category_{ci}_{p}")
            except Exception as e:
                log.error("Не вдалося завантажити %s: %s", url, e)
                if is_closed(e):
                    return rows
                continue
            found = parse_model_links(html)
            log.info("  знайдено моделей: %d", len(found))
            for s in found:
                if s not in slugs:
                    slugs.append(s)

    if models:
        slugs = list(models)
    if max_models:
        slugs = slugs[:max_models]
    log.info("Усього моделей до обробки: %d", len(slugs))

    for i, slug in enumerate(slugs, 1):
        model_url = f"{BASE}/{slug}.htm"
        prices_url = f"{BASE}/prices/{slug.lower()}/"
        try:
            model_html = get_html(session, model_url, delay, debug_dir, f"model_{slug}")
            name, description = parse_model_page(model_html)
            expected = offers_count_hint(model_html)

            if first_only:
                # лише перша пропозиція зі сторінки моделі - без списку пропозицій
                offers = parse_offers(model_html)[:max_offers]
                if not offers and browser:  # пропозиції могли підвантажитися із затримкою
                    model_html = get_html(session, model_url, delay, debug_dir,
                                          f"model_{slug}", scroll=True)
                    offers = parse_offers(model_html)[:max_offers]
            else:
                deep = max_offers == 0 or max_offers > 20
                try:
                    prices_html = get_html(session, prices_url, delay, debug_dir, f"prices_{slug}",
                                           expand=deep, expected=(max_offers or expected) if deep else None)
                    offers = parse_offers(prices_html)
                except requests.RequestException:
                    offers = []
                if not offers:  # запасний варіант: пропозиції зі сторінки моделі
                    offers = parse_offers(model_html)
        except Exception as e:
            log.error("[%d/%d] %s: %s", i, len(slugs), slug, e)
            if is_closed(e):
                log.error("Вікно браузера закрито - зберігаю вже зібране.")
                break
            continue

        if first_only:
            pass
        elif max_offers:
            offers = offers[:max_offers]
        elif expected and len(offers) < expected:
            log.warning("  %s: зібрано %d із %d пропозицій", slug, len(offers), expected)
        log.info("[%d/%d] %s -> %s", i, len(slugs), name or slug,
                 (f"{len(offers)} пропозицій" if offers else "пропозицій не знайдено"))
        if first_only and not offers:
            log.warning("  %s: немає пропозицій на сторінці моделі", slug)

        for shop, city, price in offers:
            rows.append({
                "model": name or slug,
                "description": description,
                "city": city,
                "shop": shop,
                "price": price,
            })
        if out:
            save_csv(rows, out)   # проміжне збереження після кожної моделі
    if browser:
        try:
            session.close()
        except Exception:
            pass
    return rows


# ------------------------------------------- режим "лише сторінки категорії" ---

LISTING_FIELDS = ["model", "description", "price_min", "price_max", "offers"]
TILE_LABELS = ["Экран", "Екран", "Камера", "Видео", "Відео", "Память", "Пам'ять", "CPU (GPU)",
               "ОЗУ", "ОЗП", "Аккумулятор", "Акумулятор", "Корпус"]
TILE_CUT = re.compile(r"\s*(?:Отзывы|Відгуки|Видео \d|Відео \d|Фото \d|Обсуждение|Обговорення|Обзоры|Огляди|"
                      r"Где купить|Де купити|Чехлы|Чохли|Стекла|Скла|Цвет|Колір|Цена|Ціна)\b.*$")
TITLE_PREFIX = ("Мобильный телефон", "Мобільний телефон")


def _is_model_anchor(a):
    return (a.get("title") or "").startswith(TITLE_PREFIX) and MODEL_HREF.match(a.get("href", ""))


def _num(text):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def parse_listing(html: str):
    """Плитки зі сторінки категорії -> [{model, description, price_min, price_max, offers}]"""
    soup = BeautifulSoup(html, "html.parser")
    label_re = "|".join(re.escape(x) for x in TILE_LABELS)
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        if not _is_model_anchor(a):
            continue
        slug = MODEL_HREF.match(a["href"]).group(1)
        if slug in seen:
            continue
        seen.add(slug)

        tile, node = a, a
        for _ in range(8):  # піднімаємося, доки в контейнері лише одна модель
            node = node.parent
            if node is None:
                break
            slugs = {MODEL_HREF.match(x["href"]).group(1)
                     for x in node.find_all("a", href=True) if _is_model_anchor(x)}
            if len(slugs) > 1:
                break
            tile = node
        text = clean(tile.get_text(" "))

        name = clean(a.get("title"))
        for pref in TITLE_PREFIX:
            name = name.replace(pref, "", 1).strip()

        marks = [(m.start(), m.end(), m.group(1)) for m in re.finditer(rf"({label_re}):", text)]
        specs, used = [], set()
        for i, (_, end, label) in enumerate(marks):
            if label in used:
                continue
            used.add(label)
            nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            value = TILE_CUT.sub("", text[end:nxt]).strip(" ,;")
            if value:
                specs.append(f"{label}: {value[:200]}")

        pm = re.search(r"(?:Цена|Ціна)\s*(?:от|від)\s*([\d\s]+?)\s*(?:до\s*([\d\s]+?)\s*)?грн", text)
        price_min = _num(pm.group(1)) if pm else None
        price_max = (_num(pm.group(2)) if pm and pm.group(2) else price_min)
        om = re.search(r"(\d+)\s*(?:предложени|пропозиц)", text) or re.search(r"(?:Где купить|Де купити)\s*(\d+)", text)
        items.append({
            "model": name or slug,
            "description": "; ".join(specs),
            "price_min": price_min,
            "price_max": price_max,
            "offers": int(om.group(1)) if om else None,
        })
    return items


def scrape_listing(pages, delay, debug_dir, browser, categories, max_models, out):
    session = BrowserFetcher() if browser else make_session()
    rows, names = [], set()
    try:
        for ci, cat in enumerate(categories):
            for p in range(1, pages + 1):
                url = category_page_url(p, cat)
                log.info("Категорія %s, сторінка %d: %s", cat, p, url)
                try:
                    html = get_html(session, url, delay, debug_dir, f"category_{ci}_{p}", scroll=True)
                except Exception as e:
                    log.error("Не вдалося завантажити %s: %s", url, e)
                    if is_closed(e):
                        return rows
                    continue
                items = parse_listing(html)
                log.info("  знайдено моделей: %d", len(items))
                for it in items:
                    if it["model"] not in names:
                        names.add(it["model"])
                        rows.append(it)
                save_csv(rows, out, LISTING_FIELDS)
                if max_models and len(rows) >= max_models:
                    return rows[:max_models]
    finally:
        if browser:
            try:
                session.close()
            except Exception:
                pass
    return rows


def save_csv(rows, path, fields=None):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields or FIELDS, delimiter=",", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Збір пропозицій смартфонів з EK.ua у CSV")
    ap.add_argument("--category-url", nargs="+", default=[CATEGORY], dest="categories",
                    help="категорії/списки для обходу (за замовч. /list/122/ - усі смартфони). "
                         "Напр.: https://ek.ua/ua/list/122/apple/ https://ek.ua/ua/list/122/samsung/")
    ap.add_argument("--model", nargs="+", default=None,
                    help="замість категорій - конкретні моделі (slug або посилання)")
    ap.add_argument("--listing", action="store_true",
                    help="лише сторінки категорії (без міста й магазину): діапазон цін від-до")
    ap.add_argument("--offers", action="store_true",
                    help="усі пропозиції кожної моделі (повільно) = --max-offers 0")
    ap.add_argument("--max-offers", type=int, default=5,
                    help="пропозицій на модель (за замовч. 5 - ті, що видно на сторінці моделі; "
                         ">5 відкриває повний список; 0 = усі)")
    ap.add_argument("--pages", type=int, default=5, help="кількість сторінок категорії (за замовч. 5)")
    ap.add_argument("--delay", type=float, default=1.5, help="пауза між запитами, с")
    ap.add_argument("--out", default="ek_smartphones.csv", help="вихідний CSV")
    ap.add_argument("--max-models", type=int, default=0, help="обмежити кількість моделей (для тесту)")
    ap.add_argument("--no-browser", dest="browser", action="store_false",
                    help="вимкнути браузерний режим (за замовчуванням сторінки завантажує Chromium/Playwright)")
    ap.set_defaults(browser=True)
    ap.add_argument("--debug-dir", default=None, help="папка для збереження завантаженого HTML")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    models = None
    if args.model:
        models = []
        for m in args.model:
            mm = re.search(r"([A-Za-z0-9][A-Za-z0-9\-]*)\.htm", m)
            models.append((mm.group(1) if mm else m).strip("/").upper())
    categories = [category_path(c) for c in args.categories]
    if args.listing:
        rows = scrape_listing(args.pages, args.delay, args.debug_dir, args.browser,
                              categories, args.max_models, args.out)
        if not rows:
            log.error("Дані не зібрано. Запустіть з --debug-dir debug і перевірте HTML.")
            sys.exit(1)
        save_csv(rows, args.out, LISTING_FIELDS)
        log.info("Збережено %d моделей у %s", len(rows), args.out)
        return
    max_offers = 0 if args.offers else args.max_offers
    rows = scrape(args.pages, args.delay, args.max_models, args.debug_dir, args.browser, models,
                  categories, max_offers, args.out,
                  first_only=0 < max_offers <= 5)
    if not rows:
        log.error("Дані не зібрано. Запустіть з --debug-dir debug і перевірте HTML.")
        sys.exit(1)
    save_csv(rows, args.out)
    log.info("Збережено %d записів у %s", len(rows), args.out)


if __name__ == "__main__":
    main()