"""
냉장고를 부탁해 레시피 - 네이버 블로그 자동 발행 프로그램 v6
────────────────────────────────────────────────────────────
흐름:
  Step 1 : recipes.csv 에서 발행여부=N 인 첫 번째 행 읽기
  Step 2 : imgBB URL 확인 (image_collector 로 미리 수집된 URL 재사용)
  Step 3 : Groq API (llama-3.3-70b) 로 블로그 본문(HTML) 생성
  Step 4 : Selenium 으로 네이버 블로그 자동 발행
            ├─ 쿠키 주입으로 로그인 처리
            ├─ 글쓰기 페이지 접속
            ├─ 제목 / 본문 입력
            └─ 발행 버튼 클릭
  Step 5 : CSV 발행여부 → Y 업데이트

※ 최초 실행 시 크롬 브라우저가 자동으로 열립니다.
※ 쿠키 추출 방법은 README.md 참고
"""

import os
import csv
import re
import base64
import time
import json
from pathlib import Path

import requests
from groq import Groq
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ── 환경변수 로드 ─────────────────────────────────────────────────
load_dotenv()

NAVER_COOKIE  = os.getenv("NAVER_COOKIE")       # 네이버 로그인 쿠키 전체 문자열
NAVER_BLOG_ID = os.getenv("NAVER_BLOG_ID")      # 블로그 아이디 (URL 영문 ID)
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY")      # imgBB API Key
GROQ_KEY      = os.getenv("GROQ_API_KEY")       # Groq API Key
CSV_PATH      = os.getenv("CSV_PATH", "recipes.csv")

groq_client = Groq(api_key=GROQ_KEY)


# ════════════════════════════════════════════════════════════════
# 유틸: 쿠키 문자열 → 딕셔너리 리스트 변환
# ════════════════════════════════════════════════════════════════

def parse_cookies(cookie_str: str) -> list[dict]:
    """
    'key=value; key2=value2' 형태의 쿠키 문자열을
    Selenium add_cookie() 에 맞는 딕셔너리 리스트로 변환.
    """
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            cookies.append({
                "name":   key.strip(),
                "value":  value.strip(),
                "domain": ".naver.com",
            })
    return cookies


# ════════════════════════════════════════════════════════════════
# 1. CSV 읽기 / 쓰기
# ════════════════════════════════════════════════════════════════

FIELDNAMES = [
    "요리ID", "셰프이름", "셰프인스타ID", "셰프프로필정보",
    "요리이름", "원본재료", "조리과정요약",
    "썸네일사진경로", "방송사진1경로", "방송사진2경로",
    "발행여부",
]

def load_pending_recipe(csv_path: str) -> tuple[dict | None, int]:
    """발행여부=N 인 첫 번째 행 반환."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            if row.get("발행여부", "N").strip().upper() == "N":
                return dict(row), idx
    return None, -1


def mark_as_published(csv_path: str, target_id: str) -> None:
    """요리ID 기준으로 발행여부를 Y 로 변경."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["요리ID"] == target_id:
                row["발행여부"] = "Y"
            rows.append(row)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[CSV] 요리ID={target_id} 발행여부 → Y 저장 완료")


# ════════════════════════════════════════════════════════════════
# 2. imgBB 이미지 처리
# ════════════════════════════════════════════════════════════════

