/**
 * Photo detail screen (photo.html?id=...): full analysis breakdown +
 * polling while the photo has not reached a terminal status.
 */

const POLL_MAX_ITERATIONS = 100;

let pollTimer = null;
let pollIterations = 0;

function currentPhotoId() {
  return new URLSearchParams(window.location.search).get("id");
}

function pollIntervalMs() {
  return (window.APP_CONFIG && window.APP_CONFIG.pollIntervalMs) || 3000;
}

function renderAnalysis(analysis) {
  if (!analysis) {
    return '<p class="muted">Анализ ещё не готов.</p>';
  }
  const verdict = window.format.sharpnessVerdict(analysis.blur_score);
  const eyesClosed = analysis.eyes_closed_count ?? "нет данных";
  return `
    <dl class="analysis-grid">
      <dt>Лица</dt><dd>${window.format.escapeHtml(analysis.faces_count)}</dd>
      <dt>Закрытые глаза</dt><dd>${window.format.escapeHtml(eyesClosed)}</dd>
      <dt>Резкость (blur_score)</dt>
      <dd>${analysis.blur_score.toFixed(3)} — <strong>${verdict}</strong></dd>
      <dt>is_blurred <span class="muted">(сырое поле анализатора)</span></dt>
      <dd>${analysis.is_blurred ? "true" : "false"}
        <div class="muted small">Вердикт выше считается по нашему порогу
          резкости, не по этому полю (см. design §3.6) - у реального
          анализатора оно ненадёжно.</div>
      </dd>
      <dt>Доминирующий цвет</dt>
      <dd>
        <span class="color-swatch" id="dominant-color-swatch"></span>
        ${window.format.escapeHtml(analysis.dominant_color || "—")}
      </dd>
      <dt>Теги</dt><dd>${window.format.tagsChipsHtml(analysis.tags)}</dd>
      <dt>Perceptual hash</dt>
      <dd><code>${window.format.escapeHtml(analysis.perceptual_hash)}</code></dd>
      <dt>Версия модели</dt><dd>${window.format.escapeHtml(analysis.model_version || "—")}</dd>
    </dl>
  `;
}

async function render() {
  const id = currentPhotoId();
  const container = document.getElementById("photo-detail");
  const errorBox = document.getElementById("photo-error");
  errorBox.textContent = "";
  if (!id) {
    errorBox.textContent = "Не указан id фото в адресе страницы";
    container.innerHTML = "";
    return null;
  }
  try {
    const photo = await window.api.getPhoto(id);
    document.getElementById("photo-title").textContent = photo.filename;
    document.title = `Фото — ${photo.filename}`;
    const imgUrl = window.api.photoContentUrl(id);
    // The public error format doesn't expose `last_error_code` (design
    // §8.3 explicitly keeps the response contract unchanged) - for a
    // failed photo we can only point at the service logs.
    const failedHint =
      photo.status === "failed"
        ? '<p class="muted">Анализ не удался. Подробности смотрите в логах сервиса (worker).</p>'
        : "";
    container.innerHTML = `
      <img class="photo-full" src="${imgUrl}" alt="${window.format.escapeHtml(photo.filename)}" />
      <div class="photo-info">
        ${window.format.statusBadgeHtml(photo.status)}
        ${failedHint}
        ${renderAnalysis(photo.analysis)}
      </div>
    `;
    // Set the swatch color via the DOM style API rather than interpolating
    // `dominant_color` into a CSS string (design review-2 NOTE, review-1
    // BLOCKING-3): the browser treats `element.style.backgroundColor` as a
    // property assignment, not markup, so there is no CSS/HTML injection
    // surface even if the analyzer ever returns something unexpected.
    const swatch = document.getElementById("dominant-color-swatch");
    if (swatch && photo.analysis) {
      swatch.style.backgroundColor = window.format.safeCssColor(
        photo.analysis.dominant_color,
        "#cccccc"
      );
    }
    return photo;
  } catch (err) {
    errorBox.textContent = window.describeApiError(err);
    return null;
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (document.hidden || pollIterations >= POLL_MAX_ITERATIONS) {
    return;
  }
  pollTimer = setTimeout(async () => {
    pollIterations += 1;
    const photo = await render();
    if (photo && !window.format.isTerminalPhotoStatus(photo.status)) {
      schedulePoll();
    }
  }, pollIntervalMs());
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    schedulePoll();
  }
});

(async function init() {
  const photo = await render();
  if (photo && !window.format.isTerminalPhotoStatus(photo.status)) {
    schedulePoll();
  }
})();
