/**
 * Restore items previously trashed by apply.js.
 *
 * Usage:
 *   window.__GPDEDUPE_UNDO__ = <contents of undo_*.json>
 *   Paste this script into the DevTools console on photos.google.com
 */
(async function photoOrganiserRestore() {
  if (typeof gptkApi === "undefined") {
    console.error("[gpdedupe] gptkApi not found. Install GPTK and reload.");
    return;
  }

  const UNDO = window.__GPDEDUPE_UNDO__ || null;
  if (!UNDO || !Array.isArray(UNDO.items) || !UNDO.items.length) {
    console.error(
      "[gpdedupe] Set window.__GPDEDUPE_UNDO__ to the undo_*.json payload and re-run."
    );
    return;
  }

  const batchSize = 50;
  const items = UNDO.items.filter((x) => !x.restored);
  console.log(`[gpdedupe] Restoring ${items.length} items from run ${UNDO.run_id}…`);

  let ok = 0;
  for (let i = 0; i < items.length; i += batchSize) {
    const batch = items.slice(i, i + batchSize);
    const keys = batch.map((x) => x.dedup_key).filter(Boolean);
    try {
      await gptkApi.restoreFromTrash(keys);
      ok += keys.length;
      console.log(`[gpdedupe] restored ${ok} / ${items.length}`);
    } catch (err) {
      console.error(`[gpdedupe] restore batch at ${i} failed:`, err);
    }
    await new Promise((r) => setTimeout(r, 300));
  }

  console.log(`[gpdedupe] Restore finished. ${ok} items restored.`);
})();
