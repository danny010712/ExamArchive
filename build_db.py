"""
build_db.py
===========
ExamArchive의 files/ 폴더를 스캔해서 두 파일을 만듭니다.

  1) manifest.json — 학년/연도/월(/가형·나형) 목록.
     index.html이 이 파일을 fetch해서 드롭다운을 채우므로,
     더 이상 index.html 안의 DATA 배열을 손으로 고칠 필요가 없습니다.

  2) db.json — 문제별 텍스트 DB (유사도 검색용).
     PDF의 텍스트 레이어는 한글이 깨져서(HWP 변환 서브셋 폰트에 유니코드
     매핑이 없어 사설영역 코드로 나옴) 그대로 쓸 수 없습니다. 그래서 각
     페이지를 이미지로 렌더링한 뒤 Gemini Vision으로 OCR해서 텍스트를
     얻습니다. 학생이 찍은 사진도 같은 방식(OCR)으로 인식하므로, DB와
     비교 대상의 "도메인"이 일치해서 유사도 매칭 품질이 크게 좋아집니다.

사용법:
  1) pip install pymupdf
  2) Gemini API 키를 환경변수로 설정
       PowerShell : $env:GEMINI_API_KEY = "AIza..."
       bash       : export GEMINI_API_KEY="AIza..."
  3) python build_db.py

  옵션:
    --force          캐시를 무시하고 전부 다시 OCR
    --only 고1/2024  경로에 이 문자열이 포함된 PDF만 처리 (테스트용)
    --model NAME     사용할 Gemini 모델 (기본: gemini-3.5-flash-lite).
                     gemini-2.5-flash는 신규 계정에서 더 이상 사용 불가, gemini-3.5-flash /
                     gemini-3.6-flash는 계정에 따라 무료 티어 일일 한도가 20회로 매우 낮을 수
                     있음. gemini-3.5-flash-lite는 (계정 확인 결과) 일 500회로 훨씬 여유로움.
                     실제 한도는 계정마다 다를 수 있으니 https://aistudio.google.com/rate-limit
                     에서 먼저 확인해보고, --only로 소량 테스트(품질 확인) 후 본 실행 추천.
                     lite 모델은 속도/비용 대신 정확도가 살짝 낮을 수 있으니, 테스트 결과가
                     안 좋으면 결제 계정을 연결해 gemini-3.6-flash 등 상위 모델을 쓰는 것도
                     고려해보세요.
    --delay N        OCR 호출 사이 대기 시간(초), 기본 6.5 (레이트리밋 대비)

오류가 나요:
  HTTP 429 → 요청이 너무 잦거나(분당 한도) 오늘 호출 횟수를 다 썼음(일일 한도).
             무료 티어는 대략 분당 10~15회, 일 1,000~1,500회 정도입니다.
             분당 한도면 자동으로 대기 후 재시도합니다. 일일 한도면 그 자리에서
             멈추고 지금까지 처리한 내용을 저장하니, 다음 날 같은 명령으로
             다시 실행하면 캐시 덕분에 남은 파일부터 이어서 처리됩니다.
  HTTP 503 → 구글 쪽 모델이 일시적으로 과부하 상태인 것으로, 자동 재시도됩니다.
             계속 반복되면 --delay를 늘려서 다시 시도해보세요.
  키를 다른 사람(예: AI 어시스턴트, 채팅)에게 절대 붙여넣지 마세요 — 환경변수로만
  다루면 코드/대화 어디에도 남지 않습니다.

  페이지별 OCR 결과는 ocr_cache/ 에 캐시됩니다. PDF 내용이 바뀌지 않는 한
  같은 페이지를 다시 OCR하지 않으므로, 새 PDF 몇 개만 추가했을 때는
  그 파일들만 처리되어 빠르게 끝납니다. (ocr_cache/는 git에 올리지 않아도
  됩니다 — .gitignore 처리됨)
"""

import os
import re
import sys
import json
import time
import base64
import hashlib
import argparse
import urllib.request
import urllib.error
from collections import defaultdict, deque

# Windows 콘솔(cp949 등)에서 이모지 출력 시 UnicodeEncodeError로 죽는 것을 방지.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import fitz  # pymupdf

