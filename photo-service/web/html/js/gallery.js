/**
 * Gallery screen (index.html): photo list with previews/statuses, single +
 * batch upload forms, and status polling.
 *
 * TASK-003 B4 (design §8.3): polling is a setTimeout CHAIN, not
 * setInterval - a slow API response must never let requests pile up. It
 * only reschedules itself while at least one photo is non-terminal, pauses
 * while the tab is hidden (`document.hidden`), and gives up after
 * POLL_MAX_ITERATIONS (~5 minutes at the default 3s interval), at which
 * point a manual "Обновить" button takes over.
 */

const POLL_MAX_ITERATIONS = 100;

let pollTimer = null;
let pollIterations = 0;
let lastPhotos = [];

function pollIntervalMs() {
  return (window.APP_CONFIG && window.APP_CONFIG.pollIntervalMs) || 3000;
}

function renderPhotoCard(photo) {
  const imgUrl = window.api.photoContentUrl(photo.id);
  return `
    <a class="photo-card" href="photo.html?id=${encodeURIComponent(photo.id)}">
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

async function refreshGallery() {
  const container = document.getElementById("gallery");
  const errorBox = document.getElementById("gallery-error");
  errorBox.textContent = "";
  try {
    const photos = await window.api.listPhotos(50, 0);
    lastPhotos = photos;
    container.innerHTML = photos.length
      ? photos.map(renderPhotoCard).join("")
      : '<p class="muted">Пока нет загруженных фото.</p>';
    return photos;
  } catch (err) {
    errorBox.textContent = window.describeApiError(err);
    return lastPhotos;
  }
}

function hasPendingPhotos(photos) {
  return photos.some((p) => !window.format.isTerminalPhotoStatus(p.status));
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (document.hidden) {
    // Re-armed by the visibilitychange listener below once the tab is
    // visible again - don't burn an iteration budget while hidden.
    return;
  }
  if (pollIterations >= POLL_MAX_ITERATIONS) {
    document.getElementById("refresh-button").hidden = false;
    return;
  }
  pollTimer = setTimeout(async () => {
    pollIterations += 1;
    const photos = await refreshGallery();
    if (hasPendingPhotos(photos)) {
      schedulePoll();
    }
  }, pollIntervalMs());
}

function startPollingIfNeeded(photos) {
  pollIterations = 0;
  document.getElementById("refresh-button").hidden = true;
  if (hasPendingPhotos(photos)) {
    schedulePoll();
  }
}

function setupUploadForms() {
  const singleForm = document.getElementById("upload-single-form");
  const singleError = document.getElementById("upload-single-error");
  singleForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    singleError.textContent = "";
    const input = document.getElementById("single-file-input");
    const file = input.files[0];
    if (!file) {
      singleError.textContent = "Выберите файл";
      return;
    }
    try {
      await window.api.uploadPhoto(file);
      input.value = "";
      startPollingIfNeeded(await refreshGallery());
    } catch (err) {
      singleError.textContent = window.describeApiError(err);
    }
  });

  const batchForm = document.getElementById("upload-batch-form");
  const batchError = document.getElementById("upload-batch-error");
  batchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    batchError.textContent = "";
    const input = document.getElementById("batch-file-input");
    const files = Array.from(input.files || []);
    // Client-side check before sending anything (design §8.3: "клиентская
    // проверка количества до отправки") - the server re-validates anyway
    // (INVALID_BATCH_SIZE, 400), this just avoids an obviously-doomed upload.
    if (files.length < 2 || files.length > 10) {
      batchError.textContent = "В батче должно быть от 2 до 10 файлов";
      return;
    }
    try {
      const result = await window.api.uploadBatch(files);
      input.value = "";
      window.location.href = `batch.html?id=${encodeURIComponent(result.batch_id)}`;
    } catch (err) {
      batchError.textContent = window.describeApiError(err);
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    schedulePoll();
  }
});

document.getElementById("refresh-button").addEventListener("click", async () => {
  document.getElementById("refresh-button").hidden = true;
  startPollingIfNeeded(await refreshGallery());
});

(async function init() {
  setupUploadForms();
  startPollingIfNeeded(await refreshGallery());
})();
