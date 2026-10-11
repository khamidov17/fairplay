import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1280,"height":860})
    pg.goto("http://127.0.0.1:8000/teacher/login")
    pg.wait_for_timeout(800)
    print(pg.locator("body").inner_text()[:600])
    print([ (i.get_attribute("id"), i.get_attribute("name"), i.get_attribute("type")) for i in pg.locator("input").all()])
    pg.screenshot(path="shots/01-teacher-login.png")
    b.close()
