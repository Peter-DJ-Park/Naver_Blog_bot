"""
냉장고를 부탁해 레시피 - 네이버 블로그 자동 발행 프로그램 v3
────────────────────────────────────────────────────────────
흐름:
  Step 1 : recipes.csv 에서 발행여부=N 인 첫 번째 행 읽기
  Step 2 : 로컬 이미지를 imgBB 에 업로드 → 웹 URL 확보
  Step 3 : OpenAI GPT-4o-mini 로 블로그 본문(HTML) 생성
  Step 4 : 네이버 블로그 쿠키 기반 내부 API 로 포스트 발행
            ├─ 방법 A : 내부 REST API (JSON)
            └─ 방법 B : PostSave.naver 폼 POST (Fallback)
  Step 5 : CSV 발행여부 → Y 업데이트

※ 쿠키 추출 방법 및 imgBB API 키 발급은 README.md 참고
"""

import os
import csv
import re
import base64
import time
from pathlib import Path

import requests
from openai import OpenAI
from dotenv import load_dotenv

# ── 환경변수 로드 ─────────────────────────────────────────────────
load_dotenv()

NAVER_COOKIE   = os.getenv("NAVER_COOKIE")       # 네이버 로그인 쿠키 전체 문자열
NAVER_BLOG_ID  = os.getenv("NAVER_BLOG_ID")      # 블로그 아이디 (URL 영문 ID)
IMGBB_API_KEY  = os.getenv("IMGBB_API_KEY")      # imgBB API Key
OPENAI_KEY     = os.getenv("OPENAI_API_KEY")     # OpenAI API Key
CSV_PATH       = os.getenv("CSV_PATH", "recipes.csv")

client = OpenAI(api_key=OPENAI_KEY)

# ── requests 세션 공통 설정 ───────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Cookie": NAVER_COOKIE,
})


# ════════════════════════════════════════════════════════════════
# 유틸: 네이버 쿠키 유효성 확인
# ════════════════════════════════════════════════════════════════

def check_cookie_valid() -> bool:
    """로그인 상태 확인. 유효하면 True, 만료됐으면 False."""
    try:
        resp = SESSION.get(
            f"https://blog.naver.com/PostWriteForm.naver?blogId={NAVER_BLOG_ID}",
            timeout=15,
            allow_redirects=True,
        )
        if "nid.naver.com/nidlogin" in resp.url or "로그인" in resp.text[:500]:
            return False
        return True
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════
# 유틸: CSRF 토큰 추출
# ════════════════════════════════════════════════════════════════

