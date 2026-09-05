/**
 * Optional: stage approved trash candidates into a Google Photos album
 * so you can eyeball them in the native UI before running apply.js.
 *
 * Usage:
 *   window.__GPDEDUPE_TRASH__ = <decisions_to_trash.json>
 *   window.__GPDEDUPE_STAGE_ALBUM__ = "gpdedupe-review-" + Date.now()  // optional name
 *   Paste this script on photos.google.com with GPTK loaded.
 */
(async function photoOrganiserStage() {
  if (typeof gptkApi === "undefined") {
    console.error("[gpdedupe] gptkApi not found.");
    return;
  }
  const payload = window.__GPDEDUPE_TRASH__;
  if (!payload?.items?.length) {
    console.error("[gpdedupe] Set window.__GPDEDUPE_TRASH__ first (from apply export).");
    return;
  }
  const name = window.__GPDEDUPE_STAGE_ALBUM__ || `gpdedupe-review-${Date.now()}`;
  const mediaKeys = payload.items.map((x) => x.media_key).filter(Boolean);
  console.log(`[gpdedupe] Creating album "${name}" with ${mediaKeys.length} items…`);

  const batchSize = 100;
  let albumKey = null;
  for (let i = 0; i < mediaKeys.length; i += batchSize) {
    const batch = mediaKeys.slice(i, i + batchSize);
    if (!albumKey) {
      await gptkApi.addItemsToAlbum(batch, null, name);
      // Fetch albums to resolve the new key is optional; subsequent batches use name path
      // GPTK addItemsToAlbum with albumName creates on first call — later use mediaKey if known
      const albums = await gptkApi.getAlbums(null, 20, true);
      const created = (albums?.items || []).find((a) => a.title === name);
      albumKey = created?.mediaKey || null;
    } else {
      await gptkApi.addItemsToAlbum(batch, albumKey, null);
    }
    console.log(`[gpdedupe] staged ${Math.min(i + batchSize, mediaKeys.length)} / ${mediaKeys.length}`);
    await new Promise((r) => setTimeout(r, 300));
  }
  console.log(`[gpdedupe] Staging done. Open album "${name}" in Google Photos to review.`);
})();
