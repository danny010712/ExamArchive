/**
 * ExamArchive OCR 프록시 (Cloudflare Worker)
 * ==========================================
 * 학생들이 각자 Gemini API 키를 입력하지 않도록, 이 워커가 대신
 * Gemini Vision API를 호출합니다. 실제 API 키는 워커의 secret으로만
 * 저장되고 클라이언트(index.html)에는 절대 노출되지 않습니다.
 *
 * 배포 방법은 저장소 루트의 README.md를 참고하세요.
 */

const OCR_PROMPT = `이 이미지에서 보이는 모든 텍스트를 그대로 추출해줘.
수식, 숫자, 한글, 영어 모두 포함해서 이미지에 있는 텍스트를 빠짐없이 출력해줘.
설명이나 주석 없이 추출된 텍스트만 출력해줘.`;

// build_db.py의 TAG_INSTRUCTION과 문구를 맞춰야 DB에 미리 뽑아둔 키워드와
// 여기서 사진 속 문제로 뽑는 키워드의 어휘가 서로 맞아떨어진다.
const CLASSIFY_PROMPT_PREFIX = `이 문제가 다루는 핵심 단원/개념을 2~4개의 한글 키워드로 뽑아줘.
가능하면 한국 고등학교 수학 교육과정에서 쓰는 표준 단원/개념명을 사용해줘
(예: 이차함수, 삼각함수의 그래프, 수열의 합, 미분계수, 도함수의 활용,
확률의 덧셈정리, 지수함수와 로그함수, 도형의 방정식, 경우의 수 등).
설명 없이 키워드만, 쉼표로 구분해서 한 줄로 출력해줘.

문제:
`;

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function jsonResponse(obj, status, env) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders(env) },
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Gemini가 429(레이트리밋)나 503(모델 과부하)을 반환하면 대부분 몇 초 안에
// 풀리는 일시적 현상이므로, 학생에게 바로 에러를 보여주기 전에 짧게 재시도한다.
async function callGeminiWithRetry(upstreamUrl, payload, retries = 3) {
  let lastMessage = "알 수 없는 오류";
  for (let attempt = 0; attempt < retries; attempt++) {
    let upstream;
    try {
      upstream = await fetch(upstreamUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
      });
    } catch (err) {
      lastMessage = "Gemini 호출 실패: " + err.message;
      if (attempt < retries - 1) {
        await sleep(500 * 2 ** attempt);
        continue;
      }
      return { ok: false, message: lastMessage };
    }

    const data = await upstream.json();
    if (!upstream.ok || data.error) {
      lastMessage = data.error?.message || `HTTP ${upstream.status}`;
      const retryable = upstream.status === 429 || upstream.status >= 500;
      if (retryable && attempt < retries - 1) {
        await sleep(500 * 2 ** attempt);
        continue;
      }
      return { ok: false, message: lastMessage };
    }
    return { ok: true, data };
  }
  return { ok: false, message: lastMessage };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(env) });
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "POST만 지원해요." }, 405, env);
    }
    if (!env.GEMINI_API_KEY) {
      return jsonResponse({ error: "서버에 GEMINI_API_KEY가 설정되어 있지 않아요." }, 500, env);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "잘못된 요청 형식이에요." }, 400, env);
    }

    const model = env.GEMINI_MODEL || "gemini-3.5-flash-lite";
    const upstreamUrl =
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent` +
      `?key=${env.GEMINI_API_KEY}`;

    if (body.mode === "classify") {
      // DB에 텍스트로 일치하는 문제가 없을 때, 사진에서 뽑은 문제 텍스트의
      // 단원/개념 키워드를 뽑아 db.json에 미리 저장된 키워드와 비교하기 위함.
      const { text: problemText } = body || {};
      if (!problemText || !problemText.trim()) {
        return jsonResponse({ error: "text 값이 필요해요." }, 400, env);
      }
      const payload = JSON.stringify({
        contents: [{ parts: [{ text: CLASSIFY_PROMPT_PREFIX + problemText.slice(0, 1500) }] }],
        generationConfig: { temperature: 0, maxOutputTokens: 200 },
      });
      const result = await callGeminiWithRetry(upstreamUrl, payload);
      if (!result.ok) {
        return jsonResponse({ error: result.message }, 502, env);
      }
      const raw = result.data.candidates?.[0]?.content?.parts?.[0]?.text || "";
      const tags = raw.split(/[,\n，]/).map((t) => t.trim()).filter(Boolean);
      return jsonResponse({ tags }, 200, env);
    }

    const { image, mimeType } = body || {};
    if (!image || !mimeType) {
      return jsonResponse({ error: "image, mimeType 값이 필요해요." }, 400, env);
    }

    const payload = JSON.stringify({
      contents: [
        {
          parts: [
            { inline_data: { mime_type: mimeType, data: image } },
            { text: OCR_PROMPT },
          ],
        },
      ],
      generationConfig: { temperature: 0, maxOutputTokens: 1024 },
    });

    const result = await callGeminiWithRetry(upstreamUrl, payload);
    if (!result.ok) {
      return jsonResponse({ error: result.message }, 502, env);
    }
    const text = result.data.candidates?.[0]?.content?.parts?.[0]?.text || "";
    return jsonResponse({ text }, 200, env);
  },
};
