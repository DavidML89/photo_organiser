/**
 * Fallback: download fresh 256px thumbnails while logged into Google Photos.
 *
 * Prefer first: export ALL cookies from photos.google.com, then
 *   uv run photo-organiser fetch thumbs --cookies ~/.gpdedupe/cookies.txt
 *
 * Use this script only if cookies still return HTML/403.
 *
 * Prerequisites:
 *   1. Tampermonkey + Google Photos Toolkit (gptkApi)
 *   2. Open https://photos.google.com (logged in)
 *
 * Usage:
 *   - Paste this file into the DevTools console and press Enter
 *   - Zip batches download automatically (thumbs_batch_0001.zip, …)
 *   - Move the zips into reach of WSL, then:
 *       uv run photo-organiser fetch thumbs --import-zips ~/Downloads
 *
 * Tip: set LIMIT = 20 for a smoke test before the full library run.
 */
(async function photoOrganiserFetchThumbs() {
  if (typeof gptkApi === "undefined") {
    console.error(
      "[gpdedupe] gptkApi not found. Install Google Photos Toolkit and reload photos.google.com."
    );
    return;
  }

  const SIZE = 256;
  const PAGE_SIZE = 500;
  const SOURCE = "library"; // library | archive | both
  const BATCH_ZIP = 400; // images per zip
  const CONCURRENCY = 8;
  const SKIP_VIDEOS = true;
  // Set to a number for a smoke test, or null for the full library.
  const LIMIT = null;

  function sizedUrl(thumb) {
    if (!thumb) return null;
    const base = thumb.split("=")[0].split("?")[0];
    return `${base}=w${SIZE}-h${SIZE}-k-no?authuser=0`;
  }

  // Keep mediaKey intact so Python can map the file back to the DB row.
  function fileName(mediaKey) {
    return String(mediaKey).replace(/[\/\\?%*:|"<>]/g, "_") + ".jpg";
  }

  function isProbablyImage(buf) {
    if (!buf || buf.byteLength < 12) return false;
    const u = new Uint8Array(buf);
    if (u[0] === 0xff && u[1] === 0xd8 && u[2] === 0xff) return true;
    if (u[0] === 0x89 && u[1] === 0x50 && u[2] === 0x4e && u[3] === 0x47) return true;
    if (
      u[0] === 0x52 &&
      u[1] === 0x49 &&
      u[2] === 0x46 &&
      u[3] === 0x46 &&
      u[8] === 0x57 &&
      u[9] === 0x45 &&
      u[10] === 0x42 &&
      u[11] === 0x50
    )
      return true;
    return false;
  }

  async function loadJSZip() {
    if (window.JSZip) return window.JSZip;
    await new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js";
      s.onload = resolve;
      s.onerror = () => reject(new Error("Failed to load JSZip from CDN"));
      document.head.appendChild(s);
    });
    return window.JSZip;
  }

  function downloadBlob(blob, filename) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30_000);
  }

  async function fetchThumb(url) {
    const resp = await fetch(url, {
      credentials: "include",
      referrer: "https://photos.google.com/",
      headers: { Accept: "image/*,*/*;q=0.8" },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const buf = await resp.arrayBuffer();
    if (!isProbablyImage(buf)) {
      const head = new TextDecoder().decode(buf.slice(0, 40));
      throw new Error(`not an image: ${head}`);
    }
    return buf;
  }

  const JSZip = await loadJSZip();
  console.log("[gpdedupe] Enumerating library and downloading thumbs…");

  let pageId = null;
  let timestamp = null;
  let totalSeen = 0;
  let totalOk = 0;
  let totalFail = 0;
  let batchIdx = 1;
  let zip = new JSZip();
  let inZip = 0;
  let stop = false;

  async function flushZip() {
    if (inZip === 0) return;
    console.log(
      `[gpdedupe] Writing thumbs_batch_${String(batchIdx).padStart(4, "0")}.zip (${inZip} images)…`
    );
    const blob = await zip.generateAsync({ type: "blob", compression: "STORE" });
    downloadBlob(blob, `thumbs_batch_${String(batchIdx).padStart(4, "0")}.zip`);
    batchIdx += 1;
    zip = new JSZip();
    inZip = 0;
    await new Promise((r) => setTimeout(r, 1500));
  }

  async function processItems(items) {
    const queue = items.filter((it) => {
      if (!it.thumb) return false;
      if (SKIP_VIDEOS && it.duration) return false;
      return true;
    });

    let i = 0;
    async function worker() {
      while (i < queue.length && !stop) {
        const idx = i++;
        const it = queue[idx];
        if (LIMIT != null && totalOk >= LIMIT) {
          stop = true;
          return;
        }
        try {
          const buf = await fetchThumb(sizedUrl(it.thumb));
          zip.file(fileName(it.mediaKey), buf);
          inZip += 1;
          totalOk += 1;
          if (inZip >= BATCH_ZIP) await flushZip();
        } catch (err) {
          totalFail += 1;
          if (totalFail <= 8) {
            console.warn(`[gpdedupe] fail ${it.mediaKey}: ${err.message || err}`);
          }
          if (totalOk === 0 && totalFail >= 10) {
            stop = true;
            console.error(
              "[gpdedupe] First 10 thumbs failed. Stay on the photos.google.com tab " +
                "(CORS). Confirm GPTK works, then retry with LIMIT=20."
            );
          }
        }
      }
    }

    await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));
  }

  while (!stop) {
    // Same signature as browser/census.js
    const page = await gptkApi.getItemsByTakenDate(
      timestamp,
      SOURCE,
      pageId,
      PAGE_SIZE,
      true
    );
    const items = page?.items || [];
    if (!items.length) break;

    totalSeen += items.length;
    console.log(
      `[gpdedupe] page +${items.length} (seen ${totalSeen}, ok ${totalOk}, fail ${totalFail})`
    );
    await processItems(items);

    pageId = page.nextPageId ?? null;
    timestamp = page.lastItemTimestamp ?? timestamp;
    if (!pageId) break;
    if (LIMIT != null && totalOk >= LIMIT) break;
    await new Promise((r) => setTimeout(r, 200));
  }

  await flushZip();
  console.log(
    `[gpdedupe] Done. seen=${totalSeen} ok=${totalOk} fail=${totalFail} batches=${batchIdx - 1}`
  );
  console.log(
    "[gpdedupe] Next: move thumbs_batch_*.zip into reach of WSL, then:\n" +
      "  uv run photo-organiser fetch thumbs --import-zips ~/Downloads"
  );
})();
