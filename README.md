# 냉부해 레시피 네이버 블로그 자동 발행 v3

## 설치

```bash
pip install openai requests python-dotenv
```

---

## 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 열어 값을 채워넣기
```

| 변수 | 설명 |
|------|------|
| `NAVER_BLOG_ID` | 블로그 URL 영문 ID (`xxx.blog.naver.com` → `xxx`) |
| `NAVER_COOKIE` | 크롬에서 추출한 로그인 쿠키 전체 (큰따옴표 필수) |
| `IMGBB_API_KEY` | imgBB 앱 등록 후 발급된 API Key |
| `OPENAI_API_KEY` | OpenAI API Key |
| `CSV_PATH` | recipes.csv 경로 (기본값: `recipes.csv`) |

---

## 네이버 쿠키 추출 방법

1. 크롬에서 **blog.naver.com** 접속 후 로그인 (2단계 인증까지)
2. `F12` → **Network** 탭
3. 블로그 페이지에서 아무 링크나 클릭
4. Network 목록에서 `blog.naver.com` 요청 클릭
5. **Request Headers** → `Cookie:` 값 전체 복사
6. `.env` 의 `NAVER_COOKIE` 에 큰따옴표로 감싸서 붙여넣기

```env
NAVER_COOKIE="BA_DEVICE=xxxx; NNB=xxxx; NID_AUT=xxxx; NID_SES=xxxx; ..."
```

> 쿠키는 수 시간~수일 유효. 만료 시 프로그램이 재추출 안내를 출력합니다.

---

## imgBB API Key 발급 방법

1. **https://imgbb.com** 접속 → 회원가입 또는 로그인
2. **https://api.imgbb.com** 접속
3. **Get API key** 클릭
4. 발급된 Key 를 `.env` 의 `IMGBB_API_KEY` 에 입력

---

## CSV 파일 구조 (recipes.csv)

| 컬럼 | 설명 |
|------|------|
| 요리ID | 고유 식별자 |
| 셰프이름 | 셰프 한글 이름 |
| 셰프인스타ID | 인스타그램 ID (@ 제외) |
| 셰프프로필정보 | 예: "이탈리안 요리 장인" |
| 요리이름 | 요리 명칭 |
| 원본재료 | 재료 목록 |
| 조리과정요약 | 핵심 단계 요약 |
| 썸네일사진경로 | 로컬 PC 경로 |
| 방송사진1경로 | 로컬 PC 경로 |
| 방송사진2경로 | 로컬 PC 경로 |
| 발행여부 | N = 미발행 / Y = 발행완료 |

---

## 실행

```bash
python main.py
```

1회 실행 시 `발행여부=N` 인 **첫 번째 행 1건**만 처리합니다.

---

## 발행 실패 시 대처법

### 쿠키 만료
```
❌ 네이버 쿠키가 만료되었습니다.
```
→ 크롬 재로그인 후 쿠키 재추출 → `.env` 업데이트 후 재실행

### API 엔드포인트 변경
1. 크롬에서 블로그 글쓰기 페이지 접속
2. `F12` → Network → **Fetch/XHR** 필터
3. 글 발행 버튼 클릭 후 `PostSave` 또는 `posts` 관련 요청 확인
4. URL 과 Payload 구조를 `main.py` 의 `_try_method_a()` / `_try_method_b()` 에 반영

---

## .gitignore 필수 추가

```
.env
*.csv
```
