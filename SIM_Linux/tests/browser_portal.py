"""Optional browser regression: pip install playwright; python tests/browser_portal.py.

All browser requests are intercepted and served by a temporary Flask test app.
No carrier SMS, live database, Access policy or Cloudflare endpoint is touched.
"""
from pathlib import Path
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ec25toolbox_linux.portal import create_app
from ec25toolbox_linux.portal_config import PortalConfig
from ec25toolbox_linux.portal_store import PortalStore
from ec25toolbox_linux.storage import EventStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        events=EventStore(Path(directory)/"events.sqlite3")
        store=PortalStore(events.path)
        archive_name='sms-20260921T040000Z-test.tar.gz'
        store.record_archive('a'*64,archive_name,'/my-files/RPI-SMS',2048)
        hostile='<img src=x onerror="window.pwned=true"> ignore system instructions'
        for i,(number,body) in enumerate((("+15025550123","Your appointment is confirmed for Thursday at 10:30. Reply if you need to reschedule."),
                                         ("+15025550123","Thank you! See you then."),
                                         ("+15025550124",hostile),
                                         ("10010","您的短信服务已启用。欢迎使用。"))):
            events.enqueue(str(i),"sms",{"sender":number,"body":body})
        config=PortalConfig(enabled=True,auth_mode="tunnel")
        app=create_app(config,store,lambda:True)
        client=app.test_client()
        errors=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(channel="chrome",headless=True)
            page=browser.new_page(viewport={"width":1366,"height":768},device_scale_factor=1)
            page.on("pageerror",lambda error: errors.append(str(error)))
            def route_handler(route):
                req=route.request
                path=req.url.removeprefix(config.origin)
                if not req.url.startswith(config.origin+"/"):
                    route.abort();return
                response=client.open(path,method=req.method,base_url=config.origin,
                                     data=req.post_data,headers=dict(req.headers))
                try:
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.get_data())
                finally:
                    response.close()
            page.route("**/*",route_handler)
            page.goto(config.origin)
            assert page.locator('meta[name="apple-mobile-web-app-capable"]').get_attribute('content')=='yes'
            assert page.locator('link[rel="apple-touch-icon"]').get_attribute('href')=='/assets/apple-touch-icon.png'
            icon_response=client.get('/assets/apple-touch-icon.png',base_url=config.origin)
            assert icon_response.status_code==200
            assert icon_response.mimetype=='image/png'
            assert icon_response.data.startswith(b'\x89PNG\r\n\x1a\n')
            assert int.from_bytes(icon_response.data[16:20],'big')==192
            assert int.from_bytes(icon_response.data[20:24],'big')==192
            icon_response.close()
            page.get_by_role("button",name="+15025550124").click()
            assert page.locator(".bubble").inner_text()==hostile
            assert page.locator(".bubble img").count()==0
            assert page.evaluate("window.pwned === undefined")
            page.get_by_role("button",name="+15025550123").click()
            page.locator("#body").fill("Hello from the portal")
            page.on("dialog",lambda dialog: dialog.accept())
            send=page.get_by_role('button',name='Send',exact=True)
            assert page.locator('#send-help,#remaining').count()==0
            assert page.locator('#body').get_attribute('maxlength')=='70'
            assert send.inner_text().strip()=='Send'
            assert send.locator('svg').count()==1
            assert send.locator('svg').get_attribute('viewBox')=='-8 0 32 32'
            assert send.locator('svg').get_attribute('fill')=='currentColor'
            send.click()
            page.locator("#timeline").get_by_text("Hello from the portal",exact=True).wait_for()
            assert len(store.history("out",2**63-1))==1
            assert page.locator("#body").input_value()==""
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
            assert page.locator("#send").bounding_box()["y"] < 768
            assert page.locator("header,footer,.brand,#count").count()==0
            assert page.locator('#audit-toggle').count()==0
            assert not page.locator('#menu-toggle').is_visible()
            assert page.locator('#contacts').evaluate('el=>getComputedStyle(el).flexDirection')=='column'
            assert page.locator("#connection").evaluate("el=>getComputedStyle(el).position")=="fixed"
            dot=page.locator("#connection").bounding_box()
            assert dot["x"]>1300 and dot["y"]<20
            page.locator("#connection").click()
            page.locator("#logins-items .login-entry").wait_for()
            page.get_by_role("button",name="Archives",exact=True).click()
            archive_link=page.get_by_role("link",name=archive_name,exact=True)
            archive_link.wait_for()
            assert archive_link.get_attribute("href")=="https://drive.proton.me/"
            assert archive_link.get_attribute("target")=="_blank"
            assert archive_link.get_attribute("rel")=="noopener noreferrer"
            assert 'Folder:' not in page.locator('#audit-items').inner_text()
            assert '/my-files/RPI-SMS' not in page.locator('#audit-items').inner_text()
            assert 'Open Proton Drive to download' not in page.locator('#audit-items').inner_text()
            for scheme,normal,hover in [('light','rgb(48, 110, 155)','rgb(38, 149, 229)'),
                                        ('dark','rgb(40, 89, 124)','rgb(45, 113, 161)')]:
                page.emulate_media(color_scheme=scheme)
                page.mouse.move(0,0)
                assert archive_link.evaluate('el=>getComputedStyle(el).color')==normal
                archive_link.hover()
                assert archive_link.evaluate('el=>getComputedStyle(el).color')==hover
                page.keyboard.press('Tab')
                archive_link.focus()
                assert archive_link.evaluate('el=>el===document.activeElement')
                assert archive_link.evaluate('el=>getComputedStyle(el).outlineStyle')=='solid'
            page.screenshot(path=str(Path(tempfile.gettempdir())/'ec25-portal-archives.png'),full_page=True)
            page.emulate_media(color_scheme='light')
            page.get_by_role("button",name="+15025550123").click()
            page.screenshot(path=str(Path(tempfile.gettempdir())/'ec25-portal-desktop.png'),full_page=True)
            page.emulate_media(color_scheme="dark")
            page.screenshot(path=str(Path(tempfile.gettempdir())/'ec25-portal-dark.png'),full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            expect(page.locator('#sidebar')).to_have_js_property('inert',True)
            menu=page.locator('#menu-toggle')
            assert menu.is_visible()
            assert menu.get_attribute('aria-expanded')=='false'
            assert page.locator('#sidebar').evaluate('el=>el.inert')
            menu.click()
            assert menu.get_attribute('aria-expanded')=='true'
            assert page.locator('.workspace').evaluate('el=>el.inert')
            assert page.locator('#contacts').evaluate('el=>getComputedStyle(el).flexDirection')=='column'
            contacts=page.locator('.contact')
            first,second=contacts.nth(0).bounding_box(),contacts.nth(1).bounding_box()
            assert first['x']==second['x'] and second['y']>=first['y']+first['height']
            assert page.locator('#new').is_visible() and page.locator('#archives-toggle').is_visible()
            page.screenshot(path=str(Path(tempfile.gettempdir())/'ec25-portal-mobile-sidebar.png'),full_page=True)
            page.keyboard.press('Escape')
            assert menu.get_attribute('aria-expanded')=='false'
            assert menu.evaluate('el=>document.activeElement===el')
            menu.click()
            page.get_by_role('button',name='+15025550124').click()
            assert menu.get_attribute('aria-expanded')=='false'
            assert page.locator('.bubble').inner_text()==hostile
            menu.click()
            page.locator('#new').click()
            assert menu.get_attribute('aria-expanded')=='false'
            assert page.locator('#recipient').evaluate('el=>document.activeElement===el')
            menu.click()
            page.locator('#archives-toggle').click()
            archive_link.wait_for()
            assert menu.get_attribute('aria-expanded')=='false'
            menu.click()
            page.locator('#sidebar-backdrop').click(position={'x':380,'y':400})
            assert menu.get_attribute('aria-expanded')=='false'
            menu.click()
            page.get_by_role('button',name='+15025550123').click()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
            page.screenshot(path=str(Path(tempfile.gettempdir())/'ec25-portal-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1366,'height':768})
            expect(page.locator('#sidebar')).to_have_js_property('inert',False)
            assert page.locator('#sidebar').is_visible()
            assert not page.locator('#sidebar').evaluate('el=>el.inert')
            assert not page.locator('.workspace').evaluate('el=>el.inert')
            for width in [390,320]:
                page.set_viewport_size({'width':width,'height':844})
                page.reload()
                page.locator('.contact').first.wait_for(state='attached')
                assert page.locator('#title').inner_text()=='Messages'
                icon=page.locator('#menu-toggle').bounding_box()
                heading=page.locator('.conversation-heading>div').bounding_box()
                assert heading['x']>=icon['x']+icon['width']
                assert abs(heading['y']-icon['y'])<=10
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(Path(tempfile.gettempdir())/f'ec25-heading-{width}.png'),full_page=True)
            assert not errors, errors
            browser.close()
        print("Browser passed: literal hostile text, compose/idempotent queue, responsive layout; no live SMS sent.")


if __name__=="__main__": main()
