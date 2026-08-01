/**
 * Thin fetch() wrapper for the photo-service API + unified error parsing.
 *
 * TASK-003 B3/B4 (tasks/TASK-003/20_design.md §8.3-8.4): every non-2xx
 * response is turned into an `ApiError` that carries the unified
 * `{error_code, message, request_id}` body (spec.md §2), mapped to a
 * human-readable Russian message - screens must never show a raw
 * "Failed to fetch".
 */

const ERROR_MESSAGES = {
  INVALID_FILE: "Файл пустой или повреждён",
  PAYLOAD_TOO_LARGE: "Файл больше 50 МБ",
  UNSUPPORTED_MEDIA_TYPE: "Поддерживаются только JPEG и PNG",
  INVALID_BATCH_SIZE: "В батче должно быть от 2 до 10 файлов",
  NOT_FOUND: "Не найдено",
  SERVICE_UNAVAILABLE: "Сервис временно недоступен, попробуйте позже",
  INTERNAL_ERROR: "Внутренняя ошибка сервиса",
};

class ApiError extends Error {
  constructor(errorCode, message, requestId, status) {
    super(message);
    this.name = "ApiError";
    this.errorCode = errorCode;
    this.requestId = requestId;
    this.status = status;
  }
}

function apiBaseUrl() {
  return (window.APP_CONFIG && window.APP_CONFIG.apiBaseUrl) || "";
}

async function parseErrorBody(response) {
  try {
    const body = await response.json();
    const errorCode = body.error_code || "INTERNAL_ERROR";
    // Unknown error_code falls back to the server-provided message (design
    // §8.4: "Неизвестный error_code -> показываем message от сервера").
    const message = ERROR_MESSAGES[errorCode] || body.message || "Неизвестная ошибка";
    return new ApiError(errorCode, message, body.request_id || null, response.status);
  } catch {
    // Response body wasn't valid JSON at all (e.g. an intermediary proxy's
    // own error page) - still surface something useful rather than a raw
    // JSON-parse exception.
    return new ApiError(
      "INTERNAL_ERROR",
      "Сервис вернул неожиданный ответ",
      null,
      response.status
    );
  }
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, options);
  } catch {
    // fetch() rejects with a bare TypeError on a network failure or a CORS
    // rejection - this is exactly the case the design forbids showing
    // verbatim ("Failed to fetch") in favor of an actionable message.
    throw new ApiError(
      "NETWORK_ERROR",
      "Не удалось связаться с сервисом. Проверьте, что API запущен и доступен.",
      null,
      0
    );
  }

  if (!response.ok) {
    throw await parseErrorBody(response);
  }
  return response;
}

window.ApiError = ApiError;
window.api = {
  async listPhotos(limit, offset) {
    const response = await request(`/v1/photos?limit=${limit}&offset=${offset}`);
    return response.json();
  },

  async getPhoto(photoId) {
    const response = await request(`/v1/photos/${encodeURIComponent(photoId)}`);
    return response.json();
  },

  photoContentUrl(photoId) {
    return `${apiBaseUrl()}/v1/photos/${encodeURIComponent(photoId)}/content`;
  },

  async uploadPhoto(file) {
    const formData = new FormData();
    formData.append("file", file);
    const response = await request("/v1/photos", { method: "POST", body: formData });
    return response.json();
  },

  async uploadBatch(files) {
    // design §8.3: the batch field is named `file`, REPEATED per file - NOT
    // `files` (see app/api/batches.py: `file: list[UploadFile] = File(...)`).
    const formData = new FormData();
    for (const file of files) {
      formData.append("file", file);
    }
    const response = await request("/v1/photos/batch", { method: "POST", body: formData });
    return response.json();
  },

  async getBatch(batchId) {
    const response = await request(`/v1/batches/${encodeURIComponent(batchId)}`);
    return response.json();
  },
};

/** Human-readable one-liner for any error, used by every screen. */
window.describeApiError = function describeApiError(err) {
  if (err instanceof ApiError) {
    return err.requestId ? `${err.message} (request_id: ${err.requestId})` : err.message;
  }
  return "Неизвестная ошибка";
};
