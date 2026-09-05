/**
 * Apply approved trash decisions for photo-organiser.
 *
 * Prerequisites: GPTK loaded on photos.google.com
 *
 * Usage:
 *   1. Run: photo-organiser apply export
 *      → writes decisions_to_trash.json into the data root
 *   2. Open the file, copy its contents into TRASH_PAYLOAD below
 *      (or set window.__GPDEDUPE_TRASH__ = {...} before pasting)
 *   3. Paste this script into the DevTools console on photos.google.com
 *
 * Moves items to Google Photos trash (recoverable ~60 days).
 * Writes a local undo payload you can feed to restore.js.
 */
(async function photoOrganiserApply() {
  if (typeof gptkApi === "undefined") {
    console.error("[gpdedupe] gptkApi not found. Install GPTK and reload.");
    return;
  }

  // Prefer payload injected by the user / helper:
  //   window.__GPDEDUPE_TRASH__ = { run_id, items: [{media_key, dedup_key}] }
  const TRASH_PAYLOAD = window.__GPDEDUPE_TRASH__ || null;

  if (!TRASH_PAYLOAD || !Array.isArray(TRASH_PAYLOAD.items) || !TRASH_PAYLOAD.items.length) {
    console.error(
      "[gpdedupe] No payload. Run `photo-organiser apply export`, then set:\n" +
        "  window.__GPDEDUPE_TRASH__ = <contents of decisions_to_trash.json>\n" +
        "and re-run this script."
    );
    return;
  }

  const runId = TRASH_PAYLOAD.run_id || `run-${Date.now()}`;
  const batchSize = 50;
  const trashed = [];
  const items = TRASH_PAYLOAD.items;

  console.log(`[gpdedupe] Trashing ${items.length} items (run ${runId})…`);

  for (let i = 0; i < items.length; i += batchSize) {
    const batch = items.slice(i, i + batchSize);
    const keys = batch.map((x) => x.dedup_key).filter(Boolean);
    if (!keys.length) continue;
    try {
      await gptkApi.moveItemsToTrash(keys);
      for (const item of batch) {
        trashed.push({
          media_key: item.media_key,
          dedup_key: item.dedup_key,
          trashed_at: new Date().toISOString(),
        });
      }
      console.log(`[gpdedupe] trashed ${Math.min(i + batchSize, items.length)} / ${items.length}`);
    } catch (err) {
      console.error(`[gpdedupe] batch starting at ${i} failed:`, err);
    }
    await new Promise((r) => setTimeout(r, 300));
  }

  const undo = {
    run_id: runId,
    created_at: new Date().toISOString(),
    items: trashed,
  };
  const blob = new Blob([JSON.stringify(undo, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `undo_${runId}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.log(
    `[gpdedupe] Done. ${trashed.length} moved to trash. Undo file downloaded — keep it for restore.js.`
  );
})();