# ── 설정 ───────────────────────────────────────────────
FILES_DIR = "files"
DB_OUTPUT = "db.json"
MANIFEST_OUTPUT = "manifest.json"
CACHE_DIR = "ocr_cache"
TAG_CACHE_DIR = "tag_cache"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
# 무료 티어는 대략 분당 10~15회, 일 1,000~1,500회 한도라 여유를 두고 6.5초로 설정
# (분당 약 9회). --delay로 조절 가능.
DEFAULT_DELAY = 6.5
RENDER_ZOOM = 2.2  # 페이지 렌더링 배율 (해상도가 높을수록 OCR 품질↑, 속도↓)
TAG_BATCH_SIZE = 10  # 단원/개념 태깅 시 한 번의 Gemini 호출에 묶을 문제 수
# ────────────────────────────────────────────────────────

OCR_PROMPT = """이 이미지는 한국 수학 시험지의 한 페이지입니다.
이미지에 보이는 모든 텍스트를 위에서 아래, 왼쪽에서 오른쪽 순서 그대로
빠짐없이 추출해줘.
- 문제 번호(1. 2. 3. ...)는 반드시 원래 숫자와 마침표 형식 그대로 유지해줘.
- 수식, 분수, 지수, 로그, 그리스 문자, 특수기호도 유니코드 텍스트로 최대한
  그대로 옮겨줘.
- 보기(①②③④⑤)도 그대로 옮겨줘.
- 표/그래프의 설명이 필요하면 보이는 숫자·글자만 옮기고 그림 자체를
  묘사하지는 마.
- 설명, 주석, 마크다운 없이 추출된 텍스트만 출력해줘."""

# 문제 번호가 일치하는 DB 문제가 없을 때 "단원/개념이 비슷한 문제"를 추천하기
# 위한 태깅 프롬프트. index.html/worker에서 사진 속 문제 하나를 태깅할 때도
# 같은 지침 문구를 써야 두 쪽의 키워드 어휘가 서로 맞아떨어진다(예: 둘 다
# "이차함수"라고 부르지, 한쪽만 "이차식"이라고 부르면 매칭이 안 됨).
TAG_INSTRUCTION = (
    "이 문제가 다루는 핵심 단원/개념을 2~4개의 한글 키워드로 뽑아줘. "
    "가능하면 한국 고등학교 수학 교육과정에서 쓰는 표준 단원/개념명을 사용해줘 "
    "(예: 이차함수, 삼각함수의 그래프, 수열의 합, 미분계수, 도함수의 활용, "
    "확률의 덧셈정리, 지수함수와 로그함수, 도형의 방정식, 경우의 수 등)."
)

TAG_BATCH_HEADER = f"""아래는 한국 고등학교 수학 시험 문제 여러 개입니다. 각 문제 앞에는
[라벨 N] 형식의 식별표가 붙어 있어. 각 문제마다 {TAG_INSTRUCTION}

반드시 아래 형식으로, 문제 개수만큼 한 줄씩만 출력해줘. N은 문제 자체에 적힌 번호가
아니라 [라벨 N]에 적힌 값을 그대로 써야 해. 다른 설명은 절대 넣지 마:
N|키워드1,키워드2,키워드3

문제들:
"""


class DailyQuotaExceeded(Exception):
    """무료 티어의 일일 요청 한도를 초과했을 때. 재시도해도 소용없으므로 즉시 중단용."""


