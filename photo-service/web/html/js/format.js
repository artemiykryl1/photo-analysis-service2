/**
 * Formatting helpers shared by gallery/photo/batch screens: status badges,
 * the sharpness verdict, and tag chips.
 *
 * TASK-003 design §3.6/§8.3: the sharp/blurry VERDICT shown in the UI is
 * computed from `blur_score` against `APP_CONFIG.sharpnessThreshold` - the
 * raw `is_blurred` field from the analyzer is displayed separately, as-is,
 * because it does not drive the "best photo" choice either (it was `True`
 * in every spike measurement, including the sharpest frame).
 */

const STATUS_LABELS = {
  pending: "В очереди",
  processing: "Обрабатывается",
  done: "Готово",
  failed: "Ошибка",
  completed: "Завершён",
};

function escapeHtml(value) {
  // Serializing a text node's `innerHTML` (the previous implementation)
  // only escapes `&`, `<`, `>` and U+00A0 per the HTML spec - it does NOT
  // escape `"` or `'`, because those only matter in attribute-value
  // serialization, not text-node serialization. Every call site here
  // interpolates the escaped value into double-quoted HTML attributes
  // (`alt="..."`, `title="..."`, `style="..."`), so an unescaped `"` in
  // user-controlled input (e.g. `filename`) breaks out of the attribute
  // and injects arbitrary markup - a stored XSS (TASK-003 review-1
  // BLOCKING-3). Escape all five characters explicitly instead.
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isValidCssColor(value) {
  // Defense in depth for `dominant_color`, which comes from the (untrusted,
  // per constitution.md §2.1) external analyzer: only accept the exact
  // `#RGB`/`#RRGGBB`/`#RRGGBBAA` hex-color shape it is documented to return.
  // Anything else falls back to a neutral placeholder rather than being
  // interpolated into CSS at all.
  return typeof value === "string" && /^#[0-9a-fA-F]{3,8}$/.test(value);
}

function safeCssColor(value, fallback) {
  return isValidCssColor(value) ? value : fallback || "#cccccc";
}

function statusBadgeHtml(status) {
  const label = STATUS_LABELS[status] || status;
  return `<span class="badge badge-${escapeHtml(status)}">${escapeHtml(label)}</span>`;
}

function isTerminalPhotoStatus(status) {
  return status === "done" || status === "failed";
}

function sharpnessThreshold() {
  const configured = window.APP_CONFIG && window.APP_CONFIG.sharpnessThreshold;
  return typeof configured === "number" ? configured : 100.0;
}

function sharpnessVerdict(blurScore) {
  if (blurScore === null || blurScore === undefined) {
    return "нет данных";
  }
  return blurScore >= sharpnessThreshold() ? "резкое" : "размытое";
}

function tagsChipsHtml(tags) {
  if (!tags || tags.length === 0) {
    return '<span class="muted">нет тегов</span>';
  }
  return tags.map((tag) => `<span class="chip">${escapeHtml(tag)}</span>`).join(" ");
}

window.format = {
  statusBadgeHtml,
  isTerminalPhotoStatus,
  sharpnessVerdict,
  tagsChipsHtml,
  escapeHtml,
  safeCssColor,
};
