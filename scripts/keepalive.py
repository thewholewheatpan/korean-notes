"""배포된 앱에 실제 브라우저로 접속해서 잠들지 않게 함 (GitHub Actions에서 주기적으로 실행).

- Streamlit Cloud는 단순 HTTP 요청으로는 깨지 않으므로 헤드리스 브라우저로 접속함
- 잠들어 있으면 'Yes, get this app back up!' 버튼을 눌러 깨움
- 앱 첫 화면이 학생·숙제 목록을 DB에서 읽으므로 Supabase 무료 프로젝트의 일시정지도 함께 막음

사용법: APP_URL=https://xxx.streamlit.app python scripts/keepalive.py
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

APP_URL = os.environ["APP_URL"]
APP_TEXT = "국어 오답노트"          # 앱이 떴는지 확인할 제목 글자
WAKE_TEXT = "get this app back up"  # 잠든 앱의 깨우기 버튼 글자
TIMEOUT_S = 240


def page_texts(page):
    # Streamlit Cloud는 앱을 iframe 안에 띄우므로 모든 frame의 글자를 확인함
    texts = []
    for frame in page.frames:
        try:
            texts.append(frame.inner_text("body", timeout=2000))
        except Exception:
            pass
    return "\n".join(texts)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)

        woke = False
        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            if not woke:
                for frame in page.frames:
                    button = frame.get_by_role("button", name=WAKE_TEXT)
                    if button.count():
                        print("앱이 잠들어 있어서 깨우는 중...")
                        button.first.click()
                        woke = True
                        break
            if APP_TEXT in page_texts(page):
                # 첫 화면의 DB 조회가 끝날 시간을 조금 더 줌
                page.wait_for_timeout(5000)
                print(f"OK: 앱이 떠 있습니다 ({'깨움' if woke else '원래 깨어 있음'})")
                browser.close()
                return 0
            page.wait_for_timeout(3000)

        page.screenshot(path="keepalive-failed.png", full_page=True)
        print(f"실패: {TIMEOUT_S}초 안에 앱 화면을 찾지 못했습니다.", file=sys.stderr)
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