def _parse_error_body(body):
    """Gemini 오류 응답에서 메시지 / 권장 재시도 대기시간(초) / 일일한도 여부 /
    실제 quota 식별자 원문을 뽑아낸다."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:300], None, False, []

    err = data.get("error", {})
    message = err.get("message", body[:300])
    retry_delay = None
    is_daily = False
    quota_ids = []
    for d in err.get("details", []):
        t = d.get("@type", "")
        if t.endswith("RetryInfo"):
            rd = d.get("retryDelay", "")
            if rd.endswith("s"):
                try:
                    retry_delay = float(rd[:-1])
                except ValueError:
                    pass
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []):
                quota_id = v.get("quotaId", "") or v.get("quotaMetric", "")
                quota_ids.append(quota_id)
                if "perday" in quota_id.lower().replace("_", "").replace("-", ""):
                    is_daily = True
    return message, retry_delay, is_daily, quota_ids


def _call_gemini(parts, api_key, model, retries=5, max_output_tokens=4096):
    """parts(콘텐츠 파트 리스트)를 Gemini generateContent에 보내고 첫 후보의 텍스트를
    반환한다. 429(레이트리밋/일일한도)·5xx 재시도 로직은 OCR과 태깅 호출이 공유한다."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = json.dumps(
        {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_output_tokens},
        }
    ).encode("utf-8")

    last_message = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if not candidates:
                    return ""
                cparts = candidates[0].get("content", {}).get("parts", [])
                return cparts[0].get("text", "") if cparts else ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            message, retry_delay, is_daily, quota_ids = _parse_error_body(body)
            quota_note = f" [quota: {', '.join(quota_ids)}]" if quota_ids else ""
            last_message = f"HTTP {e.code}: {message}{quota_note}"

            if e.code == 429 and is_daily:
                # 일일 무료 한도 초과 — 재시도해도 그날은 계속 실패하므로 바로 중단.
                raise DailyQuotaExceeded(f"{message}{quota_note}")

            if e.code == 429 or e.code >= 500:
                wait = retry_delay if retry_delay else min(60, (2**attempt) * 3)
                print(f"    ⚠ {last_message} — {wait:.0f}초 대기 후 재시도... ({attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            raise RuntimeError(last_message)
        except urllib.error.URLError as e:
            last_message = f"네트워크 오류: {e}"
            wait = min(30, (2**attempt) * 3)
            print(f"    ⚠ {last_message}, {wait}초 대기 후 재시도... ({attempt + 1}/{retries})")
            time.sleep(wait)
    raise RuntimeError(f"재시도 한도를 초과했어요. 마지막 오류: {last_message}")


def call_gemini_ocr(image_bytes, api_key, model, retries=5):
    b64 = base64.b64encode(image_bytes).decode("ascii")
    parts = [
        {"inline_data": {"mime_type": "image/png", "data": b64}},
        {"text": OCR_PROMPT},
    ]
    return _call_gemini(parts, api_key, model, retries=retries, max_output_tokens=4096)


def call_gemini_text(prompt, api_key, model, retries=5):
    return _call_gemini([{"text": prompt}], api_key, model, retries=retries, max_output_tokens=2048)


def render_page_png(page, zoom=RENDER_ZOOM):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return pix.tobytes("png")


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_path(pdf_hash, page_index, model):
    key = f"{pdf_hash}_{page_index}_{model}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return os.path.join(CACHE_DIR, digest[:2], digest + ".txt")


def ocr_pdf(pdf_path, api_key, model, delay, force, stats):
    doc = fitz.open(pdf_path)
    pdf_h = file_hash(pdf_path)
    texts = []
    for i, page in enumerate(doc):
        cpath = cache_path(pdf_h, i, model)
        if not force and os.path.exists(cpath):
            with open(cpath, "r", encoding="utf-8") as f:
                texts.append(f.read())
            stats["cached_pages"] += 1
            continue
        png = render_page_png(page)
        text = call_gemini_ocr(png, api_key, model)
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        with open(cpath, "w", encoding="utf-8") as f:
            f.write(text)
        texts.append(text)
        stats["ocr_pages"] += 1
        time.sleep(delay)
    doc.close()
    return "\n".join(texts)


def split_problems(text):
    """
    문제 번호(1. / 2. / ... / 30.) 기준으로 텍스트를 문제별로 분리.
    """
    pattern = re.compile(r"(?:^|\n)\s*(\d{1,2})\.\s", re.MULTILINE)
    matches = list(pattern.finditer(text))

    if not matches:
        return [{"number": None, "text": text.strip()}]

    problems = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            problems.append({"number": num, "text": content})
    return problems


def find_vertical_dividers(page):
    """
    페이지에 그려진 세로 구분선(2단 편집의 컬럼 경계선)을 찾는다.
    페이지 높이의 60% 이상을 가로지르는 얇은 수직 요소만 인정해서 표/그래프
    테두리 같은 짧은 선은 걸러낸다. 구분선은 "l"(선) 또는 아주 얇은 "re"
    (채워진 사각형)로 그려질 수 있어 둘 다 검사한다.
    """
    h = page.rect.height
    dividers = []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.x - p2.x) < 1.0 and abs(p1.y - p2.y) > 0.6 * h:
                    x = (p1.x + p2.x) / 2
                    y0, y1 = sorted([p1.y, p2.y])
                    dividers.append((x, y0, y1))
            elif item[0] == "re":
                r = item[1]
                width, height = r.x1 - r.x0, r.y1 - r.y0
                if width < 2.0 and height > 0.6 * h:
                    x = (r.x0 + r.x1) / 2
                    dividers.append((x, r.y0, r.y1))
    dividers.sort(key=lambda d: d[0])
    return dividers