def upload_to_imgbb(local_path: str) -> str:
    """
    이미 웹 URL 이면 그대로 반환.
    로컬 경로면 imgBB 에 업로드 후 URL 반환.
    """
    local_path = local_path.strip()

    # 이미 웹 URL 이면 그대로 사용
    if local_path.startswith("http://") or local_path.startswith("https://"):
        print(f"[imgBB] URL 그대로 사용: {local_path[:60]}")
        return local_path

    if not local_path or not Path(local_path).exists():
        print(f"[WARN] 이미지 없음: '{local_path}'")
        return ""

    with open(local_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    resp = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": IMGBB_API_KEY, "image": image_data},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        print(f"[WARN] imgBB 업로드 실패: {data}")
        return ""

    url = data["data"]["url"]
    print(f"[imgBB] 업로드 완료: {Path(local_path).name} → {url}")
    return url


# ════════════════════════════════════════════════════════════════
# 3. Groq 블로그 본문 생성
# ════════════════════════════════════════════════════════════════

PROMPT_TEMPLATE = """
너는 '냉장고를 부탁해' 레시피 전문 블로거이자 스타 셰프들의 열혈 팬이야.
네이버 블로그에 바로 붙여넣을 수 있는 HTML 형식으로 포스팅을 작성해 줘.

[출력 규칙]
- 코드블록(```html 등) 없이 순수 HTML 태그만 출력해.
- 외부 CSS / JS 없이 인라인 스타일만 사용해.
- 사용 가능한 태그: <div>, <p>, <img>, <ul>, <ol>, <h2>, <h3>, <span>, <a>
- 전체를 <div> 하나로 감싸서 반환해.
- 이모지를 적절히 사용해 가독성을 높여.

[입력 데이터]
- 셰프: {셰프이름} / 인스타: @{셰프인스타ID} / 프로필: {셰프프로필정보}
- 요리: {요리이름}
- 재료: {원본재료}
- 조리과정: {조리과정요약}
- 썸네일 URL : {썸네일URL}
- 방송사진1 URL: {방송사진1URL}
- 방송사진2 URL: {방송사진2URL}

[작성 규칙 — HTML 출력]

1. 제목 <h2 style="font-size:24px;font-weight:bold;color:#1a1a1a;margin:16px 0 8px;line-height:1.5;">
   "역시 {셰프프로필정보}, {셰프이름} 셰프의 {요리이름}! 15분 만에 따라잡기"

2. 도입부 <p style="font-size:15px;line-height:1.9;color:#444;margin-bottom:14px;">
   셰프 소개 2~3문장. '냉장고를 부탁해 레시피' 키워드 자연스럽게 포함.

3. 셰프 프로필 카드
   <div style="background:#f7f7f7;border-left:4px solid #03c75a;border-radius:6px;padding:16px 20px;margin:18px 0;font-size:14px;line-height:2;">
     이름 / 분야 / 인스타그램 링크 (target="_blank", color:#03c75a)
   </div>

4. 메인 사진 (썸네일)
   <img src="{썸네일URL}" style="width:100%;max-width:680px;border-radius:10px;margin:16px 0;display:block;" alt="완성된 {요리이름}">

5. 재료 섹션
   <h3 style="font-size:18px;font-weight:bold;color:#333;border-bottom:2px solid #03c75a;padding-bottom:6px;margin:20px 0 10px;">
   🛒 재료 목록
   </h3>
   <ul style="font-size:15px;line-height:2.2;padding-left:18px;color:#444;">
   구하기 어려운 재료 옆에:
   <span style="font-size:13px;color:#e74c3c;">🔁 현실 패치: 대체재 설명</span>

6. 조리 단계
   <h3 style="font-size:18px;font-weight:bold;color:#333;border-bottom:2px solid #03c75a;padding-bottom:6px;margin:20px 0 10px;">
   👨‍🍳 조리 순서
   </h3>
   <ol style="font-size:15px;line-height:2.2;padding-left:18px;color:#444;">
   중요 단계 직후 방송사진 자연스럽게 삽입:
   <img src="{방송사진1URL}" style="width:100%;max-width:680px;border-radius:8px;margin:10px 0;display:block;" alt="방송 캡처 1">
   <img src="{방송사진2URL}" style="width:100%;max-width:680px;border-radius:8px;margin:10px 0;display:block;" alt="방송 캡처 2">

7. 셰프의 킥(Point) 강조 박스
   <div style="background:#f0fff4;border:2px solid #03c75a;border-radius:10px;padding:16px 20px;margin:22px 0;">
     <p style="font-size:16px;font-weight:bold;color:#03c75a;margin:0 0 8px;">⭐ 셰프의 킥 (핵심 비법)</p>
     <p style="font-size:15px;line-height:1.8;color:#333;margin:0;">핵심 비법 내용</p>
   </div>

8. 마무리 문단 <p style="font-size:15px;line-height:1.9;color:#444;margin-top:16px;">
   '초간단 15분 요리', '{셰프이름} 인스타', '냉장고를 부탁해 레시피' 자연스럽게 포함.

9. 해시태그 (맨 하단)
   <p style="color:#aaa;font-size:13px;margin-top:28px;line-height:2;">
   #냉장고를부탁해 #냉부해레시피 #{셰프이름} #{요리이름} #초간단요리 #15분요리 #집밥
   </p>

위 규칙을 모두 반영한 완성된 HTML 을 작성해 줘.
"""

def generate_blog_content(row: dict, thumb: str, p1: str, p2: str) -> tuple[str, str]:
    """Groq (llama-3.3-70b) 로 포스트 제목과 HTML 본문 생성."""
    prompt = PROMPT_TEMPLATE.format(
        셰프이름       = row["셰프이름"],
        셰프인스타ID   = row["셰프인스타ID"],
        셰프프로필정보 = row["셰프프로필정보"],
        요리이름       = row["요리이름"],
        원본재료       = row["원본재료"],
        조리과정요약   = row["조리과정요약"],
        썸네일URL      = thumb,
        방송사진1URL   = p1,
        방송사진2URL   = p2,
    )

    resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "너는 '냉장고를 부탁해' 레시피 전문 블로거야. "
                    "코드블록 없이 순수 HTML 만 출력해. "
                    "외부 CSS/JS 없이 인라인 스타일만 사용해. "
                    "전체를 <div> 하나로 감싸서 반환해."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.8,
        max_tokens=3500,
    )

    html = resp.choices[0].message.content.strip()
    html = re.sub(r"^```[a-z]*\n?", "", html)
    html = re.sub(r"\n?```$", "", html)

    title = f"[냉부해] {row['셰프이름']} 셰프의 {row['요리이름']} 레시피"
    print(f"[AI] 본문 생성 완료 | 제목: {title}")
    return title, html.strip()


