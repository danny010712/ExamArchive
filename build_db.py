"""
build_db.py
===========
ExamArchive의 files/ 폴더 안 모든 PDF에서 텍스트를 추출해서
db.json을 생성합니다.

사용법:
  1. 이 파일을 index.html과 같은 위치(ExamArchive 폴더)에 넣으세요.
  2. 터미널(PowerShell)에서 ExamArchive 폴더로 이동 후:
       pip install pymupdf --break-system-packages  (또는 pip install pymupdf)
       python build_db.py
  3. 같은 폴더에 db.json이 생성됩니다.
  4. db.json을 index.html, files/ 폴더와 함께 GitHub에 push하면 끝.

PDF 추가 시:
  새 PDF를 files/ 폴더에 넣은 후 python build_db.py 를 다시 실행하면
  db.json이 자동으로 갱신됩니다.
"""

import os
import re
import json
import fitz  # pymupdf

# ── 설정 ───────────────────────────────────────────────
FILES_DIR = "files"          # PDF가 있는 폴더 (index.html 기준)
OUTPUT    = "db.json"        # 생성할 DB 파일명
# ────────────────────────────────────────────────────────

def extract_text(pdf_path):
    """PDF 파일에서 전체 텍스트 추출"""
    try:
        doc = fitz.open(pdf_path)
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        return "\n".join(pages)
    except Exception as e:
        print(f"  ⚠ 텍스트 추출 실패: {e}")
        return ""

def split_problems(text):
    """
    문제 번호(1. / 2. / ... / 30.) 기준으로 텍스트를 문제별로 분리.
    수학 시험지 특성상 완벽하지 않을 수 있으나 유사도 검색에는 충분합니다.
    """
    # "1." "2." 등 줄 시작의 문제 번호 패턴
    pattern = re.compile(r'(?:^|\n)\s*(\d{1,2})\.\s', re.MULTILINE)
    matches = list(pattern.finditer(text))

    if not matches:
        # 문제 번호 구분이 안 되면 전체를 하나의 덩어리로
        return [{"number": None, "text": text.strip()}]

    problems = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            problems.append({"number": num, "text": content})
    return problems

def parse_path(grade, year_str, filename):
    """
    파일명에서 month와 타입(문제/해설/가형/나형 등)을 파싱.
    예) 03_문제.pdf → month="03", type="문제"
        수능_문제.pdf → month="수능", type="문제"
        06_가형_문제.pdf → month="06", type="가형_문제"
    """
    name = os.path.splitext(filename)[0]   # 확장자 제거
    parts = name.split("_", 1)             # 첫 _ 기준으로 분리
    if not parts:
        return None, None
    month = parts[0]
    doc_type = parts[1] if len(parts) > 1 else ""
    return month, doc_type

def main():
    if not os.path.isdir(FILES_DIR):
        print(f"❌ '{FILES_DIR}' 폴더를 찾을 수 없어요. "
              f"ExamArchive 폴더 안에서 실행해주세요.")
        return

    db = []
    total_pdfs = 0
    total_problems = 0

    # files/학년/연도/*.pdf 순회
    for grade in sorted(os.listdir(FILES_DIR)):
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

            # 같은 회차(월)의 파일들을 묶기 위한 딕셔너리
            month_data = {}  # month → { "problems": [...], "files": [...] }

            for filename in sorted(os.listdir(year_path)):
                if not filename.lower().endswith(".pdf"):
                    continue

                month, doc_type = parse_path(grade, year_str, filename)
                if not month:
                    continue

                # 해설 파일은 DB 검색에 필요 없으므로 건너뜀
                if "해설" in doc_type:
                    continue

                pdf_path = os.path.join(year_path, filename)
                print(f"  처리 중: {grade}/{year_str}/{filename}")

                text = extract_text(pdf_path)
                if not text.strip():
                    print(f"    → 텍스트 없음 (이미지 PDF일 수 있어요)")
                    continue

                problems = split_problems(text)
                total_pdfs += 1
                total_problems += len(problems)

                if month not in month_data:
                    month_data[month] = {
                        "grade": grade,
                        "year": year,
                        "month": month,
                        "track": "gana" if any(k in doc_type for k in ["가형","나형"]) else None,
                        "problems": []
                    }

                # 가형/나형 구분이 있으면 문제 텍스트에 표시
                prefix = ""
                if "가형" in doc_type:
                    prefix = "[가형] "
                elif "나형" in doc_type:
                    prefix = "[나형] "

                for p in problems:
                    month_data[month]["problems"].append({
                        "number": p["number"],
                        "text": prefix + p["text"]
                    })

            for entry in month_data.values():
                db.append(entry)

    # track이 None인 항목은 키 제거 (깔끔하게)
    for entry in db:
        if entry.get("track") is None:
            del entry["track"]

    # 학년 → 연도 → 월 순으로 정렬
    db.sort(key=lambda x: (x["grade"], x["year"], x["month"]))

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 완료!")
    print(f"   PDF 처리: {total_pdfs}개")
    print(f"   문제 수:  {total_problems}개")
    print(f"   저장 위치: {OUTPUT}")

if __name__ == "__main__":
    main()
