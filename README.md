Go to : https://danny010712.github.io/ExamArchive/

## 새 회차(PDF) 추가하기

1. `files/학년/연도/` 폴더에 `월_문제.pdf`, `월_해설.pdf`를 올린다. (수능은 `수능_문제.pdf`, 가형/나형은 `06_가형_문제.pdf`처럼 파일명에 그대로 포함)
2. Gemini API 키를 환경변수로 설정하고 빌드 스크립트 실행:
   ```
   pip install pymupdf
   $env:GEMINI_API_KEY = "AIza..."   # PowerShell
   python build_db.py
   ```
   → `manifest.json`(드롭다운 목록), `db.json`(문제 텍스트 DB)이 자동 생성/갱신됨.
   기존에 처리한 페이지는 `ocr_cache/`에 캐시되어 있어서 새로 추가한 파일만 OCR한다.
3. `files/`, `manifest.json`, `db.json`을 함께 GitHub에 push하면 끝. index.html은 더 이상 손댈 필요 없음.

## 사진 인식(OCR) 프록시 배포 — 최초 1회만

학생들이 각자 Gemini API 키를 입력하지 않고 사진 인식을 쓸 수 있도록, `worker/` 폴더의
Cloudflare Worker가 OCR 요청을 대신 처리한다. 키는 워커 secret으로만 저장되고 절대
브라우저/저장소에 노출되지 않는다.

```
cd worker
npm install -g wrangler   # 최초 1회
wrangler login
wrangler secret put GEMINI_API_KEY   # 본인 Gemini API 키 입력
wrangler deploy
```

배포 후 나온 URL(예: `https://examarchive-ocr-proxy.<subdomain>.workers.dev`)을
`index.html`의 `OCR_PROXY_URL` 상수에 넣고 다시 push한다. `worker/wrangler.toml`의
`ALLOWED_ORIGIN`이 실제 GitHub Pages 주소와 일치하는지도 확인.
