/** Pure response factories used by both the HTTP handler and unit tests. */
export function v1ErrorPayload(code, retryable, message, requestId, details) {
  const payload = { ok: false, code, retryable, message };
  if (requestId) payload.requestId = requestId;
  if (details) payload.details = details;
  return payload;
}

export function v1SuccessPayload(requestId, article) {
  return {
    ok: true,
    code: 'OK',
    retryable: false,
    requestId,
    article,
  };
}