# ════════════════════════════════════════════════════════════════
# 4. Selenium 네이버 블로그 발행
# ════════════════════════════════════════════════════════════════

def create_driver() -> webdriver.Chrome:
    """Chrome 드라이버 생성 (headless 옵션 없음 — 화면 표시)."""
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def inject_naver_cookies(driver: webdriver.Chrome) -> None:
    """
    네이버 쿠키를 Selenium 세션에 주입합니다.
    쿠키 주입 전 반드시 naver.com 에 먼저 접속해야 합니다.
    """
    driver.get("https://www.naver.com")
    time.sleep(2)

    cookies = parse_cookies(NAVER_COOKIE)
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass  # 일부 쿠키 주입 실패는 무시

    print(f"[Selenium] 쿠키 {len(cookies)}개 주입 완료")


def publish_to_naver_blog(title: str, html_content: str) -> str:
    """
    Selenium 으로 네이버 블로그 스마트에디터 ONE 에 포스트를 발행합니다.
    성공 시 발행된 포스트 URL 반환.
    """
    driver = create_driver()
    wait   = WebDriverWait(driver, 20)
    post_url = ""

    try:
        # ── 1. 쿠키 주입으로 로그인 ───────────────────────────────
        print("[Selenium] 쿠키 주입 중...")
        inject_naver_cookies(driver)

        # ── 2. 글쓰기 페이지 접속 ────────────────────────────────
        print("[Selenium] 글쓰기 페이지 접속 중...")
        driver.get(f"https://blog.naver.com/{NAVER_BLOG_ID}/postwrite")
        time.sleep(4)

        # 로그인 확인 (로그인 페이지로 리다이렉트됐으면 실패)
        if "nid.naver.com" in driver.current_url:
            print("[ERROR] 로그인 실패 — 쿠키를 재추출해 주세요.")
            return ""

        print(f"[Selenium] 현재 URL: {driver.current_url}")

        # ── 3. 제목 입력 ──────────────────────────────────────────
        print("[Selenium] 제목 입력 중...")
        time.sleep(3)  # 에디터 완전 로드 대기

        # 스마트에디터 ONE 제목 영역: contenteditable div
        title_selectors = [
            "div.se-title-text",
            "div[contenteditable='true'][class*='title']",
            ".se-section-title div[contenteditable='true']",
            "div[contenteditable='true']",  # 첫 번째 contenteditable = 제목
        ]
        title_input = None
        for selector in title_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    if el.is_displayed():
                        title_input = el
                        break
                if title_input:
                    print(f"[Selenium] 제목창 발견: {selector}")
                    break
            except Exception:
                continue

        if title_input:
            # JavaScript 로 클릭 및 텍스트 입력 (element not interactable 우회)
            driver.execute_script("arguments[0].click();", title_input)
            time.sleep(0.5)
            driver.execute_script(
                "arguments[0].innerText = arguments[1];", title_input, title
            )
            # 커서를 끝으로 이동
            driver.execute_script(
                """
                var el = arguments[0];
                var range = document.createRange();
                var sel = window.getSelection();
                range.selectNodeContents(el);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
                """,
                title_input,
            )
            print(f"[Selenium] 제목 입력 완료: {title}")
        else:
            print("[WARN] 제목 입력창을 찾지 못했습니다.")

        time.sleep(1)

        # ── 4. 본문 입력 (iframe body 방식) ──────────────────────
        print("[Selenium] 본문 입력 중...")
        try:
            # iframe 탐색
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            editor_iframe = None
            for fr in iframes:
                if fr.is_displayed() and fr.size.get("width", 0) > 100:
                    editor_iframe = fr
                    break
            # 작은 iframe 도 포함해서 재시도
            if not editor_iframe and iframes:
                editor_iframe = iframes[0]

            if editor_iframe:
                driver.switch_to.frame(editor_iframe)
                body = driver.find_element(By.TAG_NAME, "body")
                # 기존 내용 삭제 후 HTML 삽입
                driver.execute_script(
                    """
                    var body = arguments[0];
                    body.focus();
                    body.innerHTML = arguments[1];
                    body.dispatchEvent(new Event('input', {bubbles: true}));
                    body.dispatchEvent(new Event('change', {bubbles: true}));
                    """,
                    body,
                    html_content,
                )
                print("[Selenium] 본문 입력 완료 (iframe body)")
                driver.switch_to.default_content()
                time.sleep(1)
            else:
                print("[WARN] 본문 iframe 을 찾지 못했습니다.")
        except Exception as e:
            print(f"[WARN] 본문 입력 오류: {e}")
            driver.switch_to.default_content()

        time.sleep(2)

        # ── 5. 발행 버튼 클릭 ────────────────────────────────────
        print("[Selenium] 발행 버튼 클릭 중...")
        publish_selectors = [
            "button.publish_btn__m9KHH",
            "button[class*='publish']",
            "button[class*='save_btn']",
            "button.confirm_btn",
            "//button[contains(text(), '발행')]",
            "//button[contains(text(), '등록')]",
            "//button[contains(text(), '공개발행')]",
        ]

        published = False
        for selector in publish_selectors:
            try:
                if selector.startswith("//"):
                    btn = wait.until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    btn = wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                btn.click()
                print(f"[Selenium] 발행 버튼 클릭: {selector}")
                published = True
                break
            except TimeoutException:
                continue

        if not published:
            print("[WARN] 발행 버튼을 찾지 못했습니다. 수동으로 발행해 주세요.")
            input("발행 완료 후 Enter 키를 누르세요...")

        time.sleep(3)

        # ── 6. 발행 확인 팝업 처리 (있는 경우) ───────────────────
        confirm_selectors = [
            "button.confirm_btn__WEaBq",
            "button[class*='confirm']",
            "//button[contains(text(), '확인')]",
            "//button[contains(text(), '공개발행')]",
            "//button[contains(text(), '발행하기')]",
        ]
        for selector in confirm_selectors:
            try:
                if selector.startswith("//"):
                    btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                btn.click()
                print(f"[Selenium] 확인 버튼 클릭: {selector}")
                break
            except TimeoutException:
                continue

        time.sleep(4)

        # ── 7. 발행된 URL 확인 ───────────────────────────────────
        current_url = driver.current_url
        print(f"[Selenium] 발행 후 URL: {current_url}")

        if NAVER_BLOG_ID in current_url and "postwrite" not in current_url:
            post_url = current_url
        else:
            # URL 에서 logNo 추출 시도
            m = re.search(r"logNo=(\d+)", current_url)
            if m:
                post_url = f"https://blog.naver.com/{NAVER_BLOG_ID}/{m.group(1)}"
            else:
                # 페이지 내 링크에서 발행된 포스트 URL 탐색
                try:
                    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='blog.naver.com']")
                    for link in links:
                        href = link.get_attribute("href") or ""
                        if NAVER_BLOG_ID in href and re.search(r"/\d+$", href):
                            post_url = href
                            break
                except Exception:
                    pass

        if post_url:
            print(f"[Selenium] 발행 성공 | URL: {post_url}")
        else:
            print("[WARN] 발행 URL 확인 불가 — 블로그에서 직접 확인해 주세요.")
            post_url = f"https://blog.naver.com/{NAVER_BLOG_ID}"

    except Exception as e:
        print(f"[ERROR] Selenium 발행 중 오류: {e}")

    finally:
        time.sleep(2)
        driver.quit()
        print("[Selenium] 브라우저 종료")

    return post_url


