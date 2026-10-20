import argparse
import csv
import logging
import re
import sys
from playwright.sync_api import sync_playwright

BASE = "https://ek.ua/ua"
CATEGORY = "/list/122/"
FIELDS = ["model", "description", "city", "shop", "price"]
SPEC_LABELS = ["Экран", "Екран", "Память", "Пам'ять", "Камера", "CPU (GPU)", "Аккумулятор", "Акумулятор"]
log = logging.getLogger("ek")

CYR = "А-Яа-яІіЇїЄєҐґ'’ʼ"
CYR_PAREN = re.compile(rf"\(([{CYR}.,\- ]{{2,40}})\)")
KNOWN_CITIES = [
    "Київ", "Харків", "Одеса", "Дніпро", "Львів", "Запоріжжя", "Кривий Ріг", "Миколаїв", "Вінниця",
    "Полтава", "Чернігів", "Черкаси", "Житомир", "Суми", "Хмельницький", "Чернівці", "Рівне",
    "Івано-Франківськ", "Тернопіль", "Луцьк", "Ужгород", "Кропивницький", "Біла Церква", "Кам'янське",
    "Мукачево", "Бровари", "Ірпінь", "Буча", "Коломия", "Дрогобич", "Мелітополь", "Нікополь", "Умань",
    "Кременчук", "Маріуполь", "Бердянськ", "Слов'янськ", "Краматорськ", "Дніпропетровськ", "Стрий",
    "Калуш", "Бориспіль", "Вишневе", "Фастів", "Обухів", "Кам'янець-Подільський", "Ковель",
    "Київська область",
]
DELIVERY_FORMS = {
    "Києву": "Київ", "Київ": "Київ", "Харкову": "Харків", "Одесі": "Одеса", "Дніпру": "Дніпро",
    "Львову": "Львів", "Запоріжжю": "Запоріжжя", "Миколаєву": "Миколаїв", "Вінниці": "Вінниця",
    "Полтаві": "Полтава", "Чернігову": "Чернігів", "Черкасах": "Черкаси", "Житомиру": "Житомир",
    "Сумах": "Суми", "Хмельницькому": "Хмельницький", "Чернівцях": "Чернівці", "Рівному": "Рівне",
    "Івано-Франківську": "Івано-Франківськ", "Тернополі": "Тернопіль", "Луцьку": "Луцьк",
    "Ужгороду": "Ужгород", "Кропивницькому": "Кропивницький", "Білій Церкві": "Біла Церква",
    "Кривому Розі": "Кривий Ріг", "Броварах": "Бровари", "Ірпені": "Ірпінь", "Бучі": "Буча",
    "Мукачеві": "Мукачево", "Коломиї": "Коломия", "Дрогобичі": "Дрогобич", "Кременчуці": "Кременчук",
}


def _word_re(words):
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![{CYR}])({alt})(?![{CYR}])")


CITY_RE = _word_re(KNOWN_CITIES)
DELIV_RE = _word_re(DELIVERY_FORMS)

JS_TEXT = """() => { const txt = el => { const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); const o = []; while (w.nextNode()) { const p = w.currentNode.parentElement; if (p && !['SCRIPT', 'STYLE'].includes(p.tagName)) o.push(w.currentNode.nodeValue); } return o.join(' '); }; return txt(document.body); }"""

JS_LINKS = """() => [...document.querySelectorAll('a[href$=".htm"]')]
  .filter(a => /^(Мобільний|Мобильный) телефон/.test(a.title || ''))
  .map(a => a.getAttribute('href'))"""

