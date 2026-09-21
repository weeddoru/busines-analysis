import csv
import json
import os
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
import scrapy


class KnuSpider(scrapy.Spider):
    name = 'klopotenko'
    start_urls = ['https://knu.ua/ua/departments']

    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'ROBOTSTXT_OBEY': False,
        'DOWNLOAD_TIMEOUT': 10,
        'LOG_LEVEL': 'INFO'
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        os.makedirs('images', exist_ok=True)

        self.categories_data = []
        self.items_data = []
        self.images_data = []
        self.img_count = 0

        self.cat_txt = open('categories.txt', 'w', encoding='utf-8')
        self.rec_txt = open('recipes.txt', 'w', encoding='utf-8')
        self.img_txt = open('images_list.txt', 'w', encoding='utf-8')

    def parse(self, response):
        # 2. Переконатись у статичності та вивести HTML у консоль
        print("\n" + "=" * 50)
        print("=== Завдання 2: HTML-код сторінки https://knu.ua/ua/departments ===")
        print(response.text[:300])
        print("=" * 50 + "\n")

        # 3. Отримати список підрозділів (факультетів/інститутів) та їх URL
        dept_links = response.css('a[href*="/ua/departments/"]')
        
        for a in dept_links:
            name = a.css('::text').get()
            url = a.css('::attr(href)').get()

            if name and url:
                name = name.strip()
                full_url = response.urljoin(url)

                # Фільтруємо головне посилання та дублікати
                if name and full_url != response.url and not any(c['url'] == full_url for c in self.categories_data):
                    cat_item = {'name': name, 'url': full_url}
                    self.categories_data.append(cat_item)
                    self.cat_txt.write(f"{name}: {full_url}\n")

        # Переходимо по перших 3 підрозділах для збору спеціальностей/елементів
        for cat in self.categories_data[:3]:
            yield scrapy.Request(
                url=cat['url'],
                callback=self.parse_category,
                meta={'category_name': cat['name']}
            )

    def parse_category(self, response):
        cat_name = response.meta['category_name']
        
        # 4. Отримати списки об'єктів (кафедри/спеціальності/напрями) зі сторінки підрозділу
        elements = response.css('ul li a, ol li a, td a, p a')
        found_count = 0

        for el in elements:
            if found_count >= 5:
                break

            title = el.css('::text').get()
            item_url = el.css('::attr(href)').get()

            if title:
                title = title.strip()

            if title and len(title) > 5 and item_url and item_url != response.url:
                full_item_url = response.urljoin(item_url)
                
                if not any(item['url'] == full_item_url for item in self.items_data):
                    item_data = {
                        'category': cat_name,
                        'title': title,
                        'url': full_item_url
                    }
                    self.items_data.append(item_data)
                    self.rec_txt.write(f"[{cat_name}] {title} - {full_item_url}\n")
                    found_count += 1

        # 5. Завантажити зображення зі сторінки підрозділу
        imgs = response.css('img::attr(src)').getall()
        for img_src in imgs[:3]:
            if img_src:
                full_img_url = response.urljoin(img_src)
                if not any(i['url'] == full_img_url for i in self.images_data):
                    self.img_count += 1
                    filename = f"image_{self.img_count}.jpg"
                    filepath = os.path.join('images', filename)

                    try:
                        req = urllib.request.Request(full_img_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=5) as res, open(filepath, 'wb') as f:
                            f.write(res.read())
                        self.img_txt.write(f"{filename}: {full_img_url}\n")
                        self.images_data.append({'filename': filename, 'url': full_img_url})
                    except Exception as e:
                        self.logger.error(f"Помилка завантаження фото: {e}")

    def closed(self, reason):
        self.cat_txt.close()
        self.rec_txt.close()
        self.img_txt.close()

        # 3. Зберегти список підрозділів у форматі XML
        root = ET.Element("categories")
        for cat in self.categories_data:
            cat_el = ET.SubElement(root, "category")
            ET.SubElement(cat_el, "name").text = cat['name']
            ET.SubElement(cat_el, "url").text = cat['url']
        tree = ET.ElementTree(root)
        tree.write("categories.xml", encoding="utf-8", xml_declaration=True)

        # 4. Зберегти результати скрапінгу елементів у JSON
        with open('recipes.json', 'w', encoding='utf-8') as f:
            json.dump(self.items_data, f, ensure_ascii=False, indent=4)

        # 5. Зберегти список зображень у CSV
        with open('images_list.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['filename', 'url'])
            writer.writeheader()
            writer.writerows(self.images_data)

        # 6. Зберегти всі зібрані дані до бази даних SQLite
        conn = sqlite3.connect('scraped_data.db')
        cursor = conn.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS categories 
                          (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS items 
                          (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, title TEXT, url TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS images 
                          (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, url TEXT)''')

        for cat in self.categories_data:
            cursor.execute("INSERT INTO categories (name, url) VALUES (?, ?)", (cat['name'], cat['url']))
        for item in self.items_data:
            cursor.execute("INSERT INTO items (category, title, url) VALUES (?, ?, ?)", 
                           (item['category'], item['title'], item['url']))
        for img in self.images_data:
            cursor.execute("INSERT INTO images (filename, url) VALUES (?, ?)", (img['filename'], img['url']))

        conn.commit()
        conn.close()
        print("\n Усі завдання (2-6) успішно виконані!")