def get_csrf_token() -> str:
    """네이버 글쓰기 폼에서 CSRF 토큰 추출. 5가지 패턴 순서대로 시도."""
    resp = SESSION.get(
        f"https://blog.naver.com/PostWriteForm.naver?blogId={NAVER_BLOG_ID}",
        timeout=15,
    )
    html = resp.text

    patterns = [
        r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']_csrf["\']',
        r'"_csrf"\s*:\s*"([^"]+)"',
        r'"token"\s*:\s*"([^"]+)"',
        r"csrf['\"]?\s*[:=]\s*['\"]([^'\"]{10,})['\"]",
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            token = m.group(1)
            print(f"[CSRF] 토큰 추출 성공: {token[:10]}...")
            return token

    print("[WARN] CSRF 토큰 추출 실패 — 쿠키 만료 가능성 있음")
    return ""


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
# 2. imgBB 이미지 업로드
# ════════════════════════════════════════════════════════════════

def upload_to_imgbb(local_path: str) -> str:
    """
    로컬 이미지를 imgBB 에 업로드 후 직접 링크(URL) 반환.
    실패 시 빈 문자열 반환.
    """
    local_path = local_path.strip()
    if not local_path or not Path(local_path).exists():
        print(f"[WARN] 이미지 없음: '{local_path}'")
        return ""

    with open(local_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    resp = requests.post(
        "https://api.imgbb.com/1/upload",
        data={
            "key":   IMGBB_API_KEY,
            "image": image_data,
        },
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
# 3. OpenAI 블로그 본문 생성 (네이버 블로그 최적화 HTML)
# ════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """
너는 '냉장고를 부탁해' 레시피 전문 블로거이자 스타 셰프들의 열혈 팬이야.
네이버 블로그에 바로 붙여넣을 수 있는 HTML 형식으로 포스팅을 작성해 줘.
규칙:
- 코드블록(```html 등) 없이 순수 HTML 태그만 출력해.
- 외부 CSS / JS 없이 인라인 스타일만 사용해.
- 네이버 블로그 에디터 호환을 위해 <div>, <p>, <img>, <ul>, <ol>, <h2>, <h3>, <span>, <a> 태그만 사용해.
- 전체를 <div> 하나로 감싸서 반환해.
- 이모지를 적절히 사용해 가독성을 높여.
"""

USER_PROMPT_TEMPLATE = """
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
    """GPT-4o-mini 로 포스트 제목과 HTML 본문 생성."""
    prompt = USER_PROMPT_TEMPLATE.format(
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

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        temperature=0.8,
        max_tokens=3500,
    )

    html = resp.choices[0].message.content.strip()

    # 코드펜스 잔재 제거
    html = re.sub(r"^```[a-z]*\n?", "", html)
    html = re.sub(r"\n?```$",       "", html)

    title = f"[냉부해] {row['셰프이름']} 셰프의 {row['요리이름']} 레시피"
    print(f"[AI] 본문 생성 완료 | 제목: {title}")
    return title, html.strip()


# ════════════════════════════════════════════════════════════════
# 4. 네이버 블로그 발행 (방법 A → B Fallback)
# ════════════════════════════════════════════════════════════════

def _try_method_a(title: str, html: str, csrf: str) -> str:
    """방법 A: 네이버 블로그 내부 REST API (JSON)"""
    endpoint = f"https://blog.naver.com/api/blogs/{NAVER_BLOG_ID}/posts"
    headers = {
        "Content-Type":     "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token":     csrf,
        "Referer": f"https://blog.naver.com/PostWriteForm.naver?blogId={NAVER_BLOG_ID}",
    }
    payload = {
        "title":         title,
        "contents":      html,
        "categoryNo":    "0",
        "tagList":       "냉장고를부탁해,냉부해레시피,요리,초간단요리",
        "publishMoment": "PUBLIC",
        "addContents":   "",
    }
    try:
        resp   = SESSION.post(endpoint, headers=headers, json=payload, timeout=30)
        data   = resp.json()
        log_no = (data.get("logNo")
                  or data.get("result", {}).get("logNo", "")
                  or data.get("data",   {}).get("logNo", ""))
        if log_no:
            url = f"https://blog.naver.com/{NAVER_BLOG_ID}/{log_no}"
            print(f"[방법A] 발행 성공 | logNo={log_no}")
            return url
        print(f"[방법A] logNo 없음: {str(data)[:200]}")
    except Exception as e:
        print(f"[방법A] 실패: {e}")
    return ""


def _try_method_b(title: str, html: str, csrf: str) -> str:
    """방법 B: PostSave.naver 폼 POST (Fallback)"""
    endpoint = "https://blog.naver.com/PostSave.naver"
    headers = {
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://blog.naver.com/PostWriteForm.naver?blogId={NAVER_BLOG_ID}",
    }
    payload = {
        "blogId":        NAVER_BLOG_ID,
        "title":         title,
        "body":          html,
        "categoryNo":    "0",
        "tag":           "냉장고를부탁해,냉부해레시피",
        "publishMoment": "PUBLIC",
        "outUrl":        "",
        "keepPost":      "N",
        "_csrf":         csrf,
    }
    try:
        resp   = SESSION.post(endpoint, headers=headers, data=payload, timeout=30)
        data   = resp.json()
        log_no = data.get("logNo", "")
        if log_no:
            url = f"https://blog.naver.com/{NAVER_BLOG_ID}/{log_no}"
            print(f"[방법B] 발행 성공 | logNo={log_no}")
            return url
        print(f"[방법B] logNo 없음: {str(data)[:200]}")
    except Exception as e:
        print(f"[방법B] 실패: {e}")
    return ""


def publish_to_naver_blog(title: str, html_content: str) -> str:
    """
    네이버 블로그에 포스트를 발행합니다.
    방법 A → 방법 B 순서로 Fallback 시도.
    """
    # 쿠키 유효성 사전 확인
    if not check_cookie_valid():
        print()
        print("=" * 60)
        print("  ❌ 네이버 쿠키가 만료되었습니다.")
        print()
        print("  재추출 방법:")
        print("  1. 크롬에서 blog.naver.com 로그인")
        print("  2. F12 → Network 탭")
        print("  3. 임의 요청 클릭 → Request Headers → Cookie 전체 복사")
        print("  4. .env 의 NAVER_COOKIE 값 업데이트 후 재실행")
        print("=" * 60)
        return ""

    csrf = get_csrf_token()

    print("[발행] 방법 A 시도 중...")
    result = _try_method_a(title, html_content, csrf)
    if result:
        return result

    time.sleep(1)
    print("[발행] 방법 B 시도 중...")
    result = _try_method_b(title, html_content, csrf)
    if result:
        return result

    print("[ERROR] 방법 A, B 모두 실패했습니다.")
    print("        네이버 내부 API 구조가 변경됐을 수 있습니다.")
    print("        README.md 의 '발행 실패 시 대처법' 섹션을 참고하세요.")
    return ""


# ════════════════════════════════════════════════════════════════
# 5. 메인 실행 흐름
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  냉부해 레시피 네이버 블로그 자동 발행 v3")
    print("=" * 60)

    # Step 1: 미발행 레시피 로드
    row, _ = load_pending_recipe(CSV_PATH)
    if row is None:
        print("[INFO] 발행할 레시피가 없습니다. 모두 완료 상태입니다.")
        return
    print(f"[STEP 1] 로드 완료 | ID={row['요리ID']} | {row['셰프이름']} - {row['요리이름']}")

    # Step 2: imgBB 이미지 업로드
    print("[STEP 2] imgBB 이미지 업로드 중...")
    thumb_url = upload_to_imgbb(row["썸네일사진경로"])
    p1_url    = upload_to_imgbb(row["방송사진1경로"])
    p2_url    = upload_to_imgbb(row["방송사진2경로"])

    # Step 3: AI 본문 생성
    print("[STEP 3] AI 블로그 본문 생성 중...")
    title, html = generate_blog_content(row, thumb_url, p1_url, p2_url)

    # Step 4: 네이버 블로그 발행
    print("[STEP 4] 네이버 블로그 발행 중...")
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