JS_OFFERS = """() => { const txt = el => { const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); const o = []; while (w.nextNode()) { const p = w.currentNode.parentElement; if (p && !['SCRIPT', 'STYLE'].includes(p.tagName)) o.push(w.currentNode.nodeValue); } return o.join(' '); };
  return [...document.querySelectorAll("img[src*='logo_shop']")].map(img => {
  let n = img, row = null;
  for (let i = 0; i < 10; i++) {
    n = n.parentElement;
    if (!n || ['TABLE', 'TBODY', 'BODY', 'HTML'].includes(n.tagName)) break;
    if (n.querySelectorAll("img[src*='logo_shop']").length > 1) break;
    if (txt(n).length > 4000) break;
    if (txt(n).includes('грн')) { row = n; if (n.tagName === 'TR') break; }
  }
  if (!row) return null;
  const a = row.querySelector('a');
  return {
    shop: (img.alt || '').trim() || (a ? a.textContent.trim() : ''),
    text: txt(row).replace(/\\s+/g, ' '),
    attrs: [...row.querySelectorAll('[title]')].map(e => e.title).join(' '),
  };
}).filter(Boolean); }"""

MORE_BTN = re.compile(r"(ще|еще|показати|показать)\s*\d*\s*(пропозиц|предлож)", re.I)


def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def category_path(url_or_path):
    path = re.sub(r"^https?://[^/]+", "", url_or_path.strip())
    path = re.sub(r"^/(ua|en)(?=/)", "", path)
    return path if path.endswith("/") else path + "/"


def open_page(page, url, delay):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    for _ in range(40): 
        if not re.search(r"just a moment|attention required|перевірка|проверка", page.title(), re.I):
            break
        log.warning("Пройдіть перевірку у вікні браузера...")
        page.wait_for_timeout(3000)
    page.wait_for_timeout(int(delay * 1000))


def scroll(page, steps=10):
    for _ in range(steps):
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(300)


def expand_offers(page, limit):
    last = stale = 0
    for _ in range(60):
        n = page.locator("img[src*='logo_shop']").count()
        if limit and n >= limit:
            break
        stale = stale + 1 if n <= last else 0
        if stale >= 3:
            break
        last = n
        page.mouse.wheel(0, 30000)
        btn = page.get_by_text(MORE_BTN).first
        try:
            if btn.count() and btn.is_visible():
                btn.click(timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(1000)


def get_slugs(page, category, pages, delay):
    slugs = []
    for p in range(1, pages + 1):
        url = f"{BASE}{category}" + ("" if p == 1 else f"{p - 1}/")
        log.info("Категорія %s, сторінка %d: %s", category, p, url)
        open_page(page, url, delay)
        scroll(page)
        for href in page.evaluate(JS_LINKS):
            m = re.search(r"/([A-Za-z0-9][A-Za-z0-9\-]*)\.htm$", href)
            if m and m.group(1) not in slugs:
                slugs.append(m.group(1))
        log.info("  усього моделей: %d", len(slugs))
    return slugs


def parse_model(page):
    name = clean(page.locator("h1").first.inner_text()) if page.locator("h1").count() else ""
    text = clean(page.evaluate(JS_TEXT))
    label_re = "|".join(re.escape(x) for x in SPEC_LABELS)
    marks = [(m.start(), m.end(), m.group(1)) for m in re.finditer(rf"({label_re}):", text)]
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
    return name, "; ".join(specs)


PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:грн|₴)")


def parse_price(text):
    vals = [int(re.sub(r"\D", "", m.group(1))) for m in PRICE_RE.finditer(text or "")]
    return max(vals) if vals else None  # найбільше: ціна товару, а не "Доставка 70 грн"


def find_city(head, after_shop, extra, shop, fulltext, text):
    for chunk in (after_shop, head):
        found = CYR_PAREN.findall(chunk)
        if found:
            return clean(found[0] if chunk is after_shop else found[-1])
    for chunk in (after_shop, extra, head):
        m = CITY_RE.search(chunk or "")
        if m:
            return m.group(1)
    if shop:  
        m = re.search(re.escape(shop) + rf"\s*\(\s*([{CYR}.\- ]{{2,30}}?)\s*\)", fulltext)
        if m:
            return clean(m.group(1))
    m = re.search(r"Доставк[аи]\s*:?\s*(.{0,80})", text)  
    f = DELIV_RE.search(m.group(1)) if m else None
    return DELIVERY_FORMS[f.group(1)] if f else ""


