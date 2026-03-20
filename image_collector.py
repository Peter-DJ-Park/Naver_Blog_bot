"""
냉장고를 부탁해 - 이미지 자동 수집 및 imgBB 업로드
────────────────────────────────────────────────────────────
흐름:
  Step 1 : recipes.csv 에서 이미지가 없는 행 읽기
           (썸네일사진경로 / 방송사진1경로 / 방송사진2경로 가 비어있는 행)
  Step 2 : 네이버 이미지 검색 API 로 "{셰프이름} {요리이름}" 검색
  Step 3 : 검색 결과 이미지를 imgBB 에 업로드 → URL 획득
  Step 4 : recipes.csv 의 이미지 컬럼에 URL 자동 저장

※ 네이버 검색 API 키 발급 방법은 README.md 참고
"""

import os
import csv
import time
import base64
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID")      # 네이버 API Client ID
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")  # 네이버 API Client Secret
IMGBB_API_KEY       = os.getenv("IMGBB_API_KEY")        # imgBB API Key
CSV_PATH            = os.getenv("CSV_PATH", "recipes.csv")

FIELDNAMES = [
    "요리ID", "셰프이름", "셰프인스타ID", "셰프프로필정보",
    "요리이름", "원본재료", "조리과정요약",
    "썸네일사진경로", "방송사진1경로", "방송사진2경로",
    "발행여부",
]


# ════════════════════════════════════════════════════════════════
# 1. CSV 읽기 / 쓰기
# ════════════════════════════════════════════════════════════════

def load_all_recipes(csv_path: str) -> list[dict]:
    """CSV 전체 행 반환."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save_all_recipes(csv_path: str, rows: list[dict]) -> None:
    """전체 행을 CSV 에 저장."""
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def needs_images(row: dict) -> bool:
    """이미지 컬럼 3개 중 하나라도 비어있으면 True."""
    return (
        not row.get("썸네일사진경로", "").strip() or
        not row.get("방송사진1경로",  "").strip() or
        not row.get("방송사진2경로",  "").strip()
    )


# ════════════════════════════════════════════════════════════════
# 2. 네이버 이미지 검색 API
# ════════════════════════════════════════════════════════════════

def search_naver_images(query: str, count: int = 5) -> list[str]:
    """
    네이버 이미지 검색 API 로 query 검색 후
    이미지 URL 리스트 반환 (최대 count 개).

    API 문서: https://developers.naver.com/docs/serviceapi/search/image/v1/image.md
    """
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query":  query,
        "display": count,
        "start":  1,
        "sort":   "sim",   # sim=유사도순 / date=날짜순
        "filter": "all",   # all / large / medium / small
    }

    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/image",
            headers=headers,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        urls  = [item["link"] for item in items if item.get("link")]
        print(f"[Naver] '{query}' 검색 결과: {len(urls)}개")
        return urls
    except Exception as e:
        print(f"[Naver] 검색 실패: {e}")
        return []


# ════════════════════════════════════════════════════════════════
# 3. imgBB 업로드 (URL 직접 업로드 방식)
# ════════════════════════════════════════════════════════════════

def upload_url_to_imgbb(image_url: str) -> str:
    """
    외부 이미지 URL 을 imgBB 에 업로드 후 imgBB CDN URL 반환.
    실패 시 빈 문자열 반환.

    imgBB 는 URL 직접 업로드를 지원합니다 (base64 변환 불필요).
    """
    try:
        # 먼저 이미지 다운로드
        img_resp = requests.get(image_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0"
        })
        img_resp.raise_for_status()

        # base64 인코딩 후 imgBB 업로드
        image_data = base64.b64encode(img_resp.content).decode("utf-8")

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
            print(f"[imgBB] 업로드 실패: {data.get('error', {})}")
            return ""

        url = data["data"]["url"]
        print(f"[imgBB] 업로드 완료 → {url}")
        return url

    except Exception as e:
        print(f"[imgBB] 업로드 오류: {e}")
        return ""


def upload_images_for_recipe(row: dict) -> tuple[str, str, str]:
    """
    레시피 1건에 대해 이미지 3장 검색 및 업로드.
    반환: (썸네일URL, 방송사진1URL, 방송사진2URL)

    검색 쿼리 전략:
    - 썸네일  : "{셰프이름} {요리이름} 완성" → 완성된 요리 사진
    - 방송사진: "{셰프이름} {요리이름} 냉장고를부탁해" → 방송 장면
    """
    chef  = row["셰프이름"]
    dish  = row["요리이름"]

    # ── 썸네일 검색 (완성된 요리 이미지) ─────────────────────────
    thumb_urls = search_naver_images(f"{chef} {dish} 완성", count=3)
    thumb_url  = ""
    for url in thumb_urls:
        thumb_url = upload_url_to_imgbb(url)
        if thumb_url:
            break
        time.sleep(0.5)

    # ── 방송사진 검색 (방송 장면) ──────────────────────────────────
    broadcast_urls = search_naver_images(f"{chef} {dish} 냉장고를부탁해", count=5)

    # 썸네일로 쓴 URL 제외
    broadcast_urls = [u for u in broadcast_urls if u not in thumb_urls[:1]]

    photo1_url = ""
    photo2_url = ""

    for url in broadcast_urls:
        if not photo1_url:
            photo1_url = upload_url_to_imgbb(url)
            time.sleep(0.5)
        elif not photo2_url:
            photo2_url = upload_url_to_imgbb(url)
            time.sleep(0.5)
        if photo1_url and photo2_url:
            break

    # 방송사진이 부족하면 일반 검색으로 보완
    if not photo1_url or not photo2_url:
        fallback_urls = search_naver_images(f"{chef} {dish}", count=5)
        for url in fallback_urls:
            if not photo1_url:
                photo1_url = upload_url_to_imgbb(url)
                time.sleep(0.5)
            elif not photo2_url:
                photo2_url = upload_url_to_imgbb(url)
                time.sleep(0.5)
            if photo1_url and photo2_url:
                break

    return thumb_url, photo1_url, photo2_url


# ════════════════════════════════════════════════════════════════
# 4. 메인 실행 흐름
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  냉부해 이미지 자동 수집 및 imgBB 업로드")
    print("=" * 60)

    rows        = load_all_recipes(CSV_PATH)
    target_rows = [r for r in rows if needs_images(r)]

    if not target_rows:
        print("[INFO] 이미지가 필요한 레시피가 없습니다. 모두 완료 상태입니다.")
        return

    print(f"[INFO] 이미지 수집 필요 레시피: {len(target_rows)}건\n")

    for i, row in enumerate(target_rows, 1):
        print(f"[{i}/{len(target_rows)}] {row['셰프이름']} - {row['요리이름']}")

        thumb, p1, p2 = upload_images_for_recipe(row)

        # CSV 의 해당 행에 URL 저장
        for r in rows:
            if r["요리ID"] == row["요리ID"]:
                r["썸네일사진경로"] = thumb
                r["방송사진1경로"]  = p1
                r["방송사진2경로"]  = p2
                break

        # 매 건마다 즉시 저장 (중간에 오류 나도 진행된 것 보존)
        save_all_recipes(CSV_PATH, rows)
        print(f"  ✅ 저장 완료 | 썸네일: {thumb[:40]}...")
        print()

        # API 호출 간격 (네이버 API 초당 10회 제한)
        time.sleep(1)

    print("=" * 60)
    print(f"  ✅ 전체 완료! {len(target_rows)}건 이미지 수집 및 저장")
    print("=" * 60)


if __name__ == "__main__":
    main()