# ════════════════════════════════════════════════════════════════
# 5. 메인 실행 흐름
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  냉부해 레시피 네이버 블로그 자동 발행 v6 (Selenium)")
    print("=" * 60)

    # Step 1: 미발행 레시피 로드
    row, _ = load_pending_recipe(CSV_PATH)
    if row is None:
        print("[INFO] 발행할 레시피가 없습니다. 모두 완료 상태입니다.")
        return
    print(f"[STEP 1] 로드 완료 | ID={row['요리ID']} | {row['셰프이름']} - {row['요리이름']}")

    # Step 2: 이미지 URL 확인
    print("[STEP 2] 이미지 URL 확인 중...")
    thumb_url = upload_to_imgbb(row["썸네일사진경로"])
    p1_url    = upload_to_imgbb(row["방송사진1경로"])
    p2_url    = upload_to_imgbb(row["방송사진2경로"])

    # Step 3: AI 본문 생성
    print("[STEP 3] AI 블로그 본문 생성 중...")
    title, html = generate_blog_content(row, thumb_url, p1_url, p2_url)

    # Step 4: Selenium 으로 발행
    print("[STEP 4] Selenium 으로 네이버 블로그 발행 중...")
    print("         ※ 크롬 브라우저가 자동으로 열립니다.")
    post_url = publish_to_naver_blog(title, html)

    # Step 5: CSV 업데이트
    if post_url:
        print("[STEP 5] CSV 상태 업데이트 중...")
        mark_as_published(CSV_PATH, row["요리ID"])
        print()
        print("=" * 60)
        print("  ✅ 발행 완료!")
        print(f"  📄 제목: {title}")
        print(f"  🔗 URL : {post_url}")
        print("=" * 60)
    else:
        print()
        print("[WARN] 발행 실패 — CSV 는 업데이트하지 않았습니다.")


if __name__ == "__main__":
    main()
