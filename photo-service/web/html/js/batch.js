/**
 * Batch screen (batch.html?id=...): every photo of the batch plus a
 * highlight of `best_photo_id`. Polls while `status === "processing"`.
 */

const POLL_MAX_ITERATIONS = 100;

let pollTimer = null;
let pollIterations = 0;

function currentBatchId() {
  return new URLSearchParams(window.location.search).get("id");
}

function pollIntervalMs() {
  return (window.APP_CONFIG && window.APP_CONFIG.pollIntervalMs) || 3000;
}

function renderPhotoTile(photo, bestPhotoId) {
  const isBest = bestPhotoId != null && photo.photo_id === bestPhotoId;
  const imgUrl = window.api.photoContentUrl(photo.photo_id);
  return `
    <a class="batch-photo ${isBest ? "best" : ""}" href="photo.html?id=${encodeURIComponent(photo.photo_id)}">
      ${isBest ? '<span class="badge badge-best">Лучший кадр</span>' : ""}
      <div class="photo-thumb">
        <img src="${imgUrl}" alt="${window.format.escapeHtml(photo.filename)}" loading="lazy" />
      </div>
      <div class="photo-meta">
        <span class="filename" title="${window.format.escapeHtml(photo.filename)}">${window.format.escapeHtml(photo.filename)}</span>
        ${window.format.statusBadgeHtml(photo.status)}
      </div>
    </a>
  `;
}

async function render() {
  const id = currentBatchId();
  const container = document.getElementById("batch-photos");
  const summary = document.getElementById("batch-summary");
  const errorBox = document.getElementById("batch-error");
  errorBox.textContent = "";
  if (!id) {
    errorBox.textContent = "Не указан id батча в адресе страницы";
    return null;
  }
  try {
    const batch = await window.api.getBatch(id);
    document.title = `Батч ${batch.batch_id}`;
    let summaryHtml = `
      ${window.format.statusBadgeHtml(batch.status)}
      <span class="muted">${batch.photos.length} фото</span>
    `;
    if (batch.status === "completed" && !batch.best_photo_id) {
      summaryHtml += '<p class="muted">Ни один кадр не проанализирован успешно.</p>';
    }
    summary.innerHTML = summaryHtml;
    container.innerHTML = batch.photos
      .map((photo) => renderPhotoTile(photo, batch.best_photo_id))
      .join("");
    return batch;
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
    const batch = await render();
    if (batch && batch.status === "processing") {
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
  const batch = await render();
  if (batch && batch.status === "processing") {
    schedulePoll();
  }
})();