def collect_offers(page):
    fulltext = clean(page.evaluate(JS_TEXT))
    offers, seen = [], set()
    for raw in page.evaluate(JS_OFFERS):
        shop, text = raw["shop"], raw["text"]
        parts = re.split(r"Поскаржит\w*|Пожаловат\w*", text, maxsplit=1)
        head, tail = (parts[0], parts[1]) if len(parts) == 2 else (text, "")
        after = head[head.rfind(shop) + len(shop):] if shop and shop in head else head
        city = find_city(head, after, raw["attrs"], shop, fulltext, text)
        price = parse_price(tail) if tail else None
        if price is None:
            price = parse_price(text)
        key = (shop, city, price)
        if shop and price is not None and key not in seen:
            seen.add(key)
            offers.append(key)
    return offers


def scrape(args):
    rows = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        page = browser.new_context(locale="uk-UA", viewport={"width": 1366, "height": 850}).new_page()
        try:
            if args.model:
                slugs = args.model
            else:
                slugs = []
                for cat in args.categories:
                    slugs += [s for s in get_slugs(page, cat, args.pages, args.delay) if s not in slugs]
            if args.max_models:
                slugs = slugs[:args.max_models]
            log.info("Усього моделей до обробки: %d", len(slugs))

            deep = args.max_offers == 0 or args.max_offers > 5
            for i, slug in enumerate(slugs, 1):
                try:
                    open_page(page, f"{BASE}/{slug}.htm", args.delay)
                    name, desc = parse_model(page)
                    scroll(page, 5)
                    model_offers = collect_offers(page)
                    if not model_offers:  
                        scroll(page, 8)
                        model_offers = collect_offers(page)
                    offers = model_offers
                    if deep:
                        try:
                            open_page(page, f"{BASE}/prices/{slug.lower()}/", args.delay)
                            expand_offers(page, args.max_offers)
                            offers = collect_offers(page) or model_offers
                        except Exception as e:
                            if "closed" in str(e).lower():
                                raise
                            offers = model_offers
                    if args.max_offers:
                        offers = offers[:args.max_offers]
                    log.info("[%d/%d] %s -> %s", i, len(slugs), name or slug,
                             f"{len(offers)} пропозицій" if offers else "пропозицій не знайдено")
                    rows += [dict(model=name or slug, description=desc, city=c, shop=s, price=p)
                             for s, c, p in offers]
                    save_csv(rows, args.out)
                except Exception as e:
                    log.error("[%d/%d] %s: %s", i, len(slugs), slug, e)
                    if "closed" in str(e).lower():
                        log.error("Вікно браузера закрито - зберігаю вже зібране.")
                        break
        except KeyboardInterrupt:
            log.warning("Перервано користувачем - зберігаю вже зібране (%d записів).", len(rows))
            save_csv(rows, args.out)
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return rows


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Смартфони з EK.ua -> CSV (Playwright)")
    ap.add_argument("--category-url", nargs="+", default=[CATEGORY], dest="categories",
                    help="категорії (за замовч. /list/122/). Напр.: https://ek.ua/ua/list/122/apple/")
    ap.add_argument("--model", nargs="+", help="конкретні моделі (slug або посилання) замість категорій")
    ap.add_argument("--offers", action="store_true", help="усі пропозиції = --max-offers 0")
    ap.add_argument("--max-offers", type=int, default=5,
                    help="пропозицій на модель (5 - зі сторінки моделі; >5 відкриває повний список; 0 = усі)")
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--max-models", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--out", default="ek_smartphones.csv")
    args = ap.parse_args()
    if args.offers:
        args.max_offers = 0
    args.categories = [category_path(c) for c in args.categories]
    if args.model:
        args.model = [(re.search(r"([A-Za-z0-9][A-Za-z0-9\-]*)\.htm", m).group(1)
                       if re.search(r"\.htm", m) else m).strip("/").upper() for m in args.model]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rows = scrape(args)
    if not rows:
        log.error("Дані не зібрано.")
        sys.exit(1)
    save_csv(rows, args.out)
    log.info("Збережено %d записів у %s", len(rows), args.out)


if __name__ == "__main__":
    main()