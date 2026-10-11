from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1280,"height":860})
    pg.goto("http://127.0.0.1:8000/teacher/login")
    pg.fill("#username","teacher"); pg.fill("#password","teacher123")
    pg.get_by_role("button", name="Sign in").click(); pg.wait_for_url("**/teacher")
    pg.goto("http://127.0.0.1:8000/teacher/exams/new")
    for b_ in ["+ Multiple choice", "+ Numeric", "+ Short answer"]:
        pg.get_by_role("button", name=b_).click()
    pg.wait_for_timeout(500)
    for i in pg.locator("input,select,textarea").all():
        lab = i.get_attribute("aria-label")
        print(i.get_attribute("id"), "|", lab, "|", i.get_attribute("type"), "|", i.get_attribute("name"), "|", i.get_attribute("placeholder"))
    print([x.inner_text() for x in pg.get_by_role("button").all()])
    print([l.inner_text() for l in pg.locator("label").all()][:60])
    pg.screenshot(path="shots/03b-editor.png", full_page=True)
    b.close()