def find_problem_rects(pdf_path):
    """
    PDF의 텍스트 레이어에서 한글은 깨져도 "1." "2." 같은 문제 번호(숫자+마침표)와
    그 좌표는 멀쩡하게 남아있다. 이를 이용해 각 문제가 차지하는 영역(페이지 번호 +
    사각형 좌표)을 찾아낸다. OCR 없이 PyMuPDF만으로 동작하므로 빠르고 API 호출이
    필요 없다.
    """
    doc = fitz.open(pdf_path)
    results = []
    marker_re = re.compile(r"^\d{1,2}\.$")

    for page_index, page in enumerate(doc):
        words = page.get_text("words")
        pr = page.rect
        dividers = find_vertical_dividers(page)

        # 콘텐츠(텍스트/도형/이미지) 바운딩박스: 다음 문제 시작 전까지의 "안전한"
        # 영역 안에서 실제로 내용이 차지하는 만큼만 잘라내기 위한 타이트닝용.
        content_boxes = [(w[0], w[1], w[2], w[3]) for w in words]
        for d in page.get_drawings():
            r = d.get("rect")
            if r is not None:
                content_boxes.append((r.x0, r.y0, r.x1, r.y1))
        for img in page.get_image_info():
            b = img.get("bbox")
            if b:
                content_boxes.append((b[0], b[1], b[2], b[3]))

        markers = []
        for w in words:
            x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
            if marker_re.match(text):
                num = int(text[:-1])
                if 1 <= num <= 30:
                    markers.append({"num": num, "x0": x0, "y0": y0})

        if not markers:
            continue

        if dividers:
            top_bound = min(d[1] for d in dividers)
            bottom_bound = max(d[2] for d in dividers)
            band_edges = [pr.x0] + [d[0] for d in dividers] + [pr.x1]
        else:
            top_bound = pr.y0
            bottom_bound = pr.height - 30
            band_edges = [pr.x0, pr.x1]

        def band_of(x0):
            for i in range(len(band_edges) - 1):
                if band_edges[i] <= x0 < band_edges[i + 1]:
                    return i
            return len(band_edges) - 2

        bands = {}
        for m in markers:
            bi = band_of(m["x0"])
            bands.setdefault(bi, []).append(m)

        pad = 6
        for bi in sorted(bands.keys()):
            col = sorted(bands[bi], key=lambda m: m["y0"])
            x_left = band_edges[bi] + pad
            x_right = band_edges[bi + 1] - pad
            for i, m in enumerate(col):
                y_top = max(top_bound, m["y0"] - pad)
                # coarse_bottom: 다음 문제 시작 전(또는 컬럼 끝)까지의 "안전한" 하한선.
                # 실제 내용은 보통 이보다 훨씬 위에서 끝나므로, 아래에서 콘텐츠
                # 바운딩박스로 다시 타이트하게 줄인다.
                coarse_bottom = col[i + 1]["y0"] - pad if i + 1 < len(col) else bottom_bound
                band_h = max(1.0, coarse_bottom - y_top)

                # 이 문제 영역(x_left~x_right, y_top~coarse_bottom) 안의 콘텐츠 박스를
                # 위에서 아래로 훑으면서, 문제 본문과 큰 간격(GAP_THRESHOLD)을 두고
                # 뚝 떨어진 콘텐츠(페이지 하단 쪽수/저작권 표기 등 30번처럼 컬럼의
                # 마지막 문제일 때 특히 자주 걸림)는 무시한다. 단순히 영역 안 최댓값을
                # 쓰면 이런 아티팩트까지 포함돼 아래 여백이 커지는 문제가 있었음.
                GAP_THRESHOLD = 45
                boxes_in_region = []
                for bx0, by0, bx1, by1 in content_boxes:
                    if bx1 <= x_left or bx0 >= x_right:
                        continue
                    if by0 < y_top - 2 or by0 > coarse_bottom:
                        continue
                    if (by1 - by0) > 0.8 * band_h:
                        continue  # 세로 구분선 등 컬럼 전체 높이짜리 아티팩트 제외
                    boxes_in_region.append((by0, by1))
                boxes_in_region.sort(key=lambda b: b[0])

                tight_bottom = y_top
                for by0, by1 in boxes_in_region:
                    if by0 - tight_bottom > GAP_THRESHOLD and tight_bottom > y_top:
                        break  # 실제 내용과 크게 떨어진 콘텐츠는 여기서 끊는다
                    tight_bottom = max(tight_bottom, by1)

                y_bottom = min(coarse_bottom, tight_bottom + 12)
                y_bottom = max(y_bottom, y_top + 20)  # 최소 높이 안전장치

                results.append({
                    "number": m["num"],
                    "page": page_index,
                    "rect": [round(x_left, 1), round(y_top, 1), round(x_right, 1), round(y_bottom, 1)],
                })

    doc.close()
    return results


