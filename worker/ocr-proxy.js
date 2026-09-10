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

    const { image, mimeType } = body || {};
    if (!image || !mimeType) {
      return jsonResponse({ error: "image, mimeType 값이 필요해요." }, 400, env);
    }

    const model = env.GEMINI_MODEL || "gemini-3.5-flash-lite";
    const upstreamUrl =
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent` +
      `?key=${env.GEMINI_API_KEY}`;

    let upstream;
    try {
      upstream = await fetch(upstreamUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [
            {
              parts: [
                { inline_data: { mime_type: mimeType, data: image } },
                { text: OCR_PROMPT },
              ],
            },
          ],
          generationConfig: { temperature: 0, maxOutputTokens: 1024 },
        }),
      });
    } catch (err) {
      return jsonResponse({ error: "Gemini 호출 실패: " + err.message }, 502, env);
    }

    const data = await upstream.json();
    if (data.error) {
      return jsonResponse({ error: data.error.message }, 502, env);
    }
    const text = data.candidates?.[0]?.content?.parts?.[0]?.text || "";
    return jsonResponse({ text }, 200, env);
  },
};
