/**
 * Google Photos library census for photo-organiser.
 *
 * Prerequisites:
 *   1. Install Tampermonkey
 *   2. Install Google Photos Toolkit (GPTK):
 *      https://github.com/xob0t/Google-Photos-Toolkit/releases
 *   3. Open https://photos.google.com and stay logged in
 *
 * Usage:
 *   - Open DevTools console (F12 / Cmd+Option+J)
 *   - Paste this entire file and press Enter
 *   - Wait until it finishes (progress logs in the console)
 *   - A census.jsonl file downloads automatically
 *   - Move it to your GPDEDUPE data root (default C:\gpdedupe\census.jsonl
 *     on Windows, ~/.gpdedupe/census.jsonl on macOS/Linux)
 *   - Run: photo-organiser census import
 *
 * Relies on window.gptkApi exported by GPTK.
 */
(async function photoOrganiserCensus() {
  if (typeof gptkApi === "undefined") {
    console.error(
      "[gpdedupe] gptkApi not found. Install Google Photos Toolkit (Tampermonkey) and reload photos.google.com."
    );
    return;
  }

  const PAGE_SIZE = 500;
  const SOURCE = "library"; // library | archive | both
  // Album scan can take a long time on huge libraries; set false to skip.
  const SCAN_ALBUMS = true;
  const lines = [];
  let pageId = null;
  let timestamp = null;
  let total = 0;
  let pages = 0;

  console.log("[gpdedupe] Recording storage quota…");
  try {
    const quota = await gptkApi.getStorageQuota();
    lines.push(
      JSON.stringify({
        _type: "quota",
        totalUsed: quota?.totalUsed ?? null,
        totalAvailable: quota?.totalAvailable ?? null,
        usedByGPhotos: quota?.usedByGPhotos ?? null,
        recordedAt: new Date().toISOString(),
      })
    );
  } catch (err) {
    console.warn("[gpdedupe] getStorageQuota failed:", err);
  }

  lines.push(
    JSON.stringify({
      _type: "meta",
      startedAt: new Date().toISOString(),
      source: SOURCE,
      userAgent: navigator.userAgent,
    })
  );

  console.log("[gpdedupe] Enumerating library via getItemsByTakenDate…");

  while (true) {
    const page = await gptkApi.getItemsByTakenDate(timestamp, SOURCE, pageId, PAGE_SIZE, true);
    const items = page?.items || [];
    if (!items.length) break;

    for (const item of items) {
      lines.push(
        JSON.stringify({
          media_key: item.mediaKey,
          dedup_key: item.dedupKey,
          timestamp: item.timestamp ?? null,
          timezone_offset: item.timezoneOffset ?? null,
          creation_timestamp: item.creationTimestamp ?? null,
          thumb: item.thumb ?? null,
          res_width: item.resWidth ?? null,
          res_height: item.resHeight ?? null,
          is_favorite: !!item.isFavorite,
          is_archived: !!item.isArchived,
          is_live_photo: !!item.isLivePhoto,
          is_owned: item.isOwned !== false,
          duration: item.duration ?? null,
          description_short: item.descriptionShort ?? null,
          in_album: false,
        })
      );
    }

    total += items.length;
    pages += 1;
    console.log(`[gpdedupe] page ${pages}: +${items.length} (total ${total})`);

    pageId = page.nextPageId ?? null;
    timestamp = page.lastItemTimestamp ?? timestamp;
    if (!pageId) break;

    // Be polite to Google's RPC endpoint
    await new Promise((r) => setTimeout(r, 200));
  }

  console.log("[gpdedupe] Done enumerating library. Scanning albums for membership…");
  const inAlbum = new Set();
  if (SCAN_ALBUMS) {
  try {
    let albumPageId = null;
    while (true) {
      const albumsPage = await gptkApi.getAlbums(albumPageId, 100, true);
      const albums = albumsPage?.items || [];
      if (!albums.length) break;
      for (const album of albums) {
        let itemPageId = null;
        do {
          const albumItems = await gptkApi.getAlbumPage(
            album.mediaKey,
            itemPageId,
            album.authKey || null,
            true
          );
          for (const it of albumItems?.items || []) {
            if (it.mediaKey) inAlbum.add(it.mediaKey);
          }
          itemPageId = albumItems?.nextPageId ?? null;
          await new Promise((r) => setTimeout(r, 150));
        } while (itemPageId);
      }
      albumPageId = albumsPage.nextPageId ?? null;
      if (!albumPageId) break;
      await new Promise((r) => setTimeout(r, 200));
    }
    console.log(`[gpdedupe] Album members flagged: ${inAlbum.size}`);
  } catch (err) {
    console.warn("[gpdedupe] Album scan failed (continuing):", err);
  }
  } else {
    console.log("[gpdedupe] Album scan skipped (SCAN_ALBUMS=false).");
  }

  // Rewrite item lines with in_album — rebuild from parsed lines for simplicity
  const out = [];
  for (const line of lines) {
    const obj = JSON.parse(line);
    if (obj.media_key && inAlbum.has(obj.media_key)) {
      obj.in_album = true;
    }
    out.push(JSON.stringify(obj));
  }

  out.push(
    JSON.stringify({
      _type: "meta",
      finishedAt: new Date().toISOString(),
      itemCount: total,
      pages,
      albumMembers: inAlbum.size,
    })
  );

  const blob = new Blob([out.join("\n") + "\n"], { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "census.jsonl";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.log(`[gpdedupe] Done. ${total} items across ${pages} pages. census.jsonl downloaded.`);
})();