def tag_cache_path(pdf_hash, index, model):
    key = f"tags_{pdf_hash}_{index}_{model}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return os.path.join(TAG_CACHE_DIR, digest[:2], digest + ".json")


def _parse_tag_batch_response(text):
    """"라벨N|키워드1,키워드2" 형식의 응답을 {라벨N: [키워드,...]} 로 파싱.
    Gemini가 지시한 형식을 정확히 안 지키고 "[라벨 3] 키워드..." 처럼 프롬프트의
    [라벨 N] 표기를 그대로 따라 쓰거나, 구분자로 |/:/./) 등을 섞어 쓰는 경우가
    있어서 최대한 관대하게 파싱한다."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 선행 "[", "라벨" 같은 장식 제거: "[라벨 3]", "라벨 3", "3" 모두 허용
        line = re.sub(r"^\[?\s*라벨\s*", "", line)
        m = re.match(r"^\[?\s*(\d+)\s*\]?\s*[|:.\-)]?\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        tags = [t.strip(" -") for t in re.split(r"[,，、]", m.group(2)) if t.strip(" -")]
        if tags:
            result[idx] = tags
    return result


def classify_topics(problems, pdf_hash, api_key, model, delay, force, stats, debug=False):
    """문제별 단원/개념 키워드를 뽑아 {problems 리스트 안의 위치: [키워드,...]} 로 반환.
    여러 문제를 TAG_BATCH_SIZE개씩 묶어 한 번의 Gemini 호출로 처리해서
    (문제당 1회 호출이면 3512번이 되어버리므로) 호출 횟수를 크게 줄인다.
    결과는 문제 단위로 캐싱되어, 재실행 시 이미 태깅된 문제는 다시 호출하지 않는다.

    캐시/배치 프롬프트의 식별자로 "문제 번호"가 아니라 problems 리스트 안의
    위치(인덱스)를 쓴다 — 수능처럼 확통/미적/기하 선택과목별로 같은 번호(예: 23번)가
    한 PDF 안에 여러 번 나오는 경우, 번호로 캐시하면 서로 다른 문제끼리 같은 캐시
    파일을 덮어써서 태그가 뒤섞이는 문제가 있었다.
    """
    tags_by_index = {}
    todo = []  # (index, problem_dict, cache_path)

    for idx, p in enumerate(problems):
        cpath = tag_cache_path(pdf_hash, idx, model)
        if not force and os.path.exists(cpath):
            with open(cpath, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                # 태깅에 성공한 것만 캐시 히트로 인정. 예전에 응답 파싱이 실패해서
                # 빈 리스트로 저장된 것은 여기서 다시 todo에 넣어 재시도한다 —
                # 그래야 --force 없이 재실행만 해도 실패했던 문제들이 복구된다.
                tags_by_index[idx] = cached
                stats["tags_cached"] += 1
                continue
        todo.append((idx, p, cpath))

    for i in range(0, len(todo), TAG_BATCH_SIZE):
        batch = todo[i:i + TAG_BATCH_SIZE]
        prompt = TAG_BATCH_HEADER + "\n".join(
            f"[라벨 {idx}]\n{p['text'][:400]}" for idx, p, _ in batch
        )
        text = call_gemini_text(prompt, api_key, model)
        if debug:
            print("    ── 태깅 응답 원문 ──")
            print(text)
            print("    ──────────────────")
        parsed = _parse_tag_batch_response(text)
        for idx, p, cpath in batch:
            tags = parsed.get(idx, [])
            if debug and not tags:
                print(f"    ⚠ 라벨 {idx} 파싱 실패 (문제: {p['text'][:60]!r})")
            os.makedirs(os.path.dirname(cpath), exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(tags, f, ensure_ascii=False)
            tags_by_index[idx] = tags
            stats["tags_new"] += 1
        time.sleep(delay)

    return tags_by_index


def parse_path(filename):
    """
    파일명에서 month와 타입(문제/해설/가형/나형 등)을 파싱.
    예) 03_문제.pdf → month="03", type="문제"
        수능_문제.pdf → month="수능", type="문제"
        06_가형_문제.pdf → month="06", type="가형_문제"
    """
    name = os.path.splitext(filename)[0]
    parts = name.split("_", 1)
    if not parts:
        return None, None
    month = parts[0]
    doc_type = parts[1] if len(parts) > 1 else ""
    return month, doc_type


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 전부 다시 OCR")
    ap.add_argument("--only", default=None, help="경로에 이 문자열이 포함된 PDF만 처리")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    ap.add_argument("--skip-tags", action="store_true",
                     help="단원/개념 태깅(유사 문제 없을 때 추천용)을 건너뜀")
    ap.add_argument("--debug-tags", action="store_true",
                     help="태깅 응답 원문과 파싱 실패 항목을 출력 (문제 진단용)")
    args = ap.parse_args()

    if not os.path.isdir(FILES_DIR):
        print(f"❌ '{FILES_DIR}' 폴더를 찾을 수 없어요. ExamArchive 폴더 안에서 실행해주세요.")
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY 환경변수가 설정되어 있지 않아요.")
        print('   PowerShell: $env:GEMINI_API_KEY = "AIza..."')
        return

    db = []
    stats = {"ocr_pages": 0, "cached_pages": 0, "pdfs": 0, "problems": 0,
              "tags_new": 0, "tags_cached": 0}
    failed_files = []
    quota_exhausted = False

    for grade in sorted(os.listdir(FILES_DIR)):
        if quota_exhausted:
            break
        grade_path = os.path.join(FILES_DIR, grade)
        if not os.path.isdir(grade_path):
            continue

        for year_str in sorted(os.listdir(grade_path)):
            year_path = os.path.join(grade_path, year_str)
            if not os.path.isdir(year_path):
                continue
            try:
                year = int(year_str)
            except ValueError:
                continue
            if quota_exhausted:
                break

            month_data = {}

            for filename in sorted(os.listdir(year_path)):
                if not filename.lower().endswith(".pdf"):
                    continue

                month, doc_type = parse_path(filename)
                if not month:
                    continue

                pdf_path = os.path.join(year_path, filename)
                rel = f"{grade}/{year_str}/{filename}"
                if args.only and args.only not in rel:
                    continue

                # month_data는 문제/해설 상관없이 "이 회차가 존재한다"는 것과
                # track(가형/나형) 여부를 파악하는 데도 쓰이므로 먼저 등록.
                if month not in month_data:
                    month_data[month] = {
                        "grade": grade,
                        "year": year,
                        "month": month,
                        "track": "gana"
                        if any(k in doc_type for k in ["가형", "나형"])
                        else None,
                        "problems": [],
                    }
                elif any(k in doc_type for k in ["가형", "나형"]):
                    month_data[month]["track"] = "gana"

                # 해설 파일은 DB 검색(문제 텍스트)에는 필요 없으므로 OCR 스킵.
                if "해설" in doc_type:
                    continue

                print(f"  처리 중: {rel}")
                try:
                    text = ocr_pdf(pdf_path, api_key, args.model, args.delay, args.force, stats)
                except DailyQuotaExceeded as e:
                    print(f"    ❌ 일일 무료 할당량을 초과했어요: {e}")
                    print("    → 지금까지 처리한 내용은 저장하고 멈춥니다. 내일(태평양시 자정 기준) "
                          "같은 명령으로 다시 실행하면 캐시 덕분에 이어서 처리돼요.")
                    quota_exhausted = True
                    break
                except Exception as e:
                    print(f"    ❌ OCR 실패, 이 파일은 건너뜁니다: {e}")
                    failed_files.append(rel)
                    continue

                if not text.strip():
                    print("    → 텍스트 없음 (OCR 실패했거나 빈 페이지일 수 있어요)")
                    continue

                problems = split_problems(text)
                stats["pdfs"] += 1
                stats["problems"] += len(problems)

                prefix = ""
                if "가형" in doc_type:
                    prefix = "[가형] "
                elif "나형" in doc_type:
                    prefix = "[나형] "

                # 문제 번호(크롭 좌표)는 OCR 텍스트가 아니라 PDF 자체의 위치 정보에서
                # 뽑으므로 API 호출 없이 빠르게 계산된다. Gemini는 페이지를 항상
                # "왼쪽 컬럼 전체 → 오른쪽 컬럼" 순서로 읽지 않아서(가로 줄 단위로 읽을
                # 때가 있음) 순서가 아니라 문제 번호 값으로 매칭한다. 수능처럼 같은
                # 번호가 여러 번(확통/미적/기하 등) 나오는 경우 페이지 등장 순서를
                # 그대로 큐로 소비해 매칭한다.
                rect_queues = defaultdict(deque)
                for r in find_problem_rects(pdf_path):
                    rect_queues[r["number"]].append(r)

                # 단원/개념 태깅: DB에 텍스트로 매칭되는 문제가 없을 때 "비슷한
                # 단원의 문제"를 추천하기 위한 사전 준비. 문제별로 캐싱되므로
                # 재실행 시 이미 태깅된 문제는 API를 다시 호출하지 않는다.
                tags_by_index = {}
                if not args.skip_tags:
                    try:
                        pdf_h = file_hash(pdf_path)
                        tags_by_index = classify_topics(
                            problems, pdf_h, api_key, args.model, args.delay, args.force, stats,
                            debug=args.debug_tags,
                        )
                    except DailyQuotaExceeded as e:
                        print(f"    ❌ 일일 무료 할당량을 초과했어요(태깅): {e}")
                        print("    → 지금까지 처리한 내용은 저장하고 멈춥니다. 내일 같은 명령으로 "
                              "다시 실행하면 캐시 덕분에 이어서 처리돼요.")
                        quota_exhausted = True
                    except Exception as e:
                        print(f"    ⚠ 태깅 실패(이 파일은 태그 없이 진행): {e}")

                for idx, p in enumerate(problems):
                    entry = {"number": p["number"], "text": prefix + p["text"]}
                    q = rect_queues.get(p["number"])
                    if q:
                        r = q.popleft()
                        entry["page"] = r["page"]
                        entry["rect"] = r["rect"]
                        entry["docType"] = doc_type
                    tags = tags_by_index.get(idx)
                    if tags:
                        entry["tags"] = tags
                    month_data[month]["problems"].append(entry)

                if quota_exhausted:
                    break

            for entry in month_data.values():
                db.append(entry)
            if quota_exhausted:
                break

    for entry in db:
        if entry.get("track") is None:
            del entry["track"]

    db.sort(key=lambda x: (x["grade"], x["year"], x["month"]))

    with open(DB_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    manifest = [
        {k: v for k, v in entry.items() if k != "problems"} for entry in db
    ]
    with open(MANIFEST_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n✅ 저장 완료!" if not quota_exhausted else "\n⏸ 일일 한도로 중단됨 (지금까지 내용은 저장됨)")
    print(f"   PDF 처리        : {stats['pdfs']}개")
    print(f"   문제 수         : {stats['problems']}개")
    print(f"   OCR 새로 호출   : {stats['ocr_pages']}페이지")
    print(f"   캐시 재사용     : {stats['cached_pages']}페이지")
    print(f"   태그 새로 호출  : {stats['tags_new']}문제")
    print(f"   태그 캐시 재사용: {stats['tags_cached']}문제")
    print(f"   회차(manifest)  : {len(manifest)}개")
    print(f"   저장 위치       : {DB_OUTPUT}, {MANIFEST_OUTPUT}")
    if failed_files:
        print(f"\n⚠ OCR에 실패해서 건너뛴 파일 {len(failed_files)}개:")
        for f in failed_files:
            print(f"   - {f}")
        print("   → 원인을 해결한 뒤 예: python build_db.py --only \"고1/2024\" 처럼 다시 실행해보세요.")
    if quota_exhausted:
        print("\n   내일 다시 python build_db.py 를 실행하면 남은 파일부터 이어서 처리해요.")


if __name__ == "__main__":
    main()